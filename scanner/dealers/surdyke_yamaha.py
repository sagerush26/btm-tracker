"""
Scraper for Surdyke Yamaha & Marina (surdyke-yamaha).

Site: https://www.surdykeyamaha.com/--inventory?category=boat&sz=50

Site tech notes
----------------
This dealer runs on the same "DealerSpike" v7 platform as Village Marina &
Yacht Club (see village_marina.py in this same package) -- confirmed by
identical markup: `<html data-platformversion="7">`, the same
`v7list-results__item` listing wrapper, the same `data-unit-*` attributes,
and the same `--inventory?...&pg=N` pagination query-string style. Despite
the "--inventory" URL superficially resembling Kelly's Port's URL
(kellysport.com/--inventory?...), Kelly's Port is actually a *different*
underlying platform (its price blocks render very differently -- long
MSRP/"Our Price"/Savings text blocks -- with no `data-unit-*` attributes),
so no code was shared between the two beyond the general fetch/paginate
approach.

The inventory listing itself is fully server-rendered plain HTML -- no JS
execution needed to see boats, prices, or attributes. Confirmed by fetching
the page with a plain `requests.get()` call: all 50/50/6 listings across the
site's 3 pages of results were present verbatim in the raw HTTP response.

Each boat listing sits inside a `<li class="v7list-results__item" ...>`
carrying convenient data-* attributes directly in the HTML, e.g.:

    data-unit-id="19130123"
    data-unit-condition="NEW"            (or "USED")
    data-unit-year="2027"
    data-unit-make="Axopar"
    data-unit-model="29 XC CROSS CABIN"
    data-unit-category="Boat"            (always "Boat" here; the URL's
                                           category=boat query param is what
                                           actually excludes ATVs/motorcycles
                                           for us server-side)
    data-unit-subcategory="Center Console"  (or "Bowrider", "Pontoon",
                                              "Fishing", "Boat" (generic),
                                              "Jet Boat", "Walkaround", etc.)

Pricing: the most reliable numeric price is the `data-psm-unitprice="259999"`
attribute that appears on the "Get a Quote" / financing link within each
listing block (present on ~102/106 units at audit time). It was cross-checked
against the visible `<span class="vehicle-price__price">$259,999</span>` text
and matches exactly wherever both are present. The remaining units show only
"Click for a Quote" with neither attribute nor visible numeric price --
these correctly yield price=None ("unknown" bucket), consistent with the
`village_marina.py` sibling scraper's handling of the same label text on the
same platform.

Category normalization: roughly 40% of listings carry the generic
`data-unit-subcategory="Boat"` (no useful signal), so in addition to mapping
the specific subcategory strings DealerSpike does provide (Bowrider,
Fishing, Pontoon, Center Console, Jet Boat, Walkaround, Sport Cruiser,
Inboard Boat, Jon, Recreational, Performance), we fall back to keyword/brand
heuristics over the make+model text (e.g. "TRITOON" -> Pontoon / Tritoon,
"CROSS CABIN" / "SUN-TOP" Axopar cabin models -> Cruiser / Cabin, Yamaha
jet-boat model codes like AR190/AR210/242X/252SE/275SDX/SX250/FX SHO ->
Performance) verified by manually spot-checking the make/model pairs that
fell into the generic "Boat" subcategory bucket at audit time.

Pagination is simple query-string pagination: `&pg=2`, `&pg=3`, ... A "Next
page" link (`<a rel="next" href="...&pg=N">`) is present in the page markup
while more pages remain, and is absent (or points back at the current page)
once the last page is reached. We follow it defensively up to a generous
page cap, and also stop as soon as a page returns zero new unit IDs.

At audit time (2026-09-05) the site reported "Showing 1-50 of 106 results"
on page 1, "51-100 of 106" on page 2, and "101-106 of 106" on page 3 (50 +
50 + 6 = 106), split 75 NEW / 31 USED. This is well above the "15-40" boats
ballpark mentioned as a rough prior estimate, but Surdyke Yamaha & Marina is
a large multi-line dealer (Yamaha Boats, G3, SunCatcher pontoons, Axopar,
Scarab, Beneteau, and more), so a 100+ unit boat inventory (category=boat
already excludes their ATV/motorcycle/PWC-adjacent lines from this count)
is plausible for its size; the URL's `category=boat` filter was left in
place exactly as instructed.

IMPORTANT reliability note: this dealer sits behind Cloudflare, and its
`--inventory` endpoint intermittently returns a Cloudflare "Just a moment..."
JS challenge (HTTP 403) to plain `requests` calls -- observed on some runs
but not others even with a normal desktop User-Agent (looks like TLS/HTTP2
fingerprinting-based bot detection rather than a simple UA check, since a
bare `curl` request succeeded while `requests`/urllib3 got challenged
seconds later from the same environment). Plain `requests` IS attempted
first (cheap, fast, works a meaningful fraction of the time), but this
scraper always falls back to Playwright (sync API, headless Chromium) when
`requests` returns a non-200, an empty body, or a body that still contains
the Cloudflare challenge markers. The Playwright fallback waits for
`domcontentloaded` (NOT `networkidle` -- the Cloudflare challenge's
background polling means the network never truly goes idle within a
reasonable timeout) and then explicitly waits for the
`li.v7list-results__item` selector to appear, which reliably clears the
challenge and lands on the real listing grid. Given this dealer's
Cloudflare gate, GitHub Actions runs should expect to hit the Playwright
path fairly often and budget the extra time/browser-launch overhead
accordingly.
"""

