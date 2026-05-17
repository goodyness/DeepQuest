import asyncio
import httpx
import asyncpg
import redis.asyncio as redis
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import logging
import os
import time
import pdfplumber
import tempfile
import hashlib

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from seeds import SEED_URLS
from allowlist import is_allowlisted, get_rate_limit

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepQuest_Crawler")

# Database & Redis configurations (from docker-compose)
POSTGRES_DSN = "postgresql://deepquest:deepquestpassword@localhost:5432/deepquestdb"
REDIS_URL = "redis://localhost:6379/0"

# Redis Keys
FRONTIER_KEY = "deepquest:frontier"
VISITED_KEY = "deepquest:visited"

# Crawler Settings
CONCURRENCY = 10
TIMEOUT = 15

# ---------------------------------------------------------------------------
# Module-level state for rate limiting and robots.txt caching
# ---------------------------------------------------------------------------

# Maps domain -> timestamp of last request (float, seconds since epoch)
_domain_last_request: dict[str, float] = {}

# Maps domain -> Crawl-delay value parsed from robots.txt (float seconds).
# A value of None means robots.txt was fetched but no Crawl-delay was found.
# A missing key means robots.txt has not been fetched yet for that domain.
_robots_cache: dict[str, float | None] = {}

# Maps domain -> current backoff wait seconds for 429 handling
_domain_backoff: dict[str, float] = {}

# Maps domain -> consecutive 429 failure count
_domain_429_count: dict[str, int] = {}


async def get_db_pool():
    return await asyncpg.create_pool(dsn=POSTGRES_DSN)


def is_valid_url(url: str) -> bool:
    parsed = urlparse(url)
    if not (bool(parsed.netloc) and bool(parsed.scheme) and parsed.scheme in ['http', 'https']):
        return False

    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    # Skip binary/non-text file types and plain text files trafilatura can't parse
    binary_extensions = (
        '.zip', '.gz', '.tar', '.bz2', '.7z', '.rar',
        '.torrent', '.iso', '.exe', '.dmg', '.pkg',
        '.jpg', '.jpeg', '.png', '.gif', '.svg', '.webp', '.ico',
        '.mp3', '.mp4', '.avi', '.mov', '.wav', '.flac',
        '.djvu', '.epub', '.mobi', '.kf8', '.epub3',
        '.jp2', '.tiff', '.tif',
        '.xml', '.json', '.csv', '.tsv',
        '.marc', '.mrc', '.pdf',
        '.txt', '.utf-8', '.txt.utf-8',
    )
    if any(path.endswith(ext) for ext in binary_extensions):
        return False

    # Skip file-delivery and download paths on any domain
    delivery_patterns = [
        '/ebooks/send/', '/ebooks/download/', '/download/',
        '/compress/', '/serve/', '/stream/',
        '.epub.', '.kf8.', '.epub3.', '.mobi.',
        '.txt.utf', 'utf-8',
    ]
    if any(pat in path for pat in delivery_patterns):
        return False

    # Block useless domains entirely
    useless_domains = [
        'change.org', 'forms.gle', 'docs.google.com', 'forms.google.com',
        'google.com', 'google.co', 'accounts.google.com',
        'twitter.com', 'x.com', 'facebook.com', 'instagram.com',
        'linkedin.com', 'reddit.com', 'pinterest.com', 'tiktok.com',
        'youtube.com', 'amazon.com', 'ebay.com', 'etsy.com',
        'apple.com', 'microsoft.com', 'github.com',
        'ycombinator.com', 'techcrunch.com', 'shopify.com',
        'outlook.com', 'outlook.live.com', 'outlook.office.com',
        'office.com', 'live.com', 'cvent.com', 'eventbrite.com',
        'zoom.us', 'calendly.com', 'meetup.com',
    ]
    for bad in useless_domains:
        if domain == bad or domain.endswith('.' + bad):
            return False

    # Block feed/RSS/API paths on any domain
    feed_patterns = ['/feed', '/rss', '/atom', '/api/', '/owa', '/comments/feed']
    if any(path.rstrip('/') == p or path.startswith(p + '/') for p in feed_patterns):
        return False

    # Per-domain path filters for allowlisted domains that have noisy sections
    domain_path_filters = {
        "openlibrary.org": ["/subjects", "/search", "/lists", "/collections",
                            "/account", "/help", "/stats", "/recentchanges"],
        "archive.org": ["/search", "/account", "/upload", "/donate",
                        "/about", "/contact", "/projects"],
        "hathitrust.org": ["/search", "/account", "/help"],
        "babel.hathitrust.org": ["/search", "/account"],
        "gutenberg.org": ["/browse", "/ebooks/search", "/catalog/world",
                          "/ebooks/send", "/ebooks/download", "/cache/"],
        "www.gutenberg.org": ["/browse", "/ebooks/search",
                              "/ebooks/send", "/ebooks/download", "/cache/"],
    }
    if domain in domain_path_filters:
        if any(path.startswith(p) for p in domain_path_filters[domain]):
            return False
        return True  # allowlisted domain, passed path filter

    # Skip noisy boilerplate pages for non-allowlisted domains
    if not is_allowlisted(domain):
        noisy_patterns = [
            "/faq", "/about", "/contact", "/privacy", "/terms",
            "/store", "/shop", "/cart", "/login", "/search",
        ]
        if any(pattern in path for pattern in noisy_patterns):
            return False

        blacklist = [
            "github.com", "ycombinator.com", "techcrunch.com",
            "twitter.com", "facebook.com", "youtube.com", "instagram.com",
            "reddit.com", "linkedin.com", "pinterest.com", "tiktok.com",
            "amazon.com", "ebay.com", "etsy.com", "shopify.com",
            "apple.com", "google.com", "microsoft.com",
        ]
        for b in blacklist:
            if domain == b or domain.endswith("." + b):
                return False

    return True


