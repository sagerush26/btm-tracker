"""
Adapted from the original manual-scan build.py. Consumes scanner/scan_output.json
(produced by run_scan.py) instead of a hand-maintained data.py, and writes the
regenerated JSON straight back into data/ (the live site's data directory) --
there's no separate "baseline vs output" split anymore since this runs
unattended and data/ IS both the baseline for tomorrow and the output for today.
"""
import json, sys, os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCANNER_DIR)
DATA_DIR = os.path.join(REPO_ROOT, "data")

with open(os.path.join(SCANNER_DIR, "scan_output.json")) as f:
    _scan_output = json.load(f)
DEALERS = _scan_output["DEALERS"]
FAILURES = _scan_output.get("failures", [])

_now_utc = datetime.now(timezone.utc)
_now_ct = _now_utc.astimezone(ZoneInfo("America/Chicago"))
SCAN_DATE = _now_ct.strftime("%Y-%m-%d")
NOW_ISO = _now_utc.isoformat()

_prev_scans = json.load(open(f"{DATA_DIR}/scans.json"))
_last_scan_id = _prev_scans["scans"][-1]["scanId"]
_last_num = int(_last_scan_id.split("-")[-1])
SCAN_ID = f"scan-{_last_num + 1:03d}"

# ---------- load existing baselines ----------
scans = json.load(open(f"{DATA_DIR}/scans.json"))
competitors = json.load(open(f"{DATA_DIR}/competitors.json"))
modelmix = json.load(open(f"{DATA_DIR}/model-mix.json"))
trends = json.load(open(f"{DATA_DIR}/trends.json"))

scan_prev = scans["scans"][-1]
scan_prev_by_id = {r["competitorId"]: r for r in scan_prev["results"]}

def fmt_money(v):
    return "${:,.0f}".format(v)

def avg(vals):
    return round(sum(vals)/len(vals), 2) if vals else None

# ---------- Build this scan's results ----------
new_results = []
dealer_avg_info = {}
for cid, d in DEALERS.items():
    prices = d["prices"]
    new_prices = [p for p, cond in prices if cond == "new"]
    used_prices = [p for p, cond in prices if cond in ("used", "unknown")]
    all_prices = [p for p, _ in prices]
    if all_prices:
        avgPrice = avg(all_prices)
    elif d["priceLow"] and d["priceHigh"]:
        avgPrice = round((d["priceLow"] * d["priceHigh"]) ** 0.5, 2)
    else:
        # nothing to go on (e.g. carried-forward dealer with no price data) -- reuse prior avg if we have it
        prev = scan_prev_by_id.get(cid)
        avgPrice = prev["avgPrice"] if prev else 0
    avgPriceNew = avg(new_prices) if new_prices else (avgPrice if not all_prices else None)
    avgPriceUsed = avg(used_prices) if used_prices else (avgPrice if not all_prices else None)
    priceRange = f"{fmt_money(d['priceLow'])} - {fmt_money(d['priceHigh'])}" if d["priceLow"] and d["priceHigh"] else "N/A"
    result = {
        "competitorId": cid,
        "totalBoats": d["total"],
        "newBoats": d["new"],
        "usedBoats": d["used"],
        "avgPrice": avgPrice,
        "avgPriceNew": avgPriceNew if avgPriceNew is not None else avgPrice,
        "avgPriceUsed": avgPriceUsed if avgPriceUsed is not None else avgPrice,
        "priceRange": priceRange,
        # Persisted so future runs can diff category/brand mix scan-over-scan for
        # real sold/added breakdowns (see trends recompute section below).
        "category": d["category"],
        "brand": d["brand"],
    }
    new_results.append(result)
    dealer_avg_info[cid] = (avgPrice, d["priceQuality"])

scan_new = {"scanId": SCAN_ID, "date": SCAN_DATE, "results": new_results}
scans["scans"].append(scan_new)

