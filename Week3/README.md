# Serverless PDF Document Pipeline

An automated, serverless pipeline that ingests digitally-native PDF documents, extracts their text, classifies them into business categories, extracts key fields, and stores structured results in DynamoDB — all without paid OCR services.

## Problem

Manually categorizing and extracting data from high-volume PDF documents (invoices, sales reports, customer applications) is slow and error-prone. This pipeline automates that process end-to-end using open-source Python libraries and AWS free-tier services.

## Architecture

```
PDF uploaded to S3
      │
      ▼
S3 Event Trigger (ObjectCreated, *.pdf)
      │
      ▼
AWS Lambda (pdf-pipeline-processor)
      │
      ├─ 1. Extract text        (extract.py — PyMuPDF)
      ├─ 2. Classify category   (classify.py — keyword scoring)
      ├─ 3. Extract key fields  (extract_fields.py — regex per category)
      │
      ▼
DynamoDB (structured, queryable record per document)
```

**Why no OCR/Textract:** the target documents are digitally-native (text is already embedded as selectable characters), so OCR is unnecessary cost and complexity. PyMuPDF extracts embedded text directly, at zero marginal cost and higher accuracy than OCR for this document type. Scanned/image-only PDFs are auto-detected and flagged (`needs_ocr_fallback`) rather than silently mis-processed.

## How It Works, End to End

The pipeline runs in three stages, chained together and triggered automatically. **`extract.py`** opens a PDF with PyMuPDF and pulls out its embedded text directly (no OCR needed since these are digitally-native PDFs), flagging any document with almost no extractable text as "likely scanned" so it can be routed elsewhere instead of silently processed wrong. **`classify.py`** takes that extracted text and scores it against weighted keyword lists for each business category (Invoice, Sales Report, Customer Application), picking the highest-scoring category if it clears a minimum confidence threshold, or returning `Unclassified` rather than force-guessing. **`extract_fields.py`** then applies category-specific regex patterns to pull out the actual values you care about (invoice number, total, applicant name, etc.), marking each field as `matched` or `not_found` rather than dropping missing ones silently. **`lambda_handler.py`** is what actually runs on AWS — triggered automatically whenever a PDF lands in your S3 bucket, it downloads the file, runs it through the extract → classify → extract_fields chain, and writes the final structured result as one item into your DynamoDB table. Drop a PDF into S3, and within seconds a fully categorized, field-extracted, searchable record appears in DynamoDB — with zero OCR cost and zero manual work.

## Categories Supported

- **Invoice** — invoice #, date, due date, bill-to, grand total, vendor
- **Sales Report** — report period, total revenue, units sold, YoY growth, sales target
- **Customer Application** — applicant name, date of birth, employer, position, address

Documents that don't clearly match a category are marked `Unclassified` rather than force-guessed.

## Files

| File | Purpose |
|---|---|
| `extract.py` | Step 1 — PyMuPDF-based text extraction, scanned-doc detection, optional pdfplumber table extraction |
| `classify.py` | Step 2 — weighted keyword scoring classifier across the three categories |
| `extract_fields.py` | Step 3 — regex-based field extraction, per category |
| `pipeline.py` | Local glue script: `process_pdf(path)` chains Steps 1→2→3 for local testing without AWS |
| `lambda_handler.py` | AWS Lambda entry point — S3 event → pipeline → DynamoDB write. This is the only file that actually runs on AWS |
| `run_test.py` | Local test runner — calls `pipeline.py` against sample PDFs to sanity-check extraction/classification before deploying |

`pipeline.py` and `run_test.py` exist purely as a local dev/debug loop, so extraction and classification logic can be verified on your laptop in seconds — without needing to re-zip and re-upload to Lambda every time you tweak a regex pattern.

## Local Setup

```powershell
pip install pymupdf pdfplumber
python pipeline.py path\to\sample.pdf
```

## AWS Deployment

### 1. Prerequisites
- S3 bucket (input documents)
- DynamoDB table — partition key: `document_id` (String), no sort key

