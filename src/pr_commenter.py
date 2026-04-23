"""Posts the cost report as a GitHub PR comment via the REST API."""

from __future__ import annotations

import json
import os


def post_pr_comment(markdown_body: str) -> None:
    """
    Post markdown_body as a comment on the current pull request.

    Reads GITHUB_TOKEN, GITHUB_REPOSITORY, and GITHUB_EVENT_PATH from the
    environment. Logs a warning and returns without raising if anything fails,
    so a commenting error never fails the action itself.
    """
    import requests  # imported here so the module is importable without requests installed

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("::warning::llm-cost-lint: GITHUB_TOKEN not set — skipping PR comment", flush=True)
        return

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not repository:
        print("::warning::llm-cost-lint: GITHUB_REPOSITORY not set — skipping PR comment", flush=True)
        return

    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not event_path:
        print("::warning::llm-cost-lint: GITHUB_EVENT_PATH not set — skipping PR comment", flush=True)
        return

    try:
        with open(event_path, encoding="utf-8") as fh:
            event = json.load(fh)
        pr_number = event["pull_request"]["number"]
    except Exception as exc:
        print(f"::warning::llm-cost-lint: could not read PR number from event payload — {exc}", flush=True)
        return

    url = f"https://api.github.com/repos/{repository}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = requests.post(url, json={"body": markdown_body}, headers=headers, timeout=15)
        response.raise_for_status()
        print(f"llm-cost-lint: posted cost report as comment on PR #{pr_number}", flush=True)
    except Exception as exc:
        print(f"::warning::llm-cost-lint: failed to post PR comment — {exc}", flush=True)
