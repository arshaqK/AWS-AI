"""Generate dataset 2: a raw end-of-month inventory snapshot from Trailhead Outfitters'
warehouse management system (fictional), one row per product per warehouse.

Like the orders export, it carries the quirks real warehouse exports have, and every
one is counted into an answer key (inventory_snapshot.expected.json).

New kinds of mess compared to orders:
  - numeric counts with stray "N/A"/"-", thousands separators and negatives (backorders)
  - weights in mixed units (kg, g, lb, lbs, oz)
  - timestamps as ISO, Unix epoch seconds, epoch milliseconds and DD/MM/YYYY HH:MM
  - a tags column that is sometimes a JSON list, sometimes a single value, sometimes "a;b"
  - booleans spelled Y/N/yes/no/1/0/true/false/TRUE/FALSE

Run from anywhere:  python data/generate_inventory.py
Output is deterministic for a given SEED.
"""
import csv
import json
import os
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

SEED = 7
N_DUPLICATES = 4
# DATA_OUT_DIR lets tests regenerate into a scratch folder without touching these files
OUT_DIR = Path(os.environ.get("DATA_OUT_DIR") or Path(__file__).resolve().parent)
CSV_PATH = OUT_DIR / "inventory_snapshot.csv"
KEY_PATH = OUT_DIR / "inventory_snapshot.expected.json"

rng = random.Random(SEED)

SNAPSHOT_DATE = "2025-06-30"
WAREHOUSES = ["SEA1", "RNO2", "DAL1", "CVG1", "TOR1"]  # clean codes: part of the key
LB, OZ = Decimal("0.45359237"), Decimal("0.028349523125")
NULL_TOKENS = ["", "N/A", "-", "null"]

# The 10 products sold in the orders export, so the datasets join on sku.
ORDER_SKUS = [
    ("SKU-TNT-2P", "Ridgeline 2P Tent", 2.35, ["tent", "2p"]),
    ("SKU-BAG-20F", "Summit 20F Sleeping Bag", 1.10, ["sleeping-bag"]),
    ("SKU-PCK-45L", "Trailblazer 45L Pack", 1.45, ["pack", "45l"]),
    ("SKU-JKT-RN", "Stormshell Rain Jacket", 0.38, ["jacket", "rain"]),
    ("SKU-BTL-1L", "Insulated Bottle 1L", 0.42, ["bottle"]),
    ("SKU-STV-MC", "MicroCook Stove", 0.09, ["stove", "cooking"]),
    ("SKU-HLP-400", "Beacon 400 Headlamp", 0.08, ["headlamp"]),
    ("SKU-PLS-CF", "Carbon Trekking Poles", 0.46, ["poles", "carbon"]),
    ("SKU-SCK-MW", "Merino Hiking Socks", 0.07, ["socks", "merino"]),
    ("SKU-MAT-UL", "UltraLite Sleeping Pad", 0.40, ["sleeping-pad"]),
]
CATEGORIES = [  # code, name, typical kg, tags
    ("TNT", "Tent", 2.8, ["tent"]), ("BAG", "Sleeping Bag", 1.3, ["sleeping-bag"]),
    ("PCK", "Pack", 1.2, ["pack"]), ("JKT", "Jacket", 0.5, ["jacket"]),
    ("BTL", "Bottle", 0.35, ["bottle"]), ("GLV", "Gloves", 0.12, ["gloves"]),
    ("HAT", "Beanie", 0.08, ["hat"]), ("LAN", "Lantern", 0.3, ["lantern"]),
]
MODELS = ["Alpine", "Basecamp", "Cascade", "Drift", "Echo", "Fjord", "Granite", "Horizon", "Ion"]


