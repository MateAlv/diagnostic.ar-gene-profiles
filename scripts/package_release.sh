#!/usr/bin/env bash
# Packs a built genes.db, and the inputs it was built from, into the two
# tarballs a GitHub release carries, and prints their sha256.
#
#   scripts/package_release.sh dist/
#
# genes-db.tar.gz          the database the app serves
# genes-build-inputs.tar.gz  the fetched sources, the LLM translations and
#                          summaries, and the previous database --reuse seeds
#                          from. Rebuilding without them means paying for the
#                          translations again, and they would not come out
#                          the same.
#
# The database is verified first; the archives are reproducible (fixed order,
# owner and mtime, gzip without a timestamp).
set -euo pipefail

out=${1:?usage: package_release.sh <out-dir>}
mkdir -p "$out"

python3 scripts/verify_db.py genes.db

pack() {
    local name=$1; shift
    tar --sort=name --mtime='2026-09-24 00:00Z' --owner=0 --group=0 --numeric-owner \
        -cf - "$@" | gzip -n -9 > "$out/$name"
}

pack genes-db.tar.gz genes.db
pack genes-build-inputs.tar.gz genes.fetch.json genes.translations.json genes.summaries.json genes_v4.db

(cd "$out" && sha256sum genes-db.tar.gz genes-build-inputs.tar.gz | tee SHA256SUMS)
