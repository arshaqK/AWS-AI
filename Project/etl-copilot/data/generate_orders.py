"""Generate the ETL Copilot sample dataset: a raw weekly orders export from a
(fictional) e-commerce vendor, Trailhead Outfitters.

The file deliberately carries the quirks real vendor CSVs have, and every one is
counted into an answer key (orders_export.expected.json) so the hand-written ETL
(Phase 1.4) and the Athena rules (Phase 1.5) can be checked against known numbers.

Run from anywhere:  python data/generate_orders.py
Output is deterministic for a given SEED.
"""
import csv
import json
import os
import random
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 42
N_ORDERS = 500
N_DUPLICATES = 8  # exact re-sent rows
# DATA_OUT_DIR lets tests regenerate into a scratch folder without touching these files
OUT_DIR = Path(os.environ.get("DATA_OUT_DIR") or Path(__file__).resolve().parent)
CSV_PATH = OUT_DIR / "orders_export.csv"
KEY_PATH = OUT_DIR / "orders_export.expected.json"

rng = random.Random(SEED)

FIRST = ["James", "Maria", "Ahmed", "Chen", "Olivia", "Liam", "Sofia", "Noah", "Aisha", "Lucas",
         "Emma", "Mateo", "Hannah", "Ravi", "Zoë", "José", "Björn", "Chloé", "Fatima", "Ethan"]
LAST = ["Smith", "García", "Khan", "Wang", "Johnson", "Müller", "Rossi", "Brown", "O'Neil", "Nguyen",
        "Patel", "Silva", "Dubois", "Kowalski", "Andersson", "Taylor", "Hernández", "Lee", "Novak", "Ali"]
DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me"]

# (canonical ISO-2, raw variants the vendor actually sends, currency, phone country code)
COUNTRIES = [
    ("US", ["US", "USA", "United States", "us", "U.S."], "USD", "1"),
    ("GB", ["GB", "UK", "United Kingdom", "gb"], "GBP", "44"),
    ("CA", ["CA", "Canada", "canada"], "CAD", "1"),
    ("DE", ["DE", "Germany", "Deutschland"], "EUR", "49"),
    ("FR", ["FR", "France"], "EUR", "33"),
]
COUNTRY_WEIGHTS = [55, 15, 12, 10, 8]

PRODUCTS = [  # sku, name, base price
    ("SKU-TNT-2P", "Ridgeline 2P Tent", 249.00),
    ("SKU-BAG-20F", "Summit 20F Sleeping Bag", 189.50),
    ("SKU-PCK-45L", "Trailblazer 45L Pack", 159.99),
    ("SKU-JKT-RN", "Stormshell Rain Jacket", 129.00),
    ("SKU-BTL-1L", "Insulated Bottle 1L", 34.95),
    ("SKU-STV-MC", "MicroCook Stove", 64.00),
    ("SKU-HLP-400", "Beacon 400 Headlamp", 42.50),
    ("SKU-PLS-CF", "Carbon Trekking Poles", 1149.00),  # >1000 so thousands separators appear
    ("SKU-SCK-MW", "Merino Hiking Socks", 22.00),
    ("SKU-MAT-UL", "UltraLite Sleeping Pad", 99.95),
]

# canonical status -> raw variants
STATUSES = {
    "delivered": ["Delivered", "delivered", "DELIVERED"],
    "shipped": ["Shipped", "shipped", "SHIPPED"],
    "pending": ["Pending", "pending"],
    "cancelled": ["Cancelled", "cancelled", "Canceled", "canceled"],  # US/UK spelling drift
    "returned": ["Returned", "returned"],
}
STATUS_WEIGHTS = [50, 20, 10, 12, 8]

NULL_TOKENS = ["", "N/A", "null", "-"]
NOTES = ["", "", "", "", "", "Leave at back door, please", "Gift wrap - birthday present",
         'Customer said "urgent" on phone', "Deliver after 5pm, ring twice", "Café delivery, ask for Zoë",
         "Replacement for order damaged in transit", "Paid via gift card, balance remaining"]

