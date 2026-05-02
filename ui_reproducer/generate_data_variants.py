#!/usr/bin/env python3
"""
Generate diverse data.json variants for UI screenshots.

Uses Faker for all PII generation and products from products_merged.ndjson.
Optionally uses OpenAI to generate product categories, prices, and breadcrumbs
when `--use-llm` is set.

Usage:
    python generate_data_variants.py --num-variants 100 --output data_variants.ndjson
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from faker import Faker
from rapidfuzz import fuzz, process as rapidfuzz_process

# Conditional imports
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


SCRIPT_DIR = Path(__file__).parent
BASE_DIR = SCRIPT_DIR.parent
TEMPLATE_DATA_PATH = SCRIPT_DIR / "template" / "src" / "data.json"

# Default data directory (can be overridden via --data-dir)
DEFAULT_DATA_DIR = BASE_DIR / "data"

# Initialize Faker
fake = Faker()


def setup_logging(log_file: Optional[Path] = None, verbose: bool = False):
    """Setup logging configuration."""
    level = logging.DEBUG if verbose else logging.INFO

    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%H:%M:%S',
        handlers=handlers
    )
    return logging.getLogger(__name__)


@dataclass
class Stats:
    """Track generation statistics."""
    start_time: float = field(default_factory=time.time)
    products_scanned: int = 0
    products_english: int = 0
    products_non_english: int = 0
    variants_generated: int = 0
    llm_calls: int = 0
    llm_errors: int = 0

    def elapsed(self) -> float:
        return time.time() - self.start_time

    def to_dict(self) -> dict:
        return {
            "elapsed_seconds": round(self.elapsed(), 2),
            "products_scanned": self.products_scanned,
            "products_english": self.products_english,
            "products_non_english": self.products_non_english,
            "variants_generated": self.variants_generated,
            "llm_calls": self.llm_calls,
            "llm_errors": self.llm_errors,
        }


# Random scaling factors for price variance
PRICE_SCALE_MIN = 0.7
PRICE_SCALE_MAX = 1.4


def predict_prices_from_llm(
    products: list[dict],
    client,
    stats: Stats,
    logger: logging.Logger
) -> list[float]:
    """Use LLM to predict realistic prices for products.

    Returns list of prices (floats) for each product, or empty list on failure.
    """
    if not client or not products:
        return []

    # Build product descriptions for the prompt
    product_lines = []
    for i, product in enumerate(products):
        name = ""
        for item in product.get("item_name", []):
            if is_english_tag(item.get("language_tag", "")):
                name = item.get("value", "")
                break
            name = item.get("value", name)

        # Get brand if available
        brand = ""
        for b in product.get("brand", []):
            brand = b.get("value", "")
            if brand:
                break

        # Get brief description
        desc = ""
        for bullet in product.get("bullet_point", [])[:1]:
            if bullet.get("value"):
                desc = bullet["value"][:80]
                break

        product_lines.append(f"{i+1}. {brand} {name[:60]} - {desc}".strip())

    products_text = "\n".join(product_lines)

    prompt = f"""Estimate realistic USD retail prices for these products. Consider brand, quality indicators, and typical market prices.

Products:
{products_text}

Return ONLY a JSON array of numbers (prices in USD, no currency symbols). Example: [29.99, 149.99, 12.50]
Return exactly {len(products)} prices in the same order."""

    try:
        stats.llm_calls += 1
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=500
        )

        content = response.choices[0].message.content.strip()
        match = re.search(r'\[.*\]', content, re.DOTALL)
        if match:
            prices = json.loads(match.group())
            if isinstance(prices, list) and len(prices) == len(products):
                return [float(p) for p in prices]
    except Exception as e:
        stats.llm_errors += 1
        logger.debug(f"LLM price prediction error: {e}")

    return []


def apply_price_scaling(base_price: float) -> float:
    """Apply random scaling to a base price and return as float."""
    scale_factor = random.uniform(PRICE_SCALE_MIN, PRICE_SCALE_MAX)
    final_price = base_price * scale_factor

    # Clamp to reasonable bounds
    final_price = max(0.99, min(final_price, 9999.99))

    # Always apply common price endings
    cents = random.choice([0.99, 0.49, 0.00, 0.95, 0.79])
    final_price = int(final_price) + cents

    return round(final_price, 2)


@dataclass
class ProductData:
    """Product information from ABO dataset."""
    name: str
    price: float
    image_path: str
    description: str
    rating: float
    num_ratings: int
    # Note: item_number and model_number are now generated at runtime
    # using generators.js with patterns matching the target website
    brand: str = ""
    item_category: str = ""


class ProductSearchIndex:
    """Pre-built fuzzy search index for fast product matching."""

    MATCH_THRESHOLD = 50.0  # 50% match threshold

    def __init__(self, products: list[dict], logger: logging.Logger):
        self.logger = logger
        self.products = products
        self.index_data = []  # List of (search_text, product_index)

        logger.info("Building product search index...")
        start = time.time()

        for i, product in enumerate(products):
            # Get product name
            name = ""
            for item in product.get("item_name", []):
                name = item.get("value", "")
                if is_english_tag(item.get("language_tag", "")):
                    break

            # Get description from bullet points
            desc = ""
            for bullet in product.get("bullet_point", [])[:2]:
                if bullet.get("value"):
                    desc = bullet["value"]
                    break

            # Create searchable text
            search_text = f"{name} {desc}".strip().lower()
            if search_text:
                self.index_data.append((search_text, i))

        elapsed = time.time() - start
        logger.info(f"Product index built: {len(self.index_data)} entries in {elapsed:.2f}s")

    def search(self, query: str, limit: int = 10, randomize: bool = True) -> list[dict]:
        """Fuzzy search for products matching the query with optional randomization."""
        if not query or not self.index_data:
            return []

        query = query.lower().strip()

        # Get more results than needed for randomization
        fetch_limit = limit * 5 if randomize else limit

        # Use rapidfuzz to find best matches
        search_texts = [item[0] for item in self.index_data]
        results = rapidfuzz_process.extract(
            query,
            search_texts,
            scorer=fuzz.token_set_ratio,
            limit=fetch_limit,
            score_cutoff=self.MATCH_THRESHOLD
        )

        matched_products = []
        for match_text, score, idx in results:
            product_idx = self.index_data[idx][1]
            matched_products.append(self.products[product_idx])

        # Randomize selection from pool of matches
        if randomize and len(matched_products) > limit:
            matched_products = random.sample(matched_products, limit)

        return matched_products


def is_english_tag(language_tag: str) -> bool:
    """Check if a language tag indicates English."""
    if not language_tag:
        return False
    tag = language_tag.lower()
    return tag.startswith("en")


def generate_breadcrumb_from_llm(
    product_name: str,
    category: str,
    client,
    stats: Stats,
    logger: logging.Logger
) -> list:
    """Use LLM to generate a realistic breadcrumb for a product as an array."""
    if not client:
        return []

    prompt = f"""Generate a realistic breadcrumb navigation path for this product in an e-commerce site.

