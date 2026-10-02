"""ETL Copilot - golden ETL for the Trailhead Outfitters orders export (Phase 1.4).

Runs as an AWS Glue Python Shell job (Python 3.9, "Load common analytics libraries"
enabled, so pandas + awswrangler are preinstalled).

Reads   s3://<bucket>/landing/orders/*.csv          (raw vendor export, all text)
Writes  s3://<bucket>/staging/orders/order_month=...  (partitioned Parquet)
and registers the table etl_copilot_clean.orders in the Glue Data Catalog.

Every rule here maps to a quirk in data/README.md. The job fails loudly on anything it
does not recognise (an unknown date format, country or status) instead of guessing.

Optional job parameters (defaults below):
  --source_path --target_path --database --table --pii_salt
"""
import hashlib
import re
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional

import pandas as pd

DEFAULTS = {
    "source_path": "s3://etl-copilot-ak/landing/orders/",
    "target_path": "s3://etl-copilot-ak/staging/orders/",
    "database": "etl_copilot_clean",
    "table": "orders",
    # Salt for the PII hashes. Unsalted SHA-256 of an email can be reversed with a
    # dictionary of known emails; set a secret value here in real use.
    "pii_salt": "",
}

NULL_TOKENS = {"", "n/a", "null", "-"}
SENTINEL_DATE = "1900-01-01"  # vendor's placeholder for "unknown"

COUNTRY_TO_ISO = {
    "us": "US", "usa": "US", "united states": "US", "u.s.": "US",
    "gb": "GB", "uk": "GB", "united kingdom": "GB",
    "ca": "CA", "canada": "CA",
    "de": "DE", "germany": "DE", "deutschland": "DE",
    "fr": "FR", "france": "FR",
}
CALLING_CODE = {"US": "1", "CA": "1", "GB": "44", "DE": "49", "FR": "33"}

