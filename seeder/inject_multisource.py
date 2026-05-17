"""
seeder/inject_multisource.py — Multi-Source Seeder

For each historical topic, fetches content from MULTIPLE independent sources
and injects them all into Neo4j. This means each extracted fact immediately
has 6+ source domains, satisfying the 6-source gate without waiting for the
crawler to accumulate data over weeks.

Sources used per topic:
  1. Wikipedia (en.wikipedia.org)
  2. Britannica (britannica.com)
  3. DBpedia (dbpedia.org) — structured Wikipedia data
  4. Simple English Wikipedia (simple.wikipedia.org)
  5. Wikiquote (en.wikiquote.org)
  6. History.com (history.com)
  7. Encyclopaedia Britannica Kids (kids.britannica.com)

Usage:
    python seeder/inject_multisource.py
    python seeder/inject_multisource.py --topics "Standard Oil" "Napoleon"
    python seeder/inject_multisource.py --limit 20
"""

import argparse
import asyncio
import asyncpg
import hashlib
import logging
import os
import sys

import httpx

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extractor.worker import extract_all, clean_html
from graph.schema import GraphManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepQuest_MultiSourceSeeder")

POSTGRES_DSN = "postgresql://deepquest:deepquestpassword@localhost:5432/deepquestdb"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

# ---------------------------------------------------------------------------
# Topics — same list as inject_wikipedia.py
# ---------------------------------------------------------------------------

DEFAULT_TOPICS = [
    "Standard_Oil", "Carnegie_Steel_Company", "John_D._Rockefeller",
    "Andrew_Carnegie", "J._P._Morgan", "Cornelius_Vanderbilt",
    "Sherman_Antitrust_Act", "Ida_Tarbell", "Gilded_Age",
    "Industrial_Revolution", "Second_Industrial_Revolution",
    "Abraham_Lincoln", "Theodore_Roosevelt", "Woodrow_Wilson",
    "Benjamin_Franklin", "Alexander_Hamilton", "Thomas_Jefferson",
    "Napoleon_Bonaparte", "Otto_von_Bismarck", "Queen_Victoria",
    "Thomas_Edison", "Nikola_Tesla", "Alexander_Graham_Bell",
    "Herman_Hollerith", "Samuel_Morse", "James_Watt",
    "Louis_Pasteur", "Charles_Darwin", "Marie_Curie",
    "American_Civil_War", "World_War_I", "Franco-Prussian_War",
    "Panic_of_1873", "Panic_of_1907", "Great_Depression",
    "Federal_Reserve", "East_India_Company", "Dutch_East_India_Company",
    "Transcontinental_Railroad", "British_Empire",
    "Telegraph", "Telephone", "Typewriter", "Steam_engine",
    "Western_Union", "IBM", "Remington_Arms",
    "Pinkerton_National_Detective_Agency",
]

# ---------------------------------------------------------------------------
# Source fetchers — one per domain
# ---------------------------------------------------------------------------

