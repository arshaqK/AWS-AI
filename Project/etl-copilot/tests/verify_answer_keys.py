"""Step 4.2.4 gate: recount every answer key straight from its CSV, independently.

The generators count quirks while they create them; this script never imports them.
It re-derives each count from the finished file with its own patterns, so a bug in a
generator's bookkeeping shows up as a mismatch. It also checks each generator is
deterministic (same seed -> byte-identical CSV and key).

Usage (from the etl-copilot folder):   python tests/verify_answer_keys.py
Offline: no AWS, no cost.
"""
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
NULLS = {"", "n/a", "null", "-"}
results = []


def check(name, actual, expected):
    ok = actual == expected
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      actual:   {actual}\n      expected: {expected}"))


def load(name):
    rows = list(csv.DictReader(open(DATA / f"{name}.csv", encoding="utf-8", newline="")))
    key = json.loads((DATA / f"{name}.expected.json").read_text(encoding="utf-8"))
    unique, seen = [], set()
    for r in rows:  # generators count per unique row (re-sent duplicates excluded)
        t = tuple(r.values())
        if t not in seen:
            seen.add(t)
            unique.append(r)
    return rows, unique, key


def null_tokens(rows, col, extra_label=None):
    c = Counter()
    for r in rows:
        if r[col].strip().lower() in NULLS:
            c[r[col] or "<empty>"] += 1
    return dict(c)


# ---------------------------------------------------------------- orders
print("=== orders_export ===")
rows, uniq, key = load("orders_export")
check("rows in file", len(rows), key["rows_in_file"])
check("exact duplicate rows", len(rows) - len(uniq), key["exact_duplicate_rows"])
check("unique order ids", len({r["order_id"] for r in rows}), key["unique_orders"])
check("unparseable unit_price", sum(r["unit_price"].strip().lower() in NULLS for r in uniq),
      key["expected_clean_output"]["rows_with_unparseable_unit_price"])
check("malformed emails", sum(r["customer_email"].strip().endswith("@") for r in uniq), key["counts"]["email_malformed"])
check("country spellings", sorted({r["ship_country"] for r in rows}), sorted(key["country_raw_to_iso"]))
check("status spellings", sorted({r["order_status"] for r in rows}), sorted(key["status_raw_to_canonical"]))

# ---------------------------------------------------------------- inventory_snapshot
print("=== inventory_snapshot ===")
rows, uniq, key = load("inventory_snapshot")
exp = key["expected_clean_output"]
check("rows in file", len(rows), key["rows_in_file"])
check("exact duplicate rows", len(rows) - len(uniq), key["exact_duplicate_rows"])
check("key (sku, warehouse) unique after dedup", len({(r["sku"], r["warehouse"]) for r in uniq}), len(uniq))
check("rows after dedup", len(uniq), exp["rows_after_dedup"])
check("distinct warehouses", len({r["warehouse"] for r in uniq}), exp["distinct_warehouses"])
check("distinct skus", len({r["sku"] for r in uniq}), exp["distinct_skus"])
check("on_hand null spellings", null_tokens(uniq, "on_hand"), key["null_tokens"]["on_hand"])
check("reserved null spellings", null_tokens(uniq, "reserved"), key["null_tokens"]["reserved"])
check("weight null spellings", null_tokens(uniq, "weight"), key["null_tokens"]["weight"])
check("on_hand with thousands separator",
      sum(bool(re.fullmatch(r"\d{1,3}(,\d{3})+", r["on_hand"])) for r in uniq),
      key["counts"]["on_hand_with_thousands_separator"])
check("negative on_hand", sum(bool(re.fullmatch(r"-\d+", r["on_hand"])) for r in uniq), exp["negative_on_hand"])
check("null on_hand", sum(r["on_hand"].strip().lower() in NULLS for r in uniq), exp["null_on_hand"])

units, total, null_w = Counter(), Decimal(0), 0
to_kg = {"kg": Decimal(1), "g": Decimal("0.001"), "lb": Decimal("0.45359237"),
         "lbs": Decimal("0.45359237"), "oz": Decimal("0.028349523125")}
for r in uniq:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s?(kg|g|lbs|lb|oz)", r["weight"])
    if m:
        units[m.group(2)] += 1
        total += (Decimal(m.group(1)) * to_kg[m.group(2)]).quantize(Decimal("0.001"), ROUND_HALF_UP)
    elif r["weight"].strip().lower() in NULLS:
        null_w += 1
    else:
        units["UNRECOGNISED:" + r["weight"]] += 1
check("weight units", dict(sorted(units.items())), key["weight_units"])
check("null weight", null_w, exp["null_weight_kg"])
check("sum of weight_kg (3 dp)", str(total), exp["sum_weight_kg"])

fmts = Counter()
for r in uniq:
    v = r["last_counted_at"]
    fmts["ISO-8601 UTC (...Z)" if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", v)
         else "epoch seconds" if re.fullmatch(r"\d{10}", v)
         else "epoch milliseconds" if re.fullmatch(r"\d{13}", v)
         else "DD/MM/YYYY HH:MM (UTC)" if re.fullmatch(r"\d\d/\d\d/\d{4} \d\d:\d\d", v)
         else "UNRECOGNISED:" + v] += 1
check("last_counted_at formats", dict(sorted(fmts.items())), key["counted_at_formats"])

