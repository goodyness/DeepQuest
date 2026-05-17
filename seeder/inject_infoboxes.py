"""
seeder/inject_infoboxes.py — Wikipedia Infobox Extraction (U8)

Fetches Wikipedia infoboxes via the MediaWiki API and converts structured
key-value pairs directly into Neo4j edges — no NLP required.

Infoboxes contain exactly the kind of high-precision facts DeepQuest needs:
  - Founded: Standard Oil, 1870
  - CEO: John D. Rockefeller
  - Dissolved: 1911
  - Successor: ExxonMobil, Chevron, etc.

Each infobox fact becomes a typed edge with a date and source URL.
Multiple topics sharing the same entity (e.g. "Standard Oil" appears in
both the Standard Oil article and the Rockefeller article) automatically
accumulate multiple source domains on the same Neo4j edge.

Usage:
    python seeder/inject_infoboxes.py
    python seeder/inject_infoboxes.py --topics "Standard_Oil" "John_D._Rockefeller"
    python seeder/inject_infoboxes.py --limit 30
"""

import argparse
import asyncio
import asyncpg
import hashlib
import logging
import os
import re
import sys

import httpx

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graph.schema import GraphManager

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepQuest_InfoboxSeeder")

POSTGRES_DSN = "postgresql://deepquest:deepquestpassword@localhost:5432/deepquestdb"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; DeepQuestBot/1.0)',
    'Accept': 'application/json',
}

# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

DEFAULT_TOPICS = [
    "Standard_Oil", "Carnegie_Steel_Company", "John_D._Rockefeller",
    "Andrew_Carnegie", "J._P._Morgan", "Cornelius_Vanderbilt",
    "Sherman_Antitrust_Act", "Ida_Tarbell", "Gilded_Age",
    "Thomas_Edison", "Nikola_Tesla", "Alexander_Graham_Bell",
    "Herman_Hollerith", "Samuel_Morse", "James_Watt",
    "Louis_Pasteur", "Charles_Darwin", "Marie_Curie",
    "Abraham_Lincoln", "Theodore_Roosevelt", "Woodrow_Wilson",
    "Napoleon_Bonaparte", "Otto_von_Bismarck", "Queen_Victoria",
    "American_Civil_War", "World_War_I", "Franco-Prussian_War",
    "Federal_Reserve", "East_India_Company", "Dutch_East_India_Company",
    "Transcontinental_Railroad", "Western_Union", "IBM",
    "Remington_Arms", "Pinkerton_National_Detective_Agency",
    "Samuel_Calvin_Tate_Dodd", "James_Densmore",
    "Sholes_and_Glidden_typewriter", "Almon_Brown_Strowger",
]

# ---------------------------------------------------------------------------
# Infobox field → Neo4j relationship type mapping
# ---------------------------------------------------------------------------

FIELD_TO_REL = {
    # People
    "birth_place":    ("BORN_IN",      "place"),
    "death_place":    ("DIED_IN",       "place"),
    "birth_date":     None,  # stored as date on node, not edge
    "death_date":     None,
    "occupation":     ("HAD_OCCUPATION", "occupation"),
    "employer":       ("WORKED_FOR",    "organisation"),
    "known_for":      ("KNOWN_FOR",     "achievement"),
    "nationality":    ("WAS_CITIZEN_OF","country"),
    "alma_mater":     ("STUDIED_AT",    "university"),
    "spouse":         ("MARRIED",       "person"),
    "children":       None,  # skip
    "parents":        None,
    "awards":         ("WON",           "award"),

    # Organisations / Companies
    "founded":        ("FOUNDED",       "event"),
    "founder":        ("WAS_FOUNDED_BY","person"),
    "dissolved":      ("DISSOLVED_IN",  "event"),
    "successor":      ("SUCCEEDED_BY",  "company"),
    "predecessor":    ("PRECEDED_BY",   "company"),
    "parent":         ("OWNED_BY",      "company"),
    "subsidiaries":   ("OWNED",         "company"),
    "industry":       ("OPERATED_IN",   "industry"),
    "products":       ("PRODUCED",      "product"),
    "key_people":     ("HAD_KEY_PERSON","person"),
    "ceo":            ("HAD_CEO",       "person"),
    "chairman":       ("HAD_CHAIRMAN",  "person"),
    "president":      ("HAD_PRESIDENT", "person"),
    "headquarters":   ("HEADQUARTERED_IN","place"),
    "location":       ("LOCATED_IN",    "place"),
    "country":        ("BASED_IN",      "country"),

    # Laws / Events
    "enacted_by":     ("ENACTED_BY",    "organisation"),
    "signed_by":      ("SIGNED_BY",     "person"),
    "date_enacted":   None,
    "effective":      None,
    "repealed":       ("REPEALED_BY",   "event"),
    "subject":        ("CONCERNED",     "topic"),

    # Wars / Conflicts
    "result":         ("RESULTED_IN",   "outcome"),
    "combatant1":     ("INVOLVED",      "country"),
    "combatant2":     ("INVOLVED",      "country"),
    "commander1":     ("HAD_COMMANDER", "person"),
    "commander2":     ("HAD_COMMANDER", "person"),
    "territory":      ("AFFECTED",      "place"),
}

