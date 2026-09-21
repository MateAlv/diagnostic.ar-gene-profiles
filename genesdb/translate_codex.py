"""Translate through the `codex` CLI instead of the public web endpoint.

The unauthenticated Google endpoint blocks an IP that calls it at volume, which
stopped two full builds. The subscription is already paid for, and for clinical
text it is also *better*: it knows standard Spanish medical nomenclature and
keeps eponyms and type numbers intact, where a general translator renders them
literally.

    python -m genesdb.translate_codex --db genes.db

Only disease names and descriptions are handled here. Symptom names come from
the official HPO Spanish labels and need no translation at all.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .codex import CodexError, CodexQuotaError
from .llm import resolve

log = logging.getLogger("genesdb.translate_codex")

BATCH_SIZE = 50

SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"en": {"type": "string"}, "es": {"type": "string"}},
                "required": ["en", "es"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}

NAME_PROMPT = """Traducí al español estos nombres de enfermedades genéticas, usando \
la nomenclatura médica estándar en español.

Reglas:
- Conservá los epónimos ('Bardet-Biedl', 'Charcot-Marie-Tooth') y los números de tipo.
- Si un nombre ya está en español, devolvelo igual.
- No agregues explicaciones ni texto extra.
- Devolvé una entrada por cada nombre recibido, con el texto original exacto en 'en'.

Nombres ({count}):
{payload}"""


def _translate_batch(texts: list[str], *, model: str | None, timeout: int,
                     complete=None) -> dict[str, str]:
    response = (complete or resolve())(
        NAME_PROMPT.format(count=len(texts), payload=json.dumps(texts, ensure_ascii=False)),
        SCHEMA, model=model, timeout=timeout,
    )
    wanted = set(texts)
    out: dict[str, str] = {}
    for item in response.get("translations", []):
        source = (item.get("en") or "").strip()
        translated = (item.get("es") or "").strip()
        # Only accept translations for strings we actually sent: a hallucinated
        # key would otherwise poison the cache for a later build.
        if source in wanted and translated:
            out[source] = translated
    return out


def collect_untranslated(conn: sqlite3.Connection, cache: dict[str, str]) -> list[str]:
    """Disease names still carrying their English source."""
    pending: set[str] = set()
    for (raw,) in conn.execute(
        "SELECT enfermedades_es FROM genes WHERE enfermedades_es NOT IN ('', '[]')"
    ):
        for disease in json.loads(raw or "[]"):
            name = (disease.get("nombre") or "").strip()
            if name and name not in cache:
                pending.add(name)
    return sorted(pending)


def apply_cache(conn: sqlite3.Connection, cache: dict[str, str]) -> int:
    """Rewrite the stored disease names through the cache. Additive: a name with
    no translation keeps whatever it had."""
    updated = 0
    for simbolo, raw in conn.execute(
        "SELECT simbolo, enfermedades_es FROM genes WHERE enfermedades_es NOT IN ('', '[]')"
    ).fetchall():
        diseases = json.loads(raw or "[]")
        changed = False
        for disease in diseases:
            name = (disease.get("nombre") or "").strip()
            translated = cache.get(name)
            if translated and translated != name:
                disease["nombre"] = translated
                changed = True
        if changed:
            conn.execute(
                "UPDATE genes SET enfermedades_es = ? WHERE simbolo = ?",
                (json.dumps(diseases, ensure_ascii=False), simbolo),
            )
            updated += 1
    conn.commit()
    return updated


def run(db_path: Path, *, cache_path: Path | None = None, batch_size: int = BATCH_SIZE,
        workers: int = 3, model: str | None = None, limit: int | None = None,
        backend: str | None = None) -> None:
    complete = resolve(backend)
    conn = sqlite3.connect(db_path)
    cache_path = cache_path or db_path.with_suffix(".translations.json")

    cache: dict[str, str] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable cache: %s", exc)

    pending = collect_untranslated(conn, cache)
    if limit:
        pending = pending[:limit]
    log.info("%d disease names to translate (%d cached)", len(pending), len(cache))

    if pending:
        batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
        started = time.monotonic()
        failed = 0

        def work(batch: list[str]) -> dict[str, str]:
            try:
                return _translate_batch(batch, model=model, timeout=600, complete=complete)
            except CodexError as exc:
                log.warning("batch failed — %s", exc)
                return {}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, result in enumerate(pool.map(work, batches), start=1):
                if not result:
                    failed += 1
                cache.update(result)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                rate = index / max(time.monotonic() - started, 1e-9)
                log.info("batch %d/%d — %d cached, %d failed, ~%.0f min left",
                         index, len(batches), len(cache), failed,
                         (len(batches) - index) / rate / 60 if rate else 0)

    updated = apply_cache(conn, cache)
    conn.close()
    log.info("done: %d genes updated", updated)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Translate disease names through codex.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--model")
    parser.add_argument("--backend", choices=["claude", "codex"],
                        help="which CLI to call (default claude)")
    parser.add_argument("--limit", type=int, help="translate only the first N (smoke test)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    run(args.db, batch_size=args.batch_size, workers=args.workers,
        model=args.model, limit=args.limit, backend=args.backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
