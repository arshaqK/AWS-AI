# Task 2: Image Analysis with Amazon Rekognition

## Overview

This task uploads a set of diverse images to an Amazon S3 bucket, then uses a
Python script (via Boto3, the AWS SDK for Python) to call Amazon Rekognition's
`DetectLabels` API on each image. The detected labels and their confidence
scores are printed to the console and saved locally as both JSON and CSV
files for review.

## Architecture / Flow

```
Local images  --->  S3 bucket  --->  Rekognition (DetectLabels)  --->  Labels + Confidence
                                                                          |
                                                                          v
                                                        rekognition_results.json / .csv
```

Note: the Python script never downloads the image bytes itself. It only
passes a bucket name + object key to Rekognition, and Rekognition fetches the
image directly from S3 on AWS's internal network. This requires the S3
bucket and the Rekognition API call to be in the **same AWS region**.


## Step 1: Create an S3 Bucket

Create a bucket in a **Rekognition-supported region** (e.g. `us-east-1`,
`eu-west-1`). Not all AWS services are available in every region —
Rekognition is not available in some regions such as `eu-north-1`.

```bash
aws s3 mb s3://your-bucket-name --region us-east-1
```

## Step 2: Upload 2–3 Diverse Images

Via AWS Console:
1. Go to **S3 → your bucket → Upload → Add files**
2. Select images from your local machine and upload

```

Rekognition only supports **JPEG and PNG** formats. If you get an
`InvalidImageFormatException`, check the actual file type with:
```bash
file your-image.jpg
```
(a file with a `.jpg` extension can still be an unsupported format
internally, e.g. WebP saved with the wrong extension).

## Step 3: IAM Permissions (Least Privilege)

Instead of using the broad managed policy `AmazonS3ReadOnlyAccess`, a custom
policy scoped to just the target bucket is used:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowListSpecificBucket",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::your-bucket-name"
    },
    {
      "Sid": "AllowGetObjectsInSpecificBucket",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::your-bucket-name/*"
    }
  ]
}
```

`AmazonRekognitionReadOnlyAccess` is used for the Rekognition side, since
Rekognition API actions are not resource-scopable the way S3 actions are.

> Note: if the identity running this script already has `AdministratorAccess`
> attached, IAM policies are additive — attaching a more restrictive policy
> alongside admin access does not reduce effective permissions. In that case,
> this custom policy is included here to document the least-privilege design
> intended for a dedicated task-specific IAM user/role in a production setup.

## Step 4: The Python Script

`rekognition_label_detection.py` does the following:

1. Creates two Boto3 clients — one for S3, one for Rekognition — each
   authenticated using whatever credentials are active in the environment
   (CloudShell session, `aws configure` profile, or IAM role).
2. Lists all `.jpg` / `.jpeg` / `.png` objects in the target S3 bucket
   (`s3_client.list_objects_v2`).
3. For each image, calls `rekognition_client.detect_labels()`, passing the
   image by S3 reference (`{"S3Object": {"Bucket": ..., "Name": ...}}`)
   rather than downloading and re-uploading raw bytes.
4. Filters results to labels with confidence ≥ 70% (`MinConfidence`) and
   caps results at 10 labels per image (`MaxLabels`).
5. Prints each label name and confidence score to the console.
6. Saves all results as:
   - `rekognition_results.json` — full nested structure per image
   - `rekognition_results.csv` — flattened, one row per label

### Config values to update before running
```python
BUCKET_NAME = "your-bucket-name"
REGION = "us-east-1"        # must match your bucket's region
MAX_LABELS = 10
MIN_CONFIDENCE = 70
```

## Step 5: Run the Script

In CloudShell or a local terminal with Boto3 installed:
```bash
python3 rekognition_label_detection.py
```

## What Rekognition Actually Returns

The `detect_labels` API returns more than just names and confidence scores.
Each label object can include:

| Field | Description |
|---|---|
| `Name` | Detected label (e.g. "Tiger") |
| `Confidence` | Model's confidence, 0–100% |
| `Instances` | Bounding boxes if the object is physically locatable in the image |
| `Parents` | Label hierarchy (e.g. Tiger → Mammal → Animal) |
| `Categories` | Broader groupings (e.g. "Animals and Pets") |
| `Aliases` | Alternate names for the same concept |

This script keeps only `Name` and `Confidence` for simplicity, per the task
requirements, but the raw API response contains richer data if needed later
(e.g. bounding boxes for object localization).

## Troubleshooting Reference

| Error | Cause | Fix |
|---|---|---|
| `Could not connect to the endpoint URL` | Rekognition not available in the bucket's region | Move bucket to a supported region (e.g. `us-east-1`, `eu-west-1`) |
| `InvalidImageFormatException` | File isn't actually JPEG/PNG despite its extension | Check with `file <filename>`, re-download/re-upload correct format |
| `AccessDeniedException` | Missing IAM permission | Confirm `s3:ListBucket`, `s3:GetObject`, `rekognition:DetectLabels` are attached to the active identity |
