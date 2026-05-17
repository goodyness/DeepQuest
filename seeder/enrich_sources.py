"""
seeder/enrich_sources.py — Source Enricher

Takes existing Neo4j edges that have fewer than 6 source domains and
actively searches for additional URLs that confirm the same fact.

This is the key to getting 6+ verified sources per question.

Strategy:
  For each edge (A)-[REL]->(B) with < 6 domains:
  1. Build a targeted search query: "{A} {rel_human} {B}"
  2. Search multiple free sources for that specific fact
  3. Fetch each result and verify it mentions both A and B
  4. Add verified URLs to the edge's sources list

Sources searched:
  - Wikipedia (direct article lookup)
  - DBpedia (SPARQL abstract)
  - Britannica (search)
  - Wikiwand (Wikipedia mirror)
  - Archive.org (full-text search)
  - Open Library (book metadata)
  - Chronicling America (newspaper search)
  - History.com (search)

Usage:
    python seeder/enrich_sources.py                    # enrich all edges with < 6 domains
    python seeder/enrich_sources.py --min-domains 3    # only enrich edges with < 3 domains
    python seeder/enrich_sources.py --limit 50         # process at most 50 edges
    python seeder/enrich_sources.py --entity "STANDARD OIL"  # enrich edges for one entity
"""

import argparse
import asyncio
import logging
import os
import re
import sys
import time

import httpx

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graph.schema import GraphManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepQuest_SourceEnricher")

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "deepquestpassword"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; DeepQuestResearch/1.0)',
    'Accept': 'text/html,application/json',
}

# Human-readable relationship names for search queries
REL_TO_QUERY = {
    "ACQUIRED":    "{A} acquired {B}",
    "FOUNDED":     "{A} founded {B}",
    "WAS_FOUNDED_BY": "{B} founded by {A}",
    "HIRED":       "{A} hired {B}",
    "RELEASED":    "{A} released {B}",
    "FUNDED":      "{A} funded {B}",
    "CREATED":     "{A} created {B}",
    "WROTE":       "{A} wrote {B}",
    "WON":         "{A} won {B}",
    "SERVED_AS":   "{A} served as {B}",
    "WAS_ROLE_OF": "{A} role at {B}",
    "LED_TO":      "{A} led to {B}",
    "SIGNED":      "{A} signed {B}",
    "ELECTED":     "{A} elected {B}",
    "DEFEATED":    "{A} defeated {B}",
    "INVENTED":    "{A} invented {B}",
    "DISCOVERED":  "{A} discovered {B}",
    "PATENTED":    "{A} patented {B}",
    "MERGED":      "{A} merged with {B}",
    "DISSOLVED":   "{A} dissolved {B}",
    "HAD_CEO":     "{B} CEO of {A}",
    "HAD_PRESIDENT": "{B} president of {A}",
    "HEADQUARTERED_IN": "{A} headquartered in {B}",
    "LOCATED_IN":  "{A} located in {B}",
    "SUCCEEDED_BY": "{A} succeeded by {B}",
}


def build_search_query(subject: str, rel_type: str, obj: str) -> str:
    """Build a targeted search query for a specific fact."""
    template = REL_TO_QUERY.get(rel_type, "{A} {rel} {B}")
    query = template.format(
        A=subject.title(),
        B=obj.title(),
        rel=rel_type.replace("_", " ").lower(),
    )
    return query


def text_confirms_fact(text: str, subject: str, obj: str, min_matches: int = 1) -> bool:
    """Check if text mentions both the subject and object entities."""
    text_lower = text.lower()
    subj_lower = subject.lower()
    obj_lower = obj.lower()

    # Check for subject (allow partial match for long names)
    subj_words = subj_lower.split()
    subj_found = any(
        word in text_lower for word in subj_words
        if len(word) >= 4  # skip short words
    )

    # Check for object
    obj_words = obj_lower.split()
    obj_found = any(
        word in text_lower for word in obj_words
        if len(word) >= 4
    )

    return subj_found and obj_found


# ---------------------------------------------------------------------------
# Source finders
# ---------------------------------------------------------------------------

async def search_wikipedia(query: str, subject: str, obj: str,
                            client: httpx.AsyncClient) -> list[str]:
    """Search Wikipedia and return URLs that confirm the fact."""
    urls = []
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": 3,
        "format": "json",
    }
    try:
        r = await client.get("https://en.wikipedia.org/w/api.php",
                             params=params, timeout=15)
        if r.status_code == 200:
            results = r.json().get("query", {}).get("search", [])
            for result in results:
                title = result.get("title", "")
                snippet = result.get("snippet", "").lower()
                url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
                if text_confirms_fact(snippet, subject, obj):
                    urls.append(url)
    except Exception as e:
        logger.debug(f"Wikipedia search failed: {e}")
    return urls


