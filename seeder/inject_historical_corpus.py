"""
seeder/inject_historical_corpus.py — Historical Corpus Ingestion (U11)

Fetches content directly from historical archive APIs that are specifically
designed for research. These sources contain the deep historical facts
DeepQuest needs — newspaper articles, historical books, government records.

Sources:
  1. Chronicling America (Library of Congress) — US newspapers 1770–1963
  2. Internet Archive Texts — historical books and documents
  3. Open Library — structured book/author metadata
  4. Europeana — European cultural heritage

All APIs are free, no authentication required.

Usage:
    python seeder/inject_historical_corpus.py
    python seeder/inject_historical_corpus.py --source chronicling --limit 50
    python seeder/inject_historical_corpus.py --source archive --query "standard oil rockefeller"
    python seeder/inject_historical_corpus.py --source all --limit 100
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
logger = logging.getLogger("DeepQuest_CorpusSeeder")

POSTGRES_DSN = "postgresql://deepquest:deepquestpassword@localhost:5432/deepquestdb"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; DeepQuestResearch/1.0)',
    'Accept': 'application/json, text/html',
}

# ---------------------------------------------------------------------------
# Historical search queries — topics that produce deep historical facts
# ---------------------------------------------------------------------------

HISTORICAL_QUERIES = [
    # Corporate / Industrial
    "standard oil rockefeller monopoly",
    "carnegie steel company pittsburgh",
    "railroad trust vanderbilt morgan",
    "sherman antitrust act 1890",
    "gilded age industrial revolution",
    "john pierpont morgan banker",
    "ida tarbell muckraker investigation",
    # Political / Presidential
    "abraham lincoln civil war president",
    "theodore roosevelt trust busting",
    "woodrow wilson federal reserve",
    "benjamin franklin founding father",
    "alexander hamilton treasury secretary",
    # Scientific / Invention
    "thomas edison electric light patent",
    "nikola tesla alternating current",
    "alexander graham bell telephone patent",
    "herman hollerith tabulating machine census",
    "samuel morse telegraph invention",
    "james watt steam engine",
    "louis pasteur germ theory",
    "charles darwin evolution natural selection",
    "marie curie radium discovery",
    # Wars / Conflicts
    "american civil war battle gettysburg",
    "world war one western front",
    "franco prussian war 1870",
    "spanish american war 1898",
    # Economic
    "panic 1873 railroad depression",
    "panic 1907 bank crisis morgan",
    "federal reserve act 1913",
    "east india company trade monopoly",
    # Technology
    "transcontinental railroad 1869",
    "suez canal construction 1869",
    "typewriter remington sholes",
    "telephone exchange switchboard",
]

# ---------------------------------------------------------------------------
# Chronicling America (Library of Congress newspaper archive)
# ---------------------------------------------------------------------------

async def fetch_chronicling_america(
    query: str,
    client: httpx.AsyncClient,
    limit: int = 5,
) -> list[tuple[str, str]]:
    """
    Search Chronicling America for newspaper pages matching the query.
    Returns list of (url, text_content) tuples.
    """
    results = []
    search_url = "https://chroniclingamerica.loc.gov/search/pages/results/"
    params = {
        "andtext": query,
        "format": "json",
        "rows": limit,
        "sort": "relevance",
    }
    try:
        r = await client.get(search_url, params=params, timeout=30)
        if r.status_code != 200:
            return results
        data = r.json()
        items = data.get("items", [])
        for item in items:
            ocr_url = item.get("url", "")
            if not ocr_url:
                continue
            # Fetch the OCR text version
            text_url = ocr_url.rstrip("/") + "/ocr.txt"
            try:
                tr = await client.get(text_url, timeout=20)
                if tr.status_code == 200 and len(tr.text.split()) > 50:
                    results.append((ocr_url, f"<p>{tr.text}</p>"))
                    await asyncio.sleep(0.5)
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"Chronicling America search failed for '{query}': {e}")
    return results


# ---------------------------------------------------------------------------
# Internet Archive full-text search
# ---------------------------------------------------------------------------

async def fetch_internet_archive(
    query: str,
    client: httpx.AsyncClient,
    limit: int = 5,
) -> list[tuple[str, str]]:
    """
    Search Internet Archive for historical texts matching the query.
    Returns list of (url, html_content) tuples.
    """
    results = []
    search_url = "https://archive.org/advancedsearch.php"
    params = {
        "q": f"{query} AND mediatype:texts AND language:English",
        "fl[]": ["identifier", "title", "description", "subject"],
        "rows": limit,
        "page": 1,
        "output": "json",
        "sort[]": "downloads desc",
    }
    try:
        r = await client.get(search_url, params=params, timeout=30)
        if r.status_code != 200:
            return results
        data = r.json()
        docs = data.get("response", {}).get("docs", [])
        for doc in docs:
            identifier = doc.get("identifier", "")
            if not identifier:
                continue
            url = f"https://archive.org/details/{identifier}"
            # Build text from metadata
            title = doc.get("title", "")
            description = doc.get("description", "")
            subject = doc.get("subject", [])
            if isinstance(subject, list):
                subject = ", ".join(subject)
            text = f"<p>{title}. {description} {subject}</p>"
            if len(text.split()) > 10:
                results.append((url, text))
    except Exception as e:
        logger.debug(f"Internet Archive search failed for '{query}': {e}")
    return results


# ---------------------------------------------------------------------------
# Open Library (structured book metadata)
# ---------------------------------------------------------------------------

async def fetch_open_library(
    query: str,
    client: httpx.AsyncClient,
    limit: int = 5,
) -> list[tuple[str, str]]:
    """
    Search Open Library for books matching the query.
    Returns list of (url, metadata_text) tuples.
    """
    results = []
    search_url = "https://openlibrary.org/search.json"
    params = {
        "q": query,
        "limit": limit,
        "fields": "title,author_name,first_publish_year,subject,publisher,place",
    }
    try:
        r = await client.get(search_url, params=params, timeout=30)
        if r.status_code != 200:
            return results
        data = r.json()
        docs = data.get("docs", [])
        for doc in docs:
            title = doc.get("title", "")
            authors = ", ".join(doc.get("author_name", [])[:3])
            year = doc.get("first_publish_year", "")
            subjects = ", ".join(doc.get("subject", [])[:5])
            publishers = ", ".join(doc.get("publisher", [])[:2])
            places = ", ".join(doc.get("place", [])[:2])

            if not title:
                continue

            url = f"https://openlibrary.org/search?q={query.replace(' ', '+')}"
            text = f"<p>{title}"
            if authors:
                text += f" by {authors}"
            if year:
                text += f", published {year}"
            if publishers:
                text += f" by {publishers}"
            if places:
                text += f" in {places}"
            if subjects:
                text += f". Subjects: {subjects}"
            text += ".</p>"

            if len(text.split()) > 8:
                results.append((url, text))
    except Exception as e:
        logger.debug(f"Open Library search failed for '{query}': {e}")
    return results


# ---------------------------------------------------------------------------
# Europeana (European cultural heritage)
# ---------------------------------------------------------------------------

async def fetch_europeana(
    query: str,
    client: httpx.AsyncClient,
    limit: int = 5,
) -> list[tuple[str, str]]:
    """
    Search Europeana for historical items matching the query.
    Returns list of (url, metadata_text) tuples.
    """
    results = []
    search_url = "https://api.europeana.eu/record/v2/search.json"
    params = {
        "wskey": "api2demo",  # public demo key
        "query": query,
        "rows": limit,
        "profile": "rich",
        "qf": "TYPE:TEXT",
    }
    try:
        r = await client.get(search_url, params=params, timeout=30)
        if r.status_code != 200:
            return results
        data = r.json()
        items = data.get("items", [])
        for item in items:
            title_list = item.get("title", [])
            title = title_list[0] if title_list else ""
            desc_list = item.get("dcDescription", [])
            desc = desc_list[0] if desc_list else ""
            creator_list = item.get("dcCreator", [])
            creator = creator_list[0] if creator_list else ""
            url = item.get("guid", "https://www.europeana.eu")

            text = f"<p>{title}"
            if creator:
                text += f" by {creator}"
            if desc:
                text += f". {desc}"
            text += "</p>"

            if len(text.split()) > 8:
                results.append((url, text))
    except Exception as e:
        logger.debug(f"Europeana search failed for '{query}': {e}")
    return results


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

async def inject_results(
    results: list[tuple[str, str]],
    domain: str,
    graph: GraphManager,
    conn,
) -> dict:
    """Extract and inject triples from a list of (url, html) results."""
    stats = {'svo': 0, 'roles': 0, 'consequences': 0, 'pages': 0}

    for url, html in results:
        text = clean_html(html)
        if not text or len(text.split()) < 20:
            # For very short metadata texts, use them directly
            text = html.replace("<p>", "").replace("</p>", "")

        if len(text.split()) < 5:
            continue

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
            if extracted['svo']:
                graph.push_svo(extracted['svo'], url, domain)
                stats['svo'] += len(extracted['svo'])
            if extracted['roles']:
                graph.push_role_relations(extracted['roles'], url, domain)
                stats['roles'] += len(extracted['roles'])
            if extracted['consequences']:
                graph.push_consequence_chains(extracted['consequences'], url, domain)
                stats['consequences'] += len(extracted['consequences'])
            stats['pages'] += 1
        except Exception as e:
            logger.debug(f"Extraction error for {url}: {e}")

    return stats


async def run_corpus_injection(
    sources: list[str],
    queries: list[str],
    limit_per_query: int = 5,
):
    logger.info(f"Starting historical corpus injection")
    logger.info(f"Sources: {sources}")
    logger.info(f"Queries: {len(queries)}")
    logger.info(f"Results per query per source: {limit_per_query}\n")

    conn = None
    while conn is None:
        try:
            conn = await asyncpg.connect(dsn=POSTGRES_DSN)
        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e} — retrying in 5s")
            await asyncio.sleep(5)

    graph = GraphManager()
    total = {'svo': 0, 'roles': 0, 'consequences': 0, 'pages': 0}

    source_map = {
        "chronicling": (fetch_chronicling_america, "chroniclingamerica.loc.gov"),
        "archive":     (fetch_internet_archive,    "archive.org"),
        "openlibrary": (fetch_open_library,        "openlibrary.org"),
        "europeana":   (fetch_europeana,           "europeana.eu"),
    }

    active_sources = {k: v for k, v in source_map.items() if k in sources or "all" in sources}

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        for i, query in enumerate(queries):
            logger.info(f"[{i+1}/{len(queries)}] Query: '{query}'")

            for source_name, (fetcher, domain) in active_sources.items():
                try:
                    results = await fetcher(query, client, limit=limit_per_query)
                    if results:
                        stats = await inject_results(results, domain, graph, conn)
                        logger.info(
                            f"  {source_name}: {len(results)} results | "
                            f"svo={stats['svo']} roles={stats['roles']} "
                            f"consequences={stats['consequences']}"
                        )
                        for k in total:
                            total[k] += stats[k]
                    else:
                        logger.debug(f"  {source_name}: no results")
                except Exception as e:
                    logger.error(f"  {source_name} error: {e}")

                await asyncio.sleep(1.0)  # polite delay between sources

            await asyncio.sleep(0.5)  # delay between queries

    graph.close()
    await conn.close()

    logger.info(
        f"\nCorpus injection complete!\n"
        f"  Queries processed: {len(queries)}\n"
        f"  Pages stored: {total['pages']}\n"
        f"  SVO triples: {total['svo']}\n"
        f"  Role relations: {total['roles']}\n"
        f"  Consequence chains: {total['consequences']}\n"
        f"  Total graph additions: {total['svo'] + total['roles'] + total['consequences']}\n"
        f"\nNext steps:\n"
        f"  python seeder\\merge_entities.py\n"
        f"  python generator\\query_engine.py --min-domains 2 --min-sources 2 --skip-verify"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Inject historical corpus data from archive APIs"
    )
    parser.add_argument(
        "--source",
        choices=["chronicling", "archive", "openlibrary", "europeana", "all"],
        default="all",
        help="Which source(s) to use (default: all)"
    )
    parser.add_argument(
        "--query", type=str, default=None,
        help="Single custom search query"
    )
    parser.add_argument(
        "--limit", type=int, default=5,
        help="Results per query per source (default: 5)"
    )
    args = parser.parse_args()

    sources = ["all"] if args.source == "all" else [args.source]
    queries = [args.query] if args.query else HISTORICAL_QUERIES

    asyncio.run(run_corpus_injection(sources, queries, limit_per_query=args.limit))


if __name__ == "__main__":
    main()
