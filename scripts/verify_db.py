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
import json
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

# HPO names every OMIM code it links to a gene, so a good build has none left.
MAX_UNNAMED = 10

# mim2gene covers 5,225 of the 5,228; the other three have no OMIM gene entry.
MIN_GENE_MIM = 5200


def build_info_value(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT valor FROM build_info WHERE clave = ?", (key,)).fetchone()
    return row[0] if row else ""


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

    # The wizard's "too general" dialog browses this tree. Without it the
    # warning never fires and nobody notices, so a missing tree fails here.
    tree_tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name IN ('hpo_hijos', 'termino_cobertura', 'hpo_terminos')"
    )}
    if len(tree_tables) == 3:
        tree_edges = conn.execute("SELECT COUNT(*) FROM hpo_hijos").fetchone()[0]
        covered = conn.execute("SELECT COUNT(*) FROM termino_cobertura").fetchone()[0]
        results.append(check("HPO term tree", tree_edges > 20_000 and covered > 8_000,
                             f"{tree_edges} edges, {covered} terms with gene coverage"))
    else:
        results.append(check("HPO term tree", False, "tables missing — run scripts/add_term_tree.py"))

    # Gene→disease links come from UniProt, which curates a subset: it knows
    # TCF4 as a diabetes susceptibility locus and not as the cause of
    # Pitt-Hopkins, so that gene shipped with 3 expected symptoms instead of
    # 66. Symptoms join by OMIM code, so a missing code drops every symptom
    # beneath it silently.
    #
    # A short symptom list is not the signal — 630 genes have four or fewer
    # because their diseases are genuinely thinly annotated, and none of those
    # have a code left to recover. The signal is a code HPO links to the gene
    # that we do not hold, which is what scripts/backfill_hpo_diseases.py adds.
    backfilled = build_info_value(conn, "hpo_disease_backfill")
    results.append(check("HPO gene-disease backfill", bool(backfilled),
                         backfilled or "never run — see scripts/backfill_hpo_diseases.py"))

    # The symptoms of a recovered code arrived without its name, so TCF4's
    # conditions read "Diabetes mellitus tipo 2" over a screen of Pitt-Hopkins
    # symptoms. A code with symptoms and no named condition is that again.
    unnamed = 0
    for raw_codes, raw_named in conn.execute("SELECT codigo_omim, enfermedades_es FROM genes"):
        named = {disease.get("omim") for disease in json.loads(raw_named or "[]")}
        unnamed += sum(1 for code in json.loads(raw_codes or "[]") if code not in named)
    results.append(check("named conditions", unnamed <= MAX_UNNAMED,
                         f"{unnamed} OMIM codes with no named condition (limit {MAX_UNNAMED})"
                         + ("" if unnamed <= MAX_UNNAMED else
                            " — run scripts/backfill_hpo_diseases.py")))

    columns = {row[1] for row in conn.execute("PRAGMA table_info(genes)")}
    gene_mim = (conn.execute("SELECT COUNT(*) FROM genes WHERE omim_gen IS NOT NULL").fetchone()[0]
                if "omim_gen" in columns else 0)
    results.append(check("gene OMIM entries", gene_mim >= MIN_GENE_MIM,
                         f"{gene_mim} genes" if "omim_gen" in columns
                         else "column missing — run scripts/add_gene_mim.py"))

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