# ---------- Build changes log ----------
prev_scan_date = scan_prev.get("date", SCAN_DATE)
try:
    _gap_days = (datetime.strptime(SCAN_DATE, "%Y-%m-%d") - datetime.strptime(prev_scan_date, "%Y-%m-%d")).days
except Exception:
    _gap_days = None
if _gap_days is not None and _gap_days > 3:
    gap_note = f"Gap vs prior scan spans {_gap_days} days ({prev_scan_date} to {SCAN_DATE}), likely due to one or more missed/failed nightly runs; reflects a cumulative shift over that period, not a single-day change."
else:
    gap_note = f"Reflects the normal one-day change since the prior scan on {prev_scan_date}."

HISTORICAL_NAME_MAP = {
    "big-thunder-marine": "Big Thunder Marine",
    "marinemax": "MarineMax Lake Ozark",
    "performance-boat-center": "Performance Boat Center",
    "kellys-port": "Kelly's Port",
    "village-marina": "Village Marina & Yacht Club",
    "iguana-marine": "Iguana Marine Group",
    "premier-advantage": "Premier Advantage Marine",
    "formula-boats-mo": "Formula Boats of Missouri",
    "performance-marine": "Performance Marine Watersports",
    "heartland-marine": "Heartland Marine",
    "stateamind": "Stateamind Water Sports",
    "premier-54": "Premier 54 Motorsports",
    "martys-marine": "Marty's Marine",
    "surdyke-yamaha": "Surdyke Yamaha & Marina",
    "all-about-boats": "All About Boats",
}

new_changes = []
for cid, d in DEALERS.items():
    prev = scan_prev_by_id.get(cid)
    prev_total = prev["totalBoats"] if prev else None
    new_total = d["total"]
    if prev_total is None:
        continue
    change = new_total - prev_total
    if change == 0:
        continue
    ctype = "inventory_increase" if change > 0 else "inventory_decrease"
    desc = f"{d['name']}: {prev_total} -> {new_total} ({'+' if change > 0 else ''}{change})"
    detail = f"Inventory {'increased' if change > 0 else 'decreased'} by {abs(change)} (was {prev_total}, now {new_total}). New: {d['new']}, Used: {d['used']}. {gap_note}"
    new_changes.append({
        "scanId": SCAN_ID,
        "date": SCAN_DATE,
        "competitorId": cid,
        "dealerId": cid,
        "dealerName": d["name"],
        "previousTotal": prev_total,
        "newTotal": new_total,
        "change": change,
        "type": ctype,
        "count": abs(change),
        "description": desc,
        "detail": detail,
        "note": gap_note,
    })
scans["changes"].extend(new_changes)
scans["lastUpdated"] = NOW_ISO

# ---------- Backfill legacy fields on ALL historical change entries ----------
backfilled = 0
for c in scans["changes"]:
    if "dealerName" not in c:
        cid = c.get("competitorId")
        name = HISTORICAL_NAME_MAP.get(cid, cid or "Unknown Dealer")
        chg = c.get("change", 0)
        c["dealerId"] = cid
        c["dealerName"] = name
        c["type"] = "inventory_increase" if chg > 0 else "inventory_decrease"
        c["count"] = abs(chg)
        c["detail"] = c.get("description") or f"Inventory {'increased' if chg > 0 else 'decreased'} by {abs(chg)} (was {c.get('previousTotal')}, now {c.get('newTotal')})."
        backfilled += 1
print("Backfilled legacy fields on", backfilled, "historical change entries")

with open(f"{DATA_DIR}/scans.json", "w") as f:
    json.dump(scans, f, indent=2)
print("scans.json written. New changes:", len(new_changes))
for c in new_changes:
    print(" ", c["description"])

