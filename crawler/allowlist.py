"""
crawler/allowlist.py

Domain allowlist and per-domain rate limits for the DeepQuest crawler.

Domains in ALLOWLIST are exempt from the noisy-pattern path filter in
is_valid_url() and from the domain blacklist, allowing deep crawling of
historical archive sites.

RATE_LIMITS maps domain → minimum seconds between requests. Any domain
not present in RATE_LIMITS falls back to the DEFAULT_RATE_LIMIT of 2.0 s.
"""

# ---------------------------------------------------------------------------
# Default rate limit (seconds between requests) for allowlisted domains
# ---------------------------------------------------------------------------
DEFAULT_RATE_LIMIT: float = 2.0

# ---------------------------------------------------------------------------
# ALLOWLIST — domains exempt from noisy-pattern path filtering
# ---------------------------------------------------------------------------
ALLOWLIST: set[str] = {
    # Internet Archive
    "archive.org",
    "web.archive.org",
    "archive-it.org",
    # Chronicling America (Library of Congress newspaper archive)
    "chroniclingamerica.loc.gov",
    # HathiTrust Digital Library
    "hathitrust.org",
    "catalog.hathitrust.org",
    "babel.hathitrust.org",
    "www.hathitrust.org",
    # SEC EDGAR
    "sec.gov",
    "efts.sec.gov",
    "www.sec.gov",
    # US National Archives
    "archives.gov",
    "catalog.archives.gov",
    "www.archives.gov",
    # Library of Congress
    "loc.gov",
    "memory.loc.gov",
    "www.loc.gov",
    "digital.library.loc.gov",
    # UK National Archives
    "nationalarchives.gov.uk",
    "discovery.nationalarchives.gov.uk",
    "www.nationalarchives.gov.uk",
    # Europeana
    "europeana.eu",
    "www.europeana.eu",
    # Project Gutenberg
    "gutenberg.org",
    "www.gutenberg.org",
    # Wikipedia (retained from current seeds)
    "en.wikipedia.org",
    # Encyclopaedia Britannica
    "britannica.com",
    "www.britannica.com",
    # JSTOR
    "jstor.org",
    "www.jstor.org",
    # Additional historical / academic archives
    "dp.la",                          # Digital Public Library of America
    "www.dp.la",
    "docsouth.unc.edu",               # Documenting the American South
    "avalon.law.yale.edu",            # Avalon Project (Yale Law)
    "history.state.gov",              # US Dept of State historical docs
    "millercenter.org",               # Miller Center presidential speeches
    "www.millercenter.org",
    "fold3.com",                      # Military records archive
    "www.fold3.com",
    "newspapers.com",                 # Historical newspaper archive
    "www.newspapers.com",
    "familysearch.org",               # Genealogical / historical records
    "www.familysearch.org",
    "biodiversitylibrary.org",        # Biodiversity Heritage Library
    "www.biodiversitylibrary.org",
    "openlibrary.org",                # Open Library (Internet Archive)
    "www.openlibrary.org",
    "gallica.bnf.fr",                 # Bibliothèque nationale de France
    "trove.nla.gov.au",               # National Library of Australia
    "paperspast.natlib.govt.nz",      # Papers Past (New Zealand)
    "eudml.org",                      # European Digital Mathematics Library
}

# ---------------------------------------------------------------------------
# RATE_LIMITS — per-domain minimum seconds between requests
# Domains not listed here fall back to DEFAULT_RATE_LIMIT (2.0 s).
# ---------------------------------------------------------------------------
RATE_LIMITS: dict[str, float] = {
    # Internet Archive — large infrastructure but polite crawling expected
    "archive.org": 3.0,
    "web.archive.org": 3.0,
    "archive-it.org": 3.0,
    # Chronicling America — government server, be conservative
    "chroniclingamerica.loc.gov": 3.0,
    # HathiTrust — rate-limited API, use longer delay
    "hathitrust.org": 3.0,
    "catalog.hathitrust.org": 3.0,
    "babel.hathitrust.org": 3.0,
    "www.hathitrust.org": 3.0,
    # SEC EDGAR — has explicit rate-limit guidance (10 req/s max, be polite)
    "sec.gov": 2.0,
    "efts.sec.gov": 2.0,
    "www.sec.gov": 2.0,
    # US National Archives
    "archives.gov": 2.0,
    "catalog.archives.gov": 2.0,
    "www.archives.gov": 2.0,
    # Library of Congress
    "loc.gov": 2.0,
    "memory.loc.gov": 2.0,
    "www.loc.gov": 2.0,
    # UK National Archives
    "nationalarchives.gov.uk": 2.0,
    "discovery.nationalarchives.gov.uk": 2.0,
    "www.nationalarchives.gov.uk": 2.0,
    # Europeana
    "europeana.eu": 2.0,
    "www.europeana.eu": 2.0,
    # Project Gutenberg — static files, lighter load
    "gutenberg.org": 1.0,
    "www.gutenberg.org": 1.0,
    # Wikipedia — well-resourced, but respect their crawl policy
    "en.wikipedia.org": 1.0,
    # Britannica
    "britannica.com": 2.0,
    "www.britannica.com": 2.0,
    # JSTOR — strict rate limiting
    "jstor.org": 4.0,
    "www.jstor.org": 4.0,
    # Gallica (BnF) — French national library, be conservative
    "gallica.bnf.fr": 3.0,
    # Trove (NLA)
    "trove.nla.gov.au": 2.0,
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def is_allowlisted(domain: str) -> bool:
    """Return True if *domain* is in the ALLOWLIST.

    The check is case-insensitive and strips a leading 'www.' prefix so that
    callers do not need to normalise the domain before calling this function.

    Args:
        domain: A registered domain string, e.g. ``"archive.org"`` or
                ``"www.archive.org"``.

    Returns:
        ``True`` if the domain (or its www-stripped variant) is in
        :data:`ALLOWLIST`, ``False`` otherwise.
    """
    domain = domain.lower().strip()
    if domain in ALLOWLIST:
        return True
    # Also check without a leading 'www.' so callers don't have to normalise
    if domain.startswith("www.") and domain[4:] in ALLOWLIST:
        return True
    return False


def get_rate_limit(domain: str) -> float:
    """Return the minimum seconds between requests for *domain*.

    Looks up *domain* in :data:`RATE_LIMITS`.  If not found, falls back to
    :data:`DEFAULT_RATE_LIMIT` (2.0 seconds).

    Args:
        domain: A registered domain string, e.g. ``"archive.org"``.

    Returns:
        A float representing the minimum number of seconds to wait between
        consecutive requests to this domain.
    """
    domain = domain.lower().strip()
    if domain in RATE_LIMITS:
        return RATE_LIMITS[domain]
    # Try without leading 'www.'
    if domain.startswith("www.") and domain[4:] in RATE_LIMITS:
        return RATE_LIMITS[domain[4:]]
    return DEFAULT_RATE_LIMIT