def build_products():
    products = [{"sku": s, "name": n, "kg": kg, "tags": t} for s, n, kg, t in ORDER_SKUS]
    for code, cname, kg, tags in CATEGORIES:
        for model in rng.sample(MODELS, 8):
            extra = rng.choice([[], [], ["ultralight"], ["kids"], ["waterproof"]])
            products.append({
                "sku": f"SKU-{code}-{model[:3].upper()}{rng.randint(10, 99)}",
                "name": f"{model} {cname}",
                "kg": round(kg * rng.uniform(0.6, 1.4), 2),
                "tags": tags + extra,
            })
    assert len({p["sku"] for p in products}) == len(products), "duplicate sku"
    return products


def render_weight(kg: float, key: dict):
    """Write the weight in a random unit, and return the exact kg the written value means."""
    if rng.random() < 0.03:
        token = rng.choice(NULL_TOKENS)
        key["null_tokens"]["weight"][token or "<empty>"] += 1
        return token, None
    unit = rng.choices(["kg", "g", "lb", "lbs", "oz"], weights=[40, 25, 15, 10, 10])[0]
    space = rng.choice([" ", ""])
    if unit == "kg":
        shown = Decimal(str(round(kg, 2)))
        exact = shown
    elif unit == "g":
        shown = Decimal(round(kg * 1000))
        exact = shown / 1000
    elif unit in ("lb", "lbs"):
        shown = (Decimal(str(kg)) / LB).quantize(Decimal("0.01"), ROUND_HALF_UP)
        exact = shown * LB
    else:
        shown = (Decimal(str(kg)) / OZ).quantize(Decimal("0.1"), ROUND_HALF_UP)
        exact = shown * OZ
    key["weight_units"][unit] += 1
    return f"{shown}{space}{unit}", exact.quantize(Decimal("0.001"), ROUND_HALF_UP)


def render_counted_at(ts: datetime, key: dict) -> str:
    r = rng.random()
    if r < 0.50:
        key["counted_at_formats"]["ISO-8601 UTC (...Z)"] += 1
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    if r < 0.75:
        key["counted_at_formats"]["epoch seconds"] += 1
        return str(int(ts.timestamp()))
    if r < 0.90:
        key["counted_at_formats"]["epoch milliseconds"] += 1
        return str(int(ts.timestamp()) * 1000)
    key["counted_at_formats"]["DD/MM/YYYY HH:MM (UTC)"] += 1
    return ts.strftime("%d/%m/%Y %H:%M")


def render_tags(tags: list, key: dict) -> str:
    if not tags or rng.random() < 0.04:
        style = rng.choice(["[]", ""])
        key["tags_formats"]["no tags (" + (style or "empty") + ")"] += 1
        return style
    if len(tags) == 1 and rng.random() < 0.5:
        key["tags_formats"]["single value"] += 1
        return tags[0]
    if len(tags) > 1 and rng.random() < 0.2:  # one tag joined by ";" is just a single value
        key["tags_formats"]["semicolon list"] += 1
        return ";".join(tags)
    key["tags_formats"]["JSON list"] += 1
    return json.dumps(tags)


