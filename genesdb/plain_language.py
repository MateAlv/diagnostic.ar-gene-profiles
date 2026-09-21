"""Rewrite each gene's technical summary into plain Spanish.

A build-time pass, deliberately: it is reviewable before shipping, costs
nothing per request, and cannot invent text for a gene at the moment a family
is reading the screen. The result is stored in `resumen_llano_es` and the
technical source is never overwritten.

    python -m genesdb.plain_language --db genes.db

Nothing here is a diagnosis. The rewrite describes what a gene does in the
body; it must not mention prognosis, treatment or likelihood of disease.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .codex import CodexError, CodexQuotaError
from .llm import resolve

log = logging.getLogger("genesdb.plain_language")

MAX_CHARS = 300

BATCH_SIZE = 25

SCHEMA = {
    "type": "object",
    "properties": {
        "genes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "simbolo": {"type": "string"},
                    "resumen_llano_es": {"type": "string"},
                },
                "required": ["simbolo", "resumen_llano_es"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["genes"],
    "additionalProperties": False,
}

PROMPT = """Sos un divulgador médico. Reescribí la descripción técnica de CADA UNO de \
estos genes en español rioplatense claro, para una familia sin formación médica que está \
buscando un diagnóstico para su hijo.

Reglas estrictas para cada gen:
- Máximo 2 oraciones, menos de {max_chars} caracteres.
- Explicá qué hace el gen en el cuerpo, en lenguaje cotidiano. Evitá jerga (no digas \
'casete de unión a ATP', 'fosforilación', 'dominio regulador').
- NO menciones diagnóstico, pronóstico, tratamiento ni probabilidad de enfermedad.
- NO inventes nada que no esté en el texto técnico de ESE gen.
- Si el texto técnico está vacío o no alcanza, devolvé string vacío para ese gen.

Devolvé una entrada por cada gen recibido, con su símbolo exacto. Son {count} genes.

