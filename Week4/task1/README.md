# AWS Glue Crawler for Sample Dataset Cataloging

## Overview
This task demonstrates cataloging a sample CSV dataset stored in Amazon S3 using an AWS Glue Crawler. The crawler scans the dataset, infers its schema, and populates the AWS Glue Data Catalog with a queryable table definition.

## Dataset
The sample dataset used for this task is a **synthetic customer orders dataset**, generated using ChatGPT. It was created purely for demonstration purposes to simulate a realistic CSV structure (e.g., customer IDs, order details, dates, amounts) that a Glue Crawler could crawl and infer a schema from.

## Objective
- Create a Glue Crawler pointed at a sample CSV dataset in S3
- Configure the crawler to detect schema (columns, data types, format)
- Populate the Glue Data Catalog with the resulting table
- Verify the cataloged table in the AWS Glue console

## Architecture / Flow
```
S3 (raw CSV data)
      │
      ▼
Glue Crawler  ──uses──▶  IAM Role (S3 read + Glue service permissions)
      │
      ▼
Glue Data Catalog
  ├── Database (metadata namespace)
  └── Table (inferred schema + S3 location pointer)
```

The Glue Data Catalog does not store any actual data — it only stores metadata (column names, data types, file format, and the S3 location). The raw data remains in S3 at all times.

## Steps Performed

1. **Prepared the S3 source** — confirmed the sample CSV file(s) were available under a single S3 prefix.
2. **Created an IAM role for Glue** — attached the `AWSGlueServiceRole` managed policy and `AmazonS3ReadOnlyAccess` for S3 read access.
3. **Created a database in the Glue Data Catalog** — a logical namespace to group the resulting table(s).
4. **Created and configured the crawler**:
   - Data source: S3 path to the sample dataset
   - IAM role: role created in step 2
   - Output: database created in step 3
   - Schedule: On demand (one-time run)
5. **Ran the crawler** — Glue scanned the CSV file, inferred the delimiter, column names, and data types.
6. **Verified the results** in the Glue console:
   - Crawler run history shows a successful run
   - Data Catalog table shows the inferred schema
   - Table properties confirm the S3 location and `csv` classification

## Screenshots

### 1. Crawler Configuration and Run History
Shows the crawler's data source, IAM role, target database, and a successful run status confirming execution completed without errors.

![Crawler configuration and run history](screenshots\crawler_configurations_and_run_history.png)

### 2. Data Catalog Schema (Inferred)
Shows the schema automatically inferred by the crawler — column names and data types detected from the CSV file.

![Data Catalog inferred schema](screenshots\data_catalog_schema.png)

### 3. Database and Table in Glue Console
Shows the table registered under the Glue database, confirming the dataset is cataloged and browsable in the console.

![Database and table view](screenshots\db_table.png)

## Result
The sample dataset is now cataloged in AWS Glue. The table can be queried directly through Amazon Athena (or used by other Glue ETL jobs / Redshift Spectrum) without needing to redefine the schema, since Athena and other services read schema and location information directly from the Glue Data Catalog.

## Notes
- The Glue database is purely a metadata namespace; no actual data is stored there.
- If the crawler infers a column as `string` when it should be numeric, this is usually due to inconsistent values in the sampled rows (e.g., a stray non-numeric value).
- Crawler runs are billed per DPU-hour; a single small CSV crawl typically completes in a few minutes at negligible cost.
