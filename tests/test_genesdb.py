"""Tests for the gene-profile database.

These run against a small fixture database built in-process — no network, so
they are safe for CI.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from genesdb.build import read_model_genes  # noqa: E402
from genesdb.hpo import _parse_frequency, read_hpo_version  # noqa: E402
from genesdb.query import GeneProfileDB  # noqa: E402
from genesdb.schema import create_schema, write_build_info  # noqa: E402
from genesdb.translate import clean_source_text  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> GeneProfileDB:
    path = tmp_path / "genes.db"
    conn = sqlite3.connect(path)
    create_schema(conn)
    diseases = [{"nombre": "Fibrosis quística", "descripcion_es": "Un trastorno…", "omim": "219700"}]
    conn.execute(
        """INSERT INTO genes (simbolo, nombre_completo, cromosoma, ubicacion, aliases,
               enfermedades_es, resumen_llano_es, tecnico_resumen_es, tecnico_funcion_es,
               tecnico_resumen_en, tecnico_funcion_en, ncbi_id, uniprot_accession,
               codigo_omim, referencias, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("CFTR", "CF transmembrane conductance regulator", "7", "7q31.2", "ABCC7",
         json.dumps(diseases, ensure_ascii=False), None,
         "Este gen codifica un transportador ABC.", "Canal de cloruro.",
         "This gene encodes an ABC transporter.", "Chloride channel.",
         "1080", "P13569", json.dumps(["219700"]), json.dumps({}), json.dumps({})),
    )
    conn.executemany(
        """INSERT INTO sintomas_esperados (omim, hpo_id, label_en, label_es,
               frecuencia_raw, frecuencia_pct, frecuencia_es, orden)
           VALUES (?,?,?,?,?,?,?,?)""",
        [("219700", "HP:0012236", "Elevated sweat chloride", "Cloruro elevado en sudor",
          "77/77", 100.0, "", 0),
         ("219700", "HP:0002110", "Bronchiectasis", "Bronquiectasia", "106/111", 95.5, "", 1)],
    )
    write_build_info(conn, {"hpo_version": "2026-06-23", "built_on": "2026-09-21"})
    conn.commit()
    conn.close()
    return GeneProfileDB(path)


def test_family_view_excludes_biochemistry(db: GeneProfileDB) -> None:
    """The default view must never leak the technical text to a family."""
    profile = db.get("CFTR")
    assert profile is not None
    assert "tecnico" not in profile
    body = json.dumps(profile, ensure_ascii=False)
    assert "Canal de cloruro" not in body
    assert "ABC transporter" not in body


def test_professional_view_includes_biochemistry(db: GeneProfileDB) -> None:
    profile = db.get("CFTR", professional=True)
    assert profile is not None
    assert profile["tecnico"]["funcion_proteina_es"] == "Canal de cloruro."
    assert profile["tecnico"]["resumen_en"].startswith("This gene encodes")


def test_disease_is_present_for_both_audiences(db: GeneProfileDB) -> None:
    """The disease is the headline — it must never be gated behind the toggle."""
    for professional in (False, True):
        profile = db.get("CFTR", professional=professional)
        assert profile["enfermedades"][0]["nombre"] == "Fibrosis quística"


def test_expected_symptoms_ordered_by_frequency(db: GeneProfileDB) -> None:
    symptoms = db.get("CFTR")["sintomas_esperados"]
    assert [s["nombre"] for s in symptoms] == ["Cloruro elevado en sudor", "Bronquiectasia"]
    assert symptoms[0]["frecuencia_pct"] == 100.0


def test_symbol_lookup_is_case_insensitive(db: GeneProfileDB) -> None:
    assert db.get("cftr") is not None


def test_unknown_gene_returns_none_rather_than_raising(db: GeneProfileDB) -> None:
    """A gene with no profile is expected, not exceptional: HPO releases drift."""
    assert db.get("NOTAGENE") is None


def test_missing_reports_only_absent_symbols(db: GeneProfileDB) -> None:
    assert db.missing(["CFTR", "MED16", "cftr"]) == ["MED16"]


def test_build_info_records_provenance(db: GeneProfileDB) -> None:
    assert db.build_info()["hpo_version"] == "2026-06-23"


@pytest.mark.parametrize("raw,expected", [
    ("77/77", 100.0), ("106/111", pytest.approx(95.495, abs=0.01)),
    ("50%", 50.0), ("HP:0040281", 90.0), ("", None), ("garbage", None), ("1/0", None),
])
def test_parse_frequency(raw, expected) -> None:
    assert _parse_frequency(raw)[0] == expected


def test_clean_source_text_strips_citation_noise() -> None:
    dirty = 'Plays a role (PubMed:12345678) in transport (By similarity) {ECO:0000269}.'
    assert clean_source_text(dirty) == "Plays a role in transport."


def test_read_hpo_version(tmp_path: Path) -> None:
    hpoa = tmp_path / "phenotype.hpoa"
    hpoa.write_text("#description: x\n#version: 2026-06-23\ndatabase_id\tdisease_name\n")
    assert read_hpo_version(hpoa) == "2026-06-23"


def test_read_model_genes_drops_the_placeholder_class(tmp_path: Path) -> None:
    """genes.json's leading "-" is the placeholder class, not a gene."""
    path = tmp_path / "genes.json"
    path.write_text(json.dumps(["-", "CFTR", "med16", " ", "ABCC4"]))
    assert read_model_genes(path) == ["CFTR", "MED16", "ABCC4"]


# --- plain-language rewrite -------------------------------------------------

from genesdb.plain_language import MAX_CHARS, validate  # noqa: E402


def test_validate_accepts_a_plain_description() -> None:
    ok, _ = validate("Este gen contiene las instrucciones para fabricar una "
                     "proteína que funciona como un canal para el paso de sales.")
    assert ok


@pytest.mark.parametrize("text", [
    "Este gen confirma el diagnóstico de fibrosis quística.",
    "El pronóstico depende de la mutación.",
    "Existe tratamiento disponible para esta condición.",
    "Su hijo tiene esta enfermedad.",
])
def test_validate_rejects_medical_advice(text: str) -> None:
    """A gene description must never read as a diagnosis or a prognosis."""
    ok, reason = validate(text)
    assert not ok and reason


def test_validate_rejects_overlong_text() -> None:
    ok, reason = validate("a" * (MAX_CHARS + 1))
    assert not ok and "too long" in reason


def test_validate_allows_empty_when_source_is_insufficient() -> None:
    assert validate("")[0]