START = date(2025, 1, 1)
END = date(2025, 6, 30)


def ascii_fold(text: str) -> str:
    """Emails are ASCII in practice: 'Zoë Müller' -> 'zoe.muller'."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def random_date() -> date:
    return START + timedelta(days=rng.randint(0, (END - START).days))


def main() -> None:
    key = {
        "dataset": "orders_export.csv",
        "description": "Raw weekly orders export from Trailhead Outfitters (fictional e-commerce vendor)",
        "seed": SEED,
        "vendor_conventions": {
            "slash_dates": "DD/MM/YYYY (vendor is EU-hosted); ambiguous days like 03/04/2025 mean 3 April",
            "sentinel_dates": "1900-01-01 is the vendor system's placeholder for 'unknown' - treat as invalid",
        },
        "counts": Counter(),
        "date_formats": Counter(),
        "null_tokens": {"phone": Counter(), "unit_price": Counter(), "discount_code": Counter()},
        "country_raw_to_iso": {},
        "status_raw_to_canonical": {},
    }

    customers = []
    for i in range(1, 141):  # ~140 customers, many repeat buyers
        iso, variants, currency, cc = rng.choices(COUNTRIES, weights=COUNTRY_WEIGHTS)[0]
        first, last = rng.choice(FIRST), rng.choice(LAST)
        local = ascii_fold(f"{first}.{last}").lower().replace("'", "").replace(" ", "")
        email = f"{local}{rng.randint(1, 99)}@{rng.choice(DOMAINS)}"  # one email per customer
        customers.append({"id": f"CUST-{10000 + i}", "first": first, "last": last, "email": email,
                          "iso": iso, "variants": variants, "currency": currency, "cc": cc})

    rows = []
    for n in range(N_ORDERS):
        c = rng.choice(customers)
        order_id = f"ORD-{100001 + n}"
        d = random_date()

        # --- order_date: three formats + a few sentinel placeholders ---
        r = rng.random()
        if n in (57, 211, 389):
            order_date = "1900-01-01"
            key["counts"]["sentinel_dates_1900"] += 1
            key["date_formats"]["sentinel 1900-01-01"] += 1
        elif r < 0.60:
            order_date = d.isoformat()
            key["date_formats"]["YYYY-MM-DD"] += 1
        elif r < 0.88:
            order_date = d.strftime("%d/%m/%Y")
            key["date_formats"]["DD/MM/YYYY"] += 1
            if d.day <= 12:
                key["counts"]["ambiguous_slash_dates_day_le_12"] += 1
        else:
            ts = datetime(d.year, d.month, d.day, rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
            order_date = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            key["date_formats"]["ISO-8601 datetime (UTC)"] += 1

        # --- PII: name, email (casing/whitespace noise, a few malformed), phone (many formats) ---
        name = f"{c['first']} {c['last']}"
        email = c["email"]
        er = rng.random()
        if er < 0.08:
            email = email.upper()
            key["counts"]["email_uppercase"] += 1
        elif er < 0.14:
            email = f"  {email} "
            key["counts"]["email_padded_whitespace"] += 1
        if n in (33, 145, 402):
            email = email.strip().split("@")[0] + "@"  # malformed: missing domain
            key["counts"]["email_malformed"] += 1

        num = f"{rng.randint(200, 989)}{rng.randint(200, 999)}{rng.randint(1000, 9999)}"
        pr = rng.random()
        if pr < 0.10:
            phone = rng.choice(NULL_TOKENS)
            key["null_tokens"]["phone"][phone or "<empty>"] += 1
        elif pr < 0.35:
            phone = f"+{c['cc']} ({num[:3]}) {num[3:6]}-{num[6:]}"
        elif pr < 0.60:
            phone = f"{num[:3]}.{num[3:6]}.{num[6:]}"
        elif pr < 0.80:
            phone = num
        else:
            phone = f"+{c['cc']}{num}"

        country = rng.choice(c["variants"])
        key["country_raw_to_iso"][country] = c["iso"]

        # --- product / quantity / price ---
        sku, _pname, base = rng.choice(PRODUCTS)
        qty = rng.choices([1, 2, 3, 4], weights=[70, 20, 7, 3])[0]
        quantity = f"{qty}.0" if rng.random() < 0.06 else str(qty)  # float-ish ints from a spreadsheet
        if quantity.endswith(".0"):
            key["counts"]["quantity_float_formatted"] += 1

        price = base * rng.choice([1.0, 1.0, 1.0, 0.9, 0.85])
        vr = rng.random()
        if vr < 0.05:
            unit_price = rng.choice(NULL_TOKENS)
            key["null_tokens"]["unit_price"][unit_price or "<empty>"] += 1
        elif vr < 0.20:
            unit_price = f"${price:,.2f}"  # currency symbol + thousands separator
            key["counts"]["unit_price_with_symbol_or_comma"] += 1
        else:
            unit_price = f"{price:.2f}"

        currency = c["currency"] if rng.random() > 0.10 else c["currency"].lower()
        if currency.islower():
            key["counts"]["currency_lowercase"] += 1

        # --- sparse legit column: mostly empty is correct, not a defect ---
        if rng.random() < 0.18:
            discount_code = rng.choice(["SPRING10", "WELCOME15", "TRAIL20", "FREESHIP"])
        else:
            discount_code = rng.choice(["", "", "", "N/A", "null"])
            key["null_tokens"]["discount_code"][discount_code or "<empty>"] += 1

        canonical = rng.choices(list(STATUSES), weights=STATUS_WEIGHTS)[0]
        status = rng.choice(STATUSES[canonical])
        key["status_raw_to_canonical"][status] = canonical

        note = rng.choice(NOTES)
        rows.append({
            "order_id": order_id, "order_date": order_date, "customer_id": c["id"],
            "customer_name": name, "customer_email": email, "phone": phone,
            "ship_country": country, "product_sku": sku, "quantity": quantity,
            "unit_price": unit_price, "currency": currency, "discount_code": discount_code,
            "order_status": status, "notes": note,
        })

    # --- vendor re-sent some rows: exact duplicates scattered through the file ---
    dup_ids = []
    for src in rng.sample(rows, N_DUPLICATES):
        rows.insert(rng.randint(0, len(rows)), dict(src))
        dup_ids.append(src["order_id"])

    fields = list(rows[0].keys())
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    key["counts"] = dict(sorted(key["counts"].items()))
    key["date_formats"] = dict(key["date_formats"])
    key["null_tokens"] = {k: dict(v) for k, v in key["null_tokens"].items()}
    key["country_raw_to_iso"] = dict(sorted(key["country_raw_to_iso"].items()))
    key["status_raw_to_canonical"] = dict(sorted(key["status_raw_to_canonical"].items()))
    key["rows_in_file"] = len(rows)
    key["unique_orders"] = N_ORDERS
    key["exact_duplicate_rows"] = N_DUPLICATES
    key["duplicated_order_ids"] = sorted(dup_ids)
    key["columns"] = fields
    key["pii_columns"] = ["customer_name", "customer_email", "phone"]
    key["expected_clean_output"] = {
        "rows_after_dedup": N_ORDERS,
        "rows_with_invalid_order_date": key["counts"]["sentinel_dates_1900"],
        "rows_with_unparseable_unit_price": sum(key["null_tokens"]["unit_price"].values()),
        "distinct_ship_country_after_normalizing": len({iso for iso, *_ in COUNTRIES}),
        "distinct_order_status_after_normalizing": len(STATUSES),
    }
    KEY_PATH.write_text(json.dumps(key, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {CSV_PATH.name}: {len(rows)} rows ({N_ORDERS} unique + {N_DUPLICATES} duplicates)")
    print(f"wrote {KEY_PATH.name}")


if __name__ == "__main__":
    main()
