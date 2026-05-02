#!/usr/bin/env python3
"""
UI Reproducer - Takes a screenshot and uses a local LLM coding CLI to reproduce
the UI. Iteratively improves the reproduction by comparing screenshots.

Usage:
    python reproduce_ui.py <image_path> [--iterations N] [--model MODEL] [--no-splits]

Example:
    python reproduce_ui.py ../data/ui_images/account-dashboard/4022-amazon-desktop.png

Iteration Strategy (default is 2 iterations):
-------------------
Iteration 1: Initial reproduction
    - The LLM backend analyzes the original image and generates App.jsx from scratch

Iteration 2: Split refinement (if image height > 1000px and --no-splits not set)
    - Phase 1: HEADER (top 200px) - always refined separately
    - Phase 2: BODY EVAL - evaluate which body sections need work
    - Phase 3: BODY REFINEMENT - only refine sections identified as needing work

    If image is short (<= 1000px) or --no-splits: standard full-image comparison

Iteration 3+: Standard full-image refinement (if --iterations is set to 3 or more)
    - Compare full original vs full screenshot
    - Fix remaining discrepancies

"""

import argparse
import subprocess
import json
import os
import signal
import socket
import sys
import time
import shutil
import re
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, Page
from PIL import Image
import numpy as np


def kill_process_tree(proc: subprocess.Popen, timeout: int = 5):
    """Kill a process and all its children (the entire process group).

    This is necessary because npm spawns Vite as a child process, and
    terminate() only kills npm, leaving Vite running and holding the port.
    """
    if proc is None:
        return

    try:
        # Try to get the process group ID
        pgid = os.getpgid(proc.pid)

        # Send SIGTERM to the entire process group
        os.killpg(pgid, signal.SIGTERM)

        # Wait for graceful shutdown
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Force kill if still running
            os.killpg(pgid, signal.SIGKILL)
            proc.wait(timeout=2)
    except (ProcessLookupError, PermissionError, OSError):
        # Process already dead or we don't have permission
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except:
                pass


def is_port_available(port: int, timeout: float = 0.5) -> bool:
    """Check if a port is available for use."""
    for family in (socket.AF_INET6, socket.AF_INET):
        for addr in (('::1', port), ('127.0.0.1', port), ('localhost', port)):
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex(addr)
                sock.close()
                if result == 0:
                    return False  # Port is in use
            except:
                pass
    return True  # Port appears available


def wait_for_port_available(port: int, timeout: int = 10) -> bool:
    """Wait for a port to become available."""
    start = time.time()
    while time.time() - start < timeout:
        if is_port_available(port):
            return True
        time.sleep(0.5)
    return False


