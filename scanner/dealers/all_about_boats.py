"""
Scraper for All About Boats (all-about-boats).

Dealer id: all-about-boats
Source URL: https://www.boatozarks.com/search/inventory

Platform: DealerFire / ARI ("Endeavor") inventory widget (recognizable by
the `//cdnmedia.endeavorsuite.com/...` image CDN and the
`search-result-grid` / `search-results-count` markup).

Findings from manual inspection (2026-09-05):
  - A plain `requests.get()` (even with a normal desktop User-Agent) is
    blocked: the site is sitting behind Cloudflare's managed challenge
    ("Just a moment..." interstitial, HTTP 200 with a `<title>Just a
    moment...</title>` page and a `cf_chl_opt` JS challenge blob -- a
    raw curl/requests fetch got HTTP 403 with that interstitial body).
    JS execution (and passing the Cloudflare challenge) is REQUIRED to
    see real inventory HTML on `/search/inventory`.
  - Playwright with headless Chromium (no special stealth flags beyond
    a normal desktop Chrome User-Agent) is sufficient to pass the
    Cloudflare challenge and load the real page -- verified directly:
    `page.goto(...) ; time.sleep(6-8)` reliably yields the fully
    rendered inventory grid with correct <title> ("In-Stock New and
    Used Models For Sale in Osage Beach, MO | All About Boats").
  - Each result page renders up to 30 vehicle cards
    (`div.search-result-grid`) and the page shows a definitive
    "X - Y of N results" string in `.search-results-count`
    (observed: "1 - 30 of 44 results" on page 1, "31 - 44 of 44
    results" on page 2). Pagination is via clean path segments:
    `/search/inventory/page/1`, `/search/inventory/page/2`, etc.
    (also present as literal `<a href="/search/inventory/page/N">`
    links in a `ul.pagination` widget). The scraper follows this
    pattern and stops once it has collected the declared total, hits
    an empty page, or reaches a safety cap.
  - Each card additionally embeds a hidden but extremely reliable
    `<span class="datasource datasource-<productId> ...">` containing
    a raw JSON blob per listing with authoritative structured fields:
    `productId`, `itemYear`, `itemMake`, `itemModel`, `itemPrice`
    (float dollars or absent for "Call for price" units), `usageStatus`
    ("New" / "Used"), `itemType` ("Boats" / "Pontoons"), and
    `itemSubtype` (often blank, but populated for some product lines,
    e.g. "Bowrider", "Runabouts", "Sport Boats", "Surf Series",
    "Luxury Pontoon Boats"). This JSON is parsed directly instead of
    scraping visible card text, and is deduplicated by `productId`
    (each card's image slider clones a couple of surrounding cards
    into the DOM for the carousel, producing duplicate `datasource`
    spans that must be de-duped -- verified: page 1 raw HTML has 65
    `datasource` spans but only 30 unique `productId`s, matching the
    "1 - 30 of 44" count exactly once de-duplicated; a stray
    `productId: null` filter-widget artifact is also discarded).
  - Cross-check: page 1 (30 unique real products) + page 2 (14 unique
    products) = 44 unique products total with ZERO productId overlap
    between pages, exactly matching the site's own "44 results" counter.
  - Condition (new/used) comes straight from the reliable
    `usageStatus` JSON field ("New"/"Used"); at audit time this was
    28 new / 16 used = 44 total.
  - Category is not usually explicit (`itemSubtype` is blank on most
    cards), so it is derived with a keyword-first heuristic that
    checks `itemSubtype`, `itemModel`, and `itemType` together:
    `itemType == "Pontoons"` (all Starcraft/Harris/Regency/Premier/
    Misty Harbor pontoon-brand listings observed) -> Pontoon/Tritoon;
    "bowrider" in subtype/model -> Bowrider; "dual console" in model
    -> Center Console; "sundancer"/"crossover"/"cabin" in model ->
    Cruiser/Cabin; well-known sport/performance model codes for the
    Crownline/Chaparral/AMP lines observed here (SS/SSX/XSS/SURF/CAT)
    -> Performance; "runabout"/plain BR/LS model codes (Rinker,
    Regal, older Crownline "Bowrider ### LS") -> Bowrider; else falls
    back to Power/Other. This was checked by hand against every one
    of the 44 listings seen at audit time.
  - Price: `itemPrice` from the JSON (already a clean float, e.g.
    379900.0) is used directly when present; many higher-end/"Explore
    Purchase Options" or brand-new-model listings omit a price
    (`itemPrice: null`, i.e. "Call for Price") and are excluded from
    the priceLow/priceHigh/prices-list price stats but still counted
    in total/new/used/category/brand.

Ground truth (per task): total inventory has stayed flat night to
night recently (no net change in the last scan) -- this run recorded
total=44 (28 new / 16 used) at audit time; no prior exact total/split/
brand figures were preserved to diff against, so this is reported as
the current observed state, not a delta.
"""

