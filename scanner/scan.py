import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

COMPETITORS_FILE = DATA_DIR / "competitors.json"
SCANS_FILE = DATA_DIR / "scans.json"
TRENDS_FILE = DATA_DIR / "trends.json"
MODEL_MIX_FILE = DATA_DIR / "model-mix.json"
LIVE_LISTINGS_FILE = DATA_DIR / "live-listings.json"

PAGE_TIMEOUT = 25000
MAX_PAGES = 8
DEALER_CONCURRENCY = 4


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: could not read {path}: {exc}")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value):
    return clean(value).lower()


def extract_year(text):
    m = re.search(r"\b(19[8-9]\d|20[0-3]\d)\b", text or "")
    return int(m.group(1)) if m else None


def extract_stock(text):
    for p in [
        r"stock\s*(?:number|#|no\.?)?\s*[:#]?\s*([A-Z0-9\-]+)",
        r"stk\s*#?\s*[:#]?\s*([A-Z0-9\-]+)",
        r"hin\s*[:#]?\s*([A-Z0-9\-]{8,})",
    ]:
        m = re.search(p, text or "", re.I)
        if m:
            return clean(m.group(1)).upper()

    return None


def extract_condition(text):
    t = norm(text)

    if any(
        x in t
        for x in [
            "pre-owned",
            "preowned",
            "used",
            "brokerage",
            "consignment",
        ]
    ):
        return "Used"

    if re.search(r"\bnew\b", t):
        return "New"

    return "Unknown"


def extract_price(text):
    text = clean(text)

    preferred = re.findall(
        r"(?:our price|sale price|price|asking)\s*:?\s*\$\s*([0-9][0-9,]*(?:\.\d{2})?)",
        text,
        re.I,
    )

    pool = preferred or re.findall(
        r"\$\s*([0-9][0-9,]*(?:\.\d{2})?)",
        text,
    )

    vals = []

    for x in pool:
        try:
            n = float(x.replace(",", ""))

            # Exclude obvious monthly payments and nonsense values.
            if 5000 <= n <= 20_000_000:
                vals.append(n)

        except Exception:
            pass

    return min(vals) if vals else None


EXCLUDED = [
    "personal watercraft",
    "waverunner",
    "wave runner",
    "jet ski",
    "jetski",
    "sea-doo",
    "seadoo",
    "trailer",
    "motorcycle",
    "automobile",
    "truck",
    "super duty",
    "f-150",
    "f-250",
    "f-350",
    "f-450",
    "kenworth",
    "motorhome",
    "rv conversion",
    "golf cart",
    "atv",
    "utv",
    "side by side",
    "snowmobile",
    "generator",
    "outboard motor",
    "outboard engine",
    "engine only",
    "motor only",
    "loose engine",
    "loose motor",
]


POSITIVE = [
    "boat",
    "pontoon",
    "tritoon",
    "yacht",
    "center console",
    "bowrider",
    "runabout",
    "wake",
    "surf",
    "cruiser",
    "catamaran",
    "deck boat",
    "cuddy",
    "fishing",
    "performance",
]


def is_boat(text, url=""):
    t = norm(text)
    u = norm(url)

    if any(x in t or x in u for x in EXCLUDED):
        return False

    if any(x in t for x in POSITIVE):
        return True

    return bool(
        extract_year(text)
        and (
            extract_stock(text)
            or extract_price(text)
        )
    )


def listing_key(record):
    dealer = record.get("dealerId", "")

    if record.get("stockNumber"):
        return f"{dealer}|stock|{record['stockNumber']}"

    if record.get("url"):
        return (
            f"{dealer}|url|"
            f"{record['url'].split('?')[0].rstrip('/').lower()}"
        )

    return (
        f"{dealer}|"
        f"{record.get('year')}|"
        f"{norm(record.get('title'))}|"
        f"{record.get('price')}"
    )


def dedupe(records):
    out = {}

    for record in records:
        out[listing_key(record)] = record

    return list(out.values())


CARD_SELECTORS = [
    "[data-inventory-item]",
    "[data-unit]",
    "[data-listing]",
    ".inventory-item",
    ".inventory-listing",
    ".inventory-card",
    ".unit-card",
    ".vehicle-card",
    ".listing-card",
    ".product-card",
    ".boat-card",
    ".boat-listing",
    "article",
]


