"""
Scraper for Performance Marine Watersports (performance-marine).

URL: https://www.performanceloz.com/missouri-new-used-boats-for-sale/

Site notes / method
--------------------
The dealer's inventory page is built on the "MarineManager" CMS
(cdn.marinemanager.com), which renders an Algolia InstantSearch-style
widget (elements carry `ais-InfiniteHits-*` classes) but the results are
delivered server-side rendered into the initial HTML rather than via a
client-visible Algolia XHR call — no `algolia.net` network requests were
observed even after scrolling. This is the same family of dealer site
templates seen on iguana-marine / stateamind (marine-industry CMS
vendor), so the same "load first N cards, click Show More" pattern
applies.

Behavior observed on 2026-09-05:
  * Initial HTML/DOM load already contains 20 `<li class="ais-InfiniteHits-item">`
    cards (first page of results) - a plain requests/BeautifulSoup GET
    only sees these 20.
  * A "Show More" button (`button:has-text("Show More")`) is present at
    the bottom of the results list. Clicking it once loads the
    remaining cards so all cards for the current filter are present in
    the DOM (37/37 in the audit run: no more "Show More" button after
    one click). Repeated clicking until the button disappears makes this
    robust for other dealers on the same CMS with more inventory.
  * A "37 results" / "Showing 37" counter is also rendered, useful as a
    sanity cross-check against len(cards).

Because the full listing set only appears after a JS interaction
(clicking "Show More"), this scraper uses Playwright (sync API,
headless Chromium) rather than plain requests. A plain-HTML fallback
path is included in case the site ever serves everything without JS
(e.g. behind a CDN cache), but in practice this dealer requires
Playwright.

Card fields used per listing:
  * New/used     -> the listing detail link contains either
                     "/new-boats-for-sale/" or "/used-boats-for-sale/"
                     (falls back to the "New"/"Used" usage badge text).
  * Title         -> <h2> inside the card (e.g. "2025 Supra SE550").
  * Brand         -> the "Manufacturer" stat block, falls back to the
                     brand slug in the detail URL
                     (.../new-boats-for-sale/<brand-slug>/<slug>).
  * Price         -> <strong class="font-bold ..."> holds the current
                     asking price when a strikethrough MSRP is also
                     shown; otherwise the single (non-strikethrough)
                     "$" amount in the price block is used. Listings
                     with no numeric price ("Call for Price" / no price
                     block, e.g. "Pending Sale" units with hidden price)
                     are counted toward total/new/used but excluded from
                     the `prices` list and priceLow/priceHigh.

Category normalization is inferred from brand + title keywords, since
the site does not expose a distinct "boat type" facet value on the
card itself. Brands seen for this dealer are wake/ski specialists
(MasterCraft, Supra, Nautique, Malibu, Moomba, Tige) and pontoon
brands (Crest, Balise, Godfrey, etc.) - mapped accordingly, with a
keyword-based fallback for any brand not in the static map.
"""

from __future__ import annotations

import json
import re
import time
from typing import Optional

DEALER_ID = "performance-marine"
DEALER_NAME = "Performance Marine Watersports"
LISTING_URL = "https://www.performanceloz.com/missouri-new-used-boats-for-sale/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

