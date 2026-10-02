# Sample dataset: `orders_export.csv`

A raw weekly orders export from **Trailhead Outfitters**, a fictional outdoor-gear
e-commerce store. It stands in for the kind of vendor CSV a data engineer is handed
and has to clean before it lands in the data lake. All names, emails and phone
numbers are synthetic.

- **508 rows** = 500 unique orders + 8 exact duplicate rows (vendor re-sent them)
- **14 columns**, UTF-8, no BOM, standard CSV quoting
- Orders dated Jan–Jun 2025, ~134 repeat customers, 5 countries

Regenerate (deterministic, seed 42):

```
python data/generate_orders.py
```

## Columns

| Column | Meaning | What's messy about it |
|---|---|---|
| `order_id` | `ORD-1000xx`, primary key | 8 orders appear twice (exact duplicate rows) |
| `order_date` | Order date | 3 formats: `YYYY-MM-DD`, `DD/MM/YYYY`, ISO datetime `…T…Z`; 3 rows are the vendor's `1900-01-01` "unknown" placeholder |
| `customer_id` | `CUST-10xxx` | Clean |
| `customer_name` | **PII** | Non-ASCII names (`José Hernández`, `Zoë Müller`) — must survive intact |
| `customer_email` | **PII** | Some UPPERCASE, some padded with spaces, 3 malformed (`name@`) |
| `phone` | **PII** | 5 formats (`+1 (415) 555-0142`, `415.555.0142`, …) and 4 null spellings |
| `ship_country` | Destination | Same country written many ways: `US`/`USA`/`United States`/`us`/`U.S.` |
| `product_sku` | Product code | Clean |
| `quantity` | Units | Some written `2.0` (spreadsheet float) |
| `unit_price` | Price per unit | Some `$1,149.00`; 26 unparseable (`""`, `N/A`, `null`, `-`) |
| `currency` | ISO currency | Some lowercase (`usd`) |
| `discount_code` | Promo code | Mostly empty **by design** — sparse, not broken |
| `order_status` | Lifecycle state | Case drift + `Cancelled`/`Canceled` spelling drift → 5 real statuses |
| `notes` | Free text | Commas, quotes and accents inside quoted fields |

## Vendor conventions (the context an engineer would know)

- Slash dates are **DD/MM/YYYY** (EU-hosted vendor). `03/04/2025` is 3 April. 50 rows
  are ambiguous this way, so the ETL must use the convention, not guess.
- `1900-01-01` means "unknown date" in the vendor's system — treat as invalid.

## Answer key

`orders_export.expected.json` records every planted quirk and its exact count, plus
the expected cleaned-output facts (500 rows after dedup, 3 invalid dates, 26
unparseable prices, 5 countries, 5 statuses). Phase 1.4's hand-written ETL and
Phase 1.5's Athena rules are checked against these numbers. Per-quirk counts are
per unique order (duplicates excluded).
