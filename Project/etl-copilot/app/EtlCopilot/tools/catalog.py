"""Refresh the raw Glue tables after the engineer uploads a CSV to landing/<dataset>/.

Runs the one crawler the Execution role may start (Phase 2 policy v2) and waits for it.
The crawler turns each landing/<dataset>/ folder into the table etl_copilot_raw.raw_<dataset>.
"""
import json
import logging
import time

from strands import tool

import narration
from aws_session import execution_session
from config import RAW_DB

CRAWLER_NAME = "etl-copilot-landing-crawler"
POLL_INTERVAL_S = 10
TIMEOUT_S = 600

progress = logging.getLogger("etl_copilot")


def raw_tables() -> list:
    names = []
    for page in execution_session().client("glue").get_paginator("get_tables").paginate(DatabaseName=RAW_DB):
        names += [t["Name"] for t in page["TableList"]]
    return sorted(names)


def run_crawler() -> dict:
    """Plain function (no LLM) so tests can call it directly."""
    glue = execution_session().client("glue")
    before = raw_tables()
    try:
        glue.start_crawler(Name=CRAWLER_NAME)
        progress.info("refresh_raw_tables: crawler started")
    except glue.exceptions.CrawlerRunningException:
        progress.info("refresh_raw_tables: crawler already running, waiting for it")
    deadline = time.time() + TIMEOUT_S
    time.sleep(POLL_INTERVAL_S)
    while True:
        crawler = glue.get_crawler(Name=CRAWLER_NAME)["Crawler"]
        if crawler["State"] == "READY":
            break
        if time.time() > deadline:
            return {"finished": False, "state": crawler["State"],
                    "message": f"crawler still {crawler['State']} after {TIMEOUT_S}s; check again later"}
        time.sleep(POLL_INTERVAL_S)
    last = crawler.get("LastCrawl", {})
    after = raw_tables()
    progress.info("refresh_raw_tables: %s, new tables %s", last.get("Status"), sorted(set(after) - set(before)))
    succeeded = last.get("Status") == "SUCCEEDED"
    result = {"finished": True, "status": last.get("Status"),
              "raw_tables": after, "new_tables": sorted(set(after) - set(before))}
    if last.get("ErrorMessage"):  # on success this is only a warning (e.g. crawler log delivery)
        result["warning" if succeeded else "error"] = last["ErrorMessage"]
    return result


@tool
def refresh_raw_tables() -> str:
    """Run the landing crawler so every landing/<dataset>/ folder has a raw table.

    Use when a dataset has a landing folder but list_datasets shows no raw table (the
    engineer uploaded a new CSV). Takes 1-3 minutes. Returns JSON with the crawl status,
    all raw tables and the ones that are new.
    """
    narration.activity("supervisor", "The Glue crawler is cataloguing the new CSV (1-3 minutes)")
    return json.dumps(run_crawler())
