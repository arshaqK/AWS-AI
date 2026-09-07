from strands.models.bedrock import BedrockModel


def load_model() -> BedrockModel:
    """Get Bedrock model client using IAM credentials."""
    return BedrockModel(
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        region_name="us-west-2", 
        guardrail_id="mwmchucje6zd",       # from Step 3
        guardrail_version="1",              # or your published numeric version, e.g. "1"
        guardrail_trace="enabled",              # so traces show when the guardrail fires
    )