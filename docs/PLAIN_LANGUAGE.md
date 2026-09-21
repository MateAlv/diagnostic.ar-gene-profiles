# Plain-language rewrites

The technical summaries are accurate but written for geneticists. A family
reading about their child sees *"un miembro de la superfamilia de
transportadores del casete de unión a ATP"* — true, and useless.

`genesdb/plain_language.py` rewrites each one into two sentences of plain
Rioplatense Spanish, stored in `resumen_llano_es`.

```bash
python -m genesdb.plain_language --db genes.db
```

## It is additive

**The pass writes exactly one column: `resumen_llano_es`.** The technical text
is never overwritten, so the professional view keeps its provenance and the
family view is an addition, not a replacement. Concretely:

- A gene that already has a rewrite is **skipped**, unless `--overwrite`.
- A rejected rewrite **leaves the column as it was** — it never blanks a value
  that was already good.
- No other column, table or row is touched.

Two tests enforce this, and both are mutation-checked:
`test_rewrite_only_touches_the_plain_language_column` fails if any other column
changes, and `test_rejected_rewrite_leaves_existing_value_untouched` fails if a
rejection clobbers an existing value.

The run is resumable. Interrupt it and start it again; it picks up where it
stopped.

## Why it runs at build time

Not at request time, deliberately:

- it can be **reviewed before it ships**,
- it costs nothing per request and adds no latency,
- it **cannot fabricate text while a family is reading the screen**.

It goes through the `codex` CLI rather than a paid API key.

## Batching

One call per gene is ~13s, so 5,228 genes would be **~19 hours**. Per-call
overhead dominates the actual work, so genes are batched.

Measured: **25 genes in 52 seconds** in one call, 24 usable rewrites, quality
indistinguishable from single-gene calls. With 3 concurrent calls a full run is
roughly **one hour**.

```
--batch-size 25    # genes per codex call
--workers 3        # concurrent calls
```

Larger batches were not pursued: the prompt and response grow linearly, and a
batch that fails loses more work. 25 is comfortably inside the response limit
with room for long summaries.

A batch that fails entirely is logged and skipped — the run continues, and
rerunning picks up whatever was missed.

## The safety validator

Every rewrite is checked before it is stored, and rejected if it:

- exceeds **300 characters**, or
- contains a term that turns a gene description into medical advice:
  `diagnóstico`, `pronóstico`, `tratamiento`, `cura`, `terapia`,
  `probabilidad de tener/padecer`, `usted tiene`, `su hijo tiene`.

A rejected gene keeps whatever it had. Empty is a legitimate answer: the prompt
instructs the model to return an empty string when the technical text is too
thin to say anything useful, and that is better than padding.

The platform suggests **a speciality to consult, never a diagnosis.** This
validator is where that could quietly stop being true, which is why it fails
closed.

**The validator is not review.** It catches forbidden words and length. It
cannot catch a fluent, confident, wrong sentence — which is the actual risk when
simplifying clinical text. See below.

---

# How to review this by hand

You cannot read 5,228 rewrites, and you should not try. The goal is to be able
to say, with a number behind it, *"I checked a sample and the error rate was
under X"* — and to have looked hardest where errors are most likely.

## What you are looking for

Four failure modes, in descending order of severity:

1. **Invented content** — a claim that is not in the technical text. The worst
   and the hardest to spot, because it reads fluently.
2. **Meaning inverted or lost** — the rewrite says the gene does something it
   does not, or drops the part that mattered.
3. **Reads as medical advice** — implies diagnosis, prognosis or treatment
   despite passing the validator.
4. **Still incomprehensible** — jargon survived; the rewrite achieved nothing.

For each row the verdict is simply **OK / not OK**, plus which of the four it
was. Do not rewrite as you go — collect verdicts first, decide after.

## The protocol

**Roughly 150 rows, in four passes, about 2–3 hours total.** Use
`scripts/review_sample.py` (see below) to generate each batch as a side-by-side
table.

### Pass 1 — the risky tail, ~40 rows (not random)

Errors concentrate where the source is hardest, so look there first:

- the 20 genes with the **longest** technical summaries (most to compress, most
  to drop),
- the 20 with the **shortest** non-empty ones (least to work from, most
  temptation to invent).

If this pass is clean, random sampling is much more meaningful. If it is not,
stop and fix the prompt before sampling anything.

### Pass 2 — random sample, 60 rows

A uniform random sample with a **fixed seed**, so the same sample can be pulled
again after a change and the two compared directly.

60 rows gives a usable bound: 0 errors in 60 means the true rate is under ~5%
with 95% confidence. That is the number worth quoting.

### Pass 3 — the genes people will actually see, ~30 rows

A uniform sample under-weights what matters. Add the genes most likely to be
ranked highly: the 30 with the **most HPO annotations**
(`sintomas_esperados`), which are the genes most connected to real phenotypes.
An error here reaches far more families than an error in an obscure gene.

### Pass 4 — spot-check the empties, ~20 rows

Genes where the pass wrote nothing. Confirm the source really was too thin. A
cluster of empties with rich source text means the prompt or the validator is
too aggressive — a silent failure that looks like success.

## Recording the result

One row per checked gene: `simbolo`, `verdict`, `failure_mode`, `note`. Keep it
in the repo next to the build it describes.

Then decide with a rule fixed **before** looking:

| Outcome | Action |
|---|---|
| 0 severity-1 or 2 errors in the sample | ship it |
| 1 error | fix the prompt, rerun with `--overwrite`, re-review the same seed |
| 2 or more | do not ship; the approach needs rethinking |

Severity 3 and 4 are worth fixing but are not ship-blockers on their own.

**Any invented content is disqualifying regardless of rate.** A fabricated
statement about a gene, shown to a family about their child, is the failure this
project cannot have.

## Re-reviewing after a change

Use the **same seed**. If the prompt changes, the previous verdicts are void for
the rows that changed — but comparing the same rows before and after is the
cheapest way to see whether the change helped or just moved the problem.

Review is **per build**. A rebuild with a new HPO release regenerates the
technical text, so the rewrites change and the sample must be redrawn.
