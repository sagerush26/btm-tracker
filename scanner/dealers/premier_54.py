"""
Scraper for Premier 54 Motorsports (Osage Beach / Branson West, MO).

Site: https://premier54.com/boats-for-sale-osage-beach-branson-west-missouri

Site tech notes
----------------
The inventory listing page server-renders the *first* batch of boat cards
(12 on initial pageload, confirmed via a plain `requests.get()`), then relies
on a jQuery "LOAD MORE" button to fetch the remaining pages via AJAX. The
button markup looks like:

    <button data-ajax_url="/boats-for-sale-osage-beach-branson-west-missouri?load_more=1"
            data-current_page="1" data-page_count="8"
            class="load_more_action blueBtn hk1">LOAD MORE</button>

and the inline `<script>` handler wires it up as:

    jQuery.ajax({
        type: "GET",
        url: ajax_url,                      // ".../boats-for-sale-osage-beach-branson-west-missouri?load_more=1"
        data: { current_page: parseFloat(current_page) + 1 },
        success: function(data) {
            jQuery('#boats-list').append(data.result);
            ...
        }
    });

This AJAX endpoint can be replayed directly with `requests` -- no Playwright
/ browser automation is required. A GET to the inventory URL with query
params `load_more=1&current_page=N` (N = 2, 3, 4, ...) returns a small JSON
payload:

    {"current_page": "2", "pageCount": 8, "result": "<div class=\"col-md-6...</div>..."}

`result` is an HTML fragment containing more `.brandInventoryCard` blocks
(the same markup found in the initial page's `#boats-list`). We keep
requesting increasing `current_page` values until `current_page == pageCount`
(as the site's own JS does), which happens at page 8 for a total of 89 cards
as of 2026-09-05.

Each `.brandInventoryCard` looks like:

    <div class="brandInventoryCard">
      <div class="brandInventoryImageContainer">
        <a href="/new-boats-for-sale-detail/2026-premier-250-sunsation-angler-...">
          <img ...>
        </a>
        ...
      </div>
      <div class="brandCardContent">
        <a href="/new-boats-for-sale-detail/...">
          <h4 class="boatCardTitle">2026 Premier 250 Sunsation Angler 6PT</h4>
        </a>
        <p><a class="boatCardPriceTag" href="javascript:void(0)">MSRP: $155,202</a></p>
        <p class="newBoatPricingNote">Call for customer pricing</p>
        <div>
          <ul class="boat-inventory-cond-list">
            <li class="cond-list-item">New</li>
            <li class="cond-list-item">26.5ft</li>
            <li class="cond-list-item">Stock#5042</li>
          </ul>
        </div>
      </div>
    </div>

Condition (new/used):
  The detail-page URL prefix is the most reliable signal and is present on
  100% of the 89 cards observed: `/new-boats-for-sale-detail/...` vs.
  `/used-pre-owned-boats-for-sale-detail/...`. A separate "New"/"Used" badge
  sometimes also appears as the first `<li class="cond-list-item">` inside
  `.boat-inventory-cond-list`, but that badge is only present on a minority
  of cards (~12/89) -- when present, it never disagreed with the URL prefix
  in manual verification, so the URL prefix is used as the primary /
  authoritative signal and the badge only as a secondary fallback for the
  rare card whose link is missing/malformed.

Price:
  `.boatCardPriceTag` text is one of:
    - "$164,900"                -> plain resale/used price
    - "MSRP: $155,202"          -> new-boat MSRP
    - "NAP: $122,823"           -> "National Advertised Price" for new boats
    - "Call For Pricing" (no digits) -> no numeric price available
  We strip any "MSRP:"/"NAP:" prefix and parse the remaining digits.

Brand:
  Not exposed as a discrete field on the card, but every observed title
  begins with "<year> <Brand> <model...>" and the brand always matches one
  of the checkbox values in the page's own "make" filter panel (Chaparral,
  Cobalt, Crownline, Four Winns, Lowe, MB Sports, Manitou, MasterCraft,
  Montara, Nautic Star, Playcraft, Premier, Ranger, Regal, Robalo, Stingray,
  Sun Tracker, Tige, Yamaha). We match the longest known brand name that the
  post-year remainder of the title starts with; if none match we fall back
  to the single word after the year.

Category:
  No explicit category/type field is exposed on the listing card or in the
  page's filter panel (the "category"/"boat_type" hidden inputs are always
  empty on this site). Category is therefore inferred from brand + model
  keywords using conservative substring rules (see `_normalize_category`),
  mapped onto the fixed schema buckets. Anything not confidently matched
  falls back to "Other".

Fallback:
  If the AJAX replay ever stops working (e.g. endpoint renamed, JSON shape
  changed, or the site starts requiring a session/CSRF token), `scrape()`
  falls back to driving a real headless Chromium via Playwright: load the
  page, repeatedly click `.load_more_action` until it disappears / the
  `data-current_page` stops advancing, then parse the fully-populated
  `#boats-list` DOM with the same BeautifulSoup card parser.
"""

