#!/usr/bin/env python3
"""
UI/UX Study Image Scraper

Downloads e-commerce checkout and account UI screenshots.
Data source: Baymard Institute (https://baymard.com) benchmark studies.
Supports multiple page types with organized folder structure.
"""

import os
import re
import time
import json
import logging
from datetime import datetime
from collections import defaultdict
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import argparse

# Page types to scrape - maps display name to URL path segment
PAGE_TYPES = {
    "cart": "/checkout-usability/benchmark/step-type/cart",
    "added-to-cart": "/ecommerce-design-examples/added-to-cart-confirmation",
    "cross-sells": "/checkout-usability/benchmark/step-type/cross-sell",
    "account-selection": "/checkout-usability/benchmark/step-type/account",
    "customer-info-address": "/checkout-usability/benchmark/step-type/shipping-address",
    "delivery-shipping": "/checkout-usability/benchmark/step-type/delivery-options",
    "address-validator": "/checkout-usability/benchmark/step-type/address-validator",
    "billing-address": "/checkout-usability/benchmark/step-type/billing-address",
    "payment": "/checkout-usability/benchmark/step-type/payment",
    "review-order": "/checkout-usability/benchmark/step-type/order-review",
    "receipt": "/checkout-usability/benchmark/step-type/receipt",
    "gifting": "/checkout-usability/benchmark/step-type/gifting",
    "store-pickup": "/checkout-usability/benchmark/step-type/store-pickup",
    "account-dashboard": "/ecommerce-design-examples/58-account-dashboard",
    "orders-overview": "/ecommerce-design-examples/62-orders-overview",
    "newsletter-management": "/ecommerce-design-examples/newsletter-management",
    "stored-credit-cards": "/ecommerce-design-examples/60-stored-credit-cards",
    "order-tracking": "/ecommerce-design-examples/63-order-tracking-page",
    "order-returns": "/ecommerce-design-examples/64-order-returns",
}


# * Added to Cart
# * Cross Sells
# * Account Selection
# * Customer Info & Address
# * Delivery & Shipping Methods
# * Address Validator
# * Billing Address
# * Payment
# * Review Order
# * Receipt / Order Confirmation
# * Gifting
# * Store Pickup
# * Account Dashboard
# * Orders Overview
# * Newsletter Management
# * Stored Credit Card
# * Order Tracking Page
# * Order Returns
# * Orders Overview


