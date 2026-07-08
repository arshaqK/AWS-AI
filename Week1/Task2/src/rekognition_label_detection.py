"""
Task 1: Image Analysis with Amazon Rekognition
------------------------------------------------
Reads 2-3 images stored in an S3 bucket, calls Rekognition's detect_labels
on each, and stores the detected labels + confidence scores to a local
JSON/CSV file for review.

Prerequisites:
  pip install boto3
  aws configure   (or have credentials/role already set up)

IAM permissions needed on your user/role:
  - s3:ListBucket, s3:GetObject   (on the target bucket)
  - rekognition:DetectLabels
"""

import boto3
import json
import csv
from datetime import datetime

# ---------- CONFIG ----------
BUCKET_NAME = "imagebucket-ak"   # <-- change to your bucket name
REGION = "eu-west-1"                         # <-- change to your bucket's region
MAX_LABELS = 10                              # number of labels to return per image
MIN_CONFIDENCE = 70                          # only keep labels above this confidence %
# -----------------------------

s3_client = boto3.client("s3", region_name=REGION)
rekognition_client = boto3.client("rekognition", region_name=REGION)


def list_images_in_bucket(bucket_name):
    """List image files (jpg/jpeg/png) in the given S3 bucket."""
    response = s3_client.list_objects_v2(Bucket=bucket_name)
    if "Contents" not in response:
        print(f"No objects found in bucket: {bucket_name}")
        return []

    image_extensions = (".jpg", ".jpeg", ".png")
    images = [
        obj["Key"] for obj in response["Contents"]
        if obj["Key"].lower().endswith(image_extensions)
    ]
    return images


def detect_labels_for_image(bucket_name, image_key):
    """Call Rekognition detect_labels for a single S3 image."""
    response = rekognition_client.detect_labels(
        Image={"S3Object": {"Bucket": bucket_name, "Name": image_key}},
        MaxLabels=MAX_LABELS,
        MinConfidence=MIN_CONFIDENCE,
    )
    return response["Labels"]


def main():
    images = list_images_in_bucket(BUCKET_NAME)
    if not images:
        print("No images found. Upload some to your S3 bucket first.")
        return

    print(f"Found {len(images)} image(s) in '{BUCKET_NAME}': {images}\n")

    all_results = []

    for image_key in images:
        print(f"Analyzing: {image_key} ...")
        try:
            labels = detect_labels_for_image(BUCKET_NAME, image_key)
        except Exception as e:
            print(f"  Error analyzing {image_key}: {e}")
            continue

        image_result = {
            "image": image_key,
            "analyzed_at": datetime.utcnow().isoformat(),
            "labels": [
                {"name": label["Name"], "confidence": round(label["Confidence"], 2)}
                for label in labels
            ],
        }
        all_results.append(image_result)

        for label in labels:
            print(f"  - {label['Name']}: {label['Confidence']:.2f}%")
        print()

    # Save results as JSON
    with open("rekognition_results.json", "w") as f:
        json.dump(all_results, f, indent=2)

    # Save results as CSV (flattened: one row per label)
    with open("rekognition_results.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["image", "label", "confidence"])
        for result in all_results:
            for label in result["labels"]:
                writer.writerow([result["image"], label["name"], label["confidence"]])

    print("Done. Results saved to rekognition_results.json and rekognition_results.csv")


if __name__ == "__main__":
    main()