from __future__ import annotations

import re
import sys
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

DEALER_ID = "premier-54"
DEALER_NAME = "Premier 54 Motorsports"
BASE_URL = "https://premier54.com/boats-for-sale-osage-beach-branson-west-missouri"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30
RETRY_COUNT = 3
RETRY_SLEEP_SECONDS = 2
MAX_AJAX_PAGES = 40  # generous safety cap; real site currently has 8 pages
PLAYWRIGHT_MAX_CLICKS = 60
PLAYWRIGHT_NAV_TIMEOUT_MS = 45_000

KNOWN_BRANDS = [
    "Chaparral", "Cobalt", "Crownline", "Four Winns", "Lowe", "MB Sports",
    "Manitou", "MasterCraft", "Montara", "Nautic Star", "Playcraft",
    "Premier", "Ranger", "Regal", "Robalo", "Stingray", "Sun Tracker",
    "Tige", "Yamaha",
]
# Longest-first so e.g. "Sun Tracker" is matched before a hypothetical "Sun".
_BRANDS_BY_LEN = sorted(KNOWN_BRANDS, key=len, reverse=True)

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

# Brands that (on this dealer's lineup) are exclusively/primarily a single
# category regardless of model keywords -- used as a fallback when the model
# text itself gives no signal.
BRAND_DEFAULT_CATEGORY = {
    # Premier Marine / Premier Pontoons builds pontoon/tritoon boats
    # exclusively (Sunsation, Solaris, Sunscape, Intrigue, Supersport, etc.
    # are all pontoon model families) -- confirmed via manufacturer site
    # https://www.pontoons.com/models/ and dealer listings e.g.
    # https://www.marinemax.com/boats-for-sale/details/new/premier/230-sunsation/2026/marinemax-crosslake/9068820
    # (class: "Pontoon Boats") and
    # https://www.boattrader.com/boats/make-premier/model-sunsation/
    # ("A powerboat built by Premier, the Sunsation is a pontoon vessel").
    "Premier": "Pontoon / Tritoon",
    "Sun Tracker": "Pontoon / Tritoon",
    "Playcraft": "Pontoon / Tritoon",
    "Manitou": "Pontoon / Tritoon",
    "MB Sports": "Wakeboard / Ski",
    "MasterCraft": "Wakeboard / Ski",
    "Tige": "Wakeboard / Ski",
    "Ranger": "Fishing",
    "Yamaha": "Performance",
}

