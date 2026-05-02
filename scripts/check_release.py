#!/usr/bin/env python3
"""Check tracked text files for common review-anonymity leaks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PATTERNS = [
    "nathanjzhao",
    "nathanzhao",
    "nathan",
    "zhao",
    "/users/",
    "stanford",
]

SKIP_SUFFIXES = {
    ".gif",
    ".ico",
    ".jpg",
    ".jpeg",
    ".pdf",
    ".png",
    ".pt",
    ".svg",
    ".tar",
    ".zip",
}


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line.strip()]


def is_text(path: Path) -> bool:
    if not path.exists() or path.is_dir():
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    try:
        path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    return True


def main() -> int:
    findings: list[tuple[str, int, str]] = []
    for path in candidate_files():
        if path == Path(__file__).resolve():
            continue
        if not is_text(path):
            continue
        rel = path.relative_to(ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lower = line.lower()
            if any(pattern in lower for pattern in PATTERNS):
                findings.append((str(rel), lineno, line.strip()))

    if findings:
        print("Potential anonymity leaks found:")
        for rel, lineno, line in findings:
            print(f"{rel}:{lineno}: {line}")
        return 1

    print("No common identity/path leaks found in tracked or untracked text files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