async def _fetch_robots_crawl_delay(domain: str, client: httpx.AsyncClient) -> float | None:
    """Fetch robots.txt for *domain* and return the Crawl-delay value (seconds),
    or None if no Crawl-delay directive is present or the fetch fails."""
    robots_url = f"https://{domain}/robots.txt"
    try:
        resp = await client.get(robots_url, timeout=10, follow_redirects=True)
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.lower().startswith("crawl-delay"):
                    parts = line.split(":", 1)
                    if len(parts) == 2:
                        try:
                            return float(parts[1].strip())
                        except ValueError:
                            pass
    except Exception as e:
        logger.debug(f"Could not fetch robots.txt for {domain}: {e}")
    return None


async def _enforce_rate_limit(domain: str, client: httpx.AsyncClient) -> None:
    """Sleep if necessary to honour the per-domain rate limit.

    On first visit to a domain, fetches robots.txt and caches the Crawl-delay.
    The effective rate limit is max(get_rate_limit(domain), crawl_delay).
    """
    # Fetch robots.txt on first visit
    if domain not in _robots_cache:
        crawl_delay = await _fetch_robots_crawl_delay(domain, client)
        _robots_cache[domain] = crawl_delay

    configured_limit = get_rate_limit(domain)
    robots_delay = _robots_cache.get(domain) or 0.0
    effective_limit = max(configured_limit, robots_delay)

    last = _domain_last_request.get(domain, 0.0)
    now = time.monotonic()
    wait = last + effective_limit - now
    if wait > 0:
        await asyncio.sleep(wait)

    _domain_last_request[domain] = time.monotonic()


