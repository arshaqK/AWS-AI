"""
classify.py
Step 2: Rule-based keyword scoring classifier.

Approach:
  - Each category has a weighted keyword/phrase list.
  - We score the extracted text against each category's keywords
    (case-insensitive, phrase matching).
  - Category with the highest score wins, provided it clears a
    minimum confidence threshold — otherwise we return
    "Unclassified" rather than force a bad guess.

Weight guide: 3 = near-certain signal, 2 = strong signal, 1 = weak/generic signal
"""

import re
from dataclasses import dataclass


CATEGORY_KEYWORDS = {
    "Invoice": [
        ("invoice #", 3), ("invoice no", 3), ("invoice number", 3),
        ("bill to", 3), ("remit payment", 3), ("grand total", 2),
        ("due date", 2), ("payment terms", 2), ("net 30", 2),
        ("unit price", 2), ("subtotal", 2), ("tax", 1),
        ("qty", 1), ("total", 1), ("invoice", 2),
    ],
    "Sales Report": [
        ("sales report", 3), ("quarterly sales", 3), ("revenue by region", 3),
        ("total revenue", 2), ("units sold", 2), ("year over year", 2),
        ("year-over-year", 2), ("sales target", 2), ("q1", 1), ("q2", 1),
        ("q3", 1), ("q4", 1), ("forecast", 1), ("growth", 1), ("kpi", 2),
    ],
    "Customer Application": [
        ("application form", 3), ("applicant name", 3), ("date of birth", 3),
        ("please sign below", 2), ("terms and conditions", 1),
        ("ssn", 2), ("social security", 2), ("employment history", 2),
        ("i hereby declare", 3), ("signature", 1), ("applicant", 2),
    ],
}

MIN_SCORE_THRESHOLD = 3  # below this total weighted score -> Unclassified


@dataclass
class ClassificationResult:
    category: str
    confidence: str      # "high" / "medium" / "low"
    scores: dict
    matched_keywords: dict


def _score_text(text: str) -> dict:
    text_lower = text.lower()
    scores = {}
    matches = {}

    for category, keywords in CATEGORY_KEYWORDS.items():
        total = 0
        matched = []
        for phrase, weight in keywords:
            pattern = re.escape(phrase)
            count = len(re.findall(pattern, text_lower))
            if count > 0:
                total += weight * count
                matched.append((phrase, count, weight))
        scores[category] = total
        matches[category] = matched

    return scores, matches


def classify(text: str) -> ClassificationResult:
    scores, matches = _score_text(text)

    best_category = max(scores, key=scores.get)
    best_score = scores[best_category]

    if best_score < MIN_SCORE_THRESHOLD:
        return ClassificationResult(
            category="Unclassified",
            confidence="low",
            scores=scores,
            matched_keywords=matches
        )

    sorted_scores = sorted(scores.values(), reverse=True)
    runner_up = sorted_scores[1] if len(sorted_scores) > 1 else 0
    margin = best_score - runner_up

    if best_score >= 8 and margin >= 4:
        confidence = "high"
    elif best_score >= 5:
        confidence = "medium"
    else:
        confidence = "low"

    return ClassificationResult(
        category=best_category,
        confidence=confidence,
        scores=scores,
        matched_keywords=matches
    )