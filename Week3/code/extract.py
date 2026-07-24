"""
extract.py
Step 1: Text extraction for digitally-native PDFs.

Strategy:
  1. Use PyMuPDF (fitz) as the primary extractor — fast, low memory,
     good general-purpose text + basic layout extraction.
  2. If PyMuPDF finds (almost) no extractable text, flag the doc as
     "likely scanned" so the pipeline can route it to an OCR fallback
     later (Tesseract or Textract) instead of silently returning junk.
  3. Optionally run pdfplumber for table extraction on documents that
     need structured tabular data (invoices, financial reports).

Output: a plain dict, easy to JSON-serialize and pass downstream to
the classification/field-extraction Lambda.
"""

import fitz  # PyMuPDF
import pdfplumber
from dataclasses import dataclass, field, asdict


MIN_CHARS_PER_PAGE_THRESHOLD = 20  # below this, page is probably scanned/image-only


@dataclass
class PageResult:
    page_number: int
    text: str
    char_count: int
    likely_scanned: bool


@dataclass
class ExtractionResult:
    filename: str
    num_pages: int
    full_text: str
    pages: list = field(default_factory=list)
    likely_scanned_doc: bool = False   # True if MOST pages have near-zero text
    tables: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def extract_text_pymupdf(pdf_path: str) -> ExtractionResult:
    """Primary extraction pass — fast, no external services, no cost."""
    doc = fitz.open(pdf_path)
    pages = []
    full_text_parts = []
    scanned_page_count = 0

    for i, page in enumerate(doc):
        text = page.get_text("text")
        char_count = len(text.strip())
        likely_scanned = char_count < MIN_CHARS_PER_PAGE_THRESHOLD
        if likely_scanned:
            scanned_page_count += 1

        pages.append(PageResult(
            page_number=i + 1,
            text=text,
            char_count=char_count,
            likely_scanned=likely_scanned
        ))
        full_text_parts.append(text)

    doc.close()

    likely_scanned_doc = scanned_page_count > (len(pages) / 2) if pages else True

    return ExtractionResult(
        filename=pdf_path,
        num_pages=len(pages),
        full_text="\n".join(full_text_parts),
        pages=[asdict(p) for p in pages],
        likely_scanned_doc=likely_scanned_doc
    )


def extract_tables_pdfplumber(pdf_path: str) -> list:
    """Secondary pass — only for docs where we need structured tables
    (e.g. invoices with line items). Slower than PyMuPDF, used selectively."""
    tables_out = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            page_tables = page.extract_tables()
            for t in page_tables:
                tables_out.append({
                    "page_number": i + 1,
                    "rows": t
                })
    return tables_out


def extract(pdf_path: str, with_tables: bool = False) -> dict:
    """Main entry point used by the Lambda handler."""
    result = extract_text_pymupdf(pdf_path)

    if result.likely_scanned_doc:
        # Don't waste effort on table extraction / downstream NLP —
        # flag it for the OCR fallback path instead.
        return result.to_dict()

    if with_tables:
        result.tables = extract_tables_pdfplumber(pdf_path)

    return result.to_dict()