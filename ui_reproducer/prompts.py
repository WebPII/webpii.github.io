"""
Prompt templates for Claude Code UI reproduction.

Architecture:
- Structure prompt: Visual reproduction + data substitution (what goes where)
- Compliance prompt: Attributes + span wrapping + input handling (mechanical rules)
- Visual refinement prompts: Pure visual fixes only
"""


def build_structure_prompt(image_path: str, output_dir: str, assets_config: str) -> str:
    """Phase 1: Visual layout + data substitution. Focus on WHAT goes WHERE."""
    return f"""You are a Lead Frontend Engineer with strong attention to detail. Reproduce this UI as a pixel-perfect React component with data substitution.

**ANALYZE THE IMAGE:**
- Layout: flex/grid containers, alignment, spacing
- Colors: exact hex codes, shadows, border-radius
- Hierarchy: break into logical components
- Modals/Sidebars (IMPORTANT):
  - **Full page coverage**: Backdrops, sidebars, and modals must cover the ENTIRE page height, not just viewport. Avoid `fixed` or `h-screen` which limit to viewport.
  - **pointer-events**: Backdrop/wrapper layers need `pointer-events-none`, modal/sidebar content needs `pointer-events-auto`. Detection uses elementFromPoint.
- **Sticky/Fixed Bottom Elements** (buttons, CTAs at page bottom):
  - NEVER use `fixed bottom-0` - this positions relative to viewport, breaks full-page screenshots
  - Use normal document flow: place at end of content, no fixed/sticky positioning

**TECHNICAL STACK:**
- React (Vite) + Tailwind CSS with arbitrary values (`w-[340px]`, `text-[13px]`)
- Icons: `lucide-react`, `@heroicons/react`, or `phosphor-react` (no emojis)
- Semantic classNames: `className="header ..."`, `className="cart-summary ..."`

**DATA SUBSTITUTION:**
Replace ALL personal/product/order data. `import data from '@data'`

PII (user info AND location-revealing data):
- `PII_FULLNAME`, `PII_FIRSTNAME`, `PII_LASTNAME`, `PII_EMAIL`, `PII_PHONE`
- `PII_LOGIN_USERNAME`, `PII_LOGIN_PASSWORD`, `PII_LOGIN_PASSWORD_CONFIRM` - credentials (confirm is same value for re-entry fields)
- `PII_DOB` (MM/DD/YYYY), `PII_DOB_ISO`, `PII_DOB_LONG`, `PII_DOB_MONTH/DAY/YEAR` - date of birth
- `PII_COMPANY`, `PII_PO_NUMBER`, `PII_JOB_CODE` - business/commercial fields
- `PII_GIFT_FIRSTNAME`, `PII_GIFT_LASTNAME`, `PII_GIFT_FULLNAME`, `PII_GIFT_EMAIL`, `PII_GIFT_MESSAGE` - gift recipient
- `PII_PHONE_AREA/PREFIX/LINE/SUFFIX` - for 3-box phone inputs
- `PII_PHONE_ALT`, `PII_PHONE_ALT_AREA/PREFIX/LINE/SUFFIX` - alternative phone number
- `PII_STREET`, `PII_CITY`, `PII_STATE`, `PII_STATE_ABBR`, `PII_POSTCODE`, `PII_POSTCODE_EXT`, `PII_POSTCODE_FULL`, `PII_COUNTRY`, `PII_COUNTRY_CODE`
- `PII_CITY_STATE`, `PII_CITY_STATE_ZIP`, `PII_ADDRESS` - composites
- `PII_AVATAR` - profile image
- `PII_CARD_TYPE` (visa/mastercard/amex/discover), `PII_CARD_IMAGE` (logo from assets), `PII_CARD_NUMBER`, `PII_CARD_LAST4`
- `PII_CARD_EXPIRY` (MM/YY), `PII_CARD_EXPIRY_MONTH`, `PII_CARD_EXPIRY_YEAR`, `PII_CARD_CVV`
- **Card logos**: Use `<img src={{data.PII_CARD_IMAGE}} />` for card brand icons - never recreate with styled divs
- `PII_SECURITY_CODE`, `PII_DELIVERY_INSTRUCTIONS` - building access/delivery info
- **Transit/hub cities**: `PII_CITY2`, `PII_CITY3`, `PII_CITY_STATE2`, `PII_CITY_STATE3` for tracking locations
- **Nearby locations** (stores, lockers, pickup): `PII_LOCATION1_STREET`, `PII_LOCATION1_CITY`, `PII_LOCATION1_CITY_STATE_ZIP` through `PII_LOCATION10_*`

Products - **EVERY product on screen uses data.PRODUCT{{N}}_*** (no hardcoded names/prices):
- Use PRODUCT1_, PRODUCT2_, PRODUCT3_... for each product visible (cart items, recommendations, carousels, etc.)
- Fields: `NAME`, `DESC`, `BRAND`, `CATEGORY`, `PRICE`, `IMAGE`, `RATING`, `NUM_RATINGS`, `QUANTITY`
- `PRICE` is a **number** (float like 316.79), not a string
- `PRODUCT{{N}}_IMAGE` - ALWAYS use `<img src={{data.PRODUCT{{N}}_IMAGE}} />`. Never recreate product packaging/labels with styled divs
- `PRODUCT1_BREADCRUMB` - array like `["Electronics", "Audio", "Headphones"]`, render with map()

**DERIVED PRODUCT VALUES (calculate, not in data.json):**
- `PRODUCT1_ORIGINAL_PRICE` - strikethrough price, derive from PRICE (e.g., `(price * 1.25).toFixed(2)`)
- Savings amount - calculate as `originalPrice - price`
- Discount % - calculate as `((originalPrice - price) / originalPrice * 100)`
- Mark derived values with `data-product="PRODUCT1_ORIGINAL_PRICE"` etc.

Order/Dates:
- `ORDER_DATE`, `ORDER_SHIPPING_DATE`, `ORDER_DELIVERY_DATE`, `ORDER_RETURN_BY_DATE`
- ALL dates derive from these (no hardcoded "Tuesday, Oct 31")
- **Shipping options** (Standard, Express, Premium, etc.): Calculate delivery dates RELATIVE to `ORDER_DELIVERY_DATE`
  - e.g., Express = `ORDER_DELIVERY_DATE - 2 days`, Premium = `ORDER_DELIVERY_DATE - 4 days`
  - Mark each with `data-order` attribute
- **Tracking timestamps**: each event needs distinct time - generate with varied hours/minutes, each with `data-order` attr

**CALCULATED VALUES (compute from products, NOT from data.*):**
- `ORDER_TOTAL` - sum of (price × qty) for cart items
- `ORDER_SUBTOTAL` - before tax/shipping
- `ORDER_TAX` - calculate as % of subtotal
- `ORDER_SHIPPING_COST` - reasonable value or "FREE"
- `ORDER_NUM_ITEMS` - count items in cart
Mark with `data-order="ORDER_*"` attributes even though calculated.

**GENERATED IDs/SKUs (order IDs, tracking numbers, item numbers):**
```jsx
import {{ createGenerators }} from '@generators'
const gen = createGenerators(data.SEED)
gen.id('###-####')      // Pattern: #=digit, X=upper, x=lower, *=alphanumeric
```

**INPUT:**
- Image: {image_path}
- Assets: {assets_config}

**OUTPUT:** Edit `{output_dir}/src/App.jsx`
"""


