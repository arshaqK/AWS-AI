"""Personal-data guard shared by everything that persists text written from the data:
the Documenter's reports (tools/docs.py) and remembered conventions (memory.py).

Phone patterns cover the formats seen in the vendor files; plain 10-11 digit runs must
stand alone, so long hex ids (Glue run ids) never trip it.
"""
import re
from typing import List

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RES = [re.compile(r"\+\d[\d ()-]{9,}\d"), re.compile(r"\b\d{3}\.\d{3}\.\d{4}\b"),
             re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}"), re.compile(r"(?<![\w.:-])\d{10,11}(?![\w.:-])")]
PLACEHOLDER_EMAILS = {"name@domain.tld", "user@example.com", "local@domain.tld"}


def find_personal_data(text: str) -> List[str]:
    # an address never ends in "." - that is the end of the sentence it sits in
    hits = [m for m in (e.rstrip(".") for e in EMAIL_RE.findall(text)) if m.lower() not in PLACEHOLDER_EMAILS]
    for pattern in PHONE_RES:
        hits += pattern.findall(text)
    return hits
