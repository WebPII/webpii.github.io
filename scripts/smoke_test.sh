#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python}"
TMP_DIR="${TMPDIR:-/tmp}"
VARIANTS_PATH="${TMP_DIR%/}/webpii_variants_smoke.ndjson"
SMOKE_PAGE="ui_reproducer/output/desktop/smoke/cart/fixture/20260502_000000"
SMOKE_SCREENSHOTS="ui_reproducer/smoke_screenshots"

cleanup() {
  rm -rf "$SMOKE_PAGE" "$SMOKE_SCREENSHOTS"
}
trap cleanup EXIT

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

mkdir -p "$SMOKE_PAGE"
tar -C ui_reproducer/template --exclude node_modules --exclude dist -cf - . | tar -C "$SMOKE_PAGE" -xf -
ln -s "$(pwd)/ui_reproducer/template/node_modules" "$SMOKE_PAGE/node_modules"

cat > "$SMOKE_PAGE/src/App.jsx" <<'EOF'
import data from '@data'
import { getPartialProps } from './partialFill'

function App() {
  return (
    <main className="min-h-screen bg-gray-100 p-8">
      <section className="max-w-xl rounded border bg-white p-6 shadow">
        <h1 className="text-2xl font-bold">Synthetic Cart</h1>
        <p className="mt-4">Signed in as <span data-pii="PII_FULLNAME">{data.PII_FULLNAME}</span></p>
        <label className="mt-4 block">
          <span className="text-sm font-medium">Email</span>
          <input className="mt-1 block w-full rounded border p-2" data-pii="PII_EMAIL" {...getPartialProps('PII_EMAIL')} />
        </label>
        <p className="mt-4">First item: <span data-product="PRODUCT1_NAME">{data.PRODUCT1_NAME}</span></p>
      </section>
    </main>
  )
}

export default App
EOF

cat > "$SMOKE_PAGE/reproduction.log" <<'EOF'
{
  "status": "completed",
  "paths": {
    "source_image": "example_data/ui_images/cart/2478-apple-desktop.png",
    "output_dir": "ui_reproducer/output/desktop/smoke/cart/fixture/20260502_000000"
  }
}
EOF

cat > "$SMOKE_PAGE/requires.json" <<'EOF'
{
  "all_fields": ["PII_FULLNAME", "PII_EMAIL", "PRODUCT1_NAME"],
  "required_fields": {
    "pii": ["PII_FULLNAME", "PII_EMAIL"],
    "pii_form_fields": ["PII_EMAIL"],
    "products": ["PRODUCT1_NAME"]
  }
}
EOF

"$PYTHON_BIN" ui_reproducer/screenshot_pages.py \
  --data "$VARIANTS_PATH" \
  --output smoke_screenshots \
  --num-variants 1 \
  --scrolls-per-variant 1 \
  --scroll-top \
  --page-filter smoke \
  --workers 1 \
  --no-full-page

test -f "$SMOKE_SCREENSHOTS/0000.png"
test -f "$SMOKE_SCREENSHOTS/0000.json"

"$PYTHON_BIN" -m py_compile \
  scripts/check_release.py \
  ui_reproducer/generate_data_variants.py \
  ui_reproducer/reproduce_ui.py \
  ui_reproducer/screenshot_pages.py \
  predict/yolo_train.py \
  predict/yolo_inference.py \
  predict/benchmark.py

echo "Smoke test passed: $VARIANTS_PATH"
