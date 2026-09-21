"""Draw a review sample and print it side by side for manual checking.

    python scripts/review_sample.py --db genes.db --pass tail
    python scripts/review_sample.py --db genes.db --pass random --seed 42
    python scripts/review_sample.py --db genes.db --pass top --out pass3.tsv

See docs/PLAIN_LANGUAGE.md for the protocol these passes implement.
"""

from __future__ import annotations

import argparse
import random
import sqlite3
import sys
import textwrap
from pathlib import Path

PASSES = {
    "tail": "longest and shortest technical summaries — where errors concentrate",
    "random": "uniform random sample with a fixed seed",
    "top": "genes with the most expected symptoms — most likely to be shown",
    "empty": "genes where no rewrite was written — confirm the source was too thin",
}


def fetch(conn: sqlite3.Connection, which: str, size: int, seed: int) -> list[sqlite3.Row]:
    written = "resumen_llano_es IS NOT NULL AND resumen_llano_es != ''"

    if which == "tail":
        half = size // 2
        longest = conn.execute(
            f"SELECT * FROM genes WHERE {written} ORDER BY LENGTH(tecnico_resumen_es) DESC LIMIT ?",
            (half,)).fetchall()
        shortest = conn.execute(
            f"""SELECT * FROM genes WHERE {written} AND LENGTH(tecnico_resumen_es) > 0
                ORDER BY LENGTH(tecnico_resumen_es) ASC LIMIT ?""", (half,)).fetchall()
        return longest + shortest

    if which == "random":
        rows = conn.execute(f"SELECT * FROM genes WHERE {written} ORDER BY simbolo").fetchall()
        random.Random(seed).shuffle(rows)
        return rows[:size]

    if which == "top":
        return conn.execute(
            f"""SELECT g.* FROM genes g
                JOIN (SELECT omim, COUNT(*) n FROM sintomas_esperados GROUP BY omim) s
                  ON g.codigo_omim LIKE '%' || s.omim || '%'
                WHERE {written}
                GROUP BY g.simbolo ORDER BY SUM(s.n) DESC LIMIT ?""", (size,)).fetchall()

    return conn.execute(
        """SELECT * FROM genes
           WHERE (resumen_llano_es IS NULL OR resumen_llano_es = '')
             AND tecnico_resumen_es IS NOT NULL AND tecnico_resumen_es != ''
           ORDER BY LENGTH(tecnico_resumen_es) DESC LIMIT ?""", (size,)).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--pass", dest="which", choices=sorted(PASSES), default="random")
    parser.add_argument("--size", type=int)
    parser.add_argument("--seed", type=int, default=42,
                        help="fixed so the same sample can be redrawn after a change")
    parser.add_argument("--out", type=Path, help="also write a TSV to record verdicts in")
    args = parser.parse_args(argv)

    defaults = {"tail": 40, "random": 60, "top": 30, "empty": 20}
    size = args.size or defaults[args.which]

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = fetch(conn, args.which, size, args.seed)

    print(f"# pass: {args.which} — {PASSES[args.which]}")
    print(f"# {len(rows)} genes, seed {args.seed}, db {args.db}")
    print("# verdict: OK / 1 invented / 2 meaning / 3 advice / 4 jargon\n")

    for index, row in enumerate(rows, start=1):
        technical = (row["tecnico_resumen_es"] or "").strip()
        plain = (row["resumen_llano_es"] or "").strip()
        print("=" * 78)
        print(f"[{index}/{len(rows)}] {row['simbolo']}  —  {row['nombre_completo'] or ''}")
        print("-" * 78)
        print("TÉCNICO:")
        print(textwrap.fill(technical, 76, initial_indent="  ", subsequent_indent="  ") or "  (vacío)")
        print("\nLLANO:")
        print(textwrap.fill(plain, 76, initial_indent="  ", subsequent_indent="  ") or "  (sin reescritura)")
        print(f"\nverdict: ____   ({len(plain)} caracteres)\n")

    if args.out:
        with args.out.open("w", encoding="utf-8") as handle:
            handle.write("simbolo\tverdict\tfailure_mode\tnote\n")
            for row in rows:
                handle.write(f"{row['simbolo']}\t\t\t\n")
        print(f"\nRecord verdicts in {args.out}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
