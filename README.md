# WebPII Code Release

This repository contains executable code for the WebPII benchmark and WebRedact
baselines. It is organized around three tasks:

- generating synthetic PII and product data variants;
- reproducing web UI screenshots as instrumented React pages;
- producing/evaluating visual PII bounding-box annotations and detectors.

The full WebPII dataset should be downloaded from the dataset URL submitted in
OpenReview. This repository includes `example_data/` only as a small runnable
fixture for checking the code path without downloading the full dataset.

## Setup

Python 3.11 and Node.js 20 are recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium

cd ui_reproducer/template
npm install
cd ../..
```

Prediction baselines have additional OCR/model dependencies:

```bash
pip install -r predict/requirements.txt
```

Tesseract-based baselines also require the system `tesseract` binary.

## Repository Layout

```text
example_data/              Small fixture used by smoke tests
ui_reproducer/             UI reproduction, synthetic data, screenshots, boxes
predict/                   OCR, Presidio, PaddleOCR, LLM, and YOLO baselines
download_pii_datasets.py   Optional helper for external text-PII sources
make_example_data.py       Rebuilds example_data/ from a full local dataset
merge_product_data.py      Merges product metadata and images
ui_scraper.py              Optional source screenshot collection helper
```

## Smoke Test

This smoke test exercises the synthetic-data path using only `example_data/`.

```bash
python ui_reproducer/generate_data_variants.py \
  --data-dir example_data \
  --num-variants 3 \
  --products-per-variant 5 \
  --max-products 50 \
  --output /tmp/webpii_variants.ndjson \
  --seed 42
```

Expected output: `/tmp/webpii_variants.ndjson` with three JSON lines containing
synthetic PII fields and product metadata.

## Full Dataset Workflow

After downloading the full WebPII dataset, place or symlink it as `data/` with
this structure:

```text
data/
├── assets/
│   ├── company_logos/
│   ├── payment_methods/
│   └── products/
├── assets_lite/
├── text_pii/
└── ui_images/
```

Generate synthetic variants:

```bash
python ui_reproducer/generate_data_variants.py \
  --data-dir data \
  --num-variants 100 \
  --products-per-variant 30 \
  --output ui_reproducer/data_variants.ndjson \
  --seed 42
```

Add `--use-llm` and set `OPENAI_API_KEY` to use LLM augmentation for product
prices, item categories, gift messages, and breadcrumbs. Without `--use-llm`,
the generator uses local deterministic fallbacks.

Generate screenshots and annotations from reproduced UI projects:

```bash
cd ui_reproducer
python screenshot_pages.py \
  --data data_variants.ndjson \
  --output screenshots \
  --num-variants 10 \
  --scrolls-per-variant 2 \
  --workers 8 \
  --scroll-top
cd ..
```

Prepare YOLO-format detection data:

```bash
python predict/yolo_train.py prepare \
  --screenshots-dir ui_reproducer/screenshots \
  --output-dir predict/yolo_dataset \
  --train-split 0.8
```

Train WebRedact/YOLO:

```bash
python predict/yolo_train.py train \
  --dataset predict/yolo_dataset/dataset.yaml \
  --output-dir predict/runs \
  --model-size n \
  --epochs 100 \
  --batch 16 \
  --imgsz 640
```

Evaluate a trained detector:

```bash
python predict/yolo_inference.py evaluate \
  --weights predict/runs/pii_detection_v8n/weights/best.pt \
  --screenshots-dir ui_reproducer/screenshots \
  --output predict/results/yolo_eval.json
```

## Optional LLM-Assisted UI Reproduction

`ui_reproducer/reproduce_ui.py` can call a local LLM coding CLI to recreate a
source screenshot as a React page. This is only needed to regenerate UI
reproductions, not to run the benchmark on released data.

```bash
cd ui_reproducer
LLM_BACKEND=claude python reproduce_ui.py ../example_data/ui_images/cart/2478-apple-desktop.png --iterations 1
```

Supported `LLM_BACKEND` values are `claude` and `opencode`. The corresponding
CLI must be installed and available on `PATH`; set `OPENCODE_BIN` to override
the OpenCode executable path.

## OpenReview

See `OPENREVIEW_CODE.md` for the recommended anonymized submission procedure and
the fields to paste into OpenReview.