TOTAL_PATTERNS = [
    r"showing\s+\d+\s*[-–]\s*\d+\s+(?:of|out of)\s+([\d,]+)",
    r"showing\s+\d+\s+to\s+\d+\s+of\s+([\d,]+)",
    r"\b([\d,]+)\s+results\b",
    r"\b([\d,]+)\s+boats?\s+(?:found|for sale)\b",
]


def displayed_total(text):
    vals = []

    for pattern in TOTAL_PATTERNS:
        for match in re.findall(pattern, text or "", re.I):
            try:
                n = int(match.replace(",", ""))

                if 1 <= n <= 5000:
                    vals.append(n)

            except Exception:
                pass

    return max(vals) if vals else None


async def goto(page, url):
    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT,
        )

        await page.wait_for_timeout(1400)

        if response and response.status >= 400:
            return False

        return True

    except PlaywrightTimeoutError:
        print(f"    timeout: {url}")
        return False

    except Exception as exc:
        print(f"    load error: {url}: {exc}")
        return False


async def auto_scroll(page):
    try:
        for _ in range(6):
            await page.evaluate(
                "window.scrollBy(0, Math.max(window.innerHeight, 900))"
            )

            await page.wait_for_timeout(180)

        await page.evaluate(
            "window.scrollTo(0, document.body.scrollHeight)"
        )

        await page.wait_for_timeout(500)

    except Exception:
        pass


async def body_text(page):
    try:
        return await page.locator("body").inner_text(
            timeout=5000
        )

    except Exception:
        return ""


async def extract_cards(page, competitor):
    best = []

    for selector in CARD_SELECTORS:

        try:
            loc = page.locator(selector)
            count = await loc.count()

            if count < 2 or count > 250:
                continue

            rows = []

            for i in range(count):
                element = loc.nth(i)

                try:
                    text = clean(
                        await element.inner_text(
                            timeout=1000
                        )
                    )

                except Exception:
                    continue

                if len(text) < 15 or len(text) > 3500:
                    continue

                try:
                    href = await element.locator(
                        "a"
                    ).first.get_attribute("href")

                except Exception:
                    href = None

                href = (
                    urljoin(
                        competitor["url"],
                        href,
                    )
                    if href
                    else None
                )

                if not is_boat(
                    text,
                    href or "",
                ):
                    continue

                try:
                    title = clean(
                        await element.locator(
                            "h1,h2,h3,h4,.title,.name,.model"
                        ).first.inner_text(
                            timeout=700
                        )
                    )

                except Exception:
                    title = ""

                if not title:
                    title = next(
                        (
                            clean(x)
                            for x in text.splitlines()
                            if clean(x)
                        ),
                        "Boat",
                    )

                rows.append(
                    {
                        "dealerId": competitor["id"],
                        "dealer": competitor["name"],
                        "title": title,
                        "year": extract_year(text),
                        "condition": extract_condition(text),
                        "price": extract_price(text),
                        "stockNumber": extract_stock(text),
                        "url": href,
                    }
                )

            rows = dedupe(rows)

            if len(rows) > len(best):
                best = rows

        except Exception:
            pass

    return best


async def next_link(page, current_url):
    try:
        links = await page.locator(
            "a"
        ).evaluate_all(
            """
            els => els.map(a => ({
                href: a.href || '',
                text: (a.innerText || a.textContent || '').trim(),
                rel: a.rel || ''
            }))
            """
        )

    except Exception:
        return None

    for link in links:
        text = norm(
            link.get("text")
        )

        rel = norm(
            link.get("rel")
        )

        if (
            rel == "next"
            or text in {
                "next",
                "next >",
                ">",
                "›",
                "»",
            }
            or (
                text.startswith("next")
                and len(text) < 20
            )
        ):
            href = link.get("href")

            if href and href != current_url:
                return urljoin(
                    current_url,
                    href,
                )

    return None


def set_page_param(url, number):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    if "page" in query:
        query["page"] = [str(number)]

    else:
        query["pg"] = [str(number)]

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(
                query,
                doseq=True,
            ),
            parsed.fragment,
        )
    )


