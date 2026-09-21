"""Translate English source text to Spanish.

The original build used an undocumented Google endpoint with no key and no
contract. It is kept as the default because it is what produced the current
data, but it is isolated here so it can be swapped without touching the build.
"""

from __future__ import annotations

import html
import logging
import re
import time

import requests

log = logging.getLogger(__name__)

GOOGLE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
MAX_BLOCK_CHARS = 300


class Translator:
    """Sentence-aware English→Spanish translation with a persistent cache."""

    def __init__(self, session: requests.Session | None = None,
                 cache: dict[str, str] | None = None, throttle: float = 0.1):
        self.session = session or requests.Session()
        self.cache = cache if cache is not None else {}
        self.throttle = throttle

    def _translate_block(self, text: str, retries: int = 3) -> str:
        text = text.strip()
        if not text:
            return ""
        if text in self.cache:
            return self.cache[text]

        delay = 1.0
        for attempt in range(retries):
            try:
                response = self.session.get(
                    GOOGLE_ENDPOINT,
                    params={"client": "gtx", "sl": "en", "tl": "es", "dt": "t", "q": text},
                    timeout=20,
                )
                if response.status_code == 200:
                    parts = response.json()[0]
                    out = "".join(p[0] for p in parts if p and p[0])
                    out = html.unescape(out)
                    self.cache[text] = out
                    return out
            except Exception as exc:  # noqa: BLE001 - retried, then falls through
                log.debug("translate attempt %d failed: %s", attempt + 1, exc)
            time.sleep(delay)
            delay *= 2

        log.warning("translation failed, keeping source text (%d chars)", len(text))
        return text

    def translate(self, text: str) -> str:
        """Translate a full text, splitting long input on sentence boundaries."""
        text = clean_source_text(text)
        if not text:
            return ""
        if len(text) < 200:
            return self._translate_block(text)

        blocks: list[str] = []
        current = ""
        for sentence in re.split(r"(?<=\.)\s+", text):
            if len(current) + len(sentence) < MAX_BLOCK_CHARS:
                current = f"{current} {sentence}".strip()
            else:
                if current:
                    blocks.append(current)
                current = sentence
        if current:
            blocks.append(current)

        out = []
        for block in blocks:
            out.append(self._translate_block(block))
            time.sleep(self.throttle)
        return " ".join(out).strip()


def clean_source_text(text: str) -> str:
    """Strip the citation noise UniProt and NCBI embed in prose."""
    if not text:
        return ""
    text = text.replace("\n", " ")
    text = re.sub(r"\s*\(PubMed:[^)]+\)", "", text)
    text = re.sub(r"\s*\(By similarity\)", "", text)
    text = re.sub(r"\s*\(Ref\.[^)]+\)", "", text)
    text = re.sub(r"\s*\{ECO:[^}]*\}", "", text)
    text = text.replace('"', "")
    text = re.sub(r"\s+\.", ".", text)
    return re.sub(r"\s+", " ", text).strip()