def build_compliance_attributes_prompt(output_dir: str, original_image: str) -> str:
    """Phase 2a: Attribute marking - what needs data-* attributes and where."""
    return f"""Mark all dynamic values with data attributes. Focus on WHAT to mark.

**CRITICAL: NOTHING should be hardcoded.** All PII, product info, prices, dates, and order values MUST use data.* variables or be calculated from them. Search for hardcoded strings like names, addresses, prices, dates - replace them ALL.

**READ:** `{output_dir}/src/App.jsx` and `{original_image}` (to catch missed substitutions)

**CHECKLIST:**

□ **DATA ATTRIBUTES** - Mark ALL dynamic/identifying values:
  - `data.PII_*` → `data-pii="PII_*"`
  - `data.PRODUCT*` → `data-product="PRODUCT*"`
  - `data.ORDER_*` or calculated order values → `data-order="ORDER_*"`
  - **Hardcoded state values also need attributes** (cart count "0", cart price "$0.00", etc.)

□ **HEADER CART** - Mark cart badge AND price in header:
  - Item count badge: `data-order="CART_NUM_ITEMS"` (even if "0")
  - Cart price/subtotal: `data-order="CART_SUBTOTAL"` (even if "$0.00")

□ **SEARCH/FILTER INPUTS** - Mark inputs used for searching or filtering:
  - Look for: search icons nearby, placeholder="Search", magnifying glass buttons, filter inputs
  - Header/site-wide search bar: `data-search="HEADER_SEARCH"`
  - Page-specific (orders, products, etc.): `data-search="SEARCH_*"`
  - Keep value empty (no prefilled text)

□ **SPAN WRAPPING** - Attributes wrap ONLY the variable, not labels:
  - WRONG: `<span data-pii="PII_EMAIL">Email: {{data.PII_EMAIL}}</span>`
  - RIGHT: `Email: <span data-pii="PII_EMAIL">{{data.PII_EMAIL}}</span>`

□ **COMPOSITE FIELDS** - Use when values display together:
  - `PII_FULLNAME` for "John Smith"
  - `PII_CITY_STATE` for "Seattle, WA"
  - `PII_CITY_STATE_ZIP` for "Seattle, WA 98101"

□ **COUNTRY FIELDS** - Often missed, always mark:
  - `PII_COUNTRY` for full name ("United States")
  - `PII_COUNTRY_CODE` for abbreviation ("US")

□ **DUPLICATE PII** - When same data appears multiple times:
  - First instance: `data-pii="PII_STREET"`
  - Second instance: `data-pii="PII_STREET_2"` (add `_2` suffix)

□ **MULTIPLE LOCATIONS** - Transit/hub cities need distinct fields:
  - Use `PII_CITY2`, `PII_CITY3`, `PII_CITY_STATE2`, `PII_CITY_STATE3`

□ **CALCULATED ORDER VALUES** - Compute from products, mark with `data-order="ORDER_*"`:
  - ORDER_TOTAL, ORDER_SUBTOTAL, ORDER_TAX, ORDER_NUM_ITEMS

□ **DERIVED PRODUCT VALUES** - NOT in data.json, calculate and mark:
  - `PRODUCT1_ORIGINAL_PRICE` - derive from PRICE (e.g., multiply by 1.2-1.3)
  - Mark with `data-product="PRODUCT1_ORIGINAL_PRICE"` etc.

□ **MISSED SUBSTITUTIONS** - Search code for ANY hardcoded values and replace:
  - Hardcoded names (e.g., "John Smith", "Amazon") → use `data.PII_*` or `data.PRODUCT*_BRAND`
  - Hardcoded prices (e.g., "$29.99", "10.50") → use `data.PRODUCT*_PRICE` or calculate
  - Hardcoded dates (e.g., "Jan 15", "Tuesday") → derive from `data.ORDER_*` dates
  - Hardcoded addresses/cities (INCLUDING LOCATION OF STORES) → use `data.PII_*` fields
  - Recreated product visuals → use `<img src={{data.PRODUCT{{N}}_IMAGE}} />`

  **PII fields** (mark with `data-pii`):
  - Names: `PII_FULLNAME`, `PII_FIRSTNAME`, `PII_LASTNAME`
  - Contact: `PII_EMAIL`, `PII_PHONE`, `PII_PHONE_ALT`
  - Phone parts: `PII_PHONE_AREA/PREFIX/SUFFIX`, `PII_PHONE_ALT_AREA/PREFIX/SUFFIX`
  - Login: `PII_LOGIN_USERNAME`, `PII_LOGIN_PASSWORD`, `PII_LOGIN_PASSWORD_CONFIRM`
  - Address: `PII_STREET`, `PII_STREET_2`, `PII_CITY`, `PII_STATE`, `PII_STATE_ABBR`, `PII_POSTCODE`, `PII_POSTCODE_EXT`, `PII_POSTCODE_FULL`, `PII_COUNTRY`, `PII_COUNTRY_CODE`
  - Composites: `PII_ADDRESS`, `PII_CITY_STATE`, `PII_CITY_STATE_ZIP`, `PII_CITY_STATE_ZIP_2`
  - Transit cities: `PII_CITY2`, `PII_CITY3`, `PII_CITY_STATE2`, `PII_CITY_STATE3`, `PII_STATE2`, `PII_STATE3`
  - Payment: `PII_CARD_TYPE`, `PII_CARD_IMAGE`, `PII_CARD_NUMBER`, `PII_CARD_LAST4`, `PII_CARD_EXPIRY`, `PII_CARD_EXPIRY_MONTH`, `PII_CARD_EXPIRY_YEAR`, `PII_CARD_CVV`
  - DOB: `PII_DOB`, `PII_DOB_ISO`, `PII_DOB_LONG`, `PII_DOB_MONTH`, `PII_DOB_DAY`, `PII_DOB_YEAR`
  - Business: `PII_COMPANY`, `PII_PO_NUMBER`, `PII_JOB_CODE`
  - Other: `PII_AVATAR`, `PII_PROMO_CODE`, `PII_SECURITY_CODE`, `PII_DELIVERY_INSTRUCTIONS`
  - Gift: `PII_GIFT_FIRSTNAME`, `PII_GIFT_LASTNAME`, `PII_GIFT_FULLNAME`, `PII_GIFT_EMAIL`, `PII_GIFT_MESSAGE`
  - Nearby locations (stores/lockers): `PII_LOCATION{{1-25}}_STREET`, `_CITY`, `_POSTCODE`, `_POSTCODE_EXT`, `_POSTCODE_FULL`, `_CITY_STATE_ZIP`

  **Product fields** (mark with `data-product`):
  - `PRODUCT{{N}}_NAME`, `_DESC`, `_BRAND`, `_ITEM_CATEGORY`
  - `PRODUCT{{N}}_PRICE`, `_IMAGE`, `_QUANTITY`
  - `PRODUCT{{N}}_RATING`, `_NUM_RATINGS`
  - `PRODUCT1_BREADCRUMB` (array)

  **Order fields** (mark with `data-order`):
  - Dates: `ORDER_DATE`, `ORDER_SHIPPING_DATE`, `ORDER_DELIVERY_DATE`, `ORDER_RETURN_BY_DATE`
  - Calculated: `ORDER_TOTAL`, `ORDER_SUBTOTAL`, `ORDER_TAX`, `ORDER_SHIPPING_COST`, `ORDER_NUM_ITEMS`
  - Cart: `CART_NUM_ITEMS`, `CART_SUBTOTAL`

**OUTPUT:** Edit App.jsx, then list changes made.
"""


