"""The HPO tree and gene coverage behind the wizard's "too general" dialog."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from genesdb.schema import create_schema  # noqa: E402

_spec = importlib.util.spec_from_file_location("add_term_tree", ROOT / "scripts" / "add_term_tree.py")
add_term_tree = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(add_term_tree)

# root ─ seizure ─┬─ focal ── focal-aware
#                 └─ generalized
#      └ obsolete (no is_a: not in the tree)
OBO = """format-version: 1.2
data-version: hp/releases/2099-01-01

[Term]
id: HP:0000001
name: All

[Term]
id: HP:0001250
name: Seizure
is_a: HP:0000001 ! All

[Term]
id: HP:0007359
name: Focal-onset seizure
is_a: HP:0001250 ! Seizure

[Term]
id: HP:0002197
name: Generalized-onset seizure
is_a: HP:0001250 ! Seizure

[Term]
id: HP:0032677
name: Focal aware seizure
is_a: HP:0007359 ! Focal-onset seizure

[Term]
id: HP:9999999
name: obsolete thing
is_obsolete: true
"""


def build(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "genes.db"
    conn = sqlite3.connect(db)
    create_schema(conn)
    conn.executemany(
        "INSERT INTO genes (simbolo, codigo_omim, raw_json) VALUES (?, ?, '{}')",
        [("A", json.dumps(["100"])), ("B", json.dumps(["200", "300"])), ("C", json.dumps([]))],
    )
    conn.executemany(
        "INSERT INTO sintomas_esperados (omim, hpo_id, label_en, orden) VALUES (?, ?, ?, 0)",
        [("100", "HP:0032677", "Focal aware seizure"),
         ("200", "HP:0002197", "Generalized-onset seizure"),
         ("300", "HP:0032677", "Focal aware seizure")],
    )
    conn.commit()
    conn.close()
    (tmp_path / "hp.obo").write_text(OBO, encoding="utf-8")
    (tmp_path / "hp-es.json").write_text(
        json.dumps({"HP:0001250": "Convulsiones", "HP:0007359": "Convulsiones focales"}),
        encoding="utf-8",
    )
    add_term_tree.run(db, tmp_path / "hp.obo", tmp_path / "hp-es.json")
    return sqlite3.connect(db)


def test_a_term_counts_the_genes_reaching_it_through_any_descendant(tmp_path):
    conn = build(tmp_path)
    coverage = dict(conn.execute("SELECT hpo_id, genes FROM termino_cobertura"))
    # A reaches seizure through focal-aware; B through generalized and, via
    # a second disease, focal-aware too -- counted once. C reaches nothing.
    assert coverage["HP:0001250"] == 2
    assert coverage["HP:0007359"] == 2
    assert coverage["HP:0002197"] == 1
    assert coverage["HP:0032677"] == 2
    assert "HP:9999999" not in coverage


def test_the_tree_holds_direct_edges_only(tmp_path):
    conn = build(tmp_path)
    children = sorted(r[0] for r in conn.execute("SELECT hijo FROM hpo_hijos WHERE padre = 'HP:0001250'"))
    assert children == ["HP:0002197", "HP:0007359"]


def test_labels_are_spanish_with_english_as_fallback(tmp_path):
    conn = build(tmp_path)
    names = dict(conn.execute("SELECT hpo_id, nombre FROM hpo_terminos"))
    assert names["HP:0001250"] == "Convulsiones"
    assert names["HP:0002197"] == "Generalized-onset seizure"
    assert "HP:9999999" not in names


def test_a_rerun_replaces_rather_than_duplicates(tmp_path):
    conn = build(tmp_path)
    before = conn.execute("SELECT COUNT(*) FROM hpo_hijos").fetchone()[0]
    conn.close()
    add_term_tree.run(tmp_path / "genes.db", tmp_path / "hp.obo", tmp_path / "hp-es.json")
    conn = sqlite3.connect(tmp_path / "genes.db")
    assert conn.execute("SELECT COUNT(*) FROM hpo_hijos").fetchone()[0] == before
    info = dict(conn.execute("SELECT clave, valor FROM build_info"))
    assert info["term_tree_hpo_version"] == "hp/releases/2099-01-01"
