"""
lambda_handler.py
Entry point for the S3-triggered Lambda function.

Flow:
  1. S3 ObjectCreated event fires -> Lambda invoked with bucket/key
  2. Download PDF to /tmp (Lambda's writable scratch space)
  3. Run extract -> classify -> extract_fields pipeline
  4. Write structured result to DynamoDB
  5. (If likely_scanned_doc) skip classification, mark for OCR fallback
"""

import json
import os
import uuid
import boto3
from datetime import datetime, timezone

from extract import extract
from classify import classify
from extract_fields import extract_fields

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "DocumentPipelineTable")
table = dynamodb.Table(TABLE_NAME)


def lambda_handler(event, context):
    results = []

    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]

        if not key.lower().endswith(".pdf"):
            continue

        local_path = f"/tmp/{os.path.basename(key)}"
        s3.download_file(bucket, key, local_path)

        try:
            record_result = process_document(local_path, bucket, key)
            results.append(record_result)
        except Exception as e:
            error_id = str(uuid.uuid4())
            table.put_item(Item={
                "document_id": error_id,
                "s3_key": key,
                "status": "error",
                "error_message": str(e),
                "processed_at": datetime.now(timezone.utc).isoformat(),
            })
            results.append({"s3_key": key, "status": "error", "error": str(e)})
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)

    return {
        "statusCode": 200,
        "body": json.dumps(results)
    }


def process_document(local_path: str, bucket: str, key: str) -> dict:
    extraction = extract(local_path, with_tables=False)

    if extraction["likely_scanned_doc"]:
        doc_id = str(uuid.uuid4())
        item = {
            "document_id": doc_id,
            "s3_key": key,
            "status": "needs_ocr_fallback",
            "category": "Unknown",
            "num_pages": extraction["num_pages"],
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }
        table.put_item(Item=item)
        return item

    classification = classify(extraction["full_text"])
    field_result = extract_fields(extraction["full_text"], classification.category)

    doc_id = (
        field_result.fields.get("invoice_number")
        or field_result.fields.get("applicant_name")
        or str(uuid.uuid4())
    )

    item = {
        "document_id": doc_id,
        "s3_key": key,
        "status": "processed",
        "category": classification.category,
        "classification_confidence": classification.confidence,
        "fields": _clean_for_dynamo(field_result.fields),
        "field_confidence": field_result.field_confidence,
        "num_pages": extraction["num_pages"],
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    table.put_item(Item=item)
    return item


def _clean_for_dynamo(fields: dict) -> dict:
    return {k: (v if v is not None else "N/A") for k, v in fields.items()}