def main() -> None:
    key = {
        "dataset": "inventory_snapshot.csv",
        "description": "End-of-month inventory snapshot from Trailhead Outfitters' warehouse system (fictional)",
        "seed": SEED,
        "primary_key": ["sku", "warehouse"],
        "vendor_conventions": {
            "timestamps": "all timestamps are UTC; numeric ones are Unix epoch seconds or milliseconds",
            "slash_dates": "DD/MM/YYYY HH:MM",
            "negative_on_hand": "a negative on_hand is a backorder (more sold than in stock) and is valid",
            "weights": "1 lb = 0.45359237 kg, 1 oz = 0.028349523125 kg",
        },
        "counts": Counter(),
        "null_tokens": {"on_hand": Counter(), "reserved": Counter(), "weight": Counter()},
        "weight_units": Counter(),
        "counted_at_formats": Counter(),
        "tags_formats": Counter(),
        "is_active_raw_to_bool": {},
    }
    yes, no = ["Y", "yes", "1", "true", "TRUE", "Yes"], ["N", "no", "0", "false", "FALSE", "No"]

    products = build_products()
    snapshot_end = datetime(2025, 6, 30, 23, 0, tzinfo=timezone.utc)
    rows, exact = [], []
    for p in products:
        for wh in WAREHOUSES:
            if rng.random() > 0.65:
                continue
            on_hand = rng.randint(0, 400) if p["kg"] > 0.2 else rng.randint(200, 2600)
            r = rng.random()
            if r < 0.05:
                on_hand_raw = rng.choice(NULL_TOKENS)
                key["null_tokens"]["on_hand"][on_hand_raw or "<empty>"] += 1
                on_hand_val = None
            elif r < 0.08:
                on_hand_val = -rng.randint(1, 12)
                on_hand_raw = str(on_hand_val)
                key["counts"]["negative_on_hand"] += 1
            else:
                on_hand_val = on_hand
                if on_hand >= 1000 and rng.random() < 0.6:
                    on_hand_raw = f"{on_hand:,}"
                    key["counts"]["on_hand_with_thousands_separator"] += 1
                else:
                    on_hand_raw = str(on_hand)

            if rng.random() < 0.03:
                reserved_raw = rng.choice(["N/A", "", "-"])
                key["null_tokens"]["reserved"][reserved_raw or "<empty>"] += 1
            else:
                reserved_raw = str(rng.randint(0, max(0, on_hand_val or 0) // 5))

            weight_raw, weight_kg = render_weight(p["kg"], key)
            counted = snapshot_end - timedelta(days=rng.randint(0, 29), minutes=rng.randint(0, 1439))
            active = rng.random() > 0.12
            is_active_raw = rng.choice(yes if active else no)
            key["is_active_raw_to_bool"][is_active_raw] = active

            rows.append({
                "snapshot_date": SNAPSHOT_DATE,
                "warehouse": wh,
                "sku": p["sku"],
                "product_name": p["name"],
                "on_hand": on_hand_raw,
                "reserved": reserved_raw,
                "weight": weight_raw,
                "last_counted_at": render_counted_at(counted, key),
                "tags": render_tags(p["tags"], key),
                "is_active": is_active_raw,
            })
            exact.append({"on_hand": on_hand_val, "weight_kg": weight_kg, "active": active,
                          "tags": rows[-1]["tags"]})

    n_unique = len(rows)
    for src in rng.sample(rows, N_DUPLICATES):  # the warehouse system re-sent some rows
        rows.insert(rng.randint(0, len(rows)), dict(src))

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    weights = [e["weight_kg"] for e in exact if e["weight_kg"] is not None]
    key["counts"] = dict(sorted(key["counts"].items()))
    for k in ("weight_units", "counted_at_formats", "tags_formats"):
        key[k] = dict(sorted(key[k].items()))
    key["null_tokens"] = {k: dict(v) for k, v in key["null_tokens"].items()}
    key["is_active_raw_to_bool"] = dict(sorted(key["is_active_raw_to_bool"].items()))
    key["rows_in_file"] = len(rows)
    key["unique_rows"] = n_unique
    key["exact_duplicate_rows"] = N_DUPLICATES
    key["products"] = len(products)
    key["columns"] = list(rows[0])
    key["pii_columns"] = []
    key["expected_clean_output"] = {
        "rows_after_dedup": n_unique,
        "distinct_warehouses": len({r["warehouse"] for r in rows}),
        "distinct_skus": len({r["sku"] for r in rows}),
        "null_on_hand": sum(e["on_hand"] is None for e in exact),
        "negative_on_hand": sum(e["on_hand"] is not None and e["on_hand"] < 0 for e in exact),
        "null_weight_kg": sum(e["weight_kg"] is None for e in exact),
        "sum_weight_kg": str(sum(weights)),
        "active_rows": sum(e["active"] for e in exact),
        "rows_without_tags": sum(e["tags"] in ("", "[]") for e in exact),
    }
    KEY_PATH.write_text(json.dumps(key, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {CSV_PATH.name}: {len(rows)} rows ({n_unique} unique + {N_DUPLICATES} duplicates)")
    print(f"wrote {KEY_PATH.name}")


if __name__ == "__main__":
    main()
