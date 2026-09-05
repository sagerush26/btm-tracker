"""
Scraper for Kelly's Port (kellys-port).

Site: https://www.kellysport.com/--inventory
Platform: DealerSpike (v7 list template) - server-rendered HTML, no JS execution required to
get listing data (confirmed via inspection - no `window.__STATE__`-style embedded JSON was
found either; the DOM itself already carries structured `data-unit-*` attributes per boat).

Fetching / anti-bot notes
--------------------------
The site sits behind Cloudflare. During development, plain Python `requests` calls were
reliably blocked with an HTTP 403 "Just a moment..." Cloudflare challenge page on every
attempt, while the system `curl` binary (identical User-Agent header) reliably got a normal
200 response with the full listing HTML every time. This is consistent with Cloudflare doing
TLS/JA3 fingerprinting that flags Python's `requests`/urllib3 TLS stack specifically, rather
than blocking based on headers or IP. Headless Playwright/Chromium was also tried, but in the
sandbox used for development its requests to this host stalled indefinitely on render-blocking
third-party resources (jQuery/Bootstrap CDNs, Google Fonts) without ever reaching
`domcontentloaded`, even though a raw TCP/TLS connection to the site succeeded instantly -
this may be sandbox-network-specific and could behave differently in GitHub Actions.

To maximize reliability across different runner environments, `_http_get` tries, in order:
  1. `requests` (fastest, pure-pip - works if the runner's TLS fingerprint isn't flagged),
  2. the system `curl` binary via `subprocess` (proven reliable in development; `curl` is
     preinstalled on GitHub Actions' `ubuntu-latest` runners),
  3. Playwright (sync API, headless Chromium) as a last resort, in case a future site change
     actually requires JS execution or both of the above get blocked in a given environment.
Each strategy is only used if the previous one raises or returns a non-200/blocked response.

Each boat is a `<li class="v7list-results__item">` element carrying rich `data-unit-*`
attributes directly on the tag (make, model, year, condition, category, subcategory, id) -
these are used directly instead of parsing visible text, which is far more robust.

Pagination
----------
The site's own `sz` (page size) query parameter only honors specific values (20/50); values
above 50 (e.g. 200) are silently ignored and fall back to a default page size, so it cannot be
used to fetch all listings on one page. We therefore fetch with `sz=50` (the largest page size
that is honored) and paginate using the `pg` query parameter.

`pg` is quirky: `pg=0` (or omitted) returns page 1, `pg=1` reliably returns 0 items (confirmed
via repeated manual fetches - not a transient error), and `pg=2`, `pg=3`, ... return subsequent
pages. Rather than hardcoding this off-by-one, we walk `pg=0,1,2,3,...` and:
  - skip any page that returns 0 items but continue on to the next `pg` value, and
  - stop once we hit two consecutive empty pages (safety net against infinite loops / a
    genuinely-finished result set), or a hard `max_pages` cap.
We de-duplicate by `data-unit-id` across all fetched pages as a final safety net in case the
site's paging ever overlaps or changes behavior.

Pricing
-------
Each listing can show up to three price spans: "Retail Price" (MSRP, class
`vehicle-price--old`), "Our Price" / current asking price (class `vehicle-price--current`),
and "Savings" (class `vehicle-price--savings`). Only `vehicle-price--current` is a real,
comparable asking price - "Retail Price" is frequently a much higher MSRP shown for contrast,
and many listings show "Click for a Quote" (no numeric price at all) instead of a current
price. We therefore only extract `vehicle-price--current` numeric values; listings without one
are treated as having no price (consistent with the "range-only / mostly no exact prices"
ground-truth note for this dealer).

Validated against a manual ground-truth audit on 2026-09-05: total=114, new=70, used=44,
priceLow=59900, priceHigh=1526489, and category/brand breakdowns all matched exactly using
this method.

Reliability caveats
--------------------
- Inventory changes daily; exact counts will drift over time (the task description itself
  expects this).
- The `pg=1`-is-empty quirk is a real, observed behavior of this dealer's platform as of
  2026-09-05. If DealerSpike changes their pagination scheme this may need revisiting; the
  walk-until-two-consecutive-empty-pages logic is intended to degrade gracefully rather than
  silently under-count if that happens.
- A handful of listings are not actually boats (e.g. a boat lift was observed in inventory
  under make "Summerset"). The dealer's own site lists these as inventory items, and the
  ground-truth counts include it, so we keep it and bucket it into the "Other" category
  rather than filtering it out.
- If the site ever moves to JS-rendered listings, a Playwright-based fallback would be needed;
  as of this writing plain `requests` + BeautifulSoup is sufficient and preferred for speed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Optional

import requests
from bs4 import BeautifulSoup

DEALER_ID = "kellys-port"
DEALER_NAME = "Kelly's Port"

BASE_URL = "https://www.kellysport.com/--inventory"
PAGE_SIZE = 50  # largest `sz` value actually honored by the site
MAX_PAGES = 20  # hard safety cap on number of `pg` values walked
MAX_CONSECUTIVE_EMPTY = 2  # stop after this many empty pages in a row

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Normalize dealer subcategory strings into the required output labels.
SUBCATEGORY_MAP = {
    "pontoon / tritoon": "Pontoon / Tritoon",
    "pontoon": "Pontoon / Tritoon",
    "tritoon": "Pontoon / Tritoon",
    "wake / surf / ski": "Wakeboard / Ski",
    "wakeboard": "Wakeboard / Ski",
    "ski": "Wakeboard / Ski",
    "center console": "Center Console",
    "cruiser": "Cruiser / Cabin",
    "cruiser / cabin": "Cruiser / Cabin",
    "cabin": "Cruiser / Cabin",
    "bowrider": "Bowrider",
    "performance": "Performance",
    "high performance": "Performance",
    "yacht": "Yacht",
    "fishing": "Fishing",
    "fish": "Fishing",
    "power": "Power",
}
ALLOWED_CATEGORIES = {
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
}


def _normalize_category(raw_subcategory: str) -> str:
    key = (raw_subcategory or "").strip().lower()
    return SUBCATEGORY_MAP.get(key, "Other")


def _normalize_brand(raw_make: str) -> str:
    name = (raw_make or "").strip()
    if not name:
        return "Unknown"
    # Merge common variant spellings/suffixes observed in this dealer's data.
    aliases = {
        "grady white": "Grady-White",
        "grady-white": "Grady-White",
        "tiara": "Tiara Yachts",
        "tiara yachts": "Tiara Yachts",
        "cobalt": "Cobalt Boats",
        "cobalt boats": "Cobalt Boats",
    }
    key = name.lower()
    if key in aliases:
        return aliases[key]
    return name


def _extract_current_price(item) -> Optional[int]:
    """Return the numeric 'current' (asking) price for a listing, if any.

    Only the `vehicle-price--current` span is used - `vehicle-price--old` is the
    (often much higher) MSRP/retail price, and many listings only show a
    "Click for a Quote" link with no numeric current price at all.
    """
    price_el = item.select_one(".vehicle-price--current .vehicle-price__price")
    if not price_el:
        return None
    text = price_el.get_text(strip=True)
    match = re.search(r"[\d,]+", text)
    if not match:
        return None
    digits = match.group().replace(",", "")
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _looks_blocked(html: str) -> bool:
    if not html:
        return True
    snippet = html[:2000].lower()
    return "just a moment" in snippet or "attention required" in snippet or "cf-browser-verification" in snippet


def _fetch_via_requests(session: requests.Session, url: str, params: dict) -> Optional[str]:
    try:
        resp = session.get(url, params=params, headers=HEADERS, timeout=30)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    if _looks_blocked(resp.text):
        return None
    return resp.text


def _fetch_via_curl(url: str, params: dict) -> Optional[str]:
    if shutil.which("curl") is None:
        return None
    query = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    full_url = f"{url}?{query}"
    try:
        result = subprocess.run(
            [
                "curl", "-s", "-L", "--max-time", "30",
                "-A", HEADERS["User-Agent"],
                "-H", f"Accept-Language: {HEADERS['Accept-Language']}",
                full_url,
            ],
            capture_output=True,
            timeout=40,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    html = result.stdout.decode("utf-8", errors="replace")
    if _looks_blocked(html):
        return None
    return html


def _fetch_via_playwright(url: str, params: dict) -> Optional[str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    query = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    full_url = f"{url}?{query}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.goto(full_url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_selector(".v7list-results__item", timeout=15000)
                html = page.content()
            finally:
                browser.close()
    except Exception:
        return None
    if _looks_blocked(html):
        return None
    return html


class _FetchState:
    """Remembers which fetch strategy last succeeded so we don't retry a strategy that has
    already proven blocked (e.g. `requests` getting a Cloudflare 403) on every single page."""

    def __init__(self):
        self.working_method: Optional[str] = None


def _fetch_page(session: requests.Session, pg: int, state: "_FetchState") -> str:
    params = {
        "sortby": "length_overall|asc",
        "sz": str(PAGE_SIZE),
        "pg": str(pg),
    }

    methods = [
        ("requests", lambda: _fetch_via_requests(session, BASE_URL, params)),
        ("curl", lambda: _fetch_via_curl(BASE_URL, params)),
        ("playwright", lambda: _fetch_via_playwright(BASE_URL, params)),
    ]
    # Try the previously-successful method first to avoid re-paying the cost/latency of a
    # method we already know is blocked in this environment.
    if state.working_method:
        methods.sort(key=lambda m: m[0] != state.working_method)

    for name, fn in methods:
        html = fn()
        if html is not None:
            state.working_method = name
            return html

    raise RuntimeError(
        f"Failed to fetch Kelly's Port inventory page {pg} via requests, curl, and Playwright."
    )


def _fetch_all_items(session: requests.Session):
    """Walk `pg=0,1,2,...` collecting listing elements, de-duplicated by unit id."""
    seen_ids = set()
    items_by_id = {}
    consecutive_empty = 0
    state = _FetchState()

    for pg in range(0, MAX_PAGES):
        html = _fetch_page(session, pg, state)
        soup = BeautifulSoup(html, "lxml")
        page_items = soup.select(".v7list-results__item")

        if not page_items:
            consecutive_empty += 1
            if consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                break
            continue

        consecutive_empty = 0
        new_on_page = 0
        for item in page_items:
            unit_id = item.get("data-unit-id")
            if unit_id and unit_id in seen_ids:
                continue
            if unit_id:
                seen_ids.add(unit_id)
            items_by_id[unit_id or id(item)] = item
            new_on_page += 1

        # If a page returns items but they're all duplicates we've already seen,
        # treat it like an empty page (avoids infinite loop if `pg` ever wraps).
        if new_on_page == 0:
            consecutive_empty += 1
            if consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
                break

    return list(items_by_id.values())


def scrape() -> dict:
    session = requests.Session()
    items = _fetch_all_items(session)

    total = len(items)
    new_count = 0
    used_count = 0
    category_counts: dict = {}
    brand_counts: dict = {}
    prices = []

    for item in items:
        condition = (item.get("data-unit-condition") or "").strip().lower()
        if condition == "new":
            new_count += 1
        elif condition in ("used", "pre-owned", "preowned"):
            used_count += 1
            condition = "used"
        else:
            condition = "unknown"

        category = _normalize_category(item.get("data-unit-subcategory", ""))
        if category not in ALLOWED_CATEGORIES:
            category = "Other"
        category_counts[category] = category_counts.get(category, 0) + 1

        brand = _normalize_brand(item.get("data-unit-make", ""))
        brand_counts[brand] = brand_counts.get(brand, 0) + 1

        price = _extract_current_price(item)
        if price is not None:
            price_condition = condition if condition in ("new", "used") else "unknown"
            prices.append((price, price_condition))

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
