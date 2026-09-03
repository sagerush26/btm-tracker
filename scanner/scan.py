import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from playwright.async_api import async_playwright


# ============================================================
# BTM COMPETITOR TRACKER
#
# IMPORTANT:
# - This scanner DOES NOT silently carry old numbers forward.
# - If a dealer cannot be verified, the scan fails.
# - The website date only advances after a fully valid scan.
# ============================================================


def find_root():
    here = Path.cwd()

    if (here / "data" / "competitors.json").exists():
        return here

    matches = list(here.glob("**/data/competitors.json"))

    if not matches:
        raise FileNotFoundError("Could not find data/competitors.json")

    return matches[0].parent.parent


ROOT = find_root()
DATA = ROOT / "data"

COMPETITORS_FILE = DATA / "competitors.json"
SCANS_FILE = DATA / "scans.json"
TRENDS_FILE = DATA / "trends.json"
MODEL_MIX_FILE = DATA / "model-mix.json"


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def money_values(text):
    values = []

    for match in re.findall(r"\$\s*([0-9][0-9,]{3,})", text):
        try:
            value = int(match.replace(",", ""))

            if 1000 <= value <= 10_000_000:
                values.append(value)

        except ValueError:
            pass

    return list(dict.fromkeys(values))


def first_valid_match(text, patterns):
    lower = text.lower()

    for pattern in patterns:
        matches = re.findall(pattern, lower, re.I)

        for match in matches:
            try:
                if isinstance(match, tuple):
                    match = match[-1]

                value = int(str(match).replace(",", ""))

                if 1 <= value <= 1000:
                    return value

            except Exception:
                continue

    return None


def general_total_from_text(text):
    patterns = [
        r"showing\s+\d+\s*[-–]\s*\d+\s+of\s+([\d,]+)",
        r"showing\s+([\d,]+)\s+boats?",
        r"([\d,]+)\s+total\s+results?",
        r"([\d,]+)\s+results?",
        r"([\d,]+)\s+boats?\s+found",
        r"([\d,]+)\s+boats?\s+available",
        r"inventory\s*\(\s*([\d,]+)\s*\)",
    ]

    return first_valid_match(text, patterns)


def condition_counts(text, total=None):
    lower = text.lower()

    new_count = first_valid_match(
        lower,
        [
            r"\bnew\s*\(\s*([\d,]+)\s*\)",
            r"\bnew boats?\s*\(\s*([\d,]+)\s*\)",
            r"\bnew\s+([\d,]+)\b",
        ],
    )

    used_count = first_valid_match(
        lower,
        [
            r"\bused\s*\(\s*([\d,]+)\s*\)",
            r"\bpre[- ]owned\s*\(\s*([\d,]+)\s*\)",
            r"\bused boats?\s*\(\s*([\d,]+)\s*\)",
        ],
    )

    if total:
        if new_count is not None and new_count > total:
            new_count = None

        if used_count is not None and used_count > total:
            used_count = None

    return new_count, used_count


async def scroll_page(page):
    previous_height = 0

    for _ in range(15):
        try:
            current_height = await page.evaluate("document.body.scrollHeight")

            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )

            await page.wait_for_timeout(700)

            buttons = page.locator(
                "button:has-text('Load More'),"
                "a:has-text('Load More'),"
                "button:has-text('Show More'),"
                "a:has-text('Show More'),"
                "button:has-text('View More'),"
                "a:has-text('View More')"
            )

            count = await buttons.count()

            for i in range(min(count, 3)):
                try:
                    button = buttons.nth(i)

                    if await button.is_visible():
                        await button.click(timeout=2000)
                        await page.wait_for_timeout(800)

                except Exception:
                    pass

            if current_height == previous_height:
                break

            previous_height = current_height

        except Exception:
            break


async def unique_listing_links(page):
    selectors = [
        "a[href*='/inventory/']",
        "a[href*='/boat-inventory/']",
        "a[href*='/boats-for-sale/']",
        "a[href*='/listing/']",
        "a[href*='/listings/']",
        "a[href*='unitdetails']",
        "a[href*='unit-detail']",
        "a[href*='inventory-details']",
    ]

    links = set()

    for selector in selectors:
        try:
            elements = page.locator(selector)
            count = await elements.count()

            for i in range(count):
                try:
                    href = await elements.nth(i).get_attribute("href")

                    if not href:
                        continue

                    href_lower = href.lower()

                    # Ignore generic inventory landing pages.
                    if href_lower.rstrip("/").endswith(
                        (
                            "/inventory",
                            "/boats-for-sale",
                            "/listings",
                        )
                    ):
                        continue

                    links.add(href)

                except Exception:
                    continue

        except Exception:
            continue

    return links


