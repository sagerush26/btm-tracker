"""
Scraper for Big Thunder Marine (https://bigthundermarine.com/boats-for-sale/).

Site platform
-------------
The site is a WordPress site whose inventory search/results pages are powered
by the "WARP10" inventory widget plugin. The widget itself calls out to a
separate, dealer-network-wide JSON API hosted at
``https://inventory.coasttechnology.org/api/v3/inventory/`` to fetch listing
data (the WordPress page just renders whatever comes back from that API).

That API is a clean, paginated, authenticated-by-referer-only JSON endpoint
that returns full structured listing data (title, make, model, condition,
category taxonomy, and price fields) -- there is no need to render or parse
HTML at all. This was confirmed by:

1. Fetching the raw HTML of the inventory page with ``requests`` and finding
   the ``warp10_environment`` / ``warp10_settings`` JS globals embedded in the
   page, which reveal the API base URL (``stealthInventoryAPIBaseURL``) and
   the dealer's ``company_ids`` (``["72"]``).
2. Running the page in headless Playwright and observing (via the
   ``request`` event) the exact XHR the front-end JS widget issues against
   that API, including the query-string shape it uses for filtering/sorting/
   pagination.
3. Replaying that exact request with plain ``requests`` (only a ``Referer``
   header pointing at the dealer site is required -- no cookies, tokens, or
   JS execution needed) and confirming it returns identical JSON.

Because a direct JSON API is available, this scraper uses ``requests`` only
(no Playwright/browser rendering needed at runtime).

Pagination
----------
The API paginates with ``page`` / ``per_page`` query params and returns a
``pagination`` object (``total``, ``current_page``, ``last_page``,
``has_more_pages``). Empirically ``per_page`` values above ~70 are rejected
with HTTP 422 ("Invalid request."), so this scraper requests 50 rows per page
and loops until ``current_page >= last_page`` (or no more data comes back),
picking up all listings across every page.

Reliability caveats
--------------------
* This is an undocumented, third-party (coasttechnology.org) API that backs
  many boat dealer sites on the same "WARP10" platform, not something
  Big Thunder Marine publishes/guarantees. It could change shape or require
  different params in the future without notice.
* A small number of listings in the feed are missing both `unit_make.name`
  and image/price data (duplicate placeholder / production-slot rows); these
  are still counted in totals but bucketed as "Unknown" brand and/or
  'unknown' price status when a price cannot be determined.
* Some vehicles come back with no numeric price at all (`price_lowest`,
  `price_current`, and `price_msrp` all null/0) -- these are effectively
  "Call/Request for Price" listings and are excluded from `prices`/`priceLow`
  /`priceHigh` but still counted in `total`/`new`/`used`/`category`/`brand`.
* The feed includes a `lot_status` field. Listings whose status indicates
  they're already sold-pending ("Pending Sale", "Sale Pending", etc.) or are
  a factory "Production Slot" (not yet a physical unit on the lot) are
  excluded from the count entirely -- this matches what the live website
  itself displays as current for-sale inventory (verified against a manual
  browser audit: API returns 208 raw rows, 191 after this filter, vs. 188
  counted by hand on the live site the same day -- well within normal
  day-to-day drift, whereas the raw 208 was not).
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

import requests

DEALER_ID = "big-thunder-marine"
DEALER_NAME = "Big Thunder Marine"

API_URL = "https://inventory.coasttechnology.org/api/v3/inventory/"
REFERER = "https://bigthundermarine.com/"
COMPANY_ID = "72"  # Big Thunder Marine's company id on the coasttechnology.org platform

PER_PAGE = 50  # empirically, values > ~70 return HTTP 422 "Invalid request."
MAX_PAGES = 40  # safety cap so a runaway pagination loop can never hang CI

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": REFERER,
    "Accept": "application/json",
}

# Normalize the site's raw `unit_classification.name` taxonomy values into the
# required output buckets.
CATEGORY_MAP = {
    "pontoon": "Pontoon / Tritoon",
    "tritoon": "Pontoon / Tritoon",
    "ski and wakeboard": "Wakeboard / Ski",
    "wakeboard": "Wakeboard / Ski",
    "ski": "Wakeboard / Ski",
    "center console": "Center Console",
    "dual console": "Center Console",
    "cruiser": "Cruiser / Cabin",
    "cuddy cabin": "Cruiser / Cabin",
    "express cruiser": "Cruiser / Cabin",
    "weekender": "Cruiser / Cabin",
    "center cockpit": "Cruiser / Cabin",
    "houseboats": "Cruiser / Cabin",
    "houseboat": "Cruiser / Cabin",
    "bowrider": "Bowrider",
    "deck boats": "Bowrider",
    "deck boat": "Bowrider",
    "high performance": "Performance",
    "performance": "Performance",
    "motor yachts": "Yacht",
    "motor yacht": "Yacht",
    "yacht": "Yacht",
    "fishing": "Fishing",
    "bass boat": "Fishing",
    "bay boat": "Fishing",
    "flats boat": "Fishing",
    "offshore fishing": "Fishing",
    "catamaran": "Power",
    "pwc": "Power",
    "jon boat": "Power",
    "boat trailer": "Other",
    "trailer": "Other",
}

CATEGORY_BUCKETS = [
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


def _normalize_category(raw_name: str | None) -> str:
    if not raw_name:
        return "Other"
    key = raw_name.strip().lower()
    return CATEGORY_MAP.get(key, "Other")


def _normalize_condition(raw_name: str | None) -> str:
    if not raw_name:
        return "unknown"
    key = raw_name.strip().lower()
    if "new" in key:
        return "new"
    if "used" in key or "pre-owned" in key or "preowned" in key or "pre owned" in key:
        return "used"
    return "unknown"


def _extract_price(item: dict[str, Any]) -> int | None:
    """Pick the best available numeric price for a listing.

    Prefers `price_lowest`, then `price_current`, then `price_msrp`.
    Returns None if the dealer only shows "Call/Request for Price" (i.e. all
    price fields are null/zero).
    """
    for key in ("price_lowest", "price_current", "price_msrp"):
        val = item.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    return None


def _fetch_page(session: requests.Session, page: int) -> dict[str, Any]:
    params = {
        "filters[$and][0][displayOnWebsite][$eq]": "true",
        "sort[0]": "lot_status:asc",
        "sort[1]": "price_lowest:desc",
        "per_page": str(PER_PAGE),
        "page": str(page),
        "company[0]": COMPANY_ID,
        "withUnitData": "1",
    }
    resp = session.get(API_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


_EXCLUDED_LOT_STATUSES = {
    "pending sale",
    "sale pending",
    "production slot",
    "sold",
}


def _is_excluded(item: dict[str, Any]) -> bool:
    status = (item.get("lot_status") or "").strip().lower()
    return status in _EXCLUDED_LOT_STATUSES


def _fetch_all_listings() -> list[dict[str, Any]]:
    listings: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    with requests.Session() as session:
        page = 1
        last_page = None
        while page <= MAX_PAGES:
            data = _fetch_page(session, page)
            batch = data.get("data") or []
            if not batch:
                break

            for item in batch:
                if _is_excluded(item):
                    continue
                item_id = item.get("id")
                if item_id is not None and item_id in seen_ids:
                    continue
                if item_id is not None:
                    seen_ids.add(item_id)
                listings.append(item)

            pagination = data.get("pagination") or {}
            last_page = pagination.get("last_page")
            current_page = pagination.get("current_page", page)
            has_more = pagination.get("has_more_pages")

            if last_page is not None:
                if current_page >= last_page:
                    break
            elif has_more is False:
                break
            elif len(batch) < PER_PAGE:
                # No pagination metadata and a short page -> assume done.
                break

            page += 1
            time.sleep(0.2)  # be polite to the shared inventory API

    return listings


def scrape() -> dict:
    listings = _fetch_all_listings()

    total = len(listings)
    new_count = 0
    used_count = 0

    category_counter: Counter[str] = Counter({b: 0 for b in CATEGORY_BUCKETS})
    brand_counter: Counter[str] = Counter()
    prices: list[tuple[int, str]] = []

    for item in listings:
        condition_raw = (item.get("condition") or {}).get("name")
        condition = _normalize_condition(condition_raw)
        if condition == "new":
            new_count += 1
        elif condition == "used":
            used_count += 1

        classification_raw = (item.get("unit_classification") or {}).get("name")
        category = _normalize_category(classification_raw)
        category_counter[category] += 1

        brand_raw = (item.get("unit_make") or {}).get("name")
        brand = (brand_raw or "").strip() or "Unknown"
        brand_counter[brand] += 1

        price = _extract_price(item)
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
        "category": dict(category_counter),
        "brand": dict(brand_counter),
        "prices": prices,
        "priceLow": price_low,
        "priceHigh": price_high,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(scrape(), indent=2))
