"""
Formats an EstimationResult from estimator.py as a GitHub-flavoured Markdown
string suitable for posting as a PR comment.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from estimator import EstimationResult, CallEstimate


# ── Public interface ──────────────────────────────────────────────────────────

def generate_report(
    result: EstimationResult,
    monthly_threshold_usd: Optional[float] = None,
) -> str:
    """
    Render a Markdown cost report for a GitHub PR comment.

    Args:
        result:                 Output of estimator.estimate_calls().
        monthly_threshold_usd:  Optional cost ceiling. When the projected
                                monthly total exceeds this value a 🚨 alert
                                section is added to the report.

    Returns:
        A GitHub-flavoured Markdown string ready to post as a PR comment.
    """
    sections: list[str] = []

    sections.append(_header())
    sections.append(_summary_table(result))

    if result.unrecognized_model_ids:
        sections.append(_unrecognized_warning(result.unrecognized_model_ids))

    if monthly_threshold_usd and monthly_threshold_usd > 0:
        alert = _threshold_alert(result.total_cost_per_month_usd, monthly_threshold_usd)
        if alert:
            sections.append(alert)

    sections.append(_footer(result))

    return "\n\n".join(sections)


# ── Sections ─────────────────────────────────────────────────────────────────

def _header() -> str:
    return "## 💰 LLM Cost Lint Report"


def _summary_table(result: EstimationResult) -> str:
    rows: list[str] = []

    rows.append("| Provider | Model | File | Line | Tokens (in / out) | Est. Cost / Call | Est. Daily Cost | Est. Monthly Cost |")
    rows.append("|---|---|---|---|---|---|---|---|")

    for est in result.call_estimates:
        rows.append(_table_row(est, result.config.calls_per_day))

    rows.append(_total_row(result))

    return "\n".join(rows)


def _table_row(est: CallEstimate, calls_per_day: int) -> str:
    provider = _fmt_provider(est.call.provider)
    model = est.display_name
    file_path = est.call.file_path
    line = str(est.call.line_number)

    # Flag token values that came from defaults rather than source code.
    in_tok = _fmt_tokens(est.input_tokens, est.input_tokens_source)
    out_tok = _fmt_tokens(est.output_tokens, est.output_tokens_source)
    tokens = f"{in_tok} / {out_tok}"

    cost_per_call = _fmt_usd(est.cost_per_call_usd)
    cost_per_day = _fmt_usd(est.cost_per_day_usd)
    cost_per_month = _fmt_usd(est.cost_per_month_usd)

    return f"| {provider} | {model} | `{file_path}` | {line} | {tokens} | {cost_per_call} | {cost_per_day} | {cost_per_month} |"


def _total_row(result: EstimationResult) -> str:
    total_per_call = _fmt_usd(result.total_cost_per_call_usd)
    total_per_day = _fmt_usd(result.total_cost_per_day_usd)
    total_per_month = _fmt_usd(result.total_cost_per_month_usd)
    return f"| **Total** | | | | | **{total_per_call}** | **{total_per_day}** | **{total_per_month}** |"


def _unrecognized_warning(model_ids: list[str]) -> str:
    model_list = "\n".join(f"- `{mid}`" for mid in model_ids)
    return (
        "⚠️ **Unrecognized models** — could not estimate cost for:\n\n"
        + model_list
        + "\n\n"
        "These calls are excluded from the totals above. "
        "Add pricing entries to `config/pricing.yml` to include them."
    )


def _threshold_alert(monthly_total: float, threshold: float) -> Optional[str]:
    if monthly_total <= threshold:
        return None
    return (
        f"🚨 **Monthly cost estimate ({_fmt_usd(monthly_total)}) "
        f"exceeds threshold ({_fmt_usd(threshold)})**\n\n"
        "This PR introduces LLM calls whose projected monthly cost is above "
        "the configured limit. Review the estimates above before merging."
    )


def _footer(result: EstimationResult) -> str:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Determine whether any token counts were inferred vs extracted from code.
    any_default_input = any(
        e.input_tokens_source == "default" for e in result.call_estimates
    )
    any_default_output = any(
        e.output_tokens_source == "default" for e in result.call_estimates
    )

    token_note_parts: list[str] = []
    if any_default_input:
        token_note_parts.append(
            f"input tokens defaulted to {result.config.default_input_tokens} "
            "(†) where `max_tokens` was not set in source"
        )
    if any_default_output:
        token_note_parts.append(
            f"output tokens defaulted to {result.config.default_output_tokens} "
            "(‡) where `max_tokens` was not set in source"
        )

    token_note = (
        "**Token assumptions:** " + "; ".join(token_note_parts) + "."
        if token_note_parts
        else "Token counts were extracted directly from source for all calls."
    )

    lines = [
        "---",
        f"*Generated at {timestamp} · "
        f"Projection assumes {result.config.calls_per_day:,} calls/day · "
        "30-day month*",
        "",
        token_note,
        "",
        "**Pricing sources:** "
        "[AWS Bedrock](https://aws.amazon.com/bedrock/pricing/) · "
        "[Azure OpenAI](https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/)",
        "",
        "*Estimates are approximate. "
        "Streaming calls, prompt caching, batch inference, and provisioned throughput "
        "are priced differently and are not reflected above. "
        "Verify before production deployment.*",
    ]

    return "\n".join(lines)


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_provider(provider: str) -> str:
    return {
        "aws_bedrock": "AWS Bedrock",
        "azure_openai": "Azure OpenAI",
    }.get(provider, provider)


def _fmt_usd(amount: float) -> str:
    """
    Format a USD amount with enough precision to be meaningful at micro-cent
    scale (e.g. $0.000150) while rounding larger amounts to cents ($12.34).
    """
    if amount == 0:
        return "$0.00"
    if amount < 0.001:
        return f"${amount:.6f}"
    if amount < 1:
        return f"${amount:.4f}"
    return f"${amount:,.2f}"


def _fmt_tokens(count: int, source: str) -> str:
    """Append a marker to token counts that came from defaults, not source."""
    marker = "†" if source == "default" else ""
    return f"{count:,}{marker}"
