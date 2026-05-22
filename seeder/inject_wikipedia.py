"""
seeder/inject_wikipedia.py — Wikipedia Direct Injection (U2)

Bypasses the crawler entirely. Fetches Wikipedia articles about known
high-value historical topics directly via the Wikipedia REST API,
extracts triples using the same NLP pipeline as the extractor, and
injects them into Neo4j and PostgreSQL.

This populates the graph with thousands of triples in hours rather than
waiting weeks for the crawler to accumulate enough data.

Usage:
    python seeder/inject_wikipedia.py
    python seeder/inject_wikipedia.py --topics-file seeder/topics.txt
    python seeder/inject_wikipedia.py --topics "Standard Oil" "French Revolution"
    python seeder/inject_wikipedia.py --list-file seeder/topics.txt  # alias for --topics-file
"""

import argparse
import asyncio
import asyncpg
import hashlib
import logging
import os
import sys
import time

import httpx

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extractor.worker import extract_all, clean_html
from graph.schema import GraphManager
from lib.topics import add_topics_file_argument, resolve_topics

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepQuest_Seeder")

POSTGRES_DSN = "postgresql://deepquest:deepquestpassword@localhost:5432/deepquestdb"
WIKIPEDIA_API = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_CONTENT_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_BASE = "https://en.wikipedia.org/wiki/{title}"

# ---------------------------------------------------------------------------
# High-value historical topics — covers the kinds of facts DeepQuest needs
# ---------------------------------------------------------------------------

DEFAULT_TOPICS = [
    # Corporate / Industrial history
    "Standard_Oil",
    "Carnegie_Steel_Company",
    "United_States_Steel_Corporation",
    "Ford_Motor_Company",
    "General_Motors",
    "John_D._Rockefeller",
    "Andrew_Carnegie",
    "J._P._Morgan",
    "Cornelius_Vanderbilt",
    "Jay_Gould",
    "Sherman_Antitrust_Act",
    "Ida_Tarbell",
    "Robber_baron_(industrialist)",
    "Gilded_Age",
    "Industrial_Revolution",
    "Second_Industrial_Revolution",

    # Political / Historical figures
    "Abraham_Lincoln",
    "Ulysses_S._Grant",
    "Theodore_Roosevelt",
    "Woodrow_Wilson",
    "Benjamin_Franklin",
    "Alexander_Hamilton",
    "Thomas_Jefferson",
    "Napoleon_Bonaparte",
    "Otto_von_Bismarck",
    "Queen_Victoria",
    "Benjamin_Disraeli",
    "William_Gladstone",

    # Scientific / Invention history
    "Thomas_Edison",
    "Nikola_Tesla",
    "Alexander_Graham_Bell",
    "Herman_Hollerith",
    "Samuel_Morse",
    "Eli_Whitney",
    "James_Watt",
    "George_Stephenson",
    "Isambard_Kingdom_Brunel",
    "Louis_Pasteur",
    "Charles_Darwin",
    "Marie_Curie",
    "Michael_Faraday",
    "James_Clerk_Maxwell",

    # Wars / Conflicts
    "American_Civil_War",
    "World_War_I",
    "Franco-Prussian_War",
    "Crimean_War",
    "Spanish-American_War",
    "Boer_War",
    "Seven_Years'_War",
    "Napoleonic_Wars",

    # Economic / Financial history
    "Panic_of_1873",
    "Panic_of_1893",
    "Panic_of_1907",
    "Great_Depression",
    "Federal_Reserve",
    "Bank_of_England",
    "East_India_Company",
    "Dutch_East_India_Company",
    "Transcontinental_Railroad",
    "Erie_Canal",

    # Colonial / Imperial history
    "British_Empire",
    "French_colonial_empire",
    "Berlin_Conference",
    "Scramble_for_Africa",
    "Indian_Rebellion_of_1857",
    "Opium_Wars",

    # Technology / Communications
    "Telegraph",
    "Telephone",
    "Typewriter",
    "Printing_press",
    "Steam_engine",
    "Railway",
    "Suez_Canal",
    "Panama_Canal",

    # Notable companies / organisations
    "Western_Union",
    "American_Telephone_and_Telegraph",
    "IBM",
    "Tabulating_Machine_Company",
    "Remington_Arms",
    "Winchester_Repeating_Arms_Company",
    "Pinkerton_National_Detective_Agency",
]


async def fetch_wikipedia_article(title: str, client: httpx.AsyncClient) -> tuple[str, str] | None:
    """
    Fetch a Wikipedia article's plain text via the MediaWiki API (action=parse).
    Returns (url, html_content) or None on failure.
    """
    url = WIKIPEDIA_BASE.format(title=title)

    params = {
        "action": "parse",
        "page": title.replace("_", " "),
        "prop": "text",
        "format": "json",
        "redirects": "1",
    }

    try:
        response = await client.get(
            WIKIPEDIA_CONTENT_API,
            params=params,
            timeout=30,
        )
        if response.status_code == 200:
            data = response.json()
            if "parse" in data and "text" in data["parse"]:
                html = data["parse"]["text"]["*"]
                # Update URL to use the canonical title
                canonical = data["parse"].get("title", title).replace(" ", "_")
                url = WIKIPEDIA_BASE.format(title=canonical)
                return url, html
            elif "error" in data:
                logger.warning(f"Wikipedia API error for '{title}': {data['error'].get('info', 'unknown')}")
                return None
        else:
            logger.warning(f"Wikipedia API returned {response.status_code} for: {title}")
            return None
    except Exception as e:
        logger.error(f"Failed to fetch Wikipedia article '{title}': {e}")
        return None


