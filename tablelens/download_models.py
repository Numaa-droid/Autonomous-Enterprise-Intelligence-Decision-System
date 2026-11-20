"""Download every Hugging Face model the pipeline uses into the local HF cache, so the first run needs no network.

    pip install -r requirements.txt
    python download_models.py

The fine-tuned Table Transformer (models/tatr-finetuned-01-11) comes with `git clone` via Git LFS.
Same list as the image prefetch in gpu_server.py; PaddleOCR's orientation model still downloads on first use.
"""
from huggingface_hub import hf_hub_download, snapshot_download
from transformers import TableTransformerForObjectDetection

for repo in ("microsoft/table-transformer-structure-recognition-v1.1-all",
             "microsoft/table-transformer-detection",
             "docling-project/docling-layout-heron",
             "docling-project/DocumentFigureClassifier-v2.5"):
    print("->", repo)
    snapshot_download(repo)
for name in ("PaddleOCR-VL-1.6-GGUF.gguf", "PaddleOCR-VL-1.6-GGUF-mmproj.gguf"):
    print("->", name)
    hf_hub_download("PaddlePaddle/PaddleOCR-VL-1.6-GGUF", name)
TableTransformerForObjectDetection.from_pretrained("microsoft/table-transformer-detection")  # fetches its timm backbone
print("done")
