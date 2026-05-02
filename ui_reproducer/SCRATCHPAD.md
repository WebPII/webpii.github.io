# Partial Fill + Bounding Box Implementation Scratchpad

## Task Summary
1. Add partial input fill system (only 1-2 input fields partially filled)
2. Fix bounding box precision (inputs = full element, text = tight around value)

## Implementation Status: ✅ COMPLETE

---

## System Understanding

### Data Flow
```
data_variants.ndjson → screenshot_pages.py → inject_data_json() → data.json
                                          ↓
                       React renders App.jsx with data.json values
                                          ↓
                       Playwright takes screenshot + detects elements
                                          ↓
                       Output: screenshot.png + annotation.json
```

### Key Files & Their Roles

| File | Role | Status |
|------|------|--------|
| `template/src/partialFill.js` | Helper functions for partial input values | ✅ CREATED |
| `prompts.py` | Instructions for Claude Code to generate UIs | ✅ UPDATED |
| `screenshot_pages.py` | Takes screenshots, injects data, detects elements | ✅ UPDATED |

---

## Changes Made

### 1. template/src/partialFill.js (NEW) ✅
- `isPartialField(key)` - check if field should be partial
- `getFieldValue(key)` - get truncated or full value
- `getPartialPercent(key)` - get percentage for data attribute
- `getPartialProps(key)` - convenience function for input props

### 2. prompts.py ✅
- Added "PARTIAL INPUT FILL" section after generators (lines 103-121)
- Added "SPAN WRAPPING" rules for bbox precision (lines 161-181)
- Added "Section 7: Span Wrapping Check" to verification prompt (lines 266-271)

### 3. screenshot_pages.py ✅
- Added CLI flags: `--partial-fill`, `--partial-count` (lines 2391-2392)
- Modified `inject_data_json()` to generate PARTIAL_FILL_CONFIG (lines 278-299)
- Added partial_fill to task dict (lines 2470-2471)
- Extracted partial_fill in `process_page_task()` (lines 2211-2212)
- Added partial fill detection in DETECTION_SCRIPT (lines 1810-1820)
- Added partial_fill_info to annotation output (line 2338)
- **Orange bounding boxes** for partial fill elements in `draw_bounding_boxes()` (lines 2084, 2132-2149)
  - Color: (255, 140, 0) orange
  - Label prefix: `[P]` for partial elements

---

## screenshot_pages.py Architecture

### Call Chain
```
main()
  └── Creates tasks[] with page_info, variants, config
       └── process_page_task(task)
            └── inject_data_json(page_dir, data)  ← Need to add partial_fill config HERE
            └── start_dev_server()
            └── detect_elements()
            └── Build annotation
```

### Key Functions
- `inject_data_json(page_dir, data)` - Line 260 - writes data.json
- `process_page_task(task)` - Line 2194 - processes all variants for a page
- `detect_elements()` - runs JavaScript to find data-* elements
- `main()` - Line 2378 - creates tasks, passes to workers

### Where to Add Partial Fill Config
1. `task` dict (line 2448-2470) - add `partial_fill` and `partial_count`
2. `process_page_task()` - extract from task, pass to inject_data_json
3. `inject_data_json()` - generate PARTIAL_FILL_CONFIG based on params

---

## Usage Examples

```bash
# Normal mode - all fields fully filled
python screenshot_pages.py --output screenshots/normal

# Partial fill mode - 2 random input fields partially filled
python screenshot_pages.py --output screenshots/partial --partial-fill

# Partial fill with custom count
python screenshot_pages.py --output screenshots/partial --partial-fill --partial-count 1
```

---

## Key Constraint Reminder
**ONLY input fields can be partially filled!**
- `<input>`, `<textarea>` → use `getFieldValue()` from partialFill.js
- `<span>`, `<div>`, etc. → always use `data.*` directly

---

# Prompt Refactoring (2026-01-13)

## Problem
Single API call was responsible for too many concerns:
- Visual layout reproduction
- Data substitution (PII/product/order)
- Attribute injection (data-pii, data-product, data-order)
- Span wrapping correctness
- Input field handling (getPartialProps)
- Generator usage
- Modal/overlay logic
- Calculated values

Result: AI struggled to follow all rules consistently.

## Solution: Two-Phase Split

### Phase 1: STRUCTURE (`build_structure_prompt`)
**Focus:** Visual layout + data substitution (WHAT goes WHERE)
- Layout analysis, colors, hierarchy
- Modal/sidebar positioning rules
- Data variable reference (PII, Products, Order)
- Generated IDs/cards
- Calculated values explained

### Phase 2: COMPLIANCE (`build_compliance_prompt`)
**Focus:** Mechanical rule-following (checklist-driven)
- Data attributes for every data.X
- Span wrapping (only wrap variable, not labels)
- Input fields use getPartialProps
- Composite fields (PII_CITY_STATE, etc.)
- Multiple locations for transit tracking (PII_CITY2, PII_CITY3)
- Tracking timestamps with varied times + attrs
- Calculated values computed from products, not data.*

## New Additions (from debugging)
1. **Transit/hub cities as PII** - extended identifiers revealing user region
2. **PII_CARD_LAST4** - card numbers need bounding boxes
3. **Varied tracking timestamps** - each event needs distinct time + data-order attr
4. **Sidebar height fix** - `fixed top-0 right-0 h-screen` for full viewport height

## Backwards Compatibility
- `build_initial_prompt` → alias for `build_structure_prompt`
- `build_verification_prompt` → alias for `build_compliance_prompt`

## generate_data_variants.py Updates
Added new PII fields for transit tracking:
- `PII_CITY2`, `PII_STATE2`, `PII_STATE_ABBR2`, `PII_CITY_STATE2`
- `PII_CITY3`, `PII_STATE3`, `PII_STATE_ABBR3`, `PII_CITY_STATE3`

These represent shipping hub cities that are distinct from user's primary address but still reveal user's region (extended PII identifiers).

Note: `PII_CARD_LAST4` is NOT in data.json - it's generated at runtime with `gen.card('visa')` but needs `data-pii="PII_CARD_LAST4"` attribute for bbox tracking.

---

# Detection Script Fixes (2026-01-13)

## Issue: Bounding boxes overlapping modals

**Problem**: When an input field spans across a modal (visible on both left AND right sides of the modal), the 'center' coverage pattern was triggered, which used FULL bbox - causing ugly overlap.

**Root cause**: The 'center' pattern assumed "if all 4 edges visible, you can see the element's extent, use full bbox." But this looks bad when a modal covers the middle.

**Fix**: Changed 'center' pattern to use `computeClippedBbox()` which finds the largest visible region (left or right of modal) instead of full bbox.

```javascript
// Before: clippedBbox = null (full bbox)
// After: clippedBbox = computeClippedBbox(bbox, coveringBbox).clippedBbox
```

## Modal pointer-events pattern

For detection to work with modals:
- Centering wrapper: `pointer-events-none` (so elementFromPoint passes through)
- Dialog box: `pointer-events-auto` (so it can be detected as covering)
- Backdrop: `pointer-events-auto` with semi-transparent bg (doesn't block detection)