from __future__ import annotations

import html
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

DEALER_ID = "surdyke-yamaha"
DEALER_NAME = "Surdyke Yamaha & Marina"
BASE_URL = "https://www.surdykeyamaha.com/--inventory"
# category=boat is required to exclude this dealer's ATVs/motorcycles/etc.
INVENTORY_PARAMS = {"category": "boat", "sz": "50"}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

MAX_PAGES = 15  # generous safety cap; real site has ~3 pages of results
REQUEST_TIMEOUT = 30
RETRY_COUNT = 3
RETRY_SLEEP_SECONDS = 2

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

# Map raw dealer subcategory strings (HTML-unescaped, lowercased) -> our
# normalized category buckets. These are the specific subcategory values
# DealerSpike provides for this dealer at audit time.
SUBCATEGORY_MAP = {
    "bowrider": "Bowrider",
    "deck boat": "Pontoon / Tritoon",
    "pontoon": "Pontoon / Tritoon",
    "pontoon / tritoon": "Pontoon / Tritoon",
    "tritoon": "Pontoon / Tritoon",
    "ski/wakeboard/wakesurf": "Wakeboard / Ski",
    "ski & wakeboard": "Wakeboard / Ski",
    "wakeboard": "Wakeboard / Ski",
    "center console": "Center Console",
    "cruiser (power)": "Cruiser / Cabin",
    "express cruiser": "Cruiser / Cabin",
    "cabin cruiser": "Cruiser / Cabin",
    "sport cruiser": "Cruiser / Cabin",
    "flybridge": "Cruiser / Cabin",
    "cuddy cabin": "Cruiser / Cabin",
    "walkaround": "Cruiser / Cabin",
    "inboard boat": "Cruiser / Cabin",
    "yacht": "Yacht",
    "motor yacht": "Yacht",
    "fishing": "Fishing",
    "bass boat": "Fishing",
    "sport fishing": "Fishing",
    "offshore": "Fishing",
    "jon": "Fishing",
    "performance": "Performance",
    "high performance": "Performance",
    "jet boat": "Performance",
    "personal watercraft": "Performance",
    "pwc": "Performance",
    "power": "Power",
    "runabout": "Power",
    "recreational": "Power",
    "deck": "Power",
}