# ---------------------------------------------------------------------------
# Infobox fetcher
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r'\b(1[0-9]{3}|20[0-2][0-9])\b')
_WIKILINK_RE = re.compile(r'\[\[([^\|\]]+)(?:\|[^\]]+)?\]\]')
_TEMPLATE_RE = re.compile(r'\{\{[^}]+\}\}')
_HTML_RE = re.compile(r'<[^>]+>')
_REF_RE = re.compile(r'<ref[^>]*>.*?</ref>', re.DOTALL)


def clean_value(val: str) -> str:
    """Strip wiki markup from an infobox value."""
    val = _REF_RE.sub('', val)
    val = _HTML_RE.sub('', val)
    val = _TEMPLATE_RE.sub('', val)
    # Extract text from wikilinks: [[Target|Display]] → Display or Target
    val = _WIKILINK_RE.sub(lambda m: m.group(1), val)
    val = val.replace("'''", "").replace("''", "")
    val = re.sub(r'\s+', ' ', val).strip()
    return val


def extract_year(val: str) -> str | None:
    """Extract a 4-digit year from a value string."""
    m = _YEAR_RE.search(val)
    return m.group(1) if m else None


async def fetch_infobox(title: str, client: httpx.AsyncClient) -> dict | None:
    """
    Fetch the raw wikitext of a Wikipedia article and extract infobox fields.
    Returns a dict of {field_name: value_string} or None.
    """
    params = {
        "action": "query",
        "titles": title.replace("_", " "),
        "prop": "revisions",
        "rvprop": "content",
        "rvslots": "main",
        "format": "json",
        "redirects": "1",
        "formatversion": "2",
    }
    try:
        r = await client.get("https://en.wikipedia.org/w/api.php",
                             params=params, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
        pages = data.get("query", {}).get("pages", [])
        if not pages or "missing" in pages[0]:
            return None
        wikitext = pages[0].get("revisions", [{}])[0].get("slots", {}).get("main", {}).get("content", "")
        if not wikitext:
            return None
        return parse_infobox(wikitext)
    except Exception as e:
        logger.debug(f"Infobox fetch failed for {title}: {e}")
        return None


def parse_infobox(wikitext: str) -> dict:
    """
    Parse infobox fields from raw wikitext.
    Returns {field_name: cleaned_value}.
    """
    fields = {}

    # Find the infobox template
    infobox_match = re.search(r'\{\{[Ii]nfobox[^|]*\|', wikitext)
    if not infobox_match:
        return fields

    # Extract content between the opening {{ and matching }}
    start = infobox_match.start()
    depth = 0
    end = start
    for i, ch in enumerate(wikitext[start:], start):
        if wikitext[i:i+2] == '{{':
            depth += 1
        elif wikitext[i:i+2] == '}}':
            depth -= 1
            if depth == 0:
                end = i + 2
                break

    infobox_text = wikitext[start:end]

    # Split on | but respect nested {{ }}
    parts = []
    current = []
    depth = 0
    for ch in infobox_text:
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        elif ch == '|' and depth <= 1:
            parts.append(''.join(current))
            current = []
            continue
        current.append(ch)
    if current:
        parts.append(''.join(current))

    for part in parts[1:]:  # skip the template name
        if '=' not in part:
            continue
        key, _, val = part.partition('=')
        key = key.strip().lower().replace(' ', '_').replace('-', '_')
        val = clean_value(val.strip())
        if key and val and len(val) > 1:
            fields[key] = val

    return fields


# ---------------------------------------------------------------------------
# Injection
# ---------------------------------------------------------------------------

def inject_infobox_facts(title: str, url: str, fields: dict,
                          graph: GraphManager) -> dict:
    """Convert infobox fields to Neo4j edges."""
    stats = {'edges': 0, 'skipped': 0}
    domain = "en.wikipedia.org"
    subject = title.replace("_", " ").upper()

    # Extract the article's own date (birth_date, founded, etc.)
    article_date = None
    for date_field in ['birth_date', 'founded', 'date_enacted', 'effective', 'date']:
        if date_field in fields:
            article_date = extract_year(fields[date_field])
            if article_date:
                break

    for field, value in fields.items():
        mapping = FIELD_TO_REL.get(field)
        if mapping is None:
            stats['skipped'] += 1
            continue

        rel_type, obj_type = mapping

        # Split multi-value fields (e.g. "ExxonMobil, Chevron, BP")
        values = [v.strip() for v in re.split(r'[,;]', value) if v.strip()]

        for val in values[:5]:  # max 5 values per field
            if len(val) < 2 or len(val) > 100:
                continue

            obj = val.upper()

            # Extract date from value if present
            date = extract_year(val) or article_date

            try:
                with graph.driver.session() as session:
                    session.execute_write(
                        graph.create_relationship,
                        subject, rel_type, obj, url, domain,
                        "BIOGRAPHICAL" if obj_type == "person" else "CORPORATE",
                        date,
                    )
                stats['edges'] += 1
            except Exception as e:
                logger.debug(f"Edge creation error: {e}")
                stats['skipped'] += 1

    return stats


async def run_infobox_injection(topics: list[str]):
    logger.info(f"Starting infobox injection for {len(topics)} topics...")

    conn = None
    while conn is None:
        try:
            conn = await asyncpg.connect(dsn=POSTGRES_DSN)
        except Exception as e:
            logger.error(f"PostgreSQL connection failed: {e} — retrying in 5s")
            await asyncio.sleep(5)

    graph = GraphManager()
    total_edges = 0
    total_skipped = 0

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        for i, topic in enumerate(topics):
            logger.info(f"[{i+1}/{len(topics)}] {topic}")

            fields = await fetch_infobox(topic, client)
            if not fields:
                logger.debug(f"  No infobox found for {topic}")
                await asyncio.sleep(0.3)
                continue

            url = f"https://en.wikipedia.org/wiki/{topic}"
            stats = inject_infobox_facts(topic, url, fields, graph)

            logger.info(f"  Fields: {len(fields)} | Edges: {stats['edges']} | Skipped: {stats['skipped']}")
            total_edges += stats['edges']
            total_skipped += stats['skipped']

            await asyncio.sleep(0.5)

    graph.close()
    await conn.close()

    logger.info(
        f"\nInfobox injection complete!\n"
        f"  Topics processed: {len(topics)}\n"
        f"  Edges created: {total_edges}\n"
        f"  Fields skipped: {total_skipped}\n"
        f"\nNow run: python seeder\\merge_entities.py\n"
        f"Then:    python generator\\query_engine.py --min-domains 2 --min-sources 2 --skip-verify"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Extract Wikipedia infobox facts directly into Neo4j"
    )
    parser.add_argument("--topics", nargs="+", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    topics = args.topics if args.topics else DEFAULT_TOPICS
    if args.limit:
        topics = topics[:args.limit]

    asyncio.run(run_infobox_injection(topics))


if __name__ == "__main__":
    main()
