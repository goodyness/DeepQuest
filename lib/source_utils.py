"""
Shared helpers for source URL and domain counting.

DeepQuest questions require N distinct netlocs (e.g. 6), not N URLs on the same site.
"""

from __future__ import annotations

from urllib.parse import urlparse


def normalize_netloc(url: str) -> str:
    """Return lowercase netloc without leading www."""
    try:
        netloc = urlparse(url).netloc.lower().strip()
    except Exception:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def unique_netlocs(urls: list[str]) -> set[str]:
    """Distinct normalized domains from a list of URLs."""
    out: set[str] = set()
    for url in urls or []:
        if not url or not isinstance(url, str):
            continue
        netloc = normalize_netloc(url)
        if netloc:
            out.add(netloc)
    return out


def count_unique_domains(urls: list[str]) -> int:
    return len(unique_netlocs(urls))


def count_unique_domains_from_lists(*domain_lists: list) -> int:
    """Count unique domains stored on Neo4j edges (may be netlocs or full host strings)."""
    seen: set[str] = set()
    for lst in domain_lists:
        for d in lst or []:
            if not d:
                continue
            s = str(d).lower().strip()
            if s.startswith("www."):
                s = s[4:]
            if s:
                seen.add(s)
    return len(seen)


def pick_one_url_per_domain(urls: list[str], max_urls: int | None = None) -> list[str]:
    """
    Return one URL per unique netloc, stable order (first URL wins per domain).
    """
    domain_map: dict[str, str] = {}
    for url in urls or []:
        if not url:
            continue
        netloc = normalize_netloc(url)
        if netloc and netloc not in domain_map:
            domain_map[netloc] = url
    picked = list(domain_map.values())
    if max_urls is not None:
        return picked[:max_urls]
    return picked


def format_domain_summary(urls: list[str], limit: int = 12) -> str:
    """Short debug string: '4 domains: a.org, b.org, ...'."""
    domains = sorted(unique_netlocs(urls))
    n = len(domains)
    if n == 0:
        return "0 domains"
    shown = ", ".join(domains[:limit])
    if n > limit:
        shown += f", ... (+{n - limit} more)"
    return f"{n} domains: {shown}"
