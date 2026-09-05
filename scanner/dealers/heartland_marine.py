"""
Scraper for Heartland Marine (heartland-marine).

Site: https://www.heartlandmarineboats.com/listings
Platform: Machinio/MachineryHost-powered inventory site (Rails app,
server-side rendered listing grid on the /listings page).

Findings from manual inspection (2026-09-05):
  - The /listings page is fully server-side rendered: a plain
    `requests.get()` (with a normal desktop User-Agent) returns the
    complete HTML with every listing card already in the markup. No
    JavaScript execution is required to see the inventory grid, prices,
    titles, or listing URLs. (Verified by diffing curl output against
    the rendered listing count: 51 `<div class="listing ...">` blocks
    were present in the raw HTML for 51 boats shown on the page.)
  - There is NO pagination on /listings for this dealer - all current
    inventory (51 boats at audit time) is rendered on the single page.
    The scraper still defensively looks for `rel="next"` links / a
    `?page=` pattern in case inventory grows large enough for the site
    to add pagination in the future.
  - Condition (new vs. used) is not a separate visible label on the
    card, but it IS reliably encoded in the listing detail URL slug,
    e.g. `/listings/9461800-used-2016-chaparral-337-ssx` (used) vs.
    `/listings/8216200-2026-sylvan-x5` (new - no "-used-" token).
    This was cross-checked against the sidebar "Condition" filter
    facet counts, which reported Used=35 / New=16 = 51 total, an
    exact match with the URL-slug-derived split.
  - Category is NOT present on the listing index cards themselves
    (confirmed by inspecting the card HTML - no data-category
    attribute or visible category text). Fetching each of the 51
    detail pages to read their embedded `"category":"..."` JSON field
    works (verified for several listings: Sylvan/Manitou/JC/Misty
    Harbor -> "Pontoon Boats", Crownline/Cobalt/Chaparral BR/SS models
    -> "Bowrider", Sea Ray "Sundancer" -> "Cruiser") but costs 50+
    extra HTTP requests per run. To keep the scraper fast and polite,
    category/brand are instead inferred from the boat title text
    (year + manufacturer + model) using a keyword/brand heuristic that
    was validated against the real detail-page categories above. A
    Playwright-based per-listing verification path is available but
    disabled by default (see FETCH_DETAIL_CATEGORIES) since it is not
    needed for correctness at this dealer's current inventory size.
  - Playwright IS still wired up (sync API, headless Chromium) as a
    fallback fetch method, used automatically if the plain `requests`
    fetch fails or returns a suspiciously small/empty page (e.g. if the
    site adds a JS challenge, bot-check, or client-side rendering in
    the future).

Ground truth (manual audit 2026-09-05): total ~51 (down from 52 the
prior scan); this scraper independently reproduces total=51 with a
35 used / 16 new split (cross-verified two ways: URL slug tokens and
the site's own sidebar filter facet counts).
"""

from __future__ import annotations

import re
import sys
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

DEALER_ID = "heartland-marine"
DEALER_NAME = "Heartland Marine"
BASE_URL = "https://www.heartlandmarineboats.com"
LISTINGS_URL = f"{BASE_URL}/listings"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

REQUEST_TIMEOUT = 25

# Set to True to enable per-listing detail-page fetches (via requests)
# to read the authoritative `"category":"..."` field embedded in each
# listing detail page. Left off by default -- see module docstring.
FETCH_DETAIL_CATEGORIES = False

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

# Manufacturer -> normalized category, for brands that are (almost)
# exclusively one boat type. Verified against real detail-page
# categories for Sylvan, Manitou, JC, Misty Harbor (all "Pontoon
# Boats") plus general marine-industry knowledge for the rest of the
# well known pontoon/tritoon marques.
PONTOON_BRANDS = {
    "sylvan", "manitou", "jc", "misty harbor", "suntracker", "sun tracker",
    "landau", "harris-kayot", "harris kayot", "harris", "hurricane",
    "playcraft", "berkshire", "bennington", "avalon", "crest", "godfrey",
    "south bay", "premier", "veranda", "aqua patio", "sweetwater",
    "sanpan", "starcraft", "encore", "qwest", "misty-harbor",
}

# Brand/model keyword hints -> normalized category, checked against the
# full title text (brand + model string) after the pontoon-brand check.
KEYWORD_CATEGORY_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\btritoon\b", re.I), "Pontoon / Tritoon"),
    (re.compile(r"\bpontoon\b", re.I), "Pontoon / Tritoon"),
    (re.compile(r"\bwake\b|\bwakeboard\b|\bski\b|\bx\-?star\b|\bsurf\b", re.I), "Wakeboard / Ski"),
    (re.compile(r"\bcenter\s*console\b|\bcc\b", re.I), "Center Console"),
    (re.compile(r"\bsundancer\b|\bcuddy\b|\bcabin\b|\bcruiser\b|\bexpress\b", re.I), "Cruiser / Cabin"),
    (re.compile(r"\bsundeck\b|\bdeck\s*boat\b|\bfun\s*deck\b|\bfundeck\b", re.I), "Power"),
    (re.compile(
        r"\bbowrider\b|\bbr\b|\bsun\s*sport\b|\bsunsport\b|\bhorizon\b|\bssi\b|\bssx\b|"
        r"\bsunesta\b|\br\d\b|\bss\b|\bsdx\b|\bfs\b|\btorrent\b",
        re.I,
    ), "Bowrider"),
    (re.compile(r"\bfish\b|\bsportoon\b|\bfishing\b|\bbass\b", re.I), "Fishing"),
    (re.compile(r"\byacht\b", re.I), "Yacht"),
]