tags = Counter()
for r in uniq:
    v = r["tags"]
    tags["no tags ([])" if v == "[]" else "no tags (empty)" if v == "" else "JSON list" if v.startswith("[")
         else "semicolon list" if ";" in v else "single value"] += 1
check("tags formats", dict(sorted(tags.items())), key["tags_formats"])
check("rows without tags", sum(r["tags"] in ("", "[]") for r in uniq), exp["rows_without_tags"])
truthy = {"y", "yes", "1", "true"}
check("is_active spellings", sorted({r["is_active"] for r in rows}), sorted(key["is_active_raw_to_bool"]))
check("active rows", sum(r["is_active"].lower() in truthy for r in uniq), exp["active_rows"])

# ---------------------------------------------------------------- support_tickets
print("=== support_tickets ===")
rows, uniq, key = load("support_tickets")
exp = key["expected_clean_output"]
check("rows in file", len(rows), key["rows_in_file"])
check("exact duplicate rows", len(rows) - len(uniq), key["exact_duplicate_rows"])
check("ticket_id unique after dedup", len({r["ticket_id"] for r in uniq}), len(uniq))
check("rows after dedup", len(uniq), exp["rows_after_dedup"])

fmts = Counter()
for r in uniq:
    v = r["created_at"]
    fmts["UTC with Z" if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", v)
         else "explicit offset" if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", v)
         else "naive (vendor: UTC)" if re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", v)
         else "UNRECOGNISED:" + v] += 1
check("created_at formats", dict(sorted(fmts.items())), key["created_at_formats"])

refs = Counter()
for r in uniq:
    v = r["order_ref"]
    if v.strip().lower() in NULLS:
        continue
    refs["ORD-N" if re.fullmatch(r"ORD-\d{6}", v) else "ORDN" if re.fullmatch(r"ORD\d{6}", v)
         else "N" if re.fullmatch(r"\d{6}", v) else "#N" if re.fullmatch(r"#\d{6}", v) else "UNRECOGNISED:" + v] += 1
check("order_ref formats", dict(sorted(refs.items())), key["order_ref_formats"])
check("order_ref null spellings", null_tokens(uniq, "order_ref"), key["null_tokens"]["order_ref"])
check("null order_ref", sum(r["order_ref"].strip().lower() in NULLS for r in uniq), exp["null_order_ref"])
check("order refs point at real orders (100001-100500)",
      all(100001 <= int(re.sub(r"\D", "", r["order_ref"])) <= 100500
          for r in uniq if r["order_ref"].strip().lower() not in NULLS), True)

check("upper-case emails", sum(r["customer_email"] == r["customer_email"].upper() for r in uniq),
      key["counts"]["email_uppercase"])
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE = re.compile(r"\+\d[\d ()-]{8,}\d")
check("messages containing an email", sum(bool(EMAIL.search(r["message"])) for r in uniq),
      key["counts"]["messages_with_email"])
check("messages containing a phone", sum(bool(PHONE.search(r["message"])) for r in uniq),
      key["counts"]["messages_with_phone"])
check("messages needing redaction", sum(bool(EMAIL.search(r["message"]) or PHONE.search(r["message"])) for r in uniq),
      exp["messages_needing_redaction"])

for field in ("channel", "priority", "status"):
    check(f"{field} spellings", sorted({r[field] for r in rows}), sorted(key[f"{field}_raw_to_canonical"]))
check("distinct canonical priorities", len(set(key["priority_raw_to_canonical"].values())), exp["distinct_priorities"])
closed = [r for r in uniq if r["status"].lower() in ("closed", "resolved")]
check("closed tickets", len(closed), exp["closed_tickets"])
sat = Counter()
for r in closed:
    v = r["satisfaction"]
    if v.strip().lower() in NULLS:
        continue
    sat["N" if re.fullmatch(r"[1-5]", v) else "N/5" if re.fullmatch(r"[1-5]/5", v)
        else "N stars" if re.fullmatch(r"[1-5] stars", v) else "UNRECOGNISED:" + v] += 1
check("satisfaction formats", dict(sorted(sat.items())), key["satisfaction_formats"])
check("null satisfaction", sum(r["satisfaction"].strip().lower() in NULLS for r in uniq), exp["null_satisfaction"])
check("only closed tickets have resolved_at",
      all((r["resolved_at"].strip().lower() not in NULLS) == (r in closed) for r in uniq), True)

# ---------------------------------------------------------------- determinism
print("=== determinism ===")
# Regenerate into a scratch folder and compare with the committed files, which are never
# touched (they may be open in Excel, which locks them on Windows).
with tempfile.TemporaryDirectory() as scratch:
    for gen, out in (("generate_orders.py", "orders_export"), ("generate_inventory.py", "inventory_snapshot"),
                     ("generate_support_tickets.py", "support_tickets")):
        subprocess.run([sys.executable, str(DATA / gen)], check=True, capture_output=True,
                       env={**os.environ, "PYTHONUTF8": "1", "DATA_OUT_DIR": scratch})
        names = [f"{out}.csv", f"{out}.expected.json"]
        committed = [hashlib.sha256((DATA / n).read_bytes()).hexdigest() for n in names]
        fresh = [hashlib.sha256((Path(scratch) / n).read_bytes()).hexdigest() for n in names]
        check(f"{gen} regenerates byte-identical files", fresh, committed)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
