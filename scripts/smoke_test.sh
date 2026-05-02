#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python}"
TMP_DIR="${TMPDIR:-/tmp}"
VARIANTS_PATH="${TMP_DIR%/}/webpii_variants_smoke.ndjson"

"$PYTHON_BIN" scripts/check_release.py

npm --prefix ui_reproducer/template run build

"$PYTHON_BIN" ui_reproducer/generate_data_variants.py \
  --data-dir example_data \
  --num-variants 3 \
  --products-per-variant 5 \
  --max-products 50 \
  --output "$VARIANTS_PATH" \
  --seed 42

test "$(wc -l < "$VARIANTS_PATH")" -eq 3

"$PYTHON_BIN" -m py_compile \
  scripts/check_release.py \
  ui_reproducer/generate_data_variants.py \
  ui_reproducer/reproduce_ui.py \
  ui_reproducer/screenshot_pages.py \
  predict/yolo_train.py \
  predict/yolo_inference.py \
  predict/benchmark.py

echo "Smoke test passed: $VARIANTS_PATH"