# ---------- Update competitors.json ----------
failed_ids = {d for d, _ in FAILURES}
comp_by_id = {c["id"]: c for c in competitors["competitors"]}
for cid, d in DEALERS.items():
    c = comp_by_id.get(cid)
    if not c:
        continue
    avgPrice, quality = dealer_avg_info[cid]
    if cid in failed_ids:
        c["scanStatus"] = "carried_forward"
        c["verificationReason"] = "Live scrape failed after retry; showing last known-good values."
    else:
        c["scanStatus"] = "verified"
        c["verificationReason"] = "Verified" if quality in ("full","partial-70pct","partial-72pct","partial-82pct","partial-85pct","partial-41pct","partial-48pct") else "Verified (totals/condition from site facets; price is a range-based estimate, not a full listing average)"
    c["lastVerified"] = NOW_ISO
    c["totalInventory"] = d["total"]
    c["newInventory"] = d["new"]
    c["usedInventory"] = d["used"]
    c["averagePrice"] = avgPrice
competitors["lastScan"] = NOW_ISO

with open(f"{DATA_DIR}/competitors.json", "w") as f:
    json.dump(competitors, f, indent=2)
print("competitors.json written.")
for c in competitors["competitors"]:
    print(" ", c["id"], c.get("scanStatus"), c.get("totalInventory"), c.get("newInventory"), c.get("usedInventory"), c.get("averagePrice"))

# ---------- Build model-mix.json ----------
dealers_mm = []
market_category = {}
market_brand = {}
market_condition = {"New": 0, "Used": 0}
market_total = 0
market_new = 0
market_used = 0

for cid, d in DEALERS.items():
    avgPrice, quality = dealer_avg_info[cid]
    result = next(r for r in new_results if r["competitorId"] == cid)
    entry = {
        "id": cid,
        "name": d["name"],
        "isOwn": d["isOwn"],
        "totalBoats": d["total"],
        "newBoats": d["new"],
        "usedBoats": d["used"],
        "avgPriceNew": result["avgPriceNew"],
        "avgPriceUsed": result["avgPriceUsed"],
        "byCondition": {"New": d["new"], "Used": d["used"]},
        "byCategory": d["category"],
        "byBrand": d["brand"],
        "byLength": {},
        "byPropulsion": {},
    }
    dealers_mm.append(entry)
    for k, v in d["category"].items():
        market_category[k] = market_category.get(k, 0) + v
    for k, v in d["brand"].items():
        market_brand[k] = market_brand.get(k, 0) + v
    market_condition["New"] += d["new"]
    market_condition["Used"] += d["used"]
    market_total += d["total"]
    market_new += d["new"]
    market_used += d["used"]

modelmix["dealers"] = dealers_mm
modelmix["marketTotals"] = {
    "totalBoats": market_total,
    "newBoats": market_new,
    "usedBoats": market_used,
    "byCondition": market_condition,
    "byCategory": market_category,
    "byBrand": dict(sorted(market_brand.items(), key=lambda x: -x[1])),
    "byLength": {},
    "byPropulsion": {},
}
modelmix["scanId"] = SCAN_ID
modelmix["date"] = SCAN_DATE
modelmix["lastUpdated"] = NOW_ISO

with open(f"{DATA_DIR}/model-mix.json", "w") as f:
    json.dump(modelmix, f, indent=2)
print("model-mix.json written. Market total:", market_total)

# ---------- Fix pre-existing dealer-key duplication in trends.json ----------
for key in ["soldByDealer", "soldByCategory", "soldByBrand", "soldByPriceBand", "soldByLengthBand", "addedByCategory"]:
    val = trends.get(key)
    if not isinstance(val, dict):
        continue
    for cid, name in HISTORICAL_NAME_MAP.items():
        if cid in val:
            val[name] = val.get(name, 0) + val.pop(cid)

# ---------- Update trends.json (append snapshot + note the data gap) ----------
weighted_sum = sum(r["avgPrice"] * DEALERS[r["competitorId"]]["total"] for r in new_results)
market_avg_price = round(weighted_sum / market_total, 2) if market_total else 0
btm_avg_price = next(r["avgPrice"] for r in new_results if r["competitorId"] == "big-thunder-marine")
competitor_total = market_total - DEALERS["big-thunder-marine"]["total"]