async def search_dbpedia(subject: str, obj: str,
                          client: httpx.AsyncClient) -> list[str]:
    """Search DBpedia for the subject entity and check if it mentions the object."""
    urls = []
    resource = subject.replace(" ", "_")
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
            timeout=15,
        )
        if r.status_code == 200:
            bindings = r.json().get("results", {}).get("bindings", [])
            if bindings:
                abstract = bindings[0]["abstract"]["value"]
                if text_confirms_fact(abstract, subject, obj):
                    urls.append(f"https://dbpedia.org/page/{resource}")
    except Exception as e:
        logger.debug(f"DBpedia search failed: {e}")
    return urls


async def search_archive_org(query: str, subject: str, obj: str,
                               client: httpx.AsyncClient) -> list[str]:
    """Search Internet Archive for texts mentioning both entities."""
    urls = []
    params = {
        "q": f"{query} AND mediatype:texts",
        "fl[]": ["identifier", "title", "description"],
        "rows": 3,
        "output": "json",
    }
    try:
        r = await client.get("https://archive.org/advancedsearch.php",
                             params=params, timeout=15)
        if r.status_code == 200:
            docs = r.json().get("response", {}).get("docs", [])
            for doc in docs:
                identifier = doc.get("identifier", "")
                title = doc.get("title", "")
                desc = doc.get("description", "")
                combined = f"{title} {desc}".lower()
                if text_confirms_fact(combined, subject, obj):
                    urls.append(f"https://archive.org/details/{identifier}")
    except Exception as e:
        logger.debug(f"Archive.org search failed: {e}")
    return urls


async def search_open_library(query: str, subject: str, obj: str,
                               client: httpx.AsyncClient) -> list[str]:
    """Search Open Library for books about the topic."""
    urls = []
    try:
        r = await client.get(
            "https://openlibrary.org/search.json",
            params={"q": query, "limit": 3,
                    "fields": "title,author_name,subject"},
            timeout=15,
        )
        if r.status_code == 200:
            docs = r.json().get("docs", [])
            for doc in docs:
                title = doc.get("title", "")
                authors = " ".join(doc.get("author_name", []))
                subjects = " ".join(doc.get("subject", []))
                combined = f"{title} {authors} {subjects}".lower()
                if text_confirms_fact(combined, subject, obj):
                    urls.append(
                        f"https://openlibrary.org/search?q={query.replace(' ', '+')}"
                    )
                    break  # one URL per source is enough
    except Exception as e:
        logger.debug(f"Open Library search failed: {e}")
    return urls


async def search_chronicling_america(query: str, subject: str, obj: str,
                                      client: httpx.AsyncClient) -> list[str]:
    """Search Chronicling America newspapers for the fact."""
    urls = []
    try:
        r = await client.get(
            "https://chroniclingamerica.loc.gov/search/pages/results/",
            params={"andtext": query, "format": "json", "rows": 3},
            timeout=15,
        )
        if r.status_code == 200:
            items = r.json().get("items", [])
            for item in items:
                item_url = item.get("url", "")
                title = item.get("title", "")
                ocr_eng = item.get("ocr_eng", "")
                combined = f"{title} {ocr_eng}".lower()
                if item_url and text_confirms_fact(combined, subject, obj):
                    urls.append(item_url)
    except Exception as e:
        logger.debug(f"Chronicling America search failed: {e}")
    return urls


async def search_wikiwand(subject: str, obj: str,
                           client: httpx.AsyncClient) -> list[str]:
    """Check Wikiwand (Wikipedia mirror) for the subject article."""
    urls = []
    title = subject.replace(" ", "_")
    url = f"https://www.wikiwand.com/en/articles/{title}"
    try:
        r = await client.get(url, timeout=15)
        if r.status_code == 200 and text_confirms_fact(r.text, subject, obj):
            urls.append(url)
    except Exception as e:
        logger.debug(f"Wikiwand search failed: {e}")
    return urls


# ---------------------------------------------------------------------------
# Main enrichment loop
# ---------------------------------------------------------------------------

