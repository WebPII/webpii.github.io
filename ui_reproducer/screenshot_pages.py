#!/usr/bin/env python3
"""
Screenshot Pages - Take screenshots of reproduced UIs with PII/product bounding boxes.

Takes desktop screenshots of pages from output/, with random scroll positions.
Detects three types of bounding boxes:
1. PII elements - Exact locations of PII text (via data-pii attributes)
2. Product elements - Exact locations of product info (via data-product attributes)
3. Order elements - Exact locations of order info (via data-order attributes)
4. Cart elements - Exact locations of cart info (via data-cart attributes, renamed to ORDER_)
5. PII containers - Regions that look like they could contain PII

Detection Strategy:
- Uses data-pii, data-product, data-order, and data-cart attributes in HTML for reliable detection
- Automatically detects ALL data-product fields, including calculated fields like:
  ORDER_TAX, ORDER_SUBTOTAL, ORDER_TOTAL, ORDER_NUM_ITEMS, etc.
- CART_* fields are automatically renamed to ORDER_* for redundancy
- No need to specify calculated fields in advance - they're found automatically

Parallelization:
- Uses page-level parallelization (each worker processes one page's all variants)
- Each page has its own src/data.json file (no race conditions)
- No file locking needed since data files are per-page
- Use --workers N to control parallelism (default: 1)

Usage:
    python screenshot_pages.py --data data.json
    python screenshot_pages.py --data data_variants.ndjson --scrolls-per-variant 5 --scroll-top
    python screenshot_pages.py --data data_variants.ndjson --num-variants 5 --workers 4
    python screenshot_pages.py --no-full-page  # Viewport-only screenshots instead of full-page
    python screenshot_pages.py --after 20260113  # Only pages created on or after Jan 13, 2026
    python screenshot_pages.py --after 20260113_150000  # Only pages after 3pm on Jan 13
"""

import argparse
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, Page
from PIL import Image, ImageDraw


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


def kill_processes_on_ports(start_port: int, count: int):
    """Kill any processes using ports in range [start_port, start_port + count).

    Uses lsof to find and kill processes. Useful for cleaning up leftover
    Vite servers from crashed runs.
    """
    killed = []

    for port in range(start_port, start_port + count):
        if is_port_available(port):
            continue

        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.stdout.strip():
                pids = result.stdout.strip().split('\n')
                for pid in pids:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                        killed.append((port, int(pid)))
                    except (ProcessLookupError, ValueError, PermissionError):
                        pass
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if killed:
        print(f"Killed {len(killed)} leftover processes on ports: {[p for p, _ in killed]}")
        time.sleep(1)  # Give OS time to release ports


SCRIPT_DIR = Path(__file__).parent
BASE_DIR = SCRIPT_DIR.parent  # pii/ directory - used for relative paths
OUTPUT_BASE = SCRIPT_DIR / "output"
SCREENSHOTS_DIR = SCRIPT_DIR / "screenshots"

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800


@dataclass
class BBox:
    """Bounding box coordinates."""
    x: float
    y: float
    width: float
    height: float

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    def intersects_viewport(self, viewport_height: int) -> tuple[bool, bool]:
        """Returns (visible, clipped) - visible=any part showing, clipped=partially cut off."""
        top = self.y
        bottom = self.y + self.height

        if bottom < 0 or top > viewport_height:
            return False, False  # Completely off screen

        if top >= 0 and bottom <= viewport_height:
            return True, False  # Fully visible

        return True, True  # Partially visible (clipped)


@dataclass
class PIIElement:
    """A detected PII element."""
    key: str
    value: str
    bbox: BBox
    visible: bool
    clipped: bool
    element_type: str  # text, input, image


@dataclass
class ProductElement:
    """A detected product element."""
    key: str
    value: str
    bbox: BBox
    visible: bool
    clipped: bool
    element_type: str


@dataclass
class PIIContainer:
    """A container that looks like it could contain PII."""
    container_type: str  # input_field, text_block, profile_section, table_row, card, form_group, list_item
    bbox: BBox
    visible: bool
    clipped: bool
    contains_actual_pii: bool
    pii_keys: list[str]
    semantic_hint: str