# Keyword/brand fallback rules applied to "<make> <model>" text, used when
# the dealer's own subcategory is the generic/unhelpful "Boat" (~40% of
# listings) or unrecognized. Order matters -- first match wins.
KEYWORD_CATEGORY_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\btritoon\b|\btritonn\b", re.I), "Pontoon / Tritoon"),
    (re.compile(r"\bpontoon\b", re.I), "Pontoon / Tritoon"),
    (re.compile(r"\bwake\b|\bwakeboard\b|\bwakesurf\b|\bx-?star\b", re.I), "Wakeboard / Ski"),
    (re.compile(r"\bcenter\s*console\b", re.I), "Center Console"),
    (
        re.compile(
            r"\bcross\s*cabin\b|\bsun-?top\b|\bcuddy\b|\bcabin\b|\bcruiser\b|"
            r"\bexpress\b|\bgran\s*turismo\b|\bantares\b|\bflybridge\b",
            re.I,
        ),
        "Cruiser / Cabin",
    ),
    (re.compile(r"\byacht\b", re.I), "Yacht"),
    (
        re.compile(
            r"\bfsh\b|\bfishing\b|\bbass\b|\bgrizzly\b|\bjon\b|\bv18\b|\bidentity\b",
            re.I,
        ),
        "Fishing",
    ),
    (
        re.compile(
            # Yamaha jet-boat model codes (AR/SX/FX series, 2xxE-Series/SE/SDX)
            r"\bar\d{3}\b|\bsx\d{3}\b|\bfx\s*sho\b|\b2\d{2}[a-z]?\s*(e-series|se|sdx|x\s*e-series)\b|"
            r"\bsbi\d{3}\b|\bjet\b",
            re.I,
        ),
        "Performance",
    ),
    (
        re.compile(r"\bbowrider\b|\bssx\b|\bexecutioner\b|\btrinidad\b", re.I),
        "Bowrider",
    ),
    (re.compile(r"\bz\d{3}\b|\bz-\d{3}\b", re.I), "Fishing"),  # Ranger Z-series bass boats
    (re.compile(r"\bhp\d{3}\b", re.I), "Fishing"),  # G3 HP-series jon/utility boats
    (re.compile(r"\bgt\s*\d{2}\b|\bgran\s*turismo\b", re.I), "Cruiser / Cabin"),  # Beneteau GT/Gran Turismo
    (
        re.compile(r"\b(262|280\s*ss|323)\b", re.I),
        "Bowrider",
    ),  # confirmed Cobalt/Crownline/Formula sport-boat model codes at this dealer
    (re.compile(r"\brunabout\b|\bdeck\s*boat\b", re.I), "Power"),
]

# Brand-only fallback for listings with an empty/unhelpful model string and a
# generic "Boat" subcategory (verified against this dealer's actual lineup:
# Voyager and G3 are pontoon lines here; Scarab and Tahoe are performance/
# runabout brands; Bayliner's current lineup at this dealer is bowriders).
BRAND_CATEGORY_HINTS = {
    "voyager": "Pontoon / Tritoon",
    "scarab": "Performance",
    "tahoe": "Bowrider",
    "bayliner": "Bowrider",
}

# Yamaha model-code prefixes that are jet-boat/PWC hulls -> Performance.
YAMAHA_PERFORMANCE_PREFIXES = ("AR", "SX", "FX", "242", "252", "275")


def _normalize_category(subcategory: Optional[str], make: str, model: str) -> str:
    """Map raw dealer subcategory + make/model text onto fixed schema buckets."""
    cleaned_sub = html.unescape(subcategory or "").strip().lower()
    if cleaned_sub and cleaned_sub != "boat" and cleaned_sub in SUBCATEGORY_MAP:
        return SUBCATEGORY_MAP[cleaned_sub]

    combined = f"{make} {model}".strip()
    for pattern, category in KEYWORD_CATEGORY_RULES:
        if pattern.search(combined):
            return category

    # Brand-only fallback when the model string gave us nothing to match on
    # (empty model, generic "Boat" subcategory).
    brand_key = html.unescape(make or "").strip().lower()
    brand_key = re.sub(r"[^a-z ]", "", brand_key).strip()
    if brand_key in BRAND_CATEGORY_HINTS:
        return BRAND_CATEGORY_HINTS[brand_key]

    # Loose contains-based matching on the raw subcategory as a second pass.
    if cleaned_sub:
        for key, cat in SUBCATEGORY_MAP.items():
            if key in cleaned_sub:
                return cat
        if "bowrider" in cleaned_sub:
            return "Bowrider"
        if "pontoon" in cleaned_sub or "tritoon" in cleaned_sub:
            return "Pontoon / Tritoon"
        if "fish" in cleaned_sub or "bass" in cleaned_sub:
            return "Fishing"
        if "cruiser" in cleaned_sub or "cabin" in cleaned_sub or "walkaround" in cleaned_sub:
            return "Cruiser / Cabin"
        if "jet" in cleaned_sub or "performance" in cleaned_sub or "pwc" in cleaned_sub:
            return "Performance"
        if "yacht" in cleaned_sub:
            return "Yacht"
        if "power" in cleaned_sub or "runabout" in cleaned_sub or "recreational" in cleaned_sub:
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


