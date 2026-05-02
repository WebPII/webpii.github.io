# UI Reproducer

Tools for reproducing UIs from screenshots and generating PII-annotated datasets.

## Setup

Install Node.js dependencies in the template directory (shared across all generated projects):

```bash
cd template && npm install
```

## Scripts

### 1. `reproduce_ui.py` - Reproduce UI from Screenshot

Takes a screenshot and uses Claude Code to create a pixel-perfect React reproduction.

```bash
python reproduce_ui.py <image_path> [--iterations N]

# Example
python reproduce_ui.py ../data/ui_images/account-dashboard/4022-amazon-desktop.png --iterations 3
```

**Output:** `output/{device}/{company}/{page_type}/{timestamp}/` containing a Vite React project.

---

### 2. `generate_data_variants.py` - Generate PII Data Variants

Generates diverse `data.json` variants by sampling from PII datasets.
Uses `langdetect` to filter products for English only.
Supports LLM-guided fuzzy product search and parallel generation.

```bash
pip install langdetect rapidfuzz  # Required dependencies

# Basic generation
python generate_data_variants.py --use-llm --num-variants 100 --products-per-variant 30 --output data_variants.ndjson
```

**Options:**
| Flag | Description |
|------|-------------|
| `--num-variants N` | Number of variants to generate (default: 100) |
| `--output FILE` | Output file path (default: data_variants.ndjson) |
| `--use-llm` | Use GPT-4o-mini for fuzzy product search (requires OPENAI_API_KEY) |
| `--products-per-variant N` | Max products per variant (default: 10) |
| `--max-products N` | Max products to scan (default: 10000) |
| `--workers N` | Parallel workers, 0=auto (default: 0) |
| `--seed N` | Random seed for reproducibility |
| `-v, --verbose` | Verbose logging |

**LLM Fuzzy Search (--use-llm):**
1. Samples context text from panorama dataset
2. Uses GPT-4o-mini to generate product search queries
3. Fuzzy matches queries against product database (60% threshold)
4. Returns contextually relevant products instead of random sampling

**Output Files:**
- `data_variants.ndjson` - Generated variants (one JSON per line)
- `data_variants.log` - Execution log
- `data_variants.stats.json` - Statistics (timing, counts, etc.)

**Data Sources:**
- `data/text_pii/nemotron-pii/` - PII fields (name, email, phone, address)
- `data/text_pii/panorama/` - Context samples (Article, Online Review, Online Ad)
- `data/assets/products/products_merged.ndjson` - Product images and metadata

**Output Format (NDJSON):**
```json
{
  "PII_USERNAME": "John Doe",
  "PII_FIRSTNAME": "John",
  "PII_LASTNAME": "Doe",
  "PII_EMAIL": "john@example.com",
  "PII_PHONE": "(555) 123-4567",
  "PII_ADDRESS": "123 Main St, City, State",
  "PII_AVATAR": "/placeholders/avatar.png",
  "PII_ACCOUNT_ID": "USR-12345678",
  "PII_CARD_LAST4": "4242",
  "PII_URL": "https://example.com/user/john",
  "PII_USER_NAME": "johndoe123",
  "PRODUCT1_NAME": "Product Name",
  "PRODUCT1_PRICE": "$29.99",
  "PRODUCT1_IMAGE": "products/abo-images-small/images/...",
  "PRODUCT1_DESC": "Product description",
  "PRODUCT2_NAME": "...",
  "...": "up to PRODUCT10"
}
```

**Required PII Fields:** `first_name`, `email`, `phone_number`, `street_address`

---

### 3. `screenshot_pages.py` - Screenshot with PII Bounding Boxes

Takes screenshots of reproduced UIs with **3 types of bounding box annotations**.
Parallelizes across pages (each page gets its own worker/port).

```bash
# Basic usage - scroll to top to see PII
python screenshot_pages.py --data data_variants.ndjson --output screenshots/ --scroll-top

# Multiple variants and scroll positions
python screenshot_pages.py --data data_variants.ndjson --output screenshots/ \
    --num-variants 5 --scrolls-per-variant 3

# Parallel across all pages
python screenshot_pages.py --data data_variants.ndjson --output screenshots/ \
    --num-variants 10 --scrolls-per-variant 2 --workers 8
```

**Options:**
| Flag | Description |
|------|-------------|
| `--data FILE` | Data JSON or NDJSON file (default: data.json) |
| `--output DIR` | Output directory (default: screenshots/) |
| `--num-variants N` | Number of data variants per page (default: 1) |
| `--scrolls-per-variant N` | Scroll positions per variant (default: 1) |
| `--scroll-top` | Always scroll to top (no random scroll) |
| `--page-filter STR` | Filter pages by company name |
| `--workers N` | Number of parallel workers, 0=auto (default: 0) |
| `--seed N` | Random seed |

