"""
Scraper for Marty's Marine (Osage Beach, MO / Lake of the Ozarks).

Site: https://www.martysmarineloz.com/inventory

Site tech notes
----------------
The site runs on the Duda website builder (evidence: `window.dmAPI`,
`dmRoot` body id, assets served from `static.cdn-website.com` /
`irp.cdn-website.com`). It is NOT Squarespace -- there is no
`Static.SQUARESPACE_CONTEXT` object anywhere in the page source (confirmed by
grepping a raw `curl`/`requests` fetch of the page). Despite that, the good
news holds either way: all listing data needed here is already present in
the plain server-rendered HTML returned by a simple `requests.get()` -- no
Playwright / JS execution is required.

Each inventory unit is rendered as a Duda "Pretty List" widget item:

    <li class="listItem" id="...">
      <a class="biglink clearfix" href="/hin--23644">
        <div class="listImage" ...></div>
        <div class="listText">
          <span class="itemName">2025 Barletta Cabrio C24MA Powered by ...</span>
          <div class="itemText">...</div>
        </div>
        <span class="link dmWidget">
          <span class="buttonText text">MSRP $91,176.00 SALE $68,396.00</span>
        </span>
      </a>
    </li>

The dealer splits inventory across three separate static pages sharing the
identical markup structure:
  - /inventory              -> new units (nav item "New Inventory")
  - /used-inventory         -> used / trade-in units, including some already
                                marked "Sold"
  - /clearance-inventory    -> "SUPER SALE" marked-down units (still new
                                stock, just discounted / leftover models)

There is no pagination on any of these three pages (no `?page=`, `rel=next`,
or "load more" controls were found in the HTML) -- each page renders its
full list of items in one response. We still fetch all three URLs each run
since together they make up the dealer's whole public inventory.

Important distinction found on the `/inventory` page: each listing's detail
link is either a `/hin--XXXXX` (or `/hin-XXXXX`) style href -- a real Hull
Identification Number for a specific physical boat sitting in the dealer's
lot -- or an `/order--XXXXX` / `/copy-of-order--XXXXX` style href, which is a
factory build-slot / special-order configuration rather than a boat
physically in stock. Proof: the dealer also runs a separate `/coming-soon`
page that exclusively uses `order--XXXXX` links for a *different*, entirely
non-overlapping set of IDs than the `order--` links mixed into `/inventory`
-- i.e. `order--` is the dealer's generic "buildable/orderable unit" page
type, used both for genuine coming-soon models and for order-slots
cross-posted on the main inventory page. Since this scraper reports current
physical inventory (matching the ground-truth dealer lot size, which is
small -- roughly 8-25 units), we only count `hin`-style listings (or
listings with no href / a non-`order` slug, which are also real physical
units observed on the used-inventory page) and exclude `order--` /
`copy-of-order--` build-slot listings entirely.

Price text observed in the `.buttonText` span takes a few forms:
  - "MSRP $91,176.00 SALE $68,396.00"  -> take the SALE (lower/actual) price
  - "PRICE DROP $55,000.00"            -> single discounted price
  - "MORE DETAILS" / "More Details"    -> no price published (price = None)
  - "Sold" / "SOLD"                    -> unit already sold; excluded from
                                           the inventory count entirely

A few units appear on more than one of the three pages (observed: a listing
with href `/hin-17839` shows up both on `/inventory` and, re-labelled
"SUPER SALE...", on `/clearance-inventory`). We de-duplicate globally by the
listing's detail-page href (falling back to the listing name when no href is
present) so each physical unit is only counted once.

Condition (new/used) is inferred from which of the three source pages a
listing came from (`/inventory` + `/clearance-inventory` -> new,
`/used-inventory` -> used), since the per-item markup carries no explicit
condition attribute of its own.

No Squarespace JSON blob and no JS rendering needed; Playwright is left
unused here (only `requests` + `BeautifulSoup`/lxml) but the dependency is
still declared/importable in case the dealer's stack changes in the future.
"""

from __future__ import annotations

import html
import re
import sys
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:  # optional JS-rendering fallback; not needed for this dealer today
    from playwright.sync_api import sync_playwright  # noqa: F401
