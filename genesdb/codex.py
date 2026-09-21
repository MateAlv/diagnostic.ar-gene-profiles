"""Call an LLM through the `codex` CLI instead of a paid API key.

The subscription is already paid for, so build-time language work goes through
the CLI. See the platform's `docs/LLM_ACCESS.md`.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class CodexError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("codex") is not None


def complete(prompt: str, schema: dict[str, Any], *, model: str | None = None,
             timeout: int = 180) -> dict[str, Any]:
    """Run one `codex exec` call and return its structured response.

    stdin is closed explicitly: `codex exec` blocks forever reading stdin when
    it is piped or empty, which looks exactly like a latency problem.
    """
    if not available():
        raise CodexError("the `codex` CLI is not on PATH")

    with tempfile.TemporaryDirectory(prefix="genesdb-codex-") as tmp:
        schema_path = Path(tmp) / "schema.json"
        out_path = Path(tmp) / "out.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")

        command = ["codex", "exec", "--skip-git-repo-check",
                   "--output-schema", str(schema_path), "-o", str(out_path)]
        if model:
            command += ["-m", model]
        command.append(prompt)

        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexError(f"codex timed out after {timeout}s") from exc

        if result.returncode != 0:
            raise CodexError(f"codex exited {result.returncode}: {result.stderr[-400:]}")
        if not out_path.exists():
            raise CodexError("codex produced no output file")

        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CodexError(f"codex returned malformed JSON: {exc}") from exc