def find_free_port() -> int:
    """Find a free port by letting the OS assign one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port

from prompts import build_structure_prompt, build_split_section_prompt, build_section_eval_prompt, build_header_prompt, build_header_eval_prompt, build_compliance_attributes_prompt, build_compliance_inputs_prompt


SCRIPT_DIR = Path(__file__).parent
BASE_DIR = SCRIPT_DIR.parent  # pii/ directory - used for relative paths
OUTPUT_BASE = SCRIPT_DIR / "output"
# Use assets_lite for publicDir (logos, payment methods only - small files)
# Products are served via middleware to avoid Vite scanning 400K+ files
ASSETS_DIR = SCRIPT_DIR.parent / "data" / "assets_lite"
PRODUCTS_DIR = SCRIPT_DIR.parent / "data" / "assets" / "products"
# Note: data.json is now created in each output page's src/ directory
TEMPLATE_DIR = SCRIPT_DIR / "template"

# Target width for normalizing high-DPI images (desktop)
TARGET_WIDTH_DESKTOP = 1280
TARGET_WIDTH_MOBILE = 390

# Header height for isolated header refinement
HEADER_HEIGHT = 200


def extract_header(
    image_path: Path,
    output_path: Path,
    header_height: int = HEADER_HEIGHT
) -> dict:
    """
    Extract the header (top portion) of an image.
    Returns dict with path and position info.
    """
    with Image.open(image_path) as img:
        width, height = img.size
        end_y = min(header_height, height)
        header_img = img.crop((0, 0, width, end_y))
        header_img.save(output_path, quality=95)
        print(f"Extracted header: 0-{end_y}px -> {output_path.name}")
        return {
            "path": output_path,
            "start_y": 0,
            "end_y": end_y,
            "part_num": 0,  # 0 = header
            "is_header": True
        }


def split_image_vertically(
    image_path: Path,
    output_dir: Path,
    num_parts: int,
    overlap: int = 300,
    use_positions: list[dict] = None,
    skip_header: int = 0
) -> list[dict]:
    """
    Split an image into vertical parts with overlap.
    Returns list of dicts with path and position info for each part.

    If use_positions is provided, use those exact y positions instead of calculating new ones.
    This ensures we compare the same visual regions even if image heights differ.

    If skip_header > 0, start splitting from that y position (used when header is processed separately).
    """
    with Image.open(image_path) as img:
        width, height = img.size

        # Adjust for header skip
        effective_start = skip_header
        effective_height = height - skip_header

        parts = []
        for i in range(num_parts):
            if use_positions:
                # Use provided positions (from original image)
                start_y = use_positions[i]["start_y"]
                end_y = min(use_positions[i]["end_y"], height)  # Don't exceed this image's height

                # Skip parts that are completely beyond the image height
                if start_y >= height:
                    print(f"  Skipping part {i+1}/{num_parts}: start_y={start_y} >= height={height}")
                    continue

                # Adjust start_y if it would create invalid coordinates
                if start_y >= end_y:
                    print(f"  Adjusting part {i+1}/{num_parts}: start_y={start_y} -> {max(0, end_y - 100)}")
                    start_y = max(0, end_y - 100)  # Ensure at least 100px tall part
            else:
                # Calculate positions based on effective height (after header)
                base_height = effective_height // num_parts
                start_y = effective_start + max(0, i * base_height - (overlap // 2 if i > 0 else 0))
                if i == num_parts - 1:
                    end_y = height
                else:
                    end_y = min(height, effective_start + (i + 1) * base_height + (overlap // 2))

            # Skip if coordinates are invalid
            if start_y >= end_y:
                print(f"  Skipping part {i+1}/{num_parts}: invalid coordinates start_y={start_y} >= end_y={end_y}")
                continue

            # Crop the part
            part_img = img.crop((0, start_y, width, end_y))

            # Save the part
            part_path = output_dir / f"split_part_{i+1}.png"
            part_img.save(part_path, quality=95)

            parts.append({
                "path": part_path,
                "start_y": start_y,
                "end_y": end_y,
                "part_num": i + 1,
                "total_parts": num_parts
            })

            print(f"Split part {i+1}/{num_parts}: y={start_y}-{end_y} ({end_y - start_y}px)")

        return parts


def normalize_image(image_path: Path, output_path: Path, device: str = "desktop") -> Path:
    """
    Normalize high-DPI images to target width while preserving aspect ratio.
    Returns the path to the normalized image.
    """
    target_width = TARGET_WIDTH_DESKTOP if device == "desktop" else TARGET_WIDTH_MOBILE

    try:
        with Image.open(image_path) as img:
            width, height = img.size

            # Only resize if image is larger than target
            if width > target_width:
                ratio = target_width / width
                new_height = int(height * ratio)
                resized = img.resize((target_width, new_height), Image.Resampling.LANCZOS)
                resized.save(output_path, quality=95)
                print(f"Normalized image: {width}x{height} -> {target_width}x{new_height}")
                return output_path
            else:
                # Just copy if already small enough
                shutil.copy(image_path, output_path)
                print(f"Image already at target size: {width}x{height}")
                return output_path
    except Exception as e:
        print(f"WARNING: Could not normalize image: {e}")
        shutil.copy(image_path, output_path)
        return output_path


def get_image_dimensions(image_path: Path) -> dict:
    """Get width and height of an image file."""
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            return {"width": width, "height": height, "path": str(image_path.name)}
    except Exception as e:
        return {"width": None, "height": None, "path": str(image_path.name), "error": str(e)}


def normalize_company_name(company: str) -> str:
    """
    Normalize company names for directory paths.
    Examples:
        "lowe-s" -> "lowes" (from "lowe's")
        "macy-s" -> "macys" (from "macy's")
        "b-h-photo" -> "bh-photo" (normalize B&H Photo)
        "home-depot" -> "home-depot" (unchanged)
    """
    # Remove "-s" suffix that comes from apostrophe-s (e.g., "lowe's" -> "lowe-s" -> "lowes")
    company = re.sub(r'-s$', 's', company)

    # Normalize B&H Photo variations
    if company == "b-h-photo":
        company = "bh-photo"

    return company


def parse_image_path(image_path: Path) -> dict:
    """
    Parse image path to extract metadata.
    Expected format: .../page_type/id-company-device.png
    Also handles: .../page_type/id-company-step-N-device.png
    """
    filename = image_path.stem  # e.g., "4022-amazon-desktop" or "2349-home-depot-step-3-desktop"
    page_type = image_path.parent.name  # e.g., "account-dashboard"

    parts = filename.split("-")
    file_id = parts[0]
    device = parts[-1].lower()
    if device not in ["desktop", "mobile", "app"]:
        device = "unknown"

    if device in ["desktop", "mobile", "app", "unknown"]:
        company_parts = parts[1:-1]
    else:
        company_parts = parts[1:]

    company = "-".join(company_parts) if company_parts else "unknown"

    # Remove -step-N suffix from company name
    company = re.sub(r'-step-\d+$', '', company)
    
    # Normalize company name (e.g., "lowe-s" -> "lowes")
    company = normalize_company_name(company)

    return {
        "id": file_id,
        "company": company,
        "device": device,
        "page_type": page_type,
    }


def get_output_dir(metadata: dict) -> tuple[Path, Path]:
    """Generate output directory path with timestamp versioning."""
    # Structure: output/{device}/{company}/{page_type}/{image_id}/{timestamp}/
    base_dir = OUTPUT_BASE / metadata["device"] / metadata["company"] / metadata["page_type"] / metadata["id"]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    versioned_dir = base_dir / timestamp
    return versioned_dir, base_dir


def read_assets_config():
    """Read brands.json files for company logos and payment methods."""
    assets_info = []

    # Company logos
    logos_brands = ASSETS_DIR / "company_logos" / "brands.json"
    if logos_brands.exists():
        with open(logos_brands) as f:
            data = json.load(f)
            assets_info.append("## Company Logos (in public/company_logos/)")
            for logo in data.get("logos", []):
                note = f" - {logo['notes']}" if logo.get('notes') else ""
                assets_info.append(f"  - {logo['company']}: {logo['filename']}{note}")

    # Payment methods
    payment_brands = ASSETS_DIR / "payment_methods" / "brands.json"
    if payment_brands.exists():
        with open(payment_brands) as f:
            data = json.load(f)
            assets_info.append("\n## Payment Method Logos (in public/payment_methods/)")
            for logo in data.get("logos", []):
                note = f" - {logo['notes']}" if logo.get('notes') else ""
                assets_info.append(f"  - {logo['company']}: {logo['filename']}{note}")

    return "\n".join(assets_info)


class ReproductionLog:
    """Tracks stats and logs for a reproduction run."""

    def __init__(self, output_dir: Path, metadata: dict, source_image_path: Path = None):
        self.output_dir = output_dir
        self.metadata = metadata
        self.source_image_path = source_image_path  # Original image before normalization
        self.start_time = datetime.now()
        self.iterations = []
        self.total_success = 0
        self.total_failed = 0
        self.cost_data = None
        self.image_stats = {
            "original": None,
            "screenshots": []
        }

    def write_initial_log(self):
        """Write initial log with paths filled in, so early exits still have tracking info."""
        # Ensure output_dir exists before writing
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Compute relative paths from BASE_DIR (pii/)
        output_dir_rel = str(self.output_dir.relative_to(BASE_DIR)) if self.output_dir else None
        source_image_rel = None
        if self.source_image_path:
            try:
                source_image_rel = str(self.source_image_path.relative_to(BASE_DIR))
            except ValueError:
                source_image_rel = str(self.source_image_path)

        log_data = {
            "metadata": self.metadata,
            "paths": {
                "source_image": source_image_rel,
                "output_dir": output_dir_rel,
            },
            "start_time": self.start_time.isoformat(),
            "status": "in_progress",
            "iterations": [],
            "image_stats": self.image_stats,
            "summary": {
                "total_iterations": 0,
                "successful": 0,
                "failed": 0
            }
        }

        log_path = self.output_dir / "reproduction.log"
        with open(log_path, "w") as f:
            json.dump(log_data, f, indent=2)
        print(f"Initial log written to: {log_path}")

    def log_original_image(self, image_path: Path):
        dims = get_image_dimensions(image_path)
        self.image_stats["original"] = dims
        print(f"Original image: {dims['width']}x{dims['height']} ({dims['path']})")

    def log_screenshot(self, screenshot_path: Path, label: str):
        dims = get_image_dimensions(screenshot_path)
        dims["label"] = label
        self.image_stats["screenshots"].append(dims)
        print(f"Screenshot [{label}]: {dims['width']}x{dims['height']}")

    def log_iteration(self, iteration: int, success: bool, duration_sec: float, cost_info: dict = None):
        entry = {
            "iteration": iteration,
            "success": success,
            "duration_sec": round(duration_sec, 2),
            "timestamp": datetime.now().isoformat()
        }
        if cost_info:
            entry["cost"] = cost_info
        self.iterations.append(entry)

        if success:
            self.total_success += 1
        else:
            self.total_failed += 1

    def fetch_cost_data(self):
        """Calculate total cost from all iterations (now extracted from stream-json output)."""
        total_cost = 0.0
        total_input_tokens = 0
        total_output_tokens = 0
        total_cache_read_tokens = 0
        total_cache_creation_tokens = 0

        for iteration in self.iterations:
            if iteration.get("cost"):
                cost = iteration["cost"]
                if isinstance(cost, dict):
                    # Handle both single iteration costs and split iteration costs
                    if "turn_cost" in cost:
                        total_cost += cost.get("turn_cost", 0)
                        total_input_tokens += cost.get("input_tokens", 0)
                        total_output_tokens += cost.get("output_tokens", 0)
                        total_cache_read_tokens += cost.get("cache_read_input_tokens", 0)
                        total_cache_creation_tokens += cost.get("cache_creation_input_tokens", 0)
                    # Handle split iteration with multiple parts
                    elif "parts" in cost:
                        for part in cost.get("parts", []):
                            total_cost += part.get("turn_cost", 0)
                            total_input_tokens += part.get("input_tokens", 0)
                            total_output_tokens += part.get("output_tokens", 0)
                            total_cache_read_tokens += part.get("cache_read_input_tokens", 0)
                            total_cache_creation_tokens += part.get("cache_creation_input_tokens", 0)

        self.cost_data = {
            "total_cost": round(total_cost, 4),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_creation_tokens": total_cache_creation_tokens,
            "source": "stream-json output (from Claude CLI --output-format stream-json)"
        }

        print(f"Total cost: ${self.cost_data['total_cost']:.4f} | Input: {total_input_tokens} | Output: {total_output_tokens} | Cache read: {total_cache_read_tokens}")

    def write_log(self):
        end_time = datetime.now()
        total_duration = (end_time - self.start_time).total_seconds()
        self.fetch_cost_data()

        # Compute relative paths from BASE_DIR (pii/)
        output_dir_rel = str(self.output_dir.relative_to(BASE_DIR)) if self.output_dir else None
        source_image_rel = None
        if self.source_image_path:
            try:
                source_image_rel = str(self.source_image_path.relative_to(BASE_DIR))
            except ValueError:
                # If source image is outside BASE_DIR, use absolute path
                source_image_rel = str(self.source_image_path)

        log_data = {
            "metadata": self.metadata,
            "paths": {
                "source_image": source_image_rel,  # Original image before normalization (relative to pii/)
                "output_dir": output_dir_rel,  # Output directory (relative to pii/)
            },
            "start_time": self.start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "total_duration_sec": round(total_duration, 2),
            "status": "completed",  # Differentiates from "in_progress" in initial log
            "iterations": self.iterations,
            "image_stats": self.image_stats,
            "summary": {
                "total_iterations": len(self.iterations),
                "successful": self.total_success,
                "failed": self.total_failed
            },
            "cost": self.cost_data if self.cost_data else {
                "note": "Cost data extracted from Claude CLI stream-json output"
            }
        }

        log_path = self.output_dir / "reproduction.log"
        with open(log_path, "w") as f:
            json.dump(log_data, f, indent=2)

        print(f"Log written to: {log_path}")
        return log_path


def get_assets_relative_path(output_dir: Path) -> str:
    try:
        rel_path = os.path.relpath(ASSETS_DIR, output_dir)
        return rel_path
    except ValueError:
        return str(ASSETS_DIR)


def get_products_relative_path(output_dir: Path) -> str:
    try:
        rel_path = os.path.relpath(PRODUCTS_DIR, output_dir)
        return rel_path
    except ValueError:
        return str(PRODUCTS_DIR)

def copy_template(output_dir: Path, assets_rel_path: str, products_rel_path: str):
    if not TEMPLATE_DIR.exists():
        print(f"WARNING: Template directory not found at {TEMPLATE_DIR}")
        return False

    # Copy template but exclude node_modules (shared from template dir)
    shutil.copytree(
        TEMPLATE_DIR,
        output_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns('node_modules', 'dist', '.vite')
    )

    # Create symlink to shared node_modules in template directory
    template_node_modules = TEMPLATE_DIR / "node_modules"
    output_node_modules = output_dir / "node_modules"

    if template_node_modules.exists():
        # Remove existing node_modules if present (shouldn't happen, but just in case)
        if output_node_modules.exists():
            if output_node_modules.is_symlink():
                output_node_modules.unlink()
            else:
                shutil.rmtree(output_node_modules)

        # Create symlink to shared node_modules (use absolute path for reliability)
        os.symlink(template_node_modules.resolve(), output_node_modules, target_is_directory=True)

    vite_config_path = output_dir / "vite.config.js"
    if vite_config_path.exists():
        content = vite_config_path.read_text()
        content = content.replace("__ASSETS_PATH__", assets_rel_path)
        content = content.replace("__PRODUCTS_PATH__", products_rel_path)
        # data.json is now in src/ - update alias to point there
        content = content.replace("__DATA_PATH__", "./src/data.json")
        vite_config_path.write_text(content)

    return True


def generate_requires_json(output_dir: Path) -> Path:
    """
    Scan App.jsx for data.* references and create requires.json
    listing all data variables actually used.

    Also detects which PII fields are used as inputs (via getPartialProps)
    vs text displays (via data.PII_*), and which use getSelectValue (dropdowns).
    """
    app_jsx_path = output_dir / "src" / "App.jsx"
    requires_path = output_dir / "requires.json"

    if not app_jsx_path.exists():
        print(f"WARNING: App.jsx not found at {app_jsx_path}")
        return None

    # Read the App.jsx content
    with open(app_jsx_path, "r") as f:
        content = f.read()

    # Find all data.SOMETHING references
    # Matches: data.PII_FULLNAME, data.PRODUCT1_NAME, etc.
    pattern = r'\bdata\.([A-Z][A-Z0-9_]*)\b'
    matches = re.findall(pattern, content)

    # Get unique values and sort them
    unique_refs = sorted(set(matches))

    # Categorize the references
    pii_refs = [ref for ref in unique_refs if ref.startswith("PII_")]
    product_refs = [ref for ref in unique_refs if ref.startswith("PRODUCT")]
    order_refs = [ref for ref in unique_refs if ref.startswith("ORDER_")]
    other_refs = [ref for ref in unique_refs if not ref.startswith("PII_") and not ref.startswith("PRODUCT") and not ref.startswith("ORDER_")]

    # Detect PII form fields (text inputs and dropdowns) with unified DOM ordering
    # Use finditer to get positions for consistent ordering across both types
    input_pattern = r"getPartialProps\(['\"]([A-Z][A-Z0-9_]*)['\"]"
    select_pattern = r"getSelect(?:Props|Value)\(['\"]([A-Z][A-Z0-9_]*)['\"]"

    # Collect all matches with positions and types
    all_form_fields = []
    for match in re.finditer(input_pattern, content):
        field = match.group(1)
        if field.startswith("PII_"):
            all_form_fields.append((match.start(), field, 'input'))
    for match in re.finditer(select_pattern, content):
        field = match.group(1)
        if field.startswith("PII_"):
            all_form_fields.append((match.start(), field, 'select'))

    # Sort by position (DOM order)
    all_form_fields.sort(key=lambda x: x[0])

    # Dedupe while preserving order, separate into inputs and selects
    # Also build combined list in DOM order for partial fill
    seen = set()
    pii_inputs = []
    pii_selects = []
    pii_form_fields = []  # Combined list in DOM order (for partial fill)
    for _, field, field_type in all_form_fields:
        if field not in seen:
            seen.add(field)
            pii_form_fields.append(field)  # Add to combined list first
            if field_type == 'input':
                pii_inputs.append(field)
            else:
                pii_selects.append(field)

    # Count product indices used
    product_indices = set()
    for ref in product_refs:
        match = re.match(r'PRODUCT(\d+)_', ref)
        if match:
            product_indices.add(int(match.group(1)))

    requires_data = {
        "generated_at": datetime.now().isoformat(),
        "source_file": "src/App.jsx",
        "summary": {
            "total_references": len(unique_refs),
            "pii_fields": len(pii_refs),
            "product_fields": len(product_refs),
            "order_fields": len(order_refs),
            "products_used": sorted(product_indices),
            "other_fields": len(other_refs)
        },
        "required_fields": {
            "pii": pii_refs,
            "pii_inputs": pii_inputs,      # Fields using getPartialProps (text inputs)
            "pii_selects": pii_selects,    # Fields using getSelectProps (dropdowns)
            "pii_form_fields": pii_form_fields,  # Combined inputs+selects in DOM order (for partial fill)
            "products": product_refs,
            "order": order_refs,
            "other": other_refs
        },
        "all_fields": unique_refs
    }

    with open(requires_path, "w") as f:
        json.dump(requires_data, f, indent=2)

    print(f"\nGenerated requires.json:")
    print(f"  - PII fields: {len(pii_refs)} ({', '.join(pii_refs[:3])}{'...' if len(pii_refs) > 3 else ''})")
    print(f"  - PII inputs: {len(pii_inputs)} ({', '.join(pii_inputs[:3])}{'...' if len(pii_inputs) > 3 else ''})")
    print(f"  - PII selects: {len(pii_selects)}")
    print(f"  - Product fields: {len(product_refs)} (products: {sorted(product_indices)})")
    print(f"  - Order fields: {len(order_refs)} ({', '.join(order_refs[:3])}{'...' if len(order_refs) > 3 else ''})")
    print(f"  - Total unique references: {len(unique_refs)}")
    print(f"  - Saved to: {requires_path}")

    return requires_path


def create_default_data_json(output_dir: Path) -> Path:
    """Create a default data.json in the output page's src/ directory."""
    data_json_path = output_dir / "src" / "data.json"

    if data_json_path.exists():
        return data_json_path

    data = {
        # Name - separated and combined
        "PII_FIRSTNAME": "John",
        "PII_LASTNAME": "Doe",
        "PII_FULLNAME": "John Doe",

        # Personal
        "PII_EMAIL": "john.doe@example.com",
        "PII_PHONE": "(555) 123-4567",
        "PII_AVATAR": "/placeholders/avatar.png",

        # Address - full and separated
        "PII_ADDRESS": "123 Main St, Chicago, IL 60601",
        "PII_STREET": "123 Main St",
        "PII_CITY": "Chicago",
        "PII_STATE": "Illinois",
        "PII_STATE_ABBR": "IL",
        "PII_POSTCODE": "60601",

        # Account
        "PII_ACCOUNT_ID": "USR-12345678",
        "PII_CARD_LAST4": "4242",

        # Products
        "PRODUCT1_NAME": "Premium Widget",
        "PRODUCT1_PRICE": "$29.99",
        "PRODUCT1_IMAGE": "/placeholders/product1.png",
        "PRODUCT1_DESC": "High-quality widget for everyday use",
        "PRODUCT2_NAME": "Deluxe Gadget",
        "PRODUCT2_PRICE": "$49.99",
        "PRODUCT2_IMAGE": "/placeholders/product2.png",
        "PRODUCT2_DESC": "Advanced gadget with premium features",
        "PRODUCT3_NAME": "Basic Tool",
        "PRODUCT3_PRICE": "$9.99",
        "PRODUCT3_IMAGE": "/placeholders/product3.png",
        "PRODUCT3_DESC": "Essential tool for beginners",
    }

    # Ensure src/ directory exists
    data_json_path.parent.mkdir(parents=True, exist_ok=True)

    with open(data_json_path, "w") as f:
        json.dump(data, f, indent=2)
    return data_json_path