async def repeated_card_count(page):
    selectors = [
        "[class*='inventory-item']",
        "[class*='inventoryItem']",
        "[class*='inventory-card']",
        "[class*='vehicle-card']",
        "[class*='listing-card']",
        "[class*='boat-card']",
        "[class*='unit-card']",
        "[class*='product-card']",
    ]

    counts = []

    for selector in selectors:
        try:
            count = await page.locator(selector).count()

            if 2 <= count <= 500:
                counts.append(count)

        except Exception:
            continue

    return max(counts) if counts else None


def surdyke_boat_only_url(url):
    """
    The stored Surdyke URL can include boats + PWC + outboards.
    Force this scanner to request Boat inventory only.
    """

    parts = urlsplit(url)

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "category"
    ]

    query.append(("category", "boat"))

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query),
            parts.fragment,
        )
    )


def dealer_specific_total(dealer_id, text):
    lower = text.lower()

    patterns = {
        "performance-boat-center": [
            r"showing\s+([\d,]+)\s+boats?",
            r"([\d,]+)\s+boats?",
        ],

        "kellys-port": [
            r"showing\s+\d+\s*[-–]\s*\d+\s+of\s+([\d,]+)\s+results?",
        ],

        "iguana-marine": [
            r"([\d,]+)\s+total\s+results?",
            r"([\d,]+)\s+results?",
        ],

        "stateamind": [
            r"([\d,]+)\s+total\s+results?",
            r"([\d,]+)\s+results?",
        ],

        "all-about-boats": [
            r"\d+\s*[-–]\s*\d+\s+of\s+([\d,]+)\s+results?",
            r"showing\s+\d+\s*[-–]\s*\d+\s+of\s+([\d,]+)",
        ],

        "surdyke-yamaha": [
            r"showing\s+\d+\s*[-–]\s*\d+\s+of\s+([\d,]+)",
            r"([\d,]+)\s+results?",
        ],
    }

    dealer_patterns = patterns.get(dealer_id, [])

    if dealer_patterns:
        value = first_valid_match(lower, dealer_patterns)

        if value:
            return value

    return general_total_from_text(lower)


async def scrape_dealer(browser, dealer):
    dealer_id = dealer["id"]
    name = dealer["name"]
    configured_url = dealer["url"]

    url = configured_url

    if dealer_id == "surdyke-yamaha":
        url = surdyke_boat_only_url(configured_url)

    print("")
    print("=" * 70)
    print(f"SCANNING: {name}")
    print(f"URL: {url}")
    print("=" * 70)

    context = await browser.new_context(
        viewport={"width": 1440, "height": 1200},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )

    page = await context.new_page()

    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        if response:
            print(f"HTTP STATUS: {response.status}")

        await page.wait_for_timeout(4000)

        await scroll_page(page)

        body_text = await page.locator("body").inner_text(timeout=20000)
        body_text = clean_text(body_text)

        if len(body_text) < 100:
            raise RuntimeError("Page returned almost no readable content")

        displayed_total = dealer_specific_total(
            dealer_id,
            body_text,
        )

        links = await unique_listing_links(page)
        card_count = await repeated_card_count(page)

        print(f"Displayed total: {displayed_total}")
        print(f"Unique listing links found: {len(links)}")
        print(f"Repeated card count: {card_count}")

        total = displayed_total

        verification_method = "displayed-count"

        # If there is no explicit dealer result count,
        # fall back to identifiable listing links/cards.
        if total is None:
            if len(links) >= 2:
                total = len(links)
                verification_method = "unique-listing-links"

            elif card_count and card_count >= 2:
                total = card_count
                verification_method = "listing-card-count"

        if total is None or total <= 0:
            raise RuntimeError(
                "Could not verify a current inventory total"
            )

        if total > 1000:
            raise RuntimeError(
                f"Implausible total returned: {total}"
            )

        new_count, used_count = condition_counts(
            body_text,
            total,
        )

        prices = money_values(body_text)

        if prices:
            avg_price = int(mean(prices))
            low_price = min(prices)
            high_price = max(prices)

            price_range = (
                f"${low_price:,.0f} - ${high_price:,.0f}"
            )

        else:
            avg_price = 0
            price_range = "Unavailable"

        result = {
            "competitorId": dealer_id,
            "totalBoats": total,
            "newBoats": new_count if new_count is not None else 0,
            "usedBoats": used_count if used_count is not None else 0,
            "avgPrice": avg_price,
            "avgPriceNew": 0,
            "avgPriceUsed": 0,
            "priceRange": price_range,
            "verification": {
                "status": "verified",
                "method": verification_method,
                "sourceCount": displayed_total,
                "listingLinksFound": len(links),
                "cardCount": card_count,
                "checkedAt": datetime.now(timezone.utc).isoformat(),
                "url": url,
            },
        }

        print(
            f"VERIFIED: {name} = {total} boats "
            f"using {verification_method}"
        )

        return result

    except Exception as exc:
        print(f"FAILED: {name}")
        print(f"REASON: {exc}")

        return {
            "competitorId": dealer_id,
            "dealerName": name,
            "error": str(exc),
            "verification": {
                "status": "failed",
                "checkedAt": datetime.now(timezone.utc).isoformat(),
                "url": url,
            },
        }

    finally:
        await context.close()


