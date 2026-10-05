# Sample datasets

Three raw exports from **Trailhead Outfitters**, a fictional outdoor-gear store, each
from a different system and each messy in its own way. All names, emails and phone
numbers are synthetic. Every file has a generator (deterministic) and an answer key
(`*.expected.json`) counting every planted quirk; `tests/verify_answer_keys.py`
recounts all three keys independently from the CSVs.

| Dataset (landing folder) | Rows | Key | Generator |
|---|---|---|---|
| `orders` | 508 = 500 + 8 duplicates | `order_id` | `generate_orders.py` (seed 42) |
| `inventory_snapshot` | 243 = 239 + 4 duplicates | `sku` + `warehouse` | `generate_inventory.py` (seed 7) |
| `support_tickets` | 405 = 400 + 5 duplicates | `ticket_id` | `generate_support_tickets.py` (seed 11) |

> **Open CSVs in a text editor, not Excel.** Saving from Excel silently rewrites them
> (dates become M/D/YYYY, long phone numbers become `4.47356E+11`). Regenerate a damaged
> file with its generator.

---

# Dataset 1: `orders_export.csv`

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

---

# Dataset 2: `inventory_snapshot.csv`

End-of-month (30 June 2025) stock snapshot from the warehouse system: one row per product
per warehouse, 73 products (including the 10 sold in the orders export, so the datasets
join on `sku`) across 5 warehouses. No personal data.

| Column | Meaning | What's messy about it |
|---|---|---|
| `snapshot_date` | Snapshot day | Clean (`2025-06-30`) |
| `warehouse` | Warehouse code, part of the key | Clean (`SEA1`, `RNO2`, `DAL1`, `CVG1`, `TOR1`) |
| `sku` | Product code, part of the key | Clean |
| `product_name` | Product name | Clean |
| `on_hand` | Units in stock | `N/A` / `-` / `null` / empty; thousands separators (`1,204`); **negatives are valid backorders** (`-4`) |
| `reserved` | Units held for open orders | `N/A` / `-` / empty |
| `weight` | Unit weight | **Mixed units**: `2.35kg`, `1100 g`, `2.43lb`, `3.1 lbs`, `14.8 oz`; some empty |
| `last_counted_at` | Last physical count | **4 layouts**: ISO `…Z`, Unix epoch seconds, epoch **milliseconds**, `DD/MM/YYYY HH:MM` |
| `tags` | Product tags | **Sometimes a JSON list** (`["tent", "2p"]`), **sometimes one value** (`tent`), sometimes `tent;2p`, sometimes `[]` or empty |
| `is_active` | Still sold? | `Y`/`N`, `yes`/`no`, `1`/`0`, `true`/`false`, `TRUE`/`FALSE`, `Yes`/`No` |

Vendor conventions: all timestamps are UTC; numeric timestamps are epoch seconds or
milliseconds; a negative `on_hand` is a backorder; 1 lb = 0.45359237 kg, 1 oz = 0.028349523125 kg.

---

# Dataset 3: `support_tickets.csv`

Helpdesk export, April-June 2025, one row per ticket, 180 customers. Personal data sits
in a column **and inside free text**.

| Column | Meaning | What's messy about it |
|---|---|---|
| `ticket_id` | Ticket id, the key | Clean (`TCK-20001`) |
| `created_at` | When the ticket was opened | **3 time-zone styles**: `…Z`, explicit offsets (`+05:00`, `-04:00`, `+05:30`), and **naive** times with no zone |
| `order_ref` | The order the ticket is about | **4 formats**: `ORD-100123`, `ORD100123`, `100123`, `#100123`; 79 tickets have none (`N/A`, `-`, empty). Every number is a real order id |
| `customer_email` | **PII** | Some UPPER-CASE |
| `channel` | How the customer got in touch | 12 spellings of 4 values (`Live Chat`, `web form`, `EMAIL`, `Phone call`...) |
| `priority` | Urgency | `P1`-`P4` mixed with `Urgent`/`High`/`Medium`/`Low`/`normal` |
| `status` | State | `open`/`pending`/`closed`, with `resolved` meaning closed, in mixed case |
| `subject` | Ticket subject | Clean |
| `message` | Customer's message | **PII inside the text**: 61 messages contain an email and 43 a phone number (and some start with a first name); commas, quotes and accents |
| `agent` | Support agent (staff, not a customer) | Clean |
| `resolved_at` | When it was closed | Only for closed tickets; open ones are empty or `-` |
| `satisfaction` | Score 1-5, closed tickets only | `4`, `4/5`, `4 stars`, `N/A`, empty |

Vendor conventions: timestamps without a zone are UTC; P1 = urgent, P2 = high,
P3 = normal, P4 = low, and "Medium" means normal; "resolved" means closed; the canonical
order reference is `ORD-<number>`.