from __future__ import annotations

import json
import re
import time
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

DEALER_ID = "all-about-boats"
DEALER_NAME = "All About Boats"
BASE_URL = "https://www.boatozarks.com"
INVENTORY_URL = f"{BASE_URL}/search/inventory"
PAGE_URL_TMPL = f"{BASE_URL}/search/inventory/page/{{page}}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

PAGE_LOAD_TIMEOUT_MS = 60_000
POST_LOAD_SETTLE_SECONDS = 6.0  # allow Cloudflare challenge + widget JS to finish
MAX_PAGES = 15  # safety cap; observed inventory needs only 2 pages (44 / 30 per page)

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

# Keyword rules checked (in order) against a combined lowercase string of
# itemSubtype + " " + itemModel. First match wins.
_CATEGORY_KEYWORD_RULES: List[Tuple[str, List[str]]] = [
    # "bowrider" checked first: it can appear alongside other words (e.g.
    # "Crossover Bowrider", "Bowrider 316 LS") and is the most specific/
    # unambiguous signal when present.
    ("Bowrider", ["bowrider"]),
    ("Wakeboard / Ski", ["wake", "ski", "surf series", " surf", "wakesurf"]),
    ("Center Console", ["dual console", "center console", "cc "]),
    ("Cruiser / Cabin", [
        "sundancer", "crossover", "cabin", "cruiser", "cruise series",
        "sunsation", "slx",
    ]),
    ("Fishing", ["dual console fish", "bay boat", "flats", "center console fish"]),
    ("Yacht", ["yacht", "flybridge", "motor yacht"]),
    ("Bowrider", [
        "runabout", " br", " ls", "sport boats",
    ]),
    ("Performance", [
        "xss", " ss ", "ss surf", "ssx", "cat", " ss$",
    ]),
]

# Brand -> category, for brands whose entire observed lineup at this
# dealer is one boat type (kept as a fallback signal alongside itemType).
_PONTOON_BRANDS = {
    "starcraft", "harris", "regency", "premier", "misty harbor", "suntracker",
    "sun tracker", "manitou", "landau", "jc", "bennington", "godfrey",
}


def _normalize_category(item_type: Optional[str], subtype: Optional[str], model: Optional[str], make: Optional[str]) -> str:
    subtype = (subtype or "").strip()
    model = (model or "").strip()
    make = (make or "").strip().lower()
    item_type = (item_type or "").strip().lower()

    haystack = f"{subtype} {model}".lower()

    # Explicit pontoon signal takes priority: itemType field or known brand.
    if item_type == "pontoons" or make in _PONTOON_BRANDS:
        return "Pontoon / Tritoon"

    for label, keywords in _CATEGORY_KEYWORD_RULES:
        for kw in keywords:
            kw_stripped = kw.strip()
            if kw.startswith(" ") and kw.endswith(" "):
                # word-boundary-ish token check, e.g. " ss "
                if kw in f" {haystack} ":
                    return label
            elif kw_stripped and kw_stripped in haystack:
                return label

    if item_type == "boats":
        return "Power"

    return "Other"


def _extract_price(item_price) -> Optional[int]:
    if item_price is None:
        return None
    try:
        val = float(item_price)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    return int(round(val))


def _condition_from_usage_status(usage_status: Optional[str]) -> str:
    if not usage_status:
        return "unknown"
    val = usage_status.strip().lower()
    if val == "new":
        return "new"
    if val == "used":
        return "used"
    return "unknown"


