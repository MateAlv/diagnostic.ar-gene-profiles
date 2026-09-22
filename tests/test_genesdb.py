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
from genesdb import translate_summaries  # noqa: E402


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


def test_rewrite_only_touches_the_plain_language_column(tmp_path: Path) -> None:
    """The pass is additive: nothing the database already holds may be lost."""
    from genesdb import plain_language

    path = tmp_path / "g.db"
    conn = sqlite3.connect(path)
    create_schema(conn)
    original = ("CFTR", "CF regulator", "7", "7q31.2", "ABCC7",
                json.dumps([{"nombre": "Fibrosis quística", "descripcion_es": "d", "omim": "219700"}]),
                None, "Texto técnico.", "Función.", "Technical.", "Function.",
                "1080", "P13569", json.dumps(["219700"]), json.dumps({}), json.dumps({"x": 1}))
    conn.execute("""INSERT INTO genes (simbolo, nombre_completo, cromosoma, ubicacion, aliases,
        enfermedades_es, resumen_llano_es, tecnico_resumen_es, tecnico_funcion_es,
        tecnico_resumen_en, tecnico_funcion_en, ncbi_id, uniprot_accession,
        codigo_omim, referencias, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", original)
    conn.commit()
    before = conn.execute("SELECT * FROM genes").fetchone()
    conn.close()

    plain_language.rewrite_batch = lambda rows, **kw: {"CFTR": "Texto llano."}
    plain_language.run(path, workers=1)

    conn = sqlite3.connect(path)
    after = conn.execute("SELECT * FROM genes").fetchone()
    columns = [d[0] for d in conn.execute("SELECT * FROM genes LIMIT 1").description]
    conn.close()

    changed = {c for c, b, a in zip(columns, before, after) if b != a}
    assert changed == {"resumen_llano_es"}
    assert after[columns.index("resumen_llano_es")] == "Texto llano."


def test_rejected_rewrite_leaves_existing_value_untouched(tmp_path: Path) -> None:
    """A rejection must not blank a column that already had a good value."""
    from genesdb import plain_language

    path = tmp_path / "g2.db"
    conn = sqlite3.connect(path)
    create_schema(conn)
    conn.execute("""INSERT INTO genes (simbolo, enfermedades_es, resumen_llano_es,
        tecnico_resumen_es, raw_json) VALUES (?,?,?,?,?)""",
        ("CFTR", "[]", "Texto previo bueno.", "Técnico.", "{}"))
    conn.commit()
    conn.close()

    plain_language.rewrite_batch = lambda rows, **kw: {}
    plain_language.run(path, workers=1, overwrite=True)

    conn = sqlite3.connect(path)
    value = conn.execute("SELECT resumen_llano_es FROM genes WHERE simbolo='CFTR'").fetchone()[0]
    conn.close()
    assert value == "Texto previo bueno."


# --- phenotype matching -----------------------------------------------------

def test_match_symptoms_splits_reported_from_unreported(db: GeneProfileDB) -> None:
    result = db.match_symptoms("CFTR", ["HP:0012236"])
    assert [m["nombre"] for m in result["matched"]] == ["Cloruro elevado en sudor"]
    assert [u["nombre"] for u in result["unreported"]] == ["Bronquiectasia"]
    assert result["expected_count"] == 2


def test_match_symptoms_orders_unreported_by_frequency(db: GeneProfileDB) -> None:
    """The most frequent unreported symptom is the most useful question."""
    result = db.match_symptoms("CFTR", [])
    frequencies = [u["frecuencia_pct"] for u in result["unreported"]]
    assert frequencies == sorted(frequencies, reverse=True)


def test_match_symptoms_is_case_insensitive_on_reported_ids(db: GeneProfileDB) -> None:
    assert len(db.match_symptoms("CFTR", ["hp:0012236"])["matched"]) == 1


def test_match_symptoms_unknown_gene_returns_none(db: GeneProfileDB) -> None:
    assert db.match_symptoms("NOTAGENE", ["HP:0012236"]) is None


def test_match_symptoms_gene_without_diseases_is_not_an_error(tmp_path: Path) -> None:
    """10% of genes have no associated disease; that is a valid, empty result."""
    path = tmp_path / "n.db"
    conn = sqlite3.connect(path)
    create_schema(conn)
    conn.execute("INSERT INTO genes (simbolo, codigo_omim, raw_json) VALUES ('XYZ', '[]', '{}')")
    conn.commit()
    conn.close()

    result = GeneProfileDB(path).match_symptoms("XYZ", ["HP:0012236"])
    assert result == {"matched": [], "unreported": [], "reported_count": 1, "expected_count": 0}


def test_read_spanish_labels_keeps_only_term_names(tmp_path: Path) -> None:
    """The babelon file carries definitions and synonyms too; we want labels."""
    from genesdb.hpo import read_spanish_labels

    path = tmp_path / "hp-es.babelon.tsv"
    path.write_text(
        "subject_id\tpredicate_id\ttranslation_value\n"
        "HP:0002110\trdfs:label\tBronquiectasias\n"
        "HP:0002110\tIAO:0000115\tUna descripción larga\n"
        "HP:0001250\trdfs:label\tConvulsiones\n",
        encoding="utf-8")
    assert read_spanish_labels(path) == {
        "HP:0002110": "Bronquiectasias", "HP:0001250": "Convulsiones"}


def test_read_spanish_labels_missing_file_is_not_fatal(tmp_path: Path) -> None:
    from genesdb.hpo import read_spanish_labels
    assert read_spanish_labels(tmp_path / "absent.tsv") == {}


# --- codex translation of disease names -------------------------------------

def _seed_diseases(path: Path, diseases: list[dict]) -> None:
    conn = sqlite3.connect(path)
    create_schema(conn)
    conn.execute(
        "INSERT INTO genes (simbolo, enfermedades_es, raw_json) VALUES (?,?,?)",
        ("CFTR", json.dumps(diseases, ensure_ascii=False), "{}"),
    )
    conn.commit()
    conn.close()


def test_collect_untranslated_skips_what_the_cache_already_has(tmp_path: Path) -> None:
    from genesdb.translate_codex import collect_untranslated

    path = tmp_path / "g.db"
    _seed_diseases(path, [
        {"nombre": "Cystic fibrosis", "descripcion_es": "", "omim": "219700"},
        {"nombre": "Alexander disease", "descripcion_es": "", "omim": "203450"},
    ])
    conn = sqlite3.connect(path)
    pending = collect_untranslated(conn, {"Alexander disease": "Enfermedad de Alexander"})
    conn.close()

    assert pending == ["Cystic fibrosis"]


def test_apply_cache_translates_names_and_leaves_the_rest(tmp_path: Path) -> None:
    from genesdb.translate_codex import apply_cache

    path = tmp_path / "g2.db"
    _seed_diseases(path, [
        {"nombre": "Cystic fibrosis", "descripcion_es": "Un trastorno…", "omim": "219700"},
        {"nombre": "Sin traducción", "descripcion_es": "Otro", "omim": "000001"},
    ])
    conn = sqlite3.connect(path)
    updated = apply_cache(conn, {"Cystic fibrosis": "Fibrosis quística"})
    diseases = json.loads(
        conn.execute("SELECT enfermedades_es FROM genes WHERE simbolo='CFTR'").fetchone()[0]
    )
    conn.close()

    assert updated == 1
    assert diseases[0]["nombre"] == "Fibrosis quística"
    # Untranslated names keep what they had, and descriptions are never touched.
    assert diseases[1]["nombre"] == "Sin traducción"
    assert diseases[0]["descripcion_es"] == "Un trastorno…"


def test_a_batch_ignores_keys_it_did_not_ask_for() -> None:
    """A hallucinated key would poison the cache for every later build."""
    from genesdb import translate_codex

    def fake_complete(*_args, **_kwargs):
        return {"translations": [
            {"en": "Cystic fibrosis", "es": "Fibrosis quística"},
            {"en": "Never sent", "es": "Inventado"},
        ]}

    result = translate_codex._translate_batch(
        ["Cystic fibrosis"], model=None, timeout=60, complete=fake_complete)

    assert result == {"Cystic fibrosis": "Fibrosis quística"}


# --- LLM backend selection --------------------------------------------------

def test_resolve_defaults_to_claude() -> None:
    from genesdb import claude_cli, llm
    assert llm.resolve() is claude_cli.complete


def test_resolve_picks_the_named_backend() -> None:
    from genesdb import codex, llm
    assert llm.resolve("codex") is codex.complete


def test_resolve_rejects_an_unknown_backend() -> None:
    from genesdb import llm
    with pytest.raises(SystemExit, match="unknown backend"):
        llm.resolve("gpt2")


def test_quota_errors_are_distinguishable_from_transient_ones() -> None:
    """Retrying a spent quota only burns minutes rediscovering the same wall."""
    from genesdb.codex import CodexError, CodexQuotaError
    assert issubclass(CodexQuotaError, CodexError)


# --- Finding the texts an interrupted build left in English -----------------

def test_looks_english_separates_the_two_languages() -> None:
    english = ("The protein encoded by this gene is a member of the superfamily "
               "of ATP-binding cassette transporters.")
    spanish = ("La proteína codificada por este gen es miembro de la superfamilia "
               "de transportadores con cassette de unión a ATP.")
    assert translate_summaries.looks_english(english)
    assert not translate_summaries.looks_english(spanish)


@pytest.mark.parametrize("text", [None, "", "   "])
def test_looks_english_ignores_empty_text(text) -> None:
    """An empty column is missing data, not an untranslated one."""
    assert not translate_summaries.looks_english(text)


def test_looks_english_tolerates_english_technical_terms() -> None:
    """Spanish molecular biology keeps terms like 'helix-loop-helix' untranslated;
    a row is not English because it contains them."""
    text = ("Este gen codifica el factor de transcripción 4, un factor de "
            "transcripción basic helix-loop-helix que reconoce un sitio E-box.")
    assert not translate_summaries.looks_english(text)


def test_collect_pending_finds_each_column_independently(tmp_path: Path) -> None:
    """The two columns drift apart: a gene can have a Spanish summary and an
    English function, or the reverse."""
    path = tmp_path / "genes.db"
    conn = sqlite3.connect(path)
    create_schema(conn)
    conn.execute(
        "INSERT INTO genes (simbolo, tecnico_resumen_es, tecnico_funcion_es, raw_json)"
        " VALUES (?, ?, ?, '{}')",
        ("ACE", "Este gen codifica una enzima que regula la presión arterial.",
         "Dipeptidyl carboxypeptidase that removes dipeptides from the C-terminus."),
    )
    conn.execute(
        "INSERT INTO genes (simbolo, tecnico_resumen_es, tecnico_funcion_es, raw_json)"
        " VALUES (?, ?, ?, '{}')",
        ("TCF4", "This gene encodes transcription factor 4, which binds an E-box.",
         "Participa en la vía de señalización Wnt y modula la expresión de MYC."),
    )
    conn.commit()

    pending = translate_summaries.collect_pending(conn)
    conn.close()

    assert {(symbol, field) for symbol, field, _ in pending} == {
        ("ACE", "tecnico_funcion_es"),
        ("TCF4", "tecnico_resumen_es"),
    }


def test_apply_cache_leaves_untranslated_rows_alone(tmp_path: Path) -> None:
    """Additive: a row with no translation keeps whatever it had."""
    path = tmp_path / "genes.db"
    conn = sqlite3.connect(path)
    create_schema(conn)
    conn.execute(
        "INSERT INTO genes (simbolo, tecnico_resumen_es, tecnico_funcion_es, raw_json)"
        " VALUES ('TCF4', 'This gene encodes transcription factor 4.', 'Vía Wnt.', '{}')"
    )
    conn.commit()

    translate_summaries.apply_cache(conn, {"TCF4:tecnico_resumen_es": "Este gen codifica el factor 4."})

    row = conn.execute(
        "SELECT tecnico_resumen_es, tecnico_funcion_es FROM genes WHERE simbolo = 'TCF4'"
    ).fetchone()
    conn.close()
    assert row == ("Este gen codifica el factor 4.", "Vía Wnt.")


def test_a_reply_still_in_english_is_rejected() -> None:
    """Echoing the source back would hide the row from the next run's detector."""
    items = [("TCF4", "tecnico_resumen_es", "This gene encodes transcription factor 4.")]

    def echo(prompt, schema, *, model=None, timeout=180):
        return {"translations": [{"id": "TCF4:tecnico_resumen_es",
                                  "text": "", "es": "This gene encodes transcription factor 4."}]}

    assert translate_summaries._translate_batch(items, model=None, timeout=1, complete=echo) == {}


def test_a_hallucinated_id_is_never_written() -> None:
    """A key we did not send would otherwise be written to whichever gene it names."""
    items = [("TCF4", "tecnico_resumen_es", "This gene encodes transcription factor 4.")]

    def invents(prompt, schema, *, model=None, timeout=180):
        return {"translations": [{"id": "CFTR:tecnico_resumen_es",
                                  "es": "Este gen codifica un canal de cloruro."}]}

    assert translate_summaries._translate_batch(items, model=None, timeout=1, complete=invents) == {}