async def fetch_and_process(url: str, redis_conn, db_pool, client: httpx.AsyncClient) -> None:
    try:
        # Mark as visited
        await redis_conn.sadd(VISITED_KEY, url)

        parsed_url = urlparse(url)
        domain = parsed_url.netloc.lower()

        # Enforce rate limit for allowlisted domains
        if is_allowlisted(domain):
            await _enforce_rate_limit(domain, client)

        logger.info(f"Fetching: {url}")

        # ------------------------------------------------------------------ #
        # Fetch with exponential backoff on HTTP 429                          #
        # ------------------------------------------------------------------ #
        max_retries = 3
        response = None
        for attempt in range(max_retries + 1):
            try:
                response = await client.get(url, timeout=TIMEOUT, follow_redirects=True)
            except Exception as fetch_exc:
                logger.error(f"Network error fetching {url}: {fetch_exc}")
                return

            if response.status_code != 429:
                # Reset backoff counters on success
                _domain_backoff.pop(domain, None)
                _domain_429_count.pop(domain, None)
                break

            # 429 handling
            _domain_429_count[domain] = _domain_429_count.get(domain, 0) + 1
            if attempt >= max_retries:
                logger.warning(
                    f"Discarding {url} after {max_retries} retries due to repeated HTTP 429"
                )
                return

            # Compute backoff: 30s → 60s → 120s
            backoff = 30.0 * (2 ** attempt)
            _domain_backoff[domain] = backoff
            logger.warning(
                f"HTTP 429 for {url} (attempt {attempt + 1}/{max_retries}). "
                f"Backing off for {backoff:.0f}s."
            )
            await asyncio.sleep(backoff)

        if response is None:
            return

        status_code = response.status_code
        final_url = str(response.url)

        # ------------------------------------------------------------------ #
        # Determine content type and extract text                             #
        # ------------------------------------------------------------------ #
        if url.lower().endswith('.pdf'):
            content_type = 'pdf'
            html = ""
            if status_code == 200:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                    tmp.write(response.content)
                    tmp_path = tmp.name
                try:
                    with pdfplumber.open(tmp_path) as pdf:
                        for page in pdf.pages:
                            text = page.extract_text()
                            if text:
                                html += text + "\n"
                except Exception as pdf_e:
                    logger.error(f"Error parsing PDF {url}: {pdf_e}")
                finally:
                    os.unlink(tmp_path)
        else:
            content_type = 'html'
            html = response.text if status_code == 200 else ""

        if status_code != 200:
            logger.info(f"Non-200 status {status_code} for {url} — skipping.")
            return

        # Quality Filter: Ignore pages with less than 300 words
        word_count = len(html.split())
        if word_count < 300:
            logger.info(f"Skipped (thin content): {url} ({word_count} words)")
            return

        # Content Hashing to prevent duplicate parsing
        content_hash = hashlib.sha256(html.encode('utf-8', errors='ignore')).hexdigest()

        # Add final (resolved) URL to visited set to prevent redirect duplicates
        if final_url != url:
            await redis_conn.sadd(VISITED_KEY, final_url)

        # Save to database — use url as the single conflict target
        try:
            page_domain = urlparse(final_url).netloc
            async with db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO pages (url, final_url, domain, raw_html, content_hash, content_type)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (url) DO NOTHING
                    """,
                    url, final_url, page_domain, html, content_hash, content_type,
                )
        except asyncpg.UniqueViolationError:
            pass  # Already stored — not an error
        except Exception as e:
            logger.error(f"DB Error for {url}: {e}")

        # Extract links if it's HTML
        new_links_count = 0
        if content_type == 'html':
            soup = BeautifulSoup(html, 'html.parser')
            for link in soup.find_all('a', href=True):
                absolute_url = urljoin(url, link['href'])
                # Canonicalize URL (strip query params and fragments)
                absolute_url = absolute_url.split("?")[0].split("#")[0]
                if absolute_url.endswith("/"):
                    absolute_url = absolute_url[:-1]

                if is_valid_url(absolute_url):
                    is_visited = await redis_conn.sismember(VISITED_KEY, absolute_url)
                    if not is_visited:
                        await redis_conn.rpush(FRONTIER_KEY, absolute_url)
                        new_links_count += 1

        logger.info(
            f"Processed: {url} | status={status_code} | words={word_count} "
            f"| hash={content_hash[:12]}... | new_links={new_links_count} "
            f"| content_type={content_type}"
        )

    except Exception as e:
        logger.error(f"Error fetching {url}: {str(e)}")


async def worker(redis_conn, db_pool, client: httpx.AsyncClient) -> None:
    while True:
        try:
            result = await redis_conn.blpop(FRONTIER_KEY, timeout=5)
            if result:
                _, url = result
                url = url.decode('utf-8')
                await fetch_and_process(url, redis_conn, db_pool, client)
            else:
                logger.info("Frontier is empty. Waiting for new URLs...")
                await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Worker loop error: {e} — retrying in 10s")
            await asyncio.sleep(10)


async def main() -> None:
    logger.info("Starting DeepQuest Crawler...")

    # Retry connecting to Postgres and Redis on startup (handles brief outages)
    db_pool = None
    while db_pool is None:
        try:
            db_pool = await get_db_pool()
        except Exception as e:
            logger.error(f"Could not connect to PostgreSQL: {e} — retrying in 10s")
            await asyncio.sleep(10)

    redis_conn = redis.from_url(REDIS_URL)

    # Seed the frontier if it's completely empty
    if await redis_conn.scard(VISITED_KEY) == 0 and await redis_conn.llen(FRONTIER_KEY) == 0:
        for url in SEED_URLS:
            await redis_conn.rpush(FRONTIER_KEY, url)
        logger.info(f"Seeded frontier with {len(SEED_URLS)} URLs.")

    # Create HTTP client
    limits = httpx.Limits(max_keepalive_connections=50, max_connections=100)
    async with httpx.AsyncClient(limits=limits, headers={'User-Agent': 'DeepQuestBot/1.0'}) as client:
        tasks = [worker(redis_conn, db_pool, client) for _ in range(CONCURRENCY)]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Crawler stopped.")
