"""Translate and rewrite through the `claude` CLI.

A second LLM backend beside `codex.py`, for the same reason that one exists at
all: the subscriptions are already paid for. Having two matters in practice —
a full rewrite pass exhausted the codex quota partway through, and the work
could continue on this one instead of waiting hours for a reset.

Same contract as `codex.complete`, so a caller can switch backends without
knowing which is in use.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import Any

from .codex import CodexError, CodexQuotaError

log = logging.getLogger(__name__)

DEFAULT_MODEL = "opus"


def available() -> bool:
    return shutil.which("claude") is not None


def complete(prompt: str, schema: dict[str, Any], *, model: str | None = None,
             timeout: int = 180) -> dict[str, Any]:
    """Run one `claude -p` call and return its structured response.

    stdin is closed explicitly: like codex, the CLI reads it when attached and
    would otherwise block forever, which looks exactly like a slow model.
    """
    if not available():
        raise CodexError("the `claude` CLI is not on PATH")

    # --json-schema takes the schema inline, not a path.
    command = [
        "claude", "-p",
        "--model", model or DEFAULT_MODEL,
        "--json-schema", json.dumps(schema),
        prompt,
    ]

    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CodexError(f"claude timed out after {timeout}s") from exc

    if result.returncode != 0:
        detail = f"{result.stdout[-600:]}\n{result.stderr[-600:]}".strip()
        if "usage limit" in detail or "rate limit" in detail.lower():
            raise CodexQuotaError(detail)
        raise CodexError(f"claude exited {result.returncode}: {detail[-600:]}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CodexError(f"claude returned malformed JSON: {exc}") from exc
