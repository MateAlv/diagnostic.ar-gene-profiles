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

## Timing and rate limits

A full build is roughly **50–70 minutes**, almost entirely network-bound.

| Stage | Cost | Notes |
|---|---|---|
| NCBI esummary | ~2 min | batched 200 ids per request, ~3 req/s |
| NCBI esearch (126 symbols) | ~1 min | only for genes absent from HPO |
| UniProt | ~1 min | ~0.6 s/gene measured |
| Translation | ~45 min | the dominant cost |

Translation dominates because the **~20,000 distinct HPO symptom labels** have
never been translated before, while gene summaries are largely served from
cache. The build therefore:

1. gathers every string that needs translating across all genes,
2. **deduplicates** them — symptom labels repeat heavily across diseases,
3. translates them concurrently under a shared rate limiter,
4. then assembles the rows from the finished cache.

Measured at **7.3 requests/second** with 6 workers and no throttling. The
endpoint is undocumented and unauthenticated, so the defaults stay well under
what it tolerates:

```
--workers 6            # concurrent requests
--translate-rate 8     # hard ceiling, requests/second across all workers
```

A token-bucket limiter with jitter enforces the ceiling across threads, and the
client backs off exponentially (2s → 60s) on HTTP 429, 403 or 5xx. If a string
still cannot be translated it **falls back to the English source** rather than
leaving the field blank, and the build logs how many. Raising these numbers
risks getting the endpoint blocked; there is no reason to.

`--reuse genes_v4.db` seeds the cache with ~10,000 translations from a previous
build and is worth passing every time.

### Other flags

| Flag | Use |
|---|---|
| `--only CFTR MED16` | build specific genes |
| `--limit N` | build the first N genes |
| `--skip-translation` | keep English; fast structural checks |
| `--ncbi-api-key` | raises NCBI from 3 to 10 req/s |

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
