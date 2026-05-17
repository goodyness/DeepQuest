"""
seeder/merge_entities.py — Entity Resolution & Edge Consolidation (U6)

Two problems this solves:

1. ENTITY MERGING: "SUPREME COURT", "THE SUPREME COURT", "U.S. SUPREME COURT"
   are stored as 3 separate nodes. This script merges them into one canonical node
   and redirects all their edges.

2. EDGE CONSOLIDATION: After merging entities, edges that represent the same
   (subject, verb, object) triple but came from different sources get their
   source lists merged into a single edge with multiple domains.

This directly increases the domain count on edges, making the 3-domain and
6-domain thresholds achievable from existing data.

Usage:
    python seeder/merge_entities.py
    python seeder/merge_entities.py --dry-run   (show what would be merged)
    python seeder/merge_entities.py --threshold 3  (min similarity score)
"""

import argparse
import logging
import os
import re
import string
import sys

from neo4j import GraphDatabase

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepQuest_EntityMerger")

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "deepquestpassword"

# ---------------------------------------------------------------------------
# Known canonical name mappings — explicit overrides
# ---------------------------------------------------------------------------

CANONICAL_MAP = {
    # US institutions
    "THE SUPREME COURT": "SUPREME COURT",
    "U.S. SUPREME COURT": "SUPREME COURT",
    "US SUPREME COURT": "SUPREME COURT",
    "UNITED STATES SUPREME COURT": "SUPREME COURT",
    "THE UNITED STATES": "UNITED STATES",
    "THE U.S.": "UNITED STATES",
    "U.S.": "UNITED STATES",
    "US": "UNITED STATES",
    "AMERICA": "UNITED STATES",
    "THE CONGRESS": "CONGRESS",
    "U.S. CONGRESS": "CONGRESS",
    "THE SENATE": "SENATE",
    "U.S. SENATE": "SENATE",
    "THE PRESIDENT": None,  # too generic — discard
    # UK
    "GREAT BRITAIN": "UNITED KINGDOM",
    "BRITAIN": "UNITED KINGDOM",
    "ENGLAND": "UNITED KINGDOM",
    "THE BRITISH EMPIRE": "BRITISH EMPIRE",
    # Companies
    "STANDARD OIL COMPANY": "STANDARD OIL",
    "STANDARD OIL CO": "STANDARD OIL",
    "STANDARD OIL CO.": "STANDARD OIL",
    "THE STANDARD OIL COMPANY": "STANDARD OIL",
    "CARNEGIE STEEL": "CARNEGIE STEEL COMPANY",
    "CARNEGIE STEEL CO": "CARNEGIE STEEL COMPANY",
    "AT&T": "AMERICAN TELEPHONE AND TELEGRAPH",
    "A.T.&T.": "AMERICAN TELEPHONE AND TELEGRAPH",
    # People
    "JOHN ROCKEFELLER": "JOHN D. ROCKEFELLER",
    "J.D. ROCKEFELLER": "JOHN D. ROCKEFELLER",
    "ROCKEFELLER": "JOHN D. ROCKEFELLER",
    "CARNEGIE": "ANDREW CARNEGIE",
    "MORGAN": "J. P. MORGAN",
    "J.P. MORGAN": "J. P. MORGAN",
    "NAPOLEON": "NAPOLEON BONAPARTE",
    "NAPOLEON I": "NAPOLEON BONAPARTE",
    "LINCOLN": "ABRAHAM LINCOLN",
    "ROOSEVELT": "THEODORE ROOSEVELT",
    "T. ROOSEVELT": "THEODORE ROOSEVELT",
    "EDISON": "THOMAS EDISON",
    "TESLA": "NIKOLA TESLA",
    "DARWIN": "CHARLES DARWIN",
    "CURIE": "MARIE CURIE",
    "PASTEUR": "LOUIS PASTEUR",
}

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_STRIP_PREFIXES = ["THE ", "A ", "AN "]
_CORP_SUFFIXES = [" INC", " INC.", " LLC", " LLC.", " CORP", " CORP.",
                  " LTD", " LTD.", " CO", " CO.", " COMPANY", " & CO"]


def normalise(name: str) -> str:
    """Normalise an entity name for comparison."""
    n = name.upper().strip(string.punctuation).strip()
    for prefix in _STRIP_PREFIXES:
        if n.startswith(prefix):
            n = n[len(prefix):]
    for suffix in _CORP_SUFFIXES:
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n.strip()


