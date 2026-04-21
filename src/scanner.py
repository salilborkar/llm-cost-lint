"""AST-based scanner for AWS Bedrock and Azure OpenAI SDK call sites."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from config import Config


@dataclass
class CallSite:
    file: str
    line: int
    provider: Literal["bedrock", "azure_openai"]
    model_id: str
    input_tokens: int
    output_tokens: int
    is_streaming: bool


_BEDROCK_METHODS = {"invoke_model", "converse", "invoke_model_with_response_stream"}
_AZURE_METHODS = {"create"}
_AZURE_NAMESPACES = {"chat.completions", "completions", "embeddings"}

_UNKNOWN_MODEL = "unknown"


def scan_files(paths: list[Path], cfg: Config) -> list[CallSite]:
    results: list[CallSite] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        results.extend(_scan_tree(tree, str(path), cfg))
    return results


def _scan_tree(tree: ast.AST, filename: str, cfg: Config) -> list[CallSite]:
    visitor = _CallVisitor(filename, cfg)
    visitor.visit(tree)
    return visitor.results


class _CallVisitor(ast.NodeVisitor):
    def __init__(self, filename: str, cfg: Config) -> None:
        self.filename = filename
        self.cfg = cfg
        self.results: list[CallSite] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        self._check_bedrock(node)
        self._check_azure(node)
        self.generic_visit(node)

    # ── Bedrock ───────────────────────────────────────────────────────────────

    def _check_bedrock(self, node: ast.Call) -> None:
        method = _attr_name(node.func)
        if method not in _BEDROCK_METHODS:
            return

        model_id = _extract_kwarg_str(node, "modelId") or _UNKNOWN_MODEL
        model_id = self.cfg.model_map.get(model_id, model_id)

        input_tok, output_tok = self._token_counts(node)

        self.results.append(CallSite(
            file=self.filename,
            line=node.lineno,
            provider="bedrock",
            model_id=model_id,
            input_tokens=input_tok,
            output_tokens=output_tok,
            is_streaming=method == "invoke_model_with_response_stream",
        ))

    # ── Azure OpenAI ─────────────────────────────────────────────────────────

    def _check_azure(self, node: ast.Call) -> None:
        if _attr_name(node.func) != "create":
            return
        chain = _attr_chain(node.func)
        # expect something like client.chat.completions.create
        if not any(ns in chain for ns in _AZURE_NAMESPACES):
            return

        model_id = _extract_kwarg_str(node, "model") or _UNKNOWN_MODEL
        model_id = self.cfg.model_map.get(model_id, model_id)

        input_tok, output_tok = self._token_counts(node)
        is_streaming = _extract_kwarg_bool(node, "stream")

        self.results.append(CallSite(
            file=self.filename,
            line=node.lineno,
            provider="azure_openai",
            model_id=model_id,
            input_tokens=input_tok,
            output_tokens=output_tok,
            is_streaming=is_streaming,
        ))

    def _token_counts(self, node: ast.Call) -> tuple[int, int]:
        inp = (
            _extract_kwarg_int(node, "max_tokens")
            or _extract_kwarg_int(node, "maxTokenCount")
            or self.cfg.default_input_tokens
        )
        return self.cfg.default_input_tokens, inp


# ── AST helpers ───────────────────────────────────────────────────────────────

def _attr_name(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _attr_chain(node: ast.expr) -> str:
    """Return dot-joined attribute chain, e.g. 'client.chat.completions.create'."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _extract_kwarg_str(node: ast.Call, name: str) -> str | None:
    for kw in node.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _extract_kwarg_int(node: ast.Call, name: str) -> int | None:
    for kw in node.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
            return kw.value.value
    return None


def _extract_kwarg_bool(node: ast.Call, name: str) -> bool:
    for kw in node.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant):
            return bool(kw.value.value)
    return False
