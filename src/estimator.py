"""
Estimates per-call and projected monthly LLM costs for a list of LLMCall
objects produced by parser.py, using pricing data from config/pricing.yml.

Known limitations (estimates will be WRONG in these cases — documented
inline at each relevant calculation):
  - Streaming calls: billed identically to non-streaming; no penalty applied.
  - Prompt caching: AWS Bedrock and Azure OpenAI cache hits cost 10–90% less
    depending on provider/model. We always charge full input-token price.
  - Batch / async inference: AWS Batch Inference and Azure batch deployments
    have separate (usually lower) pricing. We use on-demand rates.
  - System prompts and few-shot examples: input token defaults assume a plain
    user message. Real prompts with large system messages can be 5–10x larger.
  - Token counting: we use raw character-based defaults, not a real tokeniser.
    GPT-4o and Claude use different tokenisers; actual token counts may differ.
  - Cross-region / Data Zone deployments: Azure charges ~10% more for
    Regional/Data Zone deployments vs Global. We always use Global pricing.
  - Provisioned throughput: PTU pricing on both platforms is completely
    different (capacity-based, not per-token). We always use on-demand rates.
  - Embeddings: if an embeddings call is somehow detected (e.g. via a shared
    client), it will be priced as a chat completion, which is incorrect.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from parser import LLMCall


# ── Paths ─────────────────────────────────────────────────────────────────────

_PRICING_PATH = Path(__file__).parent.parent / "config" / "pricing.yml"


# ── Config ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EstimatorConfig:
    """
    Tunable assumptions used when static values cannot be extracted from source.

    All defaults are intentionally conservative (i.e. they will produce a
    higher-than-minimum estimate) so that cost reports err on the side of
    over-reporting rather than under-reporting.
    """
    # Token defaults when parser could not extract values statically.
    default_input_tokens: int = 1000
    default_output_tokens: int = 500

    # Projection frequency.
    calls_per_day: int = 1000


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass
class CallEstimate:
    """Cost estimate for a single detected call site."""
    call: LLMCall
    display_name: str           # human-readable model name from pricing.yml
    input_tokens: int           # tokens used for cost calculation
    output_tokens: int          # tokens used for cost calculation
    input_tokens_source: str    # "extracted" | "default"
    output_tokens_source: str   # "extracted" | "default"
    cost_per_call_usd: float
    cost_per_day_usd: float     # cost_per_call × calls_per_day
    cost_per_month_usd: float   # cost_per_day × 30


@dataclass
class EstimationResult:
    """Aggregated output of estimate_calls()."""
    call_estimates: list[CallEstimate]

    # Aggregate totals across all call sites.
    total_cost_per_call_usd: float
    total_cost_per_day_usd: float
    total_cost_per_month_usd: float

    # Model IDs present in the parsed calls but absent from pricing.yml.
    # Callers should surface these as warnings — they represent blind spots in
    # the cost report, not zero-cost calls.
    unrecognized_model_ids: list[str]

    # Config snapshot used to produce this result, for auditability.
    config: EstimatorConfig


# ── Public interface ──────────────────────────────────────────────────────────

def estimate_calls(
    calls: list[LLMCall],
    config: Optional[EstimatorConfig] = None,
    pricing_path: Optional[Path] = None,
) -> EstimationResult:
    """
    Estimate costs for a list of LLMCall objects.

    Args:
        calls:        Output of parser.parse_files().
        config:       Token and frequency assumptions. Uses defaults if omitted.
        pricing_path: Override path to pricing.yml, mainly for testing.

    Returns:
        EstimationResult with per-call breakdowns and aggregate totals.
    """
    if config is None:
        config = EstimatorConfig()

    pricing = _load_pricing(pricing_path or _PRICING_PATH)

    call_estimates: list[CallEstimate] = []
    unrecognized: list[str] = []

    for call in calls:
        rate = _lookup_rate(call, pricing)

        if rate is None:
            # Do NOT silently skip — record the model ID so callers can flag it.
            key = f"{call.provider}/{call.model_id}"
            if key not in unrecognized:
                unrecognized.append(key)
            warnings.warn(
                f"llm-cost-lint: no pricing found for {key!r}. "
                "This call will be excluded from cost totals.",
                stacklevel=2,
            )
            continue

        input_tokens, input_source = _resolve_input_tokens(call, config)
        output_tokens, output_source = _resolve_output_tokens(call, config)

        cost_per_call = _calc_cost(
            input_tokens, output_tokens,
            rate["input_cost_per_1k_tokens"],
            rate["output_cost_per_1k_tokens"],
        )

        # WRONG for batch/async inference: on-demand rate applied regardless.
        # WRONG for provisioned throughput: PTU is capacity-priced, not per-token.
        cost_per_day = cost_per_call * config.calls_per_day

        # 30-day month is a simplification; some months have 28–31 days.
        cost_per_month = cost_per_day * 30

        call_estimates.append(CallEstimate(
            call=call,
            display_name=rate["display_name"],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            input_tokens_source=input_source,
            output_tokens_source=output_source,
            cost_per_call_usd=cost_per_call,
            cost_per_day_usd=cost_per_day,
            cost_per_month_usd=cost_per_month,
        ))

    total_per_call = sum(e.cost_per_call_usd for e in call_estimates)
    total_per_day = sum(e.cost_per_day_usd for e in call_estimates)
    total_per_month = sum(e.cost_per_month_usd for e in call_estimates)

    return EstimationResult(
        call_estimates=call_estimates,
        total_cost_per_call_usd=total_per_call,
        total_cost_per_day_usd=total_per_day,
        total_cost_per_month_usd=total_per_month,
        unrecognized_model_ids=unrecognized,
        config=config,
    )


# ── Pricing loader ────────────────────────────────────────────────────────────

def _load_pricing(path: Path) -> dict[str, dict]:
    """
    Load pricing.yml and return a flat dict keyed by "provider/model_id".

    Example key: "aws_bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"
    """
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    flat: dict[str, dict] = {}
    for provider_key, models in raw.items():
        if not isinstance(models, dict):
            # Skip comment-only top-level keys that yaml parses as non-dicts.
            continue
        for model_id, attrs in models.items():
            if not isinstance(attrs, dict):
                continue
            flat[f"{provider_key}/{model_id}"] = attrs

    return flat


def _lookup_rate(call: LLMCall, pricing: dict[str, dict]) -> Optional[dict]:
    """
    Look up the pricing entry for a call using "provider/model_id" as the key.

    Returns None when the model is not in pricing.yml — this is surfaced as an
    unrecognized model, not silently treated as free.
    """
    key = f"{call.provider}/{call.model_id}"
    return pricing.get(key)


# ── Token resolution ──────────────────────────────────────────────────────────

def _resolve_input_tokens(call: LLMCall, config: EstimatorConfig) -> tuple[int, str]:
    """
    Input tokens are never extracted by the parser today (the parser only reads
    max_tokens / output side). Always falls back to the configured default.

    WRONG when: system prompts, conversation history, or large few-shot examples
    are present — real input token counts can be 10–100x the default.
    """
    # Future: if the call site carries a messages= or prompt= argument whose
    # token count can be estimated, use it here.
    return config.default_input_tokens, "default"


def _resolve_output_tokens(call: LLMCall, config: EstimatorConfig) -> tuple[int, str]:
    """
    Uses max_tokens extracted from the call site when available; falls back to
    the configured default otherwise.

    WRONG when:
      - max_tokens is set as a hard ceiling but the model regularly stops well
        below it (e.g. short factual answers with max_tokens=4096 set as a
        safety limit). We bill at max_tokens, which over-estimates.
      - Streaming calls: output token count depends on user behaviour (how
        long they let the stream run). We apply the same max_tokens logic.
    """
    if call.max_tokens is not None:
        return call.max_tokens, "extracted"
    return config.default_output_tokens, "default"


# ── Cost calculation ──────────────────────────────────────────────────────────

def _calc_cost(
    input_tokens: int,
    output_tokens: int,
    input_rate_per_1k: float,
    output_rate_per_1k: float,
) -> float:
    """
    Standard per-token cost formula used by both AWS and Azure.

    WRONG for:
      - Prompt cache hits: AWS Bedrock charges ~10% of the input rate for
        cache read tokens; Azure OpenAI charges 50% for cached input tokens.
        We always charge the full input rate.
      - AWS Bedrock cross-region inference: adds a small latency surcharge
        but pricing is the same on-demand rate.
    """
    input_cost = (input_tokens / 1000) * input_rate_per_1k
    output_cost = (output_tokens / 1000) * output_rate_per_1k
    return input_cost + output_cost