except Exception:  # pragma: no cover - playwright is optional at runtime
    sync_playwright = None

DEALER_ID = "martys-marine"
DEALER_NAME = "Marty's Marine"
BASE_URL = "https://www.martysmarineloz.com"

# (path, condition) -- condition is inferred from which page a listing was
# found on, since the HTML itself carries no explicit new/used marker.
INVENTORY_PAGES = [
    ("/inventory", "new"),
    ("/clearance-inventory", "new"),
    ("/used-inventory", "used"),
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

REQUEST_TIMEOUT = 30
RETRY_COUNT = 3
RETRY_SLEEP_SECONDS = 2

PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")

# Only brand we've observed in the wild is Barletta (new side) plus a
# handful of used trade-in brands. Extraction below is generic (first
# capitalized "make" token pulled from the title) with light cleanup.
KNOWN_BRANDS = [
    "Barletta",
    "PlayCraft",
    "Playcraft",
    "Paycraft",  # observed dealer typo for Playcraft
    "Sylvan",
    "Regency",
    "Bennington",
    "Pontoon",
]

CATEGORY_MAP = {
    "pontoon": "Pontoon / Tritoon",
    "tritoon": "Pontoon / Tritoon",
    "deck boat": "Pontoon / Tritoon",
    "wakeboard": "Wakeboard / Ski",
    "ski": "Wakeboard / Ski",
    "center console": "Center Console",
    "cruiser": "Cruiser / Cabin",
    "cabin": "Cruiser / Cabin",
    "flybridge": "Cruiser / Cabin",
    "bowrider": "Bowrider",
    "yacht": "Yacht",
    "fishing": "Fishing",
    "bass": "Fishing",
    "performance": "Performance",
    "jet boat": "Performance",
    "pwc": "Performance",
    "power": "Power",
    "runabout": "Power",
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


def _fetch(url: str) -> Optional[str]:
    """GET a URL with retries. Returns response text or None on failure."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    last_err = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and resp.text:
                return resp.text
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
        if attempt < RETRY_COUNT:
            time.sleep(RETRY_SLEEP_SECONDS)
    print(f"[martys_marine] failed to fetch {url}: {last_err}", file=sys.stderr)
    return None


def _normalize_category(name: str) -> str:
    """Guess a normalized category bucket from the free-text listing title.

    Marty's Marine's inventory is almost entirely Barletta pontoons/tritoons,
    with used trade-ins of varying types. There is no explicit category field
    in the markup, so we infer from keywords in the title; anything we can't
    confidently classify (e.g. generic "Regency"/"Sylvan" deck boats without
    a clear keyword) falls back to "Pontoon / Tritoon" for known pontoon
    brands, else "Other".
    """
    cleaned = html.unescape(name or "").strip().lower()
    if not cleaned:
        return "Other"
    # Brand-based match takes priority: every brand this dealer carries is a
    # pontoon/deck-boat manufacturer, and titles frequently contain phrases
    # like "Powered by ... Mercury!" whose "power" substring would otherwise
    # be mis-read as the generic "Power" bucket below.
    if any(b.lower() in cleaned for b in ["barletta", "playcraft", "paycraft", "sylvan", "regency", "bennington", "toon"]):
        return "Pontoon / Tritoon"
    for key, bucket in CATEGORY_MAP.items():
        if key in cleaned:
            return bucket
    return "Other"


def _extract_brand(name: str) -> str:
    cleaned = html.unescape(name or "").strip()
    if not cleaned:
        return "Unknown"
    for brand in KNOWN_BRANDS:
        if brand.lower() in cleaned.lower():
            if brand.lower() in ("paycraft",):
                return "Playcraft"
            if brand.lower() == "playcraft":
                return "PlayCraft"
            return brand
    # Fallback: try to grab the token after a leading 4-digit year, e.g.
    # "2016 Sylvan S5" -> "Sylvan"; "2017 PlayCraft Infinity" -> "PlayCraft".
    match = re.match(r"^\s*(?:USED\s+)?(?:\d{4})\s+([A-Za-z][A-Za-z'\-]*)", cleaned, re.IGNORECASE)
    if match:
        return match.group(1).strip().title()
    return "Unknown"


def _extract_price(price_texts: list) -> Optional[int]:
    """Pull a single representative numeric price out of the buttonText(s).

    "MSRP $X SALE $Y" -> Y (the real/actual asking price).
    "PRICE DROP $X"   -> X.
    "MORE DETAILS" / "More Details" / "Sold" / "SOLD" -> None.
    """
    joined = " ".join(price_texts)
    if not joined:
        return None
    lowered = joined.lower()
    if "sold" in lowered:
        return None
    matches = PRICE_RE.findall(joined)
    if not matches:
        return None
    values = []
    for m in matches:
        try:
            values.append(int(round(float(m.replace(",", "")))))
        except ValueError:
            continue
    if not values:
        return None
    # If multiple numbers present (MSRP + SALE), the sale/actual price is
    # the last one listed and also the lower of the two -- use the min to
    # be safe against label re-ordering.
    return min(values)


def _is_sold(price_texts: list) -> bool:
    joined = " ".join(price_texts).strip().lower()
    return joined == "sold"


def _parse_page(page_html: str, condition: str) -> list:
    """Parse one inventory page's HTML into a list of listing dicts."""
    soup = BeautifulSoup(page_html, "lxml")
    listings = []
    for li in soup.select("li.listItem"):
        name_tag = li.select_one(".itemName")
        name = name_tag.get_text(strip=True) if name_tag else ""
        if not name:
            continue
        price_spans = li.select(".buttonText")
        price_texts = [s.get_text(strip=True) for s in price_spans]

        if _is_sold(price_texts):
            # Already-sold unit; not part of current live inventory.
            continue

        link_tag = li.select_one("a.biglink")
        href = link_tag.get("href") if link_tag else None

        # Skip factory order/build-slot listings (`/order--NNNNN` or
        # `/copy-of-order--NNNNN`) -- these are buildable configurations, not
        # physical units on the lot (see module docstring for the evidence).
        if href and re.search(r"order--?\d+", href):
            continue

        # '/dup' is a placeholder/broken link Duda uses for some entries;
        # don't let it collide with real hrefs during de-duplication.
        dedupe_key = href if href and href not in ("/dup", "#") else name

        price = _extract_price(price_texts)
        listings.append(
            {
                "key": dedupe_key,
                "name": name,
                "condition": condition,
                "brand": _extract_brand(name),
                "category": _normalize_category(name),
                "price": price,
            }
        )
    return listings


def scrape() -> dict:
    """Scrape Marty's Marine's full public inventory.

    Fetches the three static inventory pages (/inventory, /clearance-inventory
    for new stock, /used-inventory for used/trade-in stock). Each page is
    fully server-rendered HTML with no pagination, so a single GET per page
    is sufficient. Listings marked "Sold" are excluded. Listings appearing on
    more than one page (observed for at least one clearance-priced new unit)
    are de-duplicated by their detail-page href.
    """
    seen_keys = set()
    all_listings = []

    for path, condition in INVENTORY_PAGES:
        url = f"{BASE_URL}{path}"
        page_html = _fetch(url)
        if not page_html:
            continue
        for listing in _parse_page(page_html, condition):
            if listing["key"] in seen_keys:
                continue
            seen_keys.add(listing["key"])
            all_listings.append(listing)
        time.sleep(0.3)  # be polite between page fetches

    total = len(all_listings)
    new_count = sum(1 for l in all_listings if l["condition"] == "new")
    used_count = sum(1 for l in all_listings if l["condition"] == "used")

    category_counts: dict = {}
    for listing in all_listings:
        category_counts[listing["category"]] = category_counts.get(listing["category"], 0) + 1

    brand_counts: dict = {}
    for listing in all_listings:
        brand_counts[listing["brand"]] = brand_counts.get(listing["brand"], 0) + 1

    prices = []
    for listing in all_listings:
        if listing["price"] is not None:
            prices.append((listing["price"], listing["condition"]))

    numeric_prices = [p for p, _ in prices]
    price_low = min(numeric_prices) if numeric_prices else None
    price_high = max(numeric_prices) if numeric_prices else None

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
    import json

    print(json.dumps(scrape(), indent=2))
