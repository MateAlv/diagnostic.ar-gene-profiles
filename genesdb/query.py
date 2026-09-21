"""Read-only access to the gene-profile database.

Two audiences, one call. `professional=False` (the default) returns what a
family sees: the disease first, no biochemistry. `professional=True` adds the
technical summary and protein function, and is only for a caller the platform
has established is a clinician.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class GeneProfileDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"gene profile database not found: {self.path}")
        self._conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def build_info(self) -> dict[str, str]:
        return {r["clave"]: r["valor"] for r in self._conn.execute("SELECT clave, valor FROM build_info")}

    def get(self, symbol: str, *, professional: bool = False,
            max_symptoms: int = 15) -> dict[str, Any] | None:
        """Return one gene profile, or None when the gene has no profile.

        A missing gene is not an error: the HPO release the model ranks over can
        move ahead of the release this database was built from. Callers must
        render the gene without a description rather than hiding it or inventing
        one.
        """
        row = self._conn.execute(
            "SELECT * FROM genes WHERE simbolo = ?", (symbol.strip().upper(),)
        ).fetchone()
        if row is None:
            return None

        diseases = json.loads(row["enfermedades_es"] or "[]")
        omim_codes = json.loads(row["codigo_omim"] or "[]")

        profile: dict[str, Any] = {
            "simbolo": row["simbolo"],
            "nombre_completo": row["nombre_completo"],
            "ubicacion": row["ubicacion"],
            "cromosoma": row["cromosoma"],
            "enfermedades": diseases,
            "resumen_llano_es": row["resumen_llano_es"],
            "sintomas_esperados": self.expected_symptoms(omim_codes, limit=max_symptoms),
            "referencias": json.loads(row["referencias"] or "{}"),
        }

        if professional:
            profile["tecnico"] = {
                "resumen_es": row["tecnico_resumen_es"],
                "funcion_proteina_es": row["tecnico_funcion_es"],
                "resumen_en": row["tecnico_resumen_en"],
                "funcion_proteina_en": row["tecnico_funcion_en"],
                "aliases": row["aliases"],
                "ncbi_id": row["ncbi_id"],
                "uniprot_accession": row["uniprot_accession"],
                "codigo_omim": omim_codes,
            }

        return profile

    def expected_symptoms(self, omim_codes: list[str], *, limit: int = 15) -> list[dict[str, Any]]:
        """Symptoms reported for these diseases, most frequent first."""
        if not omim_codes:
            return []
        placeholders = ",".join("?" * len(omim_codes))
        rows = self._conn.execute(
            f"""SELECT omim, hpo_id, label_en, label_es, frecuencia_raw,
                       frecuencia_pct, frecuencia_es
                FROM sintomas_esperados
                WHERE omim IN ({placeholders})
                ORDER BY frecuencia_pct IS NULL, frecuencia_pct DESC, orden
                LIMIT ?""",
            (*omim_codes, limit),
        ).fetchall()
        return [{
            "omim": r["omim"],
            "hpo_id": r["hpo_id"],
            "nombre": r["label_es"] or r["label_en"],
            "frecuencia_pct": r["frecuencia_pct"],
            "frecuencia_texto": r["frecuencia_es"] or r["frecuencia_raw"],
        } for r in rows]

    def missing(self, symbols: list[str]) -> list[str]:
        """Which of these symbols have no profile — for the miss counter."""
        if not symbols:
            return []
        wanted = [s.strip().upper() for s in symbols]
        placeholders = ",".join("?" * len(wanted))
        present = {
            r["simbolo"] for r in self._conn.execute(
                f"SELECT simbolo FROM genes WHERE simbolo IN ({placeholders})", wanted
            )
        }
        return [s for s in wanted if s not in present]

    def close(self) -> None:
        self._conn.close()