def dealer_start_urls(competitor):
    dealer_id = competitor["id"]

    if dealer_id == "kellys-port":
        return [
            "https://www.kellysport.com/new-boats-for-sale-lake-of-the-ozarks-missouri--inventory?sortby=length_overall%7CAsc&sz=50",
            "https://www.kellysport.com/used-boats-for-sale-lake-of-the-ozarks-missouri--inventory?condition=pre-owned&sortby=length_overall%7CAsc&sz=50",
        ]

    if dealer_id == "surdyke-yamaha":
        return [
            "https://www.surdykeyamaha.com/--inventory?category=boat&sz=50"
        ]

    return [
        competitor["url"]
    ]


def previous_result_map(scans):
    if not scans:
        return {}

    latest = scans[-1]

    return {
        result.get("competitorId"): result
        for result in latest.get(
            "results",
            [],
        )
    }


async def scrape_dealer(browser, competitor):
    print(
        f"\nSCANNING: "
        f"{competitor['name']}"
    )

    context = await browser.new_context(
        viewport={
            "width": 1440,
            "height": 1100,
        },
        user_agent=(
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "Chrome/131 Safari/537.36"
        ),
    )

    page = await context.new_page()

    records = []
    totals = []
    seen = set()

    try:
        for start_url in dealer_start_urls(
            competitor
        ):

            current_url = start_url

            for page_number in range(
                1,
                MAX_PAGES + 1,
            ):

                if (
                    not current_url
                    or current_url in seen
                ):
                    break

                seen.add(
                    current_url
                )

                if not await goto(
                    page,
                    current_url,
                ):
                    break

                await auto_scroll(
                    page
                )

                text = await body_text(
                    page
                )

                total = displayed_total(
                    text
                )

                if total:
                    totals.append(
                        total
                    )

                cards = await extract_cards(
                    page,
                    competitor,
                )

                if cards:
                    records.extend(
                        cards
                    )

                    records = dedupe(
                        records
                    )

                next_url = await next_link(
                    page,
                    current_url,
                )

                if (
                    next_url
                    and next_url not in seen
                ):
                    current_url = next_url
                    continue

                if (
                    total
                    and len(records) < total
                ):
                    guessed = set_page_param(
                        start_url,
                        page_number + 1,
                    )

                    if guessed not in seen:
                        current_url = guessed
                        continue

                break

        records = dedupe(
            [
                record
                for record in records
                if is_boat(
                    record.get(
                        "title",
                        "",
                    ),
                    record.get(
                        "url",
                        "",
                    ),
                )
            ]
        )

        parsed = len(records)

        site_total = (
            max(totals)
            if totals
            else None
        )

        verified = parsed >= 3

        reason = (
            "verified from listing cards"
            if verified
            else "too few listings extracted"
        )

        if (
            verified
            and site_total
            and parsed
            < max(
                3,
                site_total * 0.55,
            )
        ):
            verified = False

            reason = (
                f"site shows "
                f"{site_total}, "
                f"extracted {parsed}"
            )

        new_count = sum(
            1
            for record in records
            if record.get(
                "condition"
            ) == "New"
        )

        used_count = sum(
            1
            for record in records
            if record.get(
                "condition"
            ) == "Used"
        )

        new_prices = [
            record["price"]
            for record in records
            if record.get(
                "condition"
            ) == "New"
            and isinstance(
                record.get("price"),
                (int, float),
            )
        ]

        used_prices = [
            record["price"]
            for record in records
            if record.get(
                "condition"
            ) == "Used"
            and isinstance(
                record.get("price"),
                (int, float),
            )
        ]

        all_prices = [
            record["price"]
            for record in records
            if isinstance(
                record.get("price"),
                (int, float),
            )
        ]

        result = {
            "dealerId": competitor["id"],
            "dealer": competitor["name"],
            "verified": verified,
            "verificationReason": reason,
            "totalBoats": (
                parsed
                if verified
                else None
            ),
            "newBoats": (
                new_count
                if verified
                else None
            ),
            "usedBoats": (
                used_count
                if verified
                else None
            ),
            "avgPrice": (
                round(
                    sum(all_prices)
                    / len(all_prices)
                )
                if verified
                and all_prices
                else 0
            ),
            "avgPriceNew": (
                round(
                    sum(new_prices)
                    / len(new_prices)
                )
                if verified
                and new_prices
                else 0
            ),
            "avgPriceUsed": (
                round(
                    sum(used_prices)
                    / len(used_prices)
                )
                if verified
                and used_prices
                else 0
            ),
            "priceRange": (
                f"${min(all_prices):,.0f} "
                f"- "
                f"${max(all_prices):,.0f}"
                if verified
                and all_prices
                else "N/A"
            ),
            "displayedTotal": site_total,
            "recordsExtracted": parsed,
        }

        print(
            f"    "
            f"{'VERIFIED' if verified else 'FAILED'}: "
            f"extracted={parsed}, "
            f"displayed={site_total}"
        )

        return (
            result,
            records if verified else [],
        )

    except Exception as exc:
        print(
            f"    FAILED: {exc}"
        )

        return (
            {
                "dealerId": competitor["id"],
                "dealer": competitor["name"],
                "verified": False,
                "verificationReason": str(exc),
                "totalBoats": None,
                "newBoats": None,
                "usedBoats": None,
                "avgPrice": 0,
                "avgPriceNew": 0,
                "avgPriceUsed": 0,
                "priceRange": "N/A",
                "displayedTotal": None,
                "recordsExtracted": 0,
            },
            [],
        )

    finally:
        await context.close()


