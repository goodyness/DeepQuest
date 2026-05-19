"""
verifier/worker.py — SourceVerifier component

Validates that each cited URL actually contains text supporting the claimed
fact before a question is accepted.

Requirements: 7 (Source Verifier Component), 15 (Operational Observability)
"""

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
import trafilatura


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class URLResult:
    """Result of verifying a single URL."""
    url: str
    status_code: int          # HTTP status code; 0 if connection error
    word_count: int           # Word count of extracted body text
    matched_terms: list       # Key terms that matched in the page text
    verified: bool            # True if word_count >= 100 and len(matched_terms) >= 2
    elapsed_seconds: float    # Wall-clock time for the fetch + check


@dataclass
class VerificationResult:
    """Aggregate result for a full verification run over a set of URLs."""
    verified_urls: list       # URLs that passed verification
    rejected_urls: list       # URLs that failed verification
    timed_out: bool           # True if the global timeout was exceeded
    passed: bool              # True if 6-domain gate is satisfied

    @staticmethod
    def unique_domains(urls: list) -> set:
        """Return the set of registered netloc domains from a list of URLs."""
        domains = set()
        for url in urls:
            try:
                netloc = urlparse(url).netloc
                if netloc:
                    domains.add(netloc)
            except Exception:
                pass
        return domains


# ---------------------------------------------------------------------------
# Standalone helper
# ---------------------------------------------------------------------------

def get_key_terms(chain: dict) -> list:
    """
    Extract key terms from a chain dict for use as verifier search terms.

    Pulls:
    - Entity names: entity_a, entity_b, entity_c
    - Relationship types: action_1, action_2 (humanised — underscores → spaces,
      lowercased)
    - Date strings: date_1, date_2 (any key whose name starts with "date")

    Parameters
    ----------
    chain : dict
        A chain dict as produced by the Generator, e.g.::

            {
                "entity_a": "Marie Curie",
                "entity_b": "Radium Institute",
                "entity_c": "Nobel Prize",
                "action_1": "FOUNDED",
                "action_2": "WON",
                "date_1": "1898",
                "date_2": "1911",
            }

    Returns
    -------
    list[str]
        Deduplicated list of non-empty key term strings.
    """
    terms = []

    # Entity names
    for key in ("entity_a", "entity_b", "entity_c"):
        value = chain.get(key)
        if value and isinstance(value, str) and value.strip():
            terms.append(value.strip())

    # Relationship types — humanise by replacing underscores with spaces and
    # lowercasing so "WAS_CEO_OF" becomes "was ceo of"
    for key in ("action_1", "action_2"):
        value = chain.get(key)
        if value and isinstance(value, str) and value.strip():
            humanised = value.strip().replace("_", " ").lower()
            terms.append(humanised)

    # Date strings — collect any key whose name starts with "date"
    for key, value in chain.items():
        if key.startswith("date") and value and isinstance(value, str) and value.strip():
            terms.append(value.strip())

    # Deduplicate while preserving order
    seen = set()
    unique_terms = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique_terms.append(t)

    return unique_terms


# ---------------------------------------------------------------------------
# SourceVerifier
# ---------------------------------------------------------------------------

_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verification.log")

# Per-request HTTP timeout (seconds)
_REQUEST_TIMEOUT = 15.0


