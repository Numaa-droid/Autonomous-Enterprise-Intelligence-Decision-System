"""The GPU server: app.py's detection (app.run) and recognition (app.recognise) jobs on a cloud GPU, with every
model they use -- Table Transformer detection + docling-layout-heron, the fine-tuned Table Transformer
(models/tatr-finetuned-01-11) beside v1.1-all, and PaddleOCR-VL-1.6 (llama-server inside the same machine, for
sideways and scanned tables and charts). The UI stays local (http://127.0.0.1:8000, results in output/ here); app.py
uses this server when it is deployed and reachable, else runs every job on the local computer.

    pip install modal && modal setup                # once: the `modal` package and an account token
    python -m modal deploy gpu_server.py            # once, and again after a code or model change
    python app.py                                   # the UI; its jobs go to the GPU server
    python -m modal app stop tablelens-serve        # take it down
"""

import gzip
import logging
import queue
import threading
from pathlib import Path

import modal

EVENT_BATCH = 100  # events per queue write at most (app.on_gpu reads 100 at a time)


def forward(evs, put_many, size=EVENT_BATCH):
    """Every item of evs passed on to put_many(list) in order, in batches. A background thread sends what has piled
    up while its previous write was on the network, so the job never waits for one: a write per event (a ~90 ms
    round-trip to Modal's queue) was most of a detection's time. A failed write is raised here."""
    pending, failed = queue.Queue(), []

    def send():
        done = False
        while not done:
            batch = [pending.get()]
            while len(batch) < size and batch[-1] is not None:
                try:
                    batch.append(pending.get_nowait())
                except queue.Empty:
                    break
            done = batch[-1] is None  # the end mark is the last item put
            try:
                if batch := [b for b in batch if b is not None]:
                    put_many(batch)
            except Exception as e:
                failed.append(e)
                return

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    try:
        for ev in evs:
            if failed:
                break
            pending.put(ev)
    finally:  # the events so far are sent when the job fails too, as the error event is one of them
        pending.put(None)
        sender.join()
    if failed:
        raise failed[0]

ROOT = Path(__file__).parent
MODEL_DIR = "models/tatr-finetuned-01-11"  # content.STRUCTURE_MODEL
app = modal.App("tablelens-serve")  # app.SERVER
# first free one. No T4: it fits every model too but ran the same jobs several times slower (one at 1 s a page against
# 0.2 s on an A10G)
GPUS = ["A10G", "L4"]
# llama.cpp's CUDA server image: llama-server (in /app) for PaddleOCR-VL; Python and the pipeline's packages on top
# the base pinned by digest, the same as the Dockerfile's (server-cuda as of 2026-11-04)
image = (modal.Image.from_registry("ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:"
                                   "991de01d6b6ea4e17374c03e5b66810858ecac5660eaf3adf9922620696d8138", add_python="3.11")
         .entrypoint([])  # its entrypoint is llama-server itself; content.vlm_server() starts it instead
         .env({"PATH": "/app:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"})  # llama-server on PATH
         .apt_install("libgl1", "libglib2.0-0")  # OpenCV, under paddleocr
         .pip_install_from_requirements(str(ROOT / "requirements.txt"))  # the one pin list, the Dockerfile's too
         # the Hugging Face models baked into the image: no download on every cold start. The detection model is
         # loaded once here too, so the timm backbone weights it fetches are in the image as well
         .run_commands("python -c \"from huggingface_hub import snapshot_download as d, hf_hub_download as f; "
                       "[d(m) for m in ('microsoft/table-transformer-structure-recognition-v1.1-all', "
                       "'microsoft/table-transformer-detection', 'docling-project/docling-layout-heron', "
                       "'docling-project/DocumentFigureClassifier-v2.5')]; "
                       "[f('PaddlePaddle/PaddleOCR-VL-1.6-GGUF', n) for n in "
                       "('PaddleOCR-VL-1.6-GGUF.gguf', 'PaddleOCR-VL-1.6-GGUF-mmproj.gguf')]; "
                       "from transformers import TableTransformerForObjectDetection as T; "
                       "T.from_pretrained('microsoft/table-transformer-detection')\"")
         .add_local_dir(ROOT / MODEL_DIR, f"/root/{MODEL_DIR}")
         .add_local_python_source("app", "detect", "content", "structure_tatr", "structure",
                                  "spotting_check"))


