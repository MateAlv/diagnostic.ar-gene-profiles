# Building the database

The database is never committed. It is built here and copied to the server with
`scp`, so these scripts are the only record of how it was made. Every build
writes a `build_info` table recording the HPO release, the date, the gene
universe used and the gene count.

```bash
pip install -r requirements.txt

python -m genesdb.build \
  --data-dir ../diagnosticar-question-agent/data \
  --model-genes /path/to/graphens_best_model/genes.json \
  --out genes.db \
  --reuse genes_v4.db
```

## `--model-genes` is not optional in practice

GenPhenAI's classifier has a fixed output layer of 5229 classes, so it can only
ever return a symbol from its own vocabulary — `genes.json` in the checkpoint
directory. The first entry, `-`, is a placeholder, leaving **5,228 real genes**.

The HPO annotation files are the wrong list in both directions: they contain
165 genes the model can never return, and **omit 126 that it can**. Building
without `--model-genes` silently loses those 126 (`ABCC4`, `ACADL`, `AGTR2`,
`BMP10`…), because they carry no HPO annotation at all.

Those 126 therefore have no `ncbi_gene_id` in the annotation files either, so
the build resolves their symbols through NCBI esearch. All 126 resolve.

**When GenPhenAI is retrained the vocabulary changes.** Copy the new
`genes.json` and rebuild, or the two drift apart again.

## Translation: use the official labels, not the web endpoint

A build was rate-limited to a standstill on 2026-09-21: 1,020 HTTP 429s, then
the endpoint refused every request from this IP for over half an hour. The
cause was volume — roughly 20,000 HPO symptom labels, none of them cached.

**That volume is now avoidable.** The HPO Internationalisation Effort publishes
human-curated Spanish labels covering **97.6% of HPO terms** (19,932 of 20,413,
all marked `OFFICIAL`):

```bash
scripts/fetch_hpo_translations.sh
```

This drops the per-build translation load from ~20,413 strings to **481**, and
the labels are better than machine output — *Failure to thrive* becomes
*Retraso en el medro*, the term a clinician would use, rather than a literal
rendering.

The build prefers an official label whenever one exists and only falls back to
translation for the remainder. Run the fetch script before a build; without the
file the build still works, it is just slow and liable to be throttled again.

**The platform already uses this same file.** `diagnostic.ar-api` keeps a copy
at `data/hpo/hp-es.tsv`, converts it with `scripts/build_hpo_es.py` and serves
the result as `label_es` from `/extract-hpo` — so the Spanish symptom names a
user sees in the wizard come from exactly this source. Refresh both together,
or the same HPO term will be named one way in the wizard and another on a gene
card.

### Disease names go through codex, not the web endpoint

The public endpoint blocked this build twice, and disease names were the
casualty both times — the second run finished with 39% of them still in
English. `genesdb/translate_codex.py` translates them through the `codex` CLI
instead:

```bash
python -m genesdb.translate_codex --db genes.db
```

Roughly 50 names per call, three calls in parallel. It is **additive**: it
writes disease names only, never touches descriptions, and a name with no
translation keeps what it had.

It is also better than a general translator for this material, because it knows
standard Spanish medical nomenclature and leaves eponyms and type numbers
intact:

| English | codex |
|---|---|
| Acrofacial dysostosis 1, Nager type | Disostosis acrofacial 1, tipo Nager |
| Acyl-CoA dehydrogenase medium-chain deficiency | Deficiencia de acil-CoA deshidrogenasa de cadena media |
| Bardet-Biedl syndrome 1 | Síndrome de Bardet-Biedl de tipo 1 |

A translation is accepted only for a string that was actually sent, so a
hallucinated key cannot poison the cache for a later build.

### Can the translation endpoint be authorised?

Short answer: **the web endpoint cannot be, and now barely matters.**

`translate.googleapis.com/translate_a/single?client=gtx` is the undocumented
endpoint the browser widget uses. It takes no API key — there is no header or
parameter that raises its limits, because it was never meant to be called
directly. Being blocked is the expected outcome of using it at volume, and no
amount of configuration changes that.

If the remaining 481 strings ever become a problem, the real options are:

| Option | Cost | Notes |
|---|---|---|
| **Official HPO labels** | free | already covers 97.6%; do this first |
| **Google Cloud Translation API** | ~$20 per million chars | a real key, real quotas, no blocking |
| **Local model via Ollama** | free | `qwen3.5:4b` is on this box; slower, needs review |
| **Wait it out** | free | the block is temporary; the build resumes from cache |

A paid key is only worth it if translation becomes a recurring bottleneck. With
the official labels in place, a full build now needs a few hundred calls, which
is well inside what the endpoint tolerates.

## Timing and rate limits

A full build is roughly **50–70 minutes**, almost entirely network-bound.

| Stage | Cost | Notes |
|---|---|---|
| NCBI esummary | ~2 min | batched 200 ids per request |
| NCBI esearch (126 symbols) | ~1 min | only for genes absent from HPO |
| UniProt | ~35 min | one request per gene |
| Translation | a few min | with official labels; ~45 min without |

The defaults are deliberately conservative after the block:

```
--workers 2            # concurrent requests
--translate-rate 2     # hard ceiling, requests/second across all workers
```

A token-bucket limiter with jitter enforces the ceiling across threads, and the
client backs off exponentially (2s → 60s) on 429, 403 or 5xx. **Sustained
throttling is treated as a block rather than as slowness:** after 60
consecutive rejections the stage stops and reports what remains, because
backing off further cannot fix a block.