def levenshtein(a: str, b: str) -> int:
    """Simple Levenshtein distance."""
    if len(a) < len(b):
        return levenshtein(b, a)
    if len(b) == 0:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1,
                            prev[j] + (0 if ca == cb else 1)))
        prev = curr
    return prev[-1]


def similarity_score(a: str, b: str) -> float:
    """Return 0.0–1.0 similarity between two normalised names."""
    na, nb = normalise(a), normalise(b)
    if na == nb:
        return 1.0
    max_len = max(len(na), len(nb))
    if max_len == 0:
        return 1.0
    dist = levenshtein(na, nb)
    return 1.0 - dist / max_len


# ---------------------------------------------------------------------------
# Neo4j operations
# ---------------------------------------------------------------------------

def get_all_entities(driver) -> list[str]:
    with driver.session() as session:
        result = session.run("MATCH (n:Entity) RETURN n.name AS name ORDER BY name")
        return [r["name"] for r in result]


def merge_entity_nodes(driver, old_name: str, canonical_name: str, dry_run: bool = False):
    """
    Redirect all edges from old_name to canonical_name, then delete old_name node.
    """
    if dry_run:
        logger.info(f"  [DRY RUN] Would merge '{old_name}' → '{canonical_name}'")
        return

    with driver.session() as session:
        # Ensure canonical node exists
        session.run("MERGE (n:Entity {name: $name})", name=canonical_name)

        # Redirect outgoing edges
        session.run("""
            MATCH (old:Entity {name: $old})-[r]->(target:Entity)
            MATCH (canon:Entity {name: $canon})
            WHERE old <> canon
            CALL apoc.refactor.from(r, canon)
            YIELD input, output
            RETURN count(*)
        """, old=old_name, canon=canonical_name)

        # Redirect incoming edges
        session.run("""
            MATCH (source:Entity)-[r]->(old:Entity {name: $old})
            MATCH (canon:Entity {name: $canon})
            WHERE old <> canon
            CALL apoc.refactor.to(r, canon)
            YIELD input, output
            RETURN count(*)
        """, old=old_name, canon=canonical_name)

        # Delete the old node if it has no more edges
        session.run("""
            MATCH (old:Entity {name: $old})
            WHERE NOT (old)--()
            DELETE old
        """, old=old_name)


def merge_entity_nodes_no_apoc(driver, old_name: str, canonical_name: str, dry_run: bool = False):
    """
    Merge without APOC — copy edges manually then delete old node.
    Works even without APOC plugin installed.
    """
    if dry_run:
        logger.info(f"  [DRY RUN] Would merge '{old_name}' → '{canonical_name}'")
        return

    with driver.session() as session:
        # Ensure canonical node exists
        session.run("MERGE (n:Entity {name: $name})", name=canonical_name)

        # Get all outgoing relationships from old node
        out_rels = session.run("""
            MATCH (old:Entity {name: $old})-[r]->(target:Entity)
            RETURN type(r) AS rel_type, target.name AS target_name,
                   r.sources AS sources, r.domains AS domains,
                   r.context AS context, r.date AS date,
                   r.occurrences AS occurrences,
                   r.role AS role,
                   r.cause_date AS cause_date, r.effect_date AS effect_date,
                   r.numerical_value AS numerical_value,
                   r.numerical_unit AS numerical_unit
        """, old=old_name).data()

        for rel in out_rels:
            rel_type = rel['rel_type']
            target = rel['target_name']
            if target == canonical_name:
                continue
            # Create equivalent edge from canonical node
            session.run(f"""
                MATCH (canon:Entity {{name: $canon}})
                MATCH (target:Entity {{name: $target}})
                MERGE (canon)-[r:{rel_type}]->(target)
                ON CREATE SET r.sources = $sources, r.domains = $domains,
                              r.context = $context, r.date = $date,
                              r.occurrences = $occurrences
                ON MATCH SET r.sources = r.sources + [x IN $sources WHERE NOT x IN r.sources],
                             r.domains = r.domains + [x IN $domains WHERE NOT x IN r.domains],
                             r.occurrences = r.occurrences + $occurrences
            """, canon=canonical_name, target=target,
                sources=rel.get('sources') or [],
                domains=rel.get('domains') or [],
                context=rel.get('context') or 'GENERAL',
                date=rel.get('date'),
                occurrences=rel.get('occurrences') or 1)

        # Get all incoming relationships to old node
        in_rels = session.run("""
            MATCH (source:Entity)-[r]->(old:Entity {name: $old})
            RETURN type(r) AS rel_type, source.name AS source_name,
                   r.sources AS sources, r.domains AS domains,
                   r.context AS context, r.date AS date,
                   r.occurrences AS occurrences
        """, old=old_name).data()

        for rel in in_rels:
            rel_type = rel['rel_type']
            source = rel['source_name']
            if source == canonical_name:
                continue
            session.run(f"""
                MATCH (source:Entity {{name: $source}})
                MATCH (canon:Entity {{name: $canon}})
                MERGE (source)-[r:{rel_type}]->(canon)
                ON CREATE SET r.sources = $sources, r.domains = $domains,
                              r.context = $context, r.date = $date,
                              r.occurrences = $occurrences
                ON MATCH SET r.sources = r.sources + [x IN $sources WHERE NOT x IN r.sources],
                             r.domains = r.domains + [x IN $domains WHERE NOT x IN r.domains],
                             r.occurrences = r.occurrences + $occurrences
            """, source=source, canon=canonical_name,
                sources=rel.get('sources') or [],
                domains=rel.get('domains') or [],
                context=rel.get('context') or 'GENERAL',
                date=rel.get('date'),
                occurrences=rel.get('occurrences') or 1)

        # Delete old node and its edges
        session.run("""
            MATCH (old:Entity {name: $old})
            DETACH DELETE old
        """, old=old_name)


