"""Build the gene-profile database from scratch.

    python -m genesdb.build --data-dir ../diagnosticar-question-agent/data --out genes.db

The database is never committed: it is built here and copied to the server with
scp. Everything needed to reproduce it therefore lives in this repository, and
`build_info` records which HPO release the result came from.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

from . import hpo, sources
from .schema import create_schema, write_build_info
from .translate import Translator, clean_source_text

log = logging.getLogger("genesdb.build")


def _existing_translations(path: Path) -> dict[str, str]:
    """Reuse translations from a previous database so a rebuild is cheap."""
    if not path.exists():
        return {}
    cache: dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "genes" not in tables:
            return {}
        for (raw,) in conn.execute("SELECT raw_json FROM genes"):
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            for en_key, es_key in (("resumen", "resumen_es"), ("funcion_proteina", "funcion_proteina_es")):
                en, es = data.get(en_key), data.get(es_key)
                if en and es and en != es:
                    cache[clean_source_text(en)] = es
        conn.close()
    except sqlite3.Error as exc:
        log.warning("could not read previous translations from %s: %s", path, exc)
        return {}
    log.info("reusing %d cached translations", len(cache))
    return cache


def build(data_dir: Path, out_path: Path, *, limit: int | None = None,
          only: list[str] | None = None, reuse: Path | None = None,
          ncbi_api_key: str | None = None, skip_translation: bool = False) -> None:
    phenotype_to_genes = data_dir / "phenotype_to_genes.txt"
    hpoa = data_dir / "phenotype.hpoa"
    obo = data_dir / "hp.obo"
    for required in (phenotype_to_genes, hpoa, obo):
        if not required.exists():
            raise SystemExit(f"missing required data file: {required}")

    hpo_version = hpo.read_hpo_version(hpoa)
    log.info("HPO release: %s", hpo_version)

    universe = hpo.read_gene_universe(phenotype_to_genes)
    labels = hpo.read_hpo_labels(obo)
    expected = hpo.read_expected_symptoms(hpoa, labels)

    if only:
        wanted = {s.strip().upper() for s in only}
        universe = {k: v for k, v in universe.items() if k in wanted}
    if limit:
        universe = dict(list(universe.items())[:limit])
    log.info("building %d genes", len(universe))

    session = sources._session(ncbi_api_key)
    by_ncbi = sources.fetch_ncbi(universe.values(), session)
    log.info("NCBI returned %d/%d genes", len(by_ncbi), len(universe))

    translator = Translator(cache=_existing_translations(reuse) if reuse else {})

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(out_path)
    create_schema(conn)

    symbol_to_ncbi = {sym: nid for sym, nid in universe.items()}
    rows, sintomas, done, failures = [], [], 0, 0

    for symbol, ncbi_id in symbol_to_ncbi.items():
        profile = by_ncbi.get(ncbi_id)
        if profile is None:
            profile = sources.GeneProfile(simbolo=symbol, ncbi_id=ncbi_id)
            failures += 1
        profile.simbolo = symbol

        try:
            uni = sources.fetch_uniprot(symbol, session)
        except sources.FetchError as exc:
            log.warning("UniProt failed for %s: %s", symbol, exc)
            uni = {}

        if uni:
            profile.uniprot_accession = uni.get("uniprot_accession", "")
            profile.funcion_proteina = uni.get("funcion_proteina", "")
            profile.enfermedades_asociadas = uni.get("enfermedades_asociadas", [])
            profile.codigo_omim = uni.get("codigo_omim", [])
            if uni.get("referencia"):
                profile.referencias["UniProt"] = uni["referencia"]

        resumen_en = clean_source_text(profile.resumen)
        funcion_en = clean_source_text(profile.funcion_proteina)
        if skip_translation:
            resumen_es, funcion_es = resumen_en, funcion_en
        else:
            resumen_es = translator.translate(resumen_en)
            funcion_es = translator.translate(funcion_en)

        enfermedades_es = []
        for disease in profile.enfermedades_asociadas:
            nombre = (disease.get("nombre") or "").strip()
            desc = clean_source_text(disease.get("descripcion") or "")
            if not nombre and not desc:
                continue
            enfermedades_es.append({
                "nombre": nombre if skip_translation else translator.translate(nombre),
                "descripcion_es": desc if skip_translation else translator.translate(desc),
                "omim": (disease.get("omim") or "").strip(),
            })

        payload = profile.to_json()
        payload["resumen_es"] = resumen_es
        payload["funcion_proteina_es"] = funcion_es
        payload["enfermedades_asociadas_es"] = enfermedades_es

        rows.append((
            symbol, profile.nombre_completo, profile.cromosoma, profile.ubicacion,
            profile.aliases,
            json.dumps(enfermedades_es, ensure_ascii=False),
            None,
            resumen_es, funcion_es, resumen_en, funcion_en,
            profile.ncbi_id, profile.uniprot_accession,
            json.dumps(profile.codigo_omim, ensure_ascii=False),
            json.dumps(profile.referencias, ensure_ascii=False),
            json.dumps(payload, ensure_ascii=False),
        ))

        for omim in profile.codigo_omim:
            for order, symptom in enumerate(expected.get(f"OMIM:{omim}", [])):
                label_es = "" if skip_translation else translator.translate(symptom.label_en)
                sintomas.append((
                    omim, symptom.hpo_id, symptom.label_en, label_es,
                    symptom.frequency_raw, symptom.frequency_pct,
                    symptom.frequency_label, order,
                ))

        done += 1
        if done % 100 == 0:
            _flush(conn, rows, sintomas)
            log.info("%d/%d genes", done, len(symbol_to_ncbi))

    _flush(conn, rows, sintomas)

    write_build_info(conn, {
        "hpo_version": hpo_version,
        "built_on": date.today().isoformat(),
        "gene_count": str(done),
        "ncbi_missing": str(failures),
        "source_phenotype_to_genes": str(phenotype_to_genes),
        "translated": "no" if skip_translation else "google-translate-web",
    })
    conn.execute("VACUUM")
    conn.close()
    log.info("wrote %s (%d genes, %d NCBI misses)", out_path, done, failures)


def _flush(conn: sqlite3.Connection, rows: list, sintomas: list) -> None:
    if rows:
        conn.executemany(
            """INSERT OR REPLACE INTO genes (
                simbolo, nombre_completo, cromosoma, ubicacion, aliases,
                enfermedades_es, resumen_llano_es,
                tecnico_resumen_es, tecnico_funcion_es,
                tecnico_resumen_en, tecnico_funcion_en,
                ncbi_id, uniprot_accession, codigo_omim, referencias, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        rows.clear()
    if sintomas:
        conn.executemany(
            """INSERT OR REPLACE INTO sintomas_esperados (
                omim, hpo_id, label_en, label_es,
                frecuencia_raw, frecuencia_pct, frecuencia_es, orden
            ) VALUES (?,?,?,?,?,?,?,?)""", sintomas)
        sintomas.clear()
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the gene-profile database.")
    parser.add_argument("--data-dir", type=Path, required=True,
                        help="directory holding phenotype_to_genes.txt, phenotype.hpoa and hp.obo")
    parser.add_argument("--out", type=Path, default=Path("genes.db"))
    parser.add_argument("--limit", type=int, help="build only the first N genes (smoke test)")
    parser.add_argument("--only", nargs="+", help="build only these gene symbols")
    parser.add_argument("--reuse", type=Path, help="previous .db to reuse translations from")
    parser.add_argument("--ncbi-api-key", help="raises the NCBI rate limit from 3/s to 10/s")
    parser.add_argument("--skip-translation", action="store_true",
                        help="keep English text; for fast structural testing")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    build(args.data_dir, args.out, limit=args.limit, only=args.only, reuse=args.reuse,
          ncbi_api_key=args.ncbi_api_key, skip_translation=args.skip_translation)
    return 0


if __name__ == "__main__":
    sys.exit(main())
