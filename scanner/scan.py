import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# ============================================================
# BTM COMPETITOR TRACKER
# Live inventory scanner
#
# RULES
# - Boats only
# - New + Used + Brokerage/Consignment
# - All company locations represented by each inventory URL
# - Exclude PWC, trailers, loose motors/outboards, vehicles, parts
# - Deduplicate inventory
# - Never silently publish an obviously failed scrape as current
# ============================================================


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

COMPETITORS_FILE = DATA_DIR / "competitors.json"
SCANS_FILE = DATA_DIR / "scans.json"
TRENDS_FILE = DATA_DIR / "trends.json"
MODEL_MIX_FILE = DATA_DIR / "model-mix.json"
LIVE_LISTINGS_FILE = DATA_DIR / "live-listings.json"

MAX_PAGES = 30
PAGE_TIMEOUT = 60000


# ------------------------------------------------------------
# BASIC HELPERS
# ------------------------------------------------------------

def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: Could not read {path}: {exc}")
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize(text):
    return clean(text).lower()


def money_to_number(value):
    if value is None:
        return None

    text = clean(value)

    matches = re.findall(
        r"\$\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d{2})?|[0-9]{4,}(?:\.\d{2})?)",
        text
    )

    if not matches:
        return None

    values = []

    for match in matches:
        try:
            number = float(match.replace(",", ""))
            if number >= 1000:
                values.append(number)
        except Exception:
            pass

    if not values:
        return None

    return min(values)


def extract_year(text):
    match = re.search(r"\b(19[8-9]\d|20[0-3]\d)\b", text or "")
    if match:
        return int(match.group(1))
    return None


