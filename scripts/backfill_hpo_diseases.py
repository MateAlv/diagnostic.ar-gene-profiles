#!/usr/bin/env python3
"""Add the OMIM links HPO knows and UniProt does not.

    python scripts/backfill_hpo_diseases.py genes.db \\
        --data-dir ../diagnosticar-question-agent/data

A gene's disease list comes from UniProt, which curates a *subset*. It knows
TCF4 as a type 2 diabetes susceptibility locus and not as the cause of
Pitt-Hopkins syndrome — even though HPO annotates Pitt-Hopkins with 63
phenotypes and links it to TCF4.

Expected symptoms are joined by OMIM code, so a missing code silently drops
every symptom under it. Measured on the shipped database: **504 genes (10%)
were missing at least one code**, 638 codes in total, and TCF4 was left with
three symptoms for a syndrome that has 63.

Additive and idempotent: it only adds OMIM codes and the symptom rows they
unlock, never removes a disease UniProt supplied, and a rerun changes nothing.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genesdb import hpo  # noqa: E402

log = logging.getLogger("backfill_hpo_diseases")


def run(db_path: Path, data_dir: Path) -> tuple[int, int]:
    conn = sqlite3.connect(db_path)

    labels = hpo.read_hpo_labels(data_dir / "hp.obo")
    spanish = hpo.read_spanish_labels(data_dir / "hp-es.babelon.tsv")
    expected = hpo.read_expected_symptoms(data_dir / "phenotype.hpoa", labels)
    links = hpo.read_gene_diseases(data_dir / "phenotype_to_genes.txt")

    genes_touched = 0
    symptom_rows = 0

    for simbolo, raw in conn.execute("SELECT simbolo, codigo_omim FROM genes").fetchall():
        ours = json.loads(raw or "[]")
        missing = sorted(links.get(simbolo, set()) - set(ours))
        if not missing:
            continue

        # UniProt's codes stay first: they carry the curated descriptions the
        # card shows, and their order is what a reader has already seen.
        conn.execute(
            "UPDATE genes SET codigo_omim = ? WHERE simbolo = ?",
            (json.dumps(ours + missing, ensure_ascii=False), simbolo),
        )
        genes_touched += 1

        for omim in missing:
            for order, symptom in enumerate(expected.get(f"OMIM:{omim}", [])):
                conn.execute(
                    """INSERT OR IGNORE INTO sintomas_esperados
                       (omim, hpo_id, label_en, label_es, frecuencia_raw,
                        frecuencia_pct, frecuencia_es, orden)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        omim, symptom.hpo_id, symptom.label_en,
                        spanish.get(symptom.hpo_id) or symptom.label_en,
                        symptom.frequency_raw, symptom.frequency_pct,
                        symptom.frequency_label, order,
                    ),
                )
                symptom_rows += 1

    conn.execute(
        "INSERT OR REPLACE INTO build_info (clave, valor) VALUES ('hpo_disease_backfill', ?)",
        (f"{genes_touched} genes, {symptom_rows} symptom rows",),
    )
    conn.commit()
    conn.close()
    return genes_touched, symptom_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    genes, rows = run(args.db, args.data_dir)
    print(f"{genes} genes gained an OMIM code; {rows} symptom rows added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
