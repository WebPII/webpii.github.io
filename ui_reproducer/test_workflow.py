#!/usr/bin/env python3
"""
Test Workflow - End-to-end test of UI reproduction pipeline.

Picks random desktop screenshots from data/ui_images/, reproduces them,
takes annotated screenshots with bounding boxes, and tracks what's been processed.

Usage:
    python test_workflow.py                          # Run with random desktop image
    python test_workflow.py --allow-repeats          # Allow re-running same images
    python test_workflow.py --image path/to/img.png  # Run specific image
    python test_workflow.py --skip-data-gen          # Use existing data variants
    python test_workflow.py --skip-reproduce         # Skip UI reproduction step
    python test_workflow.py --skip-screenshot        # Skip taking annotated screenshot
    python test_workflow.py --skip-state-update      # Skip updating processed state
    python test_workflow.py --skip-print-results     # Skip printing output file paths
    python test_workflow.py --iterations 3           # More reproduction iterations
"""

import argparse
import json
import random
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from screenshot_pages import take_screenshot_with_annotations, inject_data_json
from reproduce_ui import normalize_company_name


def find_free_port() -> int:
    """Find a free port by letting the OS assign one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port

SCRIPT_DIR = Path(__file__).parent
BASE_DIR = SCRIPT_DIR.parent  # pii/
OUTPUT_DIR = SCRIPT_DIR / "output"
STATE_FILE = SCRIPT_DIR / ".test_workflow_state.json"
DATA_VARIANTS_FILE = SCRIPT_DIR / "data_variants.ndjson"
SCREENSHOTS_DIR = SCRIPT_DIR / "screenshots"

# Default data directory (can be overridden via --data-dir argument)
DEFAULT_DATA_DIR = BASE_DIR / "data"


def load_state() -> dict:
    """Load state tracking which images have been processed."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"processed": [], "runs": [], "last_run": None}


def save_state(state: dict):
    """Save state to disk."""
    state["last_run"] = datetime.now().isoformat()
    # Ensure runs list exists for backwards compatibility
    if "runs" not in state:
        state["runs"] = []
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def record_run(state: dict, image_path: Path, output_dir: Path):
    """Record a run with timestamp and output directory."""
    if "runs" not in state:
        state["runs"] = []

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_record = {
        "timestamp": timestamp,
        "image": str(image_path.relative_to(BASE_DIR)),
        "output_dir": str(output_dir.relative_to(BASE_DIR)) if output_dir else None
    }
    state["runs"].append(run_record)


def find_desktop_images(data_dir: Path) -> list[Path]:
    """Find all desktop images in data_dir/ui_images/."""
    images_dir = data_dir / "ui_images"
    if not images_dir.exists():
        print(f"ERROR: Data images directory not found: {images_dir}")
        return []

    desktop_images = []
    for page_type_dir in images_dir.iterdir():
        if not page_type_dir.is_dir():
            continue

        for img_file in page_type_dir.glob("*-desktop.png"):
            desktop_images.append(img_file)

    return sorted(desktop_images)


def pick_random_image(data_dir: Path, allow_repeats: bool = False) -> Path | None:
    """Pick a random desktop image that hasn't been processed yet."""
    all_images = find_desktop_images(data_dir)
    if not all_images:
        print("ERROR: No desktop images found")
        return None

    state = load_state()
    processed = set(state.get("processed", []))

    if allow_repeats:
        available = all_images
    else:
        available = [img for img in all_images if str(img.relative_to(BASE_DIR)) not in processed]

    if not available:
        print("All desktop images have been processed!")
        print(f"Run with --allow-repeats to re-run, or clear state: rm {STATE_FILE}")
        return None

    chosen = random.choice(available)
    print(f"Selected: {chosen.relative_to(BASE_DIR)}")
    print(f"  ({len(available)} unprocessed / {len(all_images)} total desktop images)")
    return chosen


def get_source_image_from_screenshot(index: int) -> Path | None:
    """Look up the source image path from a screenshot index."""
    screenshot_json = SCREENSHOTS_DIR / f"{index:04d}.json"
    if not screenshot_json.exists():
        print(f"ERROR: Screenshot annotation not found: {screenshot_json}")
        return None

    try:
        with open(screenshot_json) as f:
            annotation = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to read {screenshot_json}: {e}")
        return None

    source_image = annotation.get("source_image")
    if not source_image:
        # Fall back to reconstructing from company/page_type
        company = annotation.get("company")
        page_type = annotation.get("page_type")
        if company and page_type:
            print(f"WARNING: No source_image in annotation, have company={company}, page_type={page_type}")
        print(f"ERROR: No source_image found in {screenshot_json}")
        return None

    return BASE_DIR / source_image


