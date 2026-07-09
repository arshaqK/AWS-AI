"""
Task 2: Sentiment Analysis of Tweets using Amazon Comprehend
--------------------------------------------------------------
Calls Comprehend's detect_sentiment on a list of sample tweets and prints
the detected sentiment (POSITIVE / NEGATIVE / NEUTRAL / MIXED) along with
the confidence scores for each sentiment class.

Prerequisites:
  pip install boto3   (already pre-installed if using AWS CloudShell)
  aws configure        (not needed in CloudShell - it uses your console session)

IAM permission needed on your user/role:
  - comprehend:DetectSentiment
"""

import boto3
import json

# ---------- CONFIG ----------
REGION = "eu-central-1"   # Comprehend must be available in this region
# -----------------------------

comprehend_client = boto3.client("comprehend", region_name=REGION)

# Sample tweets to analyze (replace with your own dataset if you have one)
tweets = [
    "I absolutely love the new update, it's fantastic!",
    "This is the worst service I have ever experienced.",
    "The event starts at 6 PM tomorrow at the community hall.",
    "I'm not sure how I feel about this, it's okay I guess but could be better.",
    "Best concert ever!! The band was incredible and the crowd was amazing.",
    "Why is customer support so slow? I've been waiting for 3 days.",
]


def detect_sentiment(text):
    """Call Comprehend detect_sentiment for a single piece of text."""
    response = comprehend_client.detect_sentiment(
        Text=text,
        LanguageCode="en",
    )
    return response["Sentiment"], response["SentimentScore"]


def main():
    results = []

    for tweet in tweets:
        sentiment, scores = detect_sentiment(tweet)

        print(f"Tweet: {tweet}")
        print(f"  Sentiment: {sentiment}")
        print(f"  Scores -> Positive: {scores['Positive']:.4f}, "
              f"Negative: {scores['Negative']:.4f}, "
              f"Neutral: {scores['Neutral']:.4f}, "
              f"Mixed: {scores['Mixed']:.4f}")
        print()

        results.append({
            "tweet": tweet,
            "sentiment": sentiment,
            "scores": {
                "positive": round(scores["Positive"], 4),
                "negative": round(scores["Negative"], 4),
                "neutral": round(scores["Neutral"], 4),
                "mixed": round(scores["Mixed"], 4),
            }
        })

    # Save results to a JSON file
    with open("comprehend_sentiment_results.json", "w") as f:
        json.dump(results, f, indent=2)

    print("Done. Results saved to comprehend_sentiment_results.json")


if __name__ == "__main__":
    main()
