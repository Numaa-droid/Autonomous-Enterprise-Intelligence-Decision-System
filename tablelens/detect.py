"""Pre-trained microsoft/table-transformer-detection on PDF pages -> COCO detection JSON.

Preprocessing is the model's own preprocessor_config.json (DetrFeatureExtractor: resize
size/max_size 800, ImageNet mean/std normalize), loaded with AutoImageProcessor -- nothing
hand-rolled, no fine-tuned weights, no Azure DI reference.

    python detect.py <pdf> [<pdf> ...] [--pages 1,3-5] [--dpi 150] [--threshold 0.5] [--out out]

Output (--out):
  coco.json            {"images", "annotations", "categories"} -- bbox = [x, y, w, h] in image
                       pixels at --dpi, plus "score"; images carry pdf/page/dpi
  images/<pdf>_p<N>.png  rendered page with the detected boxes drawn
"""

import argparse
import json
from pathlib import Path

import pymupdf
import torch
from PIL import Image, ImageDraw
from transformers import AutoImageProcessor, TableTransformerForObjectDetection

MODEL = "microsoft/table-transformer-detection"


def parse_pages(spec, n):
    if not spec:
        return list(range(1, n + 1))
    pages = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        pages += range(int(a), int(b or a) + 1)
    return [p for p in pages if 1 <= p <= n]


def to_coco_bbox(x0, y0, x1, y1):
    return [round(x0, 2), round(y0, 2), round(x1 - x0, 2), round(y1 - y0, 2)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+", type=Path)
    ap.add_argument("--pages", help="1-based, e.g. 1,3-5 (default: all)")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--out", type=Path, default=Path("out"))
    args = ap.parse_args()

    processor = AutoImageProcessor.from_pretrained(MODEL)
    model = TableTransformerForObjectDetection.from_pretrained(MODEL).eval()
    (args.out / "images").mkdir(parents=True, exist_ok=True)

    coco = {"images": [], "annotations": [],
            "categories": [{"id": i, "name": n} for i, n in sorted(model.config.id2label.items())]}
    for pdf in args.pdfs:
        doc = pymupdf.open(pdf)
        for page_no in parse_pages(args.pages, doc.page_count):
            pix = doc[page_no - 1].get_pixmap(dpi=args.dpi)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            inputs = processor(images=image, return_tensors="pt")
            with torch.no_grad():
                outputs = model(**inputs)
            result = processor.post_process_object_detection(
                outputs, threshold=args.threshold, target_sizes=[(image.height, image.width)])[0]

            image_id = len(coco["images"]) + 1
            file_name = f"{pdf.stem}_p{page_no}.png"
            coco["images"].append({"id": image_id, "file_name": file_name, "width": image.width,
                                   "height": image.height, "pdf": pdf.name, "page": page_no, "dpi": args.dpi})
            draw = ImageDraw.Draw(image)
            for score, label, box in zip(result["scores"].tolist(), result["labels"].tolist(),
                                         result["boxes"].tolist()):
                bbox = to_coco_bbox(*box)
                coco["annotations"].append({"id": len(coco["annotations"]) + 1, "image_id": image_id,
                                            "category_id": label, "bbox": bbox,
                                            "area": round(bbox[2] * bbox[3], 2), "iscrowd": 0,
                                            "score": round(score, 4)})
                draw.rectangle(box, outline="red", width=3)
                draw.text((box[0] + 4, box[1] + 4), f"{model.config.id2label[label]} {score:.2f}", fill="red")
            image.save(args.out / "images" / file_name)
            print(f"{pdf.name} p{page_no}: {len(result['scores'])} detection(s)")

    (args.out / "coco.json").write_text(json.dumps(coco, indent=1), encoding="utf-8")
    print(f"{len(coco['annotations'])} detections on {len(coco['images'])} page(s) -> {args.out / 'coco.json'}")


if __name__ == "__main__":
    assert to_coco_bbox(10, 20, 110, 70) == [10, 20, 100, 50]
    assert parse_pages("1,3-4,99", 5) == [1, 3, 4]
    main()
