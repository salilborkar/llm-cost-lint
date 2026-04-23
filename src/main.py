"""
Entrypoint for the llm-cost-lint GitHub Action.

Reads configuration from environment variables, scans Python files for LLM
API calls, estimates costs, and writes a Markdown report to stdout and to
$GITHUB_OUTPUT.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from estimator import EstimatorConfig, estimate_calls
from parser import parse_file, LLMCall
from pr_commenter import post_pr_comment
from reporter import generate_report


# ── Paths ─────────────────────────────────────────────────────────────────────

# Resolve pricing.yml relative to this file so it works regardless of the
# working directory the Action runner uses.
_REPO_ROOT = Path(__file__).parent.parent
_PRICING_PATH = _REPO_ROOT / "config" / "pricing.yml"


# ── Config ────────────────────────────────────────────────────────────────────

def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _die(f"Environment variable {name} must be an integer, got: {raw!r}")
    if value <= 0:
        _die(f"Environment variable {name} must be a positive integer, got: {value}")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _die(f"Environment variable {name} must be a number, got: {raw!r}")
    if value < 0:
        _die(f"Environment variable {name} must be >= 0, got: {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes"}


# ── File discovery ────────────────────────────────────────────────────────────

def _find_python_files(root: Path) -> list[Path]:
    """
    Recursively collect all .py files under root.
    If root is a file, return it directly (allows scanning a single file).
    """
    if root.is_file():
        return [root] if root.suffix == ".py" else []
    return sorted(root.rglob("*.py"))


# ── Scanning ──────────────────────────────────────────────────────────────────

def _scan_files(paths: list[Path]) -> list[LLMCall]:
    """
    Parse each file, collecting LLMCall objects. Files that fail to parse
    are skipped with a warning — one bad file should not abort the whole scan.
    """
    calls: list[LLMCall] = []
    for path in paths:
        try:
            file_calls = parse_file(path)
            calls.extend(file_calls)
        except Exception as exc:  # noqa: BLE001
            # Log and continue — a parse failure in one file should not prevent
            # the rest of the PR from being analysed.
            print(f"::warning file={path}::llm-cost-lint: skipping {path} — {exc}", flush=True)
    return calls


# ── GitHub Actions output ─────────────────────────────────────────────────────

def _write_github_output(key: str, value: str) -> None:
    """
    Write a key/value pair to $GITHUB_OUTPUT using the multiline heredoc
    format, which handles values that contain newlines (e.g. the full report).
    """
    output_file = os.environ.get("GITHUB_OUTPUT", "")
    if not output_file:
        return

    delimiter = "EOF_COST_REPORT"
    with open(output_file, "a", encoding="utf-8") as fh:
        fh.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def _write_github_step_summary(content: str) -> None:
    """Append content to $GITHUB_STEP_SUMMARY so the report appears in the
    Actions run summary tab as well as the PR comment."""
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if not summary_file:
        return
    with open(summary_file, "a", encoding="utf-8") as fh:
        fh.write(content + "\n")


# ── Error helpers ─────────────────────────────────────────────────────────────

def _die(message: str) -> None:
    """Print an Actions-formatted error annotation and exit non-zero."""
    print(f"::error::{message}", file=sys.stderr, flush=True)
    sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # 1. Read inputs from environment.
    scan_root = Path(_env_str("INPUT_PATH", "."))
    monthly_calls = _env_int("INPUT_MONTHLY_CALLS", 30_000)
    default_input_tokens = _env_int("INPUT_DEFAULT_INPUT_TOKENS", 1000)
    default_output_tokens = _env_int("INPUT_DEFAULT_OUTPUT_TOKENS", 500)
    cost_threshold = _env_float("INPUT_COST_THRESHOLD", 100.0)
    fail_on_threshold = _env_bool("INPUT_FAIL_ON_THRESHOLD", False)
    should_post_comment = _env_bool("INPUT_POST_PR_COMMENT", True)

    # Convert monthly calls to daily for EstimatorConfig.
    calls_per_day = max(1, round(monthly_calls / 30))

    # 2. Validate prerequisites.
    if not scan_root.exists():
        _die(
            f"INPUT_PATH does not exist: {scan_root}\n"
            "Set INPUT_PATH to a directory or .py file within your repository."
        )

    if not _PRICING_PATH.exists():
        _die(
            f"Pricing file not found: {_PRICING_PATH}\n"
            "Expected config/pricing.yml at the repository root. "
            "Ensure the file exists and the action is run from the repo root."
        )

    # 3. Discover Python files.
    py_files = _find_python_files(scan_root)
    if not py_files:
        print("llm-cost-lint: no Python files found under the scan path.", flush=True)
        _write_github_output("cost-report", "No Python files found.")
        sys.exit(0)

    print(f"llm-cost-lint: scanning {len(py_files)} Python file(s) under '{scan_root}'", flush=True)

    # 4. Parse files for LLM call sites.
    calls = _scan_files(py_files)

    if not calls:
        no_calls_msg = (
            "## 💰 LLM Cost Lint Report\n\n"
            "No LLM API calls detected in the scanned files."
        )
        print(no_calls_msg, flush=True)
        _write_github_output("cost-report", no_calls_msg)
        _write_github_step_summary(no_calls_msg)
        sys.exit(0)

    print(f"llm-cost-lint: found {len(calls)} LLM call site(s)", flush=True)

    # 5. Estimate costs.
    config = EstimatorConfig(
        default_input_tokens=default_input_tokens,
        default_output_tokens=default_output_tokens,
        calls_per_day=calls_per_day,
    )

    result = estimate_calls(calls, config=config, pricing_path=_PRICING_PATH)

    # 6. Generate report.
    report = generate_report(
        result,
        monthly_threshold_usd=cost_threshold if cost_threshold > 0 else None,
    )

    # 7. Emit report.
    print(report, flush=True)
    _write_github_output("cost-report", report)
    _write_github_step_summary(report)

    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request" and should_post_comment:
        post_pr_comment(report)

    # 8. Threshold gate.
    threshold_exceeded = (
        cost_threshold > 0
        and result.total_cost_per_month_usd > cost_threshold
    )

    if threshold_exceeded:
        print(
            f"::warning::llm-cost-lint: projected monthly cost "
            f"${result.total_cost_per_month_usd:.2f} exceeds threshold ${cost_threshold:.2f}",
            flush=True,
        )
        if fail_on_threshold:
            _die(
                f"Projected monthly cost (${result.total_cost_per_month_usd:.2f}) "
                f"exceeds INPUT_COST_THRESHOLD (${cost_threshold:.2f}). "
                "Set INPUT_FAIL_ON_THRESHOLD=false to report without blocking."
            )


if __name__ == "__main__":
    main()
