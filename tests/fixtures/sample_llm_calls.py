"""
Simulated LLM API usage for llm-cost-lint parser/estimator tests.

Each function wraps a single SDK call. Comments above each call indicate
what the parser should detect.
"""

import json
import boto3
from openai import AzureOpenAI


# ── AWS Bedrock client setup ──────────────────────────────────────────────────

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")


# ── Azure OpenAI client setup ─────────────────────────────────────────────────

azure_client = AzureOpenAI(
    api_key="placeholder",
    api_version="2024-02-01",
    azure_endpoint="https://my-resource.openai.azure.com",
)


# ── 1. Bedrock / Claude 3.5 Sonnet v2 — invoke_model ─────────────────────────

def summarise_document(document: str) -> str:
    """Summarise a document using Claude 3.5 Sonnet v2 on Bedrock."""
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": f"Summarise this document:\n\n{document}"}],
    }

    # PARSER SHOULD DETECT: provider=aws_bedrock, model=anthropic.claude-3-5-sonnet-20241022-v2:0, max_tokens=1000
    response = bedrock.invoke_model(
        modelId="anthropic.claude-3-5-sonnet-20241022-v2:0",
        body=json.dumps(payload),
        contentType="application/json",
        accept="application/json",
    )

    body = json.loads(response["body"].read())
    return body["content"][0]["text"]


# ── 2. Bedrock / Claude 3 Haiku — converse ────────────────────────────────────

def classify_intent(user_message: str) -> str:
    """Classify the intent of a user message using Claude 3 Haiku on Bedrock."""

    # PARSER SHOULD DETECT: provider=aws_bedrock, model=anthropic.claude-3-haiku-20240307-v1:0, max_tokens=500
    response = bedrock.converse(
        modelId="anthropic.claude-3-haiku-20240307-v1:0",
        messages=[{"role": "user", "content": [{"type": "text", "text": user_message}]}],
        inferenceConfig={"maxTokens": 500, "temperature": 0.2},
    )

    return response["output"]["message"]["content"][0]["text"]


# ── 3. Azure OpenAI / GPT-4o — chat completion ───────────────────────────────

def generate_code_review(diff: str) -> str:
    """Generate a code review summary for a git diff using GPT-4o."""

    # PARSER SHOULD DETECT: provider=azure_openai, model=gpt-4o-2024-11-20, max_tokens=800
    response = azure_client.chat.completions.create(
        model="gpt-4o-2024-11-20",
        messages=[
            {"role": "system", "content": "You are an expert code reviewer."},
            {"role": "user", "content": f"Review this diff:\n\n{diff}"},
        ],
        max_tokens=800,
        temperature=0.3,
    )

    return response.choices[0].message.content


# ── 4. Azure OpenAI / GPT-4o Mini — chat completion ─────────────────────────

def generate_commit_message(diff: str) -> str:
    """Generate a one-line commit message for a git diff using GPT-4o Mini."""

    # PARSER SHOULD DETECT: provider=azure_openai, model=gpt-4o-mini-2024-07-18, max_tokens=200
    response = azure_client.chat.completions.create(
        model="gpt-4o-mini-2024-07-18",
        messages=[
            {"role": "system", "content": "You write concise git commit messages."},
            {"role": "user", "content": f"Write a commit message for:\n\n{diff}"},
        ],
        max_tokens=200,
    )

    return response.choices[0].message.content


# ── 5. Bedrock / unknown model — invoke_model ────────────────────────────────

def experimental_call(prompt: str) -> str:
    """Calls a model ID that does not exist in pricing.yml.
    This should trigger the unrecognized model warning in the estimator.
    """
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }

    # PARSER SHOULD DETECT: provider=aws_bedrock, model=anthropic.claude-99-fake-v1:0, max_tokens=300
    # ESTIMATOR SHOULD WARN: unrecognized model ID, excluded from cost totals
    response = bedrock.invoke_model(
        modelId="anthropic.claude-99-fake-v1:0",
        body=json.dumps(payload),
        contentType="application/json",
        accept="application/json",
    )

    body = json.loads(response["body"].read())
    return body["content"][0]["text"]
