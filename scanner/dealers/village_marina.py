"""
Scraper for Village Marina & Yacht Club (Lake Ozark, MO).

Site: https://www.villagemarina.com/default.asp?page=xAllInventory

Site tech notes
----------------
The site runs on a classic "default.asp?page=xAllInventory"-style CMS. The
inventory listing markup itself is fully server-rendered plain HTML (no JS
execution needed to see boats, prices, or attributes) -- confirmed by
inspecting the raw response body, which already contains every listing's
data-* attributes and price text.

HOWEVER, the site sits behind Cloudflare bot management that issues a "Just a
moment..." JS challenge to plain `requests`/`urllib`-style HTTP clients (no
real browser TLS/JS fingerprint) essentially every time, returning HTTP 403
with a challenge page instead of the real inventory HTML. A bare `curl` was
observed to pass most of the time (different TLS fingerprint than Python's
`requests`), but that is not a dependency we can rely on / ship in a
GitHub Actions Python job per the task's allowed-package constraints, and it
is not guaranteed to stay reliable. Playwright (real headless Chromium) was
verified to reliably render past the Cloudflare challenge and get the true
inventory HTML.

Fetch strategy implemented here:
  1. Try a plain `requests.get()` first (cheap/fast) in case Cloudflare is not
     actively challenging this particular request.
  2. If that response looks like a Cloudflare challenge page (HTTP 403 and/or
     "Just a moment" / "cf-chl" markers, or simply missing the expected
     `data-unit-id` listing markers), fall back to fetching the same URL with
     Playwright's sync API (headless Chromium), which executes the challenge
     JS like a real browser and returns the fully rendered inventory HTML.
  3. All subsequent HTML parsing (listing extraction, pricing, pagination) is
     identical regardless of which fetch path produced the HTML string.

Each boat listing sits inside a `<li class="v7list-results__item" ...>` (or a
similarly classed element in older markup) that carries convenient data-*
attributes directly in the HTML:

    data-unit-id="19141988"
    data-unit-condition="NEW"            (or "USED")
    data-unit-year="2027"
    data-unit-make="Cobalt Boats"
    data-unit-model="R33"
    data-unit-category="Boat"
    data-unit-subcategory="Bowrider"

Pricing sits in a nearby `<span class="vehicle-price vehicle-price--current">`
block. Two variants were observed:
  - `<span class="vehicle-price__label">Our Price</span>` followed by
    `<span class="vehicle-price__price">$469,000</span>` -> real numeric price.
  - `<span class="vehicle-price__label">Click for a Quote</span>` with no
    numeric price -> unit has no public price ("unknown").

Pagination is simple query-string pagination: `&pg=2`, `&pg=3`, ... A "Next
page" link (`rel="next"`) is present in the page footer/nav while more pages
remain; it disappears (or repeats the current page) once the last page is
reached. We follow it defensively up to a generous page cap, and also stop as
soon as a page returns zero listings or repeats unit IDs we've already seen.

Playwright (sync API, headless Chromium) is required as the primary/fallback
fetch mechanism to get past the Cloudflare challenge described above.
`requests` is still attempted first as a fast path. Both `BeautifulSoup`
(lxml parser) and light regex are used for parsing once we have real HTML.
"""

from __future__ import annotations

import html
import re
import sys
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

