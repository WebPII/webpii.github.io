#!/usr/bin/env python3
"""
Dataset Statistics Script
Analyzes bounding box annotations in the screenshots dataset.

Outputs statistics like:
- Category count
- Percentage of total
- Average per image
- Average box area (in pixels)
"""

import json
import re
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
import argparse


@dataclass
class CategoryStats:
    count: int = 0
    total_area: float = 0.0
    visible_count: int = 0


def normalize_category(key: str) -> str:
    """
    Normalize element keys into categories.

    Examples:
        PII_FIRSTNAME -> PII_NAME
        PII_LASTNAME -> PII_NAME
        PII_FULLNAME -> PII_NAME
        PII_FULLNAME -> PII_NAME
        PII_EMAIL -> PII_EMAIL
        PII_PHONE -> PII_PHONE
        PII_ADDRESS -> PII_ADDRESS
        PII_STREET -> PII_ADDRESS
        PII_CITY -> PII_ADDRESS
        PII_STATE -> PII_ADDRESS
        PII_STATE_ABBR -> PII_ADDRESS
        PII_POSTCODE -> PII_ADDRESS
        PII_COUNTRY -> PII_ADDRESS
        PII_COUNTRY_CODE -> PII_ADDRESS
        PII_AVATAR -> PII_AVATAR
        PII_ACCOUNT_ID -> PII_ACCOUNT
        PII_CARD_LAST4 -> PII_PAYMENT
        PRODUCT1_NAME -> PRODUCT_NAME
        PRODUCT1_PRICE -> PRODUCT_PRICE
        PRODUCT1_IMAGE -> PRODUCT_IMAGE
        etc.
    """
    # PII name-related fields
    if re.match(r'^PII_(FIRSTNAME|LASTNAME|USERNAME|FULLNAME|NAME)$', key):
        return 'PII_NAME'

    # PII address-related fields
    if re.match(r'^PII_(ADDRESS|STREET|CITY|STATE|STATE_ABBR|POSTCODE|COUNTRY|COUNTRY_CODE)$', key):
        return 'PII_ADDRESS'

    # PII payment-related
    if re.match(r'^PII_(CARD_LAST4|CARD_.*)$', key):
        return 'PII_PAYMENT'

    # PII account-related
    if re.match(r'^PII_(ACCOUNT_ID|ACCOUNT_.*)$', key):
        return 'PII_ACCOUNT'

    # Simple PII fields (email, phone, avatar)
    if key in ['PII_EMAIL', 'PII_PHONE', 'PII_AVATAR']:
        return key

    # Product fields - strip the number
    product_match = re.match(r'^PRODUCT\d+_(.+)$', key)
    if product_match:
        field = product_match.group(1)
        return f'PRODUCT_{field}'

    # Order fields
    if key.startswith('ORDER_'):
        return key

    # Input fields
    if key.startswith('INPUT_'):
        return 'INPUT_FIELD'

    # Return as-is for other keys
    return key


def calculate_box_area(bbox: dict) -> float:
    """Calculate area of bounding box in pixels."""
    return bbox.get('width', 0) * bbox.get('height', 0)


def process_elements(elements: list, stats: dict[str, CategoryStats], visible_only: bool = False):
    """Process a list of elements and update stats."""
    for elem in elements:
        key = elem.get('key', '')
        bbox = elem.get('bbox', {})
        visible = elem.get('visible', True)

        if visible_only and not visible:
            continue

        category = normalize_category(key)
        area = calculate_box_area(bbox)

        stats[category].count += 1
        stats[category].total_area += area
        if visible:
            stats[category].visible_count += 1


def analyze_dataset(screenshots_dir: Path, visible_only: bool = False) -> dict:
    """Analyze all JSON files in the screenshots directory."""
    stats = defaultdict(CategoryStats)
    num_images = 0
    total_boxes = 0

    json_files = sorted(screenshots_dir.glob('*.json'))
    json_files = [f for f in json_files if f.name != 'manifest.json']

    for json_file in json_files:
        with open(json_file, 'r') as f:
            data = json.load(f)

        num_images += 1

        # Process PII elements
        pii_elements = data.get('pii_elements', [])
        process_elements(pii_elements, stats, visible_only)
        total_boxes += len(pii_elements)

        # Process product elements
        product_elements = data.get('product_elements', [])
        process_elements(product_elements, stats, visible_only)
        total_boxes += len(product_elements)

        # Process pii_containers if present
        pii_containers = data.get('pii_containers', [])
        for container in pii_containers:
            category = 'PII_CONTAINER'
            bbox = container.get('bbox', {})
            area = calculate_box_area(bbox)
            stats[category].count += 1
            stats[category].total_area += area
            stats[category].visible_count += 1
            total_boxes += 1

    return {
        'stats': dict(stats),
        'num_images': num_images,
        'total_boxes': total_boxes
    }