### Builds resume

Both the fetched source data and the translations are cached to disk next to
the output (`genes.fetch.json`, `genes.translations.json`) and reloaded on the
next run. An interrupted build picks up where it stopped rather than repeating
~35 minutes of NCBI and UniProt work. Both are gitignored.

`--reuse genes_v4.db` additionally seeds ~10,000 translations from a previous
build.

### Other flags

| Flag | Use |
|---|---|
| `--only CFTR MED16` | build specific genes |
| `--limit N` | build the first N genes |
| `--skip-translation` | keep English; fast structural checks |
| `--ncbi-api-key` | raises NCBI from 3 to 10 req/s |

## Verify before copying

Nothing between this machine and the results screen inspects the database: it
is built here and shipped with `scp`. A translation stage that stopped partway
once put **809 English texts on a Spanish page** in front of families, and
nothing failed — the build reported success, the copy succeeded, and the defect
was found by reading the staging site.

So the copy is gated:

```bash
python scripts/verify_db.py genes.db && \
  scp genes.db nodo0-diagnostic.ar:~/diagnostic.ar/diagnostic.ar-staging/data/genes.db
```

It checks the gene count against the model's vocabulary, summary coverage,
expected-symptom volume, recorded provenance, and how much text still reads as
English. Thresholds sit just above what a good build produces, so ordinary
variation passes and a stage that silently did nothing does not.

### Publishing a release

The database and the files it was built from exist only where they were
built. Every database that reaches a server is also published as a GitHub
release, so it can be fetched, checked and rebuilt from somewhere other than
one laptop:

```bash
scripts/package_release.sh dist/      # verifies genes.db, then packs it
gh release create db-vX.Y.Z dist/genes-db.tar.gz dist/genes-build-inputs.tar.gz dist/SHA256SUMS \
  --target main --title db-vX.Y.Z --notes "..."
```

`genes-build-inputs.tar.gz` carries `genes.fetch.json`,
`genes.translations.json`, `genes.summaries.json` and `genes_v4.db`: without
them a rebuild pays for every LLM translation again, and does not come out the
same. The archives are reproducible, so packing the same files twice gives the
same sha256 — that hash is what a deployment pins.

### The HPO ancestor graph

The database carries a closure over HPO's `is_a` graph (`hpo_ancestros`), which
is what lets a reported phenotype match the more specific ones a disease is
annotated with. Without it the results screen reads **"sin coincidencias" on
every gene**: families write general language, so the extractor returns
*Convulsiones*, while diseases are annotated with *Espasmos infantiles*.

```bash
python scripts/add_hpo_graph.py genes.db --obo ../diagnostic.ar-api/data/hpo/hp.obo
```

Bounded at **three hops**, and the bound is load-bearing. Depth 3 admits
*Convulsiones febriles* under a reported *Convulsiones*; depth 4 adds nothing;
unbounded, every term reaches *Phenotypic abnormality* and its 18,708
descendants, at which point every gene matches every symptom.

A descendant-count cap was tried first and rejected: *Seizure* has 346
descendants and *Abnormality of the mouth* has 493, so no threshold separates a
useful clinical finding from a structural category. Hop distance does.

Only the terms diseases are actually annotated with get a closure — 8,728 of
the ontology's 19,934, about 35,000 edges and 3 MB.

### The HPO term tree and gene coverage

The wizard warns when a reported symptom barely narrows the search and lets the
person browse down to a more specific one. That reads three tables: every HPO
term with its Spanish label (`hpo_terminos`), every direct `is_a` edge
(`hpo_hijos`), and how many genes each term reaches — genes with a disease
annotated with it or with anything beneath it (`termino_cobertura`).

```bash
python3 scripts/add_term_tree.py genes.db \
  --obo ../diagnostic.ar-api/data/hpo/hp.obo \
  --labels ../diagnostic.ar-api/data/hpo/hp-es.json
```

Use the symptom search's own `hp.obo` and `hp-es.json`, so a term reads the
same in the search and in the dialog. Additive and idempotent; half a second.
`verify_db.py` fails a database without it. For scale: "Convulsiones" reaches
1,560 genes, "Micropene" 214, "Fiebre baja" 1; 1,000 terms reach 100 or more.

### Repairing an interrupted translation

The two Spanish columns drift apart independently — a gene can have a Spanish
summary and an English function, or the reverse — so the rows are found by
reading the text rather than by a flag:

```bash
python -m genesdb.translate_summaries --db genes.db --report   # count only
python -m genesdb.translate_summaries --db genes.db            # fix them
```

Additive and resumable: translations are cached per batch, a failed batch
leaves its rows untouched, and a reply that comes back still in English is
refused rather than stored — storing it would hide the row from the next run.

## Where the data comes from

| Source | Gives |
|---|---|
| `genes.json` (model checkpoint) | the authoritative gene vocabulary |
| `phenotype_to_genes.txt` (HPO) | `ncbi_gene_id` for most genes |
| NCBI Gene (E-utilities) | name, chromosome, location, aliases, summary |
| UniProtKB (reviewed only) | protein function, diseases, OMIM ids |
| `phenotype.hpoa` + `hp.obo` (HPO) | expected symptoms with frequencies |

Symptoms carrying the `NOT` qualifier are excluded, so a phenotype recorded as
*absent* in a disease is never presented as expected.