def extract_stock(text):
    patterns = [
        r"stock\s*(?:number|#|no\.?)?\s*[:#]?\s*([A-Z0-9\-]+)",
        r"stock\s*[:#]\s*([A-Z0-9\-]+)",
        r"stk\s*#?\s*[:#]?\s*([A-Z0-9\-]+)",
        r"hin\s*[:#]?\s*([A-Z0-9\-]{8,})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text or "", re.I)
        if match:
            return clean(match.group(1)).upper()

    return None


def extract_length(text):
    patterns = [
        r"(?:length|loa|length overall)\s*[:\-]?\s*(\d{1,3}(?:\.\d+)?)\s*(?:ft|feet|')",
        r"\b(\d{2}(?:\.\d+)?)\s*(?:ft|feet|')\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text or "", re.I)
        if match:
            try:
                value = float(match.group(1))
                if 8 <= value <= 150:
                    return value
            except Exception:
                pass

    return None


def extract_condition(text):
    t = normalize(text)

    if any(word in t for word in [
        "pre-owned",
        "preowned",
        "used",
        "brokerage",
        "consignment"
    ]):
        return "Used"

    if re.search(r"\bnew\b", t):
        return "New"

    return "Unknown"


def extract_location(text):
    patterns = [
        r"(?:location|located at|store)\s*[:\-]\s*([^\n|]{3,80})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text or "", re.I)
        if match:
            return clean(match.group(1))

    return None


EXCLUDED_WORDS = [
    "personal watercraft",
    "watercraft",
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
]


OUTBOARD_ONLY_PATTERNS = [
    r"\boutboard motor\b",
    r"\boutboard engine\b",
    r"\bengine only\b",
    r"\bmotor only\b",
    r"\bloose engine\b",
    r"\bloose motor\b",
]


def is_boat_record(text, url=""):
    t = normalize(text)
    u = normalize(url)

    for word in EXCLUDED_WORDS:
        if word in t or word in u:
            return False

    for pattern in OUTBOARD_ONLY_PATTERNS:
        if re.search(pattern, t, re.I):
            return False

    # Explicit boat indicators are strong positives.
    positive_words = [
        "boat",
        "pontoon",
        "tritoon",
        "yacht",
        "center console",
        "bowrider",
        "runabout",
        "wake boat",
        "surf boat",
        "cruiser",
        "catamaran",
        "deck boat",
        "fishing boat",
        "cuddy",
        "performance boat",
    ]

    if any(word in t for word in positive_words):
        return True

    # Most dealer cards do not literally say "boat".
    # A year + model/price/stock combination is treated as inventory
    # unless an excluded vehicle type was detected above.
    year = extract_year(text)
    stock = extract_stock(text)
    price = money_to_number(text)

    if year and (stock or price):
        return True

    return False


def listing_key(record):
    dealer = record.get("dealerId", "")

    if record.get("stockNumber"):
        return f"{dealer}|stock|{record['stockNumber'].upper()}"

    url = record.get("url")
    if url:
        return f"{dealer}|url|{url.split('?')[0].rstrip('/').lower()}"

    return "|".join([
        dealer,
        str(record.get("year") or ""),
        normalize(record.get("title")),
        str(record.get("price") or "")
    ])


def dedupe(records):
    result = {}
    for record in records:
        result[listing_key(record)] = record
    return list(result.values())


# ------------------------------------------------------------
# PAGE HELPERS
# ------------------------------------------------------------

async def goto(page, url):
    print(f"    Loading: {url}")

    try:
        response = await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT
        )

        await page.wait_for_timeout(2500)

        if response and response.status >= 400:
            raise RuntimeError(f"HTTP {response.status}")

        return True

    except PlaywrightTimeoutError:
        print(f"    WARNING: Timeout loading {url}")
        return False

    except Exception as exc:
        print(f"    WARNING: Could not load {url}: {exc}")
        return False


async def auto_scroll(page):
    try:
        for _ in range(12):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await page.wait_for_timeout(400)

        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


async def page_text(page):
    try:
        return await page.locator("body").inner_text(timeout=10000)
    except Exception:
        return ""


async def get_links(page):
    try:
        return await page.locator("a").evaluate_all(
            """els => els.map(a => ({
                href: a.href || '',
                text: (a.innerText || a.textContent || '').trim()
            }))"""
        )
    except Exception:
        return []


# ------------------------------------------------------------
# INVENTORY LINK DETECTION
# ------------------------------------------------------------

DETAIL_HINTS = [
    "/inventory/",
    "/boat/",
    "/boats/",
    "/product/",
    "/listing/",
    "/listings/",
    "unitdetail",
    "unit-detail",
    "vehicle-details",
    "inventory-detail",
    "inventorydetail",
    "boat-details",
    "boatdetail",
    "details/",
    "viewdetails",
]


BAD_LINK_HINTS = [
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "linkedin.com",
    "mailto:",
    "tel:",
    "/contact",
    "/about",
    "/service",
    "/parts",
    "/finance",
    "/privacy",
    "/terms",
    "/careers",
    "/events",
    "/blog",
    "/news",
]


def looks_like_detail_link(url, text, dealer_url):
    if not url:
        return False

    u = normalize(url)
    txt = normalize(text)

    if any(x in u for x in BAD_LINK_HINTS):
        return False

    try:
        base_host = urlparse(dealer_url).netloc.replace("www.", "")
        link_host = urlparse(url).netloc.replace("www.", "")

        if base_host and link_host and base_host != link_host:
            return False
    except Exception:
        return False

    if any(hint in u for hint in DETAIL_HINTS):
        if any(word in txt for word in [
            "details",
            "view",
            "learn more",
            "more info",
            "price",
            "new",
            "used",
            "pre-owned"
        ]):
            return True

        if extract_year(txt):
            return True

    if extract_year(txt) and len(txt) > 8:
        return True

    return False


# ------------------------------------------------------------
# CARD EXTRACTION
# ------------------------------------------------------------

CARD_SELECTORS = [
    "[data-inventory-item]",
    "[data-unit]",
    "[data-listing]",
    ".inventory-item",
    ".inventory-listing",
    ".inventory-card",
    ".unit",
    ".unit-card",
    ".vehicle",
    ".vehicle-card",
    ".listing",
    ".listing-card",
    ".product-item",
    ".product-card",
    ".boat-card",
    ".boat-listing",
    "article",
]


async def extract_cards(page, competitor):
    candidates = []

    for selector in CARD_SELECTORS:
        try:
            elements = page.locator(selector)
            count = await elements.count()

            if count < 2:
                continue

            # Avoid gigantic selectors that match layout wrappers.
            if count > 300:
                continue

            local = []

            for i in range(count):
                element = elements.nth(i)

                try:
                    text = clean(await element.inner_text(timeout=2000))
                except Exception:
                    continue

                if len(text) < 15 or len(text) > 5000:
                    continue

                try:
                    href = await element.locator("a").first.get_attribute("href")
                except Exception:
                    href = None

                if href:
                    href = urljoin(competitor["url"], href)

                if not is_boat_record(text, href or ""):
                    continue

                title = ""

                try:
                    title = clean(
                        await element.locator(
                            "h1,h2,h3,h4,.title,.name,.model"
                        ).first.inner_text(timeout=1000)
                    )
                except Exception:
                    pass

                if not title:
                    lines = [clean(x) for x in text.splitlines() if clean(x)]
                    title = lines[0] if lines else "Boat"

                record = {
                    "dealerId": competitor["id"],
                    "dealer": competitor["name"],
                    "title": title,
                    "year": extract_year(text),
                    "condition": extract_condition(text),
                    "price": money_to_number(text),
                    "stockNumber": extract_stock(text),
                    "length": extract_length(text),
                    "location": extract_location(text),
                    "url": href,
                    "rawText": text[:1500]
                }

                local.append(record)

            local = dedupe(local)

            if len(local) > len(candidates):
                candidates = local

        except Exception:
            continue

    return candidates


# ------------------------------------------------------------
# LINK-BASED EXTRACTION
# ------------------------------------------------------------

async def extract_listing_links(page, competitor):
    links = await get_links(page)

    found = []

    for link in links:
        href = link.get("href", "")
        text = link.get("text", "")

        if looks_like_detail_link(href, text, competitor["url"]):
            found.append(href)

    # Preserve order while deduping.
    return list(dict.fromkeys(found))


async def extract_detail_page(context, competitor, url):
    page = await context.new_page()

    try:
        if not await goto(page, url):
            return None

        text = clean(await page_text(page))

        if not is_boat_record(text, url):
            return None

        title = ""

        try:
            title = clean(await page.locator("h1").first.inner_text(timeout=3000))
        except Exception:
            pass

        if not title:
            title = clean((await page.title()).split("|")[0])

        return {
            "dealerId": competitor["id"],
            "dealer": competitor["name"],
            "title": title or "Boat",
            "year": extract_year(title or text),
            "condition": extract_condition(text),
            "price": money_to_number(text),
            "stockNumber": extract_stock(text),
            "length": extract_length(text),
            "location": extract_location(text),
            "url": url,
            "rawText": text[:1500]
        }

    finally:
        await page.close()


# ------------------------------------------------------------
# PAGINATION
# ------------------------------------------------------------

def set_page_parameter(url, page_number):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    if "pg" in query:
        query["pg"] = [str(page_number)]
    elif "page" in query and str(query["page"][0]).isdigit():
        query["page"] = [str(page_number)]
    else:
        query["pg"] = [str(page_number)]

    new_query = urlencode(query, doseq=True)

    return urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        parsed.params,
        new_query,
        parsed.fragment
    ))