def print_stats_table(results: dict, visible_only: bool = False):
    """Print statistics as a formatted table."""
    stats = results['stats']
    num_images = results['num_images']
    total_boxes = results['total_boxes']

    # Calculate totals for percentage
    total_count = sum(s.count for s in stats.values())

    # Sort categories: PII first, then PRODUCT, then others
    def sort_key(cat):
        if cat.startswith('PII_'):
            return (0, cat)
        elif cat.startswith('PRODUCT_'):
            return (1, cat)
        elif cat.startswith('ORDER_'):
            return (2, cat)
        else:
            return (3, cat)

    sorted_categories = sorted(stats.keys(), key=sort_key)

    # Print header
    mode = "(Visible Only)" if visible_only else "(All Elements)"
    print(f"\n{'='*90}")
    print(f"Dataset Statistics {mode}")
    print(f"{'='*90}")
    print(f"Total Images: {num_images}")
    print(f"Total Bounding Boxes: {total_boxes}")
    print(f"{'='*90}\n")

    # Table header
    header = f"{'Category':<25} {'Count':>8} {'% of Total':>12} {'Avg/Image':>12} {'Avg Area (px²)':>16} {'Visible':>10}"
    print(header)
    print('-' * 90)

    # Print each category
    for category in sorted_categories:
        s = stats[category]
        pct = (s.count / total_count * 100) if total_count > 0 else 0
        avg_per_image = s.count / num_images if num_images > 0 else 0
        avg_area = s.total_area / s.count if s.count > 0 else 0
        visible_pct = (s.visible_count / s.count * 100) if s.count > 0 else 0

        print(f"{category:<25} {s.count:>8} {pct:>11.1f}% {avg_per_image:>12.2f} {avg_area:>16.1f} {visible_pct:>9.1f}%")

    print('-' * 90)

    # Summary by type
    pii_count = sum(s.count for cat, s in stats.items() if cat.startswith('PII_'))
    product_count = sum(s.count for cat, s in stats.items() if cat.startswith('PRODUCT_'))
    order_count = sum(s.count for cat, s in stats.items() if cat.startswith('ORDER_'))
    other_count = total_count - pii_count - product_count - order_count

    print(f"\n{'Summary by Type':}")
    print(f"  PII Elements:     {pii_count:>6} ({pii_count/total_count*100:.1f}%)" if total_count else "  PII Elements:     0")
    print(f"  Product Elements: {product_count:>6} ({product_count/total_count*100:.1f}%)" if total_count else "  Product Elements: 0")
    print(f"  Order Elements:   {order_count:>6} ({order_count/total_count*100:.1f}%)" if total_count else "  Order Elements:   0")
    if other_count > 0:
        print(f"  Other Elements:   {other_count:>6} ({other_count/total_count*100:.1f}%)")
    print()


def export_csv(results: dict, output_file: Path, visible_only: bool = False):
    """Export statistics to CSV file."""
    stats = results['stats']
    num_images = results['num_images']
    total_count = sum(s.count for s in stats.values())

    with open(output_file, 'w') as f:
        f.write("Category,Count,% of Total,Avg per Image,Avg Box Area (px²),Visible %\n")

        for category, s in sorted(stats.items()):
            pct = (s.count / total_count * 100) if total_count > 0 else 0
            avg_per_image = s.count / num_images if num_images > 0 else 0
            avg_area = s.total_area / s.count if s.count > 0 else 0
            visible_pct = (s.visible_count / s.count * 100) if s.count > 0 else 0

            f.write(f"{category},{s.count},{pct:.2f},{avg_per_image:.2f},{avg_area:.1f},{visible_pct:.1f}\n")

    print(f"CSV exported to: {output_file}")


def main():
    parser = argparse.ArgumentParser(description='Analyze dataset statistics for bounding box annotations')
    parser.add_argument('--screenshots-dir', type=str,
                        default='ui_reproducer/screenshots',
                        help='Path to screenshots directory')
    parser.add_argument('--visible-only', action='store_true',
                        help='Only count visible elements')
    parser.add_argument('--csv', type=str, nargs='?', const='results/dataset_stats.csv',
                        help='Export results to CSV file (default: results/dataset_stats.csv)')

    args = parser.parse_args()

    # Resolve path
    screenshots_dir = Path(args.screenshots_dir)
    if not screenshots_dir.is_absolute():
        # Try relative to script location or cwd
        script_dir = Path(__file__).parent.parent
        screenshots_dir = script_dir / args.screenshots_dir

    if not screenshots_dir.exists():
        print(f"Error: Screenshots directory not found: {screenshots_dir}")
        return 1

    print(f"Analyzing: {screenshots_dir}")

    # Analyze
    results = analyze_dataset(screenshots_dir, args.visible_only)

    # Print table
    print_stats_table(results, args.visible_only)

    # Export CSV if requested
    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.is_absolute():
            csv_path = Path(__file__).parent / args.csv
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        export_csv(results, csv_path, args.visible_only)

    return 0


if __name__ == '__main__':
    exit(main())
