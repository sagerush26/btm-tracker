import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from playwright.async_api import async_playwright


MAX_CREDIBLE_CHANGE = 30

PROBLEM_DEALERS = {
    "surdyke-yamaha",
    "performance-boat-center",
    "iguana-marine",
    "stateamind",
    "premier-advantage",
}


def find_root():
    here = Path.cwd()

    # Normal/root layout
    if (here / "data" / "competitors.json").exists():
        return here

    # Search for the uploaded tracker folder automatically
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


def money_values(text):
    values = []

    patterns = [
        r"\$\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d{2})?)",
        r"\$\s*([0-9]{4,7}(?:\.\d{2})?)",
    ]

    for pattern in patterns:
        for match in re.findall(pattern, text):
            try:
                value = float(match.replace(",", ""))
                if 1000 <= value <= 10000000:
                    values.append(value)
            except Exception:
                pass

    # remove duplicates while retaining useful range
    return list(dict.fromkeys(values))


def get_count_from_text(text):
    patterns = [
        r"showing\s+\d+\s*[-–]\s*\d+\s+of\s+(\d+)",
        r"(\d+)\s+(?:boats?|results?|inventory|listings?)\s+(?:found|available)",
        r"(\d+)\s+(?:boats?|results?|listings?)",
        r"inventory\s*\(?\s*(\d+)\s*\)?",
        r"results\s*\(?\s*(\d+)\s*\)?",
    ]

    lower = text.lower()

    candidates = []

    for pattern in patterns:
        for match in re.findall(pattern, lower):
            try:
                n = int(match)
                if 1 <= n <= 1000:
                    candidates.append(n)
            except Exception:
                pass

    return max(candidates) if candidates else None


def condition_counts(text):
    lower = text.lower()

    new_patterns = [
        r"new\s*\(?\s*(\d+)\s*\)?",
        r"new boats?\s*\(?\s*(\d+)\s*\)?",
    ]

    used_patterns = [
        r"used\s*\(?\s*(\d+)\s*\)?",
        r"pre[- ]owned\s*\(?\s*(\d+)\s*\)?",
        r"used boats?\s*\(?\s*(\d+)\s*\)?",
    ]

    def first_number(patterns):
        for pattern in patterns:
            m = re.search(pattern, lower)
            if m:
                try:
                    n = int(m.group(1))
                    if 0 <= n <= 1000:
                        return n
                except Exception:
                    pass
        return None

    return first_number(new_patterns), first_number(used_patterns)


async def expand_page(page):
    # Scroll repeatedly for lazy-loaded inventory.
    previous_height = 0

    for _ in range(12):
        try:
            height = await page.evaluate("document.body.scrollHeight")
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)

            # Try common load-more controls.
            buttons = page.locator(
                "button:has-text('Load More'), "
                "a:has-text('Load More'), "
                "button:has-text('Show More'), "
                "a:has-text('Show More'), "
                "button:has-text('View More')"
            )

            count = await buttons.count()

            for i in range(min(count, 3)):
                try:
                    if await buttons.nth(i).is_visible():
                        await buttons.nth(i).click(timeout=2000)
                        await page.wait_for_timeout(1200)
                except Exception:
                    pass

            if height == previous_height:
                break

            previous_height = height

        except Exception:
            break