async def find_next_link(page, current_url):
    links = await get_links(page)

    candidates = []

    for link in links:
        href = link.get("href", "")
        text = normalize(link.get("text", ""))

        if not href:
            continue

        if text in ["next", "next >", ">", "›", "»"]:
            candidates.append(href)
            continue

        if "next" in text and len(text) < 30:
            candidates.append(href)

    for href in candidates:
        full = urljoin(current_url, href)
        if full != current_url:
            return full

    return None


# ------------------------------------------------------------
# DISPLAYED TOTAL DETECTION
# ------------------------------------------------------------

TOTAL_PATTERNS = [
    r"showing\s+\d+\s*[-–]\s*\d+\s+of\s+([\d,]+)\s+results",
    r"showing\s+\d+\s+to\s+\d+\s+of\s+([\d,]+)\s+results",
    r"\b([\d,]+)\s+results\b",
    r"showing\s+([\d,]+)\s+boats?\b",
    r"\b([\d,]+)\s+boats?\s+found\b",
    r"\b([\d,]+)\s+boats?\s+for\s+sale\b",
]


def displayed_total(text):
    values = []

    for pattern in TOTAL_PATTERNS:
        for match in re.findall(pattern, text or "", re.I):
            try:
                value = int(match.replace(",", ""))
                if 1 <= value <= 10000:
                    values.append(value)
            except Exception:
                pass

    if not values:
        return None

    # Prefer the largest plausible inventory total.
    return max(values)


# ------------------------------------------------------------
# DEALER-SPECIFIC START URLS
# ------------------------------------------------------------