def run_command(cmd: list[str], description: str, cwd: Path = SCRIPT_DIR) -> bool:
    """Run a command and return success status."""
    print(f"\n{'='*60}")
    print(f"{description}")
    print(f"{'='*60}")
    print(f"Command: {' '.join(cmd)}")
    print()

    try:
        subprocess.run(
            cmd,
            cwd=str(cwd),
            check=True,
            text=True,
            capture_output=False  # Show output in real-time
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Command failed with exit code {e.returncode}")
        return False
    except Exception as e:
        print(f"ERROR: {e}")
        return False


def find_latest_output(image_path: Path) -> dict | None:
    """Find the latest output directory for a reproduced image."""
    # Parse image metadata
    filename = image_path.stem  # e.g., "4022-amazon-desktop"
    page_type = image_path.parent.name  # e.g., "account-dashboard"

    parts = filename.split("-")
    image_id = parts[0]  # First part is the image ID
    device = "desktop"
    company_parts = parts[1:-1]  # Between ID and device
    company = "-".join(company_parts) if company_parts else "unknown"
    company = normalize_company_name(company)

    # Find latest timestamp directory (new structure includes image_id)
    # Structure: output/{device}/{company}/{page_type}/{image_id}/{timestamp}/
    search_dir = OUTPUT_DIR / device / company / page_type / image_id
    if not search_dir.exists():
        return None

    timestamp_dirs = sorted(
        [d for d in search_dir.iterdir() if d.is_dir() and d.name[0].isdigit()],
        key=lambda d: d.name,
        reverse=True
    )

    if not timestamp_dirs:
        return None

    latest = timestamp_dirs[0]
    return {
        "output_dir": latest,
        "final_screenshot": latest / "final.png",
        "original_image": latest / "original.png",
        "app_jsx": latest / "src" / "App.jsx",
        "data_json": latest / "src" / "data.json",
        "reproduction_log": latest / "reproduction.log",
        "requires_json": latest / "requires.json",
        "iteration_logs": list(latest.glob("iteration_*.log"))
    }


def take_annotated_screenshot(image_path: Path, screenshot_index: int) -> dict | None:
    """Take screenshots with bounding boxes for detected PII/product elements.

    Generates both:
    - annotated.png: Full fill (all fields populated)
    - annotated_partial.png: Partial fill (simulates user mid-typing)
    """
    output_info = find_latest_output(image_path)
    if not output_info:
        print("\nWARNING: Could not find output directory for screenshot")
        return None

    # Load data and required fields
    try:
        with open(output_info['data_json']) as f:
            data = json.load(f)

        required_fields = []
        if output_info['requires_json'].exists():
            with open(output_info['requires_json']) as f:
                requires_data = json.load(f)
                required_fields = requires_data.get('required_fields', [])

        # Parse metadata from image path
        filename = image_path.stem
        page_type = image_path.parent.name
        parts = filename.split("-")
        image_id = parts[0]
        company_parts = parts[1:-1]
        company = "-".join(company_parts) if company_parts else "unknown"

        # Create page_info
        page_info = {
            "path": output_info['output_dir'],
            "company": company,
            "page_type": page_type,
            "device": "desktop",
            "required_fields": required_fields,
            "source_image": str(image_path),
            "output_dir": str(output_info['output_dir'].relative_to(SCRIPT_DIR.parent))
        }

        print(f"\n{'='*60}")
        print("TAKING ANNOTATED SCREENSHOTS")
        print(f"{'='*60}")
        print(f"Output dir: {output_info['output_dir'].relative_to(BASE_DIR)}")
        print(f"Required fields: {len(required_fields)}")
        print()

        # === FULL SCREENSHOT ===
        port = find_free_port()
        annotation = take_screenshot_with_annotations(
            page_info=page_info,
            data=data,
            scroll_y=0,
            output_dir=output_info['output_dir'],
            index=screenshot_index,
            port=port,
            full_page=True,
            partial_fill=False
        )

        # Rename to annotated.png/json
        screenshot_id = f"{screenshot_index:04d}"
        old_png = output_info['output_dir'] / f"{screenshot_id}.png"
        old_json = output_info['output_dir'] / f"{screenshot_id}.json"
        new_png = output_info['output_dir'] / "annotated.png"
        new_json = output_info['output_dir'] / "annotated.json"
        if old_png.exists():
            old_png.rename(new_png)
        if old_json.exists():
            old_json.rename(new_json)

        print(f"✓ Full screenshot: {output_info['output_dir'].relative_to(BASE_DIR)}/annotated.png")

        # === PARTIAL SCREENSHOT ===
        port = find_free_port()
        partial_annotation = take_screenshot_with_annotations(
            page_info=page_info,
            data=data,
            scroll_y=0,
            output_dir=output_info['output_dir'],
            index=screenshot_index + 1,  # Use next index to avoid overwriting
            port=port,
            full_page=True,
            partial_fill=True
        )

        # Rename to annotated_partial.png/json
        partial_id = f"{screenshot_index + 1:04d}"
        old_partial_png = output_info['output_dir'] / f"{partial_id}.png"
        old_partial_json = output_info['output_dir'] / f"{partial_id}.json"
        new_partial_png = output_info['output_dir'] / "annotated_partial.png"
        new_partial_json = output_info['output_dir'] / "annotated_partial.json"
        if old_partial_png.exists():
            old_partial_png.rename(new_partial_png)
        if old_partial_json.exists():
            old_partial_json.rename(new_partial_json)

        print(f"✓ Partial screenshot: {output_info['output_dir'].relative_to(BASE_DIR)}/annotated_partial.png")
        print()

        # Compute stats from full screenshot
        pii_elements = annotation.get("pii_elements", [])
        product_elements = annotation.get("product_elements", [])
        pii_containers = annotation.get("pii_containers", [])

        pii_visible = [e for e in pii_elements if e.get("visible")]
        products_visible = len([e for e in product_elements if e.get("visible")])
        containers_found = len(pii_containers)

        # Count fillable (input) vs non-fillable (text) PII elements
        fillable = [e for e in pii_visible if e.get("element_type") == "input"]
        non_fillable = [e for e in pii_visible if e.get("element_type") != "input"]

        # Get partial fill config from disk (inject_data_json writes it there)
        partial_config = {}
        try:
            with open(output_info['data_json']) as f:
                written_data = json.load(f)
                partial_config = written_data.get("PARTIAL_FILL_CONFIG", {})
        except Exception:
            pass

        print("Detection results (full):")
        print(f"  PII elements: {len(pii_elements)} found, {len(pii_visible)} visible")
        print(f"    - Fillable inputs: {len(fillable)} (purple)")
        print(f"    - Text/image (not fillable): {len(non_fillable)} (red)")
        print(f"  Product elements: {len(product_elements)} found, {products_visible} visible")
        print(f"  Containers: {containers_found} found")
        print()

        if partial_config.get("enabled"):
            print("Partial fill config:")
            print(f"  Partial field: {partial_config.get('partialField')} (stops at char {partial_config.get('stopCharCount')})")
            print(f"  Empty fields: {partial_config.get('emptyFields', [])}")

        # Restore full data for future use
        inject_data_json(output_info['output_dir'], data, partial_fill=False)

        return annotation

    except Exception as e:
        print(f"\nERROR taking annotated screenshot: {e}")
        import traceback
        traceback.print_exc()
        return None


def extract_cost_from_log(log_path: Path) -> dict | None:
    """Extract cost information from reproduction.log JSON."""
    if not log_path.exists():
        return None
    try:
        with open(log_path) as f:
            log_data = json.load(f)
        cost = log_data.get("cost", {})
        return {
            "total_cost": cost.get("total_cost", 0.0),
            "input_tokens": cost.get("total_input_tokens", 0),
            "output_tokens": cost.get("total_output_tokens", 0),
            "cache_read_tokens": cost.get("total_cache_read_tokens", 0),
        }
    except Exception:
        return None


def print_results(image_path: Path, screenshot_index: int = None):
    """Print paths to all generated files."""
    output_info = find_latest_output(image_path)
    if not output_info:
        print("\nWARNING: Could not find output directory")
        return

    output_rel = output_info['output_dir'].relative_to(BASE_DIR)

    # Extract cost from reproduction.log
    cost_info = extract_cost_from_log(output_info['reproduction_log'])

    # Extract fields from requires.json
    fields_data = None
    if output_info['requires_json'].exists():
        try:
            with open(output_info['requires_json']) as f:
                requires = json.load(f)
                fields_data = {
                    "summary": requires.get("summary", {}),
                    "required_fields": requires.get("required_fields", {})
                }
        except Exception:
            pass

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    # Iterations first
    print("Iterations:")
    print(f"  Log: {output_rel}/reproduction.log")
    for log in sorted(output_info['iteration_logs'], key=lambda x: x.name):
        print(f"  {log.name}")
    print()

    # Fields annotated with actual field names
    if fields_data:
        summary = fields_data["summary"]
        required = fields_data["required_fields"]
        pii_count = summary.get("pii_fields", 0)
        product_count = summary.get("product_fields", 0)
        order_count = summary.get("order_fields", 0)
        total = summary.get("total_references", 0)

        print(f"Fields annotated: {total} total")

        pii_fields = required.get("pii", [])
        if pii_fields:
            print(f"  PII ({pii_count}): {', '.join(pii_fields)}")

        product_fields = required.get("products", [])
        if product_fields:
            products_used = summary.get("products_used", [])
            print(f"  Products ({product_count}): products {products_used}")

        order_fields = required.get("order", [])
        if order_fields:
            print(f"  Order ({order_count}): {', '.join(order_fields)}")
    print()

    # Config files (compact, just data.json and requires.json)
    print(f"Data: {output_rel}/src/data.json")
    print(f"Requires: {output_rel}/requires.json")

    # Token usage (auxiliary info, compact)
    if cost_info:
        print(f"Tokens: {cost_info['input_tokens']:,} in | {cost_info['output_tokens']:,} out | {cost_info['cache_read_tokens']:,} cache")

    # Images
    print()
    print("=" * 60)
    print("IMAGES:")
    print("=" * 60)
    print(f"  Original:  {output_rel}/original.png")
    print(f"  Final:     {output_rel}/final.png")
    if screenshot_index is not None:
        print(f"  Annotated: {output_rel}/annotated.png")
        print(f"  Partial:   {output_rel}/annotated_partial.png")

    # App.jsx near bottom
    print()
    print(f"App.jsx: {output_rel}/src/App.jsx")

    # Cost (just total, compact)
    print()
    if cost_info:
        print(f"Cost: ${cost_info['total_cost']:.4f}")
    else:
        print("Cost: (not available)")

    # Source image at the absolute bottom
    print()
    print(f"SOURCE: {image_path.relative_to(BASE_DIR)}")


def get_next_screenshot_index() -> int:
    """Get the next available screenshot index."""
    if not SCREENSHOTS_DIR.exists():
        return 0

    existing = list(SCREENSHOTS_DIR.glob("[0-9][0-9][0-9][0-9].json"))
    if not existing:
        return 0

    indices = [int(f.stem) for f in existing]
    return max(indices) + 1


def main():
    parser = argparse.ArgumentParser(description="Test UI reproduction workflow")
    parser.add_argument("--image", type=str, help="Specific image to reproduce (relative to pii/)")
    parser.add_argument("--by-screenshot", type=int, metavar="INDEX", help="Re-run by screenshot index (e.g., --by-screenshot 13)")
    parser.add_argument("--allow-repeats", action="store_true", help="Allow random selection to pick already-processed images (ignored when using --image)")
    parser.add_argument("--skip-data-gen", action="store_true", help="Skip data variant generation")
    parser.add_argument("--skip-reproduce", action="store_true", help="Skip UI reproduction step")
    parser.add_argument("--skip-screenshot", action="store_true", help="Skip taking annotated screenshot")
    parser.add_argument("--skip-state-update", action="store_true", help="Skip updating processed state")
    parser.add_argument("--skip-print-results", action="store_true", help="Skip printing output file paths")
    parser.add_argument("--iterations", type=int, default=2, help="Reproduction iterations (default=2)")
    parser.add_argument("--model", type=str, default=None, help="Model (default: opus for claude, gpt-5.2 for opencode)")
    parser.add_argument("--backend", type=str, default="claude", choices=["claude", "opencode"],
                        help="CLI backend: claude or opencode (default: claude)")
    parser.add_argument("--no-splits", action="store_true", help="Disable split-section refinement")
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    parser.add_argument("--data-dir", type=str, default=None, help="Data directory (default: data/ or example_data/)")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        print(f"Random seed: {args.seed}")

    # Determine data directory
    if args.data_dir:
        data_dir = BASE_DIR / args.data_dir
    else:
        data_dir = BASE_DIR / "data"

    print(f"\n{'='*60}")
    print("UI REPRODUCTION TEST WORKFLOW")
    print(f"{'='*60}")
    print(f"Script dir: {SCRIPT_DIR.relative_to(BASE_DIR)}")
    print(f"Data dir: {data_dir.relative_to(BASE_DIR)}")
    print()

    # Step 1: Pick image
    if args.by_screenshot is not None:
        image_path = get_source_image_from_screenshot(args.by_screenshot)
        if not image_path:
            sys.exit(1)
        if not image_path.exists():
            print(f"ERROR: Source image not found: {image_path}")
            sys.exit(1)
        print(f"Using image from screenshot #{args.by_screenshot:04d}: {image_path.relative_to(BASE_DIR)}")
    elif args.image:
        image_path = BASE_DIR / args.image
        if not image_path.exists():
            print(f"ERROR: Image not found: {image_path}")
            sys.exit(1)
        if not image_path.name.endswith("-desktop.png"):
            print("WARNING: Image is not a desktop screenshot")
        print(f"Using specified image: {image_path.relative_to(BASE_DIR)}")
    else:
        image_path = pick_random_image(data_dir, args.allow_repeats)
        if not image_path:
            sys.exit(1)

    # Step 2: Generate data variants
    if not args.skip_data_gen:
        if not DATA_VARIANTS_FILE.exists():
            print(f"\nGenerating data variants (first run)...")
            cmd = [
                "python", "generate_data_variants.py",
                "--num-variants", "100",
                "--products-per-variant", "10",
                "--output", "data_variants.ndjson",
                "--seed", str(args.seed if args.seed else 42),
                "--data-dir", str(data_dir.relative_to(BASE_DIR))
            ]
            if not run_command(cmd, "STEP 1: Generate Data Variants"):
                print("ERROR: Data generation failed")
                sys.exit(1)
        else:
            print(f"\nUsing existing data variants: {DATA_VARIANTS_FILE.relative_to(SCRIPT_DIR)}")
            print("  (Use --skip-data-gen to use without checking, or delete to regenerate)")
    else:
        print("\nSkipping data variant generation")

    # Step 3: Reproduce UI
    if args.skip_reproduce:
        print("\nSkipping UI reproduction (--skip-reproduce)")
    else:
        cmd = [
            "python", "reproduce_ui.py",
            str(image_path),
            "--iterations", str(args.iterations),
            "--backend", args.backend,
            "--data-dir", str(data_dir.relative_to(BASE_DIR))
        ]
        if args.model:
            cmd.extend(["--model", args.model])
        if args.no_splits:
            cmd.append("--no-splits")

        if not run_command(cmd, "STEP 2: Reproduce UI"):
            print("ERROR: UI reproduction failed")
            sys.exit(1)

    # Step 3.5: Take annotated screenshot with bounding boxes
    screenshot_index = None
    if args.skip_screenshot:
        print("\nSkipping annotated screenshot (--skip-screenshot)")
    else:
        screenshot_index = get_next_screenshot_index()
        take_annotated_screenshot(image_path, screenshot_index)

    # Step 4: Update state
    if args.skip_state_update:
        print("\nSkipping state update (--skip-state-update)")
    else:
        state = load_state()
        image_rel = str(image_path.relative_to(BASE_DIR))

        # Get output directory for this run
        output_info = find_latest_output(image_path)
        output_dir = output_info["output_dir"] if output_info else None

        # Record this run with timestamp and output dir
        record_run(state, image_path, output_dir)

        if image_rel not in state["processed"]:
            state["processed"].append(image_rel)
            print(f"\nMarked as processed: {image_rel}")
        else:
            print(f"\nAlready marked as processed: {image_rel}")

        save_state(state)
        if output_dir:
            print(f"Run recorded: {output_dir.relative_to(BASE_DIR)}")

    # Step 5: Print results
    if args.skip_print_results:
        print("\nSkipping results printing (--skip-print-results)")
    else:
        print_results(image_path, screenshot_index)


if __name__ == "__main__":
    main()