class SourceVerifier:
    """
    Verifies that a list of source URLs contain text supporting a claimed fact.

    Usage::

        verifier = SourceVerifier(timeout_seconds=120)
        result = verifier.verify(source_urls, key_terms)
        if result.passed:
            # proceed with question
    """

    def __init__(self, timeout_seconds: int = 120, cache_enabled: bool = True, cache_dir: str = ".verifier_cache"):
        self.timeout_seconds = timeout_seconds
        self.cache_enabled = cache_enabled
        self.cache_dir = cache_dir
        if self.cache_enabled:
            os.makedirs(self.cache_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def verify(self, source_urls: list, key_terms: list) -> VerificationResult:
        """
        Fetch and check all *source_urls* concurrently, enforcing a global
        *timeout_seconds* deadline.

        Parameters
        ----------
        source_urls : list[str]
            Candidate URLs to verify.
        key_terms : list[str]
            Terms derived from the answer chain (entity names, relationship
            verbs, date strings).

        Returns
        -------
        VerificationResult
            Contains verified_urls, rejected_urls, timed_out, and passed.
        """
        verified_urls: list = []
        rejected_urls: list = []
        timed_out = False

        with ThreadPoolExecutor() as executor:
            # Submit all URL checks
            future_to_url = {
                executor.submit(self._fetch_and_check, url, key_terms): url
                for url in source_urls
            }

            deadline = time.monotonic() + self.timeout_seconds

            try:
                for future in as_completed(future_to_url, timeout=self.timeout_seconds):
                    try:
                        result: URLResult = future.result()
                    except Exception as exc:
                        # Individual fetch raised an unexpected exception; treat
                        # as unverified.
                        url = future_to_url[future]
                        result = URLResult(
                            url=url,
                            status_code=0,
                            word_count=0,
                            matched_terms=[],
                            verified=False,
                            elapsed_seconds=0.0,
                        )

                    self._log(result)

                    if result.verified:
                        verified_urls.append(result.url)
                    else:
                        rejected_urls.append(result.url)

                    # Respect the global deadline even if as_completed hasn't
                    # timed out yet (defensive check).
                    if time.monotonic() > deadline:
                        timed_out = True
                        break

            except FuturesTimeoutError:
                timed_out = True
                # Collect results for futures that already completed
                for future, url in future_to_url.items():
                    if future.done() and not future.cancelled():
                        try:
                            result = future.result()
                            self._log(result)
                            if result.verified:
                                verified_urls.append(result.url)
                            else:
                                rejected_urls.append(result.url)
                        except Exception:
                            pass

        # 6-domain gate
        unique_domains = VerificationResult.unique_domains(verified_urls)
        passed = (
            len(verified_urls) >= 6
            and len(unique_domains) >= 6
        )

        return VerificationResult(
            verified_urls=verified_urls,
            rejected_urls=rejected_urls,
            timed_out=timed_out,
            passed=passed,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _save_to_cache(self, cache_path: str, url: str, status_code: int, body_text: str) -> None:
        """Helper to write fetched pages and status codes to filesystem cache."""
        if self.cache_enabled:
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "url": url,
                        "status_code": status_code,
                        "body_text": body_text,
                        "cached_at": time.time()
                    }, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def _fetch_and_check(self, url: str, key_terms: list) -> URLResult:
        """
        Fetch *url*, extract body text via trafilatura, and check key terms.

        Parameters
        ----------
        url : str
            The URL to fetch.
        key_terms : list[str]
            Terms to search for in the extracted body text.

        Returns
        -------
        URLResult
        """
        start = time.monotonic()

        # 1. Cache Lookup
        url_hash = hashlib.sha256(url.encode('utf-8', errors='ignore')).hexdigest()
        cache_path = os.path.join(self.cache_dir, f"{url_hash}.json")

        body_text = None
        status_code = 0
        if self.cache_enabled:
            try:
                if os.path.exists(cache_path):
                    with open(cache_path, "r", encoding="utf-8") as f:
                        cached_data = json.load(f)
                    body_text = cached_data.get("body_text", "")
                    status_code = cached_data.get("status_code", 200)
            except Exception:
                pass

        if body_text is not None:
            word_count = len(body_text.split())
            if word_count < 100 or status_code != 200:
                elapsed = time.monotonic() - start
                return URLResult(
                    url=url,
                    status_code=status_code,
                    word_count=word_count,
                    matched_terms=[],
                    verified=False,
                    elapsed_seconds=round(elapsed, 4),
                )
            matched_terms = _match_key_terms(body_text, key_terms)
            verified = len(matched_terms) >= 2
            elapsed = time.monotonic() - start
            return URLResult(
                url=url,
                status_code=status_code,
                word_count=word_count,
                matched_terms=matched_terms,
                verified=verified,
                elapsed_seconds=round(elapsed, 4),
            )

        # 2. Network Fetch (Cache Miss)
        status_code = 0
        word_count = 0
        matched_terms = []
        verified = False

        try:
            headers = {
                "User-Agent": "DeepQuestBot/1.0"
            }
            with httpx.Client(
                timeout=_REQUEST_TIMEOUT,
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = client.get(url)
                status_code = response.status_code

                if status_code != 200:
                    self._save_to_cache(cache_path, url, status_code, "")
                    elapsed = time.monotonic() - start
                    return URLResult(
                        url=url,
                        status_code=status_code,
                        word_count=0,
                        matched_terms=[],
                        verified=False,
                        elapsed_seconds=round(elapsed, 4),
                    )

                raw_html = response.text

        except Exception:
            self._save_to_cache(cache_path, url, 0, "")
            elapsed = time.monotonic() - start
            return URLResult(
                url=url,
                status_code=0,
                word_count=0,
                matched_terms=[],
                verified=False,
                elapsed_seconds=round(elapsed, 4),
            )

        # Extract clean body text using the same trafilatura pipeline as the
        # Extractor (include_comments=False, include_tables=False).
        body_text = trafilatura.extract(
            raw_html,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        ) or ""

        word_count = len(body_text.split())

        # 3. Cache Save
        self._save_to_cache(cache_path, url, status_code, body_text)

        if word_count < 100:
            elapsed = time.monotonic() - start
            return URLResult(
                url=url,
                status_code=status_code,
                word_count=word_count,
                matched_terms=[],
                verified=False,
                elapsed_seconds=round(elapsed, 4),
            )

        # Key-term matching
        matched_terms = _match_key_terms(body_text, key_terms)
        verified = len(matched_terms) >= 2

        elapsed = time.monotonic() - start
        return URLResult(
            url=url,
            status_code=status_code,
            word_count=word_count,
            matched_terms=matched_terms,
            verified=verified,
            elapsed_seconds=round(elapsed, 4),
        )

    def _log(self, result: URLResult) -> None:
        """
        Append a JSON log entry for *result* to ``verifier/verification.log``.

        Creates the file if it does not exist; appends otherwise.
        Each entry is a single JSON object on its own line (NDJSON format).
        """
        entry = {
            "url": result.url,
            "status_code": result.status_code,
            "word_count": result.word_count,
            "matched_terms": result.matched_terms,
            "verified": result.verified,
            "elapsed_seconds": result.elapsed_seconds,
        }
        try:
            with open(_LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError:
            pass  # Non-fatal; logging failure must not abort verification


# ---------------------------------------------------------------------------
# Key-term matching (module-level for easy unit testing)
# ---------------------------------------------------------------------------

def _match_key_terms(page_text: str, key_terms: list) -> list:
    """
    Return the subset of *key_terms* that appear in *page_text*.

    Matching rules (Req 7.4):
    - Case-insensitive comparison.
    - A term matches if it appears as a **substring** of the page text, OR
    - Any **prefix of length ≥ 4** of the term appears in the page text.

    Parameters
    ----------
    page_text : str
        Extracted body text of the page.
    key_terms : list[str]
        Terms to search for.

    Returns
    -------
    list[str]
        Matched terms (preserving original casing from *key_terms*).
    """
    text_lower = page_text.lower()
    matched = []

    for term in key_terms:
        term_lower = term.lower()

        # Exact substring match
        if term_lower in text_lower:
            matched.append(term)
            continue

        # Prefix match: try every prefix of length >= 4 (longest first so we
        # stop as soon as we find a match)
        term_len = len(term_lower)
        found_prefix = False
        for length in range(term_len, 3, -1):  # term_len down to 4 inclusive
            if term_lower[:length] in text_lower:
                found_prefix = True
                break

        if found_prefix:
            matched.append(term)

    return matched