def dealer_start_urls(competitor):
    dealer_id = competitor["id"]
    base = competitor["url"]

    # Kelly's Port:
    # explicitly crawl both current New and Used inventory routes.
    if dealer_id == "kellys-port":
        return [
            "https://www.kellysport.com/new-boats-for-sale-lake-of-the-ozarks-missouri--inventory?sortby=length_overall%7Casc&sz=50",
            "https://www.kellysport.com/used-boats-for-sale-lake-of-the-ozarks-missouri--inventory?condition=pre-owned&sortby=length_overall%7Casc&sz=50",
        ]

    # Surdyke:
    # category=boat is mandatory. Do not scan mixed PWC/outboard inventory.
    if dealer_id == "surdyke-yamaha":
        return [
            "https://www.surdykeyamaha.com/--inventory?category=boat&sz=50"
        ]

    return [base]


# ------------------------------------------------------------
# SCRAPE ONE DEALER
# ------------------------------------------------------------

async def scrape_dealer(browser, competitor):
    print("")
    print("=" * 70)
    print(f"SCANNING: {competitor['name']}")
    print("=" * 70)

    context = await browser.new_context(
        viewport={"width": 1440, "height": 1200},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    )

    page = await context.new_page()

    all_records = []
    all_detail_links = []
    seen_pages = set()
    totals_seen = []

    try:
        for start_url in dealer_start_urls(competitor):

            current_url = start_url

            for page_number in range(1, MAX_PAGES + 1):

                if current_url in seen_pages:
                    break

                seen_pages.add(current_url)

                ok = await goto(page, current_url)

                if not ok:
                    break

                await auto_scroll(page)

                text = await page_text(page)

                total = displayed_total(text)

                if total:
                    totals_seen.append(total)
                    print(f"    Displayed inventory total: {total}")

                cards = await extract_cards(page, competitor)

                if cards:
                    print(f"    Boat records found on page: {len(cards)}")
                    all_records.extend(cards)

                links = await extract_listing_links(page, competitor)

                if links:
                    print(f"    Possible detail links: {len(links)}")
                    all_detail_links.extend(links)

                next_url = await find_next_link(page, current_url)

                if next_url and next_url not in seen_pages:
                    current_url = next_url
                    continue

                # Dealer Spike-style sites frequently support pg=
                # even when "Next" isn't easy to detect.
                if total and total > len(dedupe(all_records)):
                    guessed = set_page_parameter(start_url, page_number + 1)

                    if guessed not in seen_pages:
                        current_url = guessed
                        continue

                break

        all_records = dedupe(all_records)
        all_detail_links = list(dict.fromkeys(all_detail_links))

        print(f"    Unique card records: {len(all_records)}")
        print(f"    Unique detail links: {len(all_detail_links)}")

        # If cards were weak, inspect detail pages.
        # Also use detail pages to enrich missing stock/price fields.
        record_urls = {
            r.get("url")
            for r in all_records
            if r.get("url")
        }

        detail_targets = [
            url for url in all_detail_links
            if url not in record_urls
        ]

        # Prevent a malformed navigation page from creating thousands
        # of detail requests.
        detail_targets = detail_targets[:500]

        if len(all_records) < 5 or (
            totals_seen and len(all_records) < max(totals_seen) * 0.60
        ):
            print("    Card extraction incomplete. Reading detail pages...")

            semaphore = asyncio.Semaphore(6)

            async def guarded_detail(url):
                async with semaphore:
                    try:
                        return await extract_detail_page(
                            context,
                            competitor,
                            url
                        )
                    except Exception as exc:
                        print(f"    Detail error: {url}: {exc}")
                        return None

            details = await asyncio.gather(
                *(guarded_detail(url) for url in detail_targets)
            )

            for record in details:
                if record:
                    all_records.append(record)

            all_records = dedupe(all_records)

        # Final boat-only filter
        all_records = [
            record
            for record in all_records
            if is_boat_record(
                record.get("rawText", "") + " " + record.get("title", ""),
                record.get("url", "")
            )
        ]

        all_records = dedupe(all_records)

        new_count = sum(
            1 for r in all_records
            if r.get("condition") == "New"
        )

        used_count = sum(
            1 for r in all_records
            if r.get("condition") == "Used"
        )

        unknown_count = (
            len(all_records) - new_count - used_count
        )

        prices = [
            r["price"]
            for r in all_records
            if isinstance(r.get("price"), (int, float))
            and r["price"] >= 1000
        ]

        average_price = (
            round(sum(prices) / len(prices))
            if prices else None
        )

        site_total = max(totals_seen) if totals_seen else None
        parsed_total = len(all_records)

        # ----------------------------------------------------
        # VERIFICATION
        # ----------------------------------------------------

        verified = True
        reason = "Verified"

        if parsed_total == 0:
            verified = False
            reason = "No boat listings extracted"

        # Displayed totals can include non-boats on some dealer sites.
        # Therefore allow up to 20% difference after boat filtering.
        if site_total and parsed_total:
            difference = abs(site_total - parsed_total)
            tolerance = max(3, round(site_total * 0.20))

            if difference > tolerance:
                verified = False
                reason = (
                    f"Displayed total {site_total}, "
                    f"but extracted {parsed_total} boat records"
                )

        verified_at = (
            datetime.now(timezone.utc).isoformat()
            if verified else None
        )

        result = {
            "dealerId": competitor["id"],
            "dealer": competitor["name"],
            "url": competitor["url"],
            "status": "verified" if verified else "failed",
            "verified": verified,
            "verificationReason": reason,
            "verifiedAt": verified_at,
            "displayedTotal": site_total,
            "totalInventory": parsed_total if verified else None,
            "newInventory": new_count if verified else None,
            "usedInventory": used_count if verified else None,
            "unknownCondition": unknown_count if verified else None,
            "averagePrice": average_price if verified else None,
            "pricedListings": len(prices) if verified else None,
            "recordsExtracted": parsed_total
        }

        if verified:
            print(
                f"VERIFIED: {competitor['name']} | "
                f"{parsed_total} boats | "
                f"{new_count} new | "
                f"{used_count} used"
            )
        else:
            print(
                f"FAILED: {competitor['name']} | {reason}"
            )

        return result, all_records

    except Exception as exc:
        print(f"FAILED: {competitor['name']} | {exc}")

        return {
            "dealerId": competitor["id"],
            "dealer": competitor["name"],
            "url": competitor["url"],
            "status": "failed",
            "verified": False,
            "verificationReason": str(exc),
            "verifiedAt": None,
            "displayedTotal": None,
            "totalInventory": None,
            "newInventory": None,
            "usedInventory": None,
            "unknownCondition": None,
            "averagePrice": None,
            "pricedListings": None,
            "recordsExtracted": 0
        }, []

    finally:
        await context.close()


