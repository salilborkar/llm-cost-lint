"""Cost estimation using a bundled static pricing table."""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from pathlib import Path

from config import Config
from scanner import CallSite

_PRICING_PATH = Path(__file__).parent / "data" / "pricing.json"

_FALLBACK_RATE = {"input_per_1k": 0.002, "output_per_1k": 0.002}


@dataclass
class CostEstimate:
    call_site: CallSite
    per_call_usd: float
    monthly_usd: float
    calls_per_month: int
    model_found: bool


def estimate_costs(call_sites: list[CallSite], cfg: Config) -> list[CostEstimate]:
    db = _load_pricing()
    return [_estimate_one(cs, db, cfg) for cs in call_sites]


def _estimate_one(cs: CallSite, db: dict, cfg: Config) -> CostEstimate:
    key = f"{cs.provider}/{cs.model_id}"
    rates = db.get(key)
    found = rates is not None
    if not found:
        warnings.warn(
            f"llm-cost-guard: no pricing found for {key!r}, using fallback rate",
            stacklevel=2,
        )
        rates = _FALLBACK_RATE

    per_call = (
        cs.input_tokens / 1000 * rates["input_per_1k"]
        + cs.output_tokens / 1000 * rates["output_per_1k"]
    )

    calls_per_month = (
        cfg.bedrock_calls_per_month
        if cs.provider == "bedrock"
        else cfg.azure_calls_per_month
    )

    return CostEstimate(
        call_site=cs,
        per_call_usd=per_call,
        monthly_usd=per_call * calls_per_month,
        calls_per_month=calls_per_month,
        model_found=found,
    )


def _load_pricing() -> dict:
    try:
        return json.loads(_PRICING_PATH.read_text())
    except FileNotFoundError:
        return {}
