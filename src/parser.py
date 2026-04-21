"""
Scans Python source files for AWS Bedrock and Azure OpenAI SDK call sites
using AST parsing.

Returns a list of LLMCall objects describing each detected invocation,
including provider, model ID, file location, and max_tokens when available.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class LLMCall:
    provider: str           # "aws_bedrock" | "azure_openai"
    model_id: str           # e.g. "anthropic.claude-3-5-sonnet-20241022-v2:0"
    file_path: str          # repo-relative or absolute path
    line_number: int        # 1-based line of the call expression
    max_tokens: Optional[int] = None  # extracted from call args when static


# ── Public interface ──────────────────────────────────────────────────────────

def parse_file(path: str | Path) -> list[LLMCall]:
    """Parse a single Python file and return all detected LLM call sites."""
    path = Path(path)
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    visitor = _LLMCallVisitor(file_path=str(path))
    visitor.visit(tree)
    return visitor.calls


def parse_files(paths: list[str | Path]) -> list[LLMCall]:
    """Parse multiple Python files and return all detected LLM call sites."""
    results: list[LLMCall] = []
    for path in paths:
        results.extend(parse_file(path))
    return results


# ── AST visitor ──────────────────────────────────────────────────────────────

# Bedrock client methods that represent model invocations.
_BEDROCK_INVOKE_METHODS = {
    "invoke_model",
    "converse",
    "invoke_model_with_response_stream",
}


class _LLMCallVisitor(ast.NodeVisitor):
    """
    Two-pass visitor:
      1. First pass (_collect_bedrock_clients) records variable names that are
         assigned a boto3 bedrock-runtime client. This narrows Bedrock detection
         to known client variables and reduces false positives.
      2. visit_Call walks every call node and delegates to provider-specific
         checkers.
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.calls: list[LLMCall] = []
        # Names bound to boto3.client("bedrock-runtime") in this file.
        self._bedrock_client_names: set[str] = field(default_factory=set)  # type: ignore[assignment]
        self._bedrock_client_names = set()

    # ── First pass: collect boto3 bedrock-runtime client names ───────────────

    def visit_Module(self, node: ast.Module) -> None:  # noqa: N802
        self._collect_bedrock_clients(node)
        self.generic_visit(node)

    def _collect_bedrock_clients(self, tree: ast.AST) -> None:
        """
        Walk all assignments looking for patterns like:

            client = boto3.client("bedrock-runtime")
            client = boto3.client("bedrock-runtime", region_name="us-east-1")
            client = session.client("bedrock-runtime")

        False positive risk: any .client("bedrock-runtime") call from any
        object named 'boto3' or 'session' will be treated as a Bedrock client.
        This is intentional — it's the common boto3 usage pattern.
        """
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Call):
                continue

            call = node.value
            service = _kwarg_str(call, "service_name") or _first_arg_str(call)
            if service != "bedrock-runtime":
                continue

            # Record each name on the left-hand side of the assignment.
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._bedrock_client_names.add(target.id)

    # ── Second pass: detect call sites ───────────────────────────────────────

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        self._check_bedrock(node)
        self._check_azure_openai(node)
        self.generic_visit(node)

    # ── AWS Bedrock ───────────────────────────────────────────────────────────

    def _check_bedrock(self, node: ast.Call) -> None:
        """
        Matches calls of the form:

            <expr>.invoke_model(modelId="...", ...)
            <expr>.converse(modelId="...", ...)
            <expr>.invoke_model_with_response_stream(modelId="...", ...)

        Where <expr> is either:
          - A name previously identified as a bedrock-runtime client, OR
          - Any attribute expression (handles chained calls like
            `get_client().invoke_model(...)` which we cannot statically resolve).

        False positive risk:
          - Any object with a method named invoke_model/converse that also
            accepts a 'modelId' keyword will be flagged. This is unlikely in
            practice but theoretically possible with non-boto3 libraries.
          - When _bedrock_client_names is empty (e.g. the client is returned
            from a helper function), we fall back to method-name matching only,
            increasing false positive risk.
        """
        if not isinstance(node.func, ast.Attribute):
            return

        method_name = node.func.attr
        if method_name not in _BEDROCK_INVOKE_METHODS:
            return

        # If we identified any bedrock clients, require the receiver to be one.
        # If we found none (dynamic client creation), accept any receiver so
        # we don't silently miss real calls.
        receiver = node.func.value
        if self._bedrock_client_names:
            if isinstance(receiver, ast.Name) and receiver.id not in self._bedrock_client_names:
                return
            # Non-Name receivers (attribute chains, calls) are always checked —
            # we cannot resolve them statically.

        model_id = _kwarg_str(node, "modelId") or "unknown"
        max_tokens = _kwarg_int(node, "maxTokens")

        self.calls.append(LLMCall(
            provider="aws_bedrock",
            model_id=model_id,
            file_path=self.file_path,
            line_number=node.lineno,
            max_tokens=max_tokens,
        ))

    # ── Azure OpenAI ─────────────────────────────────────────────────────────

    def _check_azure_openai(self, node: ast.Call) -> None:
        """
        Matches calls of the form:

            <expr>.chat.completions.create(model="...", ...)

        where <expr> is any variable (typically an AzureOpenAI client instance).
        The attribute chain must end in …chat.completions.create to avoid
        matching unrelated .create() calls elsewhere in the codebase.

        False positive risk:
          - Any object that exposes a .chat.completions.create() interface —
            including the standard openai.OpenAI client — will be matched. This
            is intentional: the cost model is the same and we'd rather over-
            report than silently miss an Azure call hidden behind a base class.
          - Aliased attribute chains (e.g. comps = client.chat.completions;
            comps.create(...)) will NOT be detected because we only inspect the
            call node's own attribute chain, not prior assignments.
        """
        chain = _attr_chain(node.func)

        # Require the call to look like "*.chat.completions.create"
        if not chain.endswith("chat.completions.create"):
            return

        model_id = _kwarg_str(node, "model") or "unknown"
        max_tokens = _kwarg_int(node, "max_tokens")

        self.calls.append(LLMCall(
            provider="azure_openai",
            model_id=model_id,
            file_path=self.file_path,
            line_number=node.lineno,
            max_tokens=max_tokens,
        ))


# ── AST helpers ───────────────────────────────────────────────────────────────

def _attr_chain(node: ast.expr) -> str:
    """
    Reconstruct a dotted attribute chain from an AST node.

    ast.Attribute(value=ast.Attribute(value=ast.Name(id='a'), attr='b'), attr='c')
    → "a.b.c"

    Returns an empty string for non-attribute/non-name nodes.
    """
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _first_arg_str(node: ast.Call) -> Optional[str]:
    """Return the first positional argument if it is a string literal."""
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _kwarg_str(node: ast.Call, name: str) -> Optional[str]:
    """Return the value of a keyword argument if it is a string literal."""
    for kw in node.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _kwarg_int(node: ast.Call, name: str) -> Optional[int]:
    """Return the value of a keyword argument if it is an integer literal."""
    for kw in node.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
            return kw.value.value
    return None
