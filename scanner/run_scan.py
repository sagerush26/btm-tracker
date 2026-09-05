"""
Orchestrator: imports every dealer module in scanner/dealers/, calls scrape(),
normalizes the result into the DEALERS dict shape build.py expects, and writes
it to scanner/scan_output.json for build.py to consume.

Design goals:
- Never let one dealer's failure kill the whole run.
- If a dealer's scrape() raises or returns an implausible result (total == 0
  while a prior run had a real total), retry once, then fall back to the
  last known-good values already in data/competitors.json (carried forward)
  rather than writing zeros/garbage.
"""
import importlib
import json
import os
import sys
import time
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEALERS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dealers")
DATA_DIR = os.path.join(REPO_ROOT, "data")
sys.path.insert(0, DEALERS_DIR)

# module_name -> dealer id (must match ids already used in data/competitors.json)
DEALER_MODULES = {
    "big_thunder_marine": "big-thunder-marine",
    "marinemax": "marinemax",
    "performance_boat_center": "performance-boat-center",
    "kellys_port": "kellys-port",
    "village_marina": "village-marina",
    "iguana_marine": "iguana-marine",
    "premier_advantage": "premier-advantage",
    "formula_boats_mo": "formula-boats-mo",
    "performance_marine": "performance-marine",
    "heartland_marine": "heartland-marine",
    "stateamind": "stateamind",
    "premier_54": "premier-54",
    "martys_marine": "martys-marine",
    "surdyke_yamaha": "surdyke-yamaha",
    "all_about_boats": "all-about-boats",
}

OWN_DEALER_ID = "big-thunder-marine"


def load_prior_competitors():
    path = os.path.join(DATA_DIR, "competitors.json")
    with open(path) as f:
        data = json.load(f)
    return {c["id"]: c for c in data["competitors"]}


def price_quality(prices, total):
    if total <= 0:
        return "range-only"
    pct = round(100 * len(prices) / total)
    if pct >= 95:
        return "full"
    if pct == 0:
        return "range-only"
    return f"partial-{pct}pct"


def run_one(module_name, dealer_id, prior):
    mod = importlib.import_module(module_name)
    last_err = None
    for attempt in range(2):
        try:
            result = mod.scrape()
            total = result.get("total", 0)
            if total and total > 0:
                return result, None
            last_err = "scrape() returned total <= 0"
        except Exception:
            last_err = traceback.format_exc()
        if attempt == 0:
            time.sleep(2)
    # Both attempts failed / returned nothing usable -> carry forward last known values
    p = prior.get(dealer_id, {})
    carried = {
        "id": dealer_id,
        "name": p.get("name", dealer_id),
        "total": p.get("totalInventory", 0),
        "new": p.get("newInventory", 0),
        "used": p.get("usedInventory", 0),
        "category": {},
        "brand": {},
        "prices": [],
        "priceLow": None,
        "priceHigh": None,
        "_carried_forward": True,
    }
    return carried, last_err


def main():
    prior = load_prior_competitors()
    DEALERS = {}
    failures = []

    for module_name, dealer_id in DEALER_MODULES.items():
        print(f"--- scanning {dealer_id} ---")
        result, err = run_one(module_name, dealer_id, prior)
        carried = result.pop("_carried_forward", False)
        if carried:
            failures.append((dealer_id, err))
            print(f"  ! carried forward last known values for {dealer_id}: {err}")

        prices = result.get("prices") or []
        total = result.get("total", 0)
        priceLow = result.get("priceLow")
        priceHigh = result.get("priceHigh")
        if priceLow is None or priceHigh is None:
            if prices:
                vals = [p for p, _ in prices]
                priceLow, priceHigh = min(vals), max(vals)
            else:
                pc = prior.get(dealer_id, {})
                priceLow = pc.get("priceLow", 0)
                priceHigh = pc.get("priceHigh", 0)

        DEALERS[dealer_id] = dict(
            name=result.get("name", dealer_id),
            isOwn=(dealer_id == OWN_DEALER_ID),
            total=total,
            new=result.get("new", 0),
            used=result.get("used", 0),
            category=result.get("category", {}) or {},
            brand=result.get("brand", {}) or {},
            prices=prices,
            priceLow=priceLow,
            priceHigh=priceHigh,
            priceQuality=price_quality(prices, total),
        )
        print(f"  total={total} new={result.get('new')} used={result.get('used')}")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_output.json")
    with open(out_path, "w") as f:
        json.dump({"DEALERS": DEALERS, "failures": failures}, f, indent=2)
    print(f"\nWrote {out_path}")
    if failures:
        print("Dealers that used carried-forward data:", [d for d, _ in failures])
    return DEALERS, failures


if __name__ == "__main__":
    main()
