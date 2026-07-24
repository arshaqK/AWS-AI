"""
pipeline.py
Wires extraction -> classification -> field extraction into one call.
This is roughly what the Lambda handler will invoke per PDF.
"""

from extract import extract
from classify import classify
from extract_fields import extract_fields


def process_pdf(pdf_path: str) -> dict:
    extraction = extract(pdf_path, with_tables=False)

    if extraction["likely_scanned_doc"]:
        return {
            "filename": pdf_path,
            "status": "needs_ocr_fallback",
            "category": None,
            "fields": {},
        }

    classification = classify(extraction["full_text"])

    field_result = extract_fields(extraction["full_text"], classification.category)

    return {
        "filename": pdf_path,
        "status": "processed",
        "category": classification.category,
        "classification_confidence": classification.confidence,
        "fields": field_result.fields,
        "field_confidence": field_result.field_confidence,
        "num_pages": extraction["num_pages"],
    }