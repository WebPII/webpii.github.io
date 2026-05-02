#!/usr/bin/env python3
"""
Batch runner for test_workflow.py - runs N random untested desktop images.
"""

import subprocess
import random
import argparse
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

STATE_FILE = Path("ui_reproducer/.test_workflow_state.json")


def get_all_desktop_images():
    """Get all desktop images from data/ui_images."""
    ui_images_dir = Path("data/ui_images")
    return list(ui_images_dir.glob("**/*-desktop.png"))


def get_tested_images():
    """Get set of images that have already been tested from state file."""
    if not STATE_FILE.exists():
        return set()

    with open(STATE_FILE) as f:
        state = json.load(f)

    # Return set of processed image paths (normalized)
    return set(state.get("processed", []))


def run_single_test(rel_path, backend=None, capture=True):
    """Run a single test and return the result."""
    cmd = ["python", "ui_reproducer/test_workflow.py", "--image", rel_path]
    if backend:
        cmd.extend(["--backend", backend])

    try:
        if capture:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        else:
            result = subprocess.run(cmd, check=True)
        return (rel_path, True, None)
    except subprocess.CalledProcessError as e:
        return (rel_path, False, str(e))


def main():
    parser = argparse.ArgumentParser(description="Run batch tests on random untested images")
    parser.add_argument("-n", type=int, default=50, help="Number of images to test (default: 50)")
    parser.add_argument("--backend", type=str, default=None, help="Backend to use (e.g., opencode)")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running")
    parser.add_argument("--workers", "-w", type=int, default=4, help="Number of parallel workers (default: 4)")
    args = parser.parse_args()

    all_images = get_all_desktop_images()
    tested = get_tested_images()

    print(f"Total desktop images: {len(all_images)}")
    print(f"Already tested: {len(tested)}")

    # Filter to untested images by comparing path strings
    # State file uses relative paths like "data/ui_images/..."
    untested = []
    for img in all_images:
        # Convert to relative path format matching state file
        rel_path = str(img).replace(str(Path.cwd()) + "/", "")
        if not rel_path.startswith("data/"):
            rel_path = f"data/ui_images/{img.parent.name}/{img.name}"
        if rel_path not in tested:
            untested.append((img, rel_path))

    print(f"Untested images: {len(untested)}")

    if not untested:
        print("No untested images remaining!")
        return

    # Select random sample
    n = min(args.n, len(untested))
    selected = random.sample(untested, n)

    print(f"\nRunning {n} random untested images with {args.workers} workers:\n")

    if args.dry_run:
        for i, (img, rel_path) in enumerate(selected, 1):
            cmd = ["python", "ui_reproducer/test_workflow.py", "--image", rel_path]
            if args.backend:
                cmd.extend(["--backend", args.backend])
            print(f"[{i}/{n}] Would run: {' '.join(cmd)}")
        return

    # Run tests
    completed = 0
    failed = []

    try:
        if args.workers == 1:
            # Sequential mode - show full output
            for i, (img, rel_path) in enumerate(selected, 1):
                print(f"\n[{i}/{n}] {rel_path}")
                rel_path, success, error = run_single_test(rel_path, args.backend, capture=False)
                completed += 1
                if not success:
                    print(f"  Error: {error}")
                    failed.append(rel_path)
        else:
            # Parallel mode - capture output to avoid interleaving
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(run_single_test, rel_path, args.backend, capture=True): rel_path
                    for (img, rel_path) in selected
                }

                for future in as_completed(futures):
                    rel_path, success, error = future.result()
                    completed += 1
                    if success:
                        print(f"[{completed}/{n}] ✓ {rel_path}")
                    else:
                        print(f"[{completed}/{n}] ✗ {rel_path}: {error}")
                        failed.append(rel_path)
    except KeyboardInterrupt:
        print("\nInterrupted by user")

    print(f"\nCompleted: {completed}/{n}, Failed: {len(failed)}")


if __name__ == "__main__":
    main()
