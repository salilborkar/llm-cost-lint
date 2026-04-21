"""Reads and validates action inputs from environment variables."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class Config:
    github_token: str
    repo: str           # owner/repo
    pr_number: int

    monthly_cost_threshold: float
    block_on_threshold: bool

    scan_paths: list[str]
    exclude_paths: list[str]

    aws_region: str
    bedrock_calls_per_month: int

    azure_region: str
    azure_calls_per_month: int

    default_input_tokens: int
    default_output_tokens: int

    model_map: Dict[str, str]

    post_pr_comment: bool
    comment_mode: str   # create | update | replace
    report_format: str  # markdown | json

    @classmethod
    def from_env(cls) -> Config:
        def _env(key: str, default: str = "") -> str:
            return os.environ.get(f"INPUT_{key.upper()}", default).strip()

        def _bool(key: str, default: str = "false") -> bool:
            return _env(key, default).lower() == "true"

        def _int(key: str, default: str) -> int:
            val = _env(key, default)
            try:
                return int(val)
            except ValueError as exc:
                raise ValueError(f"Input '{key}' must be an integer, got: {val!r}") from exc

        def _float(key: str, default: str) -> float:
            val = _env(key, default)
            try:
                return float(val)
            except ValueError as exc:
                raise ValueError(f"Input '{key}' must be a number, got: {val!r}") from exc

        def _list(key: str, default: str = "") -> list[str]:
            raw = _env(key, default)
            items = [p.strip() for p in raw.replace("\n", ",").split(",")]
            return [p for p in items if p]

        threshold = _float("monthly_cost_threshold", "0")
        if threshold < 0:
            raise ValueError("monthly_cost_threshold must be >= 0")

        comment_mode = _env("comment_mode", "update")
        if comment_mode not in {"create", "update", "replace"}:
            raise ValueError(f"comment_mode must be create|update|replace, got: {comment_mode!r}")

        report_format = _env("report_format", "markdown")
        if report_format not in {"markdown", "json"}:
            raise ValueError(f"report_format must be markdown|json, got: {report_format!r}")

        model_map_raw = _env("model_map", "{}")
        try:
            model_map = json.loads(model_map_raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"model_map is not valid JSON: {exc}") from exc

        return cls(
            github_token=_env("github_token"),
            repo=os.environ.get("GITHUB_REPOSITORY", ""),
            pr_number=int(os.environ.get("GITHUB_PR_NUMBER", "0")),
            monthly_cost_threshold=threshold,
            block_on_threshold=_bool("block_on_threshold"),
            scan_paths=_list("scan_paths", "**/*.py"),
            exclude_paths=_list("exclude_paths"),
            aws_region=_env("aws_region", "us-east-1"),
            bedrock_calls_per_month=_int("bedrock_calls_per_month", "1000"),
            azure_region=_env("azure_region", "eastus"),
            azure_calls_per_month=_int("azure_calls_per_month", "1000"),
            default_input_tokens=_int("default_input_tokens", "500"),
            default_output_tokens=_int("default_output_tokens", "500"),
            model_map=model_map,
            post_pr_comment=_bool("post_pr_comment", "true"),
            comment_mode=comment_mode,
            report_format=report_format,
        )
