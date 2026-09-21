"""Translate English source text to Spanish.

The original build used an undocumented Google endpoint with no key and no
contract. It is kept as the default because it is what produced the current
data, but it is isolated here so it can be swapped without touching the build.
"""

from __future__ import annotations

import html
import json
import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


class RateLimiter:
    """Token-bucket limiter shared across threads.

    Google's translate endpoint is undocumented and unauthenticated, so the
    only safe posture is to stay well under whatever it tolerates and back off
    hard when it complains.
    """

    def __init__(self, rate_per_second: float):
        self.min_interval = 1.0 / rate_per_second
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            # Jitter keeps parallel workers from forming a lockstep burst.
            self._next_allowed = now + self.min_interval * random.uniform(0.9, 1.2)


class BulkTranslator:
    """Translate many distinct strings concurrently, deduplicating first.

    The per-gene translator issued one request per text in sequence, which made
    a full build take many hours — most of it spent on the ~20k HPO symptom
    labels, which repeat across diseases and had never been cached.
    """

    def __init__(self, cache: dict[str, str] | None = None, *,
                 workers: int = 2, rate_per_second: float = 2.0,
                 max_failures: int = 40, cache_path: "Path | None" = None):
        self.cache = cache if cache is not None else {}
        self.workers = workers
        self.limiter = RateLimiter(rate_per_second)
        self.max_failures = max_failures
        self.cache_path = cache_path
        self._failures = 0
        self._consecutive_throttles = 0
        self._blocked = False
        self._lock = threading.Lock()
        self._local = threading.local()

    def save(self) -> None:
        """Persist the cache so an interrupted run resumes instead of restarting."""
        if not self.cache_path:
            return
        try:
            self.cache_path.write_text(
                json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write translation cache: %s", exc)

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4)
            session.mount("https://", adapter)
            self._local.session = session
        return session

    def _fetch(self, text: str, retries: int = 4) -> str:
        delay = 2.0
        for _ in range(retries):
            self.limiter.acquire()
            try:
                response = self._session().get(
                    GOOGLE_ENDPOINT,
                    params={"client": "gtx", "sl": "en", "tl": "es", "dt": "t", "q": text},
                    timeout=20,
                )
                if response.status_code == 200:
                    parts = response.json()[0]
                    return html.unescape("".join(p[0] for p in parts if p and p[0]))
                if response.status_code == 200:
                    with self._lock:
                        self._consecutive_throttles = 0
                if response.status_code in (429, 403) or response.status_code >= 500:
                    with self._lock:
                        self._consecutive_throttles += 1
                        # Sustained 429s mean the IP is blocked, not that we are
                        # momentarily too fast. Backing off further just burns
                        # hours; stop and let the caller resume later.
                        if self._consecutive_throttles >= 60 and not self._blocked:
                            self._blocked = True
                            log.error("the translation endpoint is refusing requests "
                                      "(%d consecutive). Stopping; rerun later to resume "
                                      "from the cache.", self._consecutive_throttles)
                    if self._blocked:
                        raise RuntimeError("translation endpoint blocked")
                    log.warning("translate throttled (HTTP %d), backing off %.0fs",
                                response.status_code, delay)
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
            except Exception as exc:  # noqa: BLE001 - retried below
                log.debug("translate error: %s", exc)
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise RuntimeError("translation failed after retries")

    def translate_many(self, texts: list[str]) -> dict[str, str]:
        """Translate every distinct text, returning {source: translation}.

        Sources that cannot be translated map to themselves, so a build always
        produces text rather than a blank field.
        """
        pending = sorted({t.strip() for t in texts if t and t.strip()} - set(self.cache))
        if not pending:
            return self.cache

        log.info("translating %d new strings (%d already cached), %d workers",
                 len(pending), len(self.cache), self.workers)

        def work(text: str) -> tuple[str, str] | None:
            if self._blocked:
                return None
            try:
                return text, self._fetch(text)
            except Exception:  # noqa: BLE001 - counted, then degrades to source text
                with self._lock:
                    self._failures += 1
                    if self._failures == self.max_failures:
                        log.error("%d translation failures — the endpoint is likely "
                                  "blocking us; remaining text stays in English",
                                  self._failures)
                return None if self._blocked else (text, text)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for index, result in enumerate(pool.map(work, pending), start=1):
                if result is None:
                    continue
                source, translated = result
                self.cache[source] = translated
                if index % 200 == 0:
                    self.save()
                    log.info("translated %d/%d", index, len(pending))

        self.save()
        if self._blocked:
            remaining = len(pending) - sum(1 for t in pending if t in self.cache)
            log.error("stopped while blocked: %d strings still untranslated. "
                      "Rerun later — the cache resumes where this left off.", remaining)
        elif self._failures:
            log.warning("%d/%d strings could not be translated", self._failures, len(pending))
        return self.cache

    def get(self, text: str) -> str:
        """Look up an already-translated string, falling back to the source."""
        text = (text or "").strip()
        return self.cache.get(text, text)