def load_requires_json(page_dir: Path) -> dict:
    """Load requires.json from a page directory to know which data fields are used."""
    requires_path = page_dir / "requires.json"
    if requires_path.exists():
        try:
            with open(requires_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def load_reproduction_log(page_dir: Path) -> dict:
    """Load reproduction.log from a page directory to get source image path and output dir."""
    log_path = page_dir / "reproduction.log"
    if log_path.exists():
        try:
            with open(log_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def find_latest_pages(output_dir: Path, include_incomplete: bool = False, after_timestamp: str = None) -> list[dict]:
    """Find the latest version of each reproduced page.

    Args:
        output_dir: Base output directory to search
        include_incomplete: If True, include pages with status="in_progress" (default: False)
        after_timestamp: If provided, only include pages with timestamp >= this value (e.g., '20260113' or '20260113_120000')

    Returns:
        List of page info dicts, each containing path, metadata, and reproduction status
    """
    pages = []
    skipped_incomplete = 0
    skipped_before_timestamp = 0

    # Structure: output/{device}/{company}/{page_type}/{image_id}/{timestamp}/
    for device_dir in output_dir.iterdir():
        if not device_dir.is_dir() or device_dir.name.startswith("."):
            continue

        for company_dir in device_dir.iterdir():
            if not company_dir.is_dir():
                continue

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

                        # Filter by timestamp if specified
                        if after_timestamp and latest.name < after_timestamp:
                            skipped_before_timestamp += 1
                            continue

                        # Load reproduction.log for status and paths
                        repro_log = load_reproduction_log(latest)
                        paths_info = repro_log.get("paths", {})
                        status = repro_log.get("status", "unknown")

                        # Check if reproduction is complete
                        has_app_jsx = (latest / "src" / "App.jsx").exists()

                        # Determine effective status
                        if not has_app_jsx:
                            status = "incomplete"  # No App.jsx means definitely incomplete
                        elif status == "in_progress":
                            status = "in_progress"  # Reproduction started but didn't finish
                        # If status is "completed", keep it; otherwise stays "unknown"

                        # Skip non-completed pages unless explicitly requested
                        # (unknown status from old logs without status field are excluded by default)
                        if status != "completed" and not include_incomplete:
                            skipped_incomplete += 1
                            continue

                        # Skip if no App.jsx (can't screenshot without it)
                        if not has_app_jsx:
                            continue

                        # Load requires.json to know which fields are used
                        requires = load_requires_json(latest)

                        pages.append({
                            "path": latest,
                            "device": device_dir.name,
                            "company": company_dir.name,
                            "page_type": page_type_dir.name,
                            "image_id": image_id_dir.name,
                            "timestamp": latest.name,
                            "status": status,  # "completed", "in_progress", or "incomplete"
                            "required_fields": requires.get("all_fields", []),
                            "required_pii": requires.get("required_fields", {}).get("pii", []),
                            "required_products": requires.get("required_fields", {}).get("products", []),
                            # Paths from reproduction.log (relative to pii/)
                            "source_image": paths_info.get("source_image"),
                            "output_dir": paths_info.get("output_dir"),
                        })

    if skipped_incomplete > 0:
        print(f"Skipped {skipped_incomplete} non-completed reproductions (in_progress/unknown status; use --include-incomplete to include)")
    if skipped_before_timestamp > 0:
        print(f"Skipped {skipped_before_timestamp} pages with timestamp before '{after_timestamp}'")

    return pages


def inject_data_json(page_dir: Path, data: dict, partial_fill: bool = False) -> Path:
    """Write data to the page's src/data.json file.

    Each page has its own src/data.json file, so no race conditions between pages.

    Args:
        page_dir: Path to the page directory
        data: Data variant dict
        partial_fill: If True, generate PARTIAL_FILL_CONFIG with one partial field

    Returns the path to the data file.
    """
    data_path = page_dir / "src" / "data.json"

    # Remove _meta before writing (internal use only)
    clean_data = {k: v for k, v in data.items() if not k.startswith("_")}

    # Add partial fill config
    # Uses requires.json to know which PII fields are used, picks one to be partial
    if partial_fill:
        # Include page identity in seed for diversity across pages with same data variant
        page_hash = hash(str(page_dir)) % 10000
        rng = random.Random(clean_data.get('SEED', 0) + 1000 + page_hash)

        # Load requires.json to get PII form fields actually used in App.jsx
        requires_path = page_dir / "requires.json"
        pii_form_fields = []
        if requires_path.exists():
            try:
                with open(requires_path) as f:
                    requires = json.load(f)
                    # Use pii_form_fields (combined inputs+selects in DOM order)
                    # Fallback to pii_inputs for backwards compatibility
                    pii_form_fields = requires.get("required_fields", {}).get("pii_form_fields", [])
                    if not pii_form_fields:
                        pii_form_fields = requires.get("required_fields", {}).get("pii_inputs", [])
                    if not pii_form_fields:
                        # Final fallback: filter pii fields to exclude images/avatars
                        pii_fields = requires.get("required_fields", {}).get("pii", [])
                        pii_form_fields = [f for f in pii_fields if 'IMAGE' not in f and 'AVATAR' not in f]
            except Exception:
                pass

        # Use the detected form fields (inputs + selects in DOM order)
        input_pii_fields = pii_form_fields

        if input_pii_fields:
            # Pick a random field to be partial
            partial_idx = rng.randint(0, len(input_pii_fields) - 1)
            partial_field = input_pii_fields[partial_idx]

            # Compute char count based on actual field value (20-80% of length)
            field_value = str(clean_data.get(partial_field, ''))
            field_len = len(field_value)
            if field_len > 0:
                ratio = rng.uniform(0.2, 0.8)
                stop_char_count = max(1, int(field_len * ratio))
            else:
                stop_char_count = 1

            # Fields after partial_idx are empty
            empty_fields = input_pii_fields[partial_idx + 1:]

            clean_data['PARTIAL_FILL_CONFIG'] = {
                'enabled': True,
                'partialField': partial_field,
                'stopCharCount': stop_char_count,
                'emptyFields': empty_fields
            }
        else:
            clean_data['PARTIAL_FILL_CONFIG'] = {'enabled': False}
    else:
        clean_data['PARTIAL_FILL_CONFIG'] = {'enabled': False}

    # Ensure src/ directory exists
    data_path.parent.mkdir(parents=True, exist_ok=True)

    with open(data_path, "w") as f:
        json.dump(clean_data, f, indent=2)

    # Clear Vite cache to ensure fresh module resolution
    vite_cache = page_dir / "node_modules" / ".vite"
    if vite_cache.exists():
        shutil.rmtree(vite_cache, ignore_errors=True)

    return data_path




def retry_goto(page: Page, url: str, max_retries: int = 5, wait_until: str = "networkidle"):
    """Retry page.goto() with exponential backoff.

    Sometimes the server is listening but not fully ready to serve content.
    Increased to 5 retries to handle multiprocessing resource contention.
    """
    for attempt in range(max_retries):
        try:
            page.goto(url, wait_until=wait_until, timeout=45000)  # 45s timeout
            return
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            # Exponential backoff: 2s, 4s, 8s, 16s, 32s
            sleep_time = 2 ** (attempt + 1)
            time.sleep(sleep_time)


def wait_for_react_render(page: Page, url: str, max_retries: int = 4) -> bool:
    """Wait for React to render content, with retries if page is blank.

    Returns True if content rendered, False if still blank after retries.
    """
    for attempt in range(max_retries):
        # Initial wait for Vite/React to start rendering
        # Longer wait on first attempt since server might still be warming up
        initial_wait = 2500 if attempt == 0 else 1500
        page.wait_for_timeout(initial_wait)

        # Wait for body to have actual content
        try:
            page.wait_for_function(
                "() => document.body && document.body.innerText.trim().length > 50",
                timeout=20000  # Increased timeout for parallel load
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
            # Page is blank - try refreshing with longer wait
            print(f"    Page blank (text_length={text_length}), retry {attempt + 1}/{max_retries}")
            # Wait a bit before reload to let server catch up
            page.wait_for_timeout(1000)
            page.reload(wait_until="networkidle", timeout=45000)

    # Final check
    text_length = page.evaluate("() => (document.body?.innerText || '').trim().length")
    if text_length < 50:
        print(f"    WARNING: Page still appears blank after {max_retries} retries (text_length={text_length})")
        return False
    return True


def wait_for_server(port: int, timeout: int = 30) -> bool:
    """Wait for a server to be ready by polling the port.

    Returns True if server responds, False if timeout.
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', port))
            sock.close()

            if result == 0:
                # Port is open, wait a bit more for Vite to fully initialize
                time.sleep(1)
                return True
        except:
            pass

        time.sleep(0.5)

    return False


def check_server_serves_content(port: int, timeout: float = 2.0) -> bool:
    """Check if server is actually serving HTML content (not just accepting connections).

    This is more reliable than just checking if port is open, because Vite may
    accept connections before it has finished compiling.
    """
    import http.client

    for host in ['localhost', '127.0.0.1', '::1']:
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            conn.request('GET', '/')
            response = conn.getresponse()
            body = response.read(1000).decode('utf-8', errors='ignore')
            conn.close()

            # Check if we got actual HTML content (not just connection accepted)
            # Vite serves HTML with <!DOCTYPE or <html
            if response.status == 200 and ('<html' in body.lower() or '<!doctype' in body.lower()):
                return True
        except Exception:
            pass

    return False


def start_dev_server(page_dir: Path, port: int = 5173, timeout: int = 30) -> tuple[subprocess.Popen, int]:
    """Start Vite dev server for the page on a specific port.

    Waits for the server to actually be ready before returning.
    Uses HTTP content check (not just socket) to verify Vite has compiled.
    If the requested port is busy, automatically tries nearby ports.

    Returns:
        Tuple of (process, actual_port) - the port may differ from requested if it was busy.
    """
    # Check node_modules symlink exists (should be created by copy_template)
    node_modules = page_dir / "node_modules"
    if not node_modules.is_symlink() and not node_modules.exists():
        raise RuntimeError(f"node_modules missing at {page_dir}. Run: cd template && npm install")

    # Wait for port to be available (in case previous server didn't fully terminate)
    # If port is busy, try finding an available one nearby
    if not wait_for_port_available(port, timeout=2):
        # Try to find an available port instead of failing
        original_port = port
        for offset in range(1, 50):
            port = original_port + offset
            if is_port_available(port):
                break
        else:
            raise RuntimeError(f"Port {original_port} and 50 subsequent ports are all in use")

    # Use environment variable to set the port for Vite
    env = os.environ.copy()
    env["PORT"] = str(port)

    # Log file for debugging server startup issues
    log_file = page_dir / f".vite_startup_{port}.log"
    log_handle = open(log_file, "w")

    proc = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(port)],
        cwd=str(page_dir),
        stdout=log_handle,
        stderr=subprocess.STDOUT,  # Combine stderr with stdout
        env=env,
        start_new_session=True  # Create new process group so we can kill npm + vite together
    )
    # Close the handle in parent process so child can write freely
    log_handle.close()

    try:
        # Wait for server to actually serve content (not just accept connections)
        # This is more reliable than socket check because Vite may accept connections
        # before it has finished compiling
        start_time = time.time()
        port_open_time = None

        while time.time() - start_time < timeout:
            # Check if process died
            if proc.poll() is not None:
                # Read the log to see what went wrong
                time.sleep(0.1)  # Give log file a moment to flush
                try:
                    with open(log_file) as f:
                        log_contents = f.read()
                except:
                    log_contents = "(could not read log)"
                raise RuntimeError(
                    f"Vite dev server on port {port} exited with code {proc.returncode}\n"
                    f"Last 500 chars of log:\n{log_contents[-500:]}"
                )

            # First check if port is accepting connections (quick check)
            if port_open_time is None:
                port_open = False
                for host, family in [('::1', socket.AF_INET6), ('127.0.0.1', socket.AF_INET)]:
                    try:
                        test_sock = socket.socket(family, socket.SOCK_STREAM)
                        test_sock.settimeout(0.5)
                        result = test_sock.connect_ex((host, port))
                        test_sock.close()
                        if result == 0:
                            port_open = True
                            break
                    except:
                        pass

                if port_open:
                    port_open_time = time.time()
                else:
                    time.sleep(0.3)
                    continue

            # Port is open - now check if server is actually serving content
            # Give Vite some time to compile before checking
            if time.time() - port_open_time < 1.0:
                time.sleep(0.3)
                continue

            if check_server_serves_content(port, timeout=2.0):
                # Server is serving real content! Wait a bit more for React to be ready
                time.sleep(1.0)
                return proc, port

            # Not serving content yet, keep waiting
            time.sleep(0.5)

        # Timeout reached - read log to see what's happening
        time.sleep(0.1)  # Give log file a moment to flush
        try:
            with open(log_file) as f:
                log_contents = f.read()
        except:
            log_contents = "(could not read log)"

        kill_process_tree(proc)
        raise RuntimeError(
            f"Vite dev server on port {port} failed to serve content within {timeout}s\n"
            f"Process still running: {proc.poll() is None}\n"
            f"Port was open: {port_open_time is not None}\n"
            f"Last 500 chars of log:\n{log_contents[-500:]}"
        )
    except Exception as e:
        # If it's our RuntimeError, just re-raise
        if isinstance(e, RuntimeError):
            raise
        # Otherwise something unexpected happened
        kill_process_tree(proc)
        raise


def get_page_height(page: Page) -> int:
    """Get the full scrollable height of the page."""
    return page.evaluate("() => document.documentElement.scrollHeight")


def scroll_to(page: Page, scroll_y: int):
    """Scroll the page to a specific Y position."""
    page.evaluate(f"window.scrollTo(0, {scroll_y})")
    page.wait_for_timeout(500)  # Wait for scroll to settle


# JavaScript to inject for element detection
# Takes required_fields as parameter to only search for fields actually used in App.jsx
DETECTION_SCRIPT = r"""
(params) => {
    const requiredFields = params.requiredFields;
    const DEBUG = params.debug;
    const FULL_PAGE = params.fullPage;

    const results = {
        pii_elements: [],
        product_elements: [],
        search_elements: [],
        pii_containers: [],
        debug_info: []
    };

    const data = window.__INJECTED_DATA__ || {};
    const viewportHeight = window.innerHeight;
    const scrollY = window.scrollY;

    // If no required fields specified, fall back to all data keys
    const fieldsToSearch = requiredFields && requiredFields.length > 0
        ? requiredFields
        : Object.keys(data);

    // Build expanded search values (handle derived values like first name from full name)
    const expandedData = {...data};
    if (data.PII_FULLNAME) {
        const parts = data.PII_FULLNAME.split(' ');
        if (parts.length > 0) expandedData['PII_FIRSTNAME_DERIVED'] = parts[0];
        if (parts.length > 1) expandedData['PII_LASTNAME_DERIVED'] = parts[parts.length - 1];
    }
    if (data.PII_CARD_LAST4) {
        expandedData['PII_CARD_MASKED'] = '****' + data.PII_CARD_LAST4;
        expandedData['PII_CARD_DOTS'] = '••••' + data.PII_CARD_LAST4;
    }

    // Expand numeric/currency fields with formatted versions
    Object.keys(data).forEach(key => {
        const value = data[key];
        // Handle numeric values - add currency formatted versions
        if (typeof value === 'number' || (typeof value === 'string' && /^-?\d+\.?\d*$/.test(value))) {
            const numValue = typeof value === 'number' ? value : parseFloat(value);
            expandedData[key + '_FORMATTED_DOLLAR'] = '$' + numValue.toFixed(2);
            expandedData[key + '_FORMATTED_DOLLAR_NO_CENTS'] = '$' + Math.round(numValue);
            expandedData[key + '_FORMATTED_COMMA'] = numValue.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
            expandedData[key + '_FORMATTED_DOLLAR_COMMA'] = '$' + numValue.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
        }
        // Handle currency strings like "$22.40" - also store numeric version
        if (typeof value === 'string' && /^\$?\-?\d+\.?\d*$/.test(value.replace(/,/g, ''))) {
            const cleaned = value.replace(/[$,]/g, '');
            const numValue = parseFloat(cleaned);
            if (!isNaN(numValue)) {
                expandedData[key + '_NUMERIC'] = numValue.toString();
                expandedData[key + '_FORMATTED_DOLLAR'] = '$' + numValue.toFixed(2);
                expandedData[key + '_FORMATTED_COMMA'] = numValue.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
                expandedData[key + '_FORMATTED_DOLLAR_COMMA'] = '$' + numValue.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
            }
        }
    });
    // Add address component expansions
    if (data.PII_ADDRESS) {
        // Address might contain street, city, state - add derived values
        expandedData['PII_ADDRESS_FULL'] = data.PII_ADDRESS;
    }
    // Add phone number variations (normalize to 10 digits and split into parts)
    if (data.PII_PHONE) {
        // Extract just digits from phone number
        const digits = data.PII_PHONE.replace(/\\D/g, '');
        // Keep last 10 digits (remove country code if present)
        const phone10 = digits.slice(-10);
        expandedData['PII_PHONE_DIGITS'] = phone10;
        
        // Split into common parts: (AAA) BBB-CCCC
        if (phone10.length === 10) {
            expandedData['PII_PHONE_AREA'] = phone10.slice(0, 3);      // Area code
            expandedData['PII_PHONE_PREFIX'] = phone10.slice(3, 6);    // Prefix
            expandedData['PII_PHONE_LINE'] = phone10.slice(6, 10);     // Line number
            expandedData['PII_PHONE_LAST4'] = phone10.slice(6, 10);    // Last 4 digits
            
            // Common formatted variations
            expandedData['PII_PHONE_FORMATTED_DOTS'] = phone10.slice(0, 3) + '.' + phone10.slice(3, 6) + '.' + phone10.slice(6, 10);
            expandedData['PII_PHONE_FORMATTED_DASH'] = phone10.slice(0, 3) + '-' + phone10.slice(3, 6) + '-' + phone10.slice(6, 10);
            expandedData['PII_PHONE_FORMATTED_PAREN'] = '(' + phone10.slice(0, 3) + ') ' + phone10.slice(3, 6) + '-' + phone10.slice(6, 10);
        }
    }

    // Helper to get bbox relative to viewport
    function getBBox(el) {
        const rect = el.getBoundingClientRect();
        return {
            x: rect.left,
            y: rect.top,
            width: rect.width,
            height: rect.height
        };
    }

    // Helper to check if element is actually visible
    function isElementActuallyVisible(element, bbox, fullPage) {
        // Check basic visibility
        const style = window.getComputedStyle(element);
        if (style.display === 'none' || style.visibility === 'hidden') {
            return false;
        }
        const opacity = parseFloat(style.opacity);
        if (opacity < 0.1) {
            return false;
        }

        // Check if element is within viewport (skip for full-page screenshots)
        if (!fullPage && (bbox.y + bbox.height < 0 || bbox.y > viewportHeight)) {
            return false;
        }

        // Check if element has actual dimensions
        // Use bbox dimensions instead of offsetWidth/offsetHeight for inline elements
        // (offsetWidth can be 0 for inline spans even when they have visual dimensions)
        const hasDimensions = (bbox.width > 0 && bbox.height > 0) || 
                             (element.offsetWidth > 0 && element.offsetHeight > 0);
        if (!hasDimensions) {
            if (DEBUG) {
                const key = element.getAttribute('data-pii') || element.getAttribute('data-product') || element.getAttribute('data-order');
                console.log(`[DEBUG] Element ${key} rejected: no dimensions (bbox: ${bbox.width}x${bbox.height}, offset: ${element.offsetWidth}x${element.offsetHeight})`);
            }
            return false;
        }

        // If element passes basic checks, it's visible
        return true;
    }

    // Helper to check if an element (or nearest ancestor with background) is visually opaque
    // Walks up to find the first element with a defined background, then checks if opaque
    // Stops before body/html since those don't count as "covering" elements
    function isElementOpaqueBarrier(el) {
        if (!el) return false;

        // Walk up to find the first element with a background
        let current = el;
        while (current && current.tagName !== 'BODY' && current.tagName !== 'HTML') {
            const style = window.getComputedStyle(current);

            // Check element opacity - if any ancestor has low opacity, not a barrier
            const opacity = parseFloat(style.opacity);
            if (opacity < 0.85) {
                // Low opacity means we can see through - not a barrier
                return false;
            }

            // Check if this element has a defined background
            const bgColor = style.backgroundColor;
            const hasBackground = bgColor && bgColor !== 'transparent' && bgColor !== 'rgba(0, 0, 0, 0)';

            if (hasBackground) {
                // Found an element with background - check if it's opaque
                const rgbaMatch = bgColor.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)/);
                if (rgbaMatch) {
                    const alpha = rgbaMatch[4] !== undefined ? parseFloat(rgbaMatch[4]) : 1;
                    if (alpha < 0.85) {
                        // Semi-transparent background - not a barrier
                        return false;
                    }
                }

                // Opaque background found - check if element is substantial size
                const rect = current.getBoundingClientRect();
                if (rect.width >= 50 && rect.height >= 50) {
                    return true;  // This is an opaque barrier
                }
            }

            current = current.parentElement;
        }

        return false;  // No opaque barrier found before body/html
    }

    // Helper to check if element is covered by an OPAQUE element using elementFromPoint
    // Returns detailed coverage info including pattern analysis for smart clipping
    // Semi-transparent overlays (like modal backdrops) don't count - content behind is still readable
    function isElementCoveredByAnother(element, bbox) {
        // Use a dense grid of sample points to accurately detect coverage
        const gridSize = 5;  // 5x5 grid = 25 points
        const checkPoints = [];
        const coverageGrid = [];  // 2D array tracking which points are covered

        for (let row = 0; row < gridSize; row++) {
            coverageGrid[row] = [];
            for (let col = 0; col < gridSize; col++) {
                // Sample actual edges (0%, 25%, 50%, 75%, 100%) instead of inset points
                // This ensures elements with <25% visible at an edge are properly detected
                const xRatio = col / (gridSize - 1);  // 0.0, 0.25, 0.5, 0.75, 1.0
                const yRatio = row / (gridSize - 1);
                // Clamp to slightly inside bounds to avoid elementFromPoint edge issues
                const x = bbox.x + bbox.width * Math.min(xRatio, 0.99);
                const y = bbox.y + bbox.height * Math.min(yRatio, 0.99);
                checkPoints.push({
                    x: x,
                    y: y,
                    col: col,
                    row: row
                });
            }
        }

        let opaqueCoveredPoints = 0;
        let validPoints = 0;
        let coveringElements = new Map();  // Track unique covering elements and their bboxes

        for (const point of checkPoints) {
            // Skip points outside viewport
            if (point.x < 0 || point.x >= window.innerWidth || point.y < 0 || point.y >= viewportHeight) {
                coverageGrid[point.row][point.col] = 'outside';
                continue;
            }

            validPoints++;

            const topElement = document.elementFromPoint(point.x, point.y);
            if (!topElement) {
                coverageGrid[point.row][point.col] = 'visible';
                continue;
            }

            // Check if the top element is our target element or a descendant/ancestor
            const isOurElement = topElement === element ||
                                 element.contains(topElement) ||
                                 topElement.contains(element);

            // Helper to find nearest positioned ancestor (fixed/absolute/relative)
            // This identifies the "stacking context" an element belongs to
            function findPositionedAncestor(el) {
                let current = el.parentElement;
                while (current && current.tagName !== 'BODY' && current.tagName !== 'HTML') {
                    const style = window.getComputedStyle(current);
                    const position = style.position;
                    // Include relative - elements within same relative container are siblings
                    if (position === 'fixed' || position === 'absolute' || position === 'relative') {
                        return current;
                    }
                    current = current.parentElement;
                }
                return null;
            }

            // Check if both elements are in the SAME positioned container
            // This handles negative margins within the same fixed sidebar (like tracking events)
            // but still detects modals covering form fields (different positioned containers)
            let isSameStackingContext = false;
            if (!isOurElement) {
                const elPositioned = findPositionedAncestor(element);
                const topPositioned = findPositionedAncestor(topElement);
                // Only same context if BOTH have positioned ancestor AND it's the SAME one
                if (elPositioned && topPositioned && elPositioned === topPositioned) {
                    isSameStackingContext = true;
                }
            }

            if (!isOurElement && !isSameStackingContext && isElementOpaqueBarrier(topElement)) {
                opaqueCoveredPoints++;
                coverageGrid[point.row][point.col] = 'covered';

                // Find the actual opaque container (walk up to find element with bg)
                let opaqueContainer = topElement;
                while (opaqueContainer && opaqueContainer.tagName !== 'BODY') {
                    const style = window.getComputedStyle(opaqueContainer);
                    const bgColor = style.backgroundColor;
                    const hasOpaqueBg = bgColor && bgColor !== 'transparent' &&
                        bgColor !== 'rgba(0, 0, 0, 0)' && !bgColor.includes('0.5');
                    if (hasOpaqueBg) break;
                    opaqueContainer = opaqueContainer.parentElement;
                }

                if (opaqueContainer && !coveringElements.has(opaqueContainer)) {
                    const coverBbox = getBBox(opaqueContainer);
                    coveringElements.set(opaqueContainer, coverBbox);
                }
            } else {
                coverageGrid[point.row][point.col] = 'visible';
            }
        }

        // Analyze coverage pattern to determine clipping strategy
        const coverageRatio = validPoints > 0 ? opaqueCoveredPoints / validPoints : 0;
        const isFullyCovered = coverageRatio >= 0.99;  // 90%+ = fully covered
        const isPartiallyCovered = coverageRatio > 0 && coverageRatio < 0.99;

        // Analyze which edges are visible vs covered
        // Edge detection: check first/last rows and columns
        let leftEdgeVisible = 0, rightEdgeVisible = 0, topEdgeVisible = 0, bottomEdgeVisible = 0;
        let leftEdgeTotal = 0, rightEdgeTotal = 0, topEdgeTotal = 0, bottomEdgeTotal = 0;
        let centerVisible = 0, centerTotal = 0;

        for (let row = 0; row < gridSize; row++) {
            for (let col = 0; col < gridSize; col++) {
                const state = coverageGrid[row][col];
                if (state === 'outside') continue;

                const isVisible = state === 'visible';
                const isLeftEdge = col === 0;
                const isRightEdge = col === gridSize - 1;
                const isTopEdge = row === 0;
                const isBottomEdge = row === gridSize - 1;
                const isCenter = !isLeftEdge && !isRightEdge && !isTopEdge && !isBottomEdge;

                if (isLeftEdge) { leftEdgeTotal++; if (isVisible) leftEdgeVisible++; }
                if (isRightEdge) { rightEdgeTotal++; if (isVisible) rightEdgeVisible++; }
                if (isTopEdge) { topEdgeTotal++; if (isVisible) topEdgeVisible++; }
                if (isBottomEdge) { bottomEdgeTotal++; if (isVisible) bottomEdgeVisible++; }
                if (isCenter) { centerTotal++; if (isVisible) centerVisible++; }
            }
        }

        // Determine coverage pattern
        const leftVisible = leftEdgeTotal > 0 && leftEdgeVisible / leftEdgeTotal > 0.5;
        const rightVisible = rightEdgeTotal > 0 && rightEdgeVisible / rightEdgeTotal > 0.5;
        const topVisible = topEdgeTotal > 0 && topEdgeVisible / topEdgeTotal > 0.5;
        const bottomVisible = bottomEdgeTotal > 0 && bottomEdgeVisible / bottomEdgeTotal > 0.5;
        const centerCovered = centerTotal > 0 && centerVisible / centerTotal < 0.5;

        // Get the largest covering bbox (typically the modal) - need this for pattern detection
        let coveringBbox = null;
        let maxArea = 0;
        for (const [el, cbox] of coveringElements) {
            const area = cbox.width * cbox.height;
            if (area > maxArea) {
                maxArea = area;
                coveringBbox = cbox;
            }
        }

        // Coverage pattern types:
        // - 'contained': covering element is smaller (like a button overlay) → use full bbox
        // - 'center': edges visible, center/corner covered → use full bbox
        // - 'left': left side covered → clip to right portion
        // - 'right': right side covered → clip to left portion
        // - 'top': top covered → clip to bottom
        // - 'bottom': bottom covered → clip to top
        // - 'full': everything covered
        // - 'none': nothing covered
        let coveragePattern = 'none';

        // Check if covering element is smaller and contained within our element
        // This handles cases like "Add" buttons on product images
        let coveringIsContained = false;
        if (coveringBbox && isPartiallyCovered) {
            const coveringArea = coveringBbox.width * coveringBbox.height;
            const ourArea = bbox.width * bbox.height;
            // If covering element is less than 50% of our area, it's a small overlay
            if (coveringArea < ourArea * 0.5) {
                coveringIsContained = true;
            }
        }

        // Count how many edges are visible
        const visibleEdgeCount = (leftVisible ? 1 : 0) + (rightVisible ? 1 : 0) +
                                 (topVisible ? 1 : 0) + (bottomVisible ? 1 : 0);

        if (isFullyCovered) {
            coveragePattern = 'full';
        } else if (isPartiallyCovered) {
            if (coveringIsContained && visibleEdgeCount >= 3) {
                // Small overlay (like a button) with most edges visible → full bbox
                coveragePattern = 'contained';
            } else if (visibleEdgeCount === 4) {
                // All 4 edges visible (coverage is somewhere in middle) → full bbox
                coveragePattern = 'center';
            } else if (!leftVisible && rightVisible && topVisible && bottomVisible) {
                coveragePattern = 'left';  // Left side covered
            } else if (leftVisible && !rightVisible && topVisible && bottomVisible) {
                coveragePattern = 'right';  // Right side covered
            } else if (leftVisible && rightVisible && !topVisible && bottomVisible) {
                coveragePattern = 'top';  // Top covered
            } else if (leftVisible && rightVisible && topVisible && !bottomVisible) {
                coveragePattern = 'bottom';  // Bottom covered
            } else {
                coveragePattern = 'complex';  // Complex pattern, needs multiple boxes
            }
        }

        if (DEBUG && (isFullyCovered || isPartiallyCovered)) {
            results.debug_info.push({
                type: 'ELEMENT_COVERAGE_CHECK',
                elKey: element.getAttribute('data-pii') || element.getAttribute('data-product') || element.getAttribute('data-order') || 'unknown',
                opaqueCoveredPoints: opaqueCoveredPoints,
                validPoints: validPoints,
                coverageRatio: coverageRatio.toFixed(2),
                coveragePattern: coveragePattern,
                edgeVisibility: { left: leftVisible, right: rightVisible, top: topVisible, bottom: bottomVisible },
                coveringBbox: coveringBbox
            });
        }

        return {
            isFullyCovered,
            isPartiallyCovered,
            coverageRatio,
            coveragePattern,
            coveringBbox,
            coverageGrid
        };
    }

    // Helper to find covering elements (modals/overlays) that obscure this element
    function findCoveringBox(element, bbox) {
        // Look for fixed/absolute positioned elements with high z-index (modals)
        // Common selectors: .modal, [role="dialog"], .fixed, divs with high z-index
        const candidates = Array.from(document.querySelectorAll('.fixed, [role="dialog"], .modal, .z-50, .z-40'));

        if (DEBUG && candidates.length > 0) {
            results.debug_info.push({
                type: 'MODAL_CANDIDATES',
                count: candidates.length,
                elKey: element.getAttribute('data-pii') || element.getAttribute('data-product') || element.getAttribute('data-order') || 'unknown'
            });
        }

        let coveringElement = null;
        let minArea = Infinity;

        for (const candidate of candidates) {
            // Skip if it's the element itself or related
            if (candidate === element || element.contains(candidate) || candidate.contains(element)) {
                continue;
            }

            const style = window.getComputedStyle(candidate);
            const position = style.position;
            const zIndex = parseInt(style.zIndex) || 0;

            // Skip semi-transparent overlays (like bg-opacity-50 backdrops)
            // These are visually see-through, so elements behind them are still "visible"
            const opacity = parseFloat(style.opacity);
            const bgColor = style.backgroundColor;
            // Check for rgba with alpha < 1 (e.g., "rgba(0, 0, 0, 0.5)")
            const bgAlphaMatch = bgColor.match(/rgba?\([^)]*,\s*([\d.]+)\s*\)/);
            const bgAlpha = bgAlphaMatch ? parseFloat(bgAlphaMatch[1]) : 1;
            const isSemiTransparent = opacity < 0.9 || (bgAlpha < 0.9 && bgAlpha > 0);

            if (isSemiTransparent) {
                continue;  // Skip semi-transparent overlays
            }

            // Must be positioned and have reasonable z-index
            if ((position === 'fixed' || position === 'absolute') && zIndex > 10) {
                const candBbox = getBBox(candidate);

                // Skip if doesn't overlap
                const overlaps = candBbox.x < bbox.x + bbox.width &&
                    candBbox.x + candBbox.width > bbox.x &&
                    candBbox.y < bbox.y + bbox.height &&
                    candBbox.y + candBbox.height > bbox.y;

                if (DEBUG && overlaps) {
                    results.debug_info.push({
                        type: 'MODAL_OVERLAP',
                        elKey: element.getAttribute('data-pii') || 'unknown',
                        zIndex: zIndex,
                        candSize: `${candBbox.width}x${candBbox.height}`
                    });
                }

                if (!overlaps || candBbox.width < 50 || candBbox.height < 50) continue;

                // If it's a full-viewport overlay, check its children for the actual modal
                const isFullViewport = candBbox.width >= window.innerWidth * 0.9 &&
                    candBbox.height >= viewportHeight * 0.9;

                if (isFullViewport && candidate.children.length > 0) {
                    if (DEBUG) {
                        results.debug_info.push({
                            type: 'MODAL_FULLVIEWPORT',
                            elKey: element.getAttribute('data-pii') || 'unknown',
                            childCount: candidate.children.length
                        });
                    }

                    // Look for a centered, reasonably-sized child (the modal content)
                    for (const child of candidate.children) {
                        const childBbox = getBBox(child);
                        const childArea = childBbox.width * childBbox.height;

                        // Must be smaller than viewport and reasonably sized
                        const childQualifies = childBbox.width < window.innerWidth * 0.8 &&
                            childBbox.width > 200 && childBbox.height > 200;

                        if (childQualifies) {
                            const childOverlaps = childBbox.x < bbox.x + bbox.width &&
                                childBbox.x + childBbox.width > bbox.x &&
                                childBbox.y < bbox.y + bbox.height &&
                                childBbox.y + childBbox.height > bbox.y;

                            if (DEBUG) {
                                results.debug_info.push({
                                    type: 'MODAL_CHILD',
                                    elKey: element.getAttribute('data-pii') || 'unknown',
                                    childSize: `${childBbox.width}x${childBbox.height}`,
                                    overlaps: childOverlaps
                                });
                            }

                            if (childOverlaps && childArea < minArea) {
                                coveringElement = child;
                                minArea = childArea;
                            }
                        }
                    }
                } else {
                    // Regular modal (not full-viewport)
                    const area = candBbox.width * candBbox.height;
                    if (area < minArea) {
                        coveringElement = candidate;
                        minArea = area;
                    }
                }
            }
        }

        const result = coveringElement ? getBBox(coveringElement) : null;

        if (DEBUG && result) {
            results.debug_info.push({
                type: 'MODAL_FOUND',
                elKey: element.getAttribute('data-pii') || element.getAttribute('data-product') || element.getAttribute('data-order') || 'unknown',
                modalSize: `${result.width}x${result.height}`
            });
        }

        return result;
    }

    // Helper to compute clipped bbox by subtracting covering region
    function computeClippedBbox(bbox, coveringBox) {
        if (!coveringBox) return null;

        // Find the edges of the overlap
        const overlapLeft = Math.max(bbox.x, coveringBox.x);
        const overlapRight = Math.min(bbox.x + bbox.width, coveringBox.x + coveringBox.width);
        const overlapTop = Math.max(bbox.y, coveringBox.y);
        const overlapBottom = Math.min(bbox.y + bbox.height, coveringBox.y + coveringBox.height);

        // Check if there's actually an overlap
        if (overlapLeft >= overlapRight || overlapTop >= overlapBottom) {
            return null;  // No overlap, no clipping needed
        }

        // Check if element is FULLY covered by the modal
        const fullyContained = coveringBox.x <= bbox.x &&
            coveringBox.y <= bbox.y &&
            coveringBox.x + coveringBox.width >= bbox.x + bbox.width &&
            coveringBox.y + coveringBox.height >= bbox.y + bbox.height;

        if (fullyContained) {
            return { fullyCovered: true };
        }

        // Compute visible portions - can result in multiple boxes for complex cases
        // For simplicity, return the largest contiguous visible region
        const visibleRegions = [];

        // Left portion (if modal doesn't cover the left side)
        if (bbox.x < coveringBox.x) {
            visibleRegions.push({
                x: bbox.x,
                y: bbox.y,
                width: coveringBox.x - bbox.x,
                height: bbox.height
            });
        }

        // Right portion (if modal doesn't cover the right side)
        if (bbox.x + bbox.width > coveringBox.x + coveringBox.width) {
            visibleRegions.push({
                x: coveringBox.x + coveringBox.width,
                y: bbox.y,
                width: (bbox.x + bbox.width) - (coveringBox.x + coveringBox.width),
                height: bbox.height
            });
        }

        // Top portion (if modal doesn't cover the top and no left/right regions exist)
        if (bbox.y < coveringBox.y && visibleRegions.length === 0) {
            visibleRegions.push({
                x: bbox.x,
                y: bbox.y,
                width: bbox.width,
                height: coveringBox.y - bbox.y
            });
        }

        // Bottom portion (if modal doesn't cover the bottom and no other regions exist)
        if (bbox.y + bbox.height > coveringBox.y + coveringBox.height && visibleRegions.length === 0) {
            visibleRegions.push({
                x: bbox.x,
                y: coveringBox.y + coveringBox.height,
                width: bbox.width,
                height: (bbox.y + bbox.height) - (coveringBox.y + coveringBox.height)
            });
        }

        // Return the largest visible region (or all of them for multi-bbox support later)
        if (visibleRegions.length === 0) {
            return { fullyCovered: true };
        }

        // Find largest region by area
        let largest = visibleRegions[0];
        let maxArea = largest.width * largest.height;
        for (const region of visibleRegions) {
            const area = region.width * region.height;
            if (area > maxArea) {
                maxArea = area;
                largest = region;
            }
        }

        // Only return if visible region is meaningful (at least 10px in each dimension)
        if (largest.width < 10 || largest.height < 10) {
            return { fullyCovered: true };
        }

        return { clippedBbox: largest, allRegions: visibleRegions };
    }

    // Helper to check visibility and compute clipped bbox
    function checkVisibility(bbox, element) {
        // Manual override: data-force-visible="true" bypasses all visibility checks
        if (element && element.getAttribute('data-force-visible') === 'true') {
            return { visible: true, clipped: false, clippedBbox: null };
        }

        const top = bbox.y;
        const bottom = bbox.y + bbox.height;

        // Check viewport bounds (skip for full-page screenshots)
        if (!FULL_PAGE && (bottom < 0 || top > viewportHeight)) {
            return { visible: false, clipped: false, clippedBbox: null };
        }

        // Check if element is actually visible (CSS visibility, not viewport)
        if (element && !isElementActuallyVisible(element, bbox, FULL_PAGE)) {
            if (DEBUG) {
                const key = element.getAttribute('data-pii') || element.getAttribute('data-product') || element.getAttribute('data-order');
                console.log(`[DEBUG] ${key} marked not visible by isElementActuallyVisible`);
            }
            return { visible: false, clipped: false, clippedBbox: null };
        }

        // Check coverage using elementFromPoint-based detection
        let clippedBbox = null;

        if (element) {
            const coverage = isElementCoveredByAnother(element, bbox);

            if (coverage.isFullyCovered) {
                // Fully covered (90%+) - not visible
                if (DEBUG) {
                    const key = element.getAttribute('data-pii') || element.getAttribute('data-product') || element.getAttribute('data-order');
                    console.log(`[DEBUG] ${key} marked not visible: isFullyCovered`);
                }
                return { visible: false, clipped: false, clippedBbox: null };
            }

            if (coverage.isPartiallyCovered && coverage.coveringBbox) {
                // Handle based on coverage pattern
                switch (coverage.coveragePattern) {
                    case 'contained':
                        // Small overlay (like a button) on top of element
                        // Use FULL bounding box since the overlay doesn't hide the element's extent
                        clippedBbox = null;  // No clipping needed
                        if (DEBUG) {
                            results.debug_info.push({
                                type: 'CONTAINED_OVERLAY_FULL_BBOX',
                                elKey: element.getAttribute('data-pii') || element.getAttribute('data-product') || element.getAttribute('data-order') || 'unknown',
                                bbox: bbox
                            });
                        }
                        break;

                    case 'center':
                        // Edges visible but center covered - clip to largest visible region
                        // This handles inputs that span across a modal (left and right portions visible)
                        if (coverage.coveringBbox) {
                            const clipResult = computeClippedBbox(bbox, coverage.coveringBbox);
                            if (clipResult && !clipResult.fullyCovered) {
                                clippedBbox = clipResult.clippedBbox;
                            } else if (clipResult && clipResult.fullyCovered) {
                                return { visible: false, clipped: false, clippedBbox: null };
                            }
                        }
                        if (DEBUG) {
                            results.debug_info.push({
                                type: 'CENTER_COVERAGE_CLIPPED',
                                elKey: element.getAttribute('data-pii') || element.getAttribute('data-product') || element.getAttribute('data-order') || 'unknown',
                                originalBbox: bbox,
                                clippedBbox: clippedBbox
                            });
                        }
                        break;

                    case 'left':
                        // Left side covered - clip to show right portion
                        if (coverage.coveringBbox) {
                            const rightStart = coverage.coveringBbox.x + coverage.coveringBbox.width;
                            if (rightStart < bbox.x + bbox.width) {
                                clippedBbox = {
                                    x: rightStart,
                                    y: bbox.y,
                                    width: (bbox.x + bbox.width) - rightStart,
                                    height: bbox.height
                                };
                            }
                        }
                        break;

                    case 'right':
                        // Right side covered - clip to show left portion
                        if (coverage.coveringBbox) {
                            const leftEnd = coverage.coveringBbox.x;
                            if (leftEnd > bbox.x) {
                                clippedBbox = {
                                    x: bbox.x,
                                    y: bbox.y,
                                    width: leftEnd - bbox.x,
                                    height: bbox.height
                                };
                            }
                        }
                        break;

                    case 'top':
                        // Top covered - clip to show bottom portion
                        if (coverage.coveringBbox) {
                            const bottomStart = coverage.coveringBbox.y + coverage.coveringBbox.height;
                            if (bottomStart < bbox.y + bbox.height) {
                                clippedBbox = {
                                    x: bbox.x,
                                    y: bottomStart,
                                    width: bbox.width,
                                    height: (bbox.y + bbox.height) - bottomStart
                                };
                            }
                        }
                        break;

                    case 'bottom':
                        // Bottom covered - clip to show top portion
                        if (coverage.coveringBbox) {
                            const topEnd = coverage.coveringBbox.y;
                            if (topEnd > bbox.y) {
                                clippedBbox = {
                                    x: bbox.x,
                                    y: bbox.y,
                                    width: bbox.width,
                                    height: topEnd - bbox.y
                                };
                            }
                        }
                        break;

                    case 'complex':
                        // Complex pattern - use computeClippedBbox for best effort
                        const clipResult = computeClippedBbox(bbox, coverage.coveringBbox);
                        if (clipResult && !clipResult.fullyCovered) {
                            clippedBbox = clipResult.clippedBbox;
                        } else if (clipResult && clipResult.fullyCovered) {
                            return { visible: false, clipped: false, clippedBbox: null };
                        }
                        break;

                    default:
                        // 'none' or unknown - no clipping
                        break;
                }

                // Validate clipped bbox has meaningful size
                if (clippedBbox && (clippedBbox.width < 10 || clippedBbox.height < 10)) {
                    return { visible: false, clipped: false, clippedBbox: null };
                }

                if (DEBUG && clippedBbox) {
                    results.debug_info.push({
                        type: 'BBOX_CLIPPED',
                        elKey: element.getAttribute('data-pii') || element.getAttribute('data-product') || element.getAttribute('data-order') || 'unknown',
                        pattern: coverage.coveragePattern,
                        originalBbox: bbox,
                        clippedBbox: clippedBbox
                    });
                }
            }
        }

        // Check viewport clipping
        const viewportClipped = !(top >= 0 && bottom <= viewportHeight);

        return {
            visible: true,
            clipped: viewportClipped || (clippedBbox !== null),
            clippedBbox: clippedBbox  // If set, use this instead of original bbox for drawing
        };
    }

    // Helper to check if element is in footer
    function isInFooter(el) {
        let current = el;
        while (current) {
            const tag = current.tagName?.toLowerCase();
            if (tag === 'footer') return true;
            const cls = current.className?.toLowerCase() || '';
            if (cls.includes('footer')) return true;
            current = current.parentElement;
        }
        return false;
    }

    // TYPE 1 & 2: Find PII and Product elements by text content
    // Strategy 1: Look for elements with data-pii or data-product attributes first (most reliable)
    // Strategy 2: Fall back to text/image search for elements without attributes
    
    // Only search for fields that are actually used in the page (from requires.json)
    const piiKeys = fieldsToSearch.filter(k => k.startsWith('PII_') && !k.includes('IMAGE') && !k.includes('AVATAR'));
    const productTextKeys = fieldsToSearch.filter(k => (k.startsWith('PRODUCT') || k.startsWith('ORDER_') || k.startsWith('CART_')) && !k.includes('IMAGE'));
    const imageKeys = fieldsToSearch.filter(k => k.includes('IMAGE') || k.includes('AVATAR'));

    // STRATEGY 1: Find elements with data-pii attributes (most reliable)
    piiKeys.forEach(key => {
        const selector = `[data-pii="${key}"]`;
        document.querySelectorAll(selector).forEach(el => {
            if (isInFooter(el)) return;

            const bbox = getBBox(el);
            if (bbox.width < 5 || bbox.height < 5) return;

            const vis = checkVisibility(bbox, el);
            const tag = el.tagName.toLowerCase();
            const isFormElement = tag === 'input' || tag === 'textarea' || tag === 'select';
            const elementType = tag === 'img' ? 'image' : isFormElement ? 'input' : 'text';

            // For form elements (input/textarea/select), use el.value directly - don't fallback to data
            // This ensures empty inputs show as empty, not filled with data values
            // For text elements, fallback to data since we want to match displayed text
            let value;
            if (isFormElement) {
                value = el.value || '';  // Actual displayed value, empty if truly empty
            } else {
                const actualValue = el.innerText || el.textContent || '';
                value = actualValue || expandedData[key] || data[key] || '';
            }

            results.pii_elements.push({
                key: key,
                value: value,
                bbox: vis.clippedBbox || bbox,  // Use clipped bbox if available
                visible: vis.visible,
                clipped: vis.clipped,
                element_type: elementType
            });
        });
    });
    
    // STRATEGY 1b: Find elements with data-product attributes
    productTextKeys.forEach(key => {
        const selector = `[data-product="${key}"]`;
        document.querySelectorAll(selector).forEach(el => {
            if (isInFooter(el)) return;

            const bbox = getBBox(el);
            if (bbox.width < 5 || bbox.height < 5) return;

            const vis = checkVisibility(bbox, el);
            const tag = el.tagName.toLowerCase();
            const isFormElement = tag === 'input' || tag === 'textarea' || tag === 'select';
            const elementType = tag === 'img' ? 'image' : isFormElement ? 'input' : 'text';

            // For form elements, use el.value directly (don't fallback to data)
            // For text elements, get from data or element text
            let value;
            if (isFormElement) {
                value = el.value || '';
            } else {
                value = expandedData[key] || data[key];
                if (!value && (key.startsWith('ORDER_') || key.startsWith('CART_'))) {
                    // Extract displayed value from element for calculated ORDER_* and CART_* fields
                    value = el.innerText || el.textContent || '';
                    value = value.trim();
                }
                value = value || '';
            }

            results.product_elements.push({
                key: key,
                value: value,
                bbox: vis.clippedBbox || bbox,  // Use clipped bbox if available
                visible: vis.visible,
                clipped: vis.clipped,
                element_type: elementType
            });
        });
    });

    // STRATEGY 1c: Find ANY data-product attributes in DOM (not just required_fields)
    // This catches calculated fields that aren't in the original data
    const foundProductElements = new Set(results.product_elements.map(e => e.key + '_' + e.bbox.x + '_' + e.bbox.y));
    document.querySelectorAll('[data-product]').forEach(el => {
        const key = el.getAttribute('data-product');
        if (!key) return;
        if (isInFooter(el)) return;

        const bbox = getBBox(el);
        if (bbox.width < 5 || bbox.height < 5) return;

        // Check if this specific element (key + position) was already found
        const elementId = key + '_' + bbox.x + '_' + bbox.y;
        if (foundProductElements.has(elementId)) return; // Skip duplicate
        foundProductElements.add(elementId);

        const vis = checkVisibility(bbox, el);

        const tag = el.tagName.toLowerCase();
        const isFormElement = tag === 'input' || tag === 'textarea' || tag === 'select';
        const elementType = tag === 'img' ? 'image' : isFormElement ? 'input' : 'text';

        // For form elements, use el.value directly; for text, use innerText/textContent
        let value = isFormElement ? (el.value || '') : (el.innerText || el.textContent || '');
        value = value.trim();

        results.product_elements.push({
            key: key,
            value: value,
            bbox: vis.clippedBbox || bbox,  // Use clipped bbox if available
            visible: vis.visible,
            clipped: vis.clipped,
            element_type: elementType
        });
    });

    // STRATEGY 1c2: Find ANY data-order attributes in DOM (ORDER_* fields)
    // These are order-related fields like ORDER_DATE, ORDER_TOTAL, ORDER_TRACKING_NUMBER
    document.querySelectorAll('[data-order]').forEach(el => {
        const key = el.getAttribute('data-order');
        if (!key) return;
        if (isInFooter(el)) return;

        const bbox = getBBox(el);
        if (bbox.width < 5 || bbox.height < 5) return;

        // Check if this specific element (key + position) was already found
        const elementId = key + '_' + bbox.x + '_' + bbox.y;
        if (foundProductElements.has(elementId)) return; // Skip duplicate
        foundProductElements.add(elementId);

        const vis = checkVisibility(bbox, el);

        const tag = el.tagName.toLowerCase();
        const isFormElement = tag === 'input' || tag === 'textarea' || tag === 'select';
        const elementType = tag === 'img' ? 'image' : isFormElement ? 'input' : 'text';

        // For form elements, use el.value directly; for text, use innerText/textContent
        let value = isFormElement ? (el.value || '') : (el.innerText || el.textContent || '');
        value = value.trim();

        // Add to product_elements (ORDER_* fields are treated as product/order elements for display)
        results.product_elements.push({
            key: key,
            value: value,
            bbox: vis.clippedBbox || bbox,
            visible: vis.visible,
            clipped: vis.clipped,
            element_type: elementType
        });
    });

    // STRATEGY 1c2b: Find ANY data-cart attributes in DOM (CART_* fields)
    // These are cart-related fields that should be treated as ORDER_* fields for redundancy
    document.querySelectorAll('[data-cart]').forEach(el => {
        let key = el.getAttribute('data-cart');
        if (!key) return;
        if (isInFooter(el)) return;

        const bbox = getBBox(el);
        if (bbox.width < 5 || bbox.height < 5) return;

        // Rename CART_ to ORDER_ for redundancy
        if (key.startsWith('CART_')) {
            key = key.replace('CART_', 'ORDER_');
        }

        // Check if this specific element (key + position) was already found
        const elementId = key + '_' + bbox.x + '_' + bbox.y;
        if (foundProductElements.has(elementId)) return; // Skip duplicate
        foundProductElements.add(elementId);

        const vis = checkVisibility(bbox, el);

        const tag = el.tagName.toLowerCase();
        const isFormElement = tag === 'input' || tag === 'textarea' || tag === 'select';
        const elementType = tag === 'img' ? 'image' : isFormElement ? 'input' : 'text';

        // For form elements, use el.value directly; for text, use innerText/textContent
        let value = isFormElement ? (el.value || '') : (el.innerText || el.textContent || '');
        value = value.trim();

        // Add to product_elements (CART_* fields renamed to ORDER_* are treated as product/order elements)
        results.product_elements.push({
            key: key,
            value: value,
            bbox: vis.clippedBbox || bbox,
            visible: vis.visible,
            clipped: vis.clipped,
            element_type: elementType
        });
    });

    // STRATEGY 1c3: Find ANY data-search attributes in DOM (search input fields)
    // These are search fields that should be detected but kept empty
    document.querySelectorAll('[data-search]').forEach(el => {
        const key = el.getAttribute('data-search');
        if (!key) return;
        if (isInFooter(el)) return;

        const bbox = getBBox(el);
        if (bbox.width < 5 || bbox.height < 5) return;

        const vis = checkVisibility(bbox, el);

        const tag = el.tagName.toLowerCase();
        const isFormElement = tag === 'input' || tag === 'textarea';
        const elementType = isFormElement ? 'input' : 'text';

        // For form elements, use el.value directly; for text, use placeholder or empty
        let value = isFormElement ? (el.value || el.placeholder || '') : (el.innerText || el.textContent || '');
        value = value.trim();

        results.search_elements.push({
            key: key,
            value: value,
            bbox: vis.clippedBbox || bbox,
            visible: vis.visible,
            clipped: vis.clipped,
            element_type: elementType
        });
    });

    // STRATEGY 1d: Find ANY data-pii attributes in DOM (not just required_fields)
    const foundPiiElements = new Set(results.pii_elements.map(e => e.key + '_' + e.bbox.x + '_' + e.bbox.y));
    const debugPiiCity = DEBUG && document.querySelectorAll('[data-pii="PII_CITY"]').length > 0;
    document.querySelectorAll('[data-pii]').forEach(el => {
        const key = el.getAttribute('data-pii');
        if (!key) return;

        const inFooter = isInFooter(el);
        const bbox = getBBox(el);
        const tooSmall = bbox.width < 5 || bbox.height < 5;
        const elementId = key + '_' + bbox.x + '_' + bbox.y;
        const isDuplicate = foundPiiElements.has(elementId);

        const tag = el.tagName.toLowerCase();
        const isFormElement = tag === 'input' || tag === 'textarea' || tag === 'select';

        if (debugPiiCity && key === 'PII_CITY') {
            const debugValue = isFormElement ? (el.value || '') : (el.innerText || el.textContent || '');
            results.debug_info.push({
                type: 'PII_CITY_DETECTION',
                tagName: el.tagName,
                value: debugValue.substring(0, 50),
                bbox: bbox,
                inFooter: inFooter,
                tooSmall: tooSmall,
                isDuplicate: isDuplicate,
                willSkip: inFooter || tooSmall || isDuplicate
            });
        }

        if (inFooter) return;
        if (tooSmall) return;
        if (isDuplicate) return; // Skip duplicate
        foundPiiElements.add(elementId);

        const vis = checkVisibility(bbox, el);

        // For form elements, use el.value directly; for text, use innerText/textContent
        const value = isFormElement ? (el.value || '') : (el.innerText || el.textContent || '');
        const elementType = tag === 'img' ? 'image' : isFormElement ? 'input' : 'text';

        results.pii_elements.push({
            key: key,
            value: value,
            bbox: vis.clippedBbox || bbox,  // Use clipped bbox if available
            visible: vis.visible,
            clipped: vis.clipped,
            element_type: elementType
        });
    });

    // Find text elements - improved version with multiple search strategies
    function findTextElements(keys, targetArray, keyPrefix, dataSource) {
        keys.forEach(key => {
            const value = dataSource[key];
            if (!value || typeof value !== 'string' || value.length < 2) return;

            // Normalize the search value (handle special chars)
            const searchValue = value.trim();
            if (!searchValue) return;

            const foundElements = new Set(); // Track unique elements to avoid duplicates

            // Strategy 1: TreeWalker for text nodes
            try {
                const walker = document.createTreeWalker(
                    document.body,
                    NodeFilter.SHOW_TEXT,
                    null,
                    false
                );

                let node;
                while (node = walker.nextNode()) {
                    const textContent = node.textContent || '';

                    // Use word boundary check for short values (like state abbreviations "IN", "OR")
                    // to avoid matching inside words like "SHIPPING" or "Sign In"
                    let matches = false;
                    if (searchValue.length <= 3) {
                        // For short strings (like state abbreviations), use CASE-SENSITIVE word boundaries
                        // This prevents "IN" from matching "in" within "Sign in" or "SHIPPING"
                        // Escape special regex characters
                        const escaped = searchValue.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                        // Use case-sensitive matching (no 'i' flag) for short values
                        const regex = new RegExp('\\\\b' + escaped + '\\\\b');
                        matches = regex.test(textContent);
                    } else {
                        // For longer strings, case-insensitive substring match is fine
                        matches = textContent.toLowerCase().includes(searchValue.toLowerCase());
                    }

                    if (matches) {
                        const parent = node.parentElement;
                        if (!parent || isInFooter(parent)) continue;
                        if (foundElements.has(parent)) continue;

                        const bbox = getBBox(parent);
                        if (bbox.width < 5 || bbox.height < 5) continue;

                        foundElements.add(parent);
                        const vis = checkVisibility(bbox, parent);
                        targetArray.push({
                            key: key,
                            value: searchValue,
                            bbox: vis.clippedBbox || bbox,
                            visible: vis.visible,
                            clipped: vis.clipped,
                            element_type: 'text'
                        });
                    }
                }
            } catch (e) {
                console.warn('TreeWalker search failed:', e);
            }

            // Strategy 2: Query common text containers directly
            try {
                const textSelectors = 'p, span, div, h1, h2, h3, h4, h5, h6, td, th, li, a, label, strong, em, b, i';
                document.querySelectorAll(textSelectors).forEach(el => {
                    if (foundElements.has(el)) return;
                    if (isInFooter(el)) return;

                    // Check direct text content (not nested)
                    const directText = Array.from(el.childNodes)
                        .filter(n => n.nodeType === Node.TEXT_NODE)
                        .map(n => n.textContent)
                        .join('');

                    // Also check innerText for elements that might format text
                    const innerText = el.innerText || '';

                    if (directText.includes(searchValue) || innerText.includes(searchValue)) {
                        const bbox = getBBox(el);
                        if (bbox.width < 5 || bbox.height < 5) return;

                        // Avoid very large containers
                        if (bbox.width > 800 && bbox.height > 200) return;

                        foundElements.add(el);
                        const vis = checkVisibility(bbox, el);
                        targetArray.push({
                            key: key,
                            value: searchValue,
                            bbox: vis.clippedBbox || bbox,
                            visible: vis.visible,
                            clipped: vis.clipped,
                            element_type: 'text'
                        });
                    }
                });
            } catch (e) {
                console.warn('Direct selector search failed:', e);
            }
        });
    }

    // DISABLED: Text matching disabled to prevent false positives
    // findTextElements(piiKeys, results.pii_elements, 'PII_', expandedData);
    // findTextElements(productTextKeys, results.product_elements, 'PRODUCT', data);

    // Find image elements - improved with multiple matching strategies
    // First check for data-pii and data-product attributes on images
    imageKeys.forEach(key => {
        const value = data[key];
        if (!value || typeof value !== 'string') return;

        const foundImages = new Set();
        
        // STRATEGY 1: Look for images with data-pii or data-product attributes
        const attrName = key.startsWith('PII_') ? 'data-pii' : 'data-product';
        const selector = `img[${attrName}="${key}"]`;
        document.querySelectorAll(selector).forEach(img => {
            if (foundImages.has(img)) return;
            if (isInFooter(img)) return;
            
            const bbox = getBBox(img);
            if (bbox.width < 10 || bbox.height < 10) return;

            foundImages.add(img);
            const vis = checkVisibility(bbox, img);
            const targetArray = key.startsWith('PII_') ? results.pii_elements : results.product_elements;
            targetArray.push({
                key: key,
                value: value,
                bbox: vis.clippedBbox || bbox,
                visible: vis.visible,
                clipped: vis.clipped,
                element_type: 'image'
            });
        });

        // STRATEGY 2: Fall back to src matching if no attributed images found
        document.querySelectorAll('img').forEach(img => {
            if (foundImages.has(img)) return;
            if (isInFooter(img)) return;

            const src = img.src || img.getAttribute('src') || '';
            const dataSrc = img.getAttribute('data-src') || '';
            const srcset = img.srcset || img.getAttribute('srcset') || '';

            // Multiple matching strategies
            let matched = false;

            // Direct path match
            if (src.includes(value) || dataSrc.includes(value)) {
                matched = true;
            }

            // Filename match (for paths like /products/image.jpg)
            if (!matched && value.includes('/')) {
                const filename = value.split('/').pop();
                if (filename && (src.includes(filename) || dataSrc.includes(filename))) {
                    matched = true;
                }
            }

            // Srcset match
            if (!matched && srcset.includes(value)) {
                matched = true;
            }

            // Background image on parent (for CSS background images)
            if (!matched) {
                const parent = img.parentElement;
                if (parent) {
                    const style = window.getComputedStyle(parent);
                    const bgImage = style.backgroundImage || '';
                    if (bgImage.includes(value)) {
                        matched = true;
                    }
                }
            }

            if (matched) {
                const bbox = getBBox(img);
                if (bbox.width < 10 || bbox.height < 10) return;

                foundImages.add(img);
                const vis = checkVisibility(bbox, img);
                const targetArray = key.startsWith('PII_') ? results.pii_elements : results.product_elements;
                targetArray.push({
                    key: key,
                    value: value,
                    bbox: vis.clippedBbox || bbox,
                    visible: vis.visible,
                    clipped: vis.clipped,
                    element_type: 'image'
                });
            }
        });

        // Also check for background images on divs
        document.querySelectorAll('div, span, figure').forEach(el => {
            if (isInFooter(el)) return;

            const style = window.getComputedStyle(el);
            const bgImage = style.backgroundImage || '';

            if (bgImage && bgImage !== 'none' && bgImage.includes(value)) {
                const bbox = getBBox(el);
                if (bbox.width < 10 || bbox.height < 10) return;

                const vis = checkVisibility(bbox, el);
                const targetArray = key.startsWith('PII_') ? results.pii_elements : results.product_elements;
                targetArray.push({
                    key: key,
                    value: value,
                    bbox: vis.clippedBbox || bbox,
                    visible: vis.visible,
                    clipped: vis.clipped,
                    element_type: 'image'
                });
            }
        });
    });

    // TYPE 3: Find PII-candidate containers
    const containerSelectors = [
        { selector: 'input:not([type="hidden"]):not([type="submit"]):not([type="button"])', type: 'input_field' },
        { selector: 'textarea', type: 'input_field' },
        { selector: '[contenteditable="true"]', type: 'input_field' },
        { selector: 'tr', type: 'table_row' },
        { selector: 'td', type: 'table_row' },
        { selector: 'li', type: 'list_item' },
    ];

    // Find containers by selector
    containerSelectors.forEach(({ selector, type }) => {
        document.querySelectorAll(selector).forEach(el => {
            if (isInFooter(el)) return;

            const bbox = getBBox(el);
            if (bbox.width < 20 || bbox.height < 10) return;
            if (bbox.height > 500) return; // Too large

            const vis = checkVisibility(bbox, el);

            // Check if contains actual PII
            const elText = el.innerText || el.value || '';
            const containedPII = [];
            piiKeys.forEach(key => {
                const val = expandedData[key];
                if (val && elText.includes(val)) {
                    containedPII.push(key);
                }
            });

            // Generate semantic hint
            let hint = type.replace('_', ' ');
            const text = elText.toLowerCase();
            if (text.includes('@') || text.includes('email')) hint = 'email ' + hint;
            else if (/\\d{3}.*\\d{4}/.test(text) || text.includes('phone')) hint = 'phone ' + hint;
            else if (text.includes('address') || text.includes('street')) hint = 'address ' + hint;
            else if (text.includes('name')) hint = 'name ' + hint;

            results.pii_containers.push({
                container_type: type,
                bbox: vis.clippedBbox || bbox,
                visible: vis.visible,
                clipped: vis.clipped,
                contains_actual_pii: containedPII.length > 0,
                pii_keys: containedPII,
                semantic_hint: hint
            });
        });
    });

    // Find card-like containers (divs with border/shadow that contain text)
    document.querySelectorAll('div, section, article').forEach(el => {
        if (isInFooter(el)) return;

        const style = window.getComputedStyle(el);
        const hasBorder = style.borderWidth !== '0px' && style.borderStyle !== 'none';
        const hasShadow = style.boxShadow !== 'none';
        const hasBg = style.backgroundColor !== 'rgba(0, 0, 0, 0)' && style.backgroundColor !== 'transparent';

        if (!hasBorder && !hasShadow && !hasBg) return;

        const bbox = getBBox(el);
        if (bbox.width < 50 || bbox.height < 30) return;
        if (bbox.height > 600 || bbox.width > 800) return; // Too large

        // Check text content
        const text = el.innerText || '';
        if (text.length < 10 || text.length > 2000) return;

        const vis = checkVisibility(bbox, el);

        // Check for PII
        const containedPII = [];
        piiKeys.forEach(key => {
            const val = expandedData[key];
            if (val && text.includes(val)) {
                containedPII.push(key);
            }
        });

        // Determine container type
        let type = 'card';
        const hasAvatar = el.querySelector('img[class*="avatar"], img[class*="profile"], img[alt*="avatar"]');
        if (hasAvatar) type = 'profile_section';

        const hasLabel = el.querySelector('label');
        const hasInput = el.querySelector('input, textarea');
        if (hasLabel && hasInput) type = 'form_group';

        // Generate hint
        let hint = type.replace('_', ' ');
        const textLower = text.toLowerCase();
        if (hasAvatar || textLower.includes('profile')) hint = 'user profile ' + hint;
        else if (textLower.includes('account')) hint = 'account info ' + hint;
        else if (textLower.includes('order')) hint = 'order details ' + hint;
        else if (textLower.includes('payment') || textLower.includes('card')) hint = 'payment ' + hint;

        results.pii_containers.push({
            container_type: type,
            bbox: vis.clippedBbox || bbox,
            visible: vis.visible,
            clipped: vis.clipped,
            contains_actual_pii: containedPII.length > 0,
            pii_keys: containedPII,
            semantic_hint: hint
        });
    });

    // Deduplicate containers (remove overlapping smaller ones)
    results.pii_containers = results.pii_containers.filter((c, i, arr) => {
        return !arr.some((other, j) => {
            if (i === j) return false;
            // Check if c is inside other
            return c.bbox.x >= other.bbox.x &&
                   c.bbox.y >= other.bbox.y &&
                   c.bbox.x + c.bbox.width <= other.bbox.x + other.bbox.width &&
                   c.bbox.y + c.bbox.height <= other.bbox.y + other.bbox.height &&
                   (other.bbox.width * other.bbox.height) > (c.bbox.width * c.bbox.height) * 1.5;
        });
    });

    // Detect partial fill elements (inputs, textareas, selects)
    const partialElements = document.querySelectorAll('input[data-partial="true"], textarea[data-partial="true"], select[data-partial="true"]');
    results.partial_fill_info = {
        enabled: partialElements.length > 0,
        fields: Array.from(partialElements).map(el => ({
            key: el.getAttribute('data-pii') || el.getAttribute('data-product'),
            fill_status: el.getAttribute('data-fill-status') || 'partial',
            char_count: parseInt(el.getAttribute('data-partial-char-count') || '0') || null,
            displayed_value: el.value,
            element_type: el.tagName.toLowerCase() === 'select' ? 'select' : 'input'
        }))
    };

    return results;
}
"""


def detect_elements(page: Page, data: dict, required_fields: list[str] = None, debug: bool = False, full_page: bool = False) -> dict:
    """Detect PII, product, and container elements on the page.

    Args:
        page: Playwright page object
        data: Data dictionary with PII and product values
        required_fields: List of field names actually used in App.jsx (from requires.json)
                        If None, searches for all fields in data
        debug: If True, print debug information about detection
        full_page: If True, mark all elements as visible (for full-page screenshots)
    """
    # Sanitize data - escape problematic characters in values
    clean_data = {}
    for k, v in data.items():
        if isinstance(v, str):
            # Keep the value but ensure it's safe for JS
            clean_data[k] = v
        else:
            clean_data[k] = v

    # Inject data using evaluate with argument (safer than string interpolation)
    page.evaluate("(data) => { window.__INJECTED_DATA__ = data; }", clean_data)

    # Run detection with required fields filter (pass as argument, not string)
    fields_to_search = required_fields if required_fields else []

    # Debug: Count data-pii elements
    if debug:
        pii_attr_count = page.evaluate("""() => {
            return {
                pii_city: document.querySelectorAll('[data-pii="PII_CITY"]').length,
                all_pii: document.querySelectorAll('[data-pii]').length
            };
        }""")
        print(f"    DEBUG: Found {pii_attr_count['pii_city']} elements with data-pii='PII_CITY'")
        print(f"    DEBUG: Found {pii_attr_count['all_pii']} total elements with data-pii")

    if debug:
        # Debug: Check what's actually on the page
        page_debug = page.evaluate("""() => {
            const body = document.body;
            const allText = body.innerText || '';
            const allImgSrcs = Array.from(document.querySelectorAll('img')).map(img => img.src).slice(0, 20);
            return {
                textLength: allText.length,
                textSample: allText.slice(0, 500),
                imgCount: document.querySelectorAll('img').length,
                imgSrcs: allImgSrcs,
            };
        }""")
        print(f"    DEBUG: Page text length: {page_debug['textLength']}")
        print(f"    DEBUG: Text sample: {page_debug['textSample'][:200]}...")
        print(f"    DEBUG: Image count: {page_debug['imgCount']}")
        if page_debug['imgSrcs']:
            print(f"    DEBUG: First img src: {page_debug['imgSrcs'][0][:100]}")

        # Show what we're searching for
        print(f"    DEBUG: Searching for {len(fields_to_search)} fields:")
        for field in fields_to_search[:5]:
            val = clean_data.get(field, 'N/A')
            if isinstance(val, str):
                print(f"      {field}: '{val[:50]}...' " if len(val) > 50 else f"      {field}: '{val}'")

    results = page.evaluate(f"({DETECTION_SCRIPT})", {"requiredFields": fields_to_search, "debug": debug, "fullPage": full_page})

    # Print debug info if available
    if debug and results.get('debug_info'):
        print(f"    DEBUG: Detection debug info ({len(results['debug_info'])} entries):")
        for info in results['debug_info']:
            if info['type'] == 'PII_CITY_DETECTION':
                print(f"      PII_CITY element: {info['tagName']}")
                print(f"        Value: '{info['value']}'")
                print(f"        BBox: x={info['bbox']['x']}, y={info['bbox']['y']}, w={info['bbox']['width']}, h={info['bbox']['height']}")
                print(f"        InFooter={info['inFooter']}, TooSmall={info['tooSmall']}, Duplicate={info['isDuplicate']}")
                print(f"        WillSkip: {info['willSkip']}")
            elif info['type'] == 'MODAL_CANDIDATES':
                print(f"      Modal candidates found: {info['count']} for element {info['elKey']}")
            elif info['type'] == 'MODAL_OVERLAP':
                print(f"      Modal overlap: element={info['elKey']}, zIndex={info['zIndex']}, size={info['candSize']}")
            elif info['type'] == 'MODAL_FULLVIEWPORT':
                print(f"      Modal full-viewport detected: element={info['elKey']}, children={info['childCount']}")
            elif info['type'] == 'MODAL_CHILD':
                print(f"      Modal child: element={info['elKey']}, size={info['childSize']}, overlaps={info['overlaps']}")
            elif info['type'] == 'MODAL_FOUND':
                print(f"      **MODAL FOUND** for element={info['elKey']}, size={info['modalSize']}")

    # Deduplicate: remove overlapping boxes (parent containers that also match)
    # But keep separate occurrences of the same field at different locations
    def dedupe_elements(elements):
        """Remove overlapping boxes, keeping the smallest. Keep non-overlapping boxes."""
        if not elements:
            return elements

        def boxes_overlap(b1, b2, threshold=0.7):
            """Check if two boxes significantly overlap (one contains the other)."""
            # Calculate intersection
            x1 = max(b1["x"], b2["x"])
            y1 = max(b1["y"], b2["y"])
            x2 = min(b1["x"] + b1["width"], b2["x"] + b2["width"])
            y2 = min(b1["y"] + b1["height"], b2["y"] + b2["height"])

            if x1 >= x2 or y1 >= y2:
                return False  # No intersection

            intersection = (x2 - x1) * (y2 - y1)
            area1 = b1["width"] * b1["height"]
            area2 = b2["width"] * b2["height"]
            smaller_area = min(area1, area2)

            # If intersection covers most of the smaller box, they overlap
            return intersection > smaller_area * threshold

        # Group by key first
        by_key = {}
        for el in elements:
            key = el.get("key", "")
            if key not in by_key:
                by_key[key] = []
            by_key[key].append(el)

        deduped = []
        for key, els in by_key.items():
            # For each key, remove overlapping boxes (keep smallest)
            kept = []
            # Sort by area (smallest first)
            sorted_els = sorted(els, key=lambda e: e["bbox"]["width"] * e["bbox"]["height"])

            for el in sorted_els:
                # Check if this box overlaps with any already kept box
                dominated = False
                for kept_el in kept:
                    if boxes_overlap(el["bbox"], kept_el["bbox"]):
                        # Check if values match or one contains the other
                        el_value = str(el.get("value", "")).strip().lower()
                        kept_value = str(kept_el.get("value", "")).strip().lower()

                        # Normalize whitespace for comparison
                        el_normalized = ' '.join(el_value.split())
                        kept_normalized = ' '.join(kept_value.split())

                        # Consider dominated if:
                        # 1. Values are the same
                        # 2. One value contains the other (parent container with concatenated text)
                        if (el_normalized == kept_normalized or
                            kept_normalized in el_normalized or
                            el_normalized in kept_normalized):
                            dominated = True
                            break  # Found domination, no need to check other kept elements

                if not dominated:
                    kept.append(el)

            deduped.extend(kept)

        return deduped

    results["pii_elements"] = dedupe_elements(results.get("pii_elements", []))
    results["product_elements"] = dedupe_elements(results.get("product_elements", []))
    results["search_elements"] = dedupe_elements(results.get("search_elements", []))

    return results


def stitch_images_grid(image_paths: list[Path], output_path: Path, max_cols: int = 3, crop_to_viewport: tuple[int, int] = None):
    """Stitch multiple images into a grid layout.

    Args:
        image_paths: List of image paths to stitch
        output_path: Where to save the stitched image
        max_cols: Maximum number of columns in the grid
        crop_to_viewport: Optional (width, height) to crop images to (for full-page screenshots)
    """
    if not image_paths:
        return

    # Load all images
    images = [Image.open(p) for p in image_paths]

    # Crop to viewport if specified (for full-page screenshots, take scroll-top portion)
    if crop_to_viewport:
        crop_w, crop_h = crop_to_viewport
        cropped_images = []
        for img in images:
            if img.width > crop_w or img.height > crop_h:
                # Crop from top-left (scroll-top view)
                img = img.crop((0, 0, min(img.width, crop_w), min(img.height, crop_h)))
            cropped_images.append(img)
        images = cropped_images

    # Calculate grid dimensions
    num_images = len(images)
    num_cols = min(max_cols, num_images)
    num_rows = (num_images + num_cols - 1) // num_cols

    # Use first image size as reference (all should be same size after cropping)
    img_width, img_height = images[0].size

    # Create blank canvas
    canvas_width = img_width * num_cols
    canvas_height = img_height * num_rows
    canvas = Image.new('RGB', (canvas_width, canvas_height), 'white')

    # Paste images into grid
    for idx, img in enumerate(images):
        row = idx // num_cols
        col = idx % num_cols
        x = col * img_width
        y = row * img_height
        canvas.paste(img, (x, y))

    # Draw index numbers in upper right corner of each image
    draw = ImageDraw.Draw(canvas)
    for idx, path in enumerate(image_paths):
        row = idx // num_cols
        col = idx % num_cols
        x = col * img_width
        y = row * img_height

        # Get index from filename (e.g., "0004.png" -> 4)
        index_str = path.stem  # Gets filename without extension
        label = f"#{index_str}"

        # Position in upper right with padding
        padding = 10
        text_x = x + img_width - padding
        text_y = y + padding

        # Draw background rectangle for readability
        bbox = draw.textbbox((0, 0), label)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        rect_x1 = text_x - text_width - 5
        rect_y1 = text_y - 2
        rect_x2 = text_x + 5
        rect_y2 = text_y + text_height + 4
        draw.rectangle([rect_x1, rect_y1, rect_x2, rect_y2], fill=(0, 0, 0, 180))

        # Draw text (right-aligned)
        draw.text((text_x - text_width, text_y), label, fill=(255, 255, 255))

    canvas.save(output_path)
    print(f"  Stitched {num_images} images into {num_rows}x{num_cols} grid: {output_path}")


def is_solid_color_region(img: Image.Image, x: int, y: int, w: int, h: int, tolerance: float = 0.98) -> bool:
    """Check if a region in the image is a solid color (or nearly solid).

    Args:
        img: PIL Image to check
        x, y, w, h: Bounding box coordinates
        tolerance: Fraction of pixels that must match the dominant color (default 98%)

    Returns:
        True if the region is essentially one solid color
    """
    # Clamp coordinates to image bounds
    img_w, img_h = img.size
    x = max(0, min(x, img_w - 1))
    y = max(0, min(y, img_h - 1))
    x2 = max(0, min(x + w, img_w))
    y2 = max(0, min(y + h, img_h))

    if x2 <= x or y2 <= y:
        return False  # Invalid region

    # Crop the region
    region = img.crop((x, y, x2, y2))

    # Convert to RGB if necessary
    if region.mode != 'RGB':
        region = region.convert('RGB')

    # Get all pixels
    pixels = list(region.getdata())
    if not pixels:
        return False

    total_pixels = len(pixels)

    # Count occurrences of each color (with small tolerance for compression artifacts)
    # Group similar colors together (within 8 units per channel)
    def quantize_color(c):
        return (c[0] // 8 * 8, c[1] // 8 * 8, c[2] // 8 * 8)

    quantized = [quantize_color(p) for p in pixels]
    color_counts = Counter(quantized)

    # Get the most common color
    most_common_color, most_common_count = color_counts.most_common(1)[0]

    # Check if dominant color covers enough of the region
    ratio = most_common_count / total_pixels
    return ratio >= tolerance


def draw_bounding_boxes(image_path: Path, detection_results: dict, debug: bool = False) -> int:
    """Draw bounding boxes on the screenshot image.

    Returns the number of boxes drawn.
    """
    img = Image.open(image_path)
    draw = ImageDraw.Draw(img)
    boxes_drawn = 0

    # Colors for different element types
    COLORS = {
        'pii': (255, 0, 0),        # Red for PII text elements (not fillable)
        'product': (255, 105, 180), # Pink for products
        'search': (0, 191, 255),   # Deep sky blue for search fields
        'container': (0, 200, 0),   # Green for containers
        'partial': (255, 140, 0),   # Orange for partially filled inputs
        'full': (0, 100, 255),     # Blue for full-filled inputs (in a group, before stopAt)
        'empty': (128, 128, 128),  # Gray for empty inputs (in a group, after stopAt)
        'fillable': (138, 43, 226), # Purple for fillable inputs (not in partial fill mode)
    }

    # Get partial fill fields info for coloring
    partial_fill_info = detection_results.get("partial_fill_info", {})
    # Map field keys to their fill status
    fill_status_map = {f["key"]: f.get("fill_status", "partial")
                       for f in partial_fill_info.get("fields", []) if f.get("key")}
    partial_fill_keys = set(fill_status_map.keys())

    if debug:
        pii_count = len(detection_results.get("pii_elements", []))
        product_count = len(detection_results.get("product_elements", []))
        search_count = len(detection_results.get("search_elements", []))
        container_count = len(detection_results.get("pii_containers", []))
        print(f"  Detection results: {pii_count} PII, {product_count} products, {search_count} search, {container_count} containers")

    # Draw PII containers first (so they're behind the actual elements)
    for container in detection_results.get("pii_containers", []):
        if not container.get("visible", False):
            continue
        bbox = container["bbox"]
        x, y, w, h = int(bbox["x"]), int(bbox["y"]), int(bbox["width"]), int(bbox["height"])
        color = COLORS['container']
        draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
        boxes_drawn += 1

    # Draw product elements (skip solid color regions - likely placeholders/failed loads)
    solid_color_skipped = 0
    for elem in detection_results.get("product_elements", []):
        if not elem.get("visible", False):
            continue
        bbox = elem["bbox"]
        x, y, w, h = int(bbox["x"]), int(bbox["y"]), int(bbox["width"]), int(bbox["height"])

        # Skip product elements that are solid color regions (placeholders/failed image loads)
        # Only check for product-specific fields (NAME, IMAGE, DESC, PRICE, etc.)
        elem_key = elem.get("key", "")
        is_product_detail = any(k in elem_key for k in ["_NAME", "_IMAGE", "_DESC", "_PRICE", "_BRAND", "_RATING"])
        if is_product_detail and is_solid_color_region(img, x, y, w, h, tolerance=0.98):
            solid_color_skipped += 1
            if debug:
                print(f"    Skipped solid color region: {elem_key} at ({x},{y}) {w}x{h}")
            continue

        color = COLORS['product']
        draw.rectangle([x, y, x + w, y + h], outline=color, width=3)
        # Add label (shorten prefixes for cleaner display)
        label = elem.get("key", "PRODUCT")
        if label.startswith("PRODUCT"):
            label = label.replace("PRODUCT", "P", 1)  # PRODUCT1_NAME -> P1_NAME
        elif label.startswith("ORDER_"):
            label = label.replace("ORDER_", "O_", 1)  # ORDER_SHIPPING_DATE -> O_SHIPPING_DATE
        elif label.startswith("CART_"):
            label = label.replace("CART_", "O_", 1)  # CART_TOTAL -> O_TOTAL (treat as ORDER)
        draw.text((x + 2, max(0, y - 12)), label, fill=color)
        boxes_drawn += 1

    if solid_color_skipped > 0 and debug:
        print(f"    Skipped {solid_color_skipped} solid color product regions")

    # Draw search elements
    for elem in detection_results.get("search_elements", []):
        if not elem.get("visible", False):
            continue
        bbox = elem["bbox"]
        x, y, w, h = int(bbox["x"]), int(bbox["y"]), int(bbox["width"]), int(bbox["height"])

        color = COLORS['search']
        draw.rectangle([x, y, x + w, y + h], outline=color, width=3)
        # Add label
        label = elem.get("key", "SEARCH")
        if label.startswith("SEARCH_"):
            label = label.replace("SEARCH_", "S_", 1)
        draw.text((x + 2, max(0, y - 12)), label, fill=color)
        boxes_drawn += 1

    # Draw PII elements on top (most important)
    for elem in detection_results.get("pii_elements", []):
        if not elem.get("visible", False):
            continue
        bbox = elem["bbox"]
        x, y, w, h = int(bbox["x"]), int(bbox["y"]), int(bbox["width"]), int(bbox["height"])

        elem_key = elem.get("key", "")
        elem_type = elem.get("element_type", "text")
        is_input = elem_type == "input"  # input/textarea elements are fillable
        # Only apply fill_status to inputs - text elements just display values, not fill state
        fill_status = fill_status_map.get(elem_key) if is_input else None

        # Color logic:
        # - Blue: full (in a group, before stopAt)
        # - Orange: partial (at stopAt, or standalone partial)
        # - Gray: empty (in a group, after stopAt)
        # - Purple: fillable input but not in partial fill mode
        # - Red: text/image elements (not fillable)
        if fill_status == 'full':
            color = COLORS['full']
            prefix = "[F] "  # Full
        elif fill_status == 'partial':
            color = COLORS['partial']
            prefix = "[P] "  # Partial
        elif fill_status == 'empty':
            color = COLORS['empty']
            prefix = "[E] "  # Empty
        elif is_input:
            # Check if the input has a value - empty inputs should be gray
            elem_value = elem.get("value", "")
            if not elem_value or elem_value.strip() == "":
                color = COLORS['empty']
                prefix = "[E] "  # Empty input
            else:
                color = COLORS['fillable']
                prefix = "[I] "  # Filled input (fillable)
        else:
            color = COLORS['pii']
            prefix = ""

        draw.rectangle([x, y, x + w, y + h], outline=color, width=3)
        # Add label (shorten prefixes for cleaner display)
        label = elem_key or "PII"
        if label.startswith("PII_"):
            label = label.replace("PII_", "", 1)
        elif label.startswith("ORDER_"):
            label = label.replace("ORDER_", "O_", 1)
        elif label.startswith("CART_"):
            label = label.replace("CART_", "O_", 1)  # Treat CART_ as ORDER_
        label = f"{prefix}{label}"
        draw.text((x + 2, max(0, y - 12)), label, fill=color)
        boxes_drawn += 1

        if debug:
            status = f" [{fill_status.upper()}]" if fill_status else (" [INPUT]" if is_input else "")
            print(f"    Drew PII box: {elem_key} at ({x},{y}) {w}x{h}{status}")

    img.save(image_path)
    return boxes_drawn


def take_screenshot_with_annotations(
    page_info: dict,
    data: dict,
    scroll_y: int,
    output_dir: Path,
    index: int,
    port: int = 5173,
    debug: bool = False,
    full_page: bool = False,
    partial_fill: bool = False
) -> dict:
    """Take a screenshot and generate annotations.

    Args:
        partial_fill: If True, generate PARTIAL_FILL_CONFIG to partially fill inputs
    """

    page_dir = page_info["path"]
    server_proc = None
    required_fields = page_info.get("required_fields", [])

    try:
        # Write data.json to page's src/ directory and clear Vite cache
        # No locking needed since each page has its own src/data.json
        inject_data_json(page_dir, data, partial_fill=partial_fill)

        # Start dev server on specified port (may use different port if busy)
        server_proc, actual_port = start_dev_server(page_dir, port)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})

            # Navigate to the server on the actual port (with retry)
            retry_goto(page, f"http://localhost:{actual_port}", max_retries=3, wait_until="networkidle")

            # Wait for React to render content (with retries if blank)
            wait_for_react_render(page, f"http://localhost:{actual_port}")

            # Get page height and scroll
            page_height = get_page_height(page)
            max_scroll = max(0, page_height - VIEWPORT_HEIGHT)

            if scroll_y < 0:  # Random scroll
                scroll_y = random.randint(0, max_scroll) if max_scroll > 0 else 0

            # For full-page screenshots, always detect elements at scroll=0
            # so viewport-relative coordinates equal document-relative coordinates
            if full_page:
                scroll_to(page, 0)
            else:
                scroll_to(page, scroll_y)

            # Detect elements - use required_fields from requires.json for accurate detection
            detection_results = detect_elements(page, data, required_fields, debug=debug, full_page=full_page)

            # Take screenshot
            screenshot_name = f"{index:04d}.png"
            screenshot_path = output_dir / screenshot_name
            page.screenshot(path=str(screenshot_path), full_page=full_page)

            browser.close()

        # Draw bounding boxes on the screenshot
        draw_bounding_boxes(screenshot_path, detection_results)

        # Compute fillable fields info (PII input elements that could be partially filled)
        pii_elements = detection_results.get("pii_elements", [])
        fillable_elements = [e for e in pii_elements if e.get("element_type") == "input"]
        fillable_keys = list(set(e.get("key") for e in fillable_elements if e.get("key")))

        # Build annotation
        annotation = {
            "image_path": str(screenshot_path.relative_to(SCRIPT_DIR)),
            "data_json": {k: v for k, v in data.items() if not k.startswith("_")},
            "company": page_info["company"],
            "page_type": page_info["page_type"],
            "device": page_info["device"],
            "scroll_y": scroll_y,
            "page_height": page_height,
            "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            "required_fields": required_fields,  # Fields actually used in App.jsx
            # Paths and status from reproduction.log
            "source_image": page_info.get("source_image"),
            "output_dir": page_info.get("output_dir"),
            "reproduction_status": page_info.get("status", "unknown"),  # completed, in_progress, incomplete
            "pii_elements": pii_elements,
            "product_elements": detection_results.get("product_elements", []),
            "search_elements": detection_results.get("search_elements", []),
            "pii_containers": detection_results.get("pii_containers", []),
            "fillable_fields_info": {
                "fillable_keys": fillable_keys,
                "count": len(fillable_elements),
            },
            "timestamp": datetime.now().isoformat(),
            "_meta": data.get("_meta", {})
        }

        # Save annotation
        annotation_path = output_dir / f"{index:04d}.json"
        with open(annotation_path, "w") as f:
            json.dump(annotation, f, indent=2)

        return annotation

    finally:
        if server_proc:
            kill_process_tree(server_proc)
            time.sleep(0.5)


def process_page_task(task: dict) -> list[dict]:
    """Process all variants and scroll positions for a single page.

    This runs in a separate process. Each page gets its own port.
    Variants are processed sequentially (server restart needed for each).
    Multiple scroll positions per variant are taken without restart.
    """
    page_info = task["page_info"]
    page_info["path"] = Path(page_info["path"])
    variants = task["variants"]
    scrolls_per_variant = task["scrolls_per_variant"]
    scroll_top = task["scroll_top"]
    output_dir = Path(task["output_dir"])
    start_index = task["start_index"]
    port = task["port"]
    debug = task.get("debug", False)
    full_page = task.get("full_page", True)
    partial_fill = task.get("partial_fill", False)

    # Get required fields from page's requires.json for accurate detection
    required_fields = page_info.get("required_fields", [])

    results = []
    screenshot_idx = start_index

    for variant_idx, data in enumerate(variants):
        server_proc = None

        try:
            # Write data.json for this variant (full fill first, partial later)
            inject_data_json(page_info["path"], data, partial_fill=False)

            # Start dev server (may use different port if requested one is busy)
            server_proc, actual_port = start_dev_server(page_info["path"], port)

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})

                # Navigate (with retry)
                retry_goto(page, f"http://localhost:{actual_port}", max_retries=3, wait_until="networkidle")

                # Wait for React to render content (with retries if blank)
                wait_for_react_render(page, f"http://localhost:{actual_port}")

                # Get page height for scroll calculations
                page_height = get_page_height(page)
                max_scroll = max(0, page_height - VIEWPORT_HEIGHT)

                # Take screenshots at different scroll positions
                for scroll_idx in range(scrolls_per_variant):
                    if scroll_top:
                        scroll_y = 0
                    elif scrolls_per_variant == 1:
                        scroll_y = random.randint(0, max_scroll) if max_scroll > 0 else 0
                    else:
                        # Distribute scroll positions evenly
                        scroll_y = int(max_scroll * scroll_idx / max(1, scrolls_per_variant - 1)) if max_scroll > 0 else 0

                    # For full-page screenshots, always detect at scroll=0
                    # so viewport-relative coordinates equal document-relative coordinates
                    if full_page:
                        scroll_to(page, 0)
                    else:
                        scroll_to(page, scroll_y)

                    # Detect elements - use required_fields for accurate detection
                    detection_results = detect_elements(page, data, required_fields, debug=debug, full_page=full_page)

                    # Take FULL screenshot (all fields filled)
                    screenshot_path = output_dir / f"{screenshot_idx:04d}.png"
                    page.screenshot(path=str(screenshot_path), full_page=full_page)

                    # For first variant & scroll, also save to page's output dir
                    page_output_dir = page_info["path"]
                    is_first_screenshot = (variant_idx == 0 and scroll_idx == 0)

                    if is_first_screenshot:
                        # Save final.png BEFORE drawing boxes (clean screenshot)
                        final_path = page_output_dir / "final.png"
                        shutil.copy(screenshot_path, final_path)

                    # Draw bounding boxes on full screenshot
                    boxes = draw_bounding_boxes(screenshot_path, detection_results, debug=debug)

                    if is_first_screenshot:
                        # Save annotated.png AFTER drawing boxes
                        annotated_path = page_output_dir / "annotated.png"
                        shutil.copy(screenshot_path, annotated_path)
                    if debug:
                        print(f"    [{screenshot_idx}] Drew {boxes} boxes (required: {len(required_fields)} fields)")

                    # Take PARTIAL screenshot if partial_fill is enabled
                    partial_detection_results = None
                    if partial_fill:
                        # Re-inject data with partial fill config
                        inject_data_json(page_info["path"], data, partial_fill=True)

                        # Reload page to pick up new data
                        page.reload(wait_until="networkidle", timeout=45000)
                        wait_for_react_render(page, f"http://localhost:{actual_port}")

                        # Re-scroll to same position
                        if full_page:
                            scroll_to(page, 0)
                        else:
                            scroll_to(page, scroll_y)

                        # Detect elements with partial fill
                        partial_detection_results = detect_elements(page, data, required_fields, debug=debug, full_page=full_page)

                        # Take partial screenshot
                        partial_screenshot_path = output_dir / f"{screenshot_idx:04d}_partial.png"
                        page.screenshot(path=str(partial_screenshot_path), full_page=full_page)

                        # Draw bounding boxes on partial screenshot
                        partial_boxes = draw_bounding_boxes(partial_screenshot_path, partial_detection_results, debug=debug)
                        if debug:
                            print(f"    [{screenshot_idx}] Partial: drew {partial_boxes} boxes")

                        if is_first_screenshot:
                            # Save annotated_partial.png to page's output dir
                            annotated_partial_path = page_output_dir / "annotated_partial.png"
                            shutil.copy(partial_screenshot_path, annotated_partial_path)

                        # Re-inject full data for next scroll position
                        inject_data_json(page_info["path"], data, partial_fill=False)
                        page.reload(wait_until="networkidle", timeout=45000)
                        wait_for_react_render(page, f"http://localhost:{actual_port}")

                    # Build annotation
                    pii_elements = detection_results.get("pii_elements", [])
                    product_elements = detection_results.get("product_elements", [])
                    search_elements = detection_results.get("search_elements", [])
                    pii_containers = detection_results.get("pii_containers", [])

                    # Compute fillable fields info (PII input elements that could be partially filled)
                    fillable_elements = [e for e in pii_elements if e.get("element_type") == "input"]
                    fillable_keys = list(set(e.get("key") for e in fillable_elements if e.get("key")))

                    annotation = {
                        "image_path": str(screenshot_path.relative_to(SCRIPT_DIR)),
                        "data_json": {k: v for k, v in data.items() if not k.startswith("_")},
                        "company": page_info["company"],
                        "page_type": page_info["page_type"],
                        "device": page_info["device"],
                        "scroll_y": scroll_y,
                        "page_height": page_height,
                        "viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
                        "required_fields": required_fields,  # Fields actually used in App.jsx
                        # Paths and status from reproduction.log
                        "source_image": page_info.get("source_image"),
                        "output_dir": page_info.get("output_dir"),
                        "reproduction_status": page_info.get("status", "unknown"),  # completed, in_progress, incomplete
                        "detection_stats": {
                            "pii_found": len(pii_elements),
                            "pii_visible": len([e for e in pii_elements if e.get("visible")]),
                            "pii_fillable": len(fillable_elements),
                            "products_found": len(product_elements),
                            "products_visible": len([e for e in product_elements if e.get("visible")]),
                            "search_found": len(search_elements),
                            "search_visible": len([e for e in search_elements if e.get("visible")]),
                            "containers_found": len(pii_containers),
                            "boxes_drawn": boxes,
                        },
                        "pii_elements": pii_elements,
                        "product_elements": product_elements,
                        "search_elements": search_elements,
                        "pii_containers": pii_containers,
                        "fillable_fields_info": {
                            "fillable_keys": fillable_keys,
                            "count": len(fillable_elements),
                        },
                        "partial_fill_info": detection_results.get("partial_fill_info", {"enabled": False, "fields": []}),
                        "timestamp": datetime.now().isoformat(),
                        "_meta": data.get("_meta", {})
                    }

                    # Add partial screenshot info if generated
                    if partial_fill and partial_detection_results:
                        partial_pii = partial_detection_results.get("pii_elements", [])
                        partial_products = partial_detection_results.get("product_elements", [])
                        partial_search = partial_detection_results.get("search_elements", [])
                        annotation["partial"] = {
                            "image_path": str(partial_screenshot_path.relative_to(SCRIPT_DIR)),
                            "partial_fill_config": partial_detection_results.get("partial_fill_info", {}),
                            "detection_stats": {
                                "pii_found": len(partial_pii),
                                "pii_visible": len([e for e in partial_pii if e.get("visible")]),
                                "products_found": len(partial_products),
                                "products_visible": len([e for e in partial_products if e.get("visible")]),
                                "search_found": len(partial_search),
                                "search_visible": len([e for e in partial_search if e.get("visible")]),
                                "boxes_drawn": partial_boxes,
                            },
                            "pii_elements": partial_pii,
                            "product_elements": partial_products,
                            "search_elements": partial_search,
                        }

                    # Save annotation
                    annotation_path = output_dir / f"{screenshot_idx:04d}.json"
                    with open(annotation_path, "w") as f:
                        json.dump(annotation, f, indent=2)

                    results.append({"success": True, "index": screenshot_idx, "annotation": annotation})
                    screenshot_idx += 1

                browser.close()

        except Exception as e:
            import traceback
            error_msg = str(e)
            tb = traceback.format_exc()
            print(f"    ERROR in {page_info.get('company', 'unknown')}: {error_msg}")
            if debug:
                print(f"    Traceback:\n{tb}")
            # Mark remaining screenshots for this variant as failed
            for _ in range(scrolls_per_variant - (screenshot_idx - start_index - variant_idx * scrolls_per_variant)):
                results.append({"success": False, "index": screenshot_idx, "error": error_msg, "traceback": tb})
                screenshot_idx += 1

        finally:
            if server_proc:
                kill_process_tree(server_proc)
                time.sleep(0.5)

    return results


def screenshot_worker(task: dict) -> dict:
    """Worker function for parallel screenshot processing.

    Args:
        task: Dict containing page_info, data, scroll_y, output_dir, index, port
              All paths should be strings for pickle serialization
    Returns:
        Dict with result status and annotation or error
    """
    try:
        # Convert string paths back to Path objects
        page_info = task["page_info"].copy()
        page_info["path"] = Path(page_info["path"])

        annotation = take_screenshot_with_annotations(
            page_info=page_info,
            data=task["data"],
            scroll_y=task["scroll_y"],
            output_dir=Path(task["output_dir"]),
            index=task["index"],
            port=task["port"]
        )
        return {"success": True, "index": task["index"], "annotation": annotation}
    except Exception as e:
        import traceback
        return {"success": False, "index": task["index"], "error": str(e), "traceback": traceback.format_exc()}


def load_data_variants(data_path: Path) -> list[dict]:
    """Load data variants from JSON or NDJSON file."""
    variants = []

    if data_path.suffix == ".ndjson":
        with open(data_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    variants.append(json.loads(line))
    else:
        with open(data_path) as f:
            data = json.load(f)
            if isinstance(data, list):
                variants = data
            else:
                variants = [data]

    return variants


def main():
    parser = argparse.ArgumentParser(description="Take screenshots of reproduced UIs with PII bounding boxes")
    parser.add_argument("--data", type=str, default="data_variants.ndjson", help="Data JSON or NDJSON file")
    parser.add_argument("--output", type=str, default="screenshots", help="Output directory")
    parser.add_argument("--num-variants", type=int, default=1, help="Number of data variants per page")
    parser.add_argument("--scrolls-per-variant", type=int, default=1, help="Scroll positions per data variant")
    parser.add_argument("--scroll-top", action="store_true", help="Always scroll to top (no random scroll)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--page-filter", type=str, default=None, help="Filter pages by company name")
    parser.add_argument("--workers", type=int, default=1, help="Number of parallel workers (now safe with per-page data.json)")
    parser.add_argument("--debug", action="store_true", help="Enable debug output for detection")
    parser.add_argument("--full-page", action="store_true", default=True, help="Take full-page screenshots (default: True)")
    parser.add_argument("--no-full-page", action="store_false", dest="full_page", help="Disable full-page screenshots (viewport only)")
    parser.add_argument("--partial-fill", action="store_true", help="Enable partial fill mode (only 1-2 input fields filled)")
    parser.add_argument("--include-incomplete", action="store_true", help="Include reproductions that didn't fully complete (status=in_progress)")
    parser.add_argument("--after", type=str, default=None, help="Only include pages with timestamp >= this value (e.g., '20260113' or '20260113_120000')")
    args = parser.parse_args()

    random.seed(args.seed)

    # Determine worker count
    num_workers = args.workers if args.workers > 0 else (os.cpu_count() or 4)
    base_port = 5173

    # Load data variants
    data_path = SCRIPT_DIR / args.data
    if not data_path.exists():
        print(f"ERROR: Data file not found: {data_path}")
        sys.exit(1)

    variants = load_data_variants(data_path)
    print(f"Loaded {len(variants)} data variants")

    # Find pages
    pages = find_latest_pages(OUTPUT_BASE, include_incomplete=args.include_incomplete, after_timestamp=args.after)
    if args.page_filter:
        pages = [p for p in pages if args.page_filter.lower() in p["company"].lower()]

    print(f"Found {len(pages)} pages to screenshot")

    # Show status breakdown if include_incomplete is True
    if args.include_incomplete:
        completed = sum(1 for p in pages if p.get("status") == "completed")
        in_progress = sum(1 for p in pages if p.get("status") == "in_progress")
        unknown = sum(1 for p in pages if p.get("status") == "unknown")
        non_completed = in_progress + unknown
        if non_completed > 0:
            parts = [f"{completed} completed"]
            if in_progress > 0:
                parts.append(f"{in_progress} in_progress")
            if unknown > 0:
                parts.append(f"{unknown} unknown")
            print(f"  Status breakdown: {', '.join(parts)}")

    # Kill any leftover processes on ports we'll use
    kill_processes_on_ports(base_port, len(pages))

    # Show required fields summary
    pages_with_requires = [p for p in pages if p.get("required_fields")]
    if pages_with_requires:
        print(f"  {len(pages_with_requires)} pages have requires.json (targeted detection)")
        sample = pages_with_requires[0]
        print(f"  Sample ({sample['company']}): {len(sample['required_fields'])} fields")

    if not pages:
        print("ERROR: No pages found in output directory")
        sys.exit(1)

    # Create output directory
    output_dir = SCRIPT_DIR / args.output
    output_dir.mkdir(parents=True, exist_ok=True)

    total_screenshots = len(pages) * args.num_variants * args.scrolls_per_variant
    print(f"\nGenerating {total_screenshots} screenshots ({len(pages)} pages × {args.num_variants} variants × {args.scrolls_per_variant} scrolls)")
    print(f"Strategy: page-level parallelization (each worker handles one page's all variants)")
    print(f"Using {num_workers} parallel workers")
    start_time = time.time()

    # Select variants to use (cycle if needed)
    selected_variants = [variants[i % len(variants)] for i in range(args.num_variants)] if variants else [{}] * args.num_variants

    # Build tasks: one task per page (each includes all variants)
    tasks = []
    screenshot_idx = 0
    for page_idx, page_info in enumerate(pages):
        port = base_port + page_idx  # Each page gets unique port

        # Convert Path objects to strings for pickle serialization
        task = {
            "page_info": {
                "path": str(page_info["path"]),
                "company": page_info["company"],
                "page_type": page_info["page_type"],
                "device": page_info["device"],
                "image_id": page_info["image_id"],
                "timestamp": page_info["timestamp"],
                "required_fields": page_info.get("required_fields", []),
                "required_pii": page_info.get("required_pii", []),
                "required_products": page_info.get("required_products", []),
                "source_image": page_info.get("source_image"),
                "output_dir": page_info.get("output_dir"),
            },
            "variants": selected_variants,
            "scrolls_per_variant": args.scrolls_per_variant,
            "scroll_top": args.scroll_top,
            "output_dir": str(output_dir),
            "start_index": screenshot_idx,
            "port": port,
            "debug": args.debug,
            "full_page": args.full_page,
            "partial_fill": args.partial_fill,
        }
        tasks.append(task)
        screenshot_idx += args.num_variants * args.scrolls_per_variant

    # Process pages in parallel
    all_results = []
    completed = 0

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        # Stagger task submission to reduce resource contention when many workers
        # start their Vite servers simultaneously
        futures = {}
        for i, task in enumerate(tasks):
            futures[executor.submit(process_page_task, task)] = task
            # Small delay between submissions when using multiple workers
            # to prevent all Vite servers starting at exact same time
            if num_workers > 1 and i < num_workers - 1:
                time.sleep(0.5)  # 500ms stagger for first batch of workers

        for future in as_completed(futures):
            task = futures[future]
            page_name = task["page_info"]["company"]
            page_path = Path(task["page_info"]["path"]).relative_to(Path.cwd())

            try:
                results = future.result()
                all_results.extend(results)

                # Count successes for this page
                successes = sum(1 for r in results if r["success"])
                completed += 1

                if args.debug:
                    print(f"  [{completed}/{len(pages)}] {page_name}: {successes}/{len(results)} screenshots | {page_path}")
                else:
                    print(f"  [{completed}/{len(pages)}] {page_name}: done | {page_path}")

            except Exception as e:
                print(f"  ERROR: {page_name} failed: {str(e)}")
                # Mark all screenshots for this page as failed
                expected_screenshots = args.num_variants * args.scrolls_per_variant
                for _ in range(expected_screenshots):
                    all_results.append({"success": False, "error": str(e)})

    # Create stitched overview of first screenshots (scroll-top viewport crops)
    if args.num_variants > 0 and args.scrolls_per_variant > 0:
        first_screenshots = []
        first_partial_screenshots = []
        for page_idx in range(len(pages)):
            # Get first screenshot for each page (variant 0, scroll 0)
            img_idx = page_idx * args.num_variants * args.scrolls_per_variant
            screenshot_path = output_dir / f"{img_idx:04d}.png"
            partial_path = output_dir / f"{img_idx:04d}_partial.png"
            if screenshot_path.exists():
                first_screenshots.append(screenshot_path)
            if partial_path.exists():
                first_partial_screenshots.append(partial_path)

        if first_screenshots:
            stitched_path = output_dir / "stitched.png"
            # Crop to viewport size for consistent stitching (full-page images get cropped to scroll-top view)
            stitch_images_grid(first_screenshots, stitched_path, max_cols=3, crop_to_viewport=(VIEWPORT_WIDTH, VIEWPORT_HEIGHT))

        if first_partial_screenshots:
            stitched_partial_path = output_dir / "stitched_partial.png"
            stitch_images_grid(first_partial_screenshots, stitched_partial_path, max_cols=3, crop_to_viewport=(VIEWPORT_WIDTH, VIEWPORT_HEIGHT))

    # Print summary
    successful = [r for r in all_results if r["success"]]
    failed = [r for r in all_results if not r["success"]]

    if failed:
        print(f"\nWarning: {len(failed)} screenshots failed")
        # Show unique errors
        unique_errors = {}
        for r in failed:
            err = r.get("error", "Unknown error")
            if err not in unique_errors:
                unique_errors[err] = {"count": 0, "traceback": r.get("traceback", "")}
            unique_errors[err]["count"] += 1

        print("Errors:")
        for err, info in unique_errors.items():
            print(f"  [{info['count']}x] {err[:200]}")
            if args.debug and info["traceback"]:
                # Show first few lines of traceback
                tb_lines = info["traceback"].strip().split("\n")[-5:]
                for line in tb_lines:
                    print(f"      {line}")

    # Aggregate detection stats
    total_pii_found = 0
    total_products_found = 0
    total_search_found = 0
    total_boxes_drawn = 0
    for r in successful:
        if "annotation" in r and "detection_stats" in r["annotation"]:
            stats = r["annotation"]["detection_stats"]
            total_pii_found += stats.get("pii_visible", 0)
            total_products_found += stats.get("products_visible", 0)
            total_search_found += stats.get("search_visible", 0)
            total_boxes_drawn += stats.get("boxes_drawn", 0)

    elapsed = time.time() - start_time
    rate = len(successful) / elapsed if elapsed > 0 else 0
    print(f"\nDone! {len(successful)}/{total_screenshots} screenshots in {elapsed:.1f}s ({rate:.1f}/sec)")
    print(f"Detection: {total_pii_found} PII + {total_products_found} products + {total_search_found} search = {total_boxes_drawn} boxes drawn")
    print(f"Output directory: {output_dir}")

    # Write manifest
    manifest_path = output_dir / "manifest.json"
    manifest = {
        "total_screenshots": len(successful),
        "failed_screenshots": len(failed),
        "pages": [{"company": p["company"], "page_type": p["page_type"], "required_fields": len(p.get("required_fields", []))} for p in pages],
        "variants_per_page": args.num_variants,
        "scrolls_per_variant": args.scrolls_per_variant,
        "scroll_top": args.scroll_top,
        "data_source": str(data_path),
        "workers": min(num_workers, len(pages)),
        "elapsed_seconds": round(elapsed, 2),
        "detection_totals": {
            "pii_visible": total_pii_found,
            "products_visible": total_products_found,
            "search_visible": total_search_found,
            "boxes_drawn": total_boxes_drawn,
        },
        "generated_at": datetime.now().isoformat()
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


if __name__ == "__main__":
    main()
