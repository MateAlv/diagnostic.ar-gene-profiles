"""Read the HPO annotation files: the gene universe and expected symptoms.

The gene universe and the expected-symptom lists both come from an HPO release,
so the release version is recorded alongside the data it produced.
"""

from __future__ import annotations

import csv
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Frequencies arrive as an HPO term, a percentage, or a ratio of observations.
_HPO_FREQUENCY_LABELS = {
    "HP:0040280": ("Siempre presente", 100.0),
    "HP:0040281": ("Muy frecuente", 90.0),
    "HP:0040282": ("Frecuente", 55.0),
    "HP:0040283": ("Ocasional", 17.0),
    "HP:0040284": ("Poco frecuente", 4.0),
    "HP:0040285": ("Ausente", 0.0),
}


@dataclass(frozen=True)
class Symptom:
    hpo_id: str
    label_en: str
    frequency_raw: str
    frequency_pct: float | None
    frequency_label: str


def read_hpo_version(hpoa_path: Path) -> str:
    """Read the release date from phenotype.hpoa's header comments."""
    with hpoa_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.startswith("#"):
                break
            match = re.match(r"#version:\s*(\S+)", line)
            if match:
                return match.group(1)
    return "unknown"


def read_gene_universe(phenotype_to_genes: Path) -> dict[str, str]:
    """Return {gene_symbol: ncbi_gene_id} for every gene the model can rank."""
    genes: dict[str, str] = {}
    with phenotype_to_genes.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            symbol = (row.get("gene_symbol") or "").strip().upper()
            ncbi_id = (row.get("ncbi_gene_id") or "").strip()
            if symbol and ncbi_id:
                genes.setdefault(symbol, ncbi_id)
    log.info("gene universe: %d symbols", len(genes))
    return genes


def read_hpo_labels(obo_path: Path) -> dict[str, str]:
    """Map HPO id -> English term name from hp.obo."""
    labels: dict[str, str] = {}
    current: str | None = None
    with obo_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line.startswith("id: HP:"):
                current = line[4:].strip()
            elif line.startswith("name: ") and current:
                labels[current] = line[6:].strip()
                current = None
    log.info("HPO labels: %d terms", len(labels))
    return labels


def _parse_frequency(raw: str) -> tuple[float | None, str]:
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    if raw in _HPO_FREQUENCY_LABELS:
        label, pct = _HPO_FREQUENCY_LABELS[raw]
        return pct, label
    if raw.endswith("%"):
        try:
            return float(raw[:-1]), ""
        except ValueError:
            return None, ""
    if "/" in raw:
        numerator, _, denominator = raw.partition("/")
        try:
            den = float(denominator)
            if den:
                return 100.0 * float(numerator) / den, ""
        except ValueError:
            return None, ""
    return None, ""


def read_expected_symptoms(hpoa_path: Path, labels: dict[str, str]) -> dict[str, list[Symptom]]:
    """Map disease id (e.g. ``OMIM:219700``) -> observed phenotypes, most frequent first.

    Only positive phenotypic abnormalities are kept: ``aspect == "P"`` and the
    ``NOT`` qualifier excluded, so a symptom explicitly absent in a disease is
    never presented as expected.
    """
    by_disease: dict[str, list[Symptom]] = defaultdict(list)
    with hpoa_path.open(encoding="utf-8") as handle:
        rows = (line for line in handle if not line.startswith("#"))
        for row in csv.DictReader(rows, delimiter="\t"):
            if row.get("aspect") != "P" or (row.get("qualifier") or "").strip() == "NOT":
                continue
            hpo_id = (row.get("hpo_id") or "").strip()
            disease = (row.get("database_id") or "").strip()
            if not hpo_id or not disease:
                continue
            pct, label = _parse_frequency(row.get("frequency", ""))
            by_disease[disease].append(Symptom(
                hpo_id=hpo_id,
                label_en=labels.get(hpo_id, ""),
                frequency_raw=(row.get("frequency") or "").strip(),
                frequency_pct=pct,
                frequency_label=label,
            ))

    for disease, symptoms in by_disease.items():
        symptoms.sort(key=lambda s: (s.frequency_pct is None, -(s.frequency_pct or 0.0)))

    log.info("expected symptoms: %d diseases", len(by_disease))
    return dict(by_disease)


def read_spanish_labels(babelon_path: Path) -> dict[str, str]:
    """Official Spanish HPO labels from the HPO Internationalisation Effort.

    These are human-curated, so they are both better than machine translation
    and free — they remove ~20k calls to the translation endpoint, which is
    what got this build rate-limited in the first place.

    File: hpo-translations/babelon/hp-es.babelon.tsv
    """
    if not babelon_path.exists():
        log.warning("no Spanish HPO labels at %s — symptom names "
                    "will fall back to translation", babelon_path)
        return {}

    labels: dict[str, str] = {}
    with babelon_path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row.get("predicate_id") != "rdfs:label":
                continue
            subject = (row.get("subject_id") or "").strip()
            value = (row.get("translation_value") or "").strip()
            if subject and value:
                labels[subject] = value

    log.info("official Spanish HPO labels: %d", len(labels))
    return labels