CATEGORIES = [
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

# Brand -> normalized category. Keys are lowercased, punctuation-stripped.
BRAND_CATEGORY_MAP = {
    "mastercraft": "Wakeboard / Ski",
    "supra": "Wakeboard / Ski",
    "nautique": "Wakeboard / Ski",
    "malibu boats": "Wakeboard / Ski",
    "malibu": "Wakeboard / Ski",
    "moomba": "Wakeboard / Ski",
    "tige boats": "Wakeboard / Ski",
    "tige": "Wakeboard / Ski",
    "axis": "Wakeboard / Ski",
    "centurion": "Wakeboard / Ski",
    "crest": "Pontoon / Tritoon",
    "balise": "Pontoon / Tritoon",
    "godfrey": "Pontoon / Tritoon",
    "bennington": "Pontoon / Tritoon",
    "barletta": "Pontoon / Tritoon",
    "sun tracker": "Pontoon / Tritoon",
    "suntracker": "Pontoon / Tritoon",
    "pontoon": "Pontoon / Tritoon",
    "manitou": "Pontoon / Tritoon",
    "premier": "Pontoon / Tritoon",
    "avalon": "Pontoon / Tritoon",
}

KEYWORD_CATEGORY_RULES = [
    (r"\bponton|tritoon\b", "Pontoon / Tritoon"),
    (r"\bwake\s*board|\bski\b|surf\b|wakesetter|x-star|xstar", "Wakeboard / Ski"),
    (r"center\s*console", "Center Console"),
    (r"cuddy|cabin|cruiser", "Cruiser / Cabin"),
    (r"bowrider|bow rider", "Bowrider"),
    (r"\byacht\b", "Yacht"),
    (r"fish|bass|walleye|skiff", "Fishing"),
]


def _norm(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _norm_key(text: Optional[str]) -> str:
    t = _norm(text).lower()
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return t.strip()


def _parse_price(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"([\d,]+)", text)
    if not m:
        return None
    try:
        val = int(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return val if val > 0 else None


def categorize(brand: str, title: str) -> str:
    key = _norm_key(brand)
    if key in BRAND_CATEGORY_MAP:
        return BRAND_CATEGORY_MAP[key]
    # try partial brand match (e.g. "MasterCraft Boats")
    for b, cat in BRAND_CATEGORY_MAP.items():
        if b and b in key:
            return cat
    hay = f"{brand} {title}".lower()
    for pattern, cat in KEYWORD_CATEGORY_RULES:
        if re.search(pattern, hay):
            return cat
    return "Other"


def _extract_cards_from_html(html: str) -> list[dict]:
    """Parse rendered HTML (after JS/Show-More expansion) into raw listing dicts."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("li.ais-InfiniteHits-item")
    results = []

    for card in cards:
        link = card.find("a", href=True)
        href = link["href"] if link else ""

        condition = "unknown"
        if "/new-boats-for-sale/" in href:
            condition = "new"
        elif "/used-boats-for-sale/" in href:
            condition = "used"
        else:
            badge = card.select_one(
                "[class*='mm-usage-badge-bg'], [class*='usage-badge']"
            )
            if badge:
                bt = _norm(badge.get_text()).lower()
                if "new" in bt:
                    condition = "new"
                elif "used" in bt or "pre-owned" in bt or "preowned" in bt:
                    condition = "used"

        h2 = card.select_one("h2")
        title = _norm(h2.get_text()) if h2 else ""

        # Brand: prefer explicit "Manufacturer" stat block, else URL slug.
        brand = ""
        for stat in card.select(".hit-content .px-4.text-xs.text-center, .px-4.text-xs.text-center"):
            spans = stat.find_all("span")
            if len(spans) >= 2:
                label = _norm(spans[0].get_text()).lower()
                if "manufacturer" in label:
                    brand = _norm(spans[1].get_text())
                    break
        if not brand and href:
            parts = [p for p in href.split("/") if p]
            for i, p in enumerate(parts):
                if p in ("new-boats-for-sale", "used-boats-for-sale") and i + 1 < len(parts):
                    brand = parts[i + 1].replace("-", " ").title()
                    break

        # Price: strikethrough MSRP + strong sale price, or single price, or none.
        price = None
        strong_price = card.select_one("strong.font-bold")
        if strong_price:
            price = _parse_price(strong_price.get_text())
        if price is None:
            price_block = card.select_one(".text-2xl, .text-3xl")
            if price_block:
                # remove strikethrough MSRP text if present, take remaining $ amount
                clone_text = price_block.get_text(" ", strip=True)
                strike = price_block.select_one(".line-through")
                if strike:
                    strike_text = strike.get_text(" ", strip=True)
                    clone_text = clone_text.replace(strike_text, "", 1)
                price = _parse_price(clone_text)

        results.append(
            {
                "title": title,
                "brand": brand,
                "condition": condition,
                "price": price,
                "url": href,
            }
        )

    return results


def _fetch_html_playwright(url: str, timeout_ms: int = 60000) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(4000)

            # Expand results: click "Show More" until it stops appearing,
            # capped to avoid infinite loops on unexpected markup changes.
            for _ in range(15):
                btn = page.query_selector("button:has-text('Show More')")
                if not btn:
                    break
                try:
                    btn.scroll_into_view_if_needed()
                    btn.click()
                    page.wait_for_timeout(1500)
                except Exception:
                    break

            html = page.content()
        finally:
            browser.close()
    return html


def _fetch_html_requests(url: str) -> str:
    import requests

    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def scrape() -> dict:
    """Scrape Performance Marine Watersports inventory.

    Returns a dict:
        {
          'id': 'performance-marine',
          'name': 'Performance Marine Watersports',
          'total': int,
          'new': int,
          'used': int,
          'category': {category_name: count, ...},
          'brand': {brand_name: count, ...},
          'prices': [(price_int, 'new'|'used'|'unknown'), ...],
          'priceLow': int or None,
          'priceHigh': int or None,
        }
    """
    html = None
    method = "playwright"
    try:
        html = _fetch_html_playwright(LISTING_URL)
        cards = _extract_cards_from_html(html)
        # Sanity check: plain HTML would only have ~20 cards (page 1).
        # If Playwright somehow also only yielded ~20, that's still the
        # best data available; we do not fall back in that case since a
        # requests-only fetch would yield the same or fewer results.
    except Exception:
        method = "requests-fallback"
        html = _fetch_html_requests(LISTING_URL)
        cards = _extract_cards_from_html(html)

    total = len(cards)
    new_count = sum(1 for c in cards if c["condition"] == "new")
    used_count = sum(1 for c in cards if c["condition"] == "used")

    category_counts: dict = {}
    brand_counts: dict = {}
    prices: list = []

    for c in cards:
        brand = _norm(c["brand"]).title() if c["brand"] else "Unknown"
        brand_counts[brand] = brand_counts.get(brand, 0) + 1

        cat = categorize(brand, c["title"])
        category_counts[cat] = category_counts.get(cat, 0) + 1

        if c["price"] is not None:
            prices.append((c["price"], c["condition"]))

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
        "_method": method,  # informational; not part of the required schema
    }


if __name__ == "__main__":
    import json

    print(json.dumps(scrape(), indent=2))