class UIScraper:
    """Scraper for Baymard Institute UI/UX benchmark screenshots."""

    BASE_URL = "https://baymard.com"

    def __init__(self, output_dir="ui_images", delay=0.3):
        self.output_dir = output_dir
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        })
        os.makedirs(output_dir, exist_ok=True)

        # State tracking
        self.visited = set()
        self.state_file = os.path.join(output_dir, ".scraper_state.json")

        # Stats tracking
        self.companies = defaultdict(int)  # company -> count
        self.device_types = defaultdict(int)  # device type -> count
        self.premium_pages = []  # URLs that require premium
        self.url_device_types = {}  # URL -> device type (from listing page)
        self.stats = {
            "downloaded": 0,
            "skipped_exists": 0,
            "skipped_premium": 0,
            "failed": 0,
        }

        # Setup logging
        self.setup_logging()

    def setup_logging(self):
        """Setup logging to both file and console."""
        log_file = os.path.join(self.output_dir, f"scraper_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

        # Create formatter
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        # File handler
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)

        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter('%(message)s'))

        # Setup logger
        self.logger = logging.getLogger('UIScraper')
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

        self.log_file = log_file

    def load_state(self):
        """Load previously visited URLs from state file."""
        if os.path.exists(self.state_file):
            with open(self.state_file, 'r') as f:
                data = json.load(f)
                self.visited = set(data.get('visited', []))
                self.logger.info(f"Loaded {len(self.visited)} previously visited URLs")
        return self.visited

    def save_state(self):
        """Save visited URLs to state file for resume capability."""
        with open(self.state_file, 'w') as f:
            json.dump({'visited': list(self.visited)}, f)

    def extract_company_name(self, url):
        """Extract company name from study URL."""
        # URL format: /19779-crate-barrel or /4355-amazon-step-1
        match = re.search(r'/\d+-([^/]+)$', url)
        if match:
            name = match.group(1)
            # Remove step suffixes (matches -step, -step-1, -step-anything, etc.)
            name = re.sub(r'-step(?:-.*)?$', '', name)
            # Clean up name
            name = name.replace('-', ' ').title()
            return name
        return "Unknown"

    def detect_device_type(self, soup, image_url=None):
        """Detect if the screenshot is from mobile, desktop, or app.

        Uses explicit text labels from page title/content only.
        Aspect ratio is unreliable since Baymard captures full-page scrolled screenshots.
        """
        # Check title tag first (most reliable)
        # Title format: "Macy's Mobile Cart – 123 of 456 Cart Examples"
        title = soup.find("title")
        title_text = title.get_text().lower() if title else ""

        if "mobile" in title_text:
            return "mobile"
        elif "desktop" in title_text:
            return "desktop"
        elif " app" in title_text:
            return "app"

        # Check meta description or first paragraph for device indicators
        page_text = soup.get_text().lower()[:2000]  # Check first 2000 chars

        # Look for explicit patterns like "2024 Q2 - Mobile" or "Mobile screenshot"
        if " - mobile" in page_text or "mobile screenshot" in page_text:
            return "mobile"
        elif " - desktop" in page_text or "desktop screenshot" in page_text:
            return "desktop"
        elif " - app" in page_text:
            return "app"

        return "unknown"

    def get_study_links_from_listing(self, page_url):
        """Get study links from a listing page, separating free and premium.

        Also extracts device type (mobile/desktop/app) from the listing text.
        Returns: list of free URLs, and populates self.url_device_types dict
        """
        self.logger.info(f"Fetching study links from: {page_url}")

        response = self.session.get(page_url)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "html.parser")

        # Find all benchmark study links
        free_links = set()
        premium_links = set()

        # Match various URL patterns for different page types
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Match URLs like /benchmark/step-type/cart/19779-crate-barrel
            # or /ecommerce-design-examples/58-account-dashboard/12345-company
            if re.search(r'/\d+-[a-z]', href):
                full_url = urljoin(self.BASE_URL, href)
                link_text = a.get_text().lower()

                # Extract device type from link text (e.g., "2024 Q2 - Mobile")
                device_type = "unknown"
                if " - mobile" in link_text:
                    device_type = "mobile"
                elif " - desktop" in link_text:
                    device_type = "desktop"
                elif " - app" in link_text:
                    device_type = "app"

                # Store device type for this URL
                self.url_device_types[full_url] = device_type

                # Check if this is a premium/locked item
                # Premium items have "lock" icon or "upgrade" text in the link
                has_lock_img = a.find("img", alt=lambda x: x and "lock" in x.lower())

                if "upgrade" in link_text or "lock" in link_text or has_lock_img:
                    premium_links.add(full_url)
                else:
                    free_links.add(full_url)

        self.logger.info(f"Found {len(free_links)} free + {len(premium_links)} premium study links")

        # Track premium links for reporting
        for url in premium_links:
            if url not in self.premium_pages:
                self.premium_pages.append(url)

        return list(free_links)

    def get_navigation_links(self, soup, page_type_path):
        """Extract prev/next navigation links from a study page."""
        prev_link = None
        next_link = None

        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Check if this link is for the same page type
            if page_type_path in href or re.search(r'/\d+-[a-z]', href):
                img = a.find("img")
                if img:
                    alt = img.get("alt", "")
                    if "chevron-left" in alt or "left" in alt.lower():
                        prev_link = urljoin(self.BASE_URL, href)
                    elif "chevron-right" in alt or "right" in alt.lower():
                        next_link = urljoin(self.BASE_URL, href)

        return prev_link, next_link

    def is_premium_page(self, soup):
        """Check if the page requires premium access."""
        page_text = soup.get_text().lower()

        # Check for premium/upgrade indicators
        premium_indicators = [
            "upgrade to access",
            "premium access required",
            "subscribe to view",
            "upgrade your account",
            "available with premium",
            "unlock this content",
            "sign up for premium",
        ]

        for indicator in premium_indicators:
            if indicator in page_text:
                return True

        # Check for upgrade buttons/links
        for a in soup.find_all(['a', 'button']):
            text = a.get_text().lower()
            if 'upgrade' in text or 'subscribe' in text or 'premium' in text:
                # Make sure it's a prominent CTA, not just a nav link
                classes = a.get('class', [])
                if isinstance(classes, list):
                    classes = ' '.join(classes)
                if 'btn' in classes or 'button' in classes or 'cta' in classes:
                    return True

        return False

    def get_image_and_nav_from_study(self, study_url, page_type_path):
        """Extract the main screenshot image URL, navigation links, and device type from a study page."""
        try:
            time.sleep(self.delay)
            response = self.session.get(study_url, allow_redirects=True)
            response.raise_for_status()

            # Check if we were redirected (premium content redirects to listing page)
            final_url = response.url
            if final_url != study_url:
                # We were redirected - this is premium content
                return None, None, None, None, True

            soup = BeautifulSoup(response.text, "html.parser")

            # Check if this is a premium page (modal or text indicators)
            if self.is_premium_page(soup):
                return None, None, None, None, True  # is_premium = True

            # Get navigation links
            prev_link, next_link = self.get_navigation_links(soup, page_type_path)

            # Look for the main screenshot image (hosted on imgix)
            # Must be a large image (not a thumbnail) - check for width parameter
            image_url = None
            for img in soup.find_all("img"):
                src = img.get("src", "") or img.get("data-src", "")
                if "baymard-assets.imgix.net" in src and "screenshot" in src:
                    # Skip small thumbnails - real screenshots have larger width params
                    # Thumbnails typically have w=150 or similar, full images have w=375+
                    if "w=" in src:
                        # Extract width parameter
                        width_match = re.search(r'w=(\d+)', src)
                        if width_match:
                            width = int(width_match.group(1))
                            if width < 300:  # Skip thumbnails
                                continue
                    image_url = src
                    break

            # If no valid image found, might be premium
            if not image_url:
                return None, None, None, None, False

            # Detect device type (mobile/desktop/app)
            device_type = self.detect_device_type(soup, image_url)

            return image_url, prev_link, next_link, device_type, False

        except Exception as e:
            self.logger.error(f"Error fetching study {study_url}: {e}")
            return None, None, None, None, False

    def download_image(self, image_url, study_url, page_type_dir, device_type="unknown"):
        """Download an image and save it to the appropriate directory."""
        try:
            # Extract study ID and name from study URL
            match = re.search(r'/(\d+-[^/]+)$', study_url)
            if match:
                base_name = match.group(1)
            else:
                base_name = urlparse(image_url).path.split("/")[-1]

            # Include device type in filename
            # Format: 19928-macys-mobile.png or 19927-macys-desktop.png
            filename = f"{base_name}-{device_type}"

            # Determine extension
            ext = ".png"
            if ".jpg" in image_url.lower() or ".jpeg" in image_url.lower():
                ext = ".jpg"

            filename = f"{filename}{ext}"
            filepath = os.path.join(page_type_dir, filename)

            # Skip if already downloaded
            if os.path.exists(filepath):
                return filepath, True  # exists

            response = self.session.get(image_url, stream=True)
            response.raise_for_status()

            with open(filepath, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            return filepath, False  # newly downloaded

        except Exception as e:
            self.logger.error(f"Error downloading {image_url}: {e}")
            return None, False

    def crawl_page_type(self, page_type_name, page_type_path, limit=None):
        """Crawl all studies for a specific page type."""
        full_url = f"{self.BASE_URL}{page_type_path}"

        # Create directory for this page type
        page_type_dir = os.path.join(self.output_dir, page_type_name)
        os.makedirs(page_type_dir, exist_ok=True)

        self.logger.info(f"\n{'='*60}")
        self.logger.info(f"Scraping page type: {page_type_name}")
        self.logger.info(f"URL: {full_url}")
        self.logger.info(f"Output: {page_type_dir}")
        self.logger.info(f"{'='*60}")

        # Get initial seeds from listing page
        try:
            seeds = self.get_study_links_from_listing(full_url)
        except Exception as e:
            self.logger.error(f"Failed to fetch listing page: {e}")
            return 0, 0, 0, 0

        to_visit = set(seeds)
        visited_this_type = set()

        downloaded = 0
        skipped = 0
        premium = 0
        failed = 0
        count = 0

        self.logger.info(f"Starting crawl from {len(to_visit)} seed URLs...")

        while to_visit:
            if limit and count >= limit:
                self.logger.info(f"Reached limit of {limit} studies for {page_type_name}")
                break

            current_url = to_visit.pop()

            if current_url in visited_this_type:
                continue

            visited_this_type.add(current_url)
            self.visited.add(current_url)
            count += 1

            # Extract company name
            company = self.extract_company_name(current_url)

            self.logger.info(f"[{count}] {company}: {current_url}")

            image_url, prev_link, next_link, device_type, is_premium = self.get_image_and_nav_from_study(
                current_url, page_type_path
            )

            # Add navigation links to visit queue
            if prev_link and prev_link not in visited_this_type:
                to_visit.add(prev_link)
            if next_link and next_link not in visited_this_type:
                to_visit.add(next_link)

            if is_premium:
                self.logger.info(f"  [PREMIUM] Requires upgrade - skipping")
                self.premium_pages.append(current_url)
                premium += 1
                continue

            if image_url:
                # Use device type from listing page first, fall back to page detection
                final_device_type = self.url_device_types.get(current_url, device_type)
                if final_device_type == "unknown" and device_type != "unknown":
                    final_device_type = device_type

                filepath, existed = self.download_image(image_url, current_url, page_type_dir, final_device_type)
                if filepath:
                    self.companies[company] += 1
                    self.device_types[final_device_type] += 1
                    if existed:
                        self.logger.info(f"  [SKIP] Already exists ({final_device_type})")
                        skipped += 1
                    else:
                        self.logger.info(f"  [OK] Downloaded ({final_device_type})")
                        downloaded += 1
                else:
                    failed += 1
            else:
                self.logger.info(f"  [FAIL] No image found")
                failed += 1

            # Save state periodically
            if count % 50 == 0:
                self.save_state()

        return downloaded, skipped, premium, failed

    def scrape_all(self, page_types=None, limit_per_type=None):
        """Scrape all specified page types."""
        if page_types is None:
            page_types = PAGE_TYPES

        self.logger.info("="*60)
        self.logger.info("UI/UX SCRAPER (source: Baymard Institute)")
        self.logger.info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"Output directory: {self.output_dir}")
        self.logger.info(f"Page types to scrape: {len(page_types)}")
        self.logger.info("="*60)

        # Load previous state
        self.load_state()

        total_downloaded = 0
        total_skipped = 0
        total_premium = 0
        total_failed = 0

        for page_type_name, page_type_path in page_types.items():
            downloaded, skipped, premium, failed = self.crawl_page_type(
                page_type_name, page_type_path, limit=limit_per_type
            )
            total_downloaded += downloaded
            total_skipped += skipped
            total_premium += premium
            total_failed += failed

            # Update global stats
            self.stats["downloaded"] += downloaded
            self.stats["skipped_exists"] += skipped
            self.stats["skipped_premium"] += premium
            self.stats["failed"] += failed

        # Save final state
        self.save_state()

        # Log final summary
        self.log_final_summary()

        return total_downloaded, total_skipped, total_premium, total_failed

    def log_final_summary(self):
        """Log final summary with company diversity and device type stats."""
        self.logger.info("\n" + "="*60)
        self.logger.info("FINAL SUMMARY")
        self.logger.info("="*60)

        self.logger.info(f"\nDownload Statistics:")
        self.logger.info(f"  Downloaded:        {self.stats['downloaded']}")
        self.logger.info(f"  Skipped (exists):  {self.stats['skipped_exists']}")
        self.logger.info(f"  Skipped (premium): {self.stats['skipped_premium']}")
        self.logger.info(f"  Failed:            {self.stats['failed']}")
        self.logger.info(f"  Total processed:   {sum(self.stats.values())}")

        self.logger.info(f"\nDevice Type Breakdown:")
        for device, count in sorted(self.device_types.items(), key=lambda x: -x[1]):
            self.logger.info(f"  {device}: {count}")

        self.logger.info(f"\nCompany Diversity:")
        self.logger.info(f"  Unique companies: {len(self.companies)}")

        # Sort companies by count
        sorted_companies = sorted(self.companies.items(), key=lambda x: -x[1])

        self.logger.info(f"\n  Top 20 companies by # of screenshots:")
        for company, count in sorted_companies[:20]:
            self.logger.info(f"    {company}: {count}")

        if len(sorted_companies) > 20:
            self.logger.info(f"\n  All {len(sorted_companies)} companies:")
            for company, count in sorted_companies:
                self.logger.info(f"    {company}: {count}")

        if self.premium_pages:
            self.logger.info(f"\nPremium Pages Skipped ({len(self.premium_pages)}):")
            for url in self.premium_pages[:20]:
                self.logger.info(f"  {url}")
            if len(self.premium_pages) > 20:
                self.logger.info(f"  ... and {len(self.premium_pages) - 20} more")

        self.logger.info(f"\nLog file: {self.log_file}")
        self.logger.info("="*60)


def main():
    parser = argparse.ArgumentParser(description="Download UI/UX study images (source: Baymard Institute)")
    parser.add_argument("--output", "-o", default="data/ui_images",
                        help="Output directory for images")
    parser.add_argument("--limit", "-l", type=int, default=None,
                        help="Limit number of studies per page type")
    parser.add_argument("--delay", "-d", type=float, default=0.3,
                        help="Delay between requests in seconds")
    parser.add_argument("--types", "-t", nargs="+", default=None,
                        help="Specific page types to scrape (default: all)")

    args = parser.parse_args()

    # Filter page types if specified
    page_types = PAGE_TYPES
    if args.types:
        page_types = {k: v for k, v in PAGE_TYPES.items() if k in args.types}
        if not page_types:
            print(f"No matching page types found. Available types: {list(PAGE_TYPES.keys())}")
            return

    scraper = UIScraper(
        output_dir=args.output,
        delay=args.delay
    )
    scraper.scrape_all(page_types=page_types, limit_per_type=args.limit)


if __name__ == "__main__":
    main()
