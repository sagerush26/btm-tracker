"""
Scraper for Performance Boat Center (https://www.performanceboatcenter.com/all-boats/)

Site structure notes (as of 2026-09-05):
- The inventory listing page is built on a WordPress/Elementor site using a "DMS"
  (Dealer Management System) plugin (classes prefixed `dms-`, widget type
  `dms_srp_listings`). Listings are fully SERVER-RENDERED in the initial HTML
  returned by a plain `requests.get()` -- no JavaScript/Playwright is required.
- Pagination is classic numbered-page pagination via URL path segments:
      https://www.performanceboatcenter.com/all-boats/
      https://www.performanceboatcenter.com/all-boats/page/2/
      https://www.performanceboatcenter.com/all-boats/page/3/
      ... etc
  The widget `dms_srp_pagination` on page 1 renders a nav with `page-numbers`
  links showing the total number of pages, so we discover the page count from
  page 1 itself and then fetch each subsequent page. There is no AJAX/"load
  more"/infinite-scroll involved for the base listing grid -- each page/N/
  URL is a normal, fully rendered HTML document (confirmed 24 cards on pages
  1-3 and a final partial page of the remainder, e.g. 24+24+24+15=87).
- Each listing card is an `<li class="dms-listing-sri" data-listing-id="...">`
  element. Per-card text (in order) contains: [badge?], year, condition
  ("New" or "Preowned"), title ("{Brand} {Model}"), spec labels/values,
  and a `.dms-listing-price` block containing either a real formatted price
  (e.g. "$479,950", sometimes two prices like "$499,950 $479,950" for
  MSRP-vs-sale-price -- we take the *last*/lowest as the current asking
  price) or no numeric text at all (e.g. "SALE PENDING", "Call For Price").
- Condition ("New"/"Preowned") is reliably present per-card and is used
  directly for the new/used split.
- Brand is derived from the listing title by matching against a curated
  list of known makes seen on this site (falls back to the title's first
  word if no known make matches), which reproduces the site's own "Make"
  filter dropdown counts.
- Category is NOT present per listing card. Category counts are only
  available in aggregate from the sidebar filter's "Category" `<select>`
  dropdown, which the DMS plugin annotates with exact `data-total="N"`
  counts per category term (e.g. `data-term='Catamaran' data-total='25'`).
  We read those aggregate counts directly (no need to guess per-listing
  category) and remap the site's raw category taxonomy into the requested
  normalized buckets. Because a handful of listings on this dealer's site
  are tagged with generic/administrative categories (e.g. "Boat Trailer",
  "Used Vehicles") or are left uncategorized, the category dropdown total
  can be slightly less than the true inventory total -- this is expected
  and documented in the reliability caveats below.
- If, on a future crawl, the listing grid html does not contain any
  `dms-listing-sri` cards (e.g. the site switches to a client-rendered
  widget), the scraper automatically falls back to rendering the page with
  Playwright (headless Chromium) and re-parsing the resulting DOM.

Reliability caveats:
- Inventory changes daily; exact counts will drift from the 2026-09-05
  ground truth audit (total=87, new=9, used=78, priceLow=22500,
  priceHigh=2395000). The scraper's pagination-count and per-page-card
  approach is robust to this drift.
- Category counts come from an aggregate filter widget rather than
  per-listing tags, so the categories dict may undercount the grand total
  by a small amount (typically a handful of listings) relative to `total`.
  `total`, `new`, `used`, `brand`, and `prices` are all derived directly
  from per-listing cards and are the most reliable fields.
- A small number of listings show no numeric price (e.g. "SALE PENDING",
  "Call For Price"); these are simply omitted from the `prices` list and
  do not affect priceLow/priceHigh.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from typing import Optional

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.performanceboatcenter.com/all-boats/"
DEALER_ID = "performance-boat-center"
DEALER_NAME = "Performance Boat Center"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

REQUEST_TIMEOUT = 30
MAX_PAGES_SAFETY = 30  # hard safety cap in case pagination discovery misbehaves

# Known makes/brands seen on this dealer's site. Sorted longest-first so that
# e.g. "Sunsation Powerboats" is matched before a shorter partial term.
KNOWN_MAKES = sorted(
    [
        "Adrenaline", "Baja", "Boston Whaler", "Chris Craft", "Cigarette",
        "Cobalt", "Concept", "Crownline", "Donzi", "Envision", "Ford",
        "Fountain", "IRON", "Interceptor", "Kenworth", "MTI",
        "Mercury Marine", "Monterey", "Mystic", "Nor-Tech", "Outerlimits",
        "Performance Powerboats", "Ranger Boats", "Regal", "Skater",
        "Statement", "Sunsation Powerboats", "Wellcraft",
        "Wright Performance", "Bennington", "Formula", "Yamaha",
        "Sea Ray", "Scout", "Sea Hunt", "Grady-White", "Pursuit",
        "Robalo", "Tidewater", "Everglades", "HCB", "Invincible",
        "Deep Impact", "Apex", "Velocity", "DCB", "Marine Technology",
    ],
    key=len,
    reverse=True,
)

# Map the dealer's raw ("category") filter taxonomy into the normalized set
# requested: Pontoon / Tritoon, Wakeboard / Ski, Center Console,
# Cruiser / Cabin, Bowrider, Performance, Yacht, Fishing, Power, Other
CATEGORY_MAP = {
    "bass boat": "Fishing",
    "boat trailer": "Other",
    "bowrider": "Bowrider",
    "catamaran": "Other",
    "center console": "Center Console",
    "high performance": "Performance",
    "mid cabin open bow": "Cruiser / Cabin",
    "outboard engines": "Power",
    "sport cruiser": "Cruiser / Cabin",
    "used vehicles": "Other",
    "pontoon": "Pontoon / Tritoon",
    "tritoon": "Pontoon / Tritoon",
    "wakeboard": "Wakeboard / Ski",
    "ski": "Wakeboard / Ski",
    "cruiser": "Cruiser / Cabin",
    "cabin": "Cruiser / Cabin",
    "yacht": "Yacht",
    "fishing": "Fishing",
    "power": "Power",
}

NORMALIZED_CATEGORIES = [
    "Pontoon / Tritoon", "Wakeboard / Ski", "Center Console",
    "Cruiser / Cabin", "Bowrider", "Performance", "Yacht", "Fishing",
    "Power", "Other",
]


def _normalize_category(raw_term: str) -> str:
    key = raw_term.strip().lower()
    return CATEGORY_MAP.get(key, "Other")


def _fetch_html_requests(url: str) -> Optional[str]:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200 and resp.text:
            return resp.text
    except requests.RequestException:
        pass
    return None


def _fetch_html_playwright(url: str) -> Optional[str]:
    """Fallback renderer used only if plain requests HTML lacks listing cards."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=HEADERS["User-Agent"])
                page.goto(url, timeout=45000, wait_until="networkidle")
                page.wait_for_timeout(1500)
                html = page.content()
            finally:
                browser.close()
            return html
    except Exception:
        return None