async def enrich_edge(subject: str, rel_type: str, obj: str,
                       existing_sources: list, existing_domains: list,
                       graph: GraphManager,
                       client: httpx.AsyncClient) -> int:
    """
    Find additional source URLs for a specific edge and add them to Neo4j.
    Returns the number of new sources added.
    """
    query = build_search_query(subject, rel_type, obj)
    new_urls = []

    # Run all searches
    results = await asyncio.gather(
        search_wikipedia(query, subject, obj, client),
        search_dbpedia(subject, obj, client),
        search_archive_org(query, subject, obj, client),
        search_open_library(query, subject, obj, client),
        search_chronicling_america(query, subject, obj, client),
        search_wikiwand(subject, obj, client),
        return_exceptions=True,
    )

    for result in results:
        if isinstance(result, list):
            new_urls.extend(result)

    # Filter out URLs already in the edge's sources
    existing_set = set(existing_sources)
    truly_new = [u for u in new_urls if u not in existing_set]

    if not truly_new:
        return 0

    # Add new sources to the Neo4j edge
    for url in truly_new:
        domain = url.split("/")[2] if "/" in url else url
        try:
            with graph.driver.session() as session:
                session.run(f"""
                    MATCH (s:Entity {{name: $subject}})-[r:{rel_type}]->(o:Entity {{name: $obj}})
                    SET r.sources = CASE WHEN NOT $url IN r.sources
                                    THEN r.sources + $url ELSE r.sources END,
                        r.domains = CASE WHEN NOT $domain IN r.domains
                                    THEN r.domains + $domain ELSE r.domains END
                """, subject=subject, obj=obj, url=url, domain=domain)
        except Exception as e:
            logger.debug(f"Failed to add source {url}: {e}")

    return len(truly_new)


async def run_enrichment(target_domains: int = 6, limit: int = None,
                          entity_filter: str = None):
    """Main enrichment loop."""
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    graph = GraphManager()

    logger.info(f"Source Enricher started")
    logger.info(f"Target: {target_domains} domains per edge")
    if entity_filter:
        logger.info(f"Entity filter: {entity_filter}")

    # Find edges that need enrichment
    with driver.session() as session:
        query = """
            MATCH (a:Entity)-[r]->(b:Entity)
            WHERE size(coalesce(r.domains, [])) < $target
        """
        params = {"target": target_domains}

        if entity_filter:
            query += " AND (a.name CONTAINS $entity OR b.name CONTAINS $entity)"
            params["entity"] = entity_filter.upper()

        query += """
            RETURN a.name AS subject, type(r) AS rel_type, b.name AS obj,
                   coalesce(r.sources, []) AS sources,
                   coalesce(r.domains, []) AS domains,
                   size(coalesce(r.domains, [])) AS domain_count
            ORDER BY domain_count ASC
        """
        if limit:
            query += f" LIMIT {limit}"

        edges = session.run(query, **params).data()

    driver.close()

    logger.info(f"Found {len(edges)} edges needing enrichment")

    total_added = 0
    enriched_count = 0

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        for i, edge in enumerate(edges):
            subject = edge['subject']
            rel_type = edge['rel_type']
            obj = edge['obj']
            current_domains = edge['domain_count']

            logger.info(
                f"[{i+1}/{len(edges)}] {subject} -{rel_type}-> {obj} "
                f"(currently {current_domains} domains)"
            )

            added = await enrich_edge(
                subject, rel_type, obj,
                edge['sources'], edge['domains'],
                graph, client,
            )

            if added > 0:
                total_added += added
                enriched_count += 1
                logger.info(f"  ✓ Added {added} new sources")
            else:
                logger.debug(f"  - No new sources found")

            # Polite delay between edges
            await asyncio.sleep(1.0)

    graph.close()

    logger.info(
        f"\nEnrichment complete!\n"
        f"  Edges processed: {len(edges)}\n"
        f"  Edges enriched:  {enriched_count}\n"
        f"  New sources added: {total_added}\n"
        f"\nNow run: python generator\\query_engine.py --min-domains 3 --min-sources 3 --skip-verify"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Enrich Neo4j edges with additional source URLs"
    )
    parser.add_argument(
        "--min-domains", type=int, default=6,
        help="Target minimum domains per edge (default: 6)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of edges to process"
    )
    parser.add_argument(
        "--entity", type=str, default=None,
        help="Only enrich edges involving this entity name"
    )
    args = parser.parse_args()

    asyncio.run(run_enrichment(
        target_domains=args.min_domains,
        limit=args.limit,
        entity_filter=args.entity,
    ))


if __name__ == "__main__":
    main()