def build_scan_record(
    results,
    prior_map,
):
    now = datetime.now(
        timezone.utc
    )

    dashboard_results = []

    fresh = 0
    failed = 0

    for result in results:
        dealer_id = result[
            "dealerId"
        ]

        if result.get(
            "verified"
        ):
            fresh += 1

            row = {
                "competitorId": dealer_id,
                "totalBoats": result.get(
                    "totalBoats",
                    0,
                ) or 0,
                "newBoats": result.get(
                    "newBoats",
                    0,
                ) or 0,
                "usedBoats": result.get(
                    "usedBoats",
                    0,
                ) or 0,
                "avgPrice": result.get(
                    "avgPrice",
                    0,
                ) or 0,
                "avgPriceNew": result.get(
                    "avgPriceNew",
                    0,
                ) or 0,
                "avgPriceUsed": result.get(
                    "avgPriceUsed",
                    0,
                ) or 0,
                "priceRange": result.get(
                    "priceRange",
                    "N/A",
                ),
                "verified": True,
                "verificationReason": result.get(
                    "verificationReason"
                ),
            }

        else:
            failed += 1

            old = prior_map.get(
                dealer_id,
                {},
            )

            row = {
                "competitorId": dealer_id,
                "totalBoats": old.get(
                    "totalBoats",
                    0,
                ) or 0,
                "newBoats": old.get(
                    "newBoats",
                    0,
                ) or 0,
                "usedBoats": old.get(
                    "usedBoats",
                    0,
                ) or 0,
                "avgPrice": old.get(
                    "avgPrice",
                    0,
                ) or 0,
                "avgPriceNew": old.get(
                    "avgPriceNew",
                    0,
                ) or 0,
                "avgPriceUsed": old.get(
                    "avgPriceUsed",
                    0,
                ) or 0,
                "priceRange": old.get(
                    "priceRange",
                    "N/A",
                ),
                "verified": False,
                "verificationReason": result.get(
                    "verificationReason"
                ),
            }

        dashboard_results.append(
            row
        )

    return {
        "date": now.date().isoformat(),
        "scanId": (
            f"scan-"
            f"{now.strftime('%Y%m%d-%H%M%S')}"
        ),
        "results": dashboard_results,
        "freshDealers": fresh,
        "failedDealers": failed,
        "timestamp": now.isoformat(),
    }


def update_scans(
    scan_record
):
    data = load_json(
        SCANS_FILE,
        {
            "scans": []
        },
    )

    if not isinstance(
        data,
        dict,
    ):
        data = {
            "scans": []
        }

    data.setdefault(
        "scans",
        [],
    ).append(
        scan_record
    )

    data["scans"] = (
        data["scans"][-400:]
    )

    data["lastUpdated"] = (
        scan_record["timestamp"]
    )

    save_json(
        SCANS_FILE,
        data,
    )


