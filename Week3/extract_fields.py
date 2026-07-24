"""
extract_fields.py
Step 3: Key field extraction, per document category.

Approach:
  - Each category has its own set of field patterns (regex).
  - Patterns are written to be tolerant of common formatting variance
    (e.g. "Invoice #", "Invoice No.", "Invoice Number").
  - Every extracted field records which pattern matched, so results
    stay auditable — same principle as the classifier.
  - Fields that don't match are returned as None rather than omitted,
    so downstream consumers (e.g. DynamoDB writer) always see a
    consistent schema per category.
"""

import re
from dataclasses import dataclass


# ---- Field pattern definitions, per category ----
# Each entry: field_name -> list of regex patterns (first match wins)
# Patterns use named group 'val' for the captured value.

INVOICE_FIELD_PATTERNS = {
    "invoice_number": [
        r"invoice\s*#\s*:?\s*(?P<val>[A-Za-z0-9\-]+)",
        r"invoice\s*(?:no|number)\.?\s*:?\s*(?P<val>[A-Za-z0-9\-]+)",
    ],
    "invoice_date": [
        r"(?<!due\s)date\s*:?\s*(?P<val>\d{4}-\d{2}-\d{2})",
        r"(?<!due\s)date\s*:?\s*(?P<val>\d{1,2}/\d{1,2}/\d{2,4})",
    ],
    "due_date": [
        r"due\s*date\s*:?\s*(?P<val>\d{4}-\d{2}-\d{2})",
        r"due\s*date\s*:?\s*(?P<val>\d{1,2}/\d{1,2}/\d{2,4})",
    ],
    "bill_to": [
        r"bill\s*to\s*:?\s*(?P<val>[A-Za-z0-9 &.,\-]+?)(?:\n|$)",
    ],
    "grand_total": [
        r"grand\s*total\s*:?\s*\$?(?P<val>[\d,]+\.\d{2})",
        r"total\s*:?\s*\$?(?P<val>[\d,]+\.\d{2})",
    ],
    "vendor_name": [
        # heuristic: first non-empty line after the word INVOICE header
        r"invoice\s*\n+\s*(?P<val>[A-Za-z0-9 &.,\-]+)",
    ],
}

SALES_REPORT_FIELD_PATTERNS = {
    "report_period": [
        r"(?P<val>Q[1-4]\s*20\d{2})",
    ],
    "total_revenue": [
        r"total\s*revenue\s*:?\s*\$?(?P<val>[\d,]+)",
    ],
    "units_sold": [
        r"units\s*sold\s*:?\s*(?P<val>[\d,]+)",
    ],
    "yoy_growth": [
        r"year[\s\-]over[\s\-]year\s*growth\s*:?\s*(?P<val>[\d.]+%)",
    ],
    "sales_target": [
        r"sales\s*target(?:\s*for\s*\w+)?\s*:?\s*\$?(?P<val>[\d,]+)",
    ],
}

CUSTOMER_APPLICATION_FIELD_PATTERNS = {
    "applicant_name": [
        r"applicant\s*name\s*:?\s*(?P<val>[A-Za-z .\-]+?)(?:\n|$)",
    ],
    "date_of_birth": [
        r"date\s*of\s*birth\s*:?\s*(?P<val>\d{4}-\d{2}-\d{2})",
        r"date\s*of\s*birth\s*:?\s*(?P<val>\d{1,2}/\d{1,2}/\d{2,4})",
    ],
    "current_employer": [
        r"current\s*employer\s*:?\s*(?P<val>[A-Za-z0-9 &.,\-]+?)(?:\n|$)",
    ],
    "position": [
        r"position\s*:?\s*(?P<val>[A-Za-z0-9 &.,\-]+?)(?:\n|$)",
    ],
    "address": [
        r"address\s*:?\s*(?P<val>[A-Za-z0-9 &.,\-]+?)(?:\n|$)",
    ],
}

CATEGORY_PATTERN_MAP = {
    "Invoice": INVOICE_FIELD_PATTERNS,
    "Sales Report": SALES_REPORT_FIELD_PATTERNS,
    "Customer Application": CUSTOMER_APPLICATION_FIELD_PATTERNS,
}


@dataclass
class FieldExtractionResult:
    category: str
    fields: dict           # field_name -> extracted value (or None)
    field_confidence: dict  # field_name -> "matched" / "not_found"


def _extract_field(text: str, patterns: list):
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group("val").strip()
    return None


def extract_fields(text: str, category: str) -> FieldExtractionResult:
    pattern_set = CATEGORY_PATTERN_MAP.get(category)

    if pattern_set is None:
        # Unclassified or unknown category -> no field extraction attempted
        return FieldExtractionResult(
            category=category,
            fields={},
            field_confidence={}
        )

    fields = {}
    confidence = {}

    for field_name, patterns in pattern_set.items():
        value = _extract_field(text, patterns)
        fields[field_name] = value
        confidence[field_name] = "matched" if value is not None else "not_found"

    return FieldExtractionResult(
        category=category,
        fields=fields,
        field_confidence=confidence
    )