# scaledown_window: the GPU stays up 10 min after the last job, so recognition after a detection skips the cold start
# cpu: reserved cores. Without it the container had 1 core, and llama-server (PaddleOCR-VL) alone kept it at ~99%
# while Table Transformer, rendering and the PDF text waited for it
@app.cls(image=image, gpu=GPUS, cpu=4.0, scaledown_window=600, timeout=24 * 3600)
class Pipeline:
    @modal.enter()
    def load(self):
        import app as web
        web.to_gpu()  # every model on the GPU, PaddleOCR-VL started

    @modal.method()
    def job(self, kind: str, pdf: bytes, name: str, files: dict, args: dict, key: str):
        """app.py's job on the GPU: kind "detect" is app.run(name, threshold, dpi), "recognise" is
        app.recognise(mode, pages). files: the output/ files it reads ({path in output/: gzipped bytes}), written here
        first.
        Spawned by app.on_gpu: its events go to the queue app.EVENTS, partition key, as they come, so the job runs on
        when the UI's connection to Modal drops. Returns {"path", "data": gzipped} for the JSON it wrote, or None when
        the job failed before writing it."""
        import pymupdf

        import app as web
        events = modal.Queue.from_name(web.EVENTS, create_if_missing=True)
        for path, data in files.items():
            (web.OUT / path).parent.mkdir(parents=True, exist_ok=True)
            (web.OUT / path).write_bytes(gzip.decompress(data))  # app.on_gpu gzips them
        doc, stem = pymupdf.open(stream=pdf, filetype="pdf"), web.safe_stem(name)

        def without_images(evs):
            for ev in evs:
                if ev["type"] == "page":
                    # ~0.5 MB a page: downloading it was the job's speed (Modal -> the UI's PC); on_gpu renders it there
                    ev.pop("image")
                yield ev

        forward(without_images(web.run(doc, name, args["threshold"], args["dpi"], len(pdf)) if kind == "detect"
                               else web.recognise(doc, stem, args["mode"], args["pages"])),
                lambda batch: events.put_many(batch, partition=key))
        out = web.tables_json(stem) if kind == "detect" else web.structure_json(stem)
        if out.exists():  # not when the job failed before writing it
            return {"path": out.relative_to(web.OUT).as_posix(), "data": gzip.compress(out.read_bytes())}


# spotting_check.py on the GPU: its own container (not the Pipeline: none of the UI's jobs wait for it or share its
# llama-server), PaddleOCR-VL started here with the spotting mmproj copy. Its log streams to the terminal that runs
#     python -m modal run gpu_server.py::check_spotting --pdf doc/<name>.pdf --tables 3:1,4:2
# and its files come back to output/<name>/spotting_check/ there.
@app.function(image=image, gpu=GPUS, cpu=4.0, timeout=3600)
def spotting(pdf: bytes, name: str, files: dict, tables: str, coords: str):
    """files: {path in output/: gzipped bytes} (the detection's _tables.json, the _structure.json if there is one).
    Returns {path in output/: gzipped bytes} of everything the check wrote, plus its llama-server log."""
    import app as web
    import spotting_check as check

    for path, data in files.items():
        (web.OUT / path).parent.mkdir(parents=True, exist_ok=True)
        (web.OUT / path).write_bytes(gzip.decompress(data))
    stem = web.safe_stem(name)
    (web.DOC / name).write_bytes(pdf)
    out_dir = web.OUT / stem / "spotting_check"
    out_dir.mkdir(parents=True, exist_ok=True)
    server_log = out_dir / "llama-server.log"
    check.setup_log()
    proc = check.serve(server_log)
    try:
        check.LOG.handlers.clear()  # run() sets the console + run.log handlers itself
        check.run(str(web.DOC / name), check.parse_tables(tables), "http://127.0.0.1:8081", coords)
    finally:
        proc.terminate()
        logging.shutdown()  # run.log flushed before it is read
    return {f.relative_to(web.OUT).as_posix(): gzip.compress(f.read_bytes()) for f in out_dir.iterdir() if f.is_file()}


@app.local_entrypoint()
def check_spotting(pdf: str, tables: str, coords: str = "norm1000"):
    """python -m modal run gpu_server.py::check_spotting --pdf doc/<name>.pdf --tables 3:1,4:2 [--coords pixels]"""
    import app as web

    src = Path(pdf)
    stem = web.safe_stem(src.name)
    files = {f.relative_to(web.OUT).as_posix(): gzip.compress(f.read_bytes())
             for f in (web.tables_json(stem), web.structure_json(stem)) if f.exists()}
    if web.tables_json(stem).relative_to(web.OUT).as_posix() not in files:
        raise SystemExit(f"{web.tables_json(stem)} not found: run the detection first")
    print(f"sending {src.name} + {', '.join(files)} to the GPU · tables {tables}")
    result = spotting.remote(src.read_bytes(), src.name, files, tables, coords)
    for path, data in result.items():
        web.write_atomic(web.inside_out(path), gzip.decompress(data))
    print(f"saved {len(result)} file(s) in {web.OUT / stem / 'spotting_check'}")


if __name__ == "__main__":  # self-check of forward(): python gpu_server.py
    import time
    calls = []
    forward(range(250), lambda b: (time.sleep(0.01), calls.append(b)))
    assert [x for b in calls for x in b] == list(range(250)), "every event, in order"
    assert len(calls) < 250 and max(map(len, calls)) <= EVENT_BATCH, "batched"

    def broken(_):
        raise ConnectionError("queue down")
    try:
        forward(range(5), broken)
        raise AssertionError("a failed write must raise")
    except ConnectionError:
        pass
    print(f"forward ok: 250 events in {len(calls)} writes")
