#!/usr/bin/env python3
"""Add the HPO ancestor closure to an existing database.

    python scripts/add_hpo_graph.py genes.db --obo ../diagnostic.ar-api/data/hpo/hp.obo

The closure is what lets a reported phenotype match the more specific ones a
disease is annotated with. Without it every gene on the results screen reads
"sin coincidencias": families describe symptoms in general language and the
extractor returns general terms, while disease annotations are specific.

Additive and idempotent — it only writes the `hpo_ancestros` table, and a
rerun replaces it wholesale. Run it after a build, or against a database that
predates the table, rather than rebuilding 50 minutes of NCBI and UniProt work.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genesdb.hpo import MAX_ANCESTOR_DEPTH, build_ancestor_closure, read_parents  # noqa: E402
from genesdb.schema import create_schema  # noqa: E402

log = logging.getLogger("add_hpo_graph")


def run(db_path: Path, obo_path: Path, max_depth: int = MAX_ANCESTOR_DEPTH) -> int:
    conn = sqlite3.connect(db_path)
    create_schema(conn)  # CREATE TABLE IF NOT EXISTS; existing data untouched

    # Only the terms diseases are actually annotated with need a closure. The
    # full ontology is 19,934 terms; this is the 8,728 that can ever be matched.
    terms = {row[0] for row in conn.execute("SELECT DISTINCT hpo_id FROM sintomas_esperados")}
    if not terms:
        raise SystemExit("no rows in sintomas_esperados — is this the right database?")

    parents = read_parents(obo_path)
    rows = build_ancestor_closure(parents, terms, max_depth)

    conn.execute("DELETE FROM hpo_ancestros")
    conn.executemany(
        "INSERT OR REPLACE INTO hpo_ancestros (hpo_id, ancestro_id, distancia) VALUES (?, ?, ?)",
        rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO build_info (clave, valor) VALUES ('hpo_ancestor_depth', ?)",
        (str(max_depth),),
    )
    conn.commit()
    conn.close()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path)
    parser.add_argument("--obo", type=Path, required=True, help="path to hp.obo")
    parser.add_argument("--max-depth", type=int, default=MAX_ANCESTOR_DEPTH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    written = run(args.db, args.obo, args.max_depth)
    print(f"{written} ancestor edges written to {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
