# Task 3: Sentiment Analysis of Tweets using Amazon Comprehend

## Overview

This task uses a Python script (via Boto3, the AWS SDK for Python) to call
Amazon Comprehend's `DetectSentiment` API on a set of sample tweets. For
each tweet, the script prints the overall detected sentiment
(`POSITIVE` / `NEGATIVE` / `NEUTRAL` / `MIXED`) along with the confidence
score for each of the four sentiment categories, and saves all results to a
local JSON file.

## Architecture / Flow

```
Tweet text (in-script list)  --->  Comprehend (DetectSentiment)  --->  Sentiment + Confidence Scores
                                                                             |
                                                                             v
                                                          comprehend_sentiment_results.json
```

Unlike Task 1, there is no S3 step here — text is passed directly from the
Python script to Comprehend's API as a string, so there's no cross-service
region dependency to worry about beyond Comprehend's own availability.


## Step 1: Confirm Comprehend is Available in Your Region

Not all AWS services are available in every region. Before running this
script, confirm Comprehend is supported in your target region using the
official regional services list:
https://aws.amazon.com/about-aws/global-infrastructure/regional-product-services/

Or check via CLI:
```bash
aws ssm get-parameters-by-path \
  --path /aws/service/global-infrastructure/regions/us-east-1/services/comprehend \
  --output text
```
If this returns nothing, Comprehend isn't available in that region.

## Step 2: The Python Script

`comprehend_sentiment_analysis.py` does the following:

1. Creates a Boto3 client for Comprehend:
   `comprehend_client = boto3.client("comprehend", region_name=REGION)`.
2. Defines a list of sample tweets in-script (a placeholder dataset — swap
   in a real tweet dataset such as a CSV export if available; live Twitter/X
   API access requires separate developer credentials and was out of scope
   here).
3. For each tweet, calls `comprehend_client.detect_sentiment(Text=tweet, LanguageCode="en")`
   — this is the single line that performs the actual sentiment analysis.
4. Comprehend returns:
   - `Sentiment` — one of `POSITIVE`, `NEGATIVE`, `NEUTRAL`, `MIXED`
   - `SentimentScore` — confidence breakdown across all four categories
     (they sum to approximately 1.0)
5. Prints the tweet, its sentiment, and all four confidence scores to the
   console.
6. Saves all results as `comprehend_sentiment_results.json`.

### Config values to update before running
```python
REGION = "eu-central-1"   # must be a Comprehend-supported region
```

## Step 3: Run the Script

In CloudShell or a local terminal with Boto3 installed:
```bash
python3 comprehend_sentiment_analysis.py
```