def consolidate_parallel_edges(driver, dry_run: bool = False):
    """
    Find pairs of nodes that have multiple edges of the same type between them
    and merge their source lists.
    """
    logger.info("Consolidating parallel edges (merging duplicate source lists)...")

    with driver.session() as session:
        # Find all relationship types
        rel_types = session.run(
            "MATCH ()-[r]->() RETURN DISTINCT type(r) AS rel_type"
        ).data()

        total_merged = 0
        for row in rel_types:
            rel_type = row['rel_type']
            # For each rel type, find nodes with multiple edges of that type
            # and merge their sources
            result = session.run(f"""
                MATCH (a:Entity)-[r:{rel_type}]->(b:Entity)
                WITH a, b, collect(r) AS rels
                WHERE size(rels) > 1
                RETURN a.name AS a_name, b.name AS b_name, size(rels) AS count
            """).data()

            for row2 in result:
                if dry_run:
                    logger.info(
                        f"  [DRY RUN] Would consolidate {row2['count']} "
                        f"'{rel_type}' edges: {row2['a_name']} → {row2['b_name']}"
                    )
                else:
                    # Merge all sources and domains into the first edge, delete others
                    session.run(f"""
                        MATCH (a:Entity {{name: $a}})-[r:{rel_type}]->(b:Entity {{name: $b}})
                        WITH collect(r) AS rels
                        WITH rels[0] AS keep, rels[1..] AS remove,
                             reduce(s=[], r IN rels | s + coalesce(r.sources,[])) AS all_sources,
                             reduce(d=[], r IN rels | d + coalesce(r.domains,[])) AS all_domains,
                             reduce(o=0, r IN rels | o + coalesce(r.occurrences,0)) AS total_occ
                        SET keep.sources = apoc.coll.toSet(all_sources),
                            keep.domains = apoc.coll.toSet(all_domains),
                            keep.occurrences = total_occ
                        FOREACH (r IN remove | DELETE r)
                    """, a=row2['a_name'], b=row2['b_name'])
                    total_merged += row2['count'] - 1

        if not dry_run:
            logger.info(f"Consolidated {total_merged} duplicate edges")