async def inject_article(title: str, url: str, html: str, graph: GraphManager, conn) -> dict:
    """
    Extract triples from a Wikipedia article and inject into Neo4j and PostgreSQL.
    Returns stats dict.
    """
    stats = {'svo': 0, 'roles': 0, 'consequences': 0, 'skipped': False}

    # Clean HTML
    text = clean_html(html)
    if not text or len(text.split()) < 100:
        logger.debug(f"Skipping '{title}' — insufficient text after cleaning")
        stats['skipped'] = True
        return stats

    # Store in PostgreSQL (mark as processed immediately since we're extracting now)
    content_hash = hashlib.sha256(html.encode('utf-8', errors='ignore')).hexdigest()
    try:
        await conn.execute(
            """
            INSERT INTO pages (url, final_url, domain, raw_html, content_hash, content_type, processed)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
            ON CONFLICT (url) DO NOTHING
            """,
            url, url, "en.wikipedia.org", html, content_hash, "html",
        )
    except Exception as e:
        logger.debug(f"DB insert note for {url}: {e}")

    # Extract triples
    try:
        extracted = extract_all(text)
    except Exception as e:
        logger.error(f"Extraction error for '{title}': {e}")
        return stats

    svo = extracted['svo']
    roles = extracted['roles']
    consequences = extracted['consequences']

    domain = "en.wikipedia.org"

    if svo:
        graph.push_svo(svo, url, domain)
        stats['svo'] = len(svo)

    if roles:
        graph.push_role_relations(roles, url, domain)
        stats['roles'] = len(roles)

    if consequences:
        graph.push_consequence_chains(consequences, url, domain)
        stats['consequences'] = len(consequences)

    return stats


async def run_injection(topics: list[str]):
    """Main injection loop."""
    logger.info(f"Starting Wikipedia injection for {len(topics)} topics...")

    # Connect to databases
    conn = None
    while conn is None:
        try:
            conn = await asyncpg.connect(dsn=POSTGRES_DSN)
        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e} — retrying in 5s")
            await asyncio.sleep(5)

    graph = GraphManager()

    total_svo = 0
    total_roles = 0
    total_consequences = 0
    total_skipped = 0

    async with httpx.AsyncClient(
        headers={
            'User-Agent': 'DeepQuestSeeder/1.0 (https://github.com/deepquest; research project) python-httpx',
            'Accept': 'application/json',
        },
        follow_redirects=True,
    ) as client:
        for i, topic in enumerate(topics):
            logger.info(f"[{i+1}/{len(topics)}] Fetching: {topic}")

            result = await fetch_wikipedia_article(topic, client)
            if result is None:
                total_skipped += 1
                continue

            url, html = result
            stats = await inject_article(topic, url, html, graph, conn)

            if stats['skipped']:
                total_skipped += 1
            else:
                total_svo += stats['svo']
                total_roles += stats['roles']
                total_consequences += stats['consequences']
                logger.info(
                    f"  → svo={stats['svo']} | roles={stats['roles']} | "
                    f"consequences={stats['consequences']}"
                )

            # Be polite to Wikipedia — 0.5s between requests
            await asyncio.sleep(0.5)

    graph.close()
    await conn.close()

    logger.info(
        f"\nInjection complete!\n"
        f"  Topics processed: {len(topics) - total_skipped}/{len(topics)}\n"
        f"  SVO triples injected: {total_svo}\n"
        f"  Role relations injected: {total_roles}\n"
        f"  Consequence chains injected: {total_consequences}\n"
        f"  Total graph additions: {total_svo + total_roles + total_consequences}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Inject Wikipedia articles directly into DeepQuest's Neo4j graph"
    )
    parser.add_argument(
        "--topics", nargs="+", default=None,
        help="Specific Wikipedia article titles to inject (use underscores for spaces)"
    )
    add_topics_file_argument(parser, default=None)
    parser.add_argument(
        "--list-file", type=str, default=None,
        help="Alias for --topics-file (backward compatible)",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of articles to process"
    )
    args = parser.parse_args()

    topics_file = args.topics_file or args.list_file
    topics = resolve_topics(
        cli_topics=args.topics,
        topics_file=topics_file,
        default_topics=DEFAULT_TOPICS,
        limit=args.limit,
    )
    if not topics:
        logger.error("No topics to process. Add lines to seeder/topics.txt or pass --topics.")
        sys.exit(1)

    asyncio.run(run_injection(topics))


if __name__ == "__main__":
    main()
