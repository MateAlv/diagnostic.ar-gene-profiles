"""Translate the technical gene summaries that are still in English.

The build translates `tecnico_resumen_es` and `tecnico_funcion_es` from their
English sources, but a run interrupted partway — by a quota wall or by the web
endpoint blocking the host — leaves rows holding the untranslated source. The
two columns drift independently: a gene can have a Spanish summary and an
English function, or the reverse.

There is no "translated" flag to select on, so the English rows are found by
reading the text itself. `looks_english` counts function words, which separates
the two languages cleanly on prose of this length and needs no model call.

    python -m genesdb.translate_summaries --db genes.db

Additive: a row is only ever overwritten with a translation of its own text,
and a batch that fails leaves every row it covered untouched.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .codex import CodexError, CodexQuotaError
from .llm import resolve

log = logging.getLogger("genesdb.translate_summaries")

# Small batches: these are paragraphs, not names, and a failed batch costs
# everything in it.
BATCH_SIZE = 8

FIELDS = ("tecnico_resumen_es", "tecnico_funcion_es")

_ES = re.compile(
    r"\b(el|la|los|las|un|una|que|del|para|con|por|se|es|su|como|esta|este|"
    r"codifica|proteína|gen|cuyo|sobre|entre)\b",
    re.IGNORECASE,
)
_EN = re.compile(
    r"\b(the|this|of|and|is|are|which|with|encodes|protein|gene|has|been|"
    r"that|from|its|also|may|these)\b",
    re.IGNORECASE,
)


def looks_english(text: str | None) -> bool:
    """True when the text reads as English rather than Spanish.

    Both languages are scored by function-word count and the higher wins. A tie
    counts as Spanish: the cost of re-translating a Spanish row is a wasted call
    and a possible regression, while missing an English one is caught by the
    next run.
    """
    if not text or not text.strip():
        return False
    return len(_EN.findall(text)) > len(_ES.findall(text))


SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "es": {"type": "string"},
                },
                "required": ["id", "es"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}

PROMPT = """Traducí al español estos textos técnicos sobre genes humanos, usando \
la nomenclatura médica y de biología molecular estándar en español.

Reglas:
- Conservá los símbolos de genes y proteínas (TCF4, ABCD3), los epónimos y los \
números de tipo exactamente como están.
- Conservá los términos técnicos que no se traducen en la literatura en español \
('helix-loop-helix', 'E-box', 'cassette').
- Traducí el texto completo. No resumas, no agregues ni quites información.
- Si un texto ya está en español, devolvelo igual.
- Devolvé una entrada por cada texto recibido, con su 'id' exacto.

Textos ({count}):
{payload}"""


def collect_pending(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Every (symbol, column, english_text) still needing translation."""
    columns = ", ".join(FIELDS)
    pending: list[tuple[str, str, str]] = []
    for row in conn.execute(f"SELECT simbolo, {columns} FROM genes"):
        simbolo = row[0]
        for field, text in zip(FIELDS, row[1:]):
            if looks_english(text):
                pending.append((simbolo, field, text))
    return pending


def _translate_batch(items: list[tuple[str, str, str]], *, model: str | None,
                     timeout: int, complete=None) -> dict[str, str]:
    payload = [{"id": f"{simbolo}:{field}", "text": text} for simbolo, field, text in items]
    response = (complete or resolve())(
        PROMPT.format(count=len(payload), payload=json.dumps(payload, ensure_ascii=False)),
        SCHEMA, model=model, timeout=timeout,
    )

    wanted = {item["id"] for item in payload}
    out: dict[str, str] = {}
    for entry in response.get("translations", []):
        key = (entry.get("id") or "").strip()
        translated = (entry.get("es") or "").strip()
        # Only accept ids we sent. A hallucinated key would otherwise be written
        # to whichever gene it happened to name.
        if key not in wanted or not translated:
            continue
        # A reply that is still English means the model echoed the source rather
        # than translating it; keeping it would just hide the row from the next
        # run's detector.
        if looks_english(translated):
            log.warning("%s came back still in English — leaving it", key)
            continue
        out[key] = translated
    return out


def apply_cache(conn: sqlite3.Connection, cache: dict[str, str]) -> int:
    """Write cached translations back. Additive: untranslated rows keep their text."""
    updated = 0
    for key, translated in cache.items():
        simbolo, _, field = key.partition(":")
        if field not in FIELDS:
            continue
        cursor = conn.execute(
            f"UPDATE genes SET {field} = ? WHERE simbolo = ? AND {field} IS NOT ?",
            (translated, simbolo, translated),
        )
        updated += cursor.rowcount
    conn.commit()
    return updated


def run(db_path: Path, *, cache_path: Path | None = None, batch_size: int = BATCH_SIZE,
        workers: int = 3, model: str | None = None, limit: int | None = None,
        backend: str | None = None) -> None:
    complete = resolve(backend)
    conn = sqlite3.connect(db_path)
    cache_path = cache_path or db_path.with_name(db_path.stem + ".summaries.json")

    cache: dict[str, str] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable cache: %s", exc)

    pending = [item for item in collect_pending(conn)
               if f"{item[0]}:{item[1]}" not in cache]
    if limit:
        pending = pending[:limit]
    log.info("%d texts to translate (%d already cached)", len(pending), len(cache))

    if pending:
        batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
        started = time.monotonic()
        failed = 0
        stopped = False

        def work(batch):
            nonlocal stopped
            if stopped:
                return {}
            try:
                return _translate_batch(batch, model=model, timeout=600, complete=complete)
            except CodexQuotaError as exc:
                # The quota is gone; every later batch would burn a minute
                # rediscovering the same wall and log it as an ordinary failure.
                log.error("quota exhausted — stopping: %s", exc)
                stopped = True
                return {}
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

        if stopped:
            log.error("stopped early on quota; cached work is saved, rerun to resume")

    updated = apply_cache(conn, cache)
    remaining = len(collect_pending(conn))
    conn.close()
    log.info("done: %d columns updated, %d still in English", updated, remaining)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Translate gene summaries left in English by an interrupted build.")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--model")
    parser.add_argument("--backend", choices=["claude", "codex"],
                        help="which CLI to call (default claude)")
    parser.add_argument("--limit", type=int, help="translate only the first N (smoke test)")
    parser.add_argument("--report", action="store_true",
                        help="count what is pending and exit without calling the model")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    if args.report:
        conn = sqlite3.connect(args.db)
        pending = collect_pending(conn)
        conn.close()
        by_field = {field: sum(1 for _, f, _ in pending if f == field) for field in FIELDS}
        print(f"{len(pending)} texts still in English across {len({p[0] for p in pending})} genes")
        for field, count in by_field.items():
            print(f"  {field}: {count}")
        return 0

    run(args.db, batch_size=args.batch_size, workers=args.workers,
        model=args.model, limit=args.limit, backend=args.backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
