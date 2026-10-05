"""Scripted content for the static mock: the real orders onboarding of 2026-10-02.

Figures come from data/orders_export.expected.json, the approved spec 5a69b40141ba,
draft 79a94743fde0 and its quality report (7/7). Nothing here calls AWS.
"""
import json
from pathlib import Path

SPEC_FILE = Path(__file__).resolve().parents[1] / "app" / "EtlCopilot" / "datasets" / "orders.json"
SPEC_ID = "5a69b40141ba"
DRAFT_ID = "79a94743fde0"
RUN_ID = "jr_ceaabb0c526d…"
REMEMBERED = "order_date 1900-01-01 is a placeholder for an unknown date; set exactly that value to null"

PROFILE = [
    "**508 rows, 14 columns** · 500 distinct `order_id` → 8 rows are re-sent duplicates",
    "`order_date` mixes 3 formats: `YYYY-MM-DD` (302), `DD/MM/YYYY` (129), ISO datetime (66), plus **3 × `1900-01-01`**",
    "`customer_name`, `customer_email`, `phone`: **personal data**",
    "`ship_country`: 17 spellings of 5 countries (`USA`, `U.S.`, `Deutschland`, `uk`…)",
    "`unit_price`: 79 values with `$`, `€` or commas · 26 blank or `N/A`",
    "null spellings in use: empty, `null`, `N/A`, `-`",
]

SPEC_KNOWN = ("`orders` already has an approved contract (spec `5a69b40141ba`): key `order_id`, "
              "14 output columns + `order_month` partition, 7 rules. **Reusing it** — no contract approval needed.")

SPEC_PROPOSAL = [
    "Key: `order_id` — one row per order, re-sent rows dropped",
    "Output: 14 typed columns + partition `order_month` (from `order_date`)",
    "Personal data: `customer_email`, `phone` → SHA-256; `customer_name` dropped",
    "Rules: valid dates (2000-01-01..today), 5 statuses, 2-letter country codes, PII hashed",
    "Reported, not failures: null dates, null prices, invalid emails",
]

PLAN = [
    "Parse `order_date` from all three formats; slash dates are DD/MM; `1900-01-01` → null (from memory)",
    "Drop the 8 re-sent duplicate rows, one row per `order_id`",
    "`ship_country` → ISO codes (US, CA, GB, DE, FR); `order_status` → 5 lower-case values",
    "`unit_price` → decimal (strip `$`, `€`, commas); blank or `N/A` → null",
    "Hash `customer_email` and `phone` (SHA-256); drop `customer_name`",
    "Write Parquet to `staging/orders/`, partitioned by `order_month`",
]

NEEDS_CONFIRMATION = ["`phone`: numbers without a leading `+` are taken as local to `ship_country`"]

GUARDRAILS = ["writes only to `staging/orders/`", "`landing/` untouched", "500 rows in → 500 rows out"]

SCRIPT = '''def _parse_date(raw: str) -> Optional[date]:
    """
    Parse the three date formats observed:
      - YYYY-MM-DD
      - DD/MM/YYYY  (first token goes up to 24, proving DD/MM)
      - YYYY-MM-DDTHH:MM:SSZ  (ISO 8601 UTC)
    Return None for the placeholder 1900-01-01.
    """
    s = raw.strip()
    if not s or _null_like(s):
        return None
    if "T" in s:
        parsed = datetime.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S").date()
    elif "/" in s:
        parsed = datetime.strptime(s, "%d/%m/%Y").date()
    else:
        parsed = datetime.strptime(s, "%Y-%m-%d").date()

    # Engineer-confirmed: 1900-01-01 is a placeholder for unknown date
    if parsed == date(1900, 1, 1):
        return None
    return parsed

# … 240 more lines: countries, statuses, prices, PII hashing, Parquet write'''

RULES = """| Rule | Result | Detail |
|---|---|---|
| R1 schema | ✅ passed | matches the output contract |
| R2 no rows lost | ✅ passed | 500 clean rows vs 500 distinct raw keys |
| R3 unique key | ✅ passed | 0 duplicate, 0 null `order_id` |
| R4 valid dates | ✅ passed | 0 dates outside 2000-01-01..today |
| R5 statuses | ✅ passed | all in the 5 allowed values |
| R6 countries | ✅ passed | all 2-letter ISO codes |
| R7 PII hashed | ✅ passed | every hash is 64-char SHA-256 |"""

COUNTS = "Reported: 3 null `order_date` (the `1900-01-01` rows) · 26 null `unit_price` · 3 invalid emails"

DOCS = [
    "Data dictionary → `s3://etl-copilot-ak/reports/orders/data_dictionary.md`",
    "Quality report → `s3://etl-copilot-ak/reports/orders/quality_report.md`",
    "Personal-data check passed before saving",
]

FINAL = [
    "**500 clean rows** in `etl_copilot_clean.orders` (Parquet at `staging/orders/`)",
    "Quality: **7/7 rules passed**",
    "Run log: attempt 1 of this cycle — **passed first time**",
]

SUGGESTIONS = ["keep customer_name, it's a company name", "use DD/MM for slash dates",
               "treat N/A in discount_code as no discount"]

# What a note changes in the mock: (plan line removed, line added, acknowledgement). None = already true.
NOTE_EFFECTS = {
    "customer_name": ("Hash `customer_email` and `phone` (SHA-256); drop `customer_name`",
                      "Hash `customer_email` and `phone` (SHA-256); keep `customer_name` as-is (your note: company name)",
                      "Got it — `customer_name` is a company name, not personal data. Keeping it."),
    "dd/mm": None,
    "discount": ("`unit_price` → decimal (strip `$`, `€`, commas); blank or `N/A` → null",
                 "`unit_price` → decimal (strip `$`, `€`, commas); blank or `N/A` → null; `discount_code` `N/A` → no discount",
                 "Got it — `N/A` in `discount_code` means no discount."),
}


def spec_json() -> str:
    try:
        return json.dumps(json.loads(SPEC_FILE.read_text()), indent=2)[:2500] + "\n…"
    except OSError:
        return "{ … }"