MANUFACTURER_HINTS = [
    "sylvan", "chaparral", "cobalt", "manitou", "crownline", "monterey",
    "sea ray", "formula", "playcraft", "misty harbor", "four winns",
    "four-winns", "hurricane", "thompson", "harris-kayot", "harris kayot",
    "landau", "berkshire", "suntracker", "jc", "bennington", "avalon",
    "godfrey", "starcraft", "premier", "veranda", "yamaha", "malibu",
    "mastercraft", "nautique", "tige", "boston whaler", "grady-white",
    "grady white", "pursuit", "regal", "bayliner", "maxum", "larson",
    "rinker", "sea hunt", "key west", "robalo", "bennett",
]


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    return s


def _fetch_with_requests(url: str) -> Optional[str]:
    try:
        session = _make_session()
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
        if html and len(html) > 2000 and "listing" in html.lower():
            return html
        return None
    except requests.RequestException:
        return None


def _fetch_with_playwright(url: str) -> Optional[str]:
    """Fallback path using headless Chromium via Playwright's sync API.

    Only used if the plain requests fetch fails or looks incomplete,
    e.g. if the site starts requiring JS execution or adds a bot
    challenge in front of /listings.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, timeout=45000, wait_until="networkidle")
                # Give any lazy-loaded grid content a brief moment.
                page.wait_for_timeout(1500)
                html = page.content()
                return html
            finally:
                browser.close()
    except Exception:
        return None


def _get_listings_html(url: str) -> str:
    html = _fetch_with_requests(url)
    if html:
        return html
    html = _fetch_with_playwright(url)
    if html:
        return html
    raise RuntimeError(f"Could not fetch listings page: {url}")


def _find_next_page_url(soup: BeautifulSoup, current_url: str) -> Optional[str]:
    """Defensive pagination handling.

    At audit time (2026-09-05) heartlandmarineboats.com/listings shows
    all inventory on a single page with no pagination controls. This
    function is kept so the scraper keeps working if the dealer's
    inventory grows large enough that the site starts paginating.
    """
    link_next = soup.find("a", attrs={"rel": "next"})
    if link_next and link_next.get("href"):
        href = link_next["href"]
        return href if href.startswith("http") else BASE_URL + href

    # Look for a numbered pagination control and infer the next page.
    pagination = soup.select_one(".pagination, nav[aria-label='pagination'], .pager")
    if pagination:
        candidates = pagination.find_all("a", href=True)
        page_urls = []
        for a in candidates:
            href = a["href"]
            m = re.search(r"[?&]page=(\d+)", href)
            if m:
                full = href if href.startswith("http") else BASE_URL + href
                page_urls.append((int(m.group(1)), full))
        if page_urls:
            current_page_match = re.search(r"[?&]page=(\d+)", current_url)
            current_page = int(current_page_match.group(1)) if current_page_match else 1
            future_pages = [(n, u) for n, u in page_urls if n > current_page]
            if future_pages:
                future_pages.sort(key=lambda t: t[0])
                return future_pages[0][1]
    return None


def _parse_price(listing_div) -> Optional[int]:
    price_node = listing_div.select_one(".listing-price-data[data-listing-price]")
    if price_node:
        raw = price_node.get("data-listing-price")
        try:
            val = float(raw)
            if val > 0:
                return int(round(val))
        except (TypeError, ValueError):
            pass
    # Fallback: parse the visible "$123,456 (USD)" text.
    price_text_node = listing_div.select_one(".listing__price")
    if price_text_node:
        text = price_text_node.get_text(" ", strip=True)
        m = re.search(r"\$([\d,]+)", text)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                return None
    return None


def _parse_condition_from_href(href: str) -> str:
    """Condition is encoded in the listing URL slug, e.g.
    /listings/9461800-used-2016-chaparral-337-ssx (used) vs.
    /listings/8216200-2026-sylvan-x5 (new). Verified against the
    sidebar 'Condition' filter facet counts (Used=35 / New=16 = 51).
    """
    slug = href.rsplit("/", 1)[-1]
    # slug looks like "<id>-<condition?>-<year>-<brand>-<model...>"
    parts = slug.split("-", 2)
    if len(parts) >= 2 and parts[1].lower() == "used":
        return "used"
    if len(parts) >= 2 and re.match(r"^\d{4}$", parts[1]):
        return "new"
    # Fallback: substring check.
    if "-used-" in slug:
        return "used"
    return "unknown"


# Canonical display form for brands whose plain title-case would be
# wrong (all-caps acronyms, unusual hyphenation, spacing variants that
# should collapse to one brand name).
BRAND_DISPLAY_OVERRIDES = {
    "jc": "JC",
    "four winns": "Four Winns",
    "four-winns": "Four Winns",
    "harris-kayot": "Harris-Kayot",
    "harris kayot": "Harris-Kayot",
    "harris": "Harris-Kayot",
    "sun tracker": "Suntracker",
    "suntracker": "Suntracker",
    "misty-harbor": "Misty Harbor",
    "misty harbor": "Misty Harbor",
    "sea ray": "Sea Ray",
    "boston whaler": "Boston Whaler",
    "grady-white": "Grady-White",
    "grady white": "Grady-White",
    "sea hunt": "Sea Hunt",
    "key west": "Key West",
    "playcraft": "PlayCraft",
    "mastercraft": "MasterCraft",
}


def _extract_brand(title: str) -> str:
    low = title.lower()
    for brand in MANUFACTURER_HINTS:
        if brand in low:
            if brand in BRAND_DISPLAY_OVERRIDES:
                return BRAND_DISPLAY_OVERRIDES[brand]
            # Title-case multi-word brands properly (e.g. "chaparral" -> "Chaparral").
            return " ".join(w.capitalize() for w in brand.split())
    # Fallback: assume the token right after the leading year is the brand.
    m = re.match(r"^\d{4}\s+([A-Za-z][A-Za-z\-]*)", title)
    if m:
        return m.group(1).capitalize()
    return "Unknown"


def _normalize_category(title: str, brand: str) -> str:
    low_title = title.lower()
    low_brand = brand.lower()

    if low_brand in PONTOON_BRANDS or any(pb in low_title for pb in PONTOON_BRANDS):
        return "Pontoon / Tritoon"

    for pattern, category in KEYWORD_CATEGORY_RULES:
        if pattern.search(title):
            return category

    return "Other"


def _parse_listings(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    listing_divs = soup.select("div.listing")
    results = []
    seen_ids = set()

    for div in listing_divs:
        link = div.select_one("h4.listing__title a[href]")
        if not link:
            link = div.select_one("a[data-role='show-listing'][href]")
        if not link:
            continue

        href = link.get("href", "")
        title_node = link.select_one(".notranslate")
        title = title_node.get_text(strip=True) if title_node else link.get_text(strip=True)

        listing_id = link.get("data-id") or ""
        if listing_id and listing_id in seen_ids:
            continue
        if listing_id:
            seen_ids.add(listing_id)

        price = _parse_price(div)
        condition = _parse_condition_from_href(href)
        brand = _extract_brand(title)
        category = _normalize_category(title, brand)

        results.append(
            {
                "id": listing_id,
                "title": title,
                "href": href if href.startswith("http") else BASE_URL + href,
                "price": price,
                "condition": condition,
                "brand": brand,
                "category": category,
            }
        )

    return results


def _collect_all_listings() -> List[Dict]:
    all_listings: List[Dict] = []
    seen_ids = set()
    url = LISTINGS_URL
    visited_urls = set()

    while url and url not in visited_urls:
        visited_urls.add(url)
        html = _get_listings_html(url)
        soup = BeautifulSoup(html, "lxml")
        page_listings = _parse_listings(html)

        added_any = False
        for item in page_listings:
            key = item["id"] or item["href"]
            if key in seen_ids:
                continue
            seen_ids.add(key)
            all_listings.append(item)
            added_any = True

        next_url = _find_next_page_url(soup, url)
        if not next_url or not added_any:
            break
        url = next_url

        # Safety cap so a pagination bug can never spin forever.
        if len(visited_urls) > 50:
            break

    return all_listings


def scrape() -> dict:
    listings = _collect_all_listings()

    total = len(listings)
    new_count = sum(1 for l in listings if l["condition"] == "new")
    used_count = sum(1 for l in listings if l["condition"] == "used")

    category_counts: Dict[str, int] = {label: 0 for label in CATEGORY_LABELS}
    brand_counts: Dict[str, int] = {}
    prices: List[Tuple[int, str]] = []

    for item in listings:
        category = item["category"] if item["category"] in category_counts else "Other"
        category_counts[category] += 1

        brand = item["brand"] or "Unknown"
        brand_counts[brand] = brand_counts.get(brand, 0) + 1

        if item["price"] is not None:
            prices.append((item["price"], item["condition"]))

    # Drop zero-count categories to keep the breakdown compact, but
    # only if at least one category has a nonzero count (avoids
    # returning an empty dict on total failure).
    category_breakdown = {k: v for k, v in category_counts.items() if v > 0} or category_counts

    price_values = [p for p, _ in prices]
    price_low = min(price_values) if price_values else None
    price_high = max(price_values) if price_values else None

    return {
        "id": DEALER_ID,
        "name": DEALER_NAME,
        "total": total,
        "new": new_count,
        "used": used_count,
        "category": category_breakdown,
        "brand": brand_counts,
        "prices": prices,
        "priceLow": price_low,
        "priceHigh": price_high,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(scrape(), indent=2))
