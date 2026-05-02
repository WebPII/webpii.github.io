#!/usr/bin/env python3
"""
Mass UI Reproducer - Parallel reproduction of all desktop UI screenshots.

Processes all desktop images in data/ui_images/ using parallel workers.
Only keeps essential files (App.jsx, final.png, annotated.png/json) to save disk space.

Cost Tracking:
- Each output directory gets a cost.json with that reproduction's cost + timestamp
- State file tracks cumulative "total ever spent" (never resets)
- --show-costs shows both total spent AND cost of current (latest) reproductions

Usage:
    python mass_reproduce.py                      # Process all unprocessed images
    python mass_reproduce.py --workers 4          # Use 4 parallel workers
    python mass_reproduce.py --stagger 5          # 5 seconds between worker starts
    python mass_reproduce.py --allow-repeats      # Re-process already done images
    python mass_reproduce.py --keep-all           # Keep all intermediate files
    python mass_reproduce.py --dry-run            # Show what would be processed
    python mass_reproduce.py --page-filter amazon # Only process amazon images
    python mass_reproduce.py --show-costs         # Show cost breakdown
    python mass_reproduce.py --limit 10           # Only process first 10 images

Parallelism:
    - Workers are staggered on startup (default 3s) to avoid thundering herd
    - Each worker gets its own dynamically allocated port for Vite
    - Transient failures (timeout, server issues) trigger automatic retry
    - HTTP content verification ensures Vite is fully ready before page load
"""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
BASE_DIR = SCRIPT_DIR.parent  # pii/
OUTPUT_DIR = SCRIPT_DIR / "output"
STATE_FILE = SCRIPT_DIR / ".mass_reproduce_state.json"
DATA_VARIANTS_FILE = SCRIPT_DIR / "data_variants.ndjson"

# Files to keep after reproduction (relative to output dir)
ESSENTIAL_FILES = [
    "src/App.jsx",
    "src/data.json",
    "final.png",
    "annotated.png",
    "annotated.json",
    "original.png",
    "requires.json",
    "reproduction.log",
    "cost.json",
]

# Directories/patterns to always remove
CLEANUP_PATTERNS = [
    "node_modules",  # symlink but still remove
    ".vite",
    "dist",
    "*.log",  # iteration logs (reproduction.log is in ESSENTIAL)
    "split_*.png",
    "original_*.png",
    "screenshot_*.png",
    "iter*.png",
]


def load_state() -> dict:
    """Load state tracking which images have been processed."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "processed": [],
        "failed": [],
        "last_run": None,
        "total_cost": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
    }


def save_state(state: dict):
    """Save state to disk."""
    state["last_run"] = datetime.now().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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


def find_free_port() -> int:
    """Find a free port by letting the OS assign one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


def cleanup_output_dir(output_dir: Path, keep_all: bool = False):
    """Remove non-essential files from output directory."""
    if keep_all or not output_dir.exists():
        return

    # Build set of essential file paths
    essential_paths = set()
    for pattern in ESSENTIAL_FILES:
        if "*" in pattern:
            essential_paths.update(output_dir.glob(pattern))
        else:
            essential_paths.add(output_dir / pattern)

    # Remove non-essential files
    for item in output_dir.rglob("*"):
        if item.is_file():
            # Check if this file should be kept
            keep = False
            for essential in ESSENTIAL_FILES:
                if "*" not in essential:
                    if item == output_dir / essential:
                        keep = True
                        break
                else:
                    # Pattern matching (e.g., *.log means keep reproduction.log but not iteration_*.log)
                    pass

            # Check against essential paths
            rel_path = str(item.relative_to(output_dir))
            if rel_path in [e.replace("/", os.sep) for e in ESSENTIAL_FILES]:
                keep = True

            # Also keep parent directories of essential files
            for essential in ESSENTIAL_FILES:
                if rel_path == essential or rel_path.replace(os.sep, "/") == essential:
                    keep = True
                    break

            if not keep:
                # Check if it matches cleanup patterns
                name = item.name
                for pattern in CLEANUP_PATTERNS:
                    if pattern.startswith("*"):
                        if name.endswith(pattern[1:]):
                            try:
                                item.unlink()
                            except Exception:
                                pass
                            break
                    elif pattern in name or name == pattern:
                        try:
                            item.unlink()
                        except Exception:
                            pass
                        break

    # Remove node_modules symlink
    node_modules = output_dir / "node_modules"
    if node_modules.is_symlink():
        node_modules.unlink()
    elif node_modules.is_dir():
        shutil.rmtree(node_modules, ignore_errors=True)

    # Remove empty directories
    for dirpath in sorted(output_dir.rglob("*"), key=lambda x: len(str(x)), reverse=True):
        if dirpath.is_dir():
            try:
                dirpath.rmdir()  # Only removes if empty
            except OSError:
                pass