trends["inventoryTrend"].append({
    "date": SCAN_DATE, "totalMarket": market_total,
    "btm": DEALERS["big-thunder-marine"]["total"], "competitors": competitor_total,
})
trends["priceTrend"].append({
    "date": SCAN_DATE, "marketAvg": market_avg_price, "btmAvg": btm_avg_price,
})
if _gap_days is not None and _gap_days > 3:
    insight_text = (
        f"Fresh full-market scan completed {SCAN_DATE} ({SCAN_ID}) after a {_gap_days}-day gap since the prior "
        f"scan on {prev_scan_date}, likely due to one or more missed/failed nightly runs. This update reflects "
        f"cumulative change over that full gap in a single jump, not a normal nightly delta."
    )
elif failed_ids:
    insight_text = (
        f"Nightly scan completed {SCAN_DATE} ({SCAN_ID}). {len(failed_ids)} dealer(s) failed to load live data "
        f"and are showing carried-forward values from the prior scan: {', '.join(sorted(failed_ids))}."
    )
else:
    insight_text = f"Nightly scan completed {SCAN_DATE} ({SCAN_ID}), reflecting the normal one-day change since {prev_scan_date}."
trends["insights"].append({
    "date": SCAN_DATE, "type": "data_quality",
    "text": insight_text,
})
# ---------- Recompute real sold/added analytics from full scan history ----------
# Fixed 2026-09-10: soldByDealer/soldByCondition/soldByCategory/soldByBrand/
# soldByPriceBand/soldByLengthBand/recentlySold/recentlyAdded/summary.soldCount/
# addedCount were frozen at scan-070 (2026-05-22) and never recomputed by any prior
# version of this script, even though the nightly pipeline kept running -- the Trends
# tab was silently showing 3.5-month-stale "sold" data every night since. These are
# now derived every run from the real, verified scan history in scans.json instead of
# being carried forward unchanged.
all_changes = scans["changes"]

# Group by dealerId (competitorId), not the raw historical dealerName -- some
# dealers (e.g. marinemax) were renamed in DEALERS at some point in the past, and
# the change log preserves whatever name was current at the time, which would
# otherwise split one dealer's totals across two differently-named rows.
sold_by_dealer, added_by_dealer = {}, {}
for c in all_changes:
    did = c.get("dealerId") or c.get("competitorId")
    name = DEALERS.get(did, {}).get("name") or c.get("dealerName") or did or "Unknown"
    if c.get("type") == "inventory_decrease":
        sold_by_dealer[name] = sold_by_dealer.get(name, 0) + c.get("count", 0)
    elif c.get("type") == "inventory_increase":
        added_by_dealer[name] = added_by_dealer.get(name, 0) + c.get("count", 0)

# Condition (new/used) split: walk every consecutive pair of full-market scans per
# dealer using the newBoats/usedBoats stored in each historical scan snapshot.
sold_new = sold_used = added_new = added_used = 0
sold_by_category, added_by_category = {}, {}
sold_by_brand, added_by_brand = {}, {}
prev_by_id = {}
for scan_entry in scans["scans"]:
    for r in scan_entry["results"]:
        cid = r["competitorId"]
        cn, cu = r.get("newBoats", 0), r.get("usedBoats", 0)
        cat, brand = r.get("category"), r.get("brand")
        if cid in prev_by_id:
            pn, pu, pcat, pbrand = prev_by_id[cid]
            if cn < pn: sold_new += (pn - cn)
            elif cn > pn: added_new += (cn - pn)
            if cu < pu: sold_used += (pu - cu)
            elif cu > pu: added_used += (cu - pu)
            # Category/brand breakdowns only become available once two consecutive
            # scans for the same dealer both persisted these dicts (see above).
            if cat is not None and pcat is not None:
                for k, v in cat.items():
                    pv = pcat.get(k, 0)
                    if v < pv: sold_by_category[k] = sold_by_category.get(k, 0) + (pv - v)
                    elif v > pv: added_by_category[k] = added_by_category.get(k, 0) + (v - pv)
            if brand is not None and pbrand is not None:
                for k, v in brand.items():
                    pv = pbrand.get(k, 0)
                    if v < pv: sold_by_brand[k] = sold_by_brand.get(k, 0) + (pv - v)
                    elif v > pv: added_by_brand[k] = added_by_brand.get(k, 0) + (v - pv)
        prev_by_id[cid] = (cn, cu, cat, brand)

