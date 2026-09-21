"""Pick an LLM backend.

Two CLIs, one contract. Both are subscriptions already paid for, and having
both matters: exhausting one quota mid-run should not stop the work.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from . import claude_cli, codex


class Backend(Protocol):
    def __call__(self, prompt: str, schema: dict[str, Any], *,
                 model: str | None = ..., timeout: int = ...) -> dict[str, Any]: ...


BACKENDS: dict[str, tuple[Callable[..., dict[str, Any]], Callable[[], bool]]] = {
    "codex": (codex.complete, codex.available),
    "claude": (claude_cli.complete, claude_cli.available),
}

DEFAULT = "claude"


def resolve(name: str | None = None) -> Backend:
    """Return the backend's `complete`, or raise if it is not installed."""
    chosen = (name or DEFAULT).lower()
    if chosen not in BACKENDS:
        raise SystemExit(f"unknown backend {chosen!r}; choose from {sorted(BACKENDS)}")

    complete, available = BACKENDS[chosen]
    if not available():
        raise SystemExit(f"the `{chosen}` CLI is not on PATH")
    return complete