# Ordered (keyword -> category) rules checked against "<brand> <model>" text,
# lower-cased. First match wins. Checked BEFORE brand-default lookup so a
# clear model-name signal (e.g. a Premier "Angler" fishing floorplan) can
# override the brand's usual category.
MODEL_KEYWORD_RULES = [
    ("pontoon", "Pontoon / Tritoon"),
    ("tritoon", "Pontoon / Tritoon"),
    ("powertoon", "Pontoon / Tritoon"),
    ("suntracker", "Pontoon / Tritoon"),
    ("angler", "Fishing"),
    ("surf boss", "Wakeboard / Ski"),
    ("wakeboard", "Wakeboard / Ski"),
    ("wake", "Wakeboard / Ski"),
    ("surf", "Wakeboard / Ski"),
    ("ski & fish", "Wakeboard / Ski"),
    ("ski and fish", "Wakeboard / Ski"),
    ("nxt", "Wakeboard / Ski"),
    ("rzx", "Wakeboard / Ski"),
    ("tomcat", "Wakeboard / Ski"),
    ("center console", "Center Console"),
    ("cc ", "Center Console"),
    ("cayman", "Fishing"),
    ("z521r", "Fishing"),
    ("bass", "Fishing"),
    ("yacht", "Yacht"),
    ("ssx", "Performance"),
    ("ssi", "Performance"),
    ("osx", "Performance"),
    ("vortex", "Performance"),
    ("vrx", "Performance"),
    ("valanti", "Bowrider"),
    ("bowrider", "Bowrider"),
    # Crownline model-code suffixes confirmed via crownline.com / Boat Trader
    # (https://www.boattrader.com/boats/make-crownline/model-290-ss/ -- "SS"
    # is a bowrider; https://www.boattrader.com/boats/make-crownline/model-290-cr/
    # -- "CR" is a cruiser; "EX" -- e.g. boatzon.com listing "Crownline Deck
    # Boat 252 EX" -- is a deck-boat/bowrider hybrid, bucketed as Bowrider).
    (" cr", "Cruiser / Cabin"),
    (" ss", "Bowrider"),
    (" ex", "Bowrider"),
    # Chaparral "GTS"/"GT Surf" series is a bowrider/surf crossover, not a
    # pure wake boat -- confirmed via Boat Trader ("A powerboat built by
    # Chaparral, the 3 gts is a ski and wakeboard vessel") and multiple
    # dealer listings describing it as "Bow Rider" / "Bowrider power boat"
    # (https://www.boattrader.com/boats/make-chaparral/model-3-gts/,
    # https://www.boattrader.com/boat/2026-chaparral-gts-3-9970592/).
    ("gts", "Bowrider"),
]