def extract_cost_from_log(output_dir: Path, save_cost_file: bool = True) -> dict | None:
    """Extract cost information from reproduction.log and optionally save to cost.json."""
    log_path = output_dir / "reproduction.log"
    if not log_path.exists():
        return None

    try:
        with open(log_path) as f:
            log_data = json.load(f)

        cost = log_data.get("cost", {})
        cost_data = {
            "total_cost": cost.get("total_cost", 0.0),
            "input_tokens": cost.get("total_input_tokens", 0),
            "output_tokens": cost.get("total_output_tokens", 0),
            "cache_read_tokens": cost.get("total_cache_read_tokens", 0),
            "timestamp": datetime.now().isoformat(),
            "source_image": log_data.get("paths", {}).get("source_image"),
        }

        # Save cost.json to output directory for per-reproduction tracking
        if save_cost_file:
            cost_file = output_dir / "cost.json"
            with open(cost_file, "w") as f:
                json.dump(cost_data, f, indent=2)

        return cost_data
    except Exception:
        return None


def scan_all_output_costs() -> dict:
    """Scan all output directories to calculate cost of current reproductions.

    Returns dict with:
    - current_cost: sum of cost.json from latest timestamp dirs only
    - current_count: number of reproductions with cost data
    - by_company: breakdown by company
    """
    result = {
        "current_cost": 0.0,
        "current_input_tokens": 0,
        "current_output_tokens": 0,
        "current_count": 0,
        "by_company": {},
    }

    if not OUTPUT_DIR.exists():
        return result

    # Walk output/{device}/{company}/{page_type}/{image_id}/{timestamp}/
    for device_dir in OUTPUT_DIR.iterdir():
        if not device_dir.is_dir():
            continue
        for company_dir in device_dir.iterdir():
            if not company_dir.is_dir():
                continue
            company = company_dir.name
            if company not in result["by_company"]:
                result["by_company"][company] = {"cost": 0.0, "count": 0}

            for page_type_dir in company_dir.iterdir():
                if not page_type_dir.is_dir():
                    continue
                for image_id_dir in page_type_dir.iterdir():
                    if not image_id_dir.is_dir():
                        continue

                    # Find latest timestamp directory
                    timestamp_dirs = sorted(
                        [d for d in image_id_dir.iterdir() if d.is_dir() and d.name[0].isdigit()],
                        key=lambda d: d.name,
                        reverse=True
                    )

                    if timestamp_dirs:
                        latest = timestamp_dirs[0]
                        cost_file = latest / "cost.json"
                        if cost_file.exists():
                            try:
                                with open(cost_file) as f:
                                    cost_data = json.load(f)
                                cost_val = cost_data.get("total_cost", 0.0)
                                result["current_cost"] += cost_val
                                result["current_input_tokens"] += cost_data.get("input_tokens", 0)
                                result["current_output_tokens"] += cost_data.get("output_tokens", 0)
                                result["current_count"] += 1
                                result["by_company"][company]["cost"] += cost_val
                                result["by_company"][company]["count"] += 1
                            except Exception:
                                pass

    return result


def get_output_dir_for_image(image_path: Path) -> Path | None:
    """Get the expected output directory for an image."""
    from reproduce_ui import parse_image_path, normalize_company_name

    filename = image_path.stem
    page_type = image_path.parent.name

    parts = filename.split("-")
    image_id = parts[0]
    company_parts = parts[1:-1]
    company = "-".join(company_parts) if company_parts else "unknown"
    company = normalize_company_name(company)

    # Find latest timestamp directory
    search_dir = OUTPUT_DIR / "desktop" / company / page_type / image_id
    if not search_dir.exists():
        return None

    timestamp_dirs = sorted(
        [d for d in search_dir.iterdir() if d.is_dir() and d.name[0].isdigit()],
        key=lambda d: d.name,
        reverse=True
    )

    return timestamp_dirs[0] if timestamp_dirs else None