def _get_page_html(url: str) -> Optional[str]:
    """Get HTML for a listing page, using requests first, Playwright as fallback."""
    html = _fetch_html_requests(url)
    if html and "dms-listing-sri" in html:
        return html
    # Fallback: server-rendered fetch missing cards -> try JS rendering.
    html_js = _fetch_html_playwright(url)
    if html_js and "dms-listing-sri" in html_js:
        return html_js
    # Return whatever we have (may be None or missing cards); caller handles it.
    return html or html_js


def _discover_total_pages(soup: BeautifulSoup) -> int:
    """Read the numbered pagination widget on page 1 to find total page count."""
    max_page = 1
    for a in soup.select("a.page-numbers[data-page], span.page-numbers[data-page]"):
        try:
            n = int(a.get("data-page", "1"))
            max_page = max(max_page, n)
        except (TypeError, ValueError):
            continue
    # Also check plain numeric text on page-numbers anchors as a fallback.
    for a in soup.select("a.page-numbers"):
        text = a.get_text(strip=True)
        if text.isdigit():
            max_page = max(max_page, int(text))
    return max_page


def _extract_price(card) -> Optional[int]:
    price_div = card.select_one(".dms-listing-price")
    if not price_div:
        return None
    text = price_div.get_text(" ", strip=True)
    nums = re.findall(r"[\d,]{4,}", text)
    if not nums:
        return None
    values = [int(n.replace(",", "")) for n in nums]
    # When two prices are shown (e.g. "$499,950 $479,950" = was/now), the
    # second one is the current/sale asking price -- use the last value.
    return values[-1]