# ------------------------------------------------------------
# SCAN HISTORY
# ------------------------------------------------------------

def build_scan_record(results):
    now = datetime.now(timezone.utc)

    verified = [
        result for result in results
        if result["verified"]
    ]

    failed = [
        result for result in results
        if not result["verified"]
    ]

    return {
        "id": f"scan-{now.strftime('%Y%m%d-%H%M%S')}",
        "timestamp": now.isoformat(),
        "date": now.date().isoformat(),
        "status": (
            "complete"
            if not failed
            else "partial"
        ),
        "verifiedDealers": len(verified),
        "failedDealers": len(failed),
        "dealers": results
    }


def update_scans(scan_record):
    data = load_json(SCANS_FILE, {"scans": []})

    if isinstance(data, list):
        data = {"scans": data}

    if "scans" not in data:
        data["scans"] = []

    data["scans"].append(scan_record)

    # Keep roughly one year of daily scans.
    data["scans"] = data["scans"][-400:]

    save_json(SCANS_FILE, data)


def update_competitors(scan_record):
    data = load_json(
        COMPETITORS_FILE,
        {"lastScan": None, "competitors": []}
    )

    # lastScan means the scanner actually ran.
    data["lastScan"] = scan_record["timestamp"]

    result_map = {
        result["dealerId"]: result
        for result in scan_record["dealers"]
    }

    for competitor in data.get("competitors", []):
        result = result_map.get(competitor["id"])

        if not result:
            continue

        competitor["scanStatus"] = result["status"]
        competitor["verificationReason"] = result["verificationReason"]

        if result["verified"]:
            competitor["lastVerified"] = result["verifiedAt"]
            competitor["totalInventory"] = result["totalInventory"]
            competitor["newInventory"] = result["newInventory"]
            competitor["usedInventory"] = result["usedInventory"]
            competitor["averagePrice"] = result["averagePrice"]

        # IMPORTANT:
        # Failed scans do NOT overwrite the last verified inventory.
        # That prevents stale/failed numbers from being labeled as fresh.

    save_json(COMPETITORS_FILE, data)


# ------------------------------------------------------------
# TRENDS
# ------------------------------------------------------------