def build_compliance_inputs_prompt(output_dir: str) -> str:
    """Phase 2b: Input handling and edge cases - mechanical implementation details."""
    return f"""Apply input handling and edge case rules to App.jsx.

**READ:** `{output_dir}/src/App.jsx`

**CHECKLIST:**

□ **INPUT FIELDS** - Use getPartialProps for inputs/textareas:
  ```jsx
  import {{ getPartialProps, getSelectProps }} from './partialFill'
  <input data-pii="PII_EMAIL" {{...getPartialProps('PII_EMAIL')}} />
  ```
  - Fill ALL inputs with values from data.json via getPartialProps
  - Optional fields ("Address Line 2", "Alternate Phone") may have empty values - that's expected
  - **Address Line 2**: Mark with `data-pii="PII_STREET_2"` even if empty
  - **Phone Number 2**: Mark with `data-pii="PII_ALT_PHONE"` even if empty

□ **SELECT/DROPDOWN FIELDS** - Use getSelectProps:
  ```jsx
  <select data-pii="PII_STATE" {{...getSelectProps('PII_STATE')}}>
    <option value="">Select a State</option>
    <option value={{data.PII_STATE}}>{{data.PII_STATE}}</option>
  </select>
  ```

□ **TRACKING TIMESTAMPS** - Each tracking event needs:
  - Distinct time (varied hours/minutes, not all identical)
  - `data-order` attribute on each timestamp span

□ **BREADCRUMB** - If page has breadcrumb navigation:
  - Use `data.PRODUCT1_BREADCRUMB` array with map()
  - Wrap with `data-product="PRODUCT1_BREADCRUMB"`

□ **VARYING FONT SIZES / SUP TAGS** - Use `inline-block` for prices with mixed sizes:
  ```jsx
  <span className="inline-block" data-order="ORDER_TOTAL">
    <span className="text-sm align-top">$</span>
    <span className="text-2xl">73</span>
    <span className="text-sm align-top">92</span>
  </span>
  ```
  Prefer `<span>` with `align-top` over `<sup>` for better bbox containment.

□ **STAR RATINGS** - Both stars AND count need attributes:
  - Wrap star ICONS container with `data-product="PRODUCT*_RATING"`
  - Wrap review COUNT with `data-product="PRODUCT*_NUM_RATINGS"`

□ **GENERATORS** - IDs, SKUs, tracking numbers use gen.id()

**OUTPUT:** Edit App.jsx, then list changes made.
"""

