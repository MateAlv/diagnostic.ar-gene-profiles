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
from .translate import BulkTranslator, clean_source_text

log = logging.getLogger("genesdb.build")


def read_model_genes(path: Path) -> list[str]:
    """Read the model's gene vocabulary (``genes.json`` from the checkpoint).

    This is the authoritative list: the classifier has one output per entry, so
    the model cannot return a symbol that is not here. ``-`` is the placeholder
    class, not a gene.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    genes = data if isinstance(data, list) else (data.get("genes") or list(data))
    return [str(g).strip().upper() for g in genes if str(g).strip() and str(g).strip() != "-"]


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
          ncbi_api_key: str | None = None, skip_translation: bool = False,
          model_genes: Path | None = None, workers: int = 2,
          translate_rate: float = 2.0) -> None:
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

    session = sources._session(ncbi_api_key)
    vocabulary: list[str] | None = None

    if model_genes:
        vocabulary = read_model_genes(model_genes)
        log.info("model vocabulary: %d genes", len(vocabulary))

        # Genes the model ranks but HPO does not annotate have no id in the
        # annotation files; resolve those by symbol so none is left unexplained.
        unknown = [g for g in vocabulary if g not in universe]
        if unknown:
            log.info("resolving %d symbols absent from the HPO files", len(unknown))
            universe = {**universe, **sources.resolve_symbols(unknown, session)}

        universe = {g: universe[g] for g in vocabulary if g in universe}
        still_missing = [g for g in vocabulary if g not in universe]
        if still_missing:
            log.warning("%d model genes could not be resolved: %s",
                        len(still_missing), ", ".join(sorted(still_missing)[:20]))

    if only:
        wanted = {s.strip().upper() for s in only}
        universe = {k: v for k, v in universe.items() if k in wanted}
    if limit:
        universe = dict(list(universe.items())[:limit])
    log.info("building %d genes", len(universe))
    by_ncbi = sources.fetch_ncbi(universe.values(), session)
    log.info("NCBI returned %d/%d genes", len(by_ncbi), len(universe))

    translation_cache = _existing_translations(reuse) if reuse else {}
    translation_cache_path = out_path.with_suffix(".translations.json")
    if translation_cache_path.exists():
        try:
            saved = json.loads(translation_cache_path.read_text(encoding="utf-8"))
            translation_cache.update(saved)
            log.info("resuming with %d cached translations", len(saved))
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable translation cache: %s", exc)

    translator = BulkTranslator(
        cache=translation_cache, workers=workers, rate_per_second=translate_rate,
        cache_path=translation_cache_path,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(out_path)
    create_schema(conn)

    # Pass 1 — gather every gene's source data and every string needing translation.
    # Cached to disk: the fetch takes ~40 minutes, and a later failure must not
    # throw it away.
    cache_path = out_path.with_suffix(".fetch.json")
    cached: dict[str, dict] = {}
    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            log.info("reusing %d fetched genes from %s", len(cached), cache_path.name)
        except (OSError, ValueError) as exc:
            log.warning("ignoring unreadable fetch cache: %s", exc)

    log.info("fetching UniProt for %d genes", len(universe))
    gathered: list[tuple[str, sources.GeneProfile]] = []
    failures = 0

    for index, (symbol, ncbi_id) in enumerate(universe.items(), start=1):
        if symbol in cached:
            restored = sources.GeneProfile(**cached[symbol])
            gathered.append((symbol, restored))
            continue
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

        gathered.append((symbol, profile))
        cached[symbol] = {
            "simbolo": profile.simbolo, "ncbi_id": profile.ncbi_id,
            "nombre_completo": profile.nombre_completo, "cromosoma": profile.cromosoma,
            "ubicacion": profile.ubicacion, "aliases": profile.aliases,
            "resumen": profile.resumen, "funcion_proteina": profile.funcion_proteina,
            "uniprot_accession": profile.uniprot_accession,
            "enfermedades_asociadas": profile.enfermedades_asociadas,
            "codigo_omim": profile.codigo_omim, "referencias": profile.referencias,
        }
        if index % 250 == 0:
            cache_path.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
            log.info("UniProt %d/%d", index, len(universe))

    cache_path.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")

    # Pass 2 — one deduplicated translation pass over everything at once.
    if not skip_translation:
        needed: list[str] = []
        for _, profile in gathered:
            needed.append(clean_source_text(profile.resumen))
            needed.append(clean_source_text(profile.funcion_proteina))
            for disease in profile.enfermedades_asociadas:
                needed.append((disease.get("nombre") or "").strip())
                needed.append(clean_source_text(disease.get("descripcion") or ""))
            for omim in profile.codigo_omim:
                for symptom in expected.get(f"OMIM:{omim}", []):
                    needed.append(symptom.label_en)
        translator.translate_many(needed)

    # Pass 3 — assemble rows from the translated cache.
    def es(text: str) -> str:
        text = (text or "").strip()
        return text if skip_translation else translator.get(text)

    rows, sintomas, done = [], [], 0

    for symbol, profile in gathered:
        resumen_en = clean_source_text(profile.resumen)
        funcion_en = clean_source_text(profile.funcion_proteina)
        resumen_es, funcion_es = es(resumen_en), es(funcion_en)

        enfermedades_es = []
        for disease in profile.enfermedades_asociadas:
            nombre = (disease.get("nombre") or "").strip()
            desc = clean_source_text(disease.get("descripcion") or "")
            if not nombre and not desc:
                continue
            enfermedades_es.append({
                "nombre": es(nombre),
                "descripcion_es": es(desc),
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
                sintomas.append((
                    omim, symptom.hpo_id, symptom.label_en, es(symptom.label_en),
                    symptom.frequency_raw, symptom.frequency_pct,
                    symptom.frequency_label, order,
                ))

        done += 1
        if done % 250 == 0:
            _flush(conn, rows, sintomas)
            log.info("wrote %d/%d genes", done, len(gathered))

    _flush(conn, rows, sintomas)

    write_build_info(conn, {
        "hpo_version": hpo_version,
        "built_on": date.today().isoformat(),
        "gene_count": str(done),
        "ncbi_missing": str(failures),
        "source_phenotype_to_genes": str(phenotype_to_genes),
        "translated": "no" if skip_translation else "google-translate-web",
        "gene_universe": "model-vocabulary" if model_genes else "hpo-annotations",
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
    parser.add_argument("--model-genes", type=Path,
                        help="genes.json from the GenPhenAI checkpoint — the authoritative "
                             "gene vocabulary; without it the HPO annotation files are used")
    parser.add_argument("--workers", type=int, default=2,
                        help="concurrent translation requests (default 2)")
    parser.add_argument("--translate-rate", type=float, default=2.0,
                        help="max translation requests per second across all workers")
    parser.add_argument("--skip-translation", action="store_true",
                        help="keep English text; for fast structural testing")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    build(args.data_dir, args.out, limit=args.limit, only=args.only, reuse=args.reuse,
          ncbi_api_key=args.ncbi_api_key, skip_translation=args.skip_translation,
          model_genes=args.model_genes, workers=args.workers,
          translate_rate=args.translate_rate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
