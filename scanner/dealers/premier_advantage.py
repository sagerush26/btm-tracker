"""
Scraper for Premier Advantage Marine (premier-advantage).

Site: https://premieradvantagemarine.com/boats-for-sale/

Discovery notes (2026-09-05):
- Site is WordPress + WooCommerce, built with Elementor Pro "Loop Grid" widget
  (data-widget_type="loop-grid.product", pagination_type="numbers"). The static
  HTML of /boats-for-sale/ only contains the first 9 products server-side; the
  rest are loaded client-side by Elementor's loop-grid AJAX/pagination, which
  is expensive and brittle to reproduce.
- WooCommerce exposes its standard Store API at
  /wp-json/wc/store/v1/products which returns ALL products (98 at audit time)
  in a single request with per_page=100, no JS rendering needed. Response
  headers include X-WP-Total / X-WP-TotalPages for verification.
- Each product's `description` HTML embeds a "Basic Boat Info" block with
  Price / Make / Model / Condition / Category lines, but this text is
  template boilerplate copy that is frequently stale (e.g. many showroom
  "order this model" pages say "Condition: New" even though the site's own
  actual condition taxonomy tags them as regular/used inventory). The
  reliable condition signal is the WooCommerce product category taxonomy:
  slugs `new-inventory` / `pre-loved-inventory`. A handful of real, in-stock
  units (identifiable by an "ST#"/"Stock #" number in the description) carry
  neither tag; those are treated as new only if they are not part of the
  generic "*-showroom" catalog-model categories (manufacturer showroom/order
  pages default to the used/other bucket in this dealer's own site taxonomy).
- Category taxonomy on this site is intentionally NOT mutually exclusive
  (e.g. a boat can be tagged both "bowriders" and "fishing-boats", and
  Stingray dual-console boats carry "dual-consoles" + "bowriders" +
  "fishing-boats" simultaneously). To match how the dealer's own site
  represents category breakdowns, normalized category counts here are
  computed as the SUM of raw category-tag occurrences mapped into each
  normalized bucket (not deduplicated per product), so totals across
  categories will exceed the product total - this is expected and matches
  the site's own overlapping taxonomy.
- Brand is not reliably present in the WooCommerce `brands` taxonomy field
  (empty for most products) nor consistently in the description "Make:"
  line (many showroom pages omit it, and some descriptions only carry the
  outboard engine manufacturer under a "Make:" line inside the
  Engines/Speed section, which would be mistaken for the boat's brand by a
  naive regex). Brand is therefore parsed from the product title, matched
  against a list of manufacturer names actually observed on this dealer's
  site.
- Exact prices ARE available (in cents) via the Store API `prices.price`
  field for every product that has one, even though the on-page compare
  audit assumed the front-end price cards might be range-only. Since exact
  cents-level prices are available from the API without extra requests,
  they are used directly.

No Playwright/browser rendering is required: this module only calls
`requests` against the public WooCommerce Store API JSON endpoint.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

import requests

DEALER_ID = "premier-advantage"
DEALER_NAME = "Premier Advantage Marine"

BASE_URL = "https://premieradvantagemarine.com"
STORE_API_URL = f"{BASE_URL}/wp-json/wc/store/v1/products"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

REQUEST_TIMEOUT = 30
PER_PAGE = 100  # WooCommerce Store API max page size

# Known boat manufacturers carried by this dealer, used to parse brand out of
# the product title (longest names first so multi-word brands match before
# any shorter substring could).
KNOWN_BRANDS = [
    "FOUR WINNS",
    "ALUMACRAFT",
    "STINGRAY",
    "WELLCRAFT",
    "MONTARA",
    "REGAL",
    "CHAPARRAL",
    "BENNINGTON",
    "SEA RAY",
    "FOUNTAIN",
    "FORMULA",
    "SCARAB",
    "BARLETTA",
    "PREMIER",
    "CROWNLINE",
    "IKON",
    "BAJA",
]

# Normalized category taxonomy -> allowed target buckets.
NORMALIZED_CATEGORIES = [
    "Pontoon / Tritoon",
    "Wakeboard / Ski",
    "Center Console",
    "Cruiser / Cabin",
    "Bowrider",
    "Performance",
    "Yacht",
    "Fishing",
    "Power",
    "Other",
]

# WooCommerce product-category slug -> normalized bucket. A slug may map to
# only one bucket; buckets accumulate raw tag counts (see module docstring).
CATEGORY_SLUG_MAP = {
    "pontoon-boats": "Pontoon / Tritoon",
    "wakeboard-wakesurf": "Wakeboard / Ski",
    "center-consoles": "Center Console",
    "cruisers": "Cruiser / Cabin",
    "cuddy-cabins": "Cruiser / Cabin",
    "performance-cruisers": "Cruiser / Cabin",
    "bowriders": "Bowrider",
    "performance-boats": "Performance",
    "fishing-boats": "Fishing",
    "bass-boats": "Fishing",
    "yachts": "Yacht",
    "deck-boats": "Other",
    "other": "Other",
}

STOCK_NUM_RE = re.compile(r"(?:ST#|Stock\s*#)\s*(\d+)", re.IGNORECASE)


def _fetch_all_products() -> List[dict]:
    """Fetch every product from the WooCommerce Store API.

    A single request with per_page=100 covers the whole catalog at this
    dealer's current scale (~98 products), but pagination is still handled
    in case the catalog grows beyond one page.
    """
    products: List[dict] = []
    page = 1
    session = requests.Session()
    session.headers.update(HEADERS)

    while True:
        resp = session.get(
            STORE_API_URL,
            params={"per_page": PER_PAGE, "page": page},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        products.extend(batch)

        total_pages_header = resp.headers.get("X-WP-TotalPages")
        try:
            total_pages = int(total_pages_header) if total_pages_header else None
        except ValueError:
            total_pages = None

        if total_pages is not None:
            if page >= total_pages:
                break
        elif len(batch) < PER_PAGE:
            break

        page += 1
        if page > 50:  # safety guard against runaway pagination
            break

    return products


def _get_brand(name: str) -> Optional[str]:
    upper = f" {name.upper()} "
    for brand in KNOWN_BRANDS:
        if f" {brand} " in upper or upper.strip().startswith(brand):
            return brand.title()
    return None


def _classify_condition(product: dict) -> str:
    """Return 'new', 'used', or 'unknown' for a product.

    Priority:
      1. Explicit WooCommerce category tags new-inventory / pre-loved-inventory.
      2. Real in-stock units (identifiable via an ST#/Stock# in the
         description) that are NOT part of a generic manufacturer showroom
         category are treated as new.
      3. Everything else defaults to used (matches this dealer's own
         taxonomy default for showroom/catalog-style listings).
    """
    slugs = {c.get("slug", "") for c in product.get("categories", [])}

    if "new-inventory" in slugs:
        return "new"
    if "pre-loved-inventory" in slugs:
        return "used"

    is_showroom = any("showroom" in slug for slug in slugs)
    description = product.get("description") or ""
    has_stock_number = bool(STOCK_NUM_RE.search(description))

    if has_stock_number and not is_showroom:
        return "new"

    return "used"


def _normalize_categories(product: dict) -> List[str]:
    slugs = [c.get("slug", "") for c in product.get("categories", [])]
    mapped = [CATEGORY_SLUG_MAP[s] for s in slugs if s in CATEGORY_SLUG_MAP]
    return mapped


def _get_price_dollars(product: dict) -> Optional[int]:
    prices = product.get("prices") or {}
    raw = prices.get("price")
    if not raw:
        return None
    try:
        cents = int(raw)
    except (TypeError, ValueError):
        return None
    if cents <= 0:
        return None
    minor_unit = prices.get("currency_minor_unit", 2)
    try:
        divisor = 10 ** int(minor_unit)
    except (TypeError, ValueError):
        divisor = 100
    return round(cents / divisor)


def scrape() -> dict:
    products = _fetch_all_products()

    total = len(products)
    new_count = 0
    used_count = 0

    category_counter: Counter = Counter({c: 0 for c in NORMALIZED_CATEGORIES})
    brand_counter: Counter = Counter()
    prices: List[Tuple[int, str]] = []

    for product in products:
        condition = _classify_condition(product)
        if condition == "new":
            new_count += 1
        else:
            used_count += 1

        for bucket in _normalize_categories(product):
            category_counter[bucket] += 1

        brand = _get_brand(product.get("name", ""))
        if brand:
            brand_counter[brand] += 1

        price_dollars = _get_price_dollars(product)
        if price_dollars is not None:
            prices.append((price_dollars, condition if condition in ("new", "used") else "unknown"))

    price_values = [p for p, _ in prices]
    price_low = min(price_values) if price_values else None
    price_high = max(price_values) if price_values else None

    return {
        "id": DEALER_ID,
        "name": DEALER_NAME,
        "total": total,
        "new": new_count,
        "used": used_count,
        "category": dict(category_counter),
        "brand": dict(brand_counter),
        "prices": prices,
        "priceLow": price_low,
        "priceHigh": price_high,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(scrape(), indent=2))