def _clean_brand(raw: Optional[str]) -> str:
    if not raw:
        return "Unknown"
    cleaned = html.unescape(raw).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Normalize a couple of encoding artifacts seen in the raw HTML
    # (e.g. "G3&#x2F;YAMAHA" already unescaped to "G3/YAMAHA").
    if not cleaned or cleaned.upper() == "UNKNOWN":
        return "Unknown"
    return cleaned


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


def _looks_like_cloudflare_challenge(text: str) -> bool:
    if not text:
        return True
    lowered = text[:4000].lower()
    return (
        "just a moment" in lowered
        or "cf-chl" in lowered
        or ("challenges.cloudflare.com" in lowered and "v7list-results" not in text.lower())
    )


def _fetch(session: requests.Session, url: str, params: Optional[dict] = None) -> Optional[str]:
    """GET a URL with retries. Returns response text or None on failure/challenge."""
    last_err = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and resp.text and not _looks_like_cloudflare_challenge(resp.text):
                return resp.text
            if resp.status_code == 200:
                last_err = "Cloudflare challenge page returned"
            else:
                last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
        if attempt < RETRY_COUNT:
            time.sleep(RETRY_SLEEP_SECONDS)
    print(f"[surdyke_yamaha] plain requests fetch failed for {url} params={params}: {last_err}", file=sys.stderr)
    return None


class _PlaywrightFetcher:
    """Keeps a single headless Chromium instance alive across pages.

    Relaunching a fresh browser for every paginated request is wasteful,
    especially since this dealer's Cloudflare gate means we may need the
    Playwright path for every page of a multi-page run. Lazily started on
    first use; call `.close()` when done (or just let it be garbage
    collected / process-exit cleaned up).
    """

    def __init__(self) -> None:
        self._ctx = None
        self._browser = None
        self._page = None

    def _ensure_started(self) -> bool:
        if self._page is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return False
        try:
            self._ctx = sync_playwright().start()
            self._browser = self._ctx.chromium.launch(headless=True)
            self._page = self._browser.new_page(user_agent=USER_AGENT)
            return True
        except Exception as exc:
            print(f"[surdyke_yamaha] failed to start playwright: {exc}", file=sys.stderr)
            return False

    def fetch(self, url: str, retries: int = 2) -> Optional[str]:
        """Navigate to `url` and return rendered HTML, or None on failure.

        Uses `domcontentloaded` (not `networkidle`, which never fires while
        Cloudflare's challenge keeps polling in the background) and then
        explicitly waits for the listing selector, which reliably clears
        the challenge once Cloudflare is satisfied the client is a real
        browser. Retries a couple of times on navigation timeouts, since
        those have been observed to be transient (network hiccup / slow
        challenge) rather than persistent blocking.
        """
        if not self._ensure_started():
            return None
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 2):
            try:
                self._page.goto(url, timeout=60000, wait_until="domcontentloaded")
                try:
                    self._page.wait_for_selector("li.v7list-results__item", timeout=25000)
                except Exception:
                    # Page may genuinely have zero results, or challenge took
                    # longer; grab whatever content is there so the caller
                    # can decide (empty result vs. real failure).
                    self._page.wait_for_timeout(3000)
                return self._page.content()
            except Exception as exc:
                last_exc = exc
                if attempt <= retries:
                    time.sleep(2)
        print(f"[surdyke_yamaha] playwright fetch failed for {url}: {last_exc}", file=sys.stderr)
        return None

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._ctx is not None:
                self._ctx.stop()
        except Exception:
            pass


def _extract_price(item_html: str) -> Optional[int]:
    """Pull the numeric asking price out of a listing's HTML block.

    Prefers the structured `data-psm-unitprice="259999"` attribute (present
    on units with a public price), falling back to the visible
    `vehicle-price__price` text if the attribute is ever missing but a
    price is still shown. Units with only "Click for a Quote" / "Call for
    Price" correctly yield None.
    """
    psm_match = re.search(r'data-psm-unitprice="(\d+)"', item_html)
    if psm_match:
        try:
            value = int(psm_match.group(1))
            if value > 0:
                return value
        except ValueError:
            pass

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
    return value if value > 0 else None