async def scrape_dealer(browser, dealer):
    dealer_id = dealer["id"]
    name = dealer["name"]
    url = dealer["url"]

    print(f"\nScanning {name}: {url}")

    context = await browser.new_context(
        viewport={"width": 1440, "height": 1600},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/124 Safari/537.36"
        ),
    )

    page = await context.new_page()

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(3000)

        await expand_page(page)

        text = await page.locator("body").inner_text(timeout=15000)

        total = get_count_from_text(text)
        new_count, used_count = condition_counts(text)

        # Try counting repeated listing-like elements when no total indicator exists.
        if total is None:
            selectors = [
                "[class*='inventory-item']",
                "[class*='inventoryItem']",
                "[class*='vehicle-card']",
                "[class*='listing-card']",
                "[class*='boat-card']",
                "[class*='product-card']",
                "article",
            ]

            element_counts = []

            for selector in selectors:
                try:
                    n = await page.locator(selector).count()
                    if 2 <= n <= 1000:
                        element_counts.append(n)
                except Exception:
                    pass

            if element_counts:
                total = max(element_counts)

        prices = money_values(text)

        avg_price = int(mean(prices)) if prices else 0
        low_price = int(min(prices)) if prices else 0
        high_price = int(max(prices)) if prices else 0

        if low_price and high_price:
            price_range = f"${low_price:,.0f} - ${high_price:,.0f}"
        else:
            price_range = "Call for Price / unavailable"

        # Only accept condition counts when plausible.
        if total:
            if new_count is not None and new_count > total:
                new_count = None
            if used_count is not None and used_count > total:
                used_count = None

        result = {
            "competitorId": dealer_id,
            "totalBoats": total,
            "newBoats": new_count,
            "usedBoats": used_count,
            "avgPrice": avg_price,
            "avgPriceNew": 0,
            "avgPriceUsed": 0,
            "priceRange": price_range,
            "_status": "scraped",
        }

        print(
            f"  raw total={total}, new={new_count}, used={used_count}, "
            f"prices={len(prices)}"
        )

        return result

    except Exception as exc:
        print(f"  ERROR: {exc}")

        return {
            "competitorId": dealer_id,
            "totalBoats": None,
            "newBoats": None,
            "usedBoats": None,
            "avgPrice": 0,
            "avgPriceNew": 0,
            "avgPriceUsed": 0,
            "priceRange": "Unavailable",
            "_status": f"failed: {exc}",
        }

    finally:
        await context.close()


def previous_by_id(scans):
    if not scans.get("scans"):
        return {}

    latest = scans["scans"][-1]

    return {
        item["competitorId"]: item
        for item in latest.get("results", [])
    }


def smooth_result(raw, previous):
    dealer_id = raw["competitorId"]

    if not previous:
        return raw

    raw_total = raw.get("totalBoats")
    old_total = previous.get("totalBoats", 0)

    # Failed/no usable extraction = preserve prior value.
    if raw_total is None or raw_total <= 0:
        print(f"  {dealer_id}: carrying forward previous value (scan unavailable)")
        saved = dict(previous)
        saved["_status"] = "carried-forward"
        return saved

    delta = raw_total - old_total

    # Standard outlier protection.
    if abs(delta) > MAX_CREDIBLE_CHANGE:
        print(
            f"  {dealer_id}: OUTLIER {old_total} -> {raw_total}; "
            "carrying forward previous value"
        )
        saved = dict(previous)
        saved["_status"] = "outlier-carried-forward"
        return saved

    # Original tracker had extra protection for Surdyke.
    if dealer_id == "surdyke-yamaha" and old_total:
        pct = abs(delta) / old_total

        if pct > 0.20:
            print(
                f"  {dealer_id}: >20% variation; "
                "carrying forward previous value"
            )
            saved = dict(previous)
            saved["_status"] = "outlier-carried-forward"
            return saved

    result = dict(raw)

    # If condition split could not be extracted, preserve the old split
    # instead of inventing numbers.
    if result.get("newBoats") is None:
        result["newBoats"] = previous.get("newBoats", 0)

    if result.get("usedBoats") is None:
        result["usedBoats"] = previous.get("usedBoats", 0)

    # If pricing extraction failed, preserve previous pricing.
    if not result.get("avgPrice"):
        result["avgPrice"] = previous.get("avgPrice", 0)
        result["avgPriceNew"] = previous.get("avgPriceNew", 0)
        result["avgPriceUsed"] = previous.get("avgPriceUsed", 0)
        result["priceRange"] = previous.get(
            "priceRange",
            "Unavailable"
        )

    return result


def make_changes(previous_map, results, scan_id):
    changes = []

    for current in results:
        dealer_id = current["competitorId"]
        previous = previous_map.get(dealer_id)

        if not previous:
            continue

        before = previous.get("totalBoats", 0)
        after = current.get("totalBoats", before)
        delta = after - before

        if delta == 0:
            continue

        if abs(delta) > MAX_CREDIBLE_CHANGE:
            continue

        changes.append(
            {
                "scanId": scan_id,
                "date": datetime.now().date().isoformat(),
                "competitorId": dealer_id,
                "previousTotal": before,
                "newTotal": after,
                "change": delta,
                "description": f"{dealer_id}: {before} -> {after} ({delta:+d})",
            }
        )

    return changes


