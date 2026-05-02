#!/usr/bin/env python3
"""
Stitch Company UI/UX Images

Creates a side-by-side comparison of all UI/UX screenshots for a specific company,
filtered by device type (app, desktop, or mobile).
Data source: Baymard Institute (https://baymard.com) benchmark studies.
"""

import os
import re
import argparse
from pathlib import Path
from PIL import Image


def normalize_company_name(name: str) -> str:
    """Normalize company name for matching (lowercase, hyphenated)."""
    return name.lower().replace(" ", "-").replace("'", "-").replace("'", "-")


def extract_company_from_filename(filename: str) -> str:
    """Extract company name from filename like '1104-home-depot-mobile.jpg' or '1104-home-depot-step-3-mobile.jpg'."""
    # Remove extension
    name = Path(filename).stem
    # Remove leading ID (digits followed by hyphen)
    name = re.sub(r'^\d+-', '', name)
    # Remove device type suffix
    name = re.sub(r'-(mobile|desktop|app)$', '', name)
    # Remove step suffix if present (now at end after device removed)
    name = re.sub(r'-step-\d+$', '', name)
    return name.lower()


def extract_device_type(filename: str) -> str:
    """Extract device type from filename."""
    stem = Path(filename).stem.lower()
    if stem.endswith('-mobile'):
        return 'mobile'
    elif stem.endswith('-desktop'):
        return 'desktop'
    elif stem.endswith('-app'):
        return 'app'
    return 'unknown'


def find_company_images(ui_dir: str, company: str, device_type: str) -> dict[str, str]:
    """
    Find all images for a company filtered by device type.

    Returns dict mapping section name -> image path
    """
    company_normalized = normalize_company_name(company)
    images = {}

    ui_path = Path(ui_dir)

    for section_dir in ui_path.iterdir():
        if not section_dir.is_dir() or section_dir.name.startswith('.'):
            continue

        section_name = section_dir.name

        for img_file in section_dir.iterdir():
            if not img_file.is_file():
                continue
            if img_file.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
                continue

            file_company = extract_company_from_filename(img_file.name)
            file_device = extract_device_type(img_file.name)

            # Match company (fuzzy - check if normalized names match)
            if company_normalized in file_company or file_company in company_normalized:
                if file_device == device_type:
                    images[section_name] = str(img_file)

    return images


def stitch_images(images: dict[str, str], output_path: str,
                  company: str, device_type: str,
                  max_height: int = 2000,
                  padding: int = 20,
                  label_height: int = 40) -> str:
    """
    Stitch images side by side with section labels.

    Args:
        images: Dict mapping section name -> image path
        output_path: Output file path
        company: Company name for title
        device_type: Device type for title
        max_height: Maximum height to scale images to
        padding: Padding between images
        label_height: Height reserved for section labels

    Returns:
        Path to output image
    """
    if not images:
        raise ValueError("No images to stitch")

    # Load and process images
    loaded_images = []
    for section, path in sorted(images.items()):
        img = Image.open(path)

        # Scale to max height while preserving aspect ratio
        if img.height > max_height:
            ratio = max_height / img.height
            new_width = int(img.width * ratio)
            img = img.resize((new_width, max_height), Image.Resampling.LANCZOS)

        loaded_images.append((section, img))

    # Calculate total dimensions
    total_width = sum(img.width for _, img in loaded_images) + padding * (len(loaded_images) + 1)
    max_img_height = max(img.height for _, img in loaded_images)
    total_height = max_img_height + label_height + padding * 2

    # Create canvas
    canvas = Image.new('RGB', (total_width, total_height), color='white')

    # Try to add labels (requires PIL with font support)
    try:
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 16)
        except:
            font = ImageFont.load_default()
    except ImportError:
        draw = None
        font = None

    # Paste images
    x_offset = padding
    for section, img in loaded_images:
        # Draw section label
        if draw and font:
            # Clean up section name for display
            label = section.replace('-', ' ').title()
            draw.text((x_offset, padding // 2), label, fill='black', font=font)

        # Paste image below label
        y_offset = label_height + padding
        canvas.paste(img, (x_offset, y_offset))
        x_offset += img.width + padding

    # Save
    canvas.save(output_path, quality=95)
    return output_path


def list_companies(ui_dir: str) -> dict[str, dict[str, int]]:
    """List all companies and their image counts by device type."""
    companies = {}
    ui_path = Path(ui_dir)

    for section_dir in ui_path.iterdir():
        if not section_dir.is_dir() or section_dir.name.startswith('.'):
            continue

        for img_file in section_dir.iterdir():
            if not img_file.is_file():
                continue
            if img_file.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
                continue

            company = extract_company_from_filename(img_file.name)
            device = extract_device_type(img_file.name)

            if company not in companies:
                companies[company] = {'desktop': 0, 'mobile': 0, 'app': 0, 'unknown': 0}
            companies[company][device] += 1

    return companies


def main():
    parser = argparse.ArgumentParser(
        description="Stitch UI/UX images for a company side by side (source: Baymard Institute)"
    )
    parser.add_argument("--company", "-c", type=str,
                        help="Company name to filter (e.g., 'walmart', 'home-depot')")
    parser.add_argument("--device", "-d", type=str, choices=['desktop', 'mobile', 'app'],
                        default='desktop', help="Device type filter (default: desktop)")
    parser.add_argument("--input", "-i", type=str, default="data/ui_images",
                        help="Input directory containing UI images")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Output file path (default: {company}_{device}_stitched.png)")
    parser.add_argument("--max-height", type=int, default=2000,
                        help="Maximum height for each image (default: 2000)")
    parser.add_argument("--list", "-l", action="store_true",
                        help="List all available companies and their image counts")

    args = parser.parse_args()

    # Validate input directory
    if not os.path.isdir(args.input):
        print(f"Error: Input directory '{args.input}' not found")
        return 1

    # List mode
    if args.list:
        companies = list_companies(args.input)
        print(f"\nAvailable companies ({len(companies)} total):\n")
        print(f"{'Company':<30} {'Desktop':>8} {'Mobile':>8} {'App':>8} {'Total':>8}")
        print("-" * 70)

        for company, counts in sorted(companies.items(), key=lambda x: -sum(x[1].values())):
            total = sum(counts.values()) - counts.get('unknown', 0)
            if total > 0:
                print(f"{company:<30} {counts['desktop']:>8} {counts['mobile']:>8} {counts['app']:>8} {total:>8}")
        return 0

    # Require company for stitching
    if not args.company:
        print("Error: --company is required (use --list to see available companies)")
        return 1

    # Find images
    images = find_company_images(args.input, args.company, args.device)

    if not images:
        print(f"No images found for company '{args.company}' with device type '{args.device}'")
        print("Use --list to see available companies and their counts")
        return 1

    print(f"\nFound {len(images)} images for '{args.company}' ({args.device}):")
    for section in sorted(images.keys()):
        print(f"  - {section}")

    # Generate output path
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "stitched_images")
    os.makedirs(output_dir, exist_ok=True)

    if args.output:
        output_path = os.path.join(output_dir, args.output)
    else:
        company_clean = normalize_company_name(args.company)
        output_path = os.path.join(output_dir, f"{company_clean}_{args.device}_stitched.png")

    # Stitch images
    print(f"\nStitching images...")
    result = stitch_images(
        images,
        output_path,
        args.company,
        args.device,
        max_height=args.max_height
    )

    print(f"Output saved to: {result}")

    # Print image dimensions
    with Image.open(result) as img:
        print(f"Dimensions: {img.width} x {img.height} pixels")

    return 0


if __name__ == "__main__":
    exit(main())
