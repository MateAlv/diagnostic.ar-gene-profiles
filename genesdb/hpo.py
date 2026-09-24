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


def read_gene_diseases(phenotype_to_genes: Path) -> dict[str, set[str]]:
    """Return {gene_symbol: {OMIM codes}} as HPO itself links them.

    The disease list comes from UniProt, which curates a *subset*: it knows
    TCF4 as a type 2 diabetes susceptibility locus and not as the cause of
    Pitt-Hopkins syndrome, even though HPO annotates Pitt-Hopkins with 63
    phenotypes and links it to TCF4. Since expected symptoms are joined by OMIM
    code, a code missing here silently drops every symptom under it — 504 genes
    (10%) lost at least one, and TCF4 was left with three.

    HPO is the right authority for this link: it is the same file the symptoms
    themselves come from, so the two sides cannot disagree.
    """
    diseases: dict[str, set[str]] = defaultdict(set)
    with phenotype_to_genes.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            symbol = (row.get("gene_symbol") or "").strip().upper()
            disease = (row.get("disease_id") or "").strip()
            if symbol and disease.startswith("OMIM:"):
                diseases[symbol].add(disease.split(":", 1)[1])
    log.info("gene-disease links: %d genes, %d codes",
             len(diseases), sum(len(v) for v in diseases.values()))
    return dict(diseases)


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


def read_disease_names(hpoa_path: Path) -> dict[str, str]:
    """Map OMIM number (``"610954"``) -> the English disease name HPO uses."""
    names: dict[str, str] = {}
    with hpoa_path.open(encoding="utf-8") as handle:
        rows = (line for line in handle if not line.startswith("#"))
        for row in csv.DictReader(rows, delimiter="\t"):
            disease = (row.get("database_id") or "").strip()
            name = (row.get("disease_name") or "").strip()
            if disease.startswith("OMIM:") and name:
                names.setdefault(disease.removeprefix("OMIM:"), name)
    return names


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


# A reported phenotype matches an annotated one this many is_a hops below it.
#
# Measured against real extractor output: depth 3 admits "Espasmos infantiles"
# and "Convulsiones febriles" under a reported "Convulsiones", which is exactly
# the relationship a clinician would draw. Depth 4 adds nothing — the useful
# matches are exhausted — and unbounded reaches "Phenotypic abnormality", whose
# 18,708 descendants would make every gene match every symptom.
MAX_ANCESTOR_DEPTH = 3


def read_parents(obo_path: Path) -> dict[str, set[str]]:
    """Map HPO id -> its direct is_a parents."""
    parents: dict[str, set[str]] = {}
    current: str | None = None
    with obo_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if line == "[Term]":
                current = None
            elif line.startswith("id: HP:"):
                current = line[4:].strip()
            elif line.startswith("is_a: ") and current:
                parent = line[6:].split("!")[0].strip()
                if parent:
                    parents.setdefault(current, set()).add(parent)
    log.info("HPO graph: %d terms with parents", len(parents))
    return parents


def build_ancestor_closure(
    parents: dict[str, set[str]],
    terms: set[str],
    max_depth: int = MAX_ANCESTOR_DEPTH,
) -> list[tuple[str, str, int]]:
    """(term, ancestor, hops) for every term, walking up at most `max_depth`.

    Breadth-first so `hops` is the *shortest* path to each ancestor: HPO is a
    DAG, not a tree, and a term reachable by both two and four hops is two hops
    away for our purposes.
    """
    rows: list[tuple[str, str, int]] = []
    for term in terms:
        seen: dict[str, int] = {}
        frontier = {term}
        for distance in range(1, max_depth + 1):
            nxt: set[str] = set()
            for node in frontier:
                for parent in parents.get(node, ()):
                    if parent not in seen:
                        seen[parent] = distance
                        nxt.add(parent)
            if not nxt:
                break
            frontier = nxt
        rows.extend((term, ancestor, d) for ancestor, d in seen.items())
    log.info("ancestor closure: %d edges for %d terms (depth %d)",
             len(rows), len(terms), max_depth)
    return rows