def _extract_condition(card) -> str:
    """Returns 'new' or 'used' based on the per-card condition label."""
    text = card.get_text("|", strip=True)
    parts = text.split("|")
    for p in parts[:6]:
        low = p.strip().lower()
        if low == "new":
            return "new"
        if low in ("preowned", "pre-owned", "used"):
            return "used"
    return "unknown"


def _extract_title(card) -> Optional[str]:
    text = card.get_text("|", strip=True)
    parts = text.split("|")
    for i, p in enumerate(parts):
        low = p.strip().lower()
        if low in ("new", "preowned", "pre-owned", "used") and i + 1 < len(parts):
            candidate = parts[i + 1].strip()
            if candidate:
                return candidate
    return None


def _extract_brand(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    for make in KNOWN_MAKES:
        if title.lower().startswith(make.lower()):
            return make
    # Fallback: first token of the title.
    tokens = title.split()
    return tokens[0] if tokens else None


def _parse_category_dropdown(soup: BeautifulSoup) -> Counter:
    """Read aggregate category counts from the sidebar filter's Category dropdown."""
    counts: Counter = Counter()
    select = soup.select_one("select[data-listing-category='category']")
    if not select:
        return counts
    for option in select.find_all("option"):
        term = option.get("data-term")
        total = option.get("data-total")
        if not term or not total:
            continue
        try:
            total_i = int(total)
        except ValueError:
            continue
        normalized = _normalize_category(term)
        counts[normalized] += total_i
    return counts


def scrape() -> dict:
    result = {
        "id": DEALER_ID,
        "name": DEALER_NAME,
        "total": 0,
        "new": 0,
        "used": 0,
        "category": {},
        "brand": {},
        "prices": [],
        "priceLow": None,
        "priceHigh": None,
    }

    first_html = _get_page_html(BASE_URL)
    if not first_html:
        return result

    first_soup = BeautifulSoup(first_html, "lxml")
    total_pages = _discover_total_pages(first_soup)

    all_cards = []
    all_cards.extend(first_soup.select("li.dms-listing-sri"))

    seen_ids = {c.get("data-listing-id") for c in all_cards}

    for page_num in range(2, min(total_pages, MAX_PAGES_SAFETY) + 1):
        page_url = f"{BASE_URL}page/{page_num}/"
        html = _get_page_html(page_url)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("li.dms-listing-sri")
        for c in cards:
            cid = c.get("data-listing-id")
            if cid and cid in seen_ids:
                continue
            if cid:
                seen_ids.add(cid)
            all_cards.append(c)

    # Per-listing extraction: condition (new/used), brand, price.
    new_count = 0
    used_count = 0
    brand_counter: Counter = Counter()
    prices: list = []

    for card in all_cards:
        condition = _extract_condition(card)
        if condition == "new":
            new_count += 1
        elif condition == "used":
            used_count += 1

        title = _extract_title(card)
        brand = _extract_brand(title)
        if brand:
            brand_counter[brand] += 1

        price = _extract_price(card)
        if price is not None:
            status = condition if condition in ("new", "used") else "unknown"
            prices.append((price, status))

    # Aggregate category counts from the sidebar filter widget (page 1 is
    # sufficient since it reflects the full inventory, not just this page).
    category_counter = _parse_category_dropdown(first_soup)

    total = len(all_cards)

    result["total"] = total
    result["new"] = new_count
    result["used"] = used_count
    result["category"] = dict(category_counter)
    result["brand"] = dict(brand_counter)
    result["prices"] = prices

    if prices:
        price_values = [p[0] for p in prices]
        result["priceLow"] = min(price_values)
        result["priceHigh"] = max(price_values)
    else:
        result["priceLow"] = None
        result["priceHigh"] = None

    return result


if __name__ == "__main__":
    import json
    print(json.dumps(scrape(), indent=2))