def get_opencode_model(claude_model: str) -> str:
    """Map Claude model names to OpenCode model names."""
    model_map = {
        "sonnet": "openai/gpt-4o",
        "haiku": "openai/gpt-4o-mini",
        "opus": "openai/o3",
    }
    # If it's already in provider/model format, use as-is
    if "/" in claude_model:
        return claude_model
    return model_map.get(claude_model, "openai/gpt-4o")


def run_claude(
    prompt: str,
    output_dir: Path,
    allowed_tools: list[str],
    timeout: int = 600,
    retries: int = 2,
    log_name: str = "claude"
) -> tuple[bool, float, dict | None, str]:
    """Run LLM CLI (Claude or OpenCode) with JSON output and save detailed structured logs.
    Returns (success, duration, cost_info, final_result_text)."""
    backend = os.environ.get("LLM_BACKEND", "claude")
    model = os.environ.get("LLM_MODEL")

    if backend == "opencode":
        # OpenCode CLI command
        opencode_bin = os.environ.get("OPENCODE_BIN") or shutil.which("opencode") or "opencode"
        cmd = [
            opencode_bin,
            "run",
            prompt,
            "--format", "json",
            "--agent", "build",  # Use build agent for full tool access
        ]
        if model:
            opencode_model = get_opencode_model(model)
            cmd.extend(["-m", opencode_model])
    else:
        # Claude CLI command (default)
        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--allowedTools", *allowed_tools,
        ]
        if model:
            cmd.extend(["--model", model])

    log_file = output_dir / f"{log_name}.log"

    start_time = time.time()
    backend_name = "OpenCode" if backend == "opencode" else "Claude"

    for attempt in range(retries):
        try:
            print(f"Running {backend_name} (attempt {attempt + 1}/{retries}, timeout={timeout}s)...")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(output_dir)
            )

            duration = time.time() - start_time

            # Parse JSON output based on backend
            if backend == "opencode":
                parsed_data = parse_opencode_json_output(result.stdout)
            else:
                parsed_data = parse_claude_json_output(result.stdout)

            # Extract cost info from parsed data
            cost_info = None
            if parsed_data.get("total_cost_usd") is not None and parsed_data.get("usage"):
                usage = parsed_data["usage"]
                total_cost = parsed_data["total_cost_usd"]

                cost_info = {
                    "turn_cost": round(total_cost, 4),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                    "model": parsed_data.get("model", "unknown")
                }
                
                # Include per-model breakdown if available
                if parsed_data.get("model_usage"):
                    cost_info["model_usage"] = parsed_data["model_usage"]

                print(f"  Cost: ${cost_info['turn_cost']:.4f} | Tokens: in={cost_info['input_tokens']} out={cost_info['output_tokens']} cache_read={cost_info['cache_read_input_tokens']}")

            # Save structured log
            with open(log_file, "w") as f:
                f.write(f"=== {backend_name.upper()} LOG: {log_name} ===\n")
                f.write(f"Timestamp: {datetime.now().isoformat()}\n")
                f.write(f"Duration: {duration:.2f}s\n")
                f.write(f"Return code: {result.returncode}\n\n")

                if cost_info:
                    f.write(f"=== COST INFO ===\n")
                    f.write(json.dumps(cost_info, indent=2))
                    f.write("\n\n")

                f.write(f"=== PROMPT ===\n{prompt}\n\n")

                # Write summary of messages
                f.write(f"=== MESSAGE SUMMARY ===\n")
                for msg_type, count in parsed_data.get("message_counts", {}).items():
                    f.write(f"{msg_type}: {count}\n")
                f.write("\n")

                # Write tool calls
                if parsed_data.get("tool_calls"):
                    f.write(f"=== TOOL CALLS ({len(parsed_data['tool_calls'])}) ===\n")
                    for i, tool in enumerate(parsed_data["tool_calls"], 1):
                        f.write(f"\n--- Tool {i}: {tool['name']} ---\n")
                        f.write(json.dumps(tool, indent=2))
                        f.write("\n")
                    f.write("\n")

                # Write final result
                if parsed_data.get("final_result"):
                    f.write(f"=== FINAL RESULT ===\n{parsed_data['final_result']}\n\n")

                if result.stderr:
                    f.write(f"\n=== STDERR ===\n{result.stderr}\n")

            print(f"{backend_name} log saved to: {log_file}")

            final_result = parsed_data.get("final_result", "")

            if result.returncode == 0:
                if final_result:
                    preview = final_result[:500]
                    print(f"{backend_name} output:\n{preview}{'...' if len(final_result) > 500 else ''}")
                return True, duration, cost_info, final_result
            else:
                print(f"{backend_name} error: {result.stderr}")
                return False, duration, cost_info, final_result

        except subprocess.TimeoutExpired:
            print(f"WARNING: {backend_name} timed out (attempt {attempt + 1}/{retries})")
            # Save timeout info to log
            with open(log_file, "w") as f:
                f.write(f"=== {backend_name.upper()} LOG: {log_name} ===\n")
                f.write(f"Timestamp: {datetime.now().isoformat()}\n")
                f.write(f"Status: TIMEOUT after {timeout}s (attempt {attempt + 1})\n")
                f.write(f"\n=== PROMPT ===\n{prompt}\n")

            if attempt < retries - 1:
                print("Retrying...")
                continue
            else:
                return False, time.time() - start_time, None, ""
        except FileNotFoundError:
            cmd_name = "opencode" if backend == "opencode" else "claude"
            print(f"ERROR: '{cmd_name}' command not found")
            return False, time.time() - start_time, None, ""

    return False, time.time() - start_time, None, ""


