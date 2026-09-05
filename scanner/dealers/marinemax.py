"""
Scraper for MarineMax (Lake of the Ozarks location).

Dealer id: marinemax
Source URL: https://www.marinemax.com/collections/all

IMPORTANT CONTEXT:
MarineMax is a large, multi-location dealer, and the public
`/collections/all` page (and its Shopify JSON API) returns MarineMax's
ENTIRE NATIONAL brokerage/new-boat catalog (~3,000 products across
dozens of stores), not just the Lake of the Ozarks store. There is no
separate "Lake of the Ozarks" Shopify collection exposed publicly, and
the storefront's location filter is powered by a client-side Coveo
search widget rather than distinct collection URLs, so it can't be
targeted with a different collection handle.

However, every product's `handle` (URL slug) embeds the originating
store's location, e.g.:
    used-sea-ray-sundancer-320-2003-marinemax-lake-ozark-221179
    brokerage-bertram-46-1985-marinemax-stuart-217108

So this scraper:
  1. Pulls the full national catalog via Shopify's
     `/collections/all/products.json?limit=250&page=N` JSON API
     (reliable, structured, no HTML parsing needed).
  2. Filters down to only products whose handle contains the
     "lake-ozark" location token, which corresponds to the
     MarineMax Lake of the Ozarks store.
  3. Derives new/used from the handle prefix ("new-" vs "used-"/
     "brokerage-"), brand from the Shopify `vendor` field, price from
     the first (only) variant, and category from keyword matching
     against the title + description.

If MarineMax adds another store on the same lake in the future (there
is only one "lake-ozark" token observed as of 2026-09-05), this filter
would need revisiting, but for now it cleanly isolates the local store.
"""

from __future__ import annotations

import json
import re
import sys
import time
from html import unescape
from typing import Optional

import requests

DEALER_ID = "marinemax"
DEALER_NAME = "MarineMax"
BASE_PRODUCTS_URL = "https://www.marinemax.com/collections/all/products.json"
LOCATION_TOKEN = "lake-ozark"  # Lake of the Ozarks store, embedded in product handles
PAGE_LIMIT = 250
MAX_PAGES = 40  # safety cap (national catalog is ~3000 products / 250 per page = ~12 pages)
REQUEST_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CATEGORY_LABELS = [
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

# Ordered keyword rules: first match wins. Checked against a lowercased
# blob of "vendor + title + body text".
_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("Pontoon / Tritoon", [
        "pontoon", "tritoon", "grand mariner", "sunliner", "cruiser 250",
        "harris",  # Harris Boatworks is a pontoon/tritoon-only brand
    ]),
    ("Wakeboard / Ski", [
        "wakeboard", "wake boat", "ski boat", "wakesurf", "surf gate",
        "nautique", "supra", "mastercraft", "malibu wakesetter",
    ]),
    ("Yacht", [
        "yacht", "flybridge", "fly bridge", "motor yacht", "megayacht",
        "azimut", "ocean alexander", "sedan bridge",
    ]),
    ("Center Console", [
        "center console", "cc ", " cc\"", "walkaround",
    ]),
    ("Fishing", [
        "sportfish", "sport fish", "convertible", "fishing", "offshore",
        "bertram",
    ]),
    ("Cruiser / Cabin", [
        "sundancer", "cruiser", "cabin cruiser", "express cruiser",
        "cuddy cabin", "trawler", "gls", "vtr", "aviara",
    ]),
    ("Bowrider", [
        "bowrider", "bow rider", "deck boat",
    ]),
    ("Performance", [
        "performance", "sxo", "slx", "sport boat", "high performance",
        "cigarette", "outboard performance",
    ]),
]


def _classify_category(vendor: str, title: str, body_text: str) -> str:
    blob = f"{vendor} {title} {body_text}".lower()
    for label, keywords in _CATEGORY_RULES:
        for kw in keywords:
            if kw in blob:
                return label
    return "Other"


def _new_or_used_from_handle(handle: str) -> str:
    """Derive condition from the handle prefix used by MarineMax's
    Shopify product slugs (e.g. 'new-...', 'used-...', 'brokerage-...').
    Falls back to 'unknown' if no recognizable prefix is present.
    """
    prefix = handle.split("-marinemax-")[0] if "-marinemax-" in handle else handle
    if prefix.startswith("new-"):
        return "new"
    if prefix.startswith("used-") or prefix.startswith("brokerage-"):
        return "used"
    return "unknown"


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return unescape(text)


def _fetch_all_products(session: requests.Session) -> list[dict]:
    """Fetch MarineMax's full Shopify catalog via the JSON API, paginating
    until an empty page is returned."""
    all_products: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        params = {"limit": PAGE_LIMIT, "page": page}
        resp = session.get(BASE_PRODUCTS_URL, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        products = data.get("products", [])
        if not products:
            break
        all_products.extend(products)
        if len(products) < PAGE_LIMIT:
            # last page
            break
        time.sleep(0.25)  # be polite between paginated requests
    return all_products


def _extract_price(product: dict) -> Optional[int]:
    variants = product.get("variants") or []
    if not variants:
        return None
    price_str = variants[0].get("price")
    if not price_str:
        return None
    try:
        price_float = float(price_str)
    except (TypeError, ValueError):
        return None
    if price_float <= 0:
        return None
    return int(round(price_float))


def scrape() -> dict:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    all_products = _fetch_all_products(session)

    # Filter down to the Lake of the Ozarks store only (see module docstring).
    local_products = [
        p for p in all_products
        if LOCATION_TOKEN in (p.get("handle") or "")
    ]

    total = 0
    new_count = 0
    used_count = 0
    category_counts: dict[str, int] = {}
    brand_counts: dict[str, int] = {}
    prices: list[tuple[int, str]] = []

    for product in local_products:
        handle = product.get("handle", "")
        title = product.get("title", "") or ""
        vendor = (product.get("vendor") or "Unknown").strip() or "Unknown"
        body_text = _strip_html(product.get("body_html", ""))

        condition = _new_or_used_from_handle(handle)

        total += 1
        if condition == "new":
            new_count += 1
        elif condition == "used":
            used_count += 1

        category = _classify_category(vendor, title, body_text)
        category_counts[category] = category_counts.get(category, 0) + 1

        brand_counts[vendor] = brand_counts.get(vendor, 0) + 1

        price = _extract_price(product)
        if price is not None:
            prices.append((price, condition))

    price_values = [p for p, _ in prices]
    price_low = min(price_values) if price_values else None
    price_high = max(price_values) if price_values else None

    return {
        "id": DEALER_ID,
        "name": DEALER_NAME,
        "total": total,
        "new": new_count,
        "used": used_count,
        "category": category_counts,
        "brand": brand_counts,
        "prices": prices,
        "priceLow": price_low,
        "priceHigh": price_high,
    }


if __name__ == "__main__":
    print(json.dumps(scrape(), indent=2))
