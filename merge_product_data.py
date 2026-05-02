#!/usr/bin/env python3
"""
Merge Amazon Berkeley Objects listings with image metadata into a single NDJSON file.

Resolves image_id references to actual file paths for easy access.
"""

import csv
import json
from pathlib import Path

# Placeholder/invalid images to skip
BLOCKED_IMAGES = {
    "3595924e.jpg",  # amazonbasics logo placeholder
    "b5319e00.jpg",
    "874f86c4.jpg"
}


def load_image_mapping(images_csv_path: Path) -> dict[str, dict]:
    """Load images.csv into a dict mapping image_id -> {path, height, width}."""
    mapping = {}
    with open(images_csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mapping[row["image_id"]] = {
                "path": row["path"],
                "height": int(row["height"]),
                "width": int(row["width"]),
            }
    return mapping


def resolve_image(image_id: str, image_map: dict) -> dict | None:
    """Resolve an image_id to its metadata, or None if not found."""
    if image_id and image_id in image_map:
        return {"image_id": image_id, **image_map[image_id]}
    return None


def filter_amazon_values(product: dict) -> dict:
    """Remove values containing 'amazon' or strip 'umi.' and 'find.' prefixes from array fields with {language_tag, value} structure."""
    for key, val in product.items():
        if isinstance(val, list):
            filtered = []
            for item in val:
                if isinstance(item, dict) and "value" in item:
                    v = item["value"]
                    if isinstance(v, str):
                        # Skip if contains 'amazon'
                        if "amazon" in v.lower():
                            continue
                        # Strip 'umi.' or 'find.' prefix (and space after)
                        if v.lower().startswith("umi"):
                            v = v[4:].lstrip()  # Remove "umi." and any leading spaces
                            item = {**item, "value": v}
                        elif v.lower().startswith("find"):
                            v = v[5:].lstrip()  # Remove "find." and any leading spaces
                            item = {**item, "value": v}
                        elif v.lower().startswith("amaonbasics"):
                            v = v[11:].lstrip()  # Remove "amaonbasics." and any leading spaces
                            item = {**item, "value": v}
                    filtered.append(item)
                else:
                    filtered.append(item)
            product[key] = filtered
    return product


def merge_products(
    listings_dir: Path,
    image_map: dict,
    output_path: Path,
    images_base_path: str = "products/abo-images-small/images/small",
):
    """Merge all listing files with resolved image paths."""
    listing_files = sorted(listings_dir.glob("listings_*.json"))

    total_products = 0
    matched_images = 0
    skipped_blocked = 0

    with open(output_path, "w") as out:
        for listing_file in listing_files:
            print(f"Processing {listing_file.name}...")

            with open(listing_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    product = json.loads(line)
                    total_products += 1

                    # Filter out values mentioning Amazon
                    product = filter_amazon_values(product)

                    # Resolve main image
                    main_image_id = product.get("main_image_id")
                    if main_image_id:
                        img_info = resolve_image(main_image_id, image_map)
                        if img_info:
                            # Skip products with blocked placeholder images
                            img_filename = img_info["path"].split("/")[-1]
                            if img_filename in BLOCKED_IMAGES:
                                skipped_blocked += 1
                                continue
                            product["main_image"] = {
                                **img_info,
                                "full_path": f"{images_base_path}/{img_info['path']}",
                            }
                            matched_images += 1

                    # Resolve other images
                    other_image_ids = product.get("other_image_id", [])
                    if other_image_ids:
                        resolved = []
                        for img_id in other_image_ids:
                            img_info = resolve_image(img_id, image_map)
                            if img_info:
                                resolved.append({
                                    **img_info,
                                    "full_path": f"{images_base_path}/{img_info['path']}",
                                })
                        if resolved:
                            product["other_images"] = resolved

                    out.write(json.dumps(product, ensure_ascii=False) + "\n")

    return total_products, matched_images, skipped_blocked


def main():
    base_path = Path("data/assets/products")
    listings_dir = base_path / "abo-listings/listings/metadata"
    images_csv = base_path / "abo-images-small/images/metadata/images.csv"
    output_path = base_path / "products_merged.ndjson"

    print("Loading image mapping...")
    image_map = load_image_mapping(images_csv)
    print(f"Loaded {len(image_map):,} image mappings")

    print("\nMerging products...")
    total, matched, skipped = merge_products(listings_dir, image_map, output_path)

    print(f"\nDone!")
    print(f"  Total products: {total:,}")
    print(f"  Products with resolved main image: {matched:,}")
    print(f"  Skipped (blocked images): {skipped:,}")
    print(f"  Output: {output_path}")


if __name__ == "__main__":
    main()