def parse_claude_json_output(stdout: str) -> dict:
    """
    Parse the stream-json output from Claude CLI.
    Extracts tool calls, usage stats, messages, cost, and final result.
    """
    parsed = {
        "tool_calls": [],
        "message_counts": {},
        "usage": {},
        "model": None,
        "final_result": None,
        "total_cost_usd": None,
        "model_usage": None,
        "all_messages": []
    }

    # Parse each line as JSON
    for line in stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            msg_type = obj.get("type")

            # Count message types
            if msg_type:
                parsed["message_counts"][msg_type] = parsed["message_counts"].get(msg_type, 0) + 1

            # Extract tool calls from assistant messages
            if msg_type == "assistant" and obj.get("message", {}).get("content"):
                content = obj["message"]["content"]
                if isinstance(content, list):
                    for item in content:
                        if item.get("type") == "tool_use":
                            parsed["tool_calls"].append({
                                "id": item.get("id"),
                                "name": item.get("name"),
                                "input": item.get("input")
                            })

                # Get model info
                if obj.get("message", {}).get("model"):
                    parsed["model"] = obj["message"]["model"]

                # Get usage info
                if obj.get("message", {}).get("usage"):
                    usage = obj["message"]["usage"]
                    # Accumulate tokens across all assistant messages
                    for key in ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"]:
                        parsed["usage"][key] = parsed["usage"].get(key, 0) + usage.get(key, 0)

            # Extract final result and cost info
            if msg_type == "result":
                parsed["final_result"] = obj.get("result", "")
                # Also get usage from result if available
                if obj.get("usage"):
                    parsed["usage"] = obj["usage"]
                # Extract cost information if available
                if obj.get("total_cost_usd") is not None:
                    parsed["total_cost_usd"] = obj["total_cost_usd"]
                if obj.get("modelUsage"):
                    parsed["model_usage"] = obj["modelUsage"]

            # Store all messages for debugging
            parsed["all_messages"].append(obj)

        except json.JSONDecodeError:
            # Skip lines that aren't valid JSON
            continue

    return parsed