trends["soldByDealer"] = sold_by_dealer
trends["addedByDealer"] = added_by_dealer
trends["soldByCondition"] = {"New": sold_new, "Used": sold_used}
trends["addedByCondition"] = {"New": added_new, "Used": added_used}

# Price-band and length-band sold breakdowns need a per-listing price/length history
# we don't persist (only aggregate category/brand counts). Leaving these empty is
# honest -- the dashboard already renders an explicit "Data builds after scans detect
# changes" placeholder for empty chart data instead of a misleading zero/stale chart.
if not sold_by_category and not sold_by_brand:
    trends["insights"].append({
        "date": SCAN_DATE, "type": "data_quality",
        "text": (
            "Sold-by-category and sold-by-brand tracking restarted today: per-scan "
            "category/brand snapshots weren't persisted before this run, so tonight's "
            "scan has nothing to diff against yet. These charts need two consecutive "
            "nightly scans with category/brand data to compute a real difference, so "
            "they'll start populating within the next couple of nightly runs. "
            "Sold-by-dealer and sold-by-condition (new/used) are already accurate as "
            "of tonight, computed from the real scan history."
        ),
    })
trends["soldByCategory"] = sold_by_category
trends["soldByBrand"] = sold_by_brand
trends["addedByCategory"] = added_by_category
trends["addedByBrand"] = added_by_brand
# Price-band/length-band sold breakdowns are not derivable from persisted data yet.
trends["soldByPriceBand"] = {}
trends["soldByLengthBand"] = {}

# Recently sold/added: derived directly from the real, verified change log (most
# recent 50 events each) instead of the frozen May snapshot.
def _activity_item(c):
    return {
        "date": c["date"], "scanId": c["scanId"],
        "dealer": c.get("dealerName"), "competitorId": c.get("competitorId") or c.get("dealerId"),
        "count": c.get("count", abs(c.get("change", 0))),
        "previousTotal": c.get("previousTotal"), "newTotal": c.get("newTotal"),
        "detail": c.get("detail", c.get("description", "")),
    }
sold_events = [c for c in all_changes if c.get("type") == "inventory_decrease"]
added_events = [c for c in all_changes if c.get("type") == "inventory_increase"]
trends["recentlySold"] = [_activity_item(c) for c in sold_events[-50:]]
trends["recentlyAdded"] = [_activity_item(c) for c in added_events[-50:]]

trends["summary"] = {
    "date": SCAN_DATE,
    "scanId": SCAN_ID,
    "marketTotal": market_total,
    "credibleChangesCount": len(new_changes),
    "soldCount": sum(sold_by_dealer.values()),
    "addedCount": sum(added_by_dealer.values()),
}

with open(f"{DATA_DIR}/trends.json", "w") as f:
    json.dump(trends, f, indent=2)
print("trends.json written. marketAvg:", market_avg_price, "btmAvg:", btm_avg_price)
print("Real sold/added totals -- sold:", sum(sold_by_dealer.values()), "added:", sum(added_by_dealer.values()))
print("Recently sold/added events:", len(trends["recentlySold"]), len(trends["recentlyAdded"]))

if failed_ids:
    print("\n⚠ Dealers using carried-forward data this run:", sorted(failed_ids))
