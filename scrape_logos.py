#!/usr/bin/env python3
"""
Scrape company logos from multiple sources:
- Wikipedia (high-quality SVG/PNG logos)
- Company websites
"""

import os
import re
import json
import requests
import subprocess
from pathlib import Path
from urllib.parse import urljoin, unquote
from bs4 import BeautifulSoup
from PIL import Image
import cairosvg
import io

# Company info with Wikipedia page names and domains. We are looking at companies in the .log file from our scraped websites
# extra_logos: list of (index, note) tuples for additional logos to save (e.g., Walmart star icon)
# skip_primary: if True, don't save the primary logo (useful when it's just text)
# 
# SKIPPED PRIMARY LOGOS (just text, easily reproducible):
# - Walmart: text wordmark skipped, but spark icon is saved
# - Crate & Barrel: plain text wordmark
COMPANIES = {
    "Walmart": {
        "domain": "walmart.com",
        "wikipedia": "Walmart",
        "skip_primary": True,  # Text wordmark is easily reproducible
        "extra_logos": [(1, "spark icon")],  # 2nd image is the star/spark
    },
    # "Crate & Barrel": {
    #     "domain": "crateandbarrel.com",
    #     "wikipedia": "Crate_%26_Barrel",
    # },
    "Amazon": {
        "domain": "amazon.com",
        "wikipedia": "Amazon_(company)",
    },
    "Home Depot": {
        "domain": "homedepot.com",
        "wikipedia": "The_Home_Depot",
    },
    "Apple": {
        "domain": "apple.com",
        "wikipedia": "Apple_Inc.",
        "note": "black logo on transparent"
    },
    "Macys": {
        "domain": "macys.com",
        "wikipedia": "Macy%27s",
    },
    "Lowes": {
        "domain": "lowes.com",
        "wikipedia": "Lowe%27s",
    },
    "B&H Photo": {
        "domain": "bhphotovideo.com",
        "wikipedia": "B%26H_Photo_Video",
    },
    "Ulta Beauty": {
        "domain": "ulta.com",
        "wikipedia": "Ulta_Beauty",
    },
    "Slack": {
        "domain": "slack.com",
        "wikipedia": "Slack_(software)",
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# Wikipedia UI icons to skip (not actual logos)
SKIP_PATTERNS = [
    "Increase2.svg",
    "Decrease2.svg",
    "Increase_Negative.svg",
    "OOjs_UI_icon",
    "edit-ltr-progressive",
]

def sanitize_filename(name: str) -> str:
    """Create a safe filename from company name."""
    return name.lower().replace(" ", "_").replace("&", "and").replace("'", "")

def download_file(url: str, filepath: Path) -> bool:
    """Download a file from URL to filepath."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code == 200 and len(response.content) > 100:
            with open(filepath, "wb") as f:
                f.write(response.content)
            return True
    except Exception as e:
        print(f"  Error downloading {url}: {e}")
    return False

def should_skip_image(src: str) -> bool:
    """Check if image URL matches skip patterns (Wikipedia UI icons)."""
    for pattern in SKIP_PATTERNS:
        if pattern in src:
            return True
    return False


def get_wikipedia_logos(company_name: str, wiki_page: str, output_dir: Path, extra_indices: list = None) -> dict:
    """Scrape logos from Wikipedia infobox.
    Returns dict with 'primary' (index 0), 'extras' (specified indices), 'alternates' by position."""
    result = {"primary": None, "extras": {}, "alternates": {}}  # alternates keyed by position (1, 2, 3)
    extra_indices = extra_indices or []
    url = f"https://en.wikipedia.org/wiki/{wiki_page}"

    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        if response.status_code != 200:
            return result

        soup = BeautifulSoup(response.text, "html.parser")

        # Find infobox images (usually contains the logo)
        infobox = soup.find("table", class_=re.compile(r"infobox"))
        if not infobox:
            return result

        # Get all images from infobox
        images = infobox.find_all("img")
        safe_name = sanitize_filename(company_name)

        img_index = 0  # Track actual logo index (after skipping UI icons)
        for img in images:
            if img_index >= 4:  # Get up to 4 actual logos
                break

            src = img.get("src", "")
            if not src:
                continue

            # Skip Wikipedia UI icons
            if should_skip_image(src):
                print(f"  ⊘ Skipping UI icon: {src.split('/')[-1][:40]}...")
                continue

            # Convert thumbnail to full size
            if "thumb/" in src:
                src = src.replace("/thumb/", "/")
                src = "/".join(src.split("/")[:-1])

            if src.startswith("//"):
                src = "https:" + src
            elif src.startswith("/"):
                src = "https://en.wikipedia.org" + src

            ext = Path(unquote(src)).suffix.lower()
            if ext not in [".png", ".svg", ".jpg", ".jpeg", ".webp"]:
                ext = ".png"

            # Determine filename based on index
            if img_index == 0:
                filepath = output_dir / f"{safe_name}{ext}"
                label = "primary"
            elif img_index in extra_indices:
                filepath = output_dir / f"{safe_name}_v{img_index}{ext}"
                label = f"extra (v{img_index})"
            else:
                # Temp file for alternates (will be deleted after stitch)
                filepath = output_dir / f"_temp_{safe_name}_alt{img_index}{ext}"
                label = f"alt{img_index}"

            if download_file(src, filepath):
                print(f"  ✓ Wikipedia logo ({label}): {filepath.name}")
                if img_index == 0:
                    result["primary"] = filepath
                elif img_index in extra_indices:
                    result["extras"][img_index] = filepath
                else:
                    # Store alternate by position for grouped visualization
                    if img_index not in result["alternates"]:
                        result["alternates"][img_index] = []
                    result["alternates"][img_index].append((filepath, company_name))

            img_index += 1

    except Exception as e:
        print(f"  Error scraping Wikipedia for {company_name}: {e}")

    return result

def get_favicon(company_name: str, domain: str, output_dir: Path) -> list:
    """Get high-res favicon from Google."""
    downloaded = []
    url = f"https://www.google.com/s2/favicons?domain={domain}&sz=256"
    safe_name = sanitize_filename(company_name)
    filepath = output_dir / f"{safe_name}_favicon.png"

    if download_file(url, filepath):
        downloaded.append(filepath)
        print(f"  ✓ Favicon: {filepath.name}")

    return downloaded

def load_image(filepath: Path, size: int = 200) -> Image.Image:
    """Load an image file (including SVG) and resize to square."""
    try:
        if filepath.suffix.lower() == ".svg":
            # Convert SVG to PNG using cairosvg
            png_data = cairosvg.svg2png(url=str(filepath), output_width=size, output_height=size)
            img = Image.open(io.BytesIO(png_data))
        else:
            img = Image.open(filepath)

        # Convert to RGBA for consistency
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        # Resize maintaining aspect ratio, then pad to square
        img.thumbnail((size, size), Image.Resampling.LANCZOS)

        # Create white background
        background = Image.new("RGBA", (size, size), (255, 255, 255, 255))

        # Center the image
        offset = ((size - img.width) // 2, (size - img.height) // 2)
        background.paste(img, offset, img if img.mode == "RGBA" else None)

        return background
    except Exception as e:
        print(f"  Error loading {filepath}: {e}")
        return None


def create_stitched_image(logo_files: list, output_path: Path, company_names: list):
    """Create a grid of all logos with labels."""
    cell_size = 200
    padding = 10
    label_height = 30

    n_logos = len(logo_files)
    cols = min(5, n_logos)
    rows = (n_logos + cols - 1) // cols

    total_width = cols * (cell_size + padding) + padding
    total_height = rows * (cell_size + label_height + padding) + padding

    # Create canvas
    canvas = Image.new("RGB", (total_width, total_height), (255, 255, 255))

    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(canvas)

    # Try to get a font
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
    except:
        font = ImageFont.load_default()

    for i, (filepath, name) in enumerate(zip(logo_files, company_names)):
        row = i // cols
        col = i % cols

        x = padding + col * (cell_size + padding)
        y = padding + row * (cell_size + label_height + padding)

        # Load and paste logo
        img = load_image(filepath, cell_size)
        if img:
            canvas.paste(img, (x, y))

        # Draw label
        label_y = y + cell_size + 5
        draw.text((x + cell_size // 2, label_y), name, fill=(0, 0, 0), font=font, anchor="mt")

    canvas.save(output_path)
    print(f"\n✓ Stitched image saved: {output_path}")

def main():
    output_dir = Path(__file__).parent / "data" / "assets" / "company_logos"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading logos to: {output_dir}\n")

    all_logos = []
    all_names = []
    alternates_by_position = {}  # {1: [(path, name), ...], 2: [...], 3: [...]}
    brands_data = []

    for company, info in COMPANIES.items():
        print(f"{company}:")

        # Get extra logo indices if specified (e.g., Walmart spark icon)
        extra_logos = info.get("extra_logos", [])
        extra_indices = [idx for idx, _ in extra_logos]

        # Get logos from Wikipedia
        result = get_wikipedia_logos(company, info["wikipedia"], output_dir, extra_indices)

        skip_primary = info.get("skip_primary", False)
        
        if result["primary"] and not skip_primary:
            all_logos.append(result["primary"])
            all_names.append(company)

            # Add primary to brands.json
            brands_data.append({
                "company": sanitize_filename(company),
                "filename": result["primary"].name,
                "notes": info.get("note", "")
            })

        # Add any extra logos (like Walmart spark) - also add to main preview
        for idx, note in extra_logos:
            if idx in result["extras"]:
                all_logos.append(result["extras"][idx])
                all_names.append(f"{company} ({note})")
                brands_data.append({
                    "company": sanitize_filename(company),
                    "filename": result["extras"][idx].name,
                    "notes": note
                })

        # Collect alternates by position for grouped visualization
        for pos, items in result["alternates"].items():
            if pos not in alternates_by_position:
                alternates_by_position[pos] = []
            alternates_by_position[pos].extend(items)

        if not result["primary"] and not extra_logos:
            print(f"  ✗ No logo found")
        elif skip_primary and not result["extras"]:
            print(f"  ✗ No extra logos found (primary skipped)")

    print(f"\n{'='*50}")
    print(f"Downloaded: {len(all_logos)} logos (including extras)")
    for pos, items in sorted(alternates_by_position.items()):
        print(f"Alternates at position {pos}: {len(items)}")

    # Save brands.json (simplified: company, filename, notes)
    brands_json_path = output_dir / "brands.json"
    with open(brands_json_path, "w") as f:
        json.dump({"logos": brands_data}, f, indent=2)
    print(f"✓ Saved: {brands_json_path}")

    # Create stitched preview image for primary logos + extras
    if all_logos:
        stitch_path = output_dir / "all_logos_preview.png"
        create_stitched_image(all_logos, stitch_path, all_names)

    # Create stitched previews for alternates by position
    temp_files_to_delete = []
    for pos in sorted(alternates_by_position.keys()):
        items = alternates_by_position[pos]
        if items:
            alt_logos = [x[0] for x in items]
            alt_names = [x[1] for x in items]
            alt_stitch_path = output_dir / f"alternates_preview{pos}.png"
            create_stitched_image(alt_logos, alt_stitch_path, alt_names)
            print(f"✓ Alternates position {pos} preview saved: {alt_stitch_path}")
            # Mark temp files for deletion
            temp_files_to_delete.extend(alt_logos)

    # Clean up temp alternate files
    for filepath in temp_files_to_delete:
        try:
            filepath.unlink()
        except Exception:
            pass
    if temp_files_to_delete:
        print(f"✓ Cleaned up {len(temp_files_to_delete)} temp alternate files")

if __name__ == "__main__":
    main()