def parse_opencode_json_output(stdout: str) -> dict:
    """
    Parse the JSON output from OpenCode CLI.
    Extracts tool calls, usage stats, messages, cost, and final result.
    """
    parsed = {
        "tool_calls": [],
        "message_counts": {},
        "usage": {},
        "model": None,
        "final_result": None,
        "total_cost_usd": None,
        "all_messages": []
    }

    final_text_parts = []

    for line in stdout.strip().split("\n"):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            msg_type = obj.get("type")

            if msg_type:
                parsed["message_counts"][msg_type] = parsed["message_counts"].get(msg_type, 0) + 1

            # Extract text output
            if msg_type == "text":
                part = obj.get("part", {})
                if part.get("text"):
                    final_text_parts.append(part["text"])

            # Extract tool calls
            elif msg_type == "tool_use":
                part = obj.get("part", {})
                state = part.get("state", {})
                parsed["tool_calls"].append({
                    "id": part.get("id"),
                    "name": part.get("tool"),
                    "input": state.get("input"),
                    "output": state.get("output"),
                })

            # Extract cost and token info from step_finish
            elif msg_type == "step_finish":
                part = obj.get("part", {})
                if part.get("cost") is not None:
                    parsed["total_cost_usd"] = (parsed["total_cost_usd"] or 0) + part["cost"]
                tokens = part.get("tokens", {})
                if tokens:
                    parsed["usage"]["input_tokens"] = parsed["usage"].get("input_tokens", 0) + tokens.get("input", 0)
                    parsed["usage"]["output_tokens"] = parsed["usage"].get("output_tokens", 0) + tokens.get("output", 0)
                    cache = tokens.get("cache", {})
                    parsed["usage"]["cache_read_input_tokens"] = parsed["usage"].get("cache_read_input_tokens", 0) + cache.get("read", 0)
                    parsed["usage"]["cache_creation_input_tokens"] = parsed["usage"].get("cache_creation_input_tokens", 0) + cache.get("write", 0)

            parsed["all_messages"].append(obj)

        except json.JSONDecodeError:
            continue

    # Combine text parts as final result
    parsed["final_result"] = "".join(final_text_parts)

    return parsed


def install_dependencies(output_dir: Path) -> bool:
    """Check that node_modules symlink exists (created by copy_template)."""
    node_modules = output_dir / "node_modules"
    if node_modules.is_symlink():
        print("Using shared node_modules from template (symlinked)")
        return True

    print(f"ERROR: node_modules symlink missing at {node_modules}")
    print("Make sure template/node_modules exists (run: cd template && npm install)")
    return False


def check_server_serves_content(port: int, timeout: float = 2.0) -> bool:
    """Check if server is actually serving HTML content (not just accepting connections)."""
    import http.client

    for host in ['localhost', '127.0.0.1']:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.request('GET', '/')
            response = conn.getresponse()
            body = response.read(1000).decode('utf-8', errors='ignore')
            conn.close()

            if response.status == 200 and ('<html' in body.lower() or '<!doctype' in body.lower()):
                return True
        except Exception:
            pass

    return False


