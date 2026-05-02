#!/usr/bin/env python3
"""
Test single screenshot - re-generate a specific screenshot for testing detection logic.

Usage:
    python test_single_screenshot.py 0003
    python test_single_screenshot.py 8
"""

import argparse
import json
import sys
from pathlib import Path
from screenshot_pages import (
    take_screenshot_with_annotations,
    SCRIPT_DIR,
    SCREENSHOTS_DIR
)


def main():
    parser = argparse.ArgumentParser(description="Re-generate a single screenshot for testing")
    parser.add_argument("screenshot_num", type=int, help="Screenshot number (e.g., 3 for 0003.png)")
    parser.add_argument("--debug", action="store_true", help="Enable debug output")
    parser.add_argument("--partial-fill", action="store_true", help="Enable partial fill mode (partially filled form inputs)")
    args = parser.parse_args()

    # Format screenshot number
    screenshot_id = f"{args.screenshot_num:04d}"
    json_path = SCREENSHOTS_DIR / f"{screenshot_id}.json"

    if not json_path.exists():
        print(f"ERROR: Screenshot {screenshot_id}.json not found")
        sys.exit(1)

    # Load existing screenshot metadata
    with open(json_path) as f:
        metadata = json.load(f)

    print(f"Re-generating screenshot {screenshot_id}:")
    print(f"  Company: {metadata['company']}")
    print(f"  Page: {metadata['page_type']}")
    print(f"  Device: {metadata['device']}")
    print(f"  Output dir: {metadata['output_dir']}")
    print()

    # Reconstruct page_info
    page_dir = SCRIPT_DIR.parent / metadata['output_dir']
    if not page_dir.exists():
        print(f"ERROR: Page directory not found: {page_dir}")
        sys.exit(1)

    page_info = {
        "path": page_dir,
        "company": metadata["company"],
        "page_type": metadata["page_type"],
        "device": metadata["device"],
        "required_fields": metadata.get("required_fields", []),
        "source_image": metadata.get("source_image"),
        "output_dir": metadata.get("output_dir"),
    }

    # Get data (always use scroll_y=0 and full_page for testing)
    data = metadata["data_json"]
    if "_meta" in metadata:
        data["_meta"] = metadata["_meta"]
    scroll_y = 0  # Always scroll to top for consistent testing

    # Use a unique port to avoid conflicts
    port = 5173 + args.screenshot_num

    print(f"Regenerating with {len(page_info['required_fields'])} required fields...")
    if args.partial_fill:
        print(f"  Partial fill mode: ENABLED")
    if args.debug:
        print(f"  Required fields: {page_info['required_fields']}")
    print()

    try:
        # Take new screenshot (full-page, scroll_y=0 for consistent testing)
        annotation = take_screenshot_with_annotations(
            page_info=page_info,
            data=data,
            scroll_y=scroll_y,
            output_dir=SCREENSHOTS_DIR,
            index=args.screenshot_num,
            port=port,
            debug=args.debug,
            full_page=True,
            partial_fill=args.partial_fill
        )

        # Compute stats from annotation
        pii_elements = annotation.get("pii_elements", [])
        product_elements = annotation.get("product_elements", [])
        pii_containers = annotation.get("pii_containers", [])

        pii_visible = len([e for e in pii_elements if e.get("visible")])
        products_visible = len([e for e in product_elements if e.get("visible")])
        containers_found = len(pii_containers)

        print(f"✓ Screenshot saved: screenshots/{screenshot_id}.png")
        print(f"✓ Annotation saved: screenshots/{screenshot_id}.json")
        print()
        print("Detection results:")
        print(f"  PII elements: {len(pii_elements)} found, {pii_visible} visible")
        print(f"  Product elements: {len(product_elements)} found, {products_visible} visible")
        print(f"  Containers: {containers_found} found")

        if args.debug:
            print()
            print("PII elements:")
            for elem in annotation.get("pii_elements", []):
                vis = "✓" if elem["visible"] else "✗"
                clip = " (clipped)" if elem["clipped"] else ""
                val = str(elem['value'])[:50] if elem.get('value') else ''
                bbox = elem.get('bbox', {})
                pos = f" @ ({bbox.get('x', 0):.0f},{bbox.get('y', 0):.0f})"
                print(f"  {vis} {elem['key']}: {val}{clip}{pos}")

            print()
            print("Product elements:")
            for elem in annotation.get("product_elements", []):
                vis = "✓" if elem["visible"] else "✗"
                clip = " (clipped)" if elem["clipped"] else ""
                val = str(elem['value'])[:50] if elem.get('value') else ''
                bbox = elem.get('bbox', {})
                pos = f" @ ({bbox.get('x', 0):.0f},{bbox.get('y', 0):.0f})"
                print(f"  {vis} {elem['key']}: {val}{clip}{pos}")

    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