def reproduce_single_image(task: dict, max_retries: int = 2) -> dict:
    """Worker function to reproduce a single image.

    Returns dict with success status and metadata.
    Retries on transient failures (timeout, server startup issues).
    """
    image_path = Path(task["image_path"])
    data_dir = Path(task["data_dir"])
    iterations = task["iterations"]
    model = task.get("model")
    backend = task.get("backend", "claude")
    keep_all = task["keep_all"]
    worker_id = task["worker_id"]

    result = {
        "image_path": str(image_path),
        "success": False,
        "error": None,
        "output_dir": None,
        "duration": 0,
        "cost": None,
        "attempts": 0,
    }

    start_time = time.time()

    for attempt in range(max_retries):
        result["attempts"] = attempt + 1

        try:
            # Run reproduce_ui.py (uses find_free_port() internally)
            cmd = [
                sys.executable, str(SCRIPT_DIR / "reproduce_ui.py"),
                str(image_path),
                "--iterations", str(iterations),
                "--backend", backend,
                "--data-dir", str(data_dir.relative_to(BASE_DIR)),
            ]
            if model:
                cmd.extend(["--model", model])

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,  # 15 minute timeout
                cwd=str(SCRIPT_DIR)
            )

            if proc.returncode != 0:
                error_msg = proc.stderr[-500:] if proc.stderr else "Unknown error"
                # Retry on transient errors (server startup, port issues)
                if attempt < max_retries - 1 and any(x in error_msg.lower() for x in ["port", "timeout", "server", "vite"]):
                    time.sleep(5)  # Wait before retry
                    continue
                result["error"] = f"reproduce_ui failed: {error_msg}"
                result["duration"] = time.time() - start_time
                return result

            # Success - break out of retry loop
            break

        except subprocess.TimeoutExpired:
            if attempt < max_retries - 1:
                time.sleep(5)
                continue
            result["error"] = "Timeout after 15 minutes"
            result["duration"] = time.time() - start_time
            return result

    # If we get here, reproduce_ui succeeded - continue with screenshot
    try:
        # Find output directory
        output_dir = get_output_dir_for_image(image_path)
        if not output_dir:
            result["error"] = "Could not find output directory after reproduction"
            result["duration"] = time.time() - start_time
            return result

        result["output_dir"] = str(output_dir)

        # Take annotated screenshot
        try:
            from screenshot_pages import take_screenshot_with_annotations

            # Load data and required fields
            data_json_path = output_dir / "src" / "data.json"
            requires_json_path = output_dir / "requires.json"

            with open(data_json_path) as f:
                data = json.load(f)

            required_fields = []
            if requires_json_path.exists():
                with open(requires_json_path) as f:
                    requires_data = json.load(f)
                    required_fields = requires_data.get("all_fields", [])

            # Parse metadata
            filename = image_path.stem
            page_type = image_path.parent.name
            parts = filename.split("-")
            company_parts = parts[1:-1]
            company = "-".join(company_parts) if company_parts else "unknown"

            page_info = {
                "path": output_dir,
                "company": company,
                "page_type": page_type,
                "device": "desktop",
                "required_fields": required_fields,
                "source_image": str(image_path.relative_to(BASE_DIR)),
                "output_dir": str(output_dir.relative_to(BASE_DIR)),
            }

            port = find_free_port()
            annotation = take_screenshot_with_annotations(
                page_info=page_info,
                data=data,
                scroll_y=0,
                output_dir=output_dir,
                index=0,
                port=port,
                full_page=True
            )

            # Rename to annotated.png/json
            old_png = output_dir / "0000.png"
            old_json = output_dir / "0000.json"
            new_png = output_dir / "annotated.png"
            new_json = output_dir / "annotated.json"
            if old_png.exists():
                old_png.rename(new_png)
            if old_json.exists():
                old_json.rename(new_json)

        except Exception as e:
            # Screenshot failed but reproduction succeeded - partial success
            result["error"] = f"Screenshot failed: {str(e)[:200]}"

        # Extract cost before cleanup
        result["cost"] = extract_cost_from_log(output_dir)

        # Cleanup non-essential files
        cleanup_output_dir(output_dir, keep_all)

        result["success"] = True
        result["duration"] = time.time() - start_time
        return result

    except Exception as e:
        result["error"] = str(e)[:500]
        result["duration"] = time.time() - start_time
        return result