def start_dev_server(output_dir: Path, port: int = 5173, timeout: int = 20) -> subprocess.Popen:
    print(f"Starting dev server on port {port}...")

    # Wait for port to be available (in case previous server didn't fully terminate)
    if not wait_for_port_available(port, timeout=5):
        print(f"WARNING: Port {port} still in use, waiting...")
        if not wait_for_port_available(port, timeout=10):
            raise RuntimeError(f"Port {port} is still in use after waiting 15 seconds")

    proc = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(port)],
        cwd=str(output_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True  # Create new process group so we can kill npm + vite together
    )

    # Wait for server to actually serve content (not just accept connections)
    start_time = time.time()
    while time.time() - start_time < timeout:
        if proc.poll() is not None:
            raise RuntimeError(f"Dev server exited with code {proc.returncode}")

        if check_server_serves_content(port):
            print(f"  Server ready after {time.time() - start_time:.1f}s")
            return proc

        time.sleep(0.5)

    # Timeout - kill and raise
    kill_process_tree(proc)
    raise RuntimeError(f"Dev server failed to serve content within {timeout}s")



def wait_for_react_render(page: Page, max_retries: int = 3) -> bool:
    """Wait for React to render content, with retries if page is blank.

    Returns True if content rendered, False if still blank after retries.
    """
    for attempt in range(max_retries):
        # Initial wait for Vite/React to start rendering
        # Longer on first attempt since server might still be warming up
        initial_wait = 2000 if attempt == 0 else 1500
        page.wait_for_timeout(initial_wait)

        # Wait for body to have actual content
        try:
            page.wait_for_function(
                "() => document.body && document.body.innerText.trim().length > 50",
                timeout=15000
            )
            # Extra settle time for styles/images
            page.wait_for_timeout(1000)
            return True
        except:
            pass

        # Check if page is blank
        text_length = page.evaluate("() => (document.body?.innerText || '').trim().length")

        if text_length > 50:
            # Content exists, just took a while
            page.wait_for_timeout(500)
            return True

        if attempt < max_retries - 1:
            # Page is blank - try refreshing
            print(f"    Page blank (text_length={text_length}), retry {attempt + 1}/{max_retries}")
            page.wait_for_timeout(500)  # Brief pause before reload
            page.reload(wait_until="networkidle", timeout=30000)

    # Final check
    text_length = page.evaluate("() => (document.body?.innerText || '').trim().length")
    if text_length < 50:
        print(f"    WARNING: Page still appears blank after {max_retries} retries (text_length={text_length})")
        return False
    return True


def is_blank_screenshot(image_path: Path, white_threshold: float = 0.995, variance_threshold: float = 100) -> bool:
    """
    Check if a screenshot is blank (mostly white with low variance).

    Returns True if the image appears blank/white.

    Args:
        image_path: Path to the screenshot
        white_threshold: Fraction of pixels that must be "white" (>250 in all channels) to consider blank
        variance_threshold: If pixel variance is below this, image is likely blank
    """
    try:
        with Image.open(image_path) as img:
            # Convert to RGB if necessary
            if img.mode != 'RGB':
                img = img.convert('RGB')

            pixels = np.array(img)

            # Check 1: Pixel variance - blank images have very low variance
            pixel_variance = np.var(pixels)
            if pixel_variance < variance_threshold:
                print(f"  Blank check: variance={pixel_variance:.1f} (threshold={variance_threshold})")
                return True

            # Check 2: Percentage of white pixels
            # A pixel is "white" if all RGB values are > 250
            white_pixels = np.all(pixels > 250, axis=2)
            white_fraction = np.mean(white_pixels)

            if white_fraction > white_threshold:
                print(f"  Blank check: {white_fraction*100:.1f}% white pixels (threshold={white_threshold*100}%)")
                return True

            return False
    except Exception as e:
        print(f"  Warning: Could not check if screenshot is blank: {e}")
        return False


class BlankScreenshotError(Exception):
    """Raised when a screenshot is blank/white, indicating render failure."""
    pass


