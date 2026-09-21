"""Fetch gene profiles from NCBI Gene, UniProtKB and the HPO annotation files.

This is the stage that was missing: the original `crear_db.py` only repaired and
compiled a directory of per-gene JSON files that no longer exists, so the
database could not be rebuilt from scratch.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

NCBI_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
NCBI_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
UNIPROT_SEARCH = "https://rest.uniprot.org/uniprotkb/search"

# NCBI allows 3 requests/second without an API key, 10 with one.
NCBI_BATCH_SIZE = 200


class FetchError(RuntimeError):
    pass


@dataclass
class GeneProfile:
    simbolo: str
    ncbi_id: str = ""
    nombre_completo: str = ""
    cromosoma: str = ""
    ubicacion: str = ""
    aliases: str = ""
    resumen: str = ""
    funcion_proteina: str = ""
    uniprot_accession: str = ""
    enfermedades_asociadas: list[dict[str, str]] = field(default_factory=list)
    codigo_omim: list[str] = field(default_factory=list)
    referencias: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "simbolo": self.simbolo,
            "nombre_completo": self.nombre_completo,
            "cromosoma": self.cromosoma,
            "ubicacion": self.ubicacion,
            "aliases": self.aliases,
            "resumen": self.resumen,
            "funcion_proteina": self.funcion_proteina,
            "enfermedades_asociadas": self.enfermedades_asociadas,
            "codigo_omim": self.codigo_omim,
            "referencias": self.referencias,
        }


def _session(api_key: str | None = None) -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = "diagnostic.ar gene-profile builder (contact: diagnostic.ar)"
    if api_key:
        s.params = {"api_key": api_key}  # type: ignore[assignment]
    return s


def _get_json(session: requests.Session, url: str, params: dict[str, Any],
              *, retries: int = 4, timeout: int = 30) -> Any:
    delay = 1.0
    last: Exception | None = None
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                raise FetchError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001 - retried below, raised at the end
            last = exc
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    raise FetchError(f"{url} failed after {retries} attempts: {last}")


def fetch_ncbi(ncbi_ids: Iterable[str], session: requests.Session,
               *, throttle: float = 0.34) -> dict[str, GeneProfile]:
    """Fetch NCBI Gene summaries in batches, keyed by NCBI gene id."""
    ids = [str(i).strip() for i in ncbi_ids if str(i).strip()]
    out: dict[str, GeneProfile] = {}

    for start in range(0, len(ids), NCBI_BATCH_SIZE):
        batch = ids[start:start + NCBI_BATCH_SIZE]
        payload = _get_json(session, NCBI_ESUMMARY, {
            "db": "gene", "id": ",".join(batch), "retmode": "json",
        })
        result = payload.get("result", {})
        for uid in result.get("uids", []):
            rec = result.get(uid) or {}
            symbol = str(rec.get("name", "")).strip().upper()
            if not symbol:
                continue
            out[str(uid)] = GeneProfile(
                simbolo=symbol,
                ncbi_id=str(uid),
                nombre_completo=str(rec.get("description", "")).strip(),
                cromosoma=str(rec.get("chromosome", "")).strip(),
                ubicacion=str(rec.get("maplocation", "")).strip(),
                aliases=str(rec.get("otheraliases", "")).strip(),
                resumen=str(rec.get("summary", "")).strip(),
                referencias={"NCBI Gene": f"https://www.ncbi.nlm.nih.gov/gene/{uid}"},
            )
        log.info("NCBI: %d/%d genes", min(start + NCBI_BATCH_SIZE, len(ids)), len(ids))
        time.sleep(throttle)

    return out


def fetch_uniprot(symbol: str, session: requests.Session,
                  *, throttle: float = 0.2) -> dict[str, Any]:
    """Fetch reviewed (SwissProt) protein function and disease annotations."""
    payload = _get_json(session, UNIPROT_SEARCH, {
        "query": f"gene:{symbol} AND organism_id:9606 AND reviewed:true",
        "fields": "accession,cc_function,cc_disease",
        "format": "json",
        "size": 1,
    })
    results = payload.get("results") or []
    time.sleep(throttle)
    if not results:
        return {}

    entry = results[0]
    function = ""
    diseases: list[dict[str, str]] = []
    omim: list[str] = []

    for comment in entry.get("comments", []):
        kind = comment.get("commentType")
        if kind == "FUNCTION" and not function:
            texts = comment.get("texts") or []
            if texts:
                function = texts[0].get("value", "")
        elif kind == "DISEASE":
            disease = comment.get("disease") or {}
            name = disease.get("diseaseId") or ""
            if not name:
                continue
            description = (disease.get("description") or "").strip()
            cross_ref = disease.get("diseaseCrossReference") or {}
            mim = cross_ref.get("id", "") if cross_ref.get("database") == "MIM" else ""
            diseases.append({"nombre": name, "descripcion": description, "omim": mim})
            if mim:
                omim.append(mim)

    accession = entry.get("primaryAccession", "")
    return {
        "uniprot_accession": accession,
        "funcion_proteina": function,
        "enfermedades_asociadas": diseases,
        "codigo_omim": omim,
        "referencia": f"https://www.uniprot.org/uniprotkb/{accession}" if accession else "",
    }


def resolve_symbols(symbols: Iterable[str], session: requests.Session,
                    *, throttle: float = 0.34) -> dict[str, str]:
    """Resolve gene symbols to NCBI gene ids.

    Needed for genes the model ranks that carry no HPO annotation, so their id
    cannot be read out of the annotation files.
    """
    resolved: dict[str, str] = {}
    pending = [s.strip().upper() for s in symbols if s.strip()]

    for index, symbol in enumerate(pending, start=1):
        try:
            payload = _get_json(session, NCBI_ESEARCH, {
                "db": "gene",
                "term": f"{symbol}[sym] AND human[orgn]",
                "retmode": "json",
            })
            ids = (payload.get("esearchresult") or {}).get("idlist") or []
            if ids:
                resolved[symbol] = str(ids[0])
            else:
                log.warning("no NCBI gene id for symbol %s", symbol)
        except FetchError as exc:
            log.warning("symbol lookup failed for %s: %s", symbol, exc)
        if index % 50 == 0:
            log.info("resolved %d/%d symbols", index, len(pending))
        time.sleep(throttle)

    return resolved