def update_competitors(
    scan_record
):
    data = load_json(
        COMPETITORS_FILE,
        {
            "competitors": []
        },
    )

    data["lastScan"] = (
        scan_record["timestamp"]
    )

    result_map = {
        result["competitorId"]: result
        for result
        in scan_record["results"]
    }

    for competitor in data.get(
        "competitors",
        [],
    ):
        result = result_map.get(
            competitor.get("id")
        )

        if not result:
            continue

        competitor["scanStatus"] = (
            "verified"
            if result.get("verified")
            else "failed"
        )

        competitor[
            "verificationReason"
        ] = result.get(
            "verificationReason"
        )

        if result.get(
            "verified"
        ):
            competitor[
                "lastVerified"
            ] = scan_record[
                "timestamp"
            ]

            competitor[
                "totalInventory"
            ] = result.get(
                "totalBoats"
            )

            competitor[
                "newInventory"
            ] = result.get(
                "newBoats"
            )

            competitor[
                "usedInventory"
            ] = result.get(
                "usedBoats"
            )

            competitor[
                "averagePrice"
            ] = result.get(
                "avgPrice"
            )

    save_json(
        COMPETITORS_FILE,
        data,
    )


def update_trends(
    scan_record
):
    trends = load_json(
        TRENDS_FILE,
        {},
    )

    results = scan_record[
        "results"
    ]

    total_market = sum(
        result.get(
            "totalBoats",
            0,
        ) or 0
        for result in results
    )

    btm = next(
        (
            result
            for result in results
            if result.get(
                "competitorId"
            )
            == "big-thunder-marine"
        ),
        None,
    )

    btm_total = (
        btm.get(
            "totalBoats",
            0,
        )
        if btm
        else 0
    )

    inventory = trends.setdefault(
        "inventoryTrend",
        [],
    )

    inventory.append(
        {
            "date": scan_record[
                "date"
            ],
            "totalMarket": total_market,
            "btm": btm_total,
            "competitors": (
                total_market
                - btm_total
            ),
            "scanId": scan_record[
                "scanId"
            ],
        }
    )

    trends[
        "inventoryTrend"
    ] = inventory[-180:]

    priced = [
        result
        for result in results
        if (
            result.get(
                "avgPrice"
            )
            or 0
        ) > 0
    ]

    if priced:
        price_trend = trends.setdefault(
            "priceTrend",
            [],
        )

        price_trend.append(
            {
                "date": scan_record[
                    "date"
                ],
                "marketAvg": round(
                    sum(
                        result["avgPrice"]
                        for result in priced
                    )
                    / len(priced)
                ),
                "btmAvg": (
                    btm.get(
                        "avgPrice",
                        0,
                    )
                    if btm
                    else 0
                ),
            }
        )

        trends[
            "priceTrend"
        ] = price_trend[-180:]

    save_json(
        TRENDS_FILE,
        trends,
    )


def brand_from_title(
    title,
    known_brands,
):
    title_normalized = norm(
        title
    )

    matches = [
        brand
        for brand in known_brands
        if (
            "various"
            not in norm(brand)
            and norm(brand)
            in title_normalized
        )
    ]

    return (
        max(
            matches,
            key=len,
        )
        if matches
        else "Other / Unknown"
    )


