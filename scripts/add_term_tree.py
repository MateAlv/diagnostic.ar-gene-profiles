#!/usr/bin/env python3
"""Add the whole HPO tree and each term's gene coverage to an existing database.

    python scripts/add_term_tree.py genes.db \\
        --obo ../diagnostic.ar-api/data/hpo/hp.obo \\
        --labels ../diagnostic.ar-api/data/hpo/hp-es.json

The wizard warns when a reported symptom is so general it barely narrows the
search, and lets the person browse down to a more specific term. That needs
three things this database did not hold: every term with its Spanish label,
every direct parent-child edge, and how many genes each term reaches.

Use the same hp.obo and hp-es.json as the symptom search (diagnostic.ar-api's
data/hpo), so a term reads the same in the search and in the dialog.

Additive and idempotent: it only writes hpo_terminos, hpo_hijos and
termino_cobertura, and a rerun replaces them wholesale.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genesdb.hpo import read_hpo_labels, read_parents  # noqa: E402
from genesdb.schema import create_schema  # noqa: E402

log = logging.getLogger("add_term_tree")


def read_obo_version(obo_path: Path) -> str:
    with obo_path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("data-version:"):
                return line.split(":", 1)[1].strip()
            if line.startswith("[Term]"):
                break
    return "unknown"


def all_ancestors(parents: dict[str, set[str]]):
    """A memoised term -> {every ancestor} function over the is_a graph."""
    cache: dict[str, frozenset[str]] = {}

    def walk(term: str) -> frozenset[str]:
        if term in cache:
            return cache[term]
        # Iterative, so a deep branch cannot hit the recursion limit.
        seen: set[str] = set()
        stack = list(parents.get(term, ()))
        while stack:
            parent = stack.pop()
            if parent in seen:
                continue
            seen.add(parent)
            if parent in cache:
                seen |= cache[parent]
            else:
                stack.extend(parents.get(parent, ()))
        cache[term] = frozenset(seen)
        return cache[term]

    return walk


def gene_coverage(
    gene_diseases: dict[str, list[str]],
    disease_terms: dict[str, set[str]],
    parents: dict[str, set[str]],
) -> dict[str, int]:
    """term -> number of genes with a disease annotated with it or beneath it."""
    ancestors = all_ancestors(parents)
    counts: dict[str, int] = defaultdict(int)
    for diseases in gene_diseases.values():
        reached: set[str] = set()
        for omim in diseases:
            for term in disease_terms.get(omim, ()):
                reached.add(term)
                reached |= ancestors(term)
        for term in reached:
            counts[term] += 1
    return dict(counts)


def run(db_path: Path, obo_path: Path, labels_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    create_schema(conn)

    parents = read_parents(obo_path)
    labels_en = read_hpo_labels(obo_path)
    labels_es = json.loads(labels_path.read_text(encoding="utf-8"))

    gene_diseases = {
        symbol: json.loads(codes or "[]")
        for symbol, codes in conn.execute("SELECT simbolo, codigo_omim FROM genes")
    }
    disease_terms: dict[str, set[str]] = defaultdict(set)
    for omim, hpo_id in conn.execute("SELECT omim, hpo_id FROM sintomas_esperados"):
        disease_terms[omim].add(hpo_id)
    if not gene_diseases or not disease_terms:
        raise SystemExit("genes or sintomas_esperados is empty — is this the right database?")

    coverage = gene_coverage(gene_diseases, disease_terms, parents)

    # Obsolete terms have no is_a and are nobody's child; the root has neither
    # and is kept. Everything reachable is a term with a label.
    terms = sorted(set(labels_en) & (set(parents) | {"HP:0000001"}))
    edges = sorted((parent, child) for child, ps in parents.items() for parent in ps)

    conn.execute("DELETE FROM hpo_terminos")
    conn.executemany(
        "INSERT INTO hpo_terminos (hpo_id, nombre, nombre_en) VALUES (?, ?, ?)",
        [(t, labels_es.get(t) or labels_en[t], labels_en[t]) for t in terms],
    )
    conn.execute("DELETE FROM hpo_hijos")
    conn.executemany("INSERT INTO hpo_hijos (padre, hijo) VALUES (?, ?)", edges)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hijos_padre ON hpo_hijos(padre)")
    conn.execute("DELETE FROM termino_cobertura")
    conn.executemany(
        "INSERT INTO termino_cobertura (hpo_id, genes) VALUES (?, ?)",
        sorted(coverage.items()),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO build_info (clave, valor) VALUES (?, ?)",
        [("term_tree_hpo_version", read_obo_version(obo_path)),
         ("term_tree_labels", labels_path.name)],
    )
    conn.commit()
    conn.close()
    return {"terms": len(terms), "edges": len(edges), "covered": len(coverage)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path)
    parser.add_argument("--obo", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True, help="hp-es.json: HPO id -> Spanish label")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    counts = run(args.db, args.obo, args.labels)
    log.info("%(terms)d terms, %(edges)d edges, %(covered)d terms reach at least one gene", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