def build_split_section_prompt(
    original_part_path: str,
    screenshot_part_path: str,
    output_dir: str,
    part_num: int,
    total_parts: int,
    start_y: int,
    end_y: int
) -> str:
    """Visual-only refinement for a specific section."""
    section_name = {1: "TOP", 2: "MIDDLE", 3: "BOTTOM"}.get(part_num, f"PART {part_num}")
    if total_parts == 2:
        section_name = {1: "TOP", 2: "BOTTOM"}.get(part_num, f"PART {part_num}")

    return f"""You are a Lead Frontend Engineer focused on aesthetic fidelity. Fix VISUAL issues in the **{section_name} SECTION** (pixels {start_y}-{end_y}).

**Compare:**
- Original: {original_part_path}
- Current: {screenshot_part_path}

**Focus on visual fidelity such that the original and current images match in visual quality:**
- Element alignment (horizontal/vertical centering, edge alignment)
- Spacing consistency (padding, margin, gaps between items)
- Colors, backgrounds, gradients
- Borders, shadows, border-radius
- Typography (size, weight, line-height)
- Icon sizing and alignment with adjacent text

**CRITICAL:**
- Different product names/images/prices/usernames are EXPECTED (data is substituted) - ignore these visual differences
- Different # of products or content lengths shifts element positions - this is EXPECTED
- Focus on COMPOSITIONAL similarity, not pixel-perfect matching
- **DO NOT DELETE code outside the visible pixel range** - header/footer are handled separately

Edit `{output_dir}/src/App.jsx` with precise Tailwind values (`mt-[14px]`, `gap-[8px]`, `items-center`).
"""


