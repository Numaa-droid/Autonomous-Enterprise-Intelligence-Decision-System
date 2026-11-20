# TableLens (micro-detection): web UI + detection + recognition in one image, the same stack as the Modal GPU server
# (gpu_server.py) -- llama.cpp's CUDA server image (llama-server runs PaddleOCR-VL-1.6 inside the container, started
# by the app), Python and the same pinned packages on top, every model baked in (no download at start).
#   docker compose up -d --build          (docker-compose.yml: GPU, http://127.0.0.1:8000, doc/ + output/ mounted)
#   without compose: docker run -p 127.0.0.1:8000:8000 ...  (the UI has no login: never -p 8000:8000, that is the network)
# CPU only (slow): --build-arg BASE=ghcr.io/ggml-org/llama.cpp:server@sha256:ba68b6996ba6d27a49a9864452194128e118a1fcb96dd663f223a13e0a2857a8 --build-arg MODELS_ON_GPU=0
# the base pinned by digest (server-cuda as of 2026-11-04; gpu_server.py uses the same): a rebuild gets the same
# llama.cpp and system Python. Moving to a newer llama.cpp is a deliberate change of both digests.
ARG BASE=ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:991de01d6b6ea4e17374c03e5b66810858ecac5660eaf3adf9922620696d8138
FROM ${BASE}
ARG MODELS_ON_GPU=1

# libgl1/libglib2.0-0: OpenCV (under paddleocr) does not import without them
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
# llama-server is in /app in the base image: content.vlm_server() finds it on PATH
ENV PATH=/opt/venv/bin:/app:$PATH PIP_NO_CACHE_DIR=1

# the one pin list, the GPU server's too (gpu_server.py)
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Models baked in: the Hugging Face ones (+ the timm backbone the detection model fetches on its first load), the
# PaddleOCR-VL GGUF files, and PaddleOCR's page-orientation classifier
RUN python -c "from huggingface_hub import snapshot_download as d, hf_hub_download as f; \
[d(m) for m in ('microsoft/table-transformer-structure-recognition-v1.1-all', 'microsoft/table-transformer-detection', \
'docling-project/docling-layout-heron', 'docling-project/DocumentFigureClassifier-v2.5')]; \
[f('PaddlePaddle/PaddleOCR-VL-1.6-GGUF', n) for n in ('PaddleOCR-VL-1.6-GGUF.gguf', 'PaddleOCR-VL-1.6-GGUF-mmproj.gguf')]; \
from transformers import TableTransformerForObjectDetection as T; T.from_pretrained('microsoft/table-transformer-detection')" \
    && python -c "from paddleocr import DocImgOrientationClassification as D; D(model_name='PP-LCNet_x1_0_doc_ori')"

# the code + the fine-tuned Table Transformer (.dockerignore keeps doc/, output/, train/, ... out)
WORKDIR /srv/tablelens
COPY . .
RUN python -c "import app, content; app.selfcheck(); content.selfcheck(); print('self-checks ok')"

# MODELS_ON_GPU=1: every model on the GPU and llama-server started before the first job (app.to_gpu)
ENV MODELS_ON_GPU=${MODELS_ON_GPU}
EXPOSE 8000
# the app, not the base image's check of llama-server on :8080 (start period: models onto the GPU, llama-server up)
HEALTHCHECK --interval=30s --timeout=10s --start-period=10m --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=8)"
# the base image's entrypoint is llama-server itself; the app starts it instead
ENTRYPOINT []
# one worker: the jobs live in this process's memory (app.JOBS)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
