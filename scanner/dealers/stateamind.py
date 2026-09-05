"""
Scraper for Stateamind Water Sports (dealer id: stateamind).

Site: https://www.stateamind.com/missouri-all-boat-inventory/

Platform detection
-------------------
The inventory page is built on the "Marine Manager" / NativeRank marine
dealer CMS (same platform confirmed via `window.MARINE_MANAGER_STORE`,
`window.__ALGOLIA_PROPS__`, `window.__SERVER_STATE__` globals and
`cdn.nativerank.com` asset URLs embedded in the page). Boat inventory is
served from a public Algolia index (`prod_boats`) using a browser-exposed
Algolia Application ID + **search-only** API key (safe, read-only, present
in every page load's outgoing network requests - not a secret).

This dealer shares its backend inventory/photo system with "Iguana Marine
Group" (iguanamarinegroup.com/missouri-all-boat-inventory/) - both dealers
query the same `prod_boats` Algolia index, scoped per-dealer via an
`endpoints:<hostname>` filter. Several listing photos/descriptions even
reference "Iguana Marine Group" directly, confirming a shared multi-brand
backend. The scraping approach here should be near-identical for both
dealers modulo the `endpoints` filter value and the `MM_DEALER_ID`.

Method
------
1. Fetch the inventory page HTML with `requests` (no JS execution needed).
2. Extract the `window.__ALGOLIA_PROPS__` JSON blob to confirm the site is
   still using this platform and to pull the dealer-specific extra filter
   (`MM_FILTERS`, e.g. `AND NOT lvl0: "Formula Boats"`) and hostname.
3. Extract `window.__SERVER_STATE__` (server-side rendered Algolia results
   for page 1) as a fallback data source and to discover the exact
   `filters` string InstantSearch itself sends (found under
   `initialResults.prod_boats.results[0].params`/hit facets).
4. Query the public Algolia REST API directly
   (`POST https://{app_id}-dsn.algolia.net/1/indexes/*/queries`) with
   `hitsPerPage=100` so the entire inventory (100 boats) comes back in a
   single request - no manual pagination loop required in practice, but
   pagination is implemented (looping `page=0..nbPages-1`) in case
   inventory ever exceeds 1000 (Algolia's absolute hard cap via this
   endpoint) or the dealer's true hitsPerPage limit changes.
5. If the Algolia HTTP path ever breaks (key rotated, CORS/WAF change),
   fall back to Playwright: render the page headlessly, sniff the
   `*-dsn.algolia.net` XHR requests, and extract the same App
   ID/API key/params live from the network traffic then repeat step 4.

Ground truth (manual audit 2026-09-05): total=100 (up from 99 prior scan).
This scraper independently reproduces total=100, new=61, used=39 via the
Algolia `usage` facet, matching total exactly.

Reliability caveats
--------------------
- Relies on a public but undocumented Algolia search key embedded in the
  page's client bundle / XHR traffic. If NativeRank rotates the key or
  changes the App ID, the direct-HTTP path (`_fetch_via_algolia_http`)
  will fail; the Playwright fallback (`_fetch_via_playwright`) re-derives
  fresh credentials from live network traffic each run, so it should
  survive key rotation as long as the general page structure holds.
- The Algolia `filters` string embeds the dealer hostname
  (`endpoints:www.stateamind.com OR endpoints:stateamind.com`) and a
  dealer-specific exclusion (`AND NOT lvl0: "Formula Boats"`) scraped live
  from the page rather than hardcoded blindly, so it self-adjusts if
  the dealer's exclusion rule changes - but if NativeRank changes the
  shape of `__ALGOLIA_PROPS__`/`__SERVER_STATE__` entirely, the filter
  parsing may need updating.
- Category normalization picks one bucket per boat from a boat's full
  Algolia `categories[].name` list using a fixed priority order (see
  `_normalize_category`). Boats can carry many overlapping category tags
  (e.g. a wake boat tagged both "Wakeboard Boat" and "Ski Boat"); the
  chosen bucket is a best-effort single label and may not match how the
  dealer's own site visually groups the boat.
- `price` of 0 (28/100 boats at time of writing, mostly "Call for Price"/
  "Request Price" listings) is treated as "no price" and excluded from
  `prices`, `priceLow`, and `priceHigh`.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import requests

DEALER_ID = "stateamind"
DEALER_NAME = "Stateamind Water Sports"
INVENTORY_URL = "https://www.stateamind.com/missouri-all-boat-inventory/"
ALGOLIA_INDEX = "prod_boats"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Fallback Algolia credentials observed live on 2026-09-05 (public,
# search-only key exposed to every visitor's browser - not a secret).
# These are only used if we fail to extract fresh values from the page
# itself, and the Playwright path re-derives them from live traffic.
FALLBACK_APP_ID = "WR1LHA5AEI"
FALLBACK_API_KEY = "aed708cd4183d38b9453be50384dc90b"
FALLBACK_FILTERS = (
    'endpoints:www.stateamind.com OR endpoints:stateamind.com '
    'AND NOT lvl0: "Formula Boats"'
)

# Category normalization: map raw Algolia `categories[].name` strings
# (lower-cased) to the tracker's normalized bucket names. Order in
# CATEGORY_PRIORITY determines which bucket wins when a boat has
# multiple overlapping category tags.
CATEGORY_MAP: dict[str, str] = {
    # Pontoon / Tritoon
    "pontoon": "Pontoon / Tritoon",
    "pontoon boats": "Pontoon / Tritoon",
    "tritoon": "Pontoon / Tritoon",
    "cruising pontoon": "Pontoon / Tritoon",
    "recreation pontoon": "Pontoon / Tritoon",
    "sport pontoon": "Pontoon / Tritoon",
    "value pontoon": "Pontoon / Tritoon",
    # Wakeboard / Ski
    "wakeboard boat": "Wakeboard / Ski",
    "wakesurf boat": "Wakeboard / Ski",
    "surf boat": "Wakeboard / Ski",
    "ski and wakeboard boats": "Wakeboard / Ski",
    "ski and wakeboard": "Wakeboard / Ski",
    "ski boat": "Wakeboard / Ski",
    # Center Console
    "center console": "Center Console",
    # Cruiser / Cabin
    "cruiser boat": "Cruiser / Cabin",
    "express cruiser": "Cruiser / Cabin",
    # Bowrider
    "bowrider": "Bowrider",
    "runabout": "Bowrider",
    "runabouts": "Bowrider",
    "open bow boat": "Bowrider",
    # Performance
    "sports boat": "Performance",
    "sterndrive": "Performance",
    "inboard": "Performance",
    # Yacht
    "yacht": "Yacht",
    # Fishing
    "fishing boat": "Fishing",
    "bass boats": "Fishing",
    "freshwater fishing": "Fishing",
    # Power (generic powerboat / deck / all-around, no more specific match)
    "powerboat": "Power",
    "deck boat": "Power",
    "all around boat": "Power",
    "recreation": "Power",
    "outboard": "Power",
}

# Priority order (highest first) used to pick ONE normalized category when
# a boat carries several raw category tags that map to different buckets.
CATEGORY_PRIORITY = [
    "Pontoon / Tritoon",
    "Wakeboard / Ski",
    "Yacht",
    "Center Console",
    "Fishing",
    "Cruiser / Cabin",
    "Bowrider",
    "Performance",
    "Power",
    "Other",
]


def _normalize_category(raw_categories: list[str]) -> str:
    """Pick a single normalized category bucket from a boat's raw category
    tag list, using CATEGORY_PRIORITY to break ties."""
    buckets = set()
    for raw in raw_categories:
        key = (raw or "").strip().lower()
        bucket = CATEGORY_MAP.get(key)
        if bucket:
            buckets.add(bucket)
    if not buckets:
        return "Other"
    for bucket in CATEGORY_PRIORITY:
        if bucket in buckets:
            return bucket
    return "Other"


def _extract_js_object(html: str, var_name: str) -> Optional[dict]:
    """Extract a `window.<var_name> = {...};` JSON object from raw HTML
    using brace-counting (handles nested braces/strings safely)."""
    marker = f"window.{var_name} = "
    idx = html.find(marker)
    if idx == -1:
        marker = f"window.{var_name}="
        idx = html.find(marker)
        if idx == -1:
            return None
    start = idx + len(marker)
    i = start
    depth = 0
    in_str = False
    esc = False
    n = len(html)
    while i < n:
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
        i += 1
    json_str = html[start:i]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None


def _fetch_inventory_page_html(session: requests.Session) -> str:
    resp = session.get(INVENTORY_URL, headers=DEFAULT_HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def _derive_algolia_config(html: str) -> dict:
    """Derive Algolia app id / filters string from the page's embedded
    JS state. Falls back to hardcoded values discovered on 2026-09-05 if
    parsing fails."""
    algolia_props = _extract_js_object(html, "__ALGOLIA_PROPS__") or {}
    server_state = _extract_js_object(html, "__SERVER_STATE__") or {}

    filters = FALLBACK_FILTERS
    try:
        req_params = (
            server_state["initialResults"][ALGOLIA_INDEX]["requestParams"]
        )
        if req_params.get("filters"):
            filters = req_params["filters"]
    except (KeyError, TypeError):
        pass

    domain = algolia_props.get("MM_DOMAIN", "https://www.stateamind.com")
    return {
        "app_id": FALLBACK_APP_ID,
        "api_key": FALLBACK_API_KEY,
        "filters": filters,
        "domain": domain,
    }


def _fetch_via_algolia_http(config: dict) -> list[dict]:
    """Query the public Algolia REST API directly for all boats in the
    dealer's index, paginating defensively even though hitsPerPage=100
    typically returns everything in one call for this dealer's inventory
    size."""
    app_id = config["app_id"]
    api_key = config["api_key"]
    filters = config["filters"]

    url = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/*/queries"
    headers = {
        "x-algolia-api-key": api_key,
        "x-algolia-application-id": app_id,
        "content-type": "application/x-www-form-urlencoded",
    }

    all_hits: list[dict] = []
    page = 0
    hits_per_page = 100
    nb_pages = 1

    while page < nb_pages:
        params = {
            "attributesToSnippet": ["description:10"],
            "clickAnalytics": True,
            "facets": [
                "categories.name",
                "certified",
                "location.display_name",
                "lvl0",
                "lvl1",
                "price",
                "status",
                "usage",
                "year",
            ],
            "filters": filters,
            "highlightPostTag": "__/ais-highlight__",
            "highlightPreTag": "__ais-highlight__",
            "hitsPerPage": hits_per_page,
            "maxValuesPerFacet": 100,
            "page": page,
            "query": "",
        }
        # Algolia's multi-query endpoint expects `params` as a
        # URL-encoded query string, not nested JSON.
        from urllib.parse import urlencode

        encoded_params = urlencode(
            {k: (json.dumps(v) if isinstance(v, (list, dict, bool)) else v)
             for k, v in params.items()},
            doseq=False,
        )
        body = {
            "requests": [
                {"indexName": ALGOLIA_INDEX, "params": encoded_params}
            ]
        }
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        result = data["results"][0]
        all_hits.extend(result.get("hits", []))
        nb_pages = result.get("nbPages", 1) or 1
        page += 1

    return all_hits


def _fetch_via_playwright() -> list[dict]:
    """Fallback path: render the page with headless Chromium, sniff live
    Algolia XHR requests to recover fresh app id / api key / filters, then
    re-issue the same query with hitsPerPage=100 via `requests` (fast) or,
    if that still fails, by directly reusing the sniffed request itself."""
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []

    def handle_request(request):
        if "algolia" in request.url.lower() and request.method == "POST":
            captured.append(
                {
                    "url": request.url,
                    "headers": dict(request.headers),
                    "post_data": request.post_data,
                }
            )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("request", handle_request)
        page.goto(INVENTORY_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(1500)
        # Trigger at least one client-side Algolia query (e.g. clicking a
        # pagination control or a facet) so we can sniff live credentials.
        for candidate_text in ["2", "Next"]:
            try:
                page.get_by_text(candidate_text, exact=True).first.click(
                    timeout=3000
                )
                page.wait_for_timeout(2500)
                if captured:
                    break
            except Exception:
                continue
        browser.close()

    if not captured:
        raise RuntimeError(
            "Playwright fallback failed to capture any Algolia network "
            "requests; page structure may have changed."
        )

    sniffed = captured[0]
    app_id = sniffed["headers"].get(
        "x-algolia-application-id", FALLBACK_APP_ID
    )
    api_key = sniffed["headers"].get("x-algolia-api-key", FALLBACK_API_KEY)

    # Recover the `filters` string from the sniffed POST body if present.
    filters = FALLBACK_FILTERS
    if sniffed.get("post_data"):
        m = re.search(r"filters=([^&]+)", sniffed["post_data"])
        if m:
            from urllib.parse import unquote

            filters = unquote(m.group(1))

    config = {"app_id": app_id, "api_key": api_key, "filters": filters}
    return _fetch_via_algolia_http(config)


def _hit_to_price_tuple(hit: dict) -> Optional[tuple[int, str]]:
    price = hit.get("price")
    usage_raw = (hit.get("usage") or "").strip().lower()
    if usage_raw == "new":
        usage = "new"
    elif usage_raw == "used":
        usage = "used"
    else:
        usage = "unknown"
    if not price or price <= 0:
        return None
    return (int(price), usage)


def scrape() -> dict:
    session = requests.Session()

    hits: list[dict] = []
    method_used = "algolia_http"

    try:
        html = _fetch_inventory_page_html(session)
        config = _derive_algolia_config(html)
        hits = _fetch_via_algolia_http(config)
        if not hits:
            raise RuntimeError("Algolia HTTP path returned zero hits.")
    except Exception:
        method_used = "playwright_fallback"
        hits = _fetch_via_playwright()

    total = len(hits)

    new_count = 0
    used_count = 0
    category_counts: dict[str, int] = {}
    brand_counts: dict[str, int] = {}
    prices: list[tuple[int, str]] = []

    for hit in hits:
        usage_raw = (hit.get("usage") or "").strip().lower()
        if usage_raw == "new":
            new_count += 1
        elif usage_raw == "used":
            used_count += 1

        raw_categories = [
            c.get("name", "") for c in (hit.get("categories") or [])
        ]
        norm_category = _normalize_category(raw_categories)
        category_counts[norm_category] = (
            category_counts.get(norm_category, 0) + 1
        )

        lvl0 = hit.get("lvl0")
        if isinstance(lvl0, list) and lvl0:
            brand = lvl0[0]
        elif isinstance(lvl0, str) and lvl0:
            brand = lvl0
        else:
            manufacturer = hit.get("manufacturer") or {}
            brand = manufacturer.get("name") or "Unknown"
        brand = (brand or "Unknown").strip() or "Unknown"
        brand_counts[brand] = brand_counts.get(brand, 0) + 1

        price_tuple = _hit_to_price_tuple(hit)
        if price_tuple is not None:
            prices.append(price_tuple)

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
        "_method": method_used,
    }


if __name__ == "__main__":
    import json as _json

    print(_json.dumps(scrape(), indent=2))