def update_model_mix(
    all_records,
    competitors,
):
    model_mix = load_json(
        MODEL_MIX_FILE,
        {
            "lastUpdated": None,
            "dealers": [],
        },
    )

    old = {
        dealer.get("id"): dict(
            dealer
        )
        for dealer
        in model_mix.get(
            "dealers",
            [],
        )
        if (
            isinstance(
                dealer,
                dict,
            )
            and dealer.get("id")
        )
    }

    by_dealer = {}

    for record in all_records:
        by_dealer.setdefault(
            record["dealerId"],
            [],
        ).append(
            record
        )

    updated = []

    for competitor in competitors:
        dealer_id = competitor[
            "id"
        ]

        dealer = dict(
            old.get(
                dealer_id,
                {},
            )
        )

        dealer["id"] = dealer_id

        dealer["name"] = competitor.get(
            "name",
            dealer_id,
        )

        dealer["isOwn"] = bool(
            competitor.get(
                "isOwn",
                False,
            )
        )

        records = by_dealer.get(
            dealer_id,
            [],
        )

        if records:
            new_records = [
                record
                for record in records
                if record.get(
                    "condition"
                ) == "New"
            ]

            used_records = [
                record
                for record in records
                if record.get(
                    "condition"
                ) == "Used"
            ]

            new_prices = [
                record["price"]
                for record
                in new_records
                if isinstance(
                    record.get(
                        "price"
                    ),
                    (int, float),
                )
            ]

            used_prices = [
                record["price"]
                for record
                in used_records
                if isinstance(
                    record.get(
                        "price"
                    ),
                    (int, float),
                )
            ]

            dealer[
                "totalBoats"
            ] = len(
                records
            )

            dealer[
                "newBoats"
            ] = len(
                new_records
            )

            dealer[
                "usedBoats"
            ] = len(
                used_records
            )

            dealer[
                "avgPriceNew"
            ] = (
                round(
                    sum(new_prices)
                    / len(new_prices)
                )
                if new_prices
                else 0
            )

            dealer[
                "avgPriceUsed"
            ] = (
                round(
                    sum(used_prices)
                    / len(used_prices)
                )
                if used_prices
                else 0
            )

            dealer[
                "byCondition"
            ] = {
                "New": len(
                    new_records
                ),
                "Used": len(
                    used_records
                ),
            }

            brands = {}

            for record in records:
                brand = brand_from_title(
                    record.get(
                        "title",
                        "",
                    ),
                    competitor.get(
                        "brands",
                        [],
                    ),
                )

                brands[brand] = (
                    brands.get(
                        brand,
                        0,
                    )
                    + 1
                )

            dealer[
                "byBrand"
            ] = brands

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
            dealer.setdefault(
                key,
                {},
            )

        updated.append(
            dealer
        )

    model_mix[
        "dealers"
    ] = updated

    model_mix[
        "lastUpdated"
    ] = datetime.now(
        timezone.utc
    ).isoformat()

    market_totals = model_mix.get(
        "marketTotals",
        {},
    )

    market_totals[
        "totalBoats"
    ] = sum(
        dealer.get(
            "totalBoats",
            0,
        ) or 0
        for dealer in updated
    )

    market_totals[
        "newBoats"
    ] = sum(
        dealer.get(
            "newBoats",
            0,
        ) or 0
        for dealer in updated
    )

    market_totals[
        "usedBoats"
    ] = sum(
        dealer.get(
            "usedBoats",
            0,
        ) or 0
        for dealer in updated
    )

    model_mix[
        "marketTotals"
    ] = market_totals

    save_json(
        MODEL_MIX_FILE,
        model_mix,
    )


def save_live_listings(
    records
):
    save_json(
        LIVE_LISTINGS_FILE,
        {
            "generatedAt": datetime.now(
                timezone.utc
            ).isoformat(),
            "count": len(records),
            "listings": records,
        },
    )


async def main():
    config = load_json(
        COMPETITORS_FILE,
        None,
    )

    if (
        not config
        or not config.get(
            "competitors"
        )
    ):
        raise RuntimeError(
            "No competitors configured"
        )

    competitors = config[
        "competitors"
    ]

    scans_data = load_json(
        SCANS_FILE,
        {
            "scans": []
        },
    )

    prior_map = previous_result_map(
        scans_data.get(
            "scans",
            [],
        )
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

        semaphore = asyncio.Semaphore(
            DEALER_CONCURRENCY
        )

        async def run_one(
            competitor
        ):
            async with semaphore:
                return await scrape_dealer(
                    browser,
                    competitor,
                )

        try:
            scanned = await asyncio.gather(
                *(
                    run_one(
                        competitor
                    )
                    for competitor in competitors
                )
            )

        finally:
            await browser.close()

    results = []
    all_records = []

    for result, records in scanned:
        results.append(
            result
        )

        all_records.extend(
            records
        )

    all_records = dedupe(
        all_records
    )

    scan_record = build_scan_record(
        results,
        prior_map,
    )

    update_scans(
        scan_record
    )

    update_competitors(
        scan_record
    )

    update_trends(
        scan_record
    )

    update_model_mix(
        all_records,
        competitors,
    )

    save_live_listings(
        all_records
    )

    print(
        "\nSCAN COMPLETE"
    )

    print(
        f"Fresh dealers: "
        f"{scan_record['freshDealers']}/"
        f"{len(competitors)}"
    )

    print(
        f"Failed dealers using prior verified dashboard values: "
        f"{scan_record['failedDealers']}/"
        f"{len(competitors)}"
    )

    print(
        f"Fresh listing records saved: "
        f"{len(all_records)}"
    )


if __name__ == "__main__":
    asyncio.run(main())