def _extract_listing_html_blocks(page_html: str) -> List[str]:
    """Split page HTML into per-listing chunks bounded by data-unit-id."""
    starts = [m.start() for m in re.finditer(r'data-unit-id="\d+"', page_html)]
    if not starts:
        return []
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(page_html)
        block_start = page_html.rfind("<li", 0, start)
        if block_start == -1:
            block_start = max(0, start - 200)
        blocks.append(page_html[block_start:end])
    return blocks


UNIT_ATTR_RE = re.compile(
    r'data-unit-id="(?P<id>\d+)"\s*data-unit-condition="(?P<condition>[A-Za-z]*)"'
    r'\s*data-unit-year="(?P<year>\d*)"\s*data-unit-make="(?P<make>[^"]*)"'
    r'\s*data-unit-model="(?P<model>[^"]*)"\s*data-unit-category="(?P<category>[^"]*)"'
    r'\s*data-unit-subcategory="(?P<subcategory>[^"]*)"',
)


def _parse_page(page_html: str) -> List[Dict]:
    listings = []
    blocks = _extract_listing_html_blocks(page_html)
    for block in blocks:
        attr_match = UNIT_ATTR_RE.search(block)

        def _grab(attr: str) -> str:
            m = re.search(rf'data-unit-{attr}="([^"]*)"', block)
            return html.unescape(m.group(1)) if m else ""

        if attr_match:
            unit_id = attr_match.group("id")
            condition = attr_match.group("condition")
            make = html.unescape(attr_match.group("make"))
            model = html.unescape(attr_match.group("model"))
            subcategory = html.unescape(attr_match.group("subcategory"))
        else:
            unit_id = _grab("id")
            condition = _grab("condition")
            make = _grab("make")
            model = _grab("model")
            subcategory = _grab("subcategory")
            if not unit_id:
                continue

        price = _extract_price(block)
        brand = _clean_brand(make)
        category = _normalize_category(subcategory, make, model)

        listings.append(
            {
                "id": unit_id,
                "condition": _normalize_condition(condition),
                "brand": brand,
                "category": category,
                "price": price,
            }
        )
    return listings


def _find_next_page_url(page_html: str, current_url: str) -> Optional[str]:
    soup = BeautifulSoup(page_html, "lxml")
    next_link = soup.select_one('a[rel="next"]')
    if next_link and next_link.get("href"):
        href = next_link["href"]
        return requests.compat.urljoin(current_url, html.unescape(href))
    return None


def _collect_all_listings() -> List[Dict]:
    session = _make_session()
    seen_ids = set()
    all_listings: List[Dict] = []
    playwright_fetcher = _PlaywrightFetcher()

    first_url = f"{BASE_URL}?{requests.compat.urlencode(INVENTORY_PARAMS)}"
    current_url = first_url
    page_num = 1

    try:
        while current_url and page_num <= MAX_PAGES:
            page_html = _fetch(session, current_url)
            if not page_html:
                # Plain requests failed or hit a Cloudflare challenge;
                # fall back to the shared headless-browser session.
                page_html = playwright_fetcher.fetch(current_url)
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
                break

            next_url = _find_next_page_url(page_html, current_url)
            if not next_url or next_url == current_url:
                break

            current_url = next_url
            page_num += 1
            time.sleep(0.5)  # be polite between page fetches
    finally:
        playwright_fetcher.close()

    return all_listings


def scrape() -> dict:
    """Scrape Surdyke Yamaha & Marina's full boat inventory listing.

    Fetches page 1 of the DealerSpike-rendered `--inventory?category=boat`
    listing (server-rendered HTML, no JS required), then follows `&pg=N`
    pagination links until no further "next page" link is found, a page
    yields zero new unit IDs, or a safety page cap is reached.
    """
    listings = _collect_all_listings()

    total = len(listings)
    new_count = sum(1 for l in listings if l["condition"] == "new")
    used_count = sum(1 for l in listings if l["condition"] == "used")

    category_counts: Dict[str, int] = {label: 0 for label in NORMALIZED_CATEGORIES}
    for listing in listings:
        cat = listing["category"] if listing["category"] in category_counts else "Other"
        category_counts[cat] += 1
    category_breakdown = {k: v for k, v in category_counts.items() if v > 0} or category_counts

    brand_counts: Dict[str, int] = {}
    for listing in listings:
        brand_counts[listing["brand"]] = brand_counts.get(listing["brand"], 0) + 1

    prices: List[Tuple[int, str]] = [
        (listing["price"], listing["condition"]) for listing in listings if listing["price"] is not None
    ]

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