def build_header_prompt(
    original_header_path: str,
    screenshot_header_path: str,
    output_dir: str,
    assets_config: str = ""
) -> str:
    """Visual-only refinement for the header."""
    assets_section = f"\n**Assets:** {assets_config}" if assets_config else ""

    return f"""You are a Lead Frontend Engineer with pixel-perfect attention to detail. Fix VISUAL issues in the **HEADER** (top ~200px).

**Compare:**
- Original: {original_header_path}
- Current: {screenshot_header_path}

**Focus on visual fidelity such that the original and current images match in visual quality:**
- Logo placement, size, vertical alignment (use company logos from assets if available)
- Navigation links spacing, alignment, and vertical centering
- Search bar width, height, placeholder text, icon alignment
- User account area alignment (avatar, username, dropdown icons)
- Cart/notification icons sizing and badge positioning
- Background color/gradient accuracy

{assets_section}

**CRITICAL:** Different usernames/cart counts are EXPECTED (data is substituted) - ignore these visual differences.

Edit `{output_dir}/src/App.jsx`. Don't break content below the header.
"""


def build_section_eval_prompt(
    original_image: str,
    current_screenshot: str,
    num_parts: int,
    output_dir: str
) -> str:
    """Quick eval to identify which sections need visual refinement."""
    section_names = ["TOP", "BOTTOM"] if num_parts == 2 else ["TOP", "MIDDLE", "BOTTOM"]
    sections_list = ", ".join([f"{i+1}={name}" for i, name in enumerate(section_names)])

    return f"""Compare images and identify sections with VISUAL bugs.

**READ:** `{output_dir}/src/App.jsx` to see what elements exist in the code

**Images:**
- Original: {original_image}
- Current: {current_screenshot}

**Sections:** {sections_list}

**IGNORE these differences (data is substituted):**
- Different names, usernames, emails, addresses
- Different product names, images, prices, quantities
- Different dates, order numbers, tracking numbers

**IMPORTANT - Content length varies:**
- Different # of products, different text lengths = different page heights
- Elements may appear at different y-positions in the crop - this is EXPECTED
- Don't flag elements as "missing" - check App.jsx to see if they exist in code

**Flag these issues:**
- Clearly wrong colors or backgrounds
- Major spacing/alignment problems
- **Hardcoded products in code**: string literals like "Snowdrops", "$10.17" instead of data.PRODUCT{{N}}_*
- **Recreated product visuals**: divs styled as product packaging instead of <img src={{data.PRODUCT{{N}}_IMAGE}} />

Output JSON only:
```json
{{"sections_to_refine": [1, 2], "reason": "brief explanation"}}
```
"""


def build_header_eval_prompt(
    original_header: str,
    screenshot_header: str,
    output_dir: str
) -> str:
    """Quick eval to determine if header needs visual refinement."""
    return f"""Compare headers for VISUAL bugs only.

**READ:** `{output_dir}/src/App.jsx` to see what elements exist in the code

**Images:**
- Original: {original_header}
- Current: {screenshot_header}

**IGNORE these differences (data is substituted):**
- Different usernames, cart counts, notification badges
- Different account names or profile images

**Flag these issues:**
- Logo clearly wrong size/position
- Major alignment or spacing problems
- Wrong colors or backgrounds
- **Hardcoded products in code**: string literals instead of data.PRODUCT{{N}}_*
- Don't flag minor pixel differences or "missing" elements (check App.jsx first)

Output JSON only:
```json
{{"needs_refinement": true, "reason": "brief explanation"}}
```
"""
