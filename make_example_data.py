#!/usr/bin/env python3
"""
Create example_data/ directory from local data/ for GitHub Actions testing.

This script copies a minimal subset of the local data/ directory to example_data/
for use in CI/CD pipelines. It:
- Copies a few sample UI screenshots
- Copies a small subset of product images and creates a minimal products_merged.ndjson
- Skips large datasets (nemotron-pii, panorama, gretel-finance)
- Copies company logos and payment method icons if present

Usage:
    python make_example_data.py                    # Use defaults
    python make_example_data.py --num-products 50  # Limit product count
    python make_example_data.py --num-images 5     # Limit UI screenshot count
    python make_example_data.py --clean            # Remove existing example_data first
"""

import argparse
import csv
import json
import random
import shutil
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR / "data"
EXAMPLE_DATA_DIR = SCRIPT_DIR / "example_data"

# Default limits for example data
DEFAULT_NUM_UI_IMAGES = 3
DEFAULT_NUM_PRODUCTS = 50  # Small number of English products for CI
DEFAULT_NUM_PRODUCT_IMAGES = 50


def clean_example_data():
    """Remove existing example_data directory."""
    if EXAMPLE_DATA_DIR.exists():
        print(f"Removing existing {EXAMPLE_DATA_DIR}...")
        shutil.rmtree(EXAMPLE_DATA_DIR)


def copy_ui_images(num_images: int = DEFAULT_NUM_UI_IMAGES):
    """Copy a sample of UI screenshots to example_data."""
    src_dir = DATA_DIR / "ui_images"
    dst_dir = EXAMPLE_DATA_DIR / "ui_images"

    if not src_dir.exists():
        print(f"WARNING: {src_dir} does not exist, skipping UI images")
        return []

    # Find all desktop images
    desktop_images = list(src_dir.glob("**/*-desktop.png"))
    if not desktop_images:
        print("WARNING: No desktop images found")
        return []

    # Sample randomly
    selected = random.sample(desktop_images, min(num_images, len(desktop_images)))

    copied = []
    for src_img in selected:
        # Preserve directory structure (e.g., account-dashboard/4022-amazon-desktop.png)
        rel_path = src_img.relative_to(src_dir)
        dst_img = dst_dir / rel_path
        dst_img.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_img, dst_img)
        copied.append(rel_path)
        print(f"  Copied: {rel_path}")

    print(f"Copied {len(copied)} UI screenshots")
    return copied


def copy_product_data(num_products: int = DEFAULT_NUM_PRODUCTS, num_images: int = DEFAULT_NUM_PRODUCT_IMAGES):
    """Copy a subset of English product data to example_data."""
    src_products_dir = DATA_DIR / "assets" / "products"
    dst_products_dir = EXAMPLE_DATA_DIR / "assets" / "products"

    # Check for products_merged.ndjson (the merged product data)
    products_merged = src_products_dir / "products_merged.ndjson"
    if not products_merged.exists():
        print(f"WARNING: {products_merged} does not exist")
        print("  Run 'python merge_product_data.py' first to create it")
        return 0, 0

    dst_products_dir.mkdir(parents=True, exist_ok=True)

    # Read products and filter for English ones
    print(f"  Filtering for English products...")
    english_products = []
    total_scanned = 0

    with open(products_merged, "r") as f:
        for line in f:
            if not line.strip():
                continue

            total_scanned += 1
            product = json.loads(line)

            # Check for English item_name
            has_english_name = False
            for item in product.get("item_name", []):
                lang = item.get("language_tag", "").lower()
                if lang.startswith("en") and item.get("value"):
                    has_english_name = True
                    break

            # Must have English name and valid image
            if has_english_name and product.get("main_image", {}).get("full_path"):
                english_products.append((line, product))

                if len(english_products) >= num_products:
                    break

    print(f"  Found {len(english_products)} English products (scanned {total_scanned})")

    if not english_products:
        print("  WARNING: No English products found!")
        return 0, 0

    selected_products = [p for _, p in english_products]

    # Write minimal products_merged.ndjson (preserve original JSON lines)
    dst_products_file = dst_products_dir / "products_merged.ndjson"
    with open(dst_products_file, "w") as f:
        for line, _ in english_products:
            f.write(line)
    print(f"  Created {dst_products_file.name} with {len(selected_products)} English products")

    # Copy the corresponding product images
    src_images_dir = src_products_dir / "abo-images-small" / "images" / "small"
    dst_images_dir = dst_products_dir / "abo-images-small" / "images" / "small"

    images_copied = 0
    if src_images_dir.exists():
        for product in selected_products[:num_images]:
            img_path = product.get("main_image", {}).get("path", "")
            if img_path:
                src_img = src_images_dir / img_path
                if src_img.exists():
                    dst_img = dst_images_dir / img_path
                    dst_img.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_img, dst_img)
                    images_copied += 1
        print(f"  Copied {images_copied} product images")
    else:
        print(f"  WARNING: Product images directory not found: {src_images_dir}")

    # Create images.csv with just the copied images
    src_images_csv = src_products_dir / "abo-images-small" / "images" / "metadata" / "images.csv"
    if src_images_csv.exists():
        dst_metadata_dir = dst_products_dir / "abo-images-small" / "images" / "metadata"
        dst_metadata_dir.mkdir(parents=True, exist_ok=True)
        dst_images_csv = dst_metadata_dir / "images.csv"

        # Get image paths we need
        needed_images = set()
        for product in selected_products:
            img_path = product.get("main_image", {}).get("path", "")
            if img_path:
                needed_images.add(img_path)

        # Copy only relevant rows from images.csv
        with open(src_images_csv, "r") as src_f, open(dst_images_csv, "w", newline="") as dst_f:
            reader = csv.DictReader(src_f)
            writer = csv.DictWriter(dst_f, fieldnames=reader.fieldnames)
            writer.writeheader()
            rows_written = 0
            for row in reader:
                if row.get("path") in needed_images:
                    writer.writerow(row)
                    rows_written += 1
                    if rows_written >= len(needed_images):
                        break
        print(f"  Created images.csv with {rows_written} entries")

    return len(selected_products), images_copied