### 2. Build the Lambda Layer (dependencies)

PyMuPDF/pdfplumber must be built for Lambda's Linux environment, not your local OS.

```powershell
mkdir lambda_layer\python
pip install pymupdf pdfplumber `
    --platform manylinux2014_aarch64 `
    --target lambda_layer\python `
    --only-binary=:all: `
    --python-version 3.12
```

**Zip with 7-Zip, not PowerShell's `Compress-Archive`** — the native Windows zip tool can silently drop files with long nested paths (a real issue encountered with PyMuPDF's `_extra.so`):

```powershell
cd lambda_layer
& "C:\Program Files\7-Zip\7z.exe" a -r ..\pdf_pipeline_layer.zip python
```

Verify before uploading:
```powershell
& "C:\Program Files\7-Zip\7z.exe" l ..\pdf_pipeline_layer.zip | Select-String "extra.so"
```

Upload the zip to S3 (layers over ~50MB can't be uploaded directly through the console), then create the Layer pointing at that S3 object:
- Compatible architecture: **arm64**
- Compatible runtime: **Python 3.12**

### 3. Create the Lambda Function
- Runtime: Python 3.12
- Architecture: **arm64** (must match the layer)
- Attach the Layer created above
- Upload `extract.py`, `classify.py`, `extract_fields.py`, `pipeline.py`, `lambda_handler.py` as a zip (code only — no dependencies, the layer provides those)

### 4. Configure the Function
- **Code tab → Runtime settings → Handler**: `lambda_handler.lambda_handler`
- **General configuration**: Memory 512 MB, Timeout 30 sec
- **Environment variables**: `DYNAMODB_TABLE` = your table name

### 5. IAM Permissions

Attach an inline policy to the function's execution role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::YOUR_BUCKET/*" },
    { "Effect": "Allow", "Action": ["dynamodb:PutItem"], "Resource": "arn:aws:dynamodb:REGION:*:table/YOUR_TABLE" }
  ]
}
```

### 6. S3 Trigger
- Event type: All object create events
- Suffix filter: `.pdf`
- Destination: the Lambda function above

## Testing

Upload a PDF to the S3 bucket, then check:
- **DynamoDB → table → Explore table items** for the new record
- **CloudWatch → Log groups → `/aws/lambda/<function-name>`** for execution logs/errors

## DynamoDB Record Shape

```json
{
  "document_id": "INV-2026-00873",
  "s3_key": "invoice.pdf",
  "status": "processed",
  "category": "Invoice",
  "classification_confidence": "high",
  "fields": {
    "invoice_number": "INV-2026-00873",
    "invoice_date": "2026-07-18",
    "grand_total": "2,062.80"
  },
  "field_confidence": { "invoice_number": "matched", "due_date": "not_found" },
  "num_pages": 3,
  "processed_at": "2026-07-25T10:00:00+00:00"
}
```

## Cost

Entirely within AWS always-free tiers at moderate volume:
- **Lambda**: 1M free requests/month
- **S3**: 5GB free storage
- **DynamoDB**: 25GB storage + 25 RCU/WCU always free, on-demand billing charges only per request
- **No OCR/Textract fees** — the core cost driver this pipeline was designed to eliminate

## Known Limitations / Future Improvements

- Regex-based field extraction is brittle to layout drift — a template change can silently produce `not_found` rather than erroring. `field_confidence` is stored per field so incomplete records can be queried and routed for review.
- Scanned/image-only PDFs are detected and flagged (`needs_ocr_fallback`) but not yet processed — a future stage could route these to Tesseract (open-source) or Textract (paid, as a last resort).
- Classifier is rule-based (fast, free, explainable); an LLM-based classifier is a natural upgrade path if real-world document variance exceeds what keyword scoring handles well.
- Extracted fields are stored as a nested Map (`fields`) — fine for full-record retrieval, but querying/filtering directly on a specific field (e.g. `total_revenue > X`) would require flattening fields to top-level attributes or adding a GSI.