def next_scan_id(scans):
    existing = scans.get("scans", [])

    highest = 0

    for scan in existing:
        scan_id = str(scan.get("scanId", ""))

        match = re.search(r"(\d+)$", scan_id)

        if match:
            highest = max(
                highest,
                int(match.group(1)),
            )

    return f"scan-{highest + 1:03d}"


def previous_result_map(scans):
    if not scans.get("scans"):
        return {}

    latest = scans["scans"][-1]

    return {
        item["competitorId"]: item
        for item in latest.get("results", [])
    }


def make_changes(previous, results, scan_id, scan_date):
    changes = []

    for current in results:
        dealer_id = current["competitorId"]

        old = previous.get(dealer_id)

        if not old:
            continue

        before = old.get("totalBoats", 0)
        after = current.get("totalBoats", 0)

        delta = after - before

        if delta == 0:
            continue

        changes.append(
            {
                "scanId": scan_id,
                "date": scan_date,
                "competitorId": dealer_id,
                "previousTotal": before,
                "newTotal": after,
                "change": delta,
                "description": (
                    f"{dealer_id}: "
                    f"{before} -> {after} ({delta:+d})"
                ),
            }
        )

    return changes


def update_trends(
    trends,
    results,
    scan_id,
    scan_date,
):
    total_market = sum(
        result.get("totalBoats", 0)
        for result in results
    )

    btm = next(
        (
            result.get("totalBoats", 0)
            for result in results
            if result["competitorId"]
            == "big-thunder-marine"
        ),
        0,
    )

    competitors_total = total_market - btm

    inventory = trends.setdefault(
        "inventoryTrend",
        [],
    )

    inventory.append(
        {
            "date": scan_date,
            "totalMarket": total_market,
            "btm": btm,
            "competitors": competitors_total,
            "total": total_market,
            "scanId": scan_id,
        }
    )

    trends["inventoryTrend"] = inventory[-180:]


def update_model_mix(
    model_mix,
    competitors,
    results,
):
    metadata = {
        dealer["id"]: dealer
        for dealer in competitors["competitors"]
    }

    existing = {
        dealer["id"]: dealer
        for dealer in model_mix.get("dealers", [])
    }

    updated = []

    for result in results:
        dealer_id = result["competitorId"]

        old = dict(
            existing.get(
                dealer_id,
                {},
            )
        )

        meta = metadata.get(
            dealer_id,
            {},
        )

        old["id"] = dealer_id
        old["name"] = meta.get(
            "name",
            dealer_id,
        )

        old["isOwn"] = bool(
            meta.get(
                "isOwn",
                False,
            )
        )

        old["totalBoats"] = result.get(
            "totalBoats",
            0,
        )

        old["newBoats"] = result.get(
            "newBoats",
            0,
        )

        old["usedBoats"] = result.get(
            "usedBoats",
            0,
        )

        old["avgPriceNew"] = result.get(
            "avgPriceNew",
            0,
        )

        old["avgPriceUsed"] = result.get(
            "avgPriceUsed",
            0,
        )

        old["byCondition"] = {
            "New": result.get(
                "newBoats",
                0,
            ),
            "Used": result.get(
                "usedBoats",
                0,
            ),
        }

        # IMPORTANT:
        # Brand/category/model breakdowns remain unchanged
        # until we build listing-level extraction.
        for key in [
            "byCategory",
            "byCategoryNew",
            "byCategoryUsed",
            "byLength",
            "byLengthNew",
            "byLengthUsed",
            "byPropulsion",
            "byPropulsionNew",
            "byPropulsionUsed",
            "byBrand",
            "byBrandNew",
            "byBrandUsed",
        ]:
            old.setdefault(key, {})

        updated.append(old)

    model_mix["dealers"] = updated
    model_mix["lastUpdated"] = (
        datetime.now(timezone.utc).isoformat()
    )

    model_mix["marketTotals"] = {
        "totalBoats": sum(
            item.get("totalBoats", 0)
            for item in updated
        ),
        "newBoats": sum(
            item.get("newBoats", 0)
            for item in updated
        ),
        "usedBoats": sum(
            item.get("usedBoats", 0)
            for item in updated
        ),
    }


