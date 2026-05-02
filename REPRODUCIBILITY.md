# Reproducibility

This release separates reproducible benchmark artifacts from private source
materials.

## Privacy Boundary

The original website screenshots used as visual targets are not released. They
were captured from real e-commerce sessions and can contain sensitive account,
order, address, payment, or session-specific information. WebPII uses those
screenshots only as private visual references for generating instrumented React
reproductions. The released dataset images are rendered from generated UI code
with synthetic PII and public/product assets.

This means reviewers can inspect and rerun the released benchmark pipeline, but
they should not expect access to the private source screenshots.

## What Can Be Run Immediately

With only this repository, reviewers can run:

```bash
bash scripts/smoke_test.sh
```

The smoke test verifies that:

- the release has no common identity/path leaks;
- the React template builds;
- synthetic data variant generation works on `example_data/`;
- core Python scripts compile.

`example_data/` is intentionally small. It is a fixture for executable checks,
not the benchmark dataset.

## What Requires Dataset Artifacts

To train/evaluate detectors, download the dataset artifacts submitted through
OpenReview and place the screenshot annotations at:

```text
ui_reproducer/screenshots/
├── 0000.png
├── 0000.json
├── 0001.png
├── 0001.json
└── manifest.json
```

Then run:

```bash
python predict/yolo_train.py prepare \
  --screenshots-dir ui_reproducer/screenshots \
  --output-dir predict/yolo_dataset

python predict/yolo_train.py train \
  --dataset predict/yolo_dataset/dataset.yaml \
  --output-dir predict/runs \
  --model-size n

python predict/yolo_inference.py evaluate \
  --weights predict/runs/pii_detection_v8n/weights/best.pt \
  --screenshots-dir ui_reproducer/screenshots \
  --output predict/results/yolo_eval.json
```

## What Requires Released Reproductions

To regenerate screenshots and annotations from UI code, place released React
reproduction projects at:

```text
ui_reproducer/output/{device}/{company}/{page_type}/{image_id}/{timestamp}/
├── src/App.jsx
├── src/data.json
├── requires.json
└── reproduction.log
```

Then generate synthetic variants and rerender:

```bash
python ui_reproducer/generate_data_variants.py \
  --data-dir data \
  --num-variants 100 \
  --output ui_reproducer/data_variants.ndjson

cd ui_reproducer
python screenshot_pages.py \
  --data data_variants.ndjson \
  --output screenshots \
  --num-variants 10 \
  --scrolls-per-variant 2 \
  --workers 8 \
  --scroll-top
```

This path does not require the private source screenshots.

## What Is Not Fully Reproducible From Public Artifacts

The exact first-stage UI reproduction from original website screenshots is not
fully reproducible from this release because the base screenshots are withheld
for privacy. `ui_reproducer/reproduce_ui.py` is included for transparency and
for reproducing the same procedure on new screenshots that a reviewer or user
provides locally.
