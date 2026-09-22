#!/usr/bin/env python3
"""Check a built database before it is copied to the server.

The database is built locally and shipped with scp, so nothing between this
machine and the results screen looks at it. A translation stage that stopped
partway once put 809 English texts on a Spanish page in front of families, and
nothing failed — the build reported success, the copy succeeded, and the defect
was found by reading the staging site.

    python scripts/verify_db.py genes.db

Exits non-zero when a check fails, so it can gate the copy:

    python scripts/verify_db.py genes.db && scp genes.db nodo0:...

Thresholds are set just above what a good build currently produces, so ordinary
variation passes and a stage that silently did nothing does not.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genesdb.translate_summaries import FIELDS, looks_english  # noqa: E402

# The model's classifier has one output per gene, so the row count is not a
# preference: a short database means genes rank with no description at all.
EXPECTED_GENES = 5228

# A handful of texts legitimately read as English — a summary that is mostly a
# protein name and an accession, for instance. Hundreds do not.
MAX_ENGLISH = 25


def check(name: str, ok: bool, detail: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return ok


def verify(path: Path) -> bool:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    results = []

    genes = conn.execute("SELECT COUNT(*) FROM genes").fetchone()[0]
    results.append(check("gene count", genes == EXPECTED_GENES,
                         f"{genes} rows, expected {EXPECTED_GENES}"))

    english = 0
    for row in conn.execute(f"SELECT simbolo, {', '.join(FIELDS)} FROM genes"):
        english += sum(1 for text in row[1:] if looks_english(text))
    results.append(check("untranslated text", english <= MAX_ENGLISH,
                         f"{english} texts still read as English (limit {MAX_ENGLISH})"))

    # A gene with no summary renders as a bare symbol, which is the one thing
    # the description database exists to prevent.
    missing = conn.execute(
        "SELECT COUNT(*) FROM genes WHERE COALESCE(TRIM(tecnico_resumen_es), '') = ''"
    ).fetchone()[0]
    results.append(check("summary coverage", missing <= genes * 0.02,
                         f"{missing} genes with no summary ({missing / max(genes, 1):.1%})"))

    symptoms = conn.execute("SELECT COUNT(*) FROM sintomas_esperados").fetchone()[0]
    results.append(check("expected symptoms", symptoms > 100_000,
                         f"{symptoms} rows"))

    with_symptoms = conn.execute(
        "SELECT COUNT(DISTINCT omim) FROM sintomas_esperados"
    ).fetchone()[0]
    results.append(check("diseases with symptoms", with_symptoms > 5_000,
                         f"{with_symptoms} OMIM codes"))

    # Without the closure a reported "Convulsiones" matches nothing a disease
    # is annotated with, and the results screen reads "sin coincidencias" on
    # every gene — which is how it shipped once already.
    graph = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'hpo_ancestros'"
    ).fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM hpo_ancestros").fetchone()[0] if graph else 0
    results.append(check("HPO ancestor graph", edges > 20_000,
                         f"{edges} edges" if graph else "table missing — run scripts/add_hpo_graph.py"))

    build = dict(conn.execute("SELECT clave, valor FROM build_info").fetchall())
    results.append(check("build provenance", bool(build.get("hpo_version")),
                         build.get("hpo_version") or "no HPO version recorded"))

    conn.close()
    return all(results)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path)
    args = parser.parse_args(argv)

    if not args.db.exists():
        print(f"FAIL  no database at {args.db}")
        return 1

    ok = verify(args.db)
    print("\nOK — safe to copy." if ok else "\nNOT safe to copy.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
