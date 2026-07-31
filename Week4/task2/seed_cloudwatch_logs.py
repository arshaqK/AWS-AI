"""
Seed dummy CloudWatch log data for testing a Glue ETL pipeline.

Creates a log group with several log streams (simulating different
services / "name" tags), and writes a mix of INFO and ERROR log
entries spread across the last few days so you have real data to
export to S3 and test date-based partitioning in Athena.

Requirements:
    pip install boto3 --break-system-packages

Usage:
    python3 seed_cloudwatch_logs.py
"""

import boto3
import random
import time
from datetime import datetime, timedelta, timezone

# ---- Configuration ----
LOG_GROUP_NAME = "/demo/app-logs"
SERVICE_NAMES = ["auth-service", "payment-service", "inventory-service"]
DAYS_BACK = 5            # how many past days to spread logs across
ENTRIES_PER_SERVICE_PER_DAY = 15
LEVEL_WEIGHTS = {"INFO": 0.6, "ERROR": 0.25, "WARN": 0.15}  # extra levels to test filtering

SAMPLE_INFO_MESSAGES = [
    "Request processed successfully",
    "User authentication succeeded",
    "Cache refreshed",
    "Health check passed",
    "Scheduled job completed",
]
SAMPLE_ERROR_MESSAGES = [
    "Failed to connect to database",
    "Unhandled exception in request handler",
    "Timeout while calling downstream service",
    "Invalid payload received",
    "Retry limit exceeded",
]
SAMPLE_WARN_MESSAGES = [
    "High memory usage detected",
    "Deprecated API endpoint called",
    "Slow query detected",
]

logs_client = boto3.client("logs")


def ensure_log_group(name):
    try:
        logs_client.create_log_group(logGroupName=name)
        print(f"Created log group: {name}")
    except logs_client.exceptions.ResourceAlreadyExistsException:
        print(f"Log group already exists: {name}")


def ensure_log_stream(group_name, stream_name):
    try:
        logs_client.create_log_stream(
            logGroupName=group_name, logStreamName=stream_name
        )
        print(f"  Created log stream: {stream_name}")
    except logs_client.exceptions.ResourceAlreadyExistsException:
        print(f"  Log stream already exists: {stream_name}")


def pick_level():
    r = random.random()
    cumulative = 0
    for level, weight in LEVEL_WEIGHTS.items():
        cumulative += weight
        if r <= cumulative:
            return level
    return "INFO"


def message_for_level(level):
    if level == "INFO":
        return random.choice(SAMPLE_INFO_MESSAGES)
    elif level == "ERROR":
        return random.choice(SAMPLE_ERROR_MESSAGES)
    else:
        return random.choice(SAMPLE_WARN_MESSAGES)


def seed_logs():
    ensure_log_group(LOG_GROUP_NAME)

    now = datetime.now(timezone.utc)

    for service in SERVICE_NAMES:
        ensure_log_stream(LOG_GROUP_NAME, service)

        total_written = 0

        # CloudWatch rejects a single PutLogEvents call whose events
        # span more than 24 hours, so we build and send one batch
        # per day rather than one batch for the whole date range.
        # Oldest day first, since CloudWatch also expects each
        # successive call for a stream to be in non-decreasing order.
        for day_offset in range(DAYS_BACK - 1, -1, -1):
            day = now - timedelta(days=day_offset)
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)

            events = []
            for _ in range(ENTRIES_PER_SERVICE_PER_DAY):
                # Spread entries across the day, but keep within
                # CloudWatch's constraint: not older than 14 days,
                # not more than 2 hours in the future.
                random_seconds = random.randint(0, 86000)
                ts = day_start + timedelta(seconds=random_seconds)
                level = pick_level()
                message = message_for_level(level)
                log_line = f"{level} [{service}] {message}"
                events.append(
                    {
                        "timestamp": int(ts.timestamp() * 1000),
                        "message": log_line,
                    }
                )

            # CloudWatch requires events within a batch to be in
            # chronological order.
            events.sort(key=lambda e: e["timestamp"])

            logs_client.put_log_events(
                logGroupName=LOG_GROUP_NAME,
                logStreamName=service,
                logEvents=events,
            )
            total_written += len(events)

        print(f"  Wrote {total_written} log events to stream '{service}'")

    print("\nDone. Log group:", LOG_GROUP_NAME)
    print("Streams:", ", ".join(SERVICE_NAMES))


if __name__ == "__main__":
    seed_logs()
