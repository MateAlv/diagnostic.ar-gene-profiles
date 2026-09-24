#!/usr/bin/env python3
"""Record each gene's own OMIM entry number.

    curl -o mim2gene.txt https://omim.org/static/omim/data/mim2gene.txt
    python scripts/add_gene_mim.py genes.db --mim2gene mim2gene.txt

`codigo_omim` holds the gene's *diseases* (TCF4 → 610954, Pitt-Hopkins). The
gene has an entry of its own (TCF4 → 602272), and a link to OMIM for the gene
needs that number; linking a disease number instead would open the wrong page.

Matched on the approved symbol first, then on the NCBI Gene id. The symbol
wins because one stored id is wrong: CNP carries 4880, which is NPPC (whose
alias is "CNP"), so an id match linked it to the wrong gene. The id catches the
29 symbols HGNC has since renamed (CENPJ is now CPAP), which the symbol misses.
Covers 5,225 of 5,228 genes; the rest have no gene entry in OMIM and get no
link.

A gene in both pseudoautosomal regions has two entries (SHOX: 312865 on X,
400020 on Y). The first, the X copy, is the one OMIM describes in full.

Idempotent: adds the column if missing and overwrites it on every run.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def read_gene_entries(mim2gene: Path) -> tuple[dict[str, str], dict[str, str], str]:
    """Gene MIM numbers by upper-cased symbol and by NCBI Gene id, plus the
    file's generation date."""
    by_symbol: dict[str, str] = {}
    by_ncbi: dict[str, str] = {}
    generated = ""
    with mim2gene.open(encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("# Generated:"):
                generated = line.split(":", 1)[1].strip()
            if line.startswith("#"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 4 or fields[1] != "gene":
                continue
            if fields[3]:
                by_symbol.setdefault(fields[3].upper(), fields[0])
            if fields[2]:
                by_ncbi.setdefault(fields[2], fields[0])
    return by_symbol, by_ncbi, generated


def run(db_path: Path, mim2gene: Path) -> tuple[int, int]:
    by_symbol, by_ncbi, generated = read_gene_entries(mim2gene)
    conn = sqlite3.connect(db_path)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(genes)")}
    if "omim_gen" not in columns:
        conn.execute("ALTER TABLE genes ADD COLUMN omim_gen TEXT")

    found = total = 0
    for simbolo, ncbi_id in conn.execute("SELECT simbolo, ncbi_id FROM genes").fetchall():
        total += 1
        mim = by_symbol.get(simbolo.upper()) or by_ncbi.get(ncbi_id or "")
        found += bool(mim)
        conn.execute("UPDATE genes SET omim_gen = ? WHERE simbolo = ?", (mim, simbolo))

    conn.execute(
        "INSERT OR REPLACE INTO build_info (clave, valor) VALUES ('gene_mim', ?)",
        (f"{found}/{total} genes, mim2gene {generated or 'undated'}",),
    )
    conn.commit()
    conn.close()
    return found, total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path)
    parser.add_argument("--mim2gene", type=Path, required=True)
    args = parser.parse_args(argv)

    found, total = run(args.db, args.mim2gene)
    print(f"{found}/{total} genes have an OMIM gene entry")
    return 0


if __name__ == "__main__":
    sys.exit(main())
