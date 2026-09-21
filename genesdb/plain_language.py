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
from pathlib import Path

from .codex import CodexError, complete

log = logging.getLogger("genesdb.plain_language")

MAX_CHARS = 300

SCHEMA = {
    "type": "object",
    "properties": {"resumen_llano_es": {"type": "string"}},
    "required": ["resumen_llano_es"],
    "additionalProperties": False,
}

PROMPT = """Sos un divulgador médico. Reescribí la descripción técnica de este gen en \
español rioplatense claro, para una familia sin formación médica que está buscando un \
diagnóstico para su hijo.

Reglas estrictas:
- Máximo 2 oraciones, menos de {max_chars} caracteres.
- Explicá qué hace el gen en el cuerpo, en lenguaje cotidiano. Evitá jerga (no digas \
'casete de unión a ATP', 'fosforilación', 'dominio regulador').
- NO menciones diagnóstico, pronóstico, tratamiento ni probabilidad de enfermedad.
- NO inventes nada que no esté en el texto técnico.
- Si el texto técnico está vacío o no alcanza, devolvé string vacío.

Datos del gen:
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


def rewrite_one(row: sqlite3.Row, *, model: str | None = None,
                timeout: int = 180) -> str | None:
    technical = (row["tecnico_resumen_es"] or "").strip()
    if not technical:
        return None

    payload = json.dumps({
        "simbolo": row["simbolo"],
        "nombre_completo": row["nombre_completo"],
        "resumen_tecnico": technical[:900],
        "enfermedades": [d.get("nombre", "") for d in json.loads(row["enfermedades_es"] or "[]")],
    }, ensure_ascii=False)

    response = complete(
        PROMPT.format(max_chars=MAX_CHARS, payload=payload),
        SCHEMA, model=model, timeout=timeout,
    )
    text = (response.get("resumen_llano_es") or "").strip()

    ok, reason = validate(text)
    if not ok:
        log.warning("%s: rejected — %s", row["simbolo"], reason)
        return None
    return text


def run(db_path: Path, *, limit: int | None = None, only: list[str] | None = None,
        model: str | None = None, overwrite: bool = False) -> None:
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
    log.info("%d genes to rewrite", len(rows))

    done = skipped = failed = 0
    for index, row in enumerate(rows, start=1):
        try:
            text = rewrite_one(row, model=model)
        except CodexError as exc:
            log.warning("%s: codex failed — %s", row["simbolo"], exc)
            failed += 1
            continue

        if text is None:
            skipped += 1
            continue

        conn.execute("UPDATE genes SET resumen_llano_es = ? WHERE simbolo = ?",
                     (text, row["simbolo"]))
        done += 1
        if done % 20 == 0:
            conn.commit()
            log.info("%d/%d written (%d skipped, %d failed)", index, len(rows), skipped, failed)

    conn.commit()
    conn.close()
    log.info("done: %d written, %d skipped, %d failed", done, skipped, failed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rewrite gene summaries in plain Spanish.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--model")
    parser.add_argument("--overwrite", action="store_true",
                        help="redo genes that already have a rewrite")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    run(args.db, limit=args.limit, only=args.only, model=args.model, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    sys.exit(main())
