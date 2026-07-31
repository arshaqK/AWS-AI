"""
Glue Python Shell job: process exported CloudWatch logs.

Reads gzip-compressed CloudWatch export files from S3, parses each
log line (format: "<ISO timestamp> <LEVEL> [<service>] <message>"),
keeps only ERROR and INFO entries, and writes the result back to S3
partitioned by service name and date so Athena can query efficiently
by date.

Job parameters (set these under Job details > Job parameters in the
Glue console, or pass via --arguments when running the job):
    --SOURCE_BUCKET       e.g. cw-logs-export-ak
    --SOURCE_PREFIX       e.g. raw-logs/
    --DEST_BUCKET         e.g. cw-logs-export-ak (can be the same bucket)
    --DEST_PREFIX         e.g. processed-logs/
"""

import sys
import gzip
import io
import re
import csv
from collections import defaultdict

import boto3
from awsglue.utils import getResolvedOptions

args = getResolvedOptions(
    sys.argv,
    ["SOURCE_BUCKET", "SOURCE_PREFIX", "DEST_BUCKET", "DEST_PREFIX"],
)

SOURCE_BUCKET = args["SOURCE_BUCKET"]
SOURCE_PREFIX = args["SOURCE_PREFIX"]
DEST_BUCKET = args["DEST_BUCKET"]
DEST_PREFIX = args["DEST_PREFIX"].rstrip("/")

# Matches: 2026-07-26T00:55:33.000Z ERROR [auth-service] Invalid payload received
LOG_LINE_PATTERN = re.compile(
    r"^(?P<timestamp>\S+)\s+(?P<level>\S+)\s+\[(?P<service>[^\]]+)\]\s+(?P<message>.*)$"
)

KEEP_LEVELS = {"ERROR", "INFO"}

s3 = boto3.client("s3")


def list_source_files(bucket, prefix):
    """Yield every .gz object key under the given prefix."""
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".gz"):
                yield key


def read_and_decompress(bucket, key):
    """Download a .gz object and return its decompressed text lines."""
    response = s3.get_object(Bucket=bucket, Key=key)
    raw_bytes = response["Body"].read()
    with gzip.GzipFile(fileobj=io.BytesIO(raw_bytes)) as gz:
        text = gz.read().decode("utf-8", errors="replace")
    return text.splitlines()


def parse_line(line):
    """Return (timestamp, level, service, message) or None if it doesn't match."""
    match = LOG_LINE_PATTERN.match(line.strip())
    if not match:
        return None
    return (
        match.group("timestamp"),
        match.group("level"),
        match.group("service"),
        match.group("message"),
    )


def main():
    # Group filtered rows by (service, date) so we can write one file
    # per partition, matching the name=<service>/date=<date>/ layout
    # a Glue Crawler will recognize as partition columns.
    grouped_rows = defaultdict(list)
    total_lines = 0
    kept_lines = 0

    keys = list(list_source_files(SOURCE_BUCKET, SOURCE_PREFIX))
    print(f"Found {len(keys)} source files under s3://{SOURCE_BUCKET}/{SOURCE_PREFIX}")

    for key in keys:
        for line in read_and_decompress(SOURCE_BUCKET, key):
            if not line.strip():
                continue
            total_lines += 1
            parsed = parse_line(line)
            if parsed is None:
                continue
            timestamp, level, service, message = parsed
            if level not in KEEP_LEVELS:
                continue
            date = timestamp[:10]  # "2026-07-26T00:55:33.000Z" -> "2026-07-26"
            grouped_rows[(service, date)].append((timestamp, level, service, message))
            kept_lines += 1

    print(f"Parsed {total_lines} lines, kept {kept_lines} (ERROR/INFO only)")

    # Write one CSV per (service, date) partition back to S3.
    partitions_written = 0
    for (service, date), rows in grouped_rows.items():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["timestamp", "level", "service", "message"])
        writer.writerows(rows)

        dest_key = (
            f"{DEST_PREFIX}/name={service}/date={date}/data.csv"
        )
        s3.put_object(
            Bucket=DEST_BUCKET,
            Key=dest_key,
            Body=buffer.getvalue().encode("utf-8"),
        )
        partitions_written += 1

    print(f"Wrote {partitions_written} partitions to s3://{DEST_BUCKET}/{DEST_PREFIX}/")


if __name__ == "__main__":
    main()
