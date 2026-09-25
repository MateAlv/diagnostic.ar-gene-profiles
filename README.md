# gene-profiles

Spanish-language gene profiles for [diagnostic.ar](https://diagnosticar.infra.cluster.qb.fcen.uba.ar),
so a diagnosis result can explain *what a gene means* instead of only naming it.

For each gene the model can rank, this database holds the associated diseases,
the symptoms reported for those diseases with real frequencies, and — for
clinicians only — the underlying biochemistry.

> The platform suggests a **speciality to consult, never a diagnosis.** Nothing
> served from this database may be presented as one.

## The database is never committed

It is built here and copied to the server with `scp`. `*.db` is gitignored, so
**these scripts are the deliverable** — if they cannot rebuild it, the data is
unreproducible. Every build records its provenance in a `build_info` table:
which HPO release, which day, how many genes.

```bash
pip install -r requirements.txt
scripts/fetch_hpo_translations.sh      # official Spanish HPO labels — do this first

python -m genesdb.build \
  --data-dir ../diagnosticar-question-agent/data \
  --model-genes /path/to/graphens_best_model/genes.json \
  --out genes.db \
  --reuse genes_v4.db        # reuses ~10k existing translations

python -m genesdb.plain_language --db genes.db   # plain-Spanish rewrites

# Additive passes over the built file, in this order. Each is idempotent.
python scripts/add_hpo_graph.py genes.db --obo ../diagnosticar-question-agent/data/hp.obo
python scripts/backfill_hpo_diseases.py genes.db --data-dir ../diagnosticar-question-agent/data
curl -o mim2gene.txt https://omim.org/static/omim/data/mim2gene.txt
python scripts/add_gene_mim.py genes.db --mim2gene mim2gene.txt

python scripts/verify_db.py genes.db && \
  scp genes.db nodo0-diagnostic.ar:~/…          # deploy is a copy, nothing more
```

A full build is **50–70 minutes**, a full rewrite pass about **an hour**.

**`--model-genes` matters.** The model can only return symbols from its own
vocabulary; the HPO annotation files omit 126 of them. See
[docs/BUILDING.md](docs/BUILDING.md).

The plain-language pass is **additive** — it writes only `resumen_llano_es` and
never touches anything already in the database. It needs **human review before
shipping**; the protocol and the sampling tool are in
[docs/PLAIN_LANGUAGE.md](docs/PLAIN_LANGUAGE.md).

### Where the data comes from

| Source | Gives |
|---|---|
| `phenotype_to_genes.txt` (HPO) | the gene universe + `ncbi_gene_id` |
| NCBI Gene (E-utilities) | name, chromosome, location, aliases, summary |
| UniProtKB (reviewed only) | protein function, diseases, OMIM ids |
| `phenotype.hpoa` + `hp.obo` (HPO) | **expected symptoms with frequencies** |
| `hp-es.babelon.tsv` (HPO i18n) | official Spanish symptom names, 97.6% coverage |
| `phenotype_to_genes.txt` + `phenotype.hpoa` again | the OMIM codes and condition names UniProt omits |
| `mim2gene.txt` (OMIM, public) | each gene's own OMIM entry number |

The HPO files currently live in `../diagnosticar-question-agent/data/`
(release **2026-06-23**).

Symptom names come from the **official HPO Spanish translations**, which are
human-curated and cover 97.6% of terms. Only gene summaries and the remaining
~481 labels go through machine translation, which is isolated in
`genesdb/translate.py`. That endpoint is undocumented and unauthenticated and
**will block an IP that calls it at volume** — see
[docs/BUILDING.md](docs/BUILDING.md). Builds resume from disk caches, so a
block costs time rather than work.

## Two audiences, one call

The default view is for **families**; the technical view is for **clinicians**
and is returned only when the caller explicitly asks:

```python
from genesdb.query import GeneProfileDB

db = GeneProfileDB("genes.db")

db.get("CFTR")                      # disease first, no biochemistry
db.get("CFTR", professional=True)   # adds resumen/función, EN + ES
db.missing(["CFTR", "MED16"])       # symbols with no profile, for the miss counter
```

`get()` returns `None` for an unknown gene. **That is expected, not an error** —
the HPO release the model ranks over can move ahead of the release this database
was built from. Callers must show the gene without a description rather than
hiding it or inventing one.

## Layout

```
genesdb/
  build.py      orchestration + CLI
  sources.py    NCBI and UniProt fetch (the stage that was missing)
  hpo.py        gene universe, HPO labels, expected symptoms
  translate.py  EN→ES, cached
  schema.py     tables, layered by audience
  query.py      read-only access
  plain_language.py  build-time plain-Spanish rewrites, batched
  codex.py           LLM access through the codex CLI
tests/          27 tests, no network
docs/           BUILDING.md, PLAIN_LANGUAGE.md (includes the review protocol)
scripts/        review_sample.py — draws a manual-review sample
legacy/         the original scripts, kept for reference — see below
```

`legacy/crear_db.py` could not rebuild anything: its path constants were empty
and the per-gene JSON files it expected are not in this repository. The script
that actually queried NCBI/UniProt/OMIM never existed here, which is why the
fetch stage had to be written from scratch.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

No network calls, so this is CI-safe. The audience-separation tests are
mutation-checked: removing the `professional` gate in `query.py` makes
`test_family_view_excludes_biochemistry` fail.