**Parallelization:**
- ✅ Parallel across pages (each page = 1 worker on its own port)
- ❌ Sequential within a page (server restart needed per data variant)
- Total screenshots = pages × variants × scrolls

**Bounding Box Types:**

| Type | Color | Description |
|------|-------|-------------|
| `pii_elements` | 🔴 Red | Exact PII text/images (includes derived values like "John" from "John Doe", masked cards "••••4242") |
| `product_elements` | 🔵 Blue | Exact product text/images |
| `pii_containers` | 🟢 Green | Semantic regions that *look* like they could contain PII (cards, input fields, form groups, table rows) - excludes footers |

**Output Format:**
```
screenshots/
├── 0000.png          # Screenshot image
├── 0000.json         # Annotation file
├── 0001.png
├── 0001.json
└── manifest.json     # Summary of all screenshots
```

**Annotation Schema (`0000.json`):**
```json
{
  "image_path": "screenshots/0000.png",
  "data_json": {...},
  "company": "amazon",
  "page_type": "account-dashboard",
  "device": "desktop",
  "scroll_y": 342,
  "page_height": 2389,
  "viewport": {"width": 1280, "height": 800},

  "pii_elements": [
    {
      "key": "PII_FIRSTNAME_DERIVED",
      "value": "John",
      "bbox": {"x": 120, "y": 45, "width": 100, "height": 20},
      "visible": true,
      "clipped": false,
      "element_type": "text"
    }
  ],

  "product_elements": [
    {
      "key": "PRODUCT1_IMAGE",
      "value": "/path/to/image.jpg",
      "bbox": {"x": 200, "y": 150, "width": 120, "height": 120},
      "visible": true,
      "clipped": false,
      "element_type": "image"
    }
  ],

  "pii_containers": [
    {
      "container_type": "card",
      "bbox": {"x": 50, "y": 100, "width": 300, "height": 200},
      "visible": true,
      "clipped": false,
      "contains_actual_pii": true,
      "pii_keys": ["PII_USERNAME", "PII_EMAIL"],
      "semantic_hint": "user profile card"
    }
  ]
}
```

**Container Types:**
- `input_field` - Input, textarea, contenteditable elements
- `text_block` - Paragraphs, spans with text content
- `profile_section` - Elements with avatar + text
- `table_row` - Table rows/cells
- `card` - Elements with border/shadow (general containers)
- `form_group` - Label + input pairs
- `list_item` - List items with icon + text

**Visibility Fields:**
- `visible: true` - Element is fully within viewport
- `visible: true, clipped: true` - Element is partially visible (cut off by scroll)
- `visible: false` - Element is completely outside viewport

---

## Workflow Example

```bash
# 1. Generate 100 data variants with LLM-guided product search
python generate_data_variants.py --num-variants 100 --use-llm --output data_variants.ndjson

# 2. Screenshot all pages (5 variants × 3 scroll positions = 15 per page)
python screenshot_pages.py --data data_variants.ndjson --output screenshots/ \
    --num-variants 5 --scrolls-per-variant 3 --workers 8

# 3. Quick test with scroll at top (to see PII bounding boxes)
python screenshot_pages.py --data data_variants.ndjson --output screenshots_test/ \
    --num-variants 2 --scroll-top --page-filter amazon

# 4. Check output
ls screenshots/
cat screenshots/manifest.json
```

---

## Directory Structure

```
ui_reproducer/
├── reproduce_ui.py           # UI reproduction from screenshots
├── generate_data_variants.py # PII data generation
├── screenshot_pages.py       # Screenshot with bbox annotations
├── data.json                 # Default PII/product data
├── template/                 # Vite React template
├── output/                   # Reproduced UIs
│   └── {device}/{company}/{page_type}/{timestamp}/
└── screenshots/              # Generated screenshots + annotations
```

## Assets Structure

Product images (400K+ files, 5GB+) are served on-demand via Vite middleware to avoid scanning overhead:

```
../data/
├── assets/                   # Full assets (includes large products folder)
│   ├── company_logos/
│   ├── payment_methods/
│   └── products/             # 400K+ product images - NOT in publicDir
└── assets_lite/              # Symlinks for Vite publicDir (fast startup)
    ├── company_logos -> ../assets/company_logos
    └── payment_methods -> ../assets/payment_methods
```

The `vite.config.js` template uses:
- `publicDir` → `assets_lite/` (logos, payment methods only)
- Custom middleware → serves `/products/*` on-demand from `assets/products/`

---

## Notes

- The OmniParser integration (`OmniParser/`, `SETUP_OMNIPARSER.md`) was previously considered but is not currently in use.