async def main():
    print("")
    print("BTM COMPETITOR TRACKER")
    print("Starting VERIFIED inventory scan")
    print(f"Tracker root: {ROOT}")
    print("")

    competitors = load_json(
        COMPETITORS_FILE
    )

    scans = load_json(
        SCANS_FILE
    )

    trends = load_json(
        TRENDS_FILE
    )

    model_mix = load_json(
        MODEL_MIX_FILE
    )

    dealers = competitors.get(
        "competitors",
        [],
    )

    if not dealers:
        raise RuntimeError(
            "No dealers found in competitors.json"
        )

    print(
        f"Dealers configured: {len(dealers)}"
    )

    results = []
    failures = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True
        )

        for dealer in dealers:
            result = await scrape_dealer(
                browser,
                dealer,
            )

            if (
                result.get("verification", {})
                .get("status")
                == "verified"
            ):
                results.append(result)

            else:
                failures.append(result)

        await browser.close()

    print("")
    print("=" * 70)
    print("SCAN VALIDATION")
    print("=" * 70)
    print(
        f"Verified dealers: "
        f"{len(results)}/{len(dealers)}"
    )

    if failures:
        print("")
        print("FAILED DEALERS:")

        for failure in failures:
            print(
                f"- {failure.get('dealerName')}: "
                f"{failure.get('error')}"
            )

        print("")
        print(
            "SCAN REJECTED."
        )

        print(
            "NO NEW DATE WILL BE WRITTEN."
        )

        print(
            "Existing website data remains unchanged."
        )

        raise RuntimeError(
            f"{len(failures)} dealer(s) "
            "could not be verified"
        )

    # --------------------------------------------------------
    # ONLY GET HERE WHEN EVERY DEALER VERIFIED SUCCESSFULLY.
    # --------------------------------------------------------

    scan_date = datetime.now().date().isoformat()
    scan_id = next_scan_id(scans)

    previous = previous_result_map(scans)

    changes = make_changes(
        previous,
        results,
        scan_id,
        scan_date,
    )

    new_scan = {
        "date": scan_date,
        "scanId": scan_id,
        "results": results,
    }

    scans.setdefault(
        "scans",
        [],
    ).append(new_scan)

    scans.setdefault(
        "changes",
        [],
    ).extend(changes)

    scans["lastUpdated"] = (
        datetime.now(timezone.utc).isoformat()
    )

    update_trends(
        trends,
        results,
        scan_id,
        scan_date,
    )

    update_model_mix(
        model_mix,
        competitors,
        results,
    )

    competitors["lastScan"] = scan_date

    save_json(
        SCANS_FILE,
        scans,
    )

    save_json(
        TRENDS_FILE,
        trends,
    )

    save_json(
        MODEL_MIX_FILE,
        model_mix,
    )

    save_json(
        COMPETITORS_FILE,
        competitors,
    )

    print("")
    print("=" * 70)
    print("VERIFIED SCAN COMPLETE")
    print("=" * 70)

    print(f"Scan ID: {scan_id}")
    print(f"Scan date: {scan_date}")
    print(
        f"Verified dealers: "
        f"{len(results)}/{len(dealers)}"
    )

    print(
        f"Inventory changes detected: "
        f"{len(changes)}"
    )

    print("")
    print(
        "JSON files updated successfully."
    )

    print(
        "Website may now display "
        "the new verified scan date."
    )


if __name__ == "__main__":
    asyncio.run(main())
