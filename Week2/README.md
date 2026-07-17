# AWS Certified AI Practitioner – Practice Exam Progress & Ethical AI Reflection

## Practice Exam Summary

I completed four practice exams while preparing for the AWS Certified AI Practitioner (AIF-C01) certification. The results show a consistent improvement in my understanding of AI/ML fundamentals, generative AI, responsible AI, and AWS AI services.

| Attempt | Platform | Practice Test | Score | Result |
|---------|----------|---------------|-------|--------|
| 1 | Udemy | Practice Test #3 | **76% (50/65)** | ✅ Passed |
| 2 | Tutorials Dojo | Timed Mode Set 1 | **87.65% (71/81)** | ✅ Passed |
| 3 | Tutorials Dojo | Timed Mode Set 2 | **90.48% (76/84)** | ✅ Passed |
| 4 | Udemy | Practice Test #1 | **81% (53/65)** | ✅ Passed |

---

## Attempt 1 – Udemy Practice Test #3

**Score:** **76% (50/65)**  
**Status:** ✅ Passed

![Attempt 1](TestScores\1stAttempt.png)

---

## Attempt 2 – Tutorials Dojo Timed Mode Set 1

**Score:** **87.65% (71/81)**  
**Status:** ✅ Passed

![Attempt 2](TestScores\2nd%20Attempt%20AI%20Practicioner,%201st%20on%20Tutorials%20Dojo.png)

---

## Attempt 3 – Tutorials Dojo Timed Mode Set 2

**Score:** **90.48% (76/84)**  
**Status:** ✅ Passed

![Attempt 3](TestScores\3rd%20Attempt%20AI%20Practicioner,%202nd%20on%20Tutorials%20Dojo.png)

---

## Attempt 4 – Udemy Practice Test #1

**Score:** **81% (53/65)**  
**Status:** ✅ Passed

![Attempt 4](TestScores\4th%20Attempt,%20Practice%20Test%201%20Udemy.png)

---

# Ethical AI Dilemma

## AI Prompt Privacy and Data Leakage

Millions of people use AI assistants such as ChatGPT, Claude, Perplexity, DALL·E, and Meta AI every day, often sharing personal or sensitive information without realizing the associated privacy risks. A recent controversy involving Meta AI revealed that some users unintentionally made their chatbot conversations publicly visible through the app's **Discover** feed, exposing discussions about medical conditions and other sensitive topics.

Beyond accidental public sharing, many AI providers use user prompts to improve their foundation models unless users explicitly opt out. This raises concerns about how personal information is collected, stored, and reused. Another significant ethical issue is **data leakage**, where AI models may inadvertently reveal sensitive information contained within their training data. Researchers have demonstrated that AI models can sometimes reproduce portions of memorized training data, including personal information from real individuals.

This dilemma highlights the importance of protecting user privacy, ensuring transparency about data usage, securing AI systems against unintended information disclosure, and educating users about the risks of sharing confidential information with AI applications.

**Original Article:**
https://www.scu.edu/ethics/focus-areas/internet-ethics/resources/prompt-privacy-an-ai-ethics-case-study/

---

# AWS Services and Best Practices to Mitigate the Ethical Dilemma

AWS provides several services and responsible AI practices that can help reduce privacy risks and protect sensitive information:

- **Amazon S3 Server-Side Encryption** – Secures stored prompts, datasets, and model artifacts.
- **Amazon Bedrock Guardrails** – Filters harmful content and helps prevent sensitive information from being exposed by foundation models.
- **Data Governance & Human Oversight** – Implement consent management, minimize collection of personal information, continuously monitor AI systems, and require human review for high-impact decisions involving sensitive data.

Together, these AWS services and best practices help organizations build AI systems that are secure, privacy-preserving, transparent, and aligned with responsible AI principles.