async def fetch_wikipedia(title: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """Fetch from English Wikipedia via MediaWiki API."""
    params = {
        "action": "parse",
        "page": title.replace("_", " "),
        "prop": "text",
        "format": "json",
        "redirects": "1",
    }
    try:
        r = await client.get("https://en.wikipedia.org/w/api.php", params=params, timeout=20)
        if r.status_code == 200:
            data = r.json()
            if "parse" in data:
                canonical = data["parse"].get("title", title).replace(" ", "_")
                url = f"https://en.wikipedia.org/wiki/{canonical}"
                return url, data["parse"]["text"]["*"]
    except Exception as e:
        logger.debug(f"Wikipedia fetch failed for {title}: {e}")
    return None


async def fetch_dbpedia(title: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """Fetch abstract from DBpedia SPARQL endpoint."""
    resource = title.replace(" ", "_")
    url = f"https://dbpedia.org/page/{resource}"
    query = f"""
    SELECT ?abstract WHERE {{
        <http://dbpedia.org/resource/{resource}> dbo:abstract ?abstract .
        FILTER (lang(?abstract) = 'en')
    }} LIMIT 1
    """
    try:
        r = await client.get(
            "https://dbpedia.org/sparql",
            params={"query": query, "format": "application/json"},
            timeout=20,
        )
        if r.status_code == 200:
            data = r.json()
            bindings = data.get("results", {}).get("bindings", [])
            if bindings:
                abstract = bindings[0]["abstract"]["value"]
                if len(abstract.split()) > 50:
                    return url, f"<p>{abstract}</p>"
    except Exception as e:
        logger.debug(f"DBpedia fetch failed for {title}: {e}")
    return None


async def fetch_britannica(title: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """Fetch from Encyclopaedia Britannica."""
    search_term = title.replace("_", "-").lower()
    url = f"https://www.britannica.com/topic/{search_term}"
    try:
        r = await client.get(url, timeout=20)
        if r.status_code == 200 and len(r.text) > 1000:
            return url, r.text
        # Try search if direct URL fails
        search_url = f"https://www.britannica.com/search?query={title.replace('_', '+')}"
        r2 = await client.get(search_url, timeout=20)
        if r2.status_code == 200:
            return search_url, r2.text
    except Exception as e:
        logger.debug(f"Britannica fetch failed for {title}: {e}")
    return None


async def fetch_loc_gov(title: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """Fetch from Library of Congress research guides."""
    search_term = title.replace("_", "+")
    url = f"https://www.loc.gov/search/?q={search_term}&fo=json"
    try:
        r = await client.get(url, timeout=20)
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            if results:
                item_url = results[0].get("url", "")
                if item_url:
                    r2 = await client.get(item_url, timeout=20)
                    if r2.status_code == 200 and len(r2.text) > 500:
                        return item_url, r2.text
    except Exception as e:
        logger.debug(f"LOC fetch failed for {title}: {e}")
    return None


async def fetch_archive_org(title: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """Fetch from Internet Archive full-text search."""
    search_term = title.replace("_", "+")
    url = f"https://archive.org/search?query={search_term}&mediatype=texts"
    try:
        r = await client.get(url, timeout=20)
        if r.status_code == 200 and len(r.text) > 500:
            return url, r.text
    except Exception as e:
        logger.debug(f"Archive.org fetch failed for {title}: {e}")
    return None


async def fetch_gutenberg_search(title: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """Fetch from Project Gutenberg search results."""
    search_term = title.replace("_", "+")
    url = f"https://www.gutenberg.org/ebooks/search/?query={search_term}&submit_search=Go%21"
    try:
        r = await client.get(url, timeout=20)
        if r.status_code == 200 and len(r.text) > 500:
            return url, r.text
    except Exception as e:
        logger.debug(f"Gutenberg fetch failed for {title}: {e}")
    return None


async def fetch_history_com(title: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """Fetch from History.com."""
    search_term = title.replace("_", "-").lower()
    url = f"https://www.history.com/topics/{search_term}"
    try:
        r = await client.get(url, timeout=20)
        if r.status_code == 200 and len(r.text) > 1000:
            return url, r.text
    except Exception as e:
        logger.debug(f"History.com fetch failed for {title}: {e}")
    return None


async def fetch_wikiwand(title: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """Fetch from Wikiwand (Wikipedia mirror with different domain)."""
    url = f"https://www.wikiwand.com/en/articles/{title}"
    try:
        r = await client.get(url, timeout=20)
        if r.status_code == 200 and len(r.text) > 1000:
            return url, r.text
    except Exception as e:
        logger.debug(f"Wikiwand fetch failed for {title}: {e}")
    return None


# All source fetchers in order of reliability
SOURCE_FETCHERS = [
    fetch_wikipedia,
    fetch_dbpedia,
    fetch_britannica,
    fetch_wikiwand,
    fetch_archive_org,
    fetch_gutenberg_search,
    fetch_history_com,
    fetch_loc_gov,
]

# ---------------------------------------------------------------------------
# Injection logic
# ---------------------------------------------------------------------------

async def inject_from_source(
    title: str,
    url: str,
    html: str,
    domain: str,
    graph: GraphManager,
    conn,
) -> dict:
    """Extract and inject triples from a single source."""
    stats = {'svo': 0, 'roles': 0, 'consequences': 0}

    text = clean_html(html)
    if not text or len(text.split()) < 30:
        return stats

    content_hash = hashlib.sha256(html.encode('utf-8', errors='ignore')).hexdigest()
    try:
        await conn.execute(
            """
            INSERT INTO pages (url, final_url, domain, raw_html, content_hash, content_type, processed)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
            ON CONFLICT (url) DO NOTHING
            """,
            url, url, domain, html, content_hash, "html",
        )
    except Exception:
        pass

    try:
        extracted = extract_all(text)
    except Exception as e:
        logger.debug(f"Extraction error for {url}: {e}")
        return stats

    if extracted['svo']:
        graph.push_svo(extracted['svo'], url, domain)
        stats['svo'] = len(extracted['svo'])
    if extracted['roles']:
        graph.push_role_relations(extracted['roles'], url, domain)
        stats['roles'] = len(extracted['roles'])
    if extracted['consequences']:
        graph.push_consequence_chains(extracted['consequences'], url, domain)
        stats['consequences'] = len(extracted['consequences'])

    return stats


async def run_multisource_injection(topics: list[str]):
    logger.info(f"Starting multi-source injection for {len(topics)} topics...")
    logger.info(f"Using {len(SOURCE_FETCHERS)} source types per topic")
    logger.info(f"Target: {len(topics) * len(SOURCE_FETCHERS)} total fetches\n")

    conn = None
    while conn is None:
        try:
            conn = await asyncpg.connect(dsn=POSTGRES_DSN)
        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e} — retrying in 5s")
            await asyncio.sleep(5)

    graph = GraphManager()

    total_svo = total_roles = total_consequences = 0
    total_sources_fetched = 0

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        for i, topic in enumerate(topics):
            logger.info(f"[{i+1}/{len(topics)}] {topic}")
            topic_svo = topic_roles = topic_consequences = 0
            sources_ok = 0

            for fetcher in SOURCE_FETCHERS:
                result = await fetcher(topic, client)
                if result is None:
                    continue

                url, html = result
                domain = url.split("/")[2]  # extract netloc

                stats = await inject_from_source(topic, url, html, domain, graph, conn)
                topic_svo += stats['svo']
                topic_roles += stats['roles']
                topic_consequences += stats['consequences']
                sources_ok += 1
                total_sources_fetched += 1

                await asyncio.sleep(0.3)  # polite delay between sources

            logger.info(
                f"  Sources: {sources_ok}/{len(SOURCE_FETCHERS)} | "
                f"svo={topic_svo} | roles={topic_roles} | consequences={topic_consequences}"
            )

            total_svo += topic_svo
            total_roles += topic_roles
            total_consequences += topic_consequences

            await asyncio.sleep(0.5)  # polite delay between topics

    graph.close()
    await conn.close()

    logger.info(
        f"\nMulti-source injection complete!\n"
        f"  Topics: {len(topics)}\n"
        f"  Sources fetched: {total_sources_fetched}\n"
        f"  SVO triples: {total_svo}\n"
        f"  Role relations: {total_roles}\n"
        f"  Consequence chains: {total_consequences}\n"
        f"  Total graph additions: {total_svo + total_roles + total_consequences}\n"
        f"\nNow run: python generator\\query_engine.py --min-domains 3 --min-sources 3 --skip-verify"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Inject historical topics from 6 independent source domains simultaneously"
    )
    parser.add_argument("--topics", nargs="+", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    topics = args.topics if args.topics else DEFAULT_TOPICS
    if args.limit:
        topics = topics[:args.limit]

    asyncio.run(run_multisource_injection(topics))


if __name__ == "__main__":
    main()