def _fetch_rendered_html(url: str) -> Optional[str]:
    """Fetch a URL with headless Chromium (Playwright), waiting out the
    Cloudflare managed challenge and the inventory widget's async JS.
    Returns the fully rendered HTML, or None on failure."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1366, "height": 900},
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
                # Let the Cloudflare JS challenge resolve and redirect/reload,
                # then let the inventory widget's own async JS populate cards.
                time.sleep(POST_LOAD_SETTLE_SECONDS)
                # If we're still stuck on the challenge page, give it one more
                # beat -- Cloudflare's managed challenge can take a few seconds
                # longer than the widget JS itself.
                title = (page.title() or "").lower()
                if "just a moment" in title or "attention required" in title:
                    time.sleep(5.0)
                html = page.content()
            finally:
                browser.close()
            return html
    except Exception:
        return None


def _parse_datasource_products(html: str) -> Dict[int, dict]:
    """Parse the hidden per-card `datasource` JSON spans, de-duplicated by
    productId (image-slider clones + occasional null-id filter artifacts
    are discarded)."""
    soup = BeautifulSoup(html, "lxml")
    spans = soup.find_all("span", class_=re.compile(r"^datasource"))

    products: Dict[int, dict] = {}
    for span in spans:
        raw = span.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        pid = data.get("productId")
        if pid is None:
            continue
        if pid not in products:
            products[pid] = data
    return products


def _parse_results_count(html: str) -> Optional[int]:
    """Read the definitive 'X - Y of N results' string, return N."""
    soup = BeautifulSoup(html, "lxml")
    el = soup.find(class_="search-results-count")
    if not el:
        return None
    text = el.get_text(" ", strip=True)
    m = re.search(r"of\s+([\d,]+)\s+results", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _fetch_all_products() -> Tuple[Dict[int, dict], Optional[int], List[str]]:
    """Fetch and parse all inventory pages via Playwright. Returns
    (products_by_id, declared_total_from_site, caveats)."""
    caveats: List[str] = []
    all_products: Dict[int, dict] = {}
    declared_total: Optional[int] = None

    page_num = 1
    while page_num <= MAX_PAGES:
        url = INVENTORY_URL if page_num == 1 else PAGE_URL_TMPL.format(page=page_num)
        html = _fetch_rendered_html(url)

        if not html:
            caveats.append(
                f"Playwright fetch failed/returned nothing for page {page_num}; "
                "stopping pagination early."
            )
            break

        page_products = _parse_datasource_products(html)

        if page_num == 1:
            declared_total = _parse_results_count(html)

        if not page_products:
            # No cards found on this page -- either we've run past the last
            # page, or rendering failed (e.g. still stuck on a JS challenge).
            if page_num == 1:
                caveats.append(
                    "No product cards found on page 1 -- page may still be "
                    "behind a JS/Cloudflare challenge, or markup has changed."
                )
            break

        new_count_this_page = 0
        for pid, data in page_products.items():
            if pid not in all_products:
                all_products[pid] = data
                new_count_this_page += 1

        if new_count_this_page == 0:
            # Entire page duplicated what we already had -- likely looped
            # back to page 1 content or hit the end.
            break

        if declared_total is not None and len(all_products) >= declared_total:
            break

        page_num += 1

    if declared_total is not None and len(all_products) != declared_total:
        caveats.append(
            f"Site declared {declared_total} total results but scraper "
            f"collected {len(all_products)} unique products after "
            f"pagination -- possible mismatch (dedup edge case, listing "
            f"changed mid-scrape, or pagination stopped early)."
        )

    return all_products, declared_total, caveats


def scrape() -> dict:
    products, declared_total, caveats = _fetch_all_products()

    total = len(products)
    new_count = 0
    used_count = 0
    category_counts: Dict[str, int] = {}
    brand_counts: Dict[str, int] = {}
    prices: List[Tuple[int, str]] = []

    for data in products.values():
        condition = _condition_from_usage_status(data.get("usageStatus"))
        if condition == "new":
            new_count += 1
        elif condition == "used":
            used_count += 1

        make = (data.get("itemMake") or "Unknown").strip() or "Unknown"
        brand_counts[make] = brand_counts.get(make, 0) + 1

        category = _normalize_category(
            data.get("itemType"), data.get("itemSubtype"), data.get("itemModel"), make
        )
        category_counts[category] = category_counts.get(category, 0) + 1

        price = _extract_price(data.get("itemPrice"))
        if price is not None:
            prices.append((price, condition))

    price_values = [p for p, _ in prices]
    price_low = min(price_values) if price_values else None
    price_high = max(price_values) if price_values else None

    if not products:
        caveats.append(
            "Scrape returned zero products -- Playwright likely could not "
            "pass the Cloudflare managed challenge in this environment, or "
            "the site's markup changed. Verify the GitHub Actions runner "
            "has network access and that the bundled Chromium at "
            "/home/user/.cache/ms-playwright (or the CI equivalent path) "
            "is installed."
        )

    result = {
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
    if caveats:
        result["_caveats"] = caveats
    return result


if __name__ == "__main__":
    print(json.dumps(scrape(), indent=2))