DEALER_ID = "village-marina"
DEALER_NAME = "Village Marina & Yacht Club"
BASE_URL = "https://www.villagemarina.com/default.asp"
INVENTORY_PARAMS = {"page": "xAllInventory"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

MAX_PAGES = 15  # generous safety cap; real site has ~1-2 pages of results
REQUEST_TIMEOUT = 30
RETRY_COUNT = 3
RETRY_SLEEP_SECONDS = 2
REQUESTS_FASTPATH_ATTEMPTS = 1  # requests is reliably Cloudflare-blocked here; don't waste time retrying it

# Regex fallback for pulling the data-unit-* attributes off a listing element,
# used as a backstop alongside BeautifulSoup attribute access (some legacy
# ASP markup has been observed with irregular attribute ordering/whitespace).
UNIT_ITEM_RE = re.compile(
    r'data-unit-id="(?P<id>\d+)"\s*data-unit-condition="(?P<condition>[A-Za-z]*)"'
    r'\s*data-unit-year="(?P<year>\d*)"\s*data-unit-make="(?P<make>[^"]*)"'
    r'\s*data-unit-model="(?P<model>[^"]*)"\s*data-unit-category="(?P<category>[^"]*)"'
    r'\s*data-unit-subcategory="(?P<subcategory>[^"]*)"',
)

# Map raw dealer subcategory strings (HTML-unescaped) -> normalized category
# buckets required by the scanner schema.
CATEGORY_MAP = {
    "bowrider": "Bowrider",
    "deck boat": "Pontoon / Tritoon",
    "pontoon": "Pontoon / Tritoon",
    "tritoon": "Pontoon / Tritoon",
    "ski/wakeboard/wakesurf": "Wakeboard / Ski",
    "ski & wakeboard": "Wakeboard / Ski",
    "wakeboard": "Wakeboard / Ski",
    "center console": "Center Console",
    "cruiser (power)": "Cruiser / Cabin",
    "express cruiser": "Cruiser / Cabin",
    "cabin cruiser": "Cruiser / Cabin",
    "flybridge": "Cruiser / Cabin",
    "cuddy cabin": "Cruiser / Cabin",
    "walkaround": "Cruiser / Cabin",
    "yacht": "Yacht",
    "motor yacht": "Yacht",
    "fishing": "Fishing",
    "bass boat": "Fishing",
    "sport fishing": "Fishing",
    "offshore": "Fishing",
    "performance": "Performance",
    "high performance": "Performance",
    "jet boat": "Performance",
    "personal watercraft": "Performance",
    "pwc": "Performance",
    "power": "Power",
    "runabout": "Power",
    "recreational": "Other",
    "deck": "Power",
}

NORMALIZED_CATEGORIES = {
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


def _normalize_category(raw: Optional[str]) -> str:
    """Map a raw dealer subcategory string onto the fixed schema buckets."""
    if not raw:
        return "Other"
    cleaned = html.unescape(raw).strip().lower()
    if not cleaned:
        return "Other"
    if cleaned in CATEGORY_MAP:
        return CATEGORY_MAP[cleaned]
    # loose contains-based matching for variants not explicitly listed
    if "bowrider" in cleaned:
        return "Bowrider"
    if "pontoon" in cleaned or "tritoon" in cleaned or "deck boat" in cleaned:
        return "Pontoon / Tritoon"
    if "wake" in cleaned or "ski" in cleaned:
        return "Wakeboard / Ski"
    if "center console" in cleaned or "cc" == cleaned:
        return "Center Console"
    if "cruiser" in cleaned or "cabin" in cleaned or "flybridge" in cleaned or "walkaround" in cleaned:
        return "Cruiser / Cabin"
    if "yacht" in cleaned:
        return "Yacht"
    if "fish" in cleaned or "bass" in cleaned:
        return "Fishing"
    if "performance" in cleaned or "jet boat" in cleaned or "pwc" in cleaned:
        return "Performance"
    if "power" in cleaned or "runabout" in cleaned:
        return "Power"
    return "Other"


def _normalize_condition(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    cleaned = raw.strip().lower()
    if cleaned.startswith("new"):
        return "new"
    if cleaned.startswith("used") or cleaned.startswith("pre"):
        return "used"
    return "unknown"


# A few brand names appear on-site with inconsistent suffixes/casing across
# different listings (e.g. some Cobalt units are tagged "Cobalt Boats", others
# just "Cobalt"). Normalize known variants onto one canonical brand label so
# counts aren't needlessly split.
BRAND_ALIASES = {
    "cobalt boats": "Cobalt",
    "cobalt": "Cobalt",
    "formula": "Formula",
    "aviara": "Aviara",
    "tahoe\u00ae": "Tahoe",
    "tahoe": "Tahoe",
}


def _clean_brand(raw: Optional[str]) -> str:
    if not raw:
        return "Unknown"
    cleaned = html.unescape(raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned or cleaned.upper() == "UNKNOWN":
        return "Unknown"
    alias = BRAND_ALIASES.get(cleaned.lower())
    if alias:
        return alias
    return cleaned


CHALLENGE_MARKERS = ("just a moment", "cf-chl", "cf_chl", "/cdn-cgi/challenge-platform")

PLAYWRIGHT_NAV_TIMEOUT_MS = 60000


def _looks_like_challenge_or_empty(text: Optional[str], status_code: Optional[int] = None) -> bool:
    """Heuristic: does this response look like a Cloudflare challenge page
    (or otherwise lack real inventory content) rather than the real page?"""
    if not text:
        return True
    if status_code is not None and status_code != 200:
        return True
    lowered = text.lower()
    if any(marker in lowered for marker in CHALLENGE_MARKERS):
        return True
    # Real inventory pages always contain at least the results list wrapper,
    # even on an empty results page (page 1 should never be truly empty for
    # this dealer, but as a floor check: challenge pages are tiny and never
    # contain this marker at all).
    if "data-unit-id" not in text and "v7list-results" not in text:
        return True
    return False


def _fetch_with_requests(url: str) -> Optional[str]:
    """Fast-path GET via `requests`. Returns text, or None if it fails or the
    response looks like a Cloudflare challenge page."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        print(f"[village_marina] requests fetch error for {url}: {exc}", file=sys.stderr)
        return None
    if _looks_like_challenge_or_empty(resp.text, resp.status_code):
        return None
    return resp.text


_PLAYWRIGHT_STATE = {"pw": None, "browser": None, "context": None}


def _get_playwright_context():
    """Lazily start a single shared Playwright browser/context for reuse
    across paginated fetches (much cheaper than relaunching per page)."""
    if _PLAYWRIGHT_STATE["context"] is not None:
        return _PLAYWRIGHT_STATE["context"]
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(user_agent=USER_AGENT)
    _PLAYWRIGHT_STATE["pw"] = pw
    _PLAYWRIGHT_STATE["browser"] = browser
    _PLAYWRIGHT_STATE["context"] = context
    return context


def _close_playwright():
    if _PLAYWRIGHT_STATE["browser"] is not None:
        try:
            _PLAYWRIGHT_STATE["browser"].close()
        except Exception:
            pass
    if _PLAYWRIGHT_STATE["pw"] is not None:
        try:
            _PLAYWRIGHT_STATE["pw"].stop()
        except Exception:
            pass
    _PLAYWRIGHT_STATE["pw"] = None
    _PLAYWRIGHT_STATE["browser"] = None
    _PLAYWRIGHT_STATE["context"] = None


def _fetch_with_playwright(url: str) -> Optional[str]:
    """Fetch a URL with headless Chromium (Playwright sync API), letting it
    execute Cloudflare's JS challenge like a real browser would."""
    try:
        context = _get_playwright_context()
        page = context.new_page()
        try:
            page.goto(url, timeout=PLAYWRIGHT_NAV_TIMEOUT_MS, wait_until="load")
            # Give any late challenge-resolution JS a moment to finish and
            # redirect/render the real content.
            page.wait_for_timeout(1500)
            content = page.content()
            if _looks_like_challenge_or_empty(content):
                # one more short wait in case the challenge was still resolving
                page.wait_for_timeout(3000)
                content = page.content()
            return content
        finally:
            page.close()
    except Exception as exc:
        print(f"[village_marina] playwright fetch error for {url}: {exc}", file=sys.stderr)
        return None


def _fetch(url: str, params: Optional[dict] = None) -> Optional[str]:
    """Fetch a URL's HTML, trying plain `requests` first and falling back to
    Playwright (headless Chromium) if the site's Cloudflare challenge blocks
    the plain HTTP request. Retries each stage a few times before giving up.
    """
    full_url = url
    if params:
        full_url = f"{url}?{requests.compat.urlencode(params)}"

    last_err = None
    for attempt in range(1, REQUESTS_FASTPATH_ATTEMPTS + 1):
        text = _fetch_with_requests(full_url)
        if text is not None:
            return text
        last_err = "requests fetch blocked/challenged or failed"
        if attempt < REQUESTS_FASTPATH_ATTEMPTS:
            time.sleep(RETRY_SLEEP_SECONDS)

    # Fall back to Playwright, which can execute the Cloudflare JS challenge.
    for attempt in range(1, RETRY_COUNT + 1):
        text = _fetch_with_playwright(full_url)
        if text is not None and not _looks_like_challenge_or_empty(text):
            return text
        last_err = "playwright fetch blocked/challenged or failed"
        if attempt < RETRY_COUNT:
            time.sleep(RETRY_SLEEP_SECONDS)

    print(f"[village_marina] failed to fetch {full_url}: {last_err}", file=sys.stderr)
    return None


def _extract_price(item_html: str) -> Optional[int]:
    """Pull a numeric price (int, dollars) out of a listing's HTML block.

    Looks specifically for the 'Our Price' / real-price span; listings whose
    only price element says things like 'Click for a Quote' or 'Call for
    Price' have no numeric price and correctly yield None.
    """
    # Restrict search to the first price-group block within this item to
    # avoid accidentally grabbing monthly-payment or msrp figures elsewhere.
    price_block_match = re.search(
        r'vehicle-price__label">\s*([^<]*)</span>\s*<span[^>]*class="vehicle-price__price[^"]*">\s*([^<]+)</span>',
        item_html,
    )
    if not price_block_match:
        return None
    label = price_block_match.group(1).strip().lower()
    value_text = price_block_match.group(2)
    if "call" in label or "quote" in label or "contact" in label:
        return None
    digits = re.sub(r"[^\d]", "", value_text)
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def _extract_listing_html_blocks(page_html: str) -> list:
    """Split page HTML into per-listing chunks for scoped regex/price search.

    Uses the `v7list-results__item` (or generic li item with data-unit-id)
    boundaries. Falls back to whole-page search if no boundaries are found.
    """
    # Find start offsets of every listing item by its data-unit-id attribute.
    starts = [m.start() for m in re.finditer(r'data-unit-id="\d+"', page_html)]
    if not starts:
        return []
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(page_html)
        # Walk back a little to capture the opening tag containing this attr.
        block_start = page_html.rfind("<li", 0, start)
        if block_start == -1:
            block_start = max(0, start - 200)
        blocks.append(page_html[block_start:end])
    return blocks


def _parse_page(page_html: str) -> list:
    """Parse one inventory page's HTML into a list of listing dicts."""
    listings = []
    blocks = _extract_listing_html_blocks(page_html)
    for block in blocks:
        attr_match = UNIT_ITEM_RE.search(block)
        if not attr_match:
            # Try a looser attribute-order-independent extraction as backup.
            def _grab(attr):
                m = re.search(rf'data-unit-{attr}="([^"]*)"', block)
                return m.group(1) if m else ""

            unit_id = _grab("id")
            condition = _grab("condition")
            make = _grab("make")
            subcategory = _grab("subcategory")
            if not unit_id:
                continue
        else:
            unit_id = attr_match.group("id")
            condition = attr_match.group("condition")
            make = attr_match.group("make")
            subcategory = attr_match.group("subcategory")

        price = _extract_price(block)
        listings.append(
            {
                "id": unit_id,
                "condition": _normalize_condition(condition),
                "brand": _clean_brand(make),
                "category": _normalize_category(subcategory),
                "price": price,
            }
        )
    return listings


def _find_next_page_url(page_html: str, current_url: str) -> Optional[str]:
    """Locate a 'next page' link in pagination controls, if present."""
    soup = BeautifulSoup(page_html, "lxml")
    next_link = soup.select_one('a[rel="next"]')
    if next_link and next_link.get("href"):
        href = next_link["href"]
        return requests.compat.urljoin(current_url, html.unescape(href))
    return None


def scrape() -> dict:
    """Scrape Village Marina & Yacht Club's full inventory listing.

    Fetches page 1 of the ASP-rendered inventory listing (server-rendered
    HTML, no JS required), then follows `&pg=N` pagination links until no
    further "next page" link is found, a page yields zero new unit IDs, or a
    safety page cap is reached.
    """
    seen_ids = set()
    all_listings = []

    first_url = f"{BASE_URL}?{requests.compat.urlencode(INVENTORY_PARAMS)}"
    current_url = first_url
    page_num = 1

    try:
        while current_url and page_num <= MAX_PAGES:
            page_html = _fetch(current_url)
            if not page_html:
                break

            page_listings = _parse_page(page_html)
            new_count = 0
            for listing in page_listings:
                if listing["id"] in seen_ids:
                    continue
                seen_ids.add(listing["id"])
                all_listings.append(listing)
                new_count += 1

            if new_count == 0 and page_num > 1:
                # No new listings on this page -> we've looped or hit the end.
                break

            next_url = _find_next_page_url(page_html, current_url)
            if not next_url or next_url == current_url:
                break

            current_url = next_url
            page_num += 1
            time.sleep(0.5)  # be polite between page fetches
    finally:
        _close_playwright()

    total = len(all_listings)
    new_count_total = sum(1 for l in all_listings if l["condition"] == "new")
    used_count_total = sum(1 for l in all_listings if l["condition"] == "used")

    category_counts: dict = {}
    for cat in NORMALIZED_CATEGORIES:
        category_counts[cat] = 0
    for listing in all_listings:
        category_counts[listing["category"]] = category_counts.get(listing["category"], 0) + 1
    # drop zero-count categories to keep output tidy
    category_counts = {k: v for k, v in category_counts.items() if v > 0}

    brand_counts: dict = {}
    for listing in all_listings:
        brand_counts[listing["brand"]] = brand_counts.get(listing["brand"], 0) + 1

    # `prices` only contains listings with a real, extractable numeric price
    # (per the spec's tuple shape (price_int, condition)); listings whose only
    # price element is "Call for Price" / "Click for a Quote" have no numeric
    # price and are simply omitted here (they still count toward total/new/used).
    prices = [
        (listing["price"], listing["condition"])
        for listing in all_listings
        if listing["price"] is not None
    ]

    numeric_prices = [p for p, _ in prices]
    price_low = min(numeric_prices) if numeric_prices else None
    price_high = max(numeric_prices) if numeric_prices else None

    return {
        "id": DEALER_ID,
        "name": DEALER_NAME,
        "total": total,
        "new": new_count_total,
        "used": used_count_total,
        "category": category_counts,
        "brand": brand_counts,
        "prices": prices,
        "priceLow": price_low,
        "priceHigh": price_high,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(scrape(), indent=2))
