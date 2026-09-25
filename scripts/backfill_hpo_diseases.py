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

Each recovered code is also added to the gene's named condition list, under
HPO's disease name translated to Spanish. Without that the symptoms arrived
but the condition did not: TCF4 listed only "Diabetes mellitus tipo 2" while
every symptom on screen came from Pitt-Hopkins.

Additive and idempotent: it only adds OMIM codes, the symptom rows they unlock
and the names of their conditions, never removes a disease UniProt supplied,
and a rerun changes nothing.
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
from genesdb.translate import BulkTranslator  # noqa: E402

log = logging.getLogger("backfill_hpo_diseases")


def load_translator(cache_path: Path) -> BulkTranslator:
    cache: dict[str, str] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    return BulkTranslator(cache=cache, cache_path=cache_path)


def name_conditions(conn: sqlite3.Connection, names: dict[str, str],
                    translator: BulkTranslator) -> int:
    """Give every OMIM code a gene carries an entry in its condition list.

    A code whose name HPO does not know is left out rather than shown as a bare
    number. Returns how many entries were added.
    """
    pending: list[tuple[str, list[dict], list[str]]] = []
    for simbolo, raw_codes, raw_named in conn.execute(
            "SELECT simbolo, codigo_omim, enfermedades_es FROM genes").fetchall():
        named = json.loads(raw_named or "[]")
        have = {disease.get("omim") for disease in named}
        missing = [code for code in json.loads(raw_codes or "[]")
                   if code not in have and code in names]
        if missing:
            pending.append((simbolo, named, missing))

    translator.translate_many([names[code] for _, _, missing in pending for code in missing])

    added = 0
    for simbolo, named, missing in pending:
        named += [{"nombre": translator.get(names[code]), "descripcion_es": "", "omim": code}
                  for code in missing]
        conn.execute("UPDATE genes SET enfermedades_es = ? WHERE simbolo = ?",
                     (json.dumps(named, ensure_ascii=False), simbolo))
        added += len(missing)
    return added


def run(db_path: Path, data_dir: Path, translator: BulkTranslator) -> tuple[int, int, int]:
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

    # A rerun finds nothing left to add; recording that would erase what the
    # first run did.
    if genes_touched:
        conn.execute(
            "INSERT OR REPLACE INTO build_info (clave, valor) VALUES ('hpo_disease_backfill', ?)",
            (f"{genes_touched} genes, {symptom_rows} symptom rows",),
        )
    named = name_conditions(conn, hpo.read_disease_names(data_dir / "phenotype.hpoa"), translator)
    if named:
        conn.execute(
            "INSERT OR REPLACE INTO build_info (clave, valor) VALUES ('hpo_disease_names', ?)",
            (f"{named} conditions named from phenotype.hpoa",),
        )
    conn.commit()
    conn.close()
    return genes_touched, symptom_rows, named


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--translation-cache", type=Path,
                        help="defaults to the one the build keeps next to the database")
    args = parser.parse_args(argv)
    cache = args.translation_cache or args.db.with_suffix(".translations.json")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    genes, rows, named = run(args.db, args.data_dir, load_translator(cache))
    print(f"{genes} genes gained an OMIM code; {rows} symptom rows added; "
          f"{named} conditions named")
    return 0


if __name__ == "__main__":
    sys.exit(main())