Genes:
{payload}"""

# Words that would turn a description into medical advice.
_FORBIDDEN = re.compile(
    r"\b(diagn[óo]stic\w*|pron[óo]stic\w*|tratamiento|cura(?:r|ble)?|terapia|"
    r"probabilidad de (?:tener|padecer)|usted tiene|su hijo tiene)\b",
    re.IGNORECASE,
)


def validate(text: str) -> tuple[bool, str]:
    """Reject a rewrite that is too long or strays into medical advice."""
    text = (text or "").strip()
    if not text:
        return True, ""
    if len(text) > MAX_CHARS:
        return False, f"too long ({len(text)} chars)"
    match = _FORBIDDEN.search(text)
    if match:
        return False, f"forbidden term: {match.group(0)!r}"
    return True, ""


def rewrite_batch(rows: list[sqlite3.Row], *, model: str | None = None,
                  timeout: int = 600, complete=None) -> dict[str, str]:
    """Rewrite a batch of genes in one call, returning {symbol: text}.

    Batching is what makes a full run practical: one call per gene would be
    ~19 hours, and the per-call overhead dominates the actual work.
    """
    payload = json.dumps([{
        "simbolo": row["simbolo"],
        "nombre_completo": row["nombre_completo"],
        "resumen_tecnico": (row["tecnico_resumen_es"] or "").strip()[:700],
        "enfermedades": [d.get("nombre", "") for d in json.loads(row["enfermedades_es"] or "[]")][:3],
    } for row in rows], ensure_ascii=False)

    response = (complete or resolve())(
        PROMPT.format(max_chars=MAX_CHARS, count=len(rows), payload=payload),
        SCHEMA, model=model, timeout=timeout,
    )

    wanted = {row["simbolo"] for row in rows}
    out: dict[str, str] = {}
    for item in response.get("genes", []):
        symbol = (item.get("simbolo") or "").strip().upper()
        text = (item.get("resumen_llano_es") or "").strip()
        if symbol not in wanted:
            log.warning("model returned an unrequested gene %r — ignored", symbol)
            continue
        ok, reason = validate(text)
        if not ok:
            log.warning("%s: rejected — %s", symbol, reason)
            continue
        if text:
            out[symbol] = text

    if len(out) < len(rows):
        log.debug("batch returned %d/%d usable rewrites", len(out), len(rows))
    return out


def run(db_path: Path, *, limit: int | None = None, only: list[str] | None = None,
        model: str | None = None, overwrite: bool = False,
        batch_size: int = BATCH_SIZE, workers: int = 3,
        backend: str | None = None) -> None:
    """Fill `resumen_llano_es`, additively.

    Only that one column is ever written. Nothing already in the database is
    removed or overwritten — a gene that already has a rewrite is skipped
    unless `overwrite` is passed, and a rejected rewrite leaves the column
    untouched rather than blanking it.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = "SELECT * FROM genes WHERE tecnico_resumen_es IS NOT NULL AND tecnico_resumen_es != ''"
    params: list[object] = []
    if not overwrite:
        query += " AND (resumen_llano_es IS NULL OR resumen_llano_es = '')"
    if only:
        query += f" AND simbolo IN ({','.join('?' * len(only))})"
        params += [s.strip().upper() for s in only]
    query += " ORDER BY simbolo"
    if limit:
        query += f" LIMIT {int(limit)}"

    rows = conn.execute(query, params).fetchall()
    if not rows:
        log.info("nothing to do")
        conn.close()
        return

    batches = [rows[i:i + batch_size] for i in range(0, len(rows), batch_size)]
    log.info("%d genes in %d batches of %d, %d workers",
             len(rows), len(batches), batch_size, workers)

    written = failed = 0
    started = time.monotonic()

    complete = resolve(backend)
    quota_exhausted = threading.Event()

    def work(batch: list[sqlite3.Row]) -> dict[str, str]:
        # Once the subscription is out of credit every remaining batch will fail
        # the same way, several minutes at a time. Stop asking.
        if quota_exhausted.is_set():
            return {}
        try:
            return rewrite_batch(batch, model=model, complete=complete)
        except CodexQuotaError as exc:
            if not quota_exhausted.is_set():
                quota_exhausted.set()
                log.error("codex usage limit reached — stopping. Rerun after it "
                          "resets; genes already written are skipped. %s",
                          str(exc).strip().splitlines()[-1] if str(exc).strip() else "")
            return {}
        except CodexError as exc:
            log.warning("batch failed — %s", exc)
            return {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for index, result in enumerate(pool.map(work, batches), start=1):
            if not result:
                failed += 1
            for symbol, text in result.items():
                conn.execute("UPDATE genes SET resumen_llano_es = ? WHERE simbolo = ?",
                             (text, symbol))
                written += 1
            conn.commit()
            rate = index / max(time.monotonic() - started, 1e-9)
            eta = (len(batches) - index) / rate / 60 if rate else 0
            log.info("batch %d/%d — %d written, %d failed, ~%.0f min left",
                     index, len(batches), written, failed, eta)

    conn.close()
    if quota_exhausted.is_set():
        remaining = len(rows) - written
        log.error("stopped on the usage limit: %d written, %d genes still "
                  "without a summary. Rerun after the quota resets.", written, remaining)
    else:
        log.info("done: %d written, %d batches failed", written, failed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rewrite gene summaries in plain Spanish.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--model")
    parser.add_argument("--backend", choices=["claude", "codex"],
                        help="which CLI to call (default claude)")
    parser.add_argument("--overwrite", action="store_true",
                        help="redo genes that already have a rewrite")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help=f"genes per codex call (default {BATCH_SIZE})")
    parser.add_argument("--workers", type=int, default=3,
                        help="concurrent codex calls (default 3)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    run(args.db, limit=args.limit, only=args.only, model=args.model,
        overwrite=args.overwrite, batch_size=args.batch_size, workers=args.workers,
        backend=args.backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