Product: {product_name}
Category context: {category}

Requirements:
- Return a breadcrumb path with 3-5 levels as a JSON array
- Start general and get more specific
- End with the most specific category (not the product name itself)
- Make it realistic for a major e-commerce site like Amazon

Return ONLY a JSON array, nothing else. Example: ["Electronics", "Audio", "Headphones", "Wireless"]"""

    try:
        stats.llm_calls += 1
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=100
        )

        content = response.choices[0].message.content.strip()
        match = re.search(r'\[.*\]', content, re.DOTALL)
        if match:
            breadcrumb = json.loads(match.group())
            if isinstance(breadcrumb, list):
                return breadcrumb
        return []
    except Exception as e:
        stats.llm_errors += 1
        logger.debug(f"LLM breadcrumb generation error: {e}")
        return []


def extract_brand_and_category_from_llm(
    products: list[dict],
    client,
    stats: Stats,
    logger: logging.Logger
) -> list[dict]:
    """Use LLM to extract brand name and item category from product names/descriptions.

    Returns list of dicts with 'brand' and 'item_category' keys for each product.
    """
    if not client or not products:
        return [{"brand": "", "item_category": ""} for _ in products]

    # Build product list for the prompt
    product_lines = []
    for i, product in enumerate(products):
        name = ""
        for item in product.get("item_name", []):
            if is_english_tag(item.get("language_tag", "")):
                name = item.get("value", "")
                break
            name = item.get("value", name)

        desc = ""
        for bullet in product.get("bullet_point", [])[:2]:
            if bullet.get("value"):
                desc = bullet["value"][:100]
                break

        product_lines.append(f"{i+1}. {name[:80]} | {desc}")

    products_text = "\n".join(product_lines)

    prompt = f"""Extract the brand name and item category for each product below.

Products:
{products_text}

For each product, identify:
1. brand: The manufacturer/brand name (e.g., "Sony", "Nike", "KitchenAid"). Use "Generic" if no brand.
2. item_category: A short 1-3 word item type (e.g., "headphones", "running shoes", "blender", "desk lamp")

Return ONLY a JSON array with one object per product in the same order:
[{{"brand": "Sony", "item_category": "headphones"}}, {{"brand": "Nike", "item_category": "sneakers"}}]"""

    try:
        stats.llm_calls += 1
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1000
        )

        content = response.choices[0].message.content.strip()
        match = re.search(r'\[.*\]', content, re.DOTALL)
        if match:
            results = json.loads(match.group())
            if isinstance(results, list) and len(results) == len(products):
                return results
    except Exception as e:
        stats.llm_errors += 1
        logger.debug(f"LLM brand/category extraction error: {e}")

    return [{"brand": "", "item_category": ""} for _ in products]


def generate_gift_message_from_llm(
    sender_name: str,
    recipient_name: str,
    product_name: str,
    product_category: str,
    client,
    stats: Stats,
    logger: logging.Logger
) -> str:
    """Use LLM to generate a realistic gift message incorporating product details.

    Returns a short, heartfelt gift message string.
    """
    if not client:
        return f"Hope you love the {product_category or 'gift'}! - {sender_name}"

    prompt = f"""Generate a short, heartfelt gift message from {sender_name} to {recipient_name}.

Product being gifted: {product_name}
Product type: {product_category}

Requirements:
- 1-3 sentences, casual and warm tone
- Reference the gift naturally (e.g., "Thought you'd love these headphones!", "Know how much you wanted a new blender")
- Could be for any occasion (birthday, holiday, just because, thank you, congratulations, housewarming, etc.)
- Vary the style: sometimes reference the product directly, sometimes hint at it, sometimes focus on the occasion
- Sign off with just the sender's first name (no "Love," or "Best,")
- Make it feel genuine and specific, not generic

