"""
llm-cost-guard: scans Python files for AWS Bedrock and Azure OpenAI SDK calls,
estimates per-call and projected monthly cost, and reports findings as a
structured GitHub PR comment.
"""

__version__ = "0.1.0"