def take_screenshot(output_dir: Path, screenshot_name: str, port: int = 5173) -> Path:
    screenshot_path = output_dir / f"{screenshot_name}.png"
    url = f"http://localhost:{port}"
    print(f"Taking screenshot of {url}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url, wait_until="networkidle", timeout=30000)

        # Wait for React to render content (with retries if blank)
        wait_for_react_render(page)

        page.screenshot(path=str(screenshot_path), full_page=True)
        browser.close()

    # Check if screenshot is blank
    if is_blank_screenshot(screenshot_path):
        raise BlankScreenshotError(
            f"Screenshot '{screenshot_name}' is blank/white. "
            f"This usually means the React app failed to render. "
            f"Check App.jsx for syntax errors or the dev server logs."
        )

    print(f"Screenshot saved to: {screenshot_path}")
    return screenshot_path


def reproduce_ui(image_path: str, iterations: int = 3, port: int = None):
    image_path = Path(image_path).resolve()

    if not image_path.exists():
        print(f"Error: Image not found at {image_path}")
        sys.exit(1)

    metadata = parse_image_path(image_path)
    output_dir, base_dir = get_output_dir(metadata)

    # Use provided port or find a free one
    dev_port = port if port is not None else find_free_port()

    print(f"\n{'='*60}\nUI Reproducer\n{'='*60}")
    print(f"Source: {image_path}")
    print(f"Output: {output_dir}")
    print(f"Dev server port: {dev_port}")

    reproduction_log = ReproductionLog(output_dir, metadata, source_image_path=image_path)
    base_dir.mkdir(parents=True, exist_ok=True)

    # Track completion status for cleanup
    completed_successfully = False

    try:
    
        # Setup project paths
        output_dir.mkdir(parents=True, exist_ok=True)
        assets_rel_path = get_assets_relative_path(output_dir)
        products_rel_path = get_products_relative_path(output_dir)
    
        # Setup project (template already includes src/data.json from last generation)
        shutil.rmtree(output_dir)
        copy_template(output_dir, assets_rel_path, products_rel_path)
    
        # Verify data.json was copied from template
        data_json_path = output_dir / "src" / "data.json"
        if not data_json_path.exists():
            raise FileNotFoundError(
                f"data.json not found at {data_json_path}!\n"
                f"Template should include src/data.json. Check that:\n"
                f"  1. {TEMPLATE_DIR / 'src' / 'data.json'} exists\n"
                f"  2. copy_template() is working correctly\n"
                f"  3. generate_data_variants.py was run to populate template data"
            )
    
        (output_dir / "public" / "placeholders").mkdir(parents=True, exist_ok=True)

        # Write initial log so paths are captured even if we quit early
        reproduction_log.write_initial_log()

        # Prepare images
        original_copy = output_dir / "original.png"
        normalized_image = normalize_image(image_path, original_copy, device=metadata["device"])
        reproduction_log.log_original_image(image_path)
        normalized_dims = get_image_dimensions(normalized_image)
    
        assets_config = read_assets_config()
    
        # ITERATION 1
        print(f"\n{'='*60}\nITERATION 1: Initial reproduction\n{'='*60}\n")
        
        # Updated: NO tags config, embedded data variables
        initial_prompt = build_structure_prompt(
            str(normalized_image), str(output_dir), assets_config
        )
    
        success, duration, cost_info, _ = run_claude(initial_prompt, output_dir, ["Read", "Edit", "Write"], log_name="iteration_1")
        reproduction_log.log_iteration(1, success, duration, cost_info)
    
        if not success or not install_dependencies(output_dir):
            print("Initial reproduction failed!")
            reproduction_log.write_log()
            return

        # VERIFICATION PASS 1 - Attribute marking
        print(f"\n{'='*60}\nVERIFICATION PASS 1: Attribute marking\n{'='*60}\n")

        attrs_prompt = build_compliance_attributes_prompt(str(output_dir), str(normalized_image))
        v1_success, v1_duration, v1_cost, v1_result = run_claude(
            attrs_prompt, output_dir, ["Read", "Edit"],
            timeout=300, log_name="verification_attributes"
        )
        reproduction_log.log_iteration("verification_attributes", v1_success, v1_duration, v1_cost)

        if v1_result:
            if "no issues" in v1_result.lower() or "all checks pass" in v1_result.lower():
                print("Attribute marking passed!")
            else:
                print(f"Attribute marking result:\n{v1_result[:300]}...")

        # VERIFICATION PASS 2 - Input handling
        print(f"\n{'='*60}\nVERIFICATION PASS 2: Input handling\n{'='*60}\n")

        inputs_prompt = build_compliance_inputs_prompt(str(output_dir))
        v2_success, v2_duration, v2_cost, v2_result = run_claude(
            inputs_prompt, output_dir, ["Read", "Edit"],
            timeout=300, log_name="verification_inputs"
        )
        reproduction_log.log_iteration("verification_inputs", v2_success, v2_duration, v2_cost)

        if v2_result:
            if "no issues" in v2_result.lower() or "all checks pass" in v2_result.lower():
                print("Input handling passed!")
            else:
                print(f"Input handling result:\n{v2_result[:300]}...")

        # ITERATIONS 2+
        for i in range(2, iterations + 1):
            print(f"\n{'='*60}\nITERATION {i}: Improving reproduction\n{'='*60}\n")
            server_proc = start_dev_server(output_dir, dev_port)
    
            try:
                screenshot_path = take_screenshot(output_dir, f"iteration_{i-1}", dev_port)
                reproduction_log.log_screenshot(screenshot_path, f"iteration_{i-1}")
    
                normalized_height = normalized_dims.get("height", 0)
    
                # SPLIT STRATEGY (Only iter 2 and tall images, unless --no-splits)
                no_splits = os.environ.get("REPRODUCE_NO_SPLITS") == "1"
                if i == 2 and normalized_height > 1000 and not no_splits:
                    total_duration = 0.0
                    total_cost = 0.0
                    all_success = True
                    part_costs = []
    
                    # === PHASE 1: HEADER (evaluate then refine if needed) ===
                    print(f"\n--- HEADER EVALUATION (0-{HEADER_HEIGHT}px) ---")
    
                    # Extract headers from both images
                    orig_header = extract_header(normalized_image, output_dir / "original_header.png")
                    ss_header = extract_header(screenshot_path, output_dir / "screenshot_header.png")
    
                    # Evaluate if header needs refinement
                    header_eval_prompt = build_header_eval_prompt(
                        str(orig_header["path"]), str(ss_header["path"]), str(output_dir)
                    )
                    _, eval_duration, eval_cost, header_eval_text = run_claude(
                        header_eval_prompt, output_dir, ["Read"],
                        timeout=120, log_name="iteration_2_header_eval"
                    )
                    total_duration += eval_duration
                    if eval_cost:
                        total_cost += eval_cost.get("turn_cost", 0)
                        part_costs.append({"part": "header_eval", **eval_cost})
    
                    # Parse header evaluation result from Claude's actual output
                    header_needs_refinement = True  # Default: refine
                    if header_eval_text:
                        # Look for JSON - prefer code blocks, then raw JSON
                        json_str = None
                        json_block_match = re.search(r'```json\s*(\{[^`]+\})\s*```', header_eval_text, re.DOTALL)
                        if json_block_match:
                            json_str = json_block_match.group(1).strip()
                        else:
                            # Find JSON object directly in the result
                            json_match = re.search(r'\{\s*"needs_refinement"\s*:\s*(true|false)\s*,\s*"reason"\s*:\s*"[^"]*"\s*\}', header_eval_text)
                            if json_match:
                                json_str = json_match.group()
    
                        if json_str:
                            try:
                                eval_result = json.loads(json_str)
                                header_needs_refinement = eval_result.get("needs_refinement", True)
                                reason = eval_result.get("reason", "")
                                print(f"  Header evaluation: {'needs work' if header_needs_refinement else 'looks good'}")
                                if reason:
                                    print(f"  Reason: {reason}")
                            except json.JSONDecodeError:
                                print("  Could not parse header evaluation JSON, refining by default")
                        else:
                            print("  No valid JSON found in header evaluation output, refining by default")
                    else:
                        print("  No header evaluation result, refining by default")
    
                    if header_needs_refinement:
                        print(f"\n--- HEADER REFINEMENT ---")
                        header_prompt = build_header_prompt(
                            str(orig_header["path"]), str(ss_header["path"]), str(output_dir),
                            assets_config=assets_config
                        )
                        h_success, h_duration, h_cost, _ = run_claude(
                            header_prompt, output_dir, ["Read", "Write", "Edit"],
                            timeout=300, log_name="iteration_2_header"
                        )
                        total_duration += h_duration
                        if h_cost:
                            total_cost += h_cost.get("turn_cost", 0)
                            part_costs.append({"part": "header", **h_cost})
                        if not h_success:
                            all_success = False
                    else:
                        print("  Skipping header refinement - already looks good!")
    
                    # === PHASE 2: BODY SECTIONS (below header) ===
                    body_height = normalized_height - HEADER_HEIGHT
                    num_parts = 3 if body_height > 1600 else 2 # 1600 + 200px = 1800px is the threshold for 3 parts
                    print(f"\nEvaluating {num_parts} body sections (below {HEADER_HEIGHT}px) to determine which need refinement...")
    
                    # Take fresh screenshot after header fixes
                    fresh_ss_for_eval = take_screenshot(output_dir, "iter2_after_header", dev_port)
    
                    # Extract body portions (below header) for evaluation
                    orig_body_for_eval = output_dir / "original_body_eval.png"
                    ss_body_for_eval = output_dir / "screenshot_body_eval.png"
                    with Image.open(normalized_image) as img:
                        width, height = img.size
                        img.crop((0, HEADER_HEIGHT, width, height)).save(orig_body_for_eval, quality=95)
                    with Image.open(fresh_ss_for_eval) as img:
                        width, height = img.size
                        img.crop((0, HEADER_HEIGHT, width, height)).save(ss_body_for_eval, quality=95)
                    print(f"  Extracted body portions for eval (y={HEADER_HEIGHT}+)")
    
                    # Run evaluation on body sections only
                    eval_prompt = build_section_eval_prompt(
                        str(orig_body_for_eval), str(ss_body_for_eval), num_parts, str(output_dir)
                    )
                    _, eval_duration, eval_cost, eval_result_text = run_claude(
                        eval_prompt, output_dir, ["Read"], timeout=120, log_name="iteration_2_eval"
                    )
                    total_duration += eval_duration
                    if eval_cost:
                        total_cost += eval_cost.get("turn_cost", 0)
                        part_costs.append({"part": "eval", **eval_cost})
    
                    # Parse evaluation result from Claude's actual output (not log file)
                    sections_to_refine = list(range(num_parts))  # Default: all sections
                    if eval_result_text:
                        # Look for JSON - prefer code blocks, then raw JSON
                        json_str = None
                        json_block_match = re.search(r'```json\s*(\{[^`]+\})\s*```', eval_result_text, re.DOTALL)
                        if json_block_match:
                            json_str = json_block_match.group(1).strip()
                        else:
                            # Find JSON object directly in the result
                            json_match = re.search(r'\{[^}]*"sections_to_refine"[^}]*\}', eval_result_text)
                            if json_match:
                                json_str = json_match.group()
    
                        if json_str:
                            try:
                                eval_result = json.loads(json_str)
                                sections_to_refine = [s - 1 for s in eval_result.get("sections_to_refine", [])]  # Convert to 0-indexed
                                reason = eval_result.get("reason", "")
                                print(f"  Evaluation: {len(sections_to_refine)}/{num_parts} body sections need work")
                                if reason:
                                    print(f"  Reason: {reason}")
                            except json.JSONDecodeError:
                                print("  Could not parse evaluation JSON, refining all body sections")
                        else:
                            print("  No valid JSON found in evaluation output, refining all body sections")
                    else:
                        print("  No evaluation result, refining all body sections")
    
                    if not sections_to_refine:
                        print("  All body sections look good! Skipping body refinement.")
                    else:
                        print(f"Splitting body into {num_parts} parts, refining sections: {[s+1 for s in sections_to_refine]}...")
    
                        # Split original (skipping header) and save with clear names
                        original_parts = split_image_vertically(
                            normalized_image, output_dir, num_parts, skip_header=HEADER_HEIGHT
                        )
                        for j, part in enumerate(original_parts):
                            new_path = output_dir / f"original_body_{j+1}.png"
                            shutil.move(part["path"], new_path)
                            original_parts[j]["path"] = new_path
                            print(f"  Saved: {new_path.name}")
    
                        for part_idx in sections_to_refine:  # Only process sections that need work
                            # Find original part by part_num (not list index, in case parts were skipped)
                            orig_part = None
                            for p in original_parts:
                                if p["part_num"] == part_idx + 1:  # part_num is 1-indexed
                                    orig_part = p
                                    break
    
                            if not orig_part:
                                print(f"\n--- Body Section {part_idx + 1}/{num_parts} --- SKIPPED (out of bounds)")
                                continue
    
                            print(f"\n--- Body Section {part_idx + 1}/{num_parts} ---")
    
                            # Take fresh screenshot and split using ORIGINAL's positions
                            fresh_ss = take_screenshot(output_dir, f"iter2_before_body{part_idx + 1}", dev_port)
                            fresh_parts = split_image_vertically(
                                fresh_ss, output_dir, num_parts, use_positions=original_parts
                            )
    
                            # Rename screenshot parts for comparison
                            for j, part in enumerate(fresh_parts):
                                new_path = output_dir / f"screenshot_body_{part['part_num']}_before_edit{part_idx + 1}.png"
                                shutil.move(part["path"], new_path)
                                fresh_parts[j]["path"] = new_path
    
                            # Find screenshot part by part_num
                            ss_part = None
                            for p in fresh_parts:
                                if p["part_num"] == part_idx + 1:
                                    ss_part = p
                                    break
    
                            if not ss_part:
                                print(f"  SKIPPED: Screenshot part {part_idx + 1} not available (image too short)")
                                continue
    
                            print(f"  Comparing: {orig_part['path'].name} vs {ss_part['path'].name}")
    
                            prompt = build_split_section_prompt(
                                str(orig_part["path"]), str(ss_part["path"]), str(output_dir),
                                orig_part["part_num"], num_parts, orig_part["start_y"], orig_part["end_y"]
                            )
    
                            s, d, c, _ = run_claude(
                                prompt, output_dir, ["Read", "Write", "Edit"],
                                timeout=600, log_name=f"iteration_2_body{part_idx + 1}"
                            )
                            total_duration += d
                            if c:
                                total_cost += c.get("turn_cost", 0)
                                part_costs.append({"part": f"body_{part_idx + 1}", **c})
                            if not s:
                                all_success = False
    
                    # Aggregate cost info for split iteration
                    split_cost_info = {
                        "turn_cost": round(total_cost, 4),
                        "parts": part_costs
                    }
                    reproduction_log.log_iteration(i, all_success, total_duration, split_cost_info)
    
            finally:
                kill_process_tree(server_proc)
                time.sleep(0.5)
    
        # FINAL CHECK
        print(f"\n{'='*60}\nFinal Screenshot\n{'='*60}\n")
        server_proc = start_dev_server(output_dir, dev_port)
        try:
            final_ss = take_screenshot(output_dir, "final", dev_port)
            reproduction_log.log_screenshot(final_ss, "final")
        finally:
            kill_process_tree(server_proc)
            time.sleep(0.5)
    
        # Generate requires.json to document which data fields are used
        print(f"\n{'='*60}\nGenerating requires.json\n{'='*60}")
        generate_requires_json(output_dir)
    
        reproduction_log.write_log()

        # Mark as completed
        completed_successfully = True

    except KeyboardInterrupt:
        print("\n\nInterrupted by user!")
        if output_dir.exists():
            print(f"Cleaning up incomplete reproduction: {output_dir}")
            # shutil.rmtree(output_dir, ignore_errors=True)
        raise

    except Exception as e:
        print(f"\n\nError during reproduction: {e}")
        if not completed_successfully and output_dir.exists():
            print(f"Cleaning up incomplete reproduction: {output_dir}")
            # shutil.rmtree(output_dir, ignore_errors=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reproduce UI from screenshot using a local LLM coding CLI")
    parser.add_argument("image_path", help="Path to the input screenshot")
    parser.add_argument("--iterations", type=int, default=2, help="Number of improvement iterations")
    parser.add_argument("--model", type=str, default=None,
                        help="Model to use. Claude: sonnet, haiku, opus (default: opus). OpenCode: gpt-4o, o3, gpt-5.2, etc (default: openai/gpt-5.2)")
    parser.add_argument("--backend", type=str, default="claude", choices=["claude", "opencode"],
                        help="CLI backend to use (claude or opencode)")
    parser.add_argument("--no-splits", action="store_true",
                        help="Skip split-section refinement (reduces API calls)")
    parser.add_argument("--data-dir", type=str, default="data",
                        help="Data directory (default: data)")
    parser.add_argument("--port", type=int, default=None,
                        help="Fixed port for dev server (default: auto-assign)")
    args = parser.parse_args()

    # Update data directory paths based on argument
    # (ASSETS_DIR and PRODUCTS_DIR are module-level, so we can reassign directly)
    data_dir = SCRIPT_DIR.parent / args.data_dir
    ASSETS_DIR = data_dir / "assets_lite"
    PRODUCTS_DIR = data_dir / "assets" / "products"

    # Set backend via environment variable
    os.environ["LLM_BACKEND"] = args.backend
    print(f"Using backend: {args.backend}")

    # Set model via environment variable for run_llm to pick up
    # Default: opus for claude, gpt-5.2 for opencode
    if args.model:
        model = args.model
    else:
        model = "opus" if args.backend == "claude" else "openai/gpt-5.2"
    os.environ["LLM_MODEL"] = model
    print(f"Using model: {model}")

    # Set no-splits flag
    if args.no_splits:
        os.environ["REPRODUCE_NO_SPLITS"] = "1"
        print("Split-section refinement disabled")

    reproduce_ui(args.image_path, args.iterations, port=args.port)