Return ONLY the message text, nothing else."""

    try:
        stats.llm_calls += 1
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=1.0,  # High temp for maximum variety
            max_tokens=120
        )

        message = response.choices[0].message.content.strip()
        # Remove quotes if LLM wrapped it
        if message.startswith('"') and message.endswith('"'):
            message = message[1:-1]
        return message
    except Exception as e:
        stats.llm_errors += 1
        logger.debug(f"LLM gift message generation error: {e}")
        return f"Hope you love the {product_category or 'gift'}! - {sender_name}"


def generate_faker_pii() -> dict:
    """Generate PII fields using Faker."""
    # Generate gender first, then name based on gender
    gender = random.choice(['male', 'female'])
    
    if gender == 'male':
        first_name = fake.first_name_male()
        last_name = fake.last_name()
    else:
        first_name = fake.first_name_female()
        last_name = fake.last_name()

    # Generate address components (US locale by default)
    street = fake.street_address()
    city = fake.city()
    state = fake.state()
    state_abbr = fake.state_abbr()
    postcode = fake.postcode()
    postcode_ext = fake.numerify(text="####")  # ZIP+4 extension (4 digits)
    postcode_full = f"{postcode}-{postcode_ext}"  # Full ZIP+4 format
    country = "United States"
    country_code = "US"

    # Generate additional cities for transit/hub tracking (extended PII identifiers)
    # These represent shipping hub cities that reveal user's region
    city2 = fake.city()
    state2 = fake.state()
    state_abbr2 = fake.state_abbr()
    city3 = fake.city()
    state3 = fake.state()
    state_abbr3 = fake.state_abbr()

    # Generate nearby locations (same state) for lockers, stores, pickup points
    # These stay in the same state for realism
    nearby_cities = [fake.city() for _ in range(25)]
    nearby_streets = [fake.street_address() for _ in range(25)]
    nearby_postcodes = [fake.postcode() for _ in range(25)]
    nearby_postcode_exts = [fake.numerify(text="####") for _ in range(25)]
    nearby_postcodes_full = [f"{nearby_postcodes[i]}-{nearby_postcode_exts[i]}" for i in range(25)]

    # Building security code and delivery instructions
    security_code = fake.bothify(text=random.choice(["####", "###", "#####", "??##", "##??"])).upper()
    delivery_instructions = random.choice([
        "Leave at front door",
        "Ring doorbell twice",
        "Leave with doorman",
        "Place behind screen door",
        "Leave in garage if open",
        "Do not leave if raining",
        "Call upon arrival",
        "Leave at side entrance",
        "Place in package locker",
        f"Gate code: {fake.bothify('####')}",
        f"Building code: {security_code}, leave at door",
        "Hand to resident only",
        "Leave at back porch",
        "Signature required",
        "",  # Sometimes no instructions
    ])

    # Promo/coupon codes
    promo_code = fake.bothify(text=random.choice([
        "SAVE##", "????##", "DEAL####", "???SAVE", "PROMO##??",
        "WINTER##", "SUMMER##", "FALL##", "SPRING##", "HOLIDAY##",
        "WELCOME##", "FIRST##", "VIP####", "EXTRA##", "OFF##"
    ])).upper()

    # Optional Address Line 2 - 50% probability of being filled
    # Used for "Address Line 2" fields or duplicate address displays (e.g., address verification modals)
    has_street_2 = random.random() > 0.5
    if has_street_2:
        # Either a full street address or a secondary address (apt/suite)
        street_2 = fake.street_address() if random.random() > 0.5 else fake.secondary_address()
        postcode_2 = fake.postcode()
    else:
        street_2 = ""
        postcode_2 = ""

    # Generate avatar URL based on gender
    avatar_url = f"https://xsgames.co/randomusers/avatar.php?g={gender}"
    
    # Generate and normalize phone number to 10 digits
    raw_phone = fake.phone_number()
    # First remove extensions (anything after 'x' or 'X')
    phone_no_ext = re.split(r'[xX]', raw_phone)[0]
    # Extract only digits
    phone_digits = re.sub(r'\D', '', phone_no_ext)
    # Keep last 10 digits (removes country code like +1 or 001)
    phone_normalized = phone_digits[-10:] if len(phone_digits) >= 10 else phone_digits.zfill(10)
    
    # Optional alternate phone number - 50% probability of being filled
    has_alt_phone = random.random() > 0.5
    if has_alt_phone:
        raw_phone_alt = fake.phone_number()
        phone_no_ext_alt = re.split(r'[xX]', raw_phone_alt)[0]
        phone_digits_alt = re.sub(r'\D', '', phone_no_ext_alt)
        phone_normalized_alt = phone_digits_alt[-10:] if len(phone_digits_alt) >= 10 else phone_digits_alt.zfill(10)
    else:
        phone_normalized_alt = ""
    
    # Generate email based on name
    email_formats = [
        f"{first_name.lower()}.{last_name.lower()}@{fake.free_email_domain()}",
        f"{first_name.lower()}{last_name.lower()}@{fake.free_email_domain()}",
        f"{first_name[0].lower()}{last_name.lower()}@{fake.free_email_domain()}",
        f"{first_name.lower()}_{last_name.lower()}@{fake.free_email_domain()}",
    ]
    email = fake.random_element(email_formats)

    # Generate login username (distinct from display name PII_FULLNAME)
    username_formats = [
        f"{first_name.lower()}{last_name.lower()}{random.randint(1, 99)}",
        f"{first_name.lower()}_{last_name.lower()}",
        f"{first_name[0].lower()}{last_name.lower()}{random.randint(10, 999)}",
        f"{first_name.lower()}.{last_name.lower()}",
        f"{first_name.lower()}{random.randint(100, 9999)}",
    ]
    login_username = fake.random_element(username_formats)

    # Generate password (realistic looking but fake)
    password = fake.password(length=random.randint(10, 16), special_chars=True, digits=True, upper_case=True, lower_case=True)
    # Confirm password is same as password (for re-entry fields)
    password_confirm = password

    # Generate credit card info
    card_type = random.choice(['visa', 'mastercard', 'amex', 'discover'])
    # Properly capitalized card type names for display
    card_type_display_map = {
        'visa': 'Visa',
        'mastercard': 'Mastercard',
        'amex': 'American Express',
        'discover': 'Discover'
    }
    card_type_display = card_type_display_map.get(card_type, card_type.title())
    card_number = fake.credit_card_number(card_type=card_type)
    card_number_digits = re.sub(r'\D', '', card_number)  # Remove any formatting
    card_last4 = card_number_digits[-4:]
    # Format card number with spaces (groups of 4)
    card_number_formatted = ' '.join([card_number_digits[i:i+4] for i in range(0, len(card_number_digits), 4)])
    card_expiry = fake.credit_card_expire(start='now', end='+5y', date_format='%m/%y')
    card_expiry_month, card_expiry_year = card_expiry.split('/')
    card_cvv = fake.credit_card_security_code(card_type=card_type)
    # Map card type to asset image (in public/payment_methods/)
    card_image_map = {
        'visa': 'visa_brandmark_blue.png',
        'mastercard': 'mastercard_symbol.png',
        'amex': 'amex_logo_color.png',
        'discover': 'discover.webp'
    }
    card_image = card_image_map.get(card_type, 'visa_brandmark_blue.png')

    # Generate gift recipient info (different person from main user)
    gift_gender = random.choice(['male', 'female'])
    if gift_gender == 'male':
        gift_first_name = fake.first_name_male()
    else:
        gift_first_name = fake.first_name_female()
    gift_last_name = fake.last_name()
    gift_email_formats = [
        f"{gift_first_name.lower()}.{gift_last_name.lower()}@{fake.free_email_domain()}",
        f"{gift_first_name.lower()}{gift_last_name.lower()}@{fake.free_email_domain()}",
        f"{gift_first_name[0].lower()}{gift_last_name.lower()}@{fake.free_email_domain()}",
    ]
    gift_email = fake.random_element(gift_email_formats)

    # Generate date of birth (18-80 years old)
    dob = fake.date_of_birth(minimum_age=18, maximum_age=80)
    dob_month = dob.strftime("%m")
    dob_day = dob.strftime("%d")
    dob_year = dob.strftime("%Y")

    # Generate company/business name, PO number, job code - 50% probability each (optional B2B fields)
    has_company = random.random() > 0.5
    has_po_number = random.random() > 0.5
    has_job_code = random.random() > 0.5

    company_name = fake.company() if has_company else ""
    po_number = fake.bothify(text=random.choice([
        "PO-######",
        "PO######",
        "PO-####-####",
        "######",
    ])) if has_po_number else ""
    job_code = fake.bothify(text=random.choice([
        "JOB-####",
        "PRJ-####-##",
        "WO-######",
        "J######",
        "??-####",
    ])).upper() if has_job_code else ""

    # Generate order dates (random date from 2021 to today)
    today = datetime.now()
    start_2021 = datetime(2021, 1, 1)
    days_since_2021 = (today - start_2021).days
    order_date = start_2021 + timedelta(days=random.randint(0, days_since_2021))
    shipping_date = order_date + timedelta(days=random.randint(0, 2))  # 0-2 days after order
    delivery_date = shipping_date + timedelta(days=random.randint(2, 7))  # 2-7 days after shipping
    return_by_date = delivery_date + timedelta(days=random.randint(14, 30))  # 14-30 days after delivery

    return {
        # Name - separated and combined
        "PII_FIRSTNAME": first_name,
        "PII_LASTNAME": last_name,
        "PII_FULLNAME": f"{first_name} {last_name}",

        # Login credentials
        "PII_LOGIN_USERNAME": login_username,
        "PII_LOGIN_PASSWORD": password,
        "PII_LOGIN_PASSWORD_CONFIRM": password_confirm,

        # Credit card info
        "PII_CARD_TYPE": card_type_display,
        "PII_CARD_NUMBER": card_number_formatted,
        "PII_CARD_LAST4": card_last4,
        "PII_CARD_EXPIRY": card_expiry,
        "PII_CARD_EXPIRY_MONTH": card_expiry_month,
        "PII_CARD_EXPIRY_YEAR": card_expiry_year,
        "PII_CARD_CVV": card_cvv,
        "PII_CARD_IMAGE": f"/payment_methods/{card_image}",

        # Personal
        "PII_EMAIL": email,
        "PII_DOB": dob.strftime("%m/%d/%Y"),  # US format: MM/DD/YYYY
        "PII_DOB_ISO": dob.strftime("%Y-%m-%d"),  # ISO format: YYYY-MM-DD
        "PII_DOB_LONG": dob.strftime("%B %d, %Y"),  # Long format: January 15, 1990
        "PII_DOB_MONTH": dob_month,  # 01-12
        "PII_DOB_DAY": dob_day,  # 01-31
        "PII_DOB_YEAR": dob_year,  # 4-digit year
        "PII_COMPANY": company_name,
        "PII_PO_NUMBER": po_number,
        "PII_JOB_CODE": job_code,
        "PII_PHONE": phone_normalized,
        # Phone - split for 3-box inputs (area code, prefix, last 4)
        "PII_PHONE_AREA": phone_normalized[0:3],
        "PII_PHONE_PREFIX": phone_normalized[3:6],
        "PII_PHONE_LINE": phone_normalized[6:10],    # last 4 digits
        "PII_PHONE_SUFFIX": phone_normalized[6:10],  # alias for LINE (last 4 digits)
        # Alternative phone number (for cases where a 2nd number is needed)
        "PII_PHONE_ALT": phone_normalized_alt,
        "PII_PHONE_ALT_AREA": phone_normalized_alt[0:3],
        "PII_PHONE_ALT_PREFIX": phone_normalized_alt[3:6],
        "PII_PHONE_ALT_LINE": phone_normalized_alt[6:10],
        "PII_PHONE_ALT_SUFFIX": phone_normalized_alt[6:10],  # alias for LINE
        "PII_AVATAR": avatar_url,

        # Address - full combined
        "PII_ADDRESS": f"{street}, {city}, {state_abbr} {postcode}",

        # Address - separated components
        "PII_STREET": street,
        "PII_CITY": city,
        "PII_STATE": state,
        "PII_STATE_ABBR": state_abbr,
        "PII_POSTCODE": postcode,
        "PII_POSTCODE_EXT": postcode_ext,
        "PII_POSTCODE_FULL": postcode_full,
        "PII_COUNTRY": country,
        "PII_COUNTRY_CODE": country_code,
        # Address - composite fields for common UI patterns
        "PII_CITY_STATE": f"{city}, {state_abbr}",
        "PII_CITY_STATE_ZIP": f"{city}, {state_abbr} {postcode}",

        # Additional cities for transit/hub tracking (extended PII identifiers)
        # Used for shipping tracking events that reveal user's region
        "PII_CITY2": city2,
        "PII_STATE2": state2,
        "PII_STATE_ABBR2": state_abbr2,
        "PII_CITY_STATE2": f"{city2}, {state_abbr2}",
        "PII_CITY3": city3,
        "PII_STATE3": state3,
        "PII_STATE_ABBR3": state_abbr3,
        "PII_CITY_STATE3": f"{city3}, {state_abbr3}",

        # Nearby locations (same state) for lockers, stores, pickup points
        **{f"PII_LOCATION{i+1}_STREET": nearby_streets[i] for i in range(25)},
        **{f"PII_LOCATION{i+1}_CITY": nearby_cities[i] for i in range(25)},
        **{f"PII_LOCATION{i+1}_POSTCODE": nearby_postcodes[i] for i in range(25)},
        **{f"PII_LOCATION{i+1}_POSTCODE_EXT": nearby_postcode_exts[i] for i in range(25)},
        **{f"PII_LOCATION{i+1}_POSTCODE_FULL": nearby_postcodes_full[i] for i in range(25)},
        **{f"PII_LOCATION{i+1}_CITY_STATE_ZIP": f"{nearby_cities[i]}, {state_abbr} {nearby_postcodes[i]}" for i in range(25)},

        # Delivery/access info
        "PII_SECURITY_CODE": security_code,
        "PII_DELIVERY_INSTRUCTIONS": delivery_instructions,
        "PII_PROMO_CODE": promo_code,

        # Duplicate address fields (_2 suffix) for when same PII appears twice on page
        # Used for address verification modals showing "Original" vs "Recommended" address
        "PII_STREET_2": street_2,
        "PII_POSTCODE_2": postcode_2,
        # Composites using original city/state with alternate postcode
        "PII_CITY_STATE_ZIP_2": f"{city}, {state_abbr} {postcode_2}" if postcode_2 else "",

        # Gift recipient info (for gift orders - different person from main user)
        "PII_GIFT_FIRSTNAME": gift_first_name,
        "PII_GIFT_LASTNAME": gift_last_name,
        "PII_GIFT_FULLNAME": f"{gift_first_name} {gift_last_name}",
        "PII_GIFT_EMAIL": gift_email,
        # PII_GIFT_MESSAGE is added later via LLM in generate_data_variant()

        # Seed for runtime generation of IDs, cards, tracking numbers
        # Use generators.js in App.jsx to generate these with patterns matching the target site
        "SEED": random.randint(100000, 999999),

        # Order dates (calculated relative to today)
        "ORDER_DATE": order_date.strftime("%B %d, %Y"),
        "ORDER_SHIPPING_DATE": shipping_date.strftime("%B %d, %Y"),
        "ORDER_DELIVERY_DATE": delivery_date.strftime("%B %d, %Y"),
        "ORDER_RETURN_BY_DATE": return_by_date.strftime("%B %d, %Y"),
    }


def load_products(
    products_path: Path,
    max_products: int,
    stats: Stats,
    logger: logging.Logger
) -> list[dict]:
    """Load products from merged NDJSON file, filtering for English using language_tag."""
    logger.info(f"Loading products from {products_path}")
    products = []

    if not products_path.exists():
        logger.error(f"Products file not found at {products_path}")
        return products

    with open(products_path, "r") as f:
        for line in f:
            stats.products_scanned += 1

            try:
                product = json.loads(line.strip())

                # Must have name and image
                if not product.get("item_name") or not product.get("main_image"):
                    continue

                # Check language_tag for English
                has_english_name = False
                name_text = ""
                for item in product.get("item_name", []):
                    lang_tag = item.get("language_tag", "")
                    if is_english_tag(lang_tag):
                        has_english_name = True
                        name_text = item.get("value", "")
                        break

                if not has_english_name:
                    stats.products_non_english += 1
                    continue

                # Skip products with no/short name
                if not name_text or len(name_text) < 5:
                    continue

                # Also check bullet points for English (if present)
                has_english_desc = False
                for bullet in product.get("bullet_point", []):
                    if is_english_tag(bullet.get("language_tag", "")):
                        has_english_desc = True
                        break

                # If there are bullet points but none in English, skip
                if product.get("bullet_point") and not has_english_desc:
                    stats.products_non_english += 1
                    continue

                products.append(product)
                stats.products_english += 1

                if len(products) >= max_products:
                    break

            except json.JSONDecodeError:
                continue

    pct_en = 100 * stats.products_english / max(1, stats.products_scanned)
    logger.info(f"Products: {stats.products_english} English / {stats.products_scanned} scanned ({pct_en:.1f}%)")
    logger.info(f"  Filtered out {stats.products_non_english} non-English products")

    return products


def extract_product_info(product: dict, predicted_price: float) -> ProductData:
    """Extract product information from ABO format.

    Args:
        product: Raw product dict from ABO dataset
        predicted_price: LLM-predicted price. Random scaling is applied.
    """
    # Get name (prefer English)
    name = "Product"
    for item in product.get("item_name", []):
        if is_english_tag(item.get("language_tag", "")):
            name = item.get("value", name)
            break
        name = item.get("value", name)

    # Get description from bullet points (prefer English)
    description = ""
    for bullet in product.get("bullet_point", []):
        if is_english_tag(bullet.get("language_tag", "")):
            description = bullet.get("value", "")
            break
        if not description and bullet.get("value"):
            description = bullet["value"]

    if not description:
        description = f"Quality {name.split()[0] if name else 'item'} for everyday use"

    # Apply random scaling to LLM-predicted price
    price = apply_price_scaling(predicted_price)

    # Get image path
    main_image = product.get("main_image", {})
    image_path = main_image.get("full_path", "/placeholders/product.png")

    # Generate random rating (3.0 to 5.0, weighted towards higher ratings)
    rating = round(random.triangular(3.0, 5.0, 4.5), 1)

    # Generate random number of ratings (10 to 10000, log-normal distribution)
    num_ratings = int(random.lognormvariate(5, 1.5))
    num_ratings = max(10, min(num_ratings, 10000))

    # Note: item_number and model_number are generated at runtime using generators.js
    # This allows patterns to vary per website (e.g., Amazon vs Home Depot formats)

    return ProductData(
        name=name[:100],
        price=price,
        image_path=image_path,
        description=description[:200],
        rating=rating,
        num_ratings=num_ratings,
    )


def generate_data_variant(
    product_index: ProductSearchIndex,
    all_products: list[dict],
    stats: Stats,
    logger: logging.Logger,
    max_products: int = 30,
    client = None
) -> dict:
    """Generate a single data.json variant."""

    # Generate all PII using Faker
    data = generate_faker_pii()

    # Pick a random product to be product 1 (anchor product)
    product1_raw = random.choice(all_products)

    # Get product 1's name
    product1_name = ""
    for item in product1_raw.get("item_name", []):
        if is_english_tag(item.get("language_tag", "")):
            product1_name = item.get("value", "")
            break
        product1_name = item.get("value", product1_name)

    # Extract product 1's brand and category via LLM first (for searching and breadcrumb)
    product1_brand_category = extract_brand_and_category_from_llm(
        [product1_raw], client, stats, logger
    )
    product1_category = product1_brand_category[0].get("item_category", "") if product1_brand_category else ""
    product1_brand = product1_brand_category[0].get("brand", "") if product1_brand_category else ""

    # Generate gift message using LLM (now with product context for more relevant messages)
    gift_message = generate_gift_message_from_llm(
        sender_name=data["PII_FIRSTNAME"],
        recipient_name=data["PII_GIFT_FIRSTNAME"],
        product_name=product1_name,
        product_category=product1_category,
        client=client,
        stats=stats,
        logger=logger
    )
    data["PII_GIFT_MESSAGE"] = gift_message

    # Search for similar products using product 1's category
    search_term = product1_category if product1_category else product1_name
    similar_products = product_index.search(search_term, limit=max_products * 2, randomize=True)
    # Remove product1 from results if present (use item_id for comparison)
    product1_id = product1_raw.get("item_id")
    similar_products = [p for p in similar_products if p.get("item_id") != product1_id]

    # Build final product list: product1 first, then similar products
    selected_products = [product1_raw] + similar_products[:max_products - 1]

    # If we still need more, add random products
    if len(selected_products) < max_products:
        remaining = max_products - len(selected_products)
        selected_ids = {p.get("item_id") for p in selected_products}
        available = [p for p in all_products if p.get("item_id") not in selected_ids]
        if available:
            additional = random.sample(available, min(remaining, len(available)))
            selected_products.extend(additional)

    # Get LLM-predicted prices
    predicted_prices = predict_prices_from_llm(selected_products, client, stats, logger)

    # Extract product info with predicted prices
    product_infos = []
    for i, p in enumerate(selected_products):
        price = predicted_prices[i] if i < len(predicted_prices) else 29.99  # fallback if LLM returns fewer
        product_infos.append(extract_product_info(p, predicted_price=price))

    # Extract brand and item category for remaining products (product 1 already extracted)
    remaining_brand_category = extract_brand_and_category_from_llm(
        selected_products[1:], client, stats, logger
    ) if len(selected_products) > 1 else []

    # Combine: product 1's info + remaining products' info
    brand_category_info = [{"brand": product1_brand, "item_category": product1_category}] + remaining_brand_category

    # Generate breadcrumb for product 1 using its pre-extracted category
    breadcrumb = []
    if product_infos and product1_category:
        breadcrumb = generate_breadcrumb_from_llm(
            product_infos[0].name,
            product1_category,
            client,
            stats,
            logger
        )

    # Add products (up to max_products)
    # Note: PRODUCT{i}_ITEM_NUMBER and PRODUCT{i}_MODEL_NUMBER are generated at runtime
    # using generators.js to match the target website's format
    for i, prod in enumerate(product_infos, 1):
        data[f"PRODUCT{i}_NAME"] = prod.name
        data[f"PRODUCT{i}_PRICE"] = prod.price
        data[f"PRODUCT{i}_IMAGE"] = prod.image_path
        data[f"PRODUCT{i}_DESC"] = prod.description
        data[f"PRODUCT{i}_RATING"] = prod.rating
        data[f"PRODUCT{i}_NUM_RATINGS"] = prod.num_ratings
        data[f"PRODUCT{i}_QUANTITY"] = random.choices(
            range(1, 16),
            weights=[50, 20, 12, 7, 4, 2, 1.5, 1, 0.7, 0.5, 0.4, 0.3, 0.2, 0.2, 0.2]
        )[0]

        # Add brand and item category if extracted via LLM
        if i <= len(brand_category_info):
            bc = brand_category_info[i - 1]
            data[f"PRODUCT{i}_BRAND"] = bc.get("brand", "")
            item_category = bc.get("item_category", "")
            data[f"PRODUCT{i}_ITEM_CATEGORY"] = item_category.title() if item_category else ""

        # Add breadcrumb only for first product
        if i == 1 and breadcrumb:
            data[f"PRODUCT{i}_BREADCRUMB"] = breadcrumb

    # Add metadata
    data["_meta"] = {
        "anchor_product": product1_name[:60],
        "product_category": product1_category,
        "search_term": search_term,
        "real_product_count": len(product_infos),
        "generated_with": "faker",
    }

    stats.variants_generated += 1
    return data


def main():
    parser = argparse.ArgumentParser(description="Generate data.json variants for UI screenshots")
    parser.add_argument("--num-variants", type=int, default=100, help="Number of variants to generate")
    parser.add_argument("--output", type=str, default="data_variants.ndjson", help="Output file path")
    parser.add_argument("--max-products", type=int, default=10000, help="Max products to load")
    parser.add_argument("--products-per-variant", type=int, default=30, help="Max products per variant")
    parser.add_argument("--workers", type=int, default=0, help="Number of parallel workers (0=auto)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--log-file", type=str, help="Write logs to file")
    parser.add_argument("--data-dir", type=str, default="data", help="Data directory (default: data)")
    parser.add_argument("--use-llm", action="store_true", help="Use OpenAI for product prices/categories/breadcrumbs")
    args = parser.parse_args()

    # Setup
    random.seed(args.seed)
    Faker.seed(args.seed)
    stats = Stats()

    output_path = SCRIPT_DIR / args.output
    log_path = Path(args.log_file) if args.log_file else output_path.with_suffix(".log")
    logger = setup_logging(log_path, args.verbose)

    # Determine data directory and products path
    data_dir = BASE_DIR / args.data_dir
    products_path = data_dir / "assets" / "products" / "products_merged.ndjson"

    # Determine worker count
    num_workers = args.workers if args.workers > 0 else (os.cpu_count() or 4)

    logger.info("=" * 60)
    logger.info("Data Variant Generator (Faker-based)")
    logger.info("=" * 60)
    logger.info(f"Output: {output_path}")
    logger.info(f"Log: {log_path}")
    logger.info(f"Data dir: {data_dir.relative_to(BASE_DIR)}")
    logger.info(f"Variants: {args.num_variants}")
    logger.info(f"Products per variant: {args.products_per_variant}")
    logger.info(f"Workers: {num_workers}")
    logger.info(f"Seed: {args.seed}")

    # Load products
    logger.info("-" * 40)
    products = load_products(products_path, args.max_products, stats, logger)

    if not products:
        logger.error("No English products found - cannot generate variants")
        sys.exit(1)

    # Build product search index
    product_index = ProductSearchIndex(products, logger)

    client = None
    if args.use_llm:
        if not HAS_OPENAI:
            logger.error("OpenAI package required: pip install openai")
            sys.exit(1)
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            logger.error("OPENAI_API_KEY environment variable required when --use-llm is set")
            sys.exit(1)
        client = OpenAI(api_key=api_key)
        logger.info("Using 4o-mini for prices, brand/category extraction, and breadcrumbs...")
    else:
        logger.info("Using local fallback generation; pass --use-llm to enable OpenAI augmentation.")

    # Generate variants in parallel
    logger.info("-" * 40)
    logger.info(f"Generating {args.num_variants} variants with {num_workers} workers...")

    def generate_single_variant(idx: int) -> dict:
        """Worker function to generate a single variant."""
        return generate_data_variant(
            product_index=product_index,
            all_products=products,
            stats=stats,
            logger=logger,
            max_products=args.products_per_variant,
            client=client
        )

    results = []
    completed = 0

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(generate_single_variant, i): i for i in range(args.num_variants)}

        for future in as_completed(futures):
            try:
                variant = future.result()
                results.append((futures[future], variant))
                completed += 1

                if completed % 10 == 0 or completed == args.num_variants:
                    elapsed = stats.elapsed()
                    rate = completed / elapsed if elapsed > 0 else 0
                    logger.info(f"Progress: {completed}/{args.num_variants} ({rate:.1f}/sec)")
            except Exception as e:
                logger.error(f"Error generating variant: {e}")

    # Sort by original index
    results.sort(key=lambda x: x[0])

    # Find a variant with all optional fields filled and move it to front
    # This ensures both data_variants.ndjson[0] and template have complete data
    if results:
        optional_fields = [
            "PII_COMPANY", "PII_PO_NUMBER", "PII_JOB_CODE",
            "PII_STREET_2", "PII_PHONE_ALT", "PII_DELIVERY_INSTRUCTIONS"
        ]

        # Find index of first variant with all optional fields filled
        full_variant_idx = None
        for i, (_, variant) in enumerate(results):
            all_filled = all(variant.get(field, "").strip() for field in optional_fields)
            if all_filled:
                full_variant_idx = i
                break

        # Move the full variant to the front
        if full_variant_idx is not None and full_variant_idx > 0:
            full_item = results.pop(full_variant_idx)
            results.insert(0, full_item)
            logger.info(f"Moved variant with all optional fields filled to front (was index {full_variant_idx})")
        elif full_variant_idx == 0:
            logger.info("First variant already has all optional fields filled")
        else:
            logger.warning("No variant found with all optional fields filled")

    # Write to file (first item now has all optional fields)
    with open(output_path, "w") as f:
        for _, variant in results:
            f.write(json.dumps(variant) + "\n")

    # Update template/src/data.json with first variant (which has all optional fields)
    if results:
        template_variant = results[0][1]

        # Remove _meta before writing to template
        template_data = {k: v for k, v in template_variant.items() if not k.startswith("_")}

        logger.info(f"Updating template data at {TEMPLATE_DATA_PATH}")
        TEMPLATE_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TEMPLATE_DATA_PATH, "w") as f:
            json.dump(template_data, f, indent=2)
        logger.info("Template data.json updated with fresh variant (all optional fields filled)")

    # Final stats
    logger.info("=" * 60)
    logger.info("COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Output: {output_path}")
    logger.info(f"Variants: {stats.variants_generated}")
    logger.info(f"Time: {stats.elapsed():.1f}s")

    if stats.llm_calls > 0:
        logger.info(f"LLM calls: {stats.llm_calls} ({stats.llm_errors} errors)")

    # Write stats to separate file
    stats_path = output_path.with_suffix(".stats.json")
    with open(stats_path, "w") as f:
        json.dump({
            "generated_at": datetime.now().isoformat(),
            "output_file": str(output_path),
            "args": vars(args),
            "stats": stats.to_dict(),
        }, f, indent=2)
    logger.info(f"Stats: {stats_path}")

    # Show sample
    logger.info("-" * 40)
    logger.info("Sample variant:")
    with open(output_path) as f:
        sample = json.loads(f.readline())
        # Show key fields
        key_fields = [
            "PII_FULLNAME", "PII_LOGIN_USERNAME", "PII_LOGIN_PASSWORD",
            "PII_EMAIL", "PII_DOB", "PII_DOB_MONTH", "PII_DOB_DAY", "PII_DOB_YEAR",  # DOB
            "PII_COMPANY", "PII_PO_NUMBER", "PII_JOB_CODE",
            "PII_PHONE", "PII_PHONE_ALT",
            "PII_ADDRESS", "PII_STREET", "PII_CITY", "PII_STATE_ABBR", "PII_POSTCODE", "PII_POSTCODE_EXT", "PII_POSTCODE_FULL",
            "PII_CITY_STATE2", "PII_CITY_STATE3",  # Transit hub cities
            "PII_LOCATION1_CITY_STATE_ZIP",  # Nearby locations (stores/lockers)
            "PII_SECURITY_CODE", "PII_DELIVERY_INSTRUCTIONS", "PII_PROMO_CODE",
            "PII_GIFT_FULLNAME", "PII_GIFT_EMAIL", "PII_GIFT_MESSAGE",  # Gift recipient
            "SEED",  # Used by generators.js for runtime ID/card generation
            "ORDER_DATE", "ORDER_DELIVERY_DATE",
            "PRODUCT1_NAME", "PRODUCT1_PRICE", "PRODUCT1_BRAND", "PRODUCT1_ITEM_CATEGORY",
        ]
        for k in key_fields:
            if k in sample:
                v_str = str(sample[k])
                if len(v_str) > 50:
                    v_str = v_str[:47] + "..."
                logger.info(f"  {k}: {v_str}")

        # Show metadata
        if "_meta" in sample:
            logger.info("  _meta:")
            meta = sample["_meta"]
            logger.info(f"    product_category: {meta.get('product_category', 'N/A')}")
            logger.info(f"    real_product_count: {meta.get('real_product_count', 0)}")


if __name__ == "__main__":
    main()