def consolidate_parallel_edges_no_apoc(driver, dry_run: bool = False):
    """Consolidate parallel edges without APOC."""
    logger.info("Consolidating parallel edges...")

    with driver.session() as session:
        rel_types = session.run(
            "MATCH ()-[r]->() RETURN DISTINCT type(r) AS rel_type"
        ).data()

        total_merged = 0
        for row in rel_types:
            rel_type = row['rel_type']
            result = session.run(f"""
                MATCH (a:Entity)-[r:{rel_type}]->(b:Entity)
                WITH a, b, collect(r) AS rels
                WHERE size(rels) > 1
                RETURN a.name AS a_name, b.name AS b_name, size(rels) AS count
            """).data()

            for row2 in result:
                if dry_run:
                    logger.info(
                        f"  [DRY RUN] {row2['count']} '{rel_type}' edges: "
                        f"{row2['a_name']} → {row2['b_name']}"
                    )
                    continue

                # Get all edges
                edges = session.run(f"""
                    MATCH (a:Entity {{name: $a}})-[r:{rel_type}]->(b:Entity {{name: $b}})
                    RETURN id(r) AS rid, r.sources AS sources, r.domains AS domains,
                           r.occurrences AS occ
                """, a=row2['a_name'], b=row2['b_name']).data()

                if len(edges) < 2:
                    continue

                # Merge all sources and domains
                all_sources = []
                all_domains = []
                total_occ = 0
                for e in edges:
                    all_sources.extend(e.get('sources') or [])
                    all_domains.extend(e.get('domains') or [])
                    total_occ += e.get('occ') or 1

                unique_sources = list(dict.fromkeys(all_sources))
                unique_domains = list(dict.fromkeys(all_domains))

                # Keep first edge, update it, delete the rest
                keep_id = edges[0]['rid']
                session.run(f"""
                    MATCH (a:Entity {{name: $a}})-[r:{rel_type}]->(b:Entity {{name: $b}})
                    WHERE id(r) = $rid
                    SET r.sources = $sources, r.domains = $domains, r.occurrences = $occ
                """, a=row2['a_name'], b=row2['b_name'],
                    rid=keep_id, sources=unique_sources,
                    domains=unique_domains, occ=total_occ)

                for e in edges[1:]:
                    session.run(f"""
                        MATCH (a:Entity {{name: $a}})-[r:{rel_type}]->(b:Entity {{name: $b}})
                        WHERE id(r) = $rid
                        DELETE r
                    """, a=row2['a_name'], b=row2['b_name'], rid=e['rid'])
                    total_merged += 1

        logger.info(f"Consolidated {total_merged} duplicate edges")


def run_merge(dry_run: bool = False, similarity_threshold: float = 0.85):
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    logger.info("Loading all entity names from Neo4j...")
    entities = get_all_entities(driver)
    logger.info(f"Found {len(entities)} entities")

    # Step 1: Apply explicit canonical map
    explicit_merges = 0
    for entity in entities:
        canonical = CANONICAL_MAP.get(entity)
        if canonical is None and entity in CANONICAL_MAP:
            # Mapped to None = discard
            logger.info(f"Discarding generic entity: '{entity}'")
            if not dry_run:
                with driver.session() as session:
                    session.run("MATCH (n:Entity {name: $name}) DETACH DELETE n", name=entity)
            explicit_merges += 1
        elif canonical and canonical != entity:
            logger.info(f"Explicit merge: '{entity}' → '{canonical}'")
            merge_entity_nodes_no_apoc(driver, entity, canonical, dry_run)
            explicit_merges += 1

    logger.info(f"Applied {explicit_merges} explicit canonical mappings")

    # Step 2: Fuzzy merge — find similar entity names
    entities_after = get_all_entities(driver)
    fuzzy_merges = 0
    merged_set = set()

    for i, a in enumerate(entities_after):
        if a in merged_set:
            continue
        for b in entities_after[i+1:]:
            if b in merged_set:
                continue
            score = similarity_score(a, b)
            if score >= similarity_threshold and score < 1.0:
                # Pick the shorter/simpler name as canonical
                canonical = a if len(normalise(a)) <= len(normalise(b)) else b
                other = b if canonical == a else a
                logger.info(
                    f"Fuzzy merge (score={score:.2f}): '{other}' → '{canonical}'"
                )
                merge_entity_nodes_no_apoc(driver, other, canonical, dry_run)
                merged_set.add(other)
                fuzzy_merges += 1

    logger.info(f"Applied {fuzzy_merges} fuzzy merges")

    # Step 3: Consolidate parallel edges
    consolidate_parallel_edges_no_apoc(driver, dry_run)

    # Step 4: Report final state
    with driver.session() as session:
        node_count = session.run("MATCH (n:Entity) RETURN count(n) AS c").single()["c"]
        rel_count = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        max_domains = session.run(
            "MATCH ()-[r]->() RETURN max(size(coalesce(r.domains,[]))) AS m"
        ).single()["m"]

    logger.info(
        f"\nMerge complete!\n"
        f"  Entities: {len(entities)} → {node_count}\n"
        f"  Relationships: (consolidated)\n"
        f"  Max domains on any edge: {max_domains}\n"
        f"  Total relationships: {rel_count}"
    )

    driver.close()


def main():
    parser = argparse.ArgumentParser(description="Merge similar entity nodes in Neo4j")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be merged without making changes")
    parser.add_argument("--threshold", type=float, default=0.85,
                        help="Similarity threshold for fuzzy merging (0.0-1.0, default 0.85)")
    args = parser.parse_args()

    run_merge(dry_run=args.dry_run, similarity_threshold=args.threshold)


if __name__ == "__main__":
    main()