def copy_assets():
    """Copy company logos and payment method icons."""
    for asset_type in ["company_logos", "payment_methods"]:
        src_dir = DATA_DIR / "assets" / asset_type
        dst_dir = EXAMPLE_DATA_DIR / "assets" / asset_type

        if not src_dir.exists():
            print(f"  Skipping {asset_type}: not found")
            continue

        # Count files (excluding .gitkeep)
        files = [f for f in src_dir.iterdir() if f.is_file() and f.name != ".gitkeep"]
        if not files:
            print(f"  Skipping {asset_type}: empty")
            continue

        # Check total size - skip if over 10MB
        total_size = sum(f.stat().st_size for f in files)
        if total_size > 10 * 1024 * 1024:
            print(f"  Skipping {asset_type}: too large ({total_size / 1024 / 1024:.1f}MB)")
            continue

        # Copy all files
        dst_dir.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, dst_dir / f.name)
        print(f"  Copied {len(files)} {asset_type}")


def create_assets_lite():
    """Create assets_lite directory with symlinks or copies for Vite."""
    dst_assets_lite = EXAMPLE_DATA_DIR / "assets_lite"
    dst_assets_lite.mkdir(parents=True, exist_ok=True)

    for asset_type in ["company_logos", "payment_methods"]:
        src = EXAMPLE_DATA_DIR / "assets" / asset_type
        dst = dst_assets_lite / asset_type

        if src.exists():
            # Create symlink (or copy on Windows)
            try:
                dst.symlink_to(f"../assets/{asset_type}")
                print(f"  Created symlink: assets_lite/{asset_type}")
            except (OSError, NotImplementedError):
                # Fall back to copy if symlinks not supported
                if src.is_dir():
                    shutil.copytree(src, dst)
                    print(f"  Copied: assets_lite/{asset_type}")


def create_gitkeep_files():
    """Create .gitkeep files in empty directories."""
    dirs_to_keep = [
        EXAMPLE_DATA_DIR / "ui_images",
        EXAMPLE_DATA_DIR / "assets" / "products",
        EXAMPLE_DATA_DIR / "assets" / "company_logos",
        EXAMPLE_DATA_DIR / "assets" / "payment_methods",
    ]

    for d in dirs_to_keep:
        d.mkdir(parents=True, exist_ok=True)
        gitkeep = d / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.touch()


def main():
    parser = argparse.ArgumentParser(description="Create example_data from local data/")
    parser.add_argument("--num-images", type=int, default=DEFAULT_NUM_UI_IMAGES,
                       help=f"Number of UI screenshots to copy (default: {DEFAULT_NUM_UI_IMAGES})")
    parser.add_argument("--num-products", type=int, default=DEFAULT_NUM_PRODUCTS,
                       help=f"Number of products to include (default: {DEFAULT_NUM_PRODUCTS})")
    parser.add_argument("--num-product-images", type=int, default=DEFAULT_NUM_PRODUCT_IMAGES,
                       help=f"Number of product images to copy (default: {DEFAULT_NUM_PRODUCT_IMAGES})")
    parser.add_argument("--clean", action="store_true", help="Remove existing example_data first")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    args = parser.parse_args()

    random.seed(args.seed)

    print("=" * 60)
    print("Creating example_data/ from local data/")
    print("=" * 60)

    if not DATA_DIR.exists():
        print(f"ERROR: Data directory not found: {DATA_DIR}")
        print("Make sure you have data/ directory with UI images and product data")
        return 1

    if args.clean:
        clean_example_data()

    EXAMPLE_DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("\n1. Copying UI screenshots...")
    ui_images = copy_ui_images(args.num_images)

    print("\n2. Copying product data...")
    num_products, num_product_images = copy_product_data(args.num_products, args.num_product_images)

    print("\n3. Copying assets (logos, payment methods)...")
    copy_assets()

    print("\n4. Creating assets_lite symlinks...")
    create_assets_lite()

    print("\n5. Creating .gitkeep files...")
    create_gitkeep_files()

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Example data created at: {EXAMPLE_DATA_DIR}")
    print(f"  UI screenshots: {len(ui_images)}")
    print(f"  Products: {num_products}")
    print(f"  Product images: {num_product_images}")
    print()
    print("Next steps:")
    print("  1. Review the generated example_data/ directory")
    print("  2. Run bash scripts/smoke_test.sh")
    print("  3. Commit the updated fixture data")

    return 0


if __name__ == "__main__":
    exit(main())
