"""Step 3.2 gate: run a draft's clean() on the local sample CSV - no Glue job, no cost.

Checks the output against the answer key, then value-by-value against the golden
script (etl/etl-copilot-job.py). Only clean() runs; main() and its S3 I/O never do.

Usage (from app/EtlCopilot/):
    uv run --with "pandas<3" python ../../tests/dry_run_draft.py <draft_id | path/to/script.py>

pandas must be < 3, like Glue's Python 3.9 analytics libraries: pandas 3 stores missing
values in text columns as NaN instead of None, which changes how clean() behaves.

Note: this executes LLM-written code on your machine. Skim the draft first.
"""
import json
import os
import re
import sys
import types
from pathlib import Path

import pandas as pd

if int(pd.__version__.split(".")[0]) >= 3:
    sys.exit(f'pandas {pd.__version__} found; Glue runs pandas < 3. Use: uv run --with "pandas<3" ...')

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(os.getenv("ETL_COPILOT_APP", ROOT / "app" / "EtlCopilot"))
sys.path.insert(0, str(APP_DIR))

SPEC = json.loads((APP_DIR / 'datasets' / 'orders.json').read_text(encoding='utf-8'))  # orders contract
EXPECTED_COLUMNS = [(c['name'], c['type']) for c in SPEC['columns']]
ALLOWED_STATUSES = next(r['values'] for r in SPEC['rules'] if r['id'] == 'R5_statuses')

CSV_PATH = ROOT / "data" / "orders_export.csv"
KEY = json.loads((ROOT / "data" / "orders_export.expected.json").read_text(encoding="utf-8"))
GOLDEN_PATH = ROOT / "etl" / "etl-copilot-job.py"
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Columns whose values must equal the golden output exactly. The hashes are reported but
# not required to match: a different (still valid) phone normalisation changes the hash.
MUST_MATCH_GOLDEN = ["order_date", "customer_id", "email_is_valid", "ship_country", "product_sku",
                     "quantity", "unit_price", "currency", "discount_code", "order_status",
                     "notes", "order_month"]


def load_module(name: str, source: str) -> types.ModuleType:
    """Execute a script as a module without running main(). awswrangler is stubbed out."""
    sys.modules.setdefault("awswrangler", types.ModuleType("awswrangler"))
    module = types.ModuleType(name)
    exec(compile(source, name, "exec"), module.__dict__)
    return module


def read_raw() -> pd.DataFrame:
    # exactly how main() reads it on Glue: everything as text, nothing auto-converted to NaN
    return pd.read_csv(CSV_PATH, dtype=str, keep_default_na=False, na_filter=False, encoding="utf-8")


def _norm(value):
    """Compare values by meaning, not by Python type (date vs Timestamp, Decimal vs float)."""
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        value = value.date()
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def dry_run(source: str, label: str = "draft") -> bool:
    results = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        results.append(passed)
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))

    print(f"--- dry run: {label} ---")
    try:
        out = load_module(label, source).clean(read_raw())
    except Exception as e:
        check("clean() runs", False, f"{type(e).__name__}: {e}")
        return False
    check("clean() runs", True, f"{len(out)} rows")

    expected_cols = [n for n, _ in EXPECTED_COLUMNS] + ["order_month"]
    check("columns match the output contract", list(out.columns) == expected_cols,
          "" if list(out.columns) == expected_cols else f"got {list(out.columns)}")
    exp = KEY["expected_clean_output"]
    check("rows", len(out) == exp["rows_after_dedup"], f"{len(out)} vs {exp['rows_after_dedup']}")
    check("no duplicate order_id", not out["order_id"].duplicated().any())
    check("null order_date", int(out["order_date"].isna().sum()) == exp["rows_with_invalid_order_date"],
          f"{int(out['order_date'].isna().sum())} vs {exp['rows_with_invalid_order_date']}")
    check("null unit_price", int(out["unit_price"].isna().sum()) == exp["rows_with_unparseable_unit_price"],
          f"{int(out['unit_price'].isna().sum())} vs {exp['rows_with_unparseable_unit_price']}")
    invalid = int((out["email_is_valid"] == False).sum())  # noqa: E712
    check("invalid emails", invalid == KEY["counts"]["email_malformed"],
          f"{invalid} vs {KEY['counts']['email_malformed']}")
    countries = set(out["ship_country"].dropna())
    check("countries are 5 ISO-2 codes",
          len(countries) == exp["distinct_ship_country_after_normalizing"]
          and all(re.fullmatch(r"[A-Z]{2}", c) for c in countries), str(sorted(countries)))
    statuses = set(out["order_status"].dropna())
    check("statuses are the 5 canonical values", statuses == set(ALLOWED_STATUSES), str(sorted(statuses)))
    for col in ("email_sha256", "phone_sha256"):
        bad = [v for v in out[col].dropna() if not HEX64.match(str(v))]
        check(f"{col} is SHA-256 hex", not bad, f"{len(bad)} bad")

    golden = load_module("golden", GOLDEN_PATH.read_text(encoding="utf-8")).clean(read_raw())
    d = out.set_index("order_id")
    g = golden.set_index("order_id")
    common = d.index.intersection(g.index)
    for col in MUST_MATCH_GOLDEN + ["email_sha256", "phone_sha256"]:
        if col not in d.columns:
            continue
        diffs = [oid for oid in common if _norm(d.at[oid, col]) != _norm(g.at[oid, col])]
        example = (f"; e.g. {diffs[0]}: draft={_norm(d.at[diffs[0], col])!r} "
                   f"golden={_norm(g.at[diffs[0], col])!r}") if diffs else ""
        if col in MUST_MATCH_GOLDEN:
            check(f"same {col} as golden", not diffs, f"{len(diffs)} differ{example}")
        else:
            print(f"INFO  same {col} as golden: {len(common) - len(diffs)}/{len(common)}{example}")

    print(f"{sum(results)}/{len(results)} checks passed")
    return all(results)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    arg = sys.argv[1]
    if Path(arg).is_file():
        src, name = Path(arg).read_text(encoding="utf-8"), Path(arg).name
    else:
        from tools.drafts import read_draft
        src, name = read_draft(arg), f"draft {arg}"
    sys.exit(0 if dry_run(src, name) else 1)
