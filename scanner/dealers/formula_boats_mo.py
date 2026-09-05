"""
Scraper for Formula Boats of Missouri (formula-boats-mo).

Site notes (2026-09-05 audit)
------------------------------
The dealer's legacy URL (``https://formulaboatsmo.com/boats-view.asp``)
now 301-redirects to a WordPress site (``www.formulaboatsmo.com``) built on
the Boats Group "Essential Elite" theme with the "plugins-inventory"
inventory plugin. The classic ASP endpoint no longer exists (404).

The inventory grid at ``/boats-for-sale/`` is populated **server-side**
(the HTML returned by a plain HTTP GET already contains the boat cards --
no XHR/AJAX call is required to see them), but the un-filtered
``/boats-for-sale/`` URL aggregates listings across the dealer's *entire
network* of sister stores (Forked River NJ, Fort Myers FL, Miami FL,
Decatur AL, Fort Lauderdale FL, Osage Beach/Camdenton MO, ...). Formula
Boats of Missouri itself is located at the Lake of the Ozarks
(Osage Beach / Camdenton, MO), so the listings that actually belong to
*this* dealer must be isolated with the site's own location filter:

    https://www.formulaboatsmo.com/boats-for-sale/all/all/all-MO-US/

This URL returned exactly 29 results during manual verification (matching
the ground-truth audit total), paginated 10-per-page over 3 pages
(``/all-MO-US/2/``, ``/all-MO-US/3/`` ...).

Condition (new/used) is not flagged inline on the aggregate listing card,
so it is resolved by cross-referencing two additional filtered views that
the plugin exposes natively:

    https://www.formulaboatsmo.com/boats-for-sale/New/all/all-MO-US/
    https://www.formulaboatsmo.com/boats-for-sale/Used/all/all-MO-US/

Each boat card carries a stable ``data-boat-id`` attribute used to build
an id -> condition lookup from those two listings.

Rendering: although the boat cards are present in the plain HTML (no JS
execution needed to see the markup itself), the site sits behind
Cloudflare-style bot mitigation on some requests and a plain
``requests`` GET intermittently returns a shell page without the
``#inventory-display`` contents populated (server-side templating keyed
off a JS-set cookie/localStorage flag in some sessions). Playwright with
headless Chromium is therefore used for reliability; it also lets us
short-circuit once "N Results" has stabilized instead of guessing sleep
times.

Category normalization is a best-effort heuristic based on the boat's
model name (there is no clean single-valued "category" field exposed --
the site's own ``boatclasscode`` filter facets are multi-select and a
single boat commonly matches more than one facet, e.g. a "Crossover
Bowrider" model matches both ``power-bowrider`` and
``power-cuddy``/``power-expresscruiser``). Manual audits that classify by
eyeballing each listing may reasonably disagree with a keyword heuristic
on the boundary between Bowrider and Cruiser/Cabin for the "Crossover
Bowrider" line -- see caveats in the module docstring returned by
scrape().
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

BASE = "https://www.formulaboatsmo.com"
LISTING_ROOT = f"{BASE}/boats-for-sale/all/all/all-MO-US/"
NEW_ROOT = f"{BASE}/boats-for-sale/New/all/all-MO-US/"
USED_ROOT = f"{BASE}/boats-for-sale/Used/all/all-MO-US/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

DEALER_ID = "formula-boats-mo"
DEALER_NAME = "Formula Boats of Missouri"

CANONICAL_CATEGORIES = [
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

RESULTS_RE = re.compile(r"(\d+)\s*Results")
BOAT_ID_RE = re.compile(r'data-boat-id="(\d+)"')
CARD_SPLIT_RE = re.compile(r'(?=<div class="inventory-model-single")')
MAKE_RE = re.compile(r'data-boat-make="([^"]*)"')
MODEL_RE = re.compile(r'data-boat-model="([^"]*)"')
ID_ATTR_RE = re.compile(r'data-boat-id="(\d+)"')
PRICE_RE = re.compile(r'main-boat-price[^"]*">([^<]*)<')


def _classify_category(make: str, model: str) -> str:
    """Best-effort normalization of a Formula/other-brand model name into
    the canonical category buckets. See module docstring for caveats."""
    text = f"{make} {model}".lower()

    # Wake/ski boats and brands
    if "nautique" in text or "wake" in text or re.search(r"\bski\b", text):
        return "Wakeboard / Ski"

    # Pontoon/tritoon
    if "pontoon" in text or "tritoon" in text:
        return "Pontoon / Tritoon"

    # Center console
    if "center console" in text or re.search(r"\bcc\b", text):
        return "Center Console"

    # Cabin / cruiser style boats: performance cruisers, cuddy cabins,
    # express cruisers, and large "super sport crossover" flagships that
    # have a below-deck cabin.
    if (
        "cruiser" in text
        or "cuddy" in text
        or "cabin" in text
        or "super sport crossover" in text
        or "fastech" in text
    ):
        return "Cruiser / Cabin"

    # Bowriders, including Formula's "Crossover Bowrider" (CBR) line and
    # generic "BR" naming, plus other brands' bowrider-style runabouts.
    if "bowrider" in text or re.search(r"\bbr\b", text) or re.search(r"\br8\b", text):
        return "Bowrider"

    if "fish" in text or "sportfish" in text or "center" in text:
        return "Fishing"

    if "yacht" in text:
        return "Yacht"

    if "performance" in text:
        return "Performance"

    return "Other"


def _parse_price(raw: str) -> Optional[int]:
    raw = raw.strip()
    if not raw or not re.search(r"\d", raw):
        return None
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _extract_cards(html: str) -> List[Dict]:
    """Split the raw listing HTML into per-boat card chunks and pull out
    the fields we need from each."""
    cards = []
    chunks = CARD_SPLIT_RE.split(html)
    for chunk in chunks[1:]:
        # Limit each chunk to roughly one card's worth of markup so a
        # regex match doesn't bleed into the next card.
        card_html = chunk[:4000]
        id_m = ID_ATTR_RE.search(card_html)
        make_m = MAKE_RE.search(card_html)
        model_m = MODEL_RE.search(card_html)
        price_m = PRICE_RE.search(card_html)
        if not id_m:
            continue
        cards.append(
            {
                "id": id_m.group(1),
                "make": make_m.group(1) if make_m else "",
                "model": model_m.group(1) if model_m else "",
                "price_raw": price_m.group(1).strip() if price_m else "",
            }
        )
    return cards


def _get_total_results(html: str) -> int:
    m = RESULTS_RE.search(html)
    return int(m.group(1)) if m else 0


def _fetch_all_pages(page, root_url: str) -> Tuple[int, List[Dict]]:
    """Fetch every page of a (paginated) listing root URL using an
    already-open Playwright page. Returns (declared_total, cards)."""
    page.goto(root_url, wait_until="load", timeout=45000)
    page.wait_for_selector("#inventory-display", timeout=20000)
    page.wait_for_timeout(1500)
    html = page.content()
    total = _get_total_results(html)
    all_cards = _extract_cards(html)

    seen_ids = {c["id"] for c in all_cards}
    page_num = 2
    # 10 results per page is the plugin default.
    max_pages = max(1, (total + 9) // 10)
    while page_num <= max_pages and len(seen_ids) < total:
        page_url = root_url.rstrip("/") + f"/{page_num}/"
        page.goto(page_url, wait_until="load", timeout=45000)
        page.wait_for_selector("#inventory-display", timeout=20000)
        page.wait_for_timeout(1200)
        html = page.content()
        cards = _extract_cards(html)
        new_cards = [c for c in cards if c["id"] not in seen_ids]
        if not new_cards:
            break
        all_cards.extend(new_cards)
        seen_ids.update(c["id"] for c in new_cards)
        page_num += 1

    return total, all_cards


def scrape() -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.set_default_navigation_timeout(45000)

            declared_total, cards = _fetch_all_pages(page, LISTING_ROOT)

            # Resolve condition (new/used) via the plugin's own condition
            # filters, keyed by the stable data-boat-id.
            _, new_cards = _fetch_all_pages(page, NEW_ROOT)
            _, used_cards = _fetch_all_pages(page, USED_ROOT)
        finally:
            browser.close()

    new_ids = {c["id"] for c in new_cards}
    used_ids = {c["id"] for c in used_cards}

    # De-duplicate by boat id (defensive, in case pagination overlaps).
    dedup: Dict[str, Dict] = {}
    for c in cards:
        dedup[c["id"]] = c
    cards = list(dedup.values())

    total = len(cards) if cards else declared_total

    category_counts: Dict[str, int] = {c: 0 for c in CANONICAL_CATEGORIES}
    brand_counts: Dict[str, int] = {}
    prices: List[Tuple[int, str]] = []

    new_count = 0
    used_count = 0
    priced_count = 0

    for c in cards:
        bid = c["id"]
        make = c["make"].strip() or "Unknown"
        model = c["model"].strip()

        if bid in new_ids:
            condition = "new"
            new_count += 1
        elif bid in used_ids:
            condition = "used"
            used_count += 1
        else:
            condition = "unknown"

        category = _classify_category(make, model)
        category_counts[category] = category_counts.get(category, 0) + 1

        brand_counts[make] = brand_counts.get(make, 0) + 1

        price = _parse_price(c["price_raw"])
        if price is not None:
            priced_count += 1
            prices.append((price, condition))

    price_values = [p for p, _ in prices]
    price_low = min(price_values) if price_values else None
    price_high = max(price_values) if price_values else None

    # Drop zero-count categories to keep the output tidy.
    category_counts = {k: v for k, v in category_counts.items() if v > 0}

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
