# Task 1 – AWS Serverless Image Processing Pipeline

## Overview

This task demonstrates a serverless image processing workflow using AWS services. When an image is uploaded to an Amazon S3 bucket, an AWS Lambda function is automatically triggered to resize the image and store the resized version in another S3 bucket. A second Lambda function is scheduled using Amazon EventBridge Scheduler (Cron) to automatically empty the resized image bucket at the end of the day.

The project also follows the **Principle of Least Privilege** by assigning each Lambda function only the permissions it requires.

---

## Architecture

```text
                   Upload Image
                         │
                         ▼
             image-upload-bucket-ak
                         │
                 S3 ObjectCreated Event
                         │
                         ▼
             ImageResizeLambda-ak
                         │
             Resize image using Pillow
                         │
                         ▼
             image-resize-bucket-ak
                         │
                         ▼
      Amazon EventBridge Scheduler (Cron)
                         │
                         ▼
             EmptyBucketLambda-ak
                         │
                         ▼
           Deletes all objects from bucket
```

---

## AWS Services Used

* Amazon S3
* AWS Lambda
* AWS IAM
* Amazon EventBridge Scheduler
* Amazon CloudWatch
* AWS CloudShell (for building the Pillow Lambda Layer)

---

## Features

* Automatically triggers a Lambda function when an image is uploaded.
* Reads the uploaded image from Amazon S3.
* Resizes the image to **300 × 300** pixels using the Pillow library.
* Uploads the resized image to a separate S3 bucket.
* Uses CloudWatch Logs for monitoring and debugging.
* Uses a scheduled EventBridge Cron job to automatically empty the resized image bucket.
* Implements least-privilege IAM permissions for both Lambda functions.
* Removes the scheduler after verification to prevent unintended executions.

---

## Project Structure

```text
Task1/
├── src/
│   ├── lambda-functions/
│   │   ├── EmptyBucket.py
│   │   └── ImageResize.py
│   │
│   ├── iam-policies/
│   │   ├── EmptyBucketPolicy.json
│   │   └── ImageResizePolicy.json
│   │
│   └── layers/
│       └── pillow_layer.zip
│
└── screenshots/
    ├── Bucket Cleanup Logs.png
    ├── Buckets.png
    ├── Cron Job.png
    ├── Empty Bucket Lambda.png
    ├── Image Resize Lambda.png
    ├── Image Resize Logs.png
    └── Pillow Layer.png
```

---

## Workflow

### 1. Image Upload

* Upload an image to the **image-upload-bucket-ak** S3 bucket.
* Amazon S3 generates an **ObjectCreated** event.

### 2. Lambda Trigger

* The upload event automatically invokes **ImageResizeLambda-ak**.

### 3. Image Processing

The Lambda function:

* Reads the uploaded image.
* Opens it using Pillow.
* Resizes it to **300 × 300** pixels.
* Uploads the resized image to **image-resize-bucket-ak**.

### 4. Scheduled Cleanup

* Amazon EventBridge Scheduler invokes **EmptyBucketLambda-ak** using a Cron expression.
* The Lambda lists all objects in the resized bucket.
* Each object is deleted.
* After verification, the schedule is deleted to prevent unintended executions.

---

## IAM Permissions

### Image Resize Lambda

Permissions granted:

* `s3:GetObject`
* `s3:PutObject`
* CloudWatch logging permissions

### Cleanup Lambda

Permissions granted:

* `s3:ListBucket`
* `s3:DeleteObject`
* CloudWatch logging permissions

This project follows the **Principle of Least Privilege** by granting only the permissions required by each function.

---

## Challenges Faced

During implementation, several practical AWS issues were encountered and resolved:

* Configured Amazon S3 event notifications to trigger Lambda.
* Created custom IAM policies with least-privilege permissions.
* Built a Linux-compatible Pillow Lambda Layer using Docker after encountering binary compatibility issues.
* Increased Lambda timeout and memory to process larger images successfully.
* Configured Amazon EventBridge Scheduler for automated bucket cleanup.
* Verified functionality through Amazon CloudWatch Logs.
