"""Database schema.

Layered by audience. `clinical_*` columns are the default view for families;
`tecnico_*` columns hold the biochemistry and are served only when the caller
has asked for the professional view.
"""

from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS genes (
    simbolo             TEXT PRIMARY KEY,
    nombre_completo     TEXT,
    cromosoma           TEXT,
    ubicacion           TEXT,
    aliases             TEXT,

    -- Family-facing: the disease is the headline.
    enfermedades_es     TEXT,   -- JSON [{nombre, descripcion_es, omim}]
    resumen_llano_es    TEXT,   -- plain-language rewrite; NULL until generated

    -- Professional view: the source biochemistry, never rewritten.
    tecnico_resumen_es      TEXT,
    tecnico_funcion_es      TEXT,
    tecnico_resumen_en      TEXT,
    tecnico_funcion_en      TEXT,

    ncbi_id             TEXT,
    uniprot_accession   TEXT,
    codigo_omim         TEXT,   -- JSON []
    referencias         TEXT,   -- JSON {}
    raw_json            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_genes_simbolo ON genes(simbolo);

-- Expected symptoms per disease, from the HPO annotation file.
CREATE TABLE IF NOT EXISTS sintomas_esperados (
    omim            TEXT NOT NULL,
    hpo_id          TEXT NOT NULL,
    label_en        TEXT,
    label_es        TEXT,
    frecuencia_raw  TEXT,
    frecuencia_pct  REAL,
    frecuencia_es   TEXT,
    orden           INTEGER NOT NULL,
    PRIMARY KEY (omim, hpo_id)
);

CREATE INDEX IF NOT EXISTS idx_sintomas_omim ON sintomas_esperados(omim);

-- Ancestor closure over HPO's is_a graph, used to match a reported phenotype
-- against the more specific ones a disease is annotated with.
--
-- Families describe symptoms in general language and the extractor returns
-- general terms ("Convulsiones"), while disease annotations are specific
-- ("Espasmos infantiles"). An exact id comparison never fires, so every gene
-- reads "sin coincidencias". One row per (specific term, general ancestor)
-- pair, with the hop distance between them.
--
-- Bounded at MAX_ANCESTOR_DEPTH hops: a match two or three steps down is a
-- real clinical relationship, while an unbounded walk reaches "Phenotypic
-- abnormality" and makes everything match everything.
CREATE TABLE IF NOT EXISTS hpo_ancestros (
    hpo_id      TEXT NOT NULL,   -- the specific term a disease is annotated with
    ancestro_id TEXT NOT NULL,   -- a more general term that subsumes it
    distancia   INTEGER NOT NULL,
    PRIMARY KEY (hpo_id, ancestro_id)
);

CREATE INDEX IF NOT EXISTS idx_ancestros_ancestro ON hpo_ancestros(ancestro_id);

-- The whole HPO tree, for browsing from a general term to more specific ones
-- (the wizard's "too general" dialog). Unlike hpo_ancestros, which only
-- climbs a few hops from annotated terms, this holds every term and every
-- direct is_a edge, with the same Spanish labels the symptom search shows.
CREATE TABLE IF NOT EXISTS hpo_terminos (
    hpo_id    TEXT PRIMARY KEY,
    nombre    TEXT NOT NULL,     -- Spanish label, English where none exists
    nombre_en TEXT
);

CREATE TABLE IF NOT EXISTS hpo_hijos (
    padre TEXT NOT NULL,
    hijo  TEXT NOT NULL,
    PRIMARY KEY (padre, hijo)
);

-- How many genes a term reaches: genes with a disease annotated with the term
-- or with anything beneath it. The measure of how much a term narrows a
-- search -- "Convulsiones" reaches over a thousand, "Espasmos infantiles" a
-- fraction of that. Terms that reach no gene are left out.
CREATE TABLE IF NOT EXISTS termino_cobertura (
    hpo_id TEXT PRIMARY KEY,
    genes  INTEGER NOT NULL
);

-- One row. Records what this database was built from.
CREATE TABLE IF NOT EXISTS build_info (
    clave TEXT PRIMARY KEY,
    valor TEXT
);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def write_build_info(conn: sqlite3.Connection, info: dict[str, str]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO build_info (clave, valor) VALUES (?, ?)",
        list(info.items()),
    )
    conn.commit()
