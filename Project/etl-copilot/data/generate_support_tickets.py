"""Generate dataset 3: a raw support-ticket export from Trailhead Outfitters' helpdesk tool
(fictional), one row per ticket.

Every quirk is counted into an answer key (support_tickets.expected.json).

New kinds of mess compared to orders and inventory:
  - timestamps in three time-zone styles: UTC "Z", explicit offsets, and naive (no zone)
  - order references in four formats (ORD-100123, ORD100123, 100123, #100123), or none
  - labels whose spellings drift (channel, priority P1..P4 vs High/Medium/Low, status)
  - satisfaction scores written "4", "4/5" or "4 stars"
  - PERSONAL DATA INSIDE FREE TEXT: customers paste emails and phone numbers into the
    message body, so the text must be redacted, not just a column dropped

Run from anywhere:  python data/generate_support_tickets.py
Output is deterministic for a given SEED.
"""
import csv
import json
import os
import random
import unicodedata
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

SEED = 11
N_TICKETS = 400
N_DUPLICATES = 5
# DATA_OUT_DIR lets tests regenerate into a scratch folder without touching these files
OUT_DIR = Path(os.environ.get("DATA_OUT_DIR") or Path(__file__).resolve().parent)
CSV_PATH = OUT_DIR / "support_tickets.csv"
KEY_PATH = OUT_DIR / "support_tickets.expected.json"

rng = random.Random(SEED)

FIRST = ["James", "Maria", "Ahmed", "Chen", "Olivia", "Liam", "Sofia", "Noah", "Aisha", "Lucas",
         "Emma", "Mateo", "Hannah", "Ravi", "Zoë", "José", "Björn", "Chloé", "Fatima", "Ethan"]
LAST = ["Smith", "García", "Khan", "Wang", "Johnson", "Müller", "Rossi", "Brown", "O'Neil", "Nguyen",
        "Patel", "Silva", "Dubois", "Kowalski", "Andersson", "Taylor", "Hernández", "Lee", "Novak", "Ali"]
DOMAINS = ["gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me"]
AGENTS = ["Priya", "Marcus", "Elena", "Tomás", "Grace", "Kenji"]  # support staff (not customers)

CHANNELS = {"email": ["email", "Email", "EMAIL"], "web": ["web form", "Web", "WEB"],
            "chat": ["chat", "Live Chat", "Chat"], "phone": ["phone", "Phone call", "Phone"]}
# Vendor convention: P1=urgent, P2=high, P3=normal, P4=low; "Medium" means normal.
PRIORITIES = {"urgent": ["P1", "Urgent", "urgent"], "high": ["P2", "High", "HIGH"],
              "normal": ["P3", "Medium", "normal", "Normal"], "low": ["P4", "Low", "low"]}
# Vendor convention: "resolved" is the same as closed.
STATUSES = {"open": ["open", "Open", "OPEN"], "pending": ["pending", "Pending"],
            "closed": ["closed", "Closed", "resolved", "Resolved", "RESOLVED"]}
OFFSETS = [timedelta(hours=5), timedelta(hours=-4), timedelta(hours=1), timedelta(hours=5, minutes=30)]

SUBJECTS = ["Where is my order?", "Damaged item on arrival", "Wrong size delivered",
            "Refund not received", "Discount code not working", "Change delivery address",
            "Question about tent setup", "Missing item in package", "Cancel my order", "Warranty claim"]
MESSAGES = [
    "Hi, my order {ref} hasn't arrived yet. Could you check the status?",
    "The {item} arrived with a broken zip, can I get a replacement?",
    "I ordered a medium but got a large, how do I swap it?",
    "Still waiting on the refund for {ref}, it's been two weeks.",
    "The code \"TRAIL20\" says it's expired but your email said it runs until July.",
    "Please send {ref} to my work address instead, thanks!",
    "How do I pitch the {item} in wind? The guide isn't clear.",
    "The box for {ref} was missing the {item}.",
    "Please cancel {ref}, I ordered twice by mistake.",
    "My {item} stopped working after a month - is that covered?",
]
ITEMS = ["tent", "sleeping bag", "headlamp", "stove", "rain jacket", "trekking poles"]


def ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def render_created(ts: datetime, key: dict) -> str:
    r = rng.random()
    if r < 0.5:
        key["created_at_formats"]["UTC with Z"] += 1
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    if r < 0.8:
        key["created_at_formats"]["explicit offset"] += 1
        off = rng.choice(OFFSETS)
        local = (ts + off).replace(tzinfo=timezone(off))
        return local.isoformat()
    key["created_at_formats"]["naive (vendor: UTC)"] += 1
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def render_ref(n: int, key: dict) -> str:
    r = rng.random()
    fmt = "ORD-{n}" if r < 0.5 else "ORD{n}" if r < 0.7 else "{n}" if r < 0.9 else "#{n}"
    key["order_ref_formats"][fmt.replace("{n}", "N")] += 1
    return fmt.format(n=n)


def main() -> None:
    key = {
        "dataset": "support_tickets.csv",
        "description": "Support-ticket export from Trailhead Outfitters' helpdesk tool (fictional)",
        "seed": SEED,
        "primary_key": ["ticket_id"],
        "vendor_conventions": {
            "naive_timestamps": "timestamps without a zone are UTC",
            "priority": "P1=urgent, P2=high, P3=normal, P4=low; 'Medium' means normal",
            "status": "'resolved' is the same as closed",
            "order_ref": "the number after ORD / # is the order id; canonical form is ORD-<number>",
        },
        "counts": Counter(),
        "created_at_formats": Counter(),
        "order_ref_formats": Counter(),
        "null_tokens": {"order_ref": Counter(), "satisfaction": Counter(), "resolved_at": Counter()},
        "satisfaction_formats": Counter(),
        "channel_raw_to_canonical": {},
        "priority_raw_to_canonical": {},
        "status_raw_to_canonical": {},
    }

    customers = []
    for i in range(1, 181):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        email = f"{ascii_fold(first).lower()}.{ascii_fold(last).lower().replace(chr(39), '')}{rng.randint(1, 99)}@{rng.choice(DOMAINS)}"
        customers.append({"name": f"{first} {last}", "email": email,
                          "phone": f"+1 ({rng.randint(200, 989)}) {rng.randint(200, 999)}-{rng.randint(1000, 9999)}"})

    start = datetime(2025, 4, 1, tzinfo=timezone.utc)
    rows, truth = [], []
    for n in range(N_TICKETS):
        c = rng.choice(customers)
        created = start + timedelta(minutes=rng.randint(0, 91 * 24 * 60))
        ticket = {"ticket_id": f"TCK-{20001 + n}", "created_at": render_created(created, key)}

        if rng.random() < 0.15:
            token = rng.choice(["", "", "N/A", "-"])
            key["null_tokens"]["order_ref"][token or "<empty>"] += 1
            ticket["order_ref"], order_no = token, None
        else:
            order_no = rng.randint(100001, 100500)  # orders that exist in the orders export
            ticket["order_ref"] = render_ref(order_no, key)

        email = c["email"]
        if rng.random() < 0.08:
            email = email.upper()
            key["counts"]["email_uppercase"] += 1
        ticket["customer_email"] = email

        for field, table in (("channel", CHANNELS), ("priority", PRIORITIES), ("status", STATUSES)):
            canonical = rng.choice(list(table))
            raw = rng.choice(table[canonical])
            key[f"{field}_raw_to_canonical"][raw] = canonical
            ticket[field] = raw
        status = key["status_raw_to_canonical"][ticket["status"]]

        topic = rng.randrange(len(SUBJECTS))  # SUBJECTS[i] and MESSAGES[i] describe the same issue
        ticket["subject"] = SUBJECTS[topic]
        ref_text = ticket["order_ref"] if order_no is not None else "my last order"
        item = "tent" if "pitch" in MESSAGES[topic] else rng.choice(ITEMS)
        message = MESSAGES[topic].format(ref=ref_text, item=item)
        has_email = rng.random() < 0.20
        has_phone = rng.random() < 0.12
        if has_email:
            message += f" You can reach me at {c['email']}."
            key["counts"]["messages_with_email"] += 1
        if has_phone:
            message += f" Or call {c['phone']}."
            key["counts"]["messages_with_phone"] += 1
        if rng.random() < 0.15:
            message = f"{c['name'].split()[0]} here. " + message  # first names in text, also accents
        ticket["message"] = message

        ticket["agent"] = rng.choice(AGENTS)
        if status == "closed":
            resolved = created + timedelta(hours=rng.randint(1, 120))
            ticket["resolved_at"] = resolved.strftime("%Y-%m-%dT%H:%M:%SZ")
            score = rng.choices([1, 2, 3, 4, 5], weights=[5, 8, 15, 35, 37])[0]
            r = rng.random()
            if r < 0.10:
                token = rng.choice(["N/A", ""])
                key["null_tokens"]["satisfaction"][token or "<empty>"] += 1
                ticket["satisfaction"], score = token, None
            else:
                fmt = "{s}" if r < 0.65 else "{s}/5" if r < 0.85 else "{s} stars"
                key["satisfaction_formats"][fmt.replace("{s}", "N")] += 1
                ticket["satisfaction"] = fmt.format(s=score)
        else:
            token = rng.choice(["", "", "-"])
            key["null_tokens"]["resolved_at"][token or "<empty>"] += 1
            ticket["resolved_at"], ticket["satisfaction"], score = token, "", None
            key["null_tokens"]["satisfaction"]["<empty> (not closed)"] += 1

        rows.append(ticket)
        truth.append({"order_no": order_no, "status": status, "score": score,
                      "has_email": has_email, "has_phone": has_phone})

    for src in rng.sample(rows, N_DUPLICATES):  # the helpdesk export repeated some tickets
        rows.insert(rng.randint(0, len(rows)), dict(src))

    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(rows)

    key["counts"] = dict(sorted(key["counts"].items()))
    for k in ("created_at_formats", "order_ref_formats", "satisfaction_formats"):
        key[k] = dict(sorted(key[k].items()))
    key["null_tokens"] = {k: dict(v) for k, v in key["null_tokens"].items()}
    for k in ("channel_raw_to_canonical", "priority_raw_to_canonical", "status_raw_to_canonical"):
        key[k] = dict(sorted(key[k].items()))
    key["rows_in_file"] = len(rows)
    key["unique_rows"] = N_TICKETS
    key["exact_duplicate_rows"] = N_DUPLICATES
    key["columns"] = list(rows[0])
    key["pii_columns"] = ["customer_email", "message (free text: emails, phones, first names)"]
    key["expected_clean_output"] = {
        "rows_after_dedup": N_TICKETS,
        "null_order_ref": sum(t["order_no"] is None for t in truth),
        "distinct_channels": len(CHANNELS),
        "distinct_priorities": len(PRIORITIES),
        "distinct_statuses": len(STATUSES),
        "closed_tickets": sum(t["status"] == "closed" for t in truth),
        "null_satisfaction": sum(t["score"] is None for t in truth),
        "messages_needing_redaction": sum(t["has_email"] or t["has_phone"] for t in truth),
        "emails_or_phones_left_in_messages": 0,
    }
    KEY_PATH.write_text(json.dumps(key, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {CSV_PATH.name}: {len(rows)} rows ({N_TICKETS} unique + {N_DUPLICATES} duplicates)")
    print(f"wrote {KEY_PATH.name}")


if __name__ == "__main__":
    main()