STATUS_CANONICAL = {
    "delivered": "delivered", "shipped": "shipped", "pending": "pending",
    "cancelled": "cancelled", "canceled": "cancelled", "returned": "returned",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SLASH_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def get_args(argv) -> Dict[str, str]:
    """Read '--name value' job parameters; Glue also passes its own, which are ignored."""
    args = dict(DEFAULTS)
    for i, token in enumerate(argv[:-1]):
        name = token[2:] if token.startswith("--") else None
        if name in args:
            args[name] = argv[i + 1]
    return args


def to_null(value: Optional[str]) -> Optional[str]:
    """Trim, and turn every spelling of 'empty' into a real null."""
    if value is None:
        return None
    value = value.strip()
    return None if value.lower() in NULL_TOKENS else value


def parse_order_date(raw: Optional[str]) -> Optional[date]:
    if raw is None or raw == SENTINEL_DATE:
        return None
    if ISO_DATE_RE.match(raw):
        return datetime.strptime(raw, "%Y-%m-%d").date()
    if SLASH_DATE_RE.match(raw):  # vendor convention: DD/MM/YYYY, never MM/DD
        return datetime.strptime(raw, "%d/%m/%Y").date()
    if ISO_DATETIME_RE.match(raw):
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").date()
    raise ValueError("unrecognised order_date format: %r" % raw)


def parse_price(raw: Optional[str]) -> Optional[Decimal]:
    if raw is None:
        return None
    try:
        return Decimal(raw.replace("$", "").replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError("unparseable unit_price: %r" % raw)


def parse_quantity(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    number = float(raw)  # spreadsheet exports write 2 as "2.0"
    if not number.is_integer():
        raise ValueError("fractional quantity: %r" % raw)
    return int(number)


def to_iso_country(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    try:
        return COUNTRY_TO_ISO[raw.lower()]
    except KeyError:
        raise ValueError("unknown ship_country: %r" % raw)


def to_status(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    try:
        return STATUS_CANONICAL[raw.lower()]
    except KeyError:
        raise ValueError("unknown order_status: %r" % raw)


def normalize_phone(raw: Optional[str], iso_country: Optional[str]) -> Optional[str]:
    """Reduce every phone format to E.164 (+<country><number>) so equal numbers hash equally."""
    if raw is None:
        return None
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "+" + digits
    if iso_country in CALLING_CODE:
        return "+" + CALLING_CODE[iso_country] + digits
    raise ValueError("cannot place phone without country: %r" % raw)


def sha256(value: Optional[str], salt: str) -> Optional[str]:
    if value is None:
        return None
    return hashlib.sha256((salt + value).encode("utf-8")).hexdigest()


def clean(raw: pd.DataFrame, pii_salt: str = "") -> pd.DataFrame:
    """Turn the raw all-text export into a typed, de-duplicated, PII-safe table."""
    rows_in = len(raw)
    df = raw.drop_duplicates().reset_index(drop=True)
    if df["order_id"].duplicated().any():
        # Same order_id with different content = a conflicting update, not a re-send.
        clash = sorted(df.loc[df["order_id"].duplicated(keep=False), "order_id"].unique())
        raise ValueError("order_id appears with conflicting values: %s" % clash[:10])

    for col in df.columns:
        df[col] = df[col].map(to_null)

    out = pd.DataFrame()
    out["order_id"] = df["order_id"]
    out["order_date"] = df["order_date"].map(parse_order_date)
    out["customer_id"] = df["customer_id"]

    email = df["customer_email"].map(lambda e: e.lower() if e is not None else None)
    out["email_is_valid"] = email.map(lambda e: bool(e and EMAIL_RE.match(e)))
    out["email_sha256"] = email.map(lambda e: sha256(e, pii_salt))

    out["ship_country"] = df["ship_country"].map(to_iso_country)
    phones = [normalize_phone(p, c) for p, c in zip(df["phone"], out["ship_country"])]
    out["phone_sha256"] = [sha256(p, pii_salt) for p in phones]
    # customer_name is dropped: customer_id already identifies the buyer.

    out["product_sku"] = df["product_sku"]
    out["quantity"] = pd.array(df["quantity"].map(parse_quantity), dtype="Int64")
    out["unit_price"] = df["unit_price"].map(parse_price)
    out["currency"] = df["currency"].map(lambda c: c.upper() if c is not None else None)
    out["discount_code"] = df["discount_code"].map(lambda c: c.upper() if c is not None else None)
    out["order_status"] = df["order_status"].map(to_status)
    out["notes"] = df["notes"]
    out["order_month"] = out["order_date"].map(lambda d: d.strftime("%Y-%m") if d else "unknown")

    print("rows in: %d | exact duplicates removed: %d | rows out: %d"
          % (rows_in, rows_in - len(df), len(out)))
    print("invalid/unknown order_date: %d" % out["order_date"].isna().sum())
    print("unit_price null: %d" % out["unit_price"].isna().sum())
    print("invalid emails: %d" % (~out["email_is_valid"]).sum())
    print("countries: %s" % sorted(out["ship_country"].dropna().unique()))
    print("statuses: %s" % sorted(out["order_status"].dropna().unique()))
    return out


def main() -> None:
    import awswrangler as wr  # preinstalled on Glue; only needed for S3 I/O

    args = get_args(sys.argv)
    print("args: %s" % {k: v for k, v in args.items() if k != "pii_salt"})

    raw = wr.s3.read_csv(args["source_path"], dtype=str, keep_default_na=False,
                         na_filter=False, encoding="utf-8")
    cleaned = clean(raw, args["pii_salt"])

    wr.s3.to_parquet(
        df=cleaned,
        path=args["target_path"],
        dataset=True,
        mode="overwrite",  # re-runs replace the output instead of appending to it
        partition_cols=["order_month"],
        database=args["database"],
        table=args["table"],
        dtype={"order_date": "date", "unit_price": "decimal(10,2)", "quantity": "int"},
    )
    print("wrote %d rows to %s and table %s.%s"
          % (len(cleaned), args["target_path"], args["database"], args["table"]))


if __name__ == "__main__":
    main()