def update_model_mix(model_mix, competitors, results):
    dealer_meta = {
        d["id"]: d
        for d in competitors["competitors"]
    }

    old_dealers = {
        d["id"]: d
        for d in model_mix.get("dealers", [])
    }

    updated = []

    for result in results:
        dealer_id = result["competitorId"]
        existing = dict(old_dealers.get(dealer_id, {}))
        meta = dealer_meta.get(dealer_id, {})

        existing["id"] = dealer_id
        existing["name"] = meta.get("name", dealer_id)
        existing["isOwn"] = bool(meta.get("isOwn", False))

        existing["totalBoats"] = result.get("totalBoats", 0)
        existing["newBoats"] = result.get("newBoats", 0)
        existing["usedBoats"] = result.get("usedBoats", 0)
        existing["avgPriceNew"] = result.get("avgPriceNew", 0)
        existing["avgPriceUsed"] = result.get("avgPriceUsed", 0)

        existing["byCondition"] = {
            "New": result.get("newBoats", 0),
            "Used": result.get("usedBoats", 0),
        }

        # Preserve existing detailed model-mix breakdowns until a listing-
        # level extractor has reliable data for that dealer.
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
            existing.setdefault(key, {})

        updated.append(existing)

    model_mix["dealers"] = updated
    model_mix["lastUpdated"] = datetime.now(timezone.utc).isoformat()

    total_market = sum(d.get("totalBoats", 0) for d in updated)
    total_new = sum(d.get("newBoats", 0) for d in updated)
    total_used = sum(d.get("usedBoats", 0) for d in updated)

    market_totals = model_mix.get("marketTotals", {})
    market_totals["totalBoats"] = total_market
    market_totals["newBoats"] = total_new
    market_totals["usedBoats"] = total_used

    model_mix["marketTotals"] = market_totals


def update_trends(trends, competitors, results, scan_id, scan_date):
    total = sum(r.get("totalBoats", 0) for r in results)

    btm = next(
        (
            r.get("totalBoats", 0)
            for r in results
            if r["competitorId"] == "big-thunder-marine"
        ),
        0,
    )

    competitors_total = total - btm

    inventory = trends.setdefault("inventoryTrend", [])

    inventory.append(
        {
            "date": scan_date,
            "totalMarket": total,
            "btm": btm,
            "competitors": competitors_total,
            "total": total,
            "scanId": scan_id,
        }
    )

    # Keep history manageable.
    trends["inventoryTrend"] = inventory[-180:]


async def main():
    print(f"Tracker root: {ROOT}")

    competitors = load_json(COMPETITORS_FILE)
    scans = load_json(SCANS_FILE)
    trends = load_json(TRENDS_FILE)
    model_mix = load_json(MODEL_MIX_FILE)

    previous_map = previous_by_id(scans)

    scan_number = len(scans.get("scans", [])) + 1
    scan_id = f"scan-{scan_number:03d}"
    scan_date = datetime.now().date().isoformat()

    raw_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        for dealer in competitors["competitors"]:
            raw = await scrape_dealer(browser, dealer)
            raw_results.append(raw)

        await browser.close()

    results = []

    for raw in raw_results:
        previous = previous_map.get(raw["competitorId"])
        result = smooth_result(raw, previous)

        # Internal debug property should not go into dashboard JSON.
        result.pop("_status", None)

        results.append(result)

    changes = make_changes(
        previous_map,
        results,
        scan_id,
    )

    scans.setdefault("scans", []).append(
        {
            "date": scan_date,
            "scanId": scan_id,
            "results": results,
        }
    )

    scans.setdefault("changes", []).extend(changes)
    scans["lastUpdated"] = datetime.now(timezone.utc).isoformat()

    update_trends(
        trends,
        competitors,
        results,
        scan_id,
        scan_date,
    )

    update_model_mix(
        model_mix,
        competitors,
        results,
    )

    competitors["lastScan"] = datetime.now(timezone.utc).isoformat()

    save_json(SCANS_FILE, scans)
    save_json(TRENDS_FILE, trends)
    save_json(MODEL_MIX_FILE, model_mix)
    save_json(COMPETITORS_FILE, competitors)

    print("\n========================================")
    print(f"BTM scan complete: {scan_id} / {scan_date}")
    print(f"Dealers processed: {len(results)}")
    print(f"Credible changes: {len(changes)}")
    print("========================================")


if __name__ == "__main__":
    asyncio.run(main())