def update_trends():
    scans_data = load_json(SCANS_FILE, {"scans": []})

    scans = (
        scans_data.get("scans", [])
        if isinstance(scans_data, dict)
        else scans_data
    )

    trend_rows = []

    for scan in scans[-90:]:
        row = {
            "date": scan.get("date"),
            "timestamp": scan.get("timestamp"),
            "dealers": {}
        }

        for dealer in scan.get("dealers", []):
            if dealer.get("verified"):
                row["dealers"][dealer["dealerId"]] = {
                    "total": dealer.get("totalInventory"),
                    "new": dealer.get("newInventory"),
                    "used": dealer.get("usedInventory"),
                    "averagePrice": dealer.get("averagePrice")
                }

        trend_rows.append(row)

    save_json(
        TRENDS_FILE,
        {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "trends": trend_rows
        }
    )


# ------------------------------------------------------------
# MODEL MIX
# ------------------------------------------------------------

def brand_from_title(title, known_brands):
    t = normalize(title)

    matches = []

    for brand in known_brands:
        if "various" in normalize(brand):
            continue

        if normalize(brand) in t:
            matches.append(brand)

    if not matches:
        return "Other / Unknown"

    return max(matches, key=len)


def update_model_mix(all_records, competitors):
    competitor_map = {
        c["id"]: c
        for c in competitors
    }

    dealer_mix = {}

    for record in all_records:
        dealer_id = record["dealerId"]

        if dealer_id not in dealer_mix:
            dealer_mix[dealer_id] = {
                "total": 0,
                "brands": {},
                "conditions": {
                    "New": 0,
                    "Used": 0,
                    "Unknown": 0
                }
            }

        dealer_mix[dealer_id]["total"] += 1

        condition = record.get("condition", "Unknown")

        if condition not in ["New", "Used"]:
            condition = "Unknown"

        dealer_mix[dealer_id]["conditions"][condition] += 1

        competitor = competitor_map.get(dealer_id, {})
        brand = brand_from_title(
            record.get("title", ""),
            competitor.get("brands", [])
        )

        dealer_mix[dealer_id]["brands"][brand] = (
            dealer_mix[dealer_id]["brands"].get(brand, 0) + 1
        )

    save_json(
        MODEL_MIX_FILE,
        {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "dealers": dealer_mix
        }
    )


# ------------------------------------------------------------
# LIVE LISTING ARCHIVE
# ------------------------------------------------------------

def save_live_listings(records):
    cleaned_records = []

    for record in records:
        copy = dict(record)
        copy.pop("rawText", None)
        cleaned_records.append(copy)

    save_json(
        LIVE_LISTINGS_FILE,
        {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "count": len(cleaned_records),
            "listings": cleaned_records
        }
    )


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

async def main():
    print("")
    print("BTM COMPETITOR TRACKER")
    print("LIVE INVENTORY SCAN")
    print(datetime.now(timezone.utc).isoformat())
    print("")

    config = load_json(COMPETITORS_FILE, None)

    if not config:
        raise RuntimeError(
            f"Could not load {COMPETITORS_FILE}"
        )

    competitors = config.get("competitors", [])

    if not competitors:
        raise RuntimeError("No competitors configured")

    results = []
    all_records = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        try:
            # Sequential dealer scanning is intentional.
            # It is less likely to trigger dealer-site bot protection.
            for competitor in competitors:
                result, records = await scrape_dealer(
                    browser,
                    competitor
                )

                results.append(result)

                if result["verified"]:
                    all_records.extend(records)

        finally:
            await browser.close()

    all_records = dedupe(all_records)

    scan_record = build_scan_record(results)

    update_scans(scan_record)
    update_competitors(scan_record)
    update_trends()
    update_model_mix(all_records, competitors)
    save_live_listings(all_records)

    print("")
    print("=" * 70)
    print("SCAN COMPLETE")
    print("=" * 70)
    print(
        f"Verified dealers: "
        f"{scan_record['verifiedDealers']}/{len(competitors)}"
    )
    print(
        f"Failed dealers: "
        f"{scan_record['failedDealers']}/{len(competitors)}"
    )
    print(f"Verified live boat records: {len(all_records)}")
    print("")

    if scan_record["failedDealers"]:
        print("DEALERS REQUIRING ATTENTION:")

        for result in results:
            if not result["verified"]:
                print(
                    f" - {result['dealer']}: "
                    f"{result['verificationReason']}"
                )

        # IMPORTANT:
        # Do not exit with an error here.
        # Verified dealers should still be saved and published.
        # Failed dealers retain their prior verified values.


if __name__ == "__main__":
    asyncio.run(main())
