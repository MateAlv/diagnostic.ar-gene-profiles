#!/usr/bin/env bash
# Download the official Spanish HPO labels.
#
# Human-curated by the HPO Internationalisation Effort, covering ~97.6% of HPO
# terms. Using them removes ~20,000 machine-translation calls per build, which
# is what got this build rate-limited, and the quality is better.
set -euo pipefail

DEST="${1:-../diagnosticar-question-agent/data/hp-es.babelon.tsv}"
URL="https://raw.githubusercontent.com/obophenotype/hpo-translations/main/babelon/hp-es.babelon.tsv"

echo "Fetching official Spanish HPO labels..."
curl -fsSL "$URL" -o "$DEST.tmp"

# A truncated download would silently shrink symptom coverage.
lines=$(wc -l < "$DEST.tmp")
if [ "$lines" -lt 10000 ]; then
    echo "Refusing to install a suspiciously short file ($lines lines)" >&2
    rm -f "$DEST.tmp"
    exit 1
fi

mv "$DEST.tmp" "$DEST"
echo "Wrote $DEST ($lines lines)"