def _fetch(url: str, params: Optional[dict] = None, headers: Optional[dict] = None):
    """GET a URL with retries. Returns the Response object or None on failure."""
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    last_err = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.get(url, params=params, headers=hdrs, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
        if attempt < RETRY_COUNT:
            time.sleep(RETRY_SLEEP_SECONDS)
    print(f"[premier_54] failed to fetch {url} params={params}: {last_err}", file=sys.stderr)
    return None


def _extract_price(price_text: Optional[str]) -> Optional[int]:
    if not price_text:
        return None
    cleaned = price_text.strip()
    low = cleaned.lower()
    if "call" in low or "contact" in low or "quote" in low:
        return None
    # Strip known label prefixes like "MSRP:" / "NAP:".
    cleaned = re.sub(r"^\s*(msrp|nap)\s*:?\s*", "", cleaned, flags=re.IGNORECASE)
    digits = re.sub(r"[^\d]", "", cleaned)
    if not digits:
        return None
    try:
        value = int(digits)
    except ValueError:
        return None
    return value if value > 0 else None


def _condition_from_href(href: str) -> Optional[str]:
    if not href:
        return None
    if "/new-boats-for-sale-detail/" in href:
        return "new"
    if "/used-pre-owned-boats-for-sale-detail/" in href:
        return "used"
    return None


def _condition_from_badge(card) -> Optional[str]:
    cond_list = card.select_one(".boat-inventory-cond-list")
    if not cond_list:
        return None
    for li in cond_list.find_all("li"):
        text = li.get_text(strip=True).lower()
        if text == "new":
            return "new"
        if text == "used":
            return "used"
    return None


def _extract_brand(title: str) -> str:
    if not title:
        return "Unknown"
    m = re.match(r"^\s*\d{4}\s+(.*)$", title.strip())
    rest = m.group(1) if m else title.strip()
    for brand in _BRANDS_BY_LEN:
        if rest.startswith(brand):
            return brand
    # Fallback: first word after the year.
    parts = rest.split()
    return parts[0] if parts else "Unknown"


def _normalize_category(brand: str, title: str) -> str:
    text = (title or "").lower()

    # Premier Marine's entire lineup on this site is pontoon/tritoon (see
    # BRAND_DEFAULT_CATEGORY comment for sourcing). Its model names happen to
    # reuse trim badges also used by other brands for very different hull
    # types (e.g. "GTS" is a Chaparral bowrider/surf series, but "Intrigue
    # GTS" is a Premier pontoon trim level -- confirmed via
    # https://www.pontoons.com/models/intrigue/ listing "230 Intrigue GTS RF"
    # and https://www.boattrader.com/boats/make-premier/model-intrigue/
    # -- "the Intrigue is a pontoon vessel"). So for Premier boats we only
    # let an explicit pontoon/fishing keyword override the brand default;
    # generic cross-brand trim keywords (ss/cr/ex/gts/ssx/...) are ignored.
    if brand == "Premier":
        for keyword in ("pontoon", "tritoon", "powertoon", "angler"):
            if keyword in text:
                return "Fishing" if keyword == "angler" else "Pontoon / Tritoon"
        return BRAND_DEFAULT_CATEGORY.get(brand, "Pontoon / Tritoon")

    for keyword, category in MODEL_KEYWORD_RULES:
        if keyword in text:
            return category
    if brand in BRAND_DEFAULT_CATEGORY:
        return BRAND_DEFAULT_CATEGORY[brand]
    return "Other"


def _parse_cards(html_fragment: str) -> list:
    """Parse one HTML fragment (full page or AJAX `result` fragment) into a
    list of listing dicts: {condition, brand, category, price}."""
    soup = BeautifulSoup(html_fragment, "lxml")
    listings = []
    for card in soup.select(".brandInventoryCard"):
        link = card.select_one("a[href]")
        href = link["href"] if link and link.has_attr("href") else ""

        title_el = card.select_one(".boatCardTitle")
        title = title_el.get_text(strip=True) if title_el else ""
        if not title and not href:
            # Skip empty placeholder cards (seen in some skeleton-loading markup).
            continue

        condition = _condition_from_href(href) or _condition_from_badge(card) or "unknown"

        brand = _extract_brand(title)
        category = _normalize_category(brand, title)

        price_el = card.select_one(".boatCardPriceTag")
        price_text = price_el.get_text(strip=True) if price_el else None
        price = _extract_price(price_text)

        listings.append(
            {
                "condition": condition,
                "brand": brand,
                "category": category,
                "price": price,
            }
        )
    return listings


def _scrape_via_ajax_replay() -> Optional[list]:
    """Primary strategy: fetch page 1 normally, then replay the site's own
    `?load_more=1&current_page=N` AJAX calls with plain `requests` until
    `current_page == pageCount`. Returns None on any failure so the caller
    can fall back to Playwright."""
    resp = _fetch(BASE_URL)
    if resp is None:
        return None

    all_listings = _parse_cards(resp.text)

    # Confirm this page actually uses the load-more pattern we expect, and
    # find the declared page count as a sanity bound.
    page_count_match = re.search(r'data-page_count="(\d+)"', resp.text)
    if not page_count_match:
        # No load-more button found at all -- either the whole inventory is
        # already server-rendered (fine, just return what we parsed) or the
        # markup changed in a way we don't recognize. Either way there is no
        # AJAX pagination to replay.
        return all_listings

    declared_page_count = int(page_count_match.group(1))

    ajax_headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE_URL,
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    page = 2
    seen_pages = set()
    while page <= MAX_AJAX_PAGES:
        resp = _fetch(
            BASE_URL,
            params={"load_more": 1, "current_page": page},
            headers=ajax_headers,
        )
        if resp is None:
            print("[premier_54] AJAX replay failed mid-pagination; falling back", file=sys.stderr)
            return None
        try:
            data = resp.json()
        except ValueError:
            print("[premier_54] AJAX response was not JSON; falling back", file=sys.stderr)
            return None

        result_html = data.get("result", "") or ""
        page_listings = _parse_cards(result_html)
        all_listings.extend(page_listings)

        current_page = data.get("current_page")
        page_count = data.get("pageCount", declared_page_count)
        seen_pages.add(str(current_page))

        if str(current_page) == str(page_count) or not result_html.strip():
            break
        page += 1
        time.sleep(0.3)  # be polite between AJAX calls
    else:
        print("[premier_54] hit MAX_AJAX_PAGES safety cap", file=sys.stderr)

    return all_listings


def _scrape_via_playwright() -> list:
    """Fallback strategy: drive real headless Chromium, clicking the
    "LOAD MORE" button repeatedly until all listings are loaded, then parse
    the final DOM."""
    from playwright.sync_api import sync_playwright

    all_html = ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(BASE_URL, timeout=PLAYWRIGHT_NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_selector(".brandInventoryCard", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)

            clicks = 0
            while clicks < PLAYWRIGHT_MAX_CLICKS:
                button = page.query_selector(".load_more_action")
                if button is None:
                    break
                is_visible = button.is_visible()
                if not is_visible:
                    break
                before_count = len(page.query_selector_all(".brandInventoryCard"))
                try:
                    button.scroll_into_view_if_needed(timeout=5000)
                    button.click(timeout=5000)
                except Exception:
                    break
                clicks += 1
                # Wait for either the card count to grow or the button to
                # disappear/get hidden (last page reached).
                try:
                    page.wait_for_function(
                        """(prevCount) => {
                            const cards = document.querySelectorAll('.brandInventoryCard').length;
                            const btn = document.querySelector('.load_more_action');
                            const btnGone = !btn || btn.offsetParent === null;
                            return cards > prevCount || btnGone;
                        }""",
                        arg=before_count,
                        timeout=15000,
                    )
                except Exception:
                    # Timed out waiting -- assume no more content is coming.
                    break
                time.sleep(0.3)

            all_html = page.content()
        finally:
            browser.close()

    return _parse_cards(all_html)


def scrape() -> dict:
    """Scrape Premier 54 Motorsports' full inventory listing.

    Primary method: replay the site's own AJAX "load more" endpoint directly
    with `requests` (fast, no browser needed). Falls back to a real headless
    Chromium via Playwright (clicking "LOAD MORE" repeatedly) if the AJAX
    replay fails or returns no data.
    """
    listings = _scrape_via_ajax_replay()
    method = "ajax_replay"

    if not listings:
        listings = _scrape_via_playwright()
        method = "playwright"

    total = len(listings)
    new_count = sum(1 for l in listings if l["condition"] == "new")
    used_count = sum(1 for l in listings if l["condition"] == "used")

    category_counts: dict = {}
    for listing in listings:
        category_counts[listing["category"]] = category_counts.get(listing["category"], 0) + 1

    brand_counts: dict = {}
    for listing in listings:
        brand_counts[listing["brand"]] = brand_counts.get(listing["brand"], 0) + 1

    prices = [
        (listing["price"], listing["condition"])
        for listing in listings
        if listing["price"] is not None
    ]

    numeric_prices = [p for p, _ in prices]
    price_low = min(numeric_prices) if numeric_prices else None
    price_high = max(numeric_prices) if numeric_prices else None

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
    result["_method"] = method  # diagnostic only; not part of the required schema
    return result


if __name__ == "__main__":
    import json

    print(json.dumps(scrape(), indent=2))
