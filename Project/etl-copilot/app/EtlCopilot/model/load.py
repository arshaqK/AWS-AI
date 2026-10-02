import os

from strands.models.bedrock import BedrockModel

# us-west-2 is pinned explicitly so the local CLI default region (us-east-1) never leaks in.
REGION = os.getenv("AWS_REGION", "us-west-2")
# Sonnet 4.6 is the strongest model this account can invoke (verified in Phase 0.1).
MODEL_ID = os.getenv("ETL_COPILOT_MODEL_ID", "us.anthropic.claude-sonnet-4-6")


def load_model() -> BedrockModel:
    """Get Bedrock model client using IAM credentials."""
    return BedrockModel(model_id=MODEL_ID, region_name=REGION)