def main():
    parser = argparse.ArgumentParser(description="Mass parallel UI reproduction")
    parser.add_argument("--workers", type=int, default=2, help="Number of parallel workers (default: 2)")
    parser.add_argument("--allow-repeats", action="store_true", help="Re-process already completed images")
    parser.add_argument("--keep-all", action="store_true", help="Keep all intermediate files")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed without running")
    parser.add_argument("--page-filter", type=str, help="Filter images by company name")
    parser.add_argument("--limit", type=int, help="Limit number of images to process")
    parser.add_argument("--iterations", type=int, default=2, help="Reproduction iterations (default: 2)")
    parser.add_argument("--model", type=str, default=None, help="Model (default: opus for claude, gpt-5.2 for opencode)")
    parser.add_argument("--backend", type=str, default="claude", choices=["claude", "opencode"],
                        help="CLI backend: claude or opencode (default: claude)")
    parser.add_argument("--data-dir", type=str, default="data", help="Data directory (default: data)")
    parser.add_argument("--show-costs", action="store_true", help="Show cost breakdown and exit")
    parser.add_argument("--stagger", type=float, default=5.0, help="Seconds between worker starts (default: 5.0)")
    parser.add_argument("--sequential", action="store_true", help="Process images one at a time (safest, slowest)")
    args = parser.parse_args()

    data_dir = BASE_DIR / args.data_dir

    # Handle cost-only commands first
    state = load_state()

    if args.show_costs:
        print(f"\n{'='*60}")
        print("COST BREAKDOWN")
        print(f"{'='*60}")

        # Total ever spent (from state file - never resets)
        print("\n1. TOTAL EVER SPENT (all runs, including re-runs):")
        print(f"   Cost: ${state.get('total_cost', 0):.4f}")
        print(f"   Input tokens: {state.get('total_input_tokens', 0):,}")
        print(f"   Output tokens: {state.get('total_output_tokens', 0):,}")

        # Current reproductions (scan output dirs for latest versions)
        print("\n2. CURRENT REPRODUCTIONS (latest version of each image):")
        current = scan_all_output_costs()
        print(f"   Cost: ${current['current_cost']:.4f}")
        print(f"   Input tokens: {current['current_input_tokens']:,}")
        print(f"   Output tokens: {current['current_output_tokens']:,}")
        print(f"   Count: {current['current_count']} reproductions with cost data")

        if current['current_count'] > 0:
            avg = current['current_cost'] / current['current_count']
            print(f"   Avg per image: ${avg:.4f}")

        # Breakdown by company
        if current['by_company']:
            print("\n   By company:")
            for company, data in sorted(current['by_company'].items(), key=lambda x: -x[1]['cost']):
                if data['count'] > 0:
                    print(f"      {company}: ${data['cost']:.4f} ({data['count']} images)")

        # Waste calculation
        waste = state.get('total_cost', 0) - current['current_cost']
        if waste > 0:
            print(f"\n3. WASTE (re-runs, failed attempts):")
            print(f"   Cost: ${waste:.4f}")

        print()
        sys.exit(0)

    print(f"\n{'='*60}")
    print("MASS UI REPRODUCER")
    print(f"{'='*60}")
    print(f"Data dir: {data_dir.relative_to(BASE_DIR)}")
    print(f"Workers: {args.workers}")
    print(f"Model: {args.model}")
    print(f"Iterations: {args.iterations}")
    print()

    # Ensure data variants exist
    if not DATA_VARIANTS_FILE.exists():
        print("Generating data variants...")
        cmd = [
            sys.executable, str(SCRIPT_DIR / "generate_data_variants.py"),
            "--num-variants", "100",
            "--products-per-variant", "10",
            "--output", "data_variants.ndjson",
            "--seed", "42",
            "--data-dir", str(data_dir.relative_to(BASE_DIR))
        ]
        subprocess.run(cmd, cwd=str(SCRIPT_DIR), check=True)
        print()

    # Find images to process
    all_images = find_desktop_images(data_dir)
    if not all_images:
        print("ERROR: No desktop images found")
        sys.exit(1)

    # Apply filters
    if args.page_filter:
        all_images = [img for img in all_images if args.page_filter.lower() in str(img).lower()]

    # state already loaded at top of main()
    processed = set(state.get("processed", []))
    failed = set(state.get("failed", []))

    if args.allow_repeats:
        images_to_process = all_images
    else:
        images_to_process = [
            img for img in all_images
            if str(img.relative_to(BASE_DIR)) not in processed
        ]

    if args.limit:
        images_to_process = images_to_process[:args.limit]

    print(f"Found {len(all_images)} total desktop images")
    print(f"Already processed: {len(processed)}")
    print(f"Previously failed: {len(failed)}")
    print(f"To process: {len(images_to_process)}")
    print()

    if not images_to_process:
        print("Nothing to process! Use --allow-repeats to re-run.")
        sys.exit(0)

    if args.dry_run:
        print("DRY RUN - Would process:")
        for img in images_to_process[:20]:
            print(f"  {img.relative_to(BASE_DIR)}")
        if len(images_to_process) > 20:
            print(f"  ... and {len(images_to_process) - 20} more")
        sys.exit(0)

    # Build tasks
    tasks = []
    for i, img in enumerate(images_to_process):
        tasks.append({
            "image_path": str(img),
            "data_dir": str(data_dir),
            "iterations": args.iterations,
            "model": args.model,
            "backend": args.backend,
            "keep_all": args.keep_all,
            "worker_id": i,
        })

    # Process in parallel
    start_time = time.time()
    completed = 0
    succeeded = 0
    failed_count = 0

    # Cost tracking for this run
    run_cost = 0.0
    run_input_tokens = 0
    run_output_tokens = 0

    if args.sequential:
        print("Processing sequentially (--sequential mode, safest)...")
        args.workers = 1
    else:
        print(f"Starting parallel processing with {args.workers} workers (stagger: {args.stagger}s)...")
        print(f"  Tip: Use --sequential for maximum reliability, or increase --stagger if issues occur")
    print()

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        # Stagger task submission to reduce resource contention
        # This prevents all workers from:
        # 1. Starting Vite servers simultaneously (CPU/memory contention)
        # 2. Hitting Claude API at the exact same time
        # 3. Competing for ports during startup
        futures = {}
        for i, task in enumerate(tasks):
            futures[executor.submit(reproduce_single_image, task)] = task
            # Stagger first batch of workers to avoid thundering herd
            # Longer stagger in sequential mode ensures full isolation
            if i < args.workers - 1:
                time.sleep(args.stagger)

        for future in as_completed(futures):
            task = futures[future]
            image_path = Path(task["image_path"])
            image_rel = str(image_path.relative_to(BASE_DIR))

            try:
                result = future.result()
                completed += 1

                if result["success"]:
                    succeeded += 1
                    status = "✓"
                    # Update state
                    if image_rel not in state["processed"]:
                        state["processed"].append(image_rel)

                    # Aggregate cost
                    if result.get("cost"):
                        cost = result["cost"]
                        run_cost += cost.get("total_cost", 0)
                        run_input_tokens += cost.get("input_tokens", 0)
                        run_output_tokens += cost.get("output_tokens", 0)
                        state["total_cost"] = state.get("total_cost", 0) + cost.get("total_cost", 0)
                        state["total_input_tokens"] = state.get("total_input_tokens", 0) + cost.get("input_tokens", 0)
                        state["total_output_tokens"] = state.get("total_output_tokens", 0) + cost.get("output_tokens", 0)
                else:
                    failed_count += 1
                    status = "✗"
                    if image_rel not in state["failed"]:
                        state["failed"].append(image_rel)

                duration = result.get("duration", 0)
                cost_str = ""
                if result.get("cost") and result["cost"].get("total_cost"):
                    cost_str = f" ${result['cost']['total_cost']:.3f}"
                print(f"  [{completed}/{len(tasks)}] {status} {image_path.stem} ({duration:.1f}s{cost_str})")
                if result.get("error"):
                    print(f"      Error: {result['error'][:100]}")

                # Save state and show running cost periodically
                if completed % 5 == 0:
                    save_state(state)
                    if run_cost > 0:
                        print(f"      [Running total: ${run_cost:.4f} for {succeeded} images]")

            except Exception as e:
                completed += 1
                failed_count += 1
                print(f"  [{completed}/{len(tasks)}] ✗ {image_path.stem} - {str(e)[:100]}")
                if image_rel not in state["failed"]:
                    state["failed"].append(image_rel)

    # Final state save
    save_state(state)

    # Summary
    elapsed = time.time() - start_time
    rate = completed / elapsed if elapsed > 0 else 0

    print()
    print(f"{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"Completed: {completed}/{len(tasks)}")
    print(f"Succeeded: {succeeded}")
    print(f"Failed: {failed_count}")
    print(f"Duration: {elapsed:.1f}s ({rate:.2f} images/sec)")
    print()
    print("Cost (this run):")
    print(f"  Spent: ${run_cost:.4f}")
    print(f"  Input tokens: {run_input_tokens:,}")
    print(f"  Output tokens: {run_output_tokens:,}")
    if succeeded > 0:
        print(f"  Avg per image: ${run_cost/succeeded:.4f}")
    print()
    print("Cost (cumulative - all runs ever):")
    print(f"  Total spent: ${state.get('total_cost', 0):.4f}")
    print()
    print(f"Run --show-costs for detailed breakdown including current vs wasted spend")
    print(f"State saved to: {STATE_FILE.relative_to(BASE_DIR)}")
    print()


if __name__ == "__main__":
    main()
