"""
One-off idempotent backfill: applies the same trends recompute logic now living
in build.py directly against the existing data/scans.json + data/trends.json,
WITHOUT appending a new scan (today's scan-078 already ran before this fix
landed). Run once after merging the build.py fix; the nightly scanner run will
take over from here since build.py now does this automatically every night.
"""
import json, os

SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCANNER_DIR)
DATA_DIR = os.path.join(REPO_ROOT, "data")

scans = json.load(open(f"{DATA_DIR}/scans.json"))
competitors = json.load(open(f"{DATA_DIR}/competitors.json"))
trends = json.load(open(f"{DATA_DIR}/trends.json"))

NAME_BY_ID = {c["id"]: c["name"] for c in competitors["competitors"]}
last_scan = scans["scans"][-1]
SCAN_DATE = last_scan["date"]
SCAN_ID = last_scan["scanId"]
market_total = sum(r["totalBoats"] for r in last_scan["results"])

all_changes = scans["changes"]

sold_by_dealer, added_by_dealer = {}, {}
for c in all_changes:
    did = c.get("dealerId") or c.get("competitorId")
    name = NAME_BY_ID.get(did) or c.get("dealerName") or did or "Unknown"
    if c.get("type") == "inventory_decrease":
        sold_by_dealer[name] = sold_by_dealer.get(name, 0) + c.get("count", 0)
    elif c.get("type") == "inventory_increase":
        added_by_dealer[name] = added_by_dealer.get(name, 0) + c.get("count", 0)

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

if not sold_by_category and not sold_by_brand:
    already_noted = any(
        i.get("date") == SCAN_DATE and "restarted today" in i.get("text", "")
        for i in trends.get("insights", [])
    )
    if not already_noted:
        trends.setdefault("insights", []).append({
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
trends["soldByPriceBand"] = {}
trends["soldByLengthBand"] = {}

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
    "credibleChangesCount": trends.get("summary", {}).get("credibleChangesCount", 0),
    "soldCount": sum(sold_by_dealer.values()),
    "addedCount": sum(added_by_dealer.values()),
}

with open(f"{DATA_DIR}/trends.json", "w") as f:
    json.dump(trends, f, indent=2)

print("trends.json backfilled.")
print("soldByDealer:", sold_by_dealer)
print("soldByCondition:", trends["soldByCondition"])
print("summary:", trends["summary"])
print("recentlySold/recentlyAdded counts:", len(trends["recentlySold"]), len(trends["recentlyAdded"]))
