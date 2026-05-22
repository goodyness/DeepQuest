"""
check_graph.py — Graph Health Monitor

Gives a comprehensive view of the DeepQuest knowledge graph:
  - Node and relationship counts
  - Domain coverage per edge
  - Best chains available for question generation
  - Extraction yield by source domain
  - Recommendations for what to do next

Usage:
    python check_graph.py
    python check_graph.py --verbose    (show top chains in detail)
    python check_graph.py --domains    (show domain breakdown)
"""

import argparse
import os
import sys

try:
    from neo4j import GraphDatabase
except ImportError:
    print("neo4j driver not installed.")
    sys.exit(1)

try:
    import asyncpg
    import asyncio
    HAS_PG = True
except ImportError:
    HAS_PG = False

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "deepquestpassword"
PG_DSN = "postgresql://deepquest:deepquestpassword@localhost:5432/deepquestdb"


def get_pg_stats():
    if not HAS_PG:
        return {}
    async def _fetch():
        try:
            conn = await asyncpg.connect(PG_DSN)
            total = await conn.fetchval("SELECT COUNT(*) FROM pages")
            processed = await conn.fetchval("SELECT COUNT(*) FROM pages WHERE processed = TRUE")
            pending = await conn.fetchval("SELECT COUNT(*) FROM pages WHERE processed = FALSE")
            domains = await conn.fetch(
                "SELECT domain, COUNT(*) as cnt FROM pages GROUP BY domain ORDER BY cnt DESC LIMIT 10"
            )
            await conn.close()
            return {
                'total': total, 'processed': processed, 'pending': pending,
                'top_domains': [(r['domain'], r['cnt']) for r in domains]
            }
        except Exception as e:
            return {'error': str(e)}
    return asyncio.run(_fetch())


def run_check(verbose: bool = False, show_domains: bool = False):
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    print("\n" + "="*65)
    print("  DEEPQUEST GRAPH HEALTH REPORT")
    print("="*65)

    with driver.session() as session:

        # Basic counts
        nodes = session.run("MATCH (n:Entity) RETURN count(n) AS c").single()["c"]
        rels = session.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        rel_types = session.run(
            "MATCH ()-[r]->() RETURN type(r) AS t, count(r) AS c ORDER BY c DESC"
        ).data()

        print(f"\n📊 GRAPH OVERVIEW")
        print(f"   Entity nodes:      {nodes:,}")
        print(f"   Relationships:     {rels:,}")
        print(f"\n   Relationship types:")
        for rt in rel_types:
            print(f"     {rt['t']:<25} {rt['c']:>6,}")

        # Domain coverage
        max_domains = session.run(
            "MATCH ()-[r]->() RETURN max(size(coalesce(r.domains,[]))) AS m"
        ).single()["m"] or 0

        domain_dist = session.run("""
            MATCH ()-[r]->()
            WITH size(coalesce(r.domains,[])) AS d
            RETURN d, count(*) AS cnt
            ORDER BY d DESC
            LIMIT 10
        """).data()

        print(f"\n🌐 SOURCE DOMAIN COVERAGE")
        print(f"   Max domains on any edge: {max_domains}")
        print(f"   Distribution:")
        for row in domain_dist:
            bar = "█" * min(row['d'], 20)
            status = ""
            if row['d'] >= 6:
                status = " ✓ PRODUCTION READY"
            elif row['d'] >= 3:
                status = " ~ TESTING READY"
            elif row['d'] >= 1:
                status = " ✗ NEEDS MORE SOURCES"
            print(f"     {row['d']:>2} domains: {row['cnt']:>5,} edges  {bar}{status}")

        # Chains available
        chains_1 = session.run("""
            MATCH (a)-[r1]->(b)-[r2]->(c)
            WHERE a.name <> b.name AND b.name <> c.name AND a.name <> c.name
            WITH size(coalesce(r1.domains,[])) + size(coalesce(r2.domains,[])) AS d
            RETURN count(*) AS total, max(d) AS max_d
        """).single()

        chains_3 = session.run("""
            MATCH (a)-[r1]->(b)-[r2]->(c)-[r3]->(d)
            WHERE a.name <> b.name AND b.name <> c.name AND c.name <> d.name
            RETURN count(*) AS total
        """).single()

        print(f"\n🔗 CHAIN AVAILABILITY")
        print(f"   2-hop chains total:      {chains_1['total']:,}")
        print(f"   2-hop max domains (sum):   {chains_1['max_d'] or 0}  (can over-count; see below)")
        print(f"   3-hop chains total:      {chains_3['total']:,}")

        print(f"\n🎯 GENERATOR GATE (unique URLs across both hops)")
        print(f"   Questions need 6 unique netlocs from merged source URLs.")
        print(f"   If max domains per edge < 6, run: python seeder\\enrich_sources.py --limit 200")

        # Chains by threshold
        for threshold in [1, 2, 3, 6, 8]:
            count = session.run("""
                MATCH (a)-[r1]->(b)-[r2]->(c)
                WHERE a.name <> b.name AND b.name <> c.name AND a.name <> c.name
                WITH size(coalesce(r1.domains,[])) + size(coalesce(r2.domains,[])) AS d
                WHERE d >= $t
                RETURN count(*) AS cnt
            """, t=threshold).single()["cnt"]
            status = "✓" if count > 0 else "✗"
            label = ""
            if threshold == 1:
                label = " (--min-domains 1 --skip-verify)"
            elif threshold == 3:
                label = " (--min-domains 3 --skip-verify)"
            elif threshold == 6:
                label = " (production quality)"
            elif threshold == 8:
                label = " (original threshold)"
            print(f"   Chains with {threshold:>2}+ domains:  {count:>6,} {status}{label}")

        # Temporal coverage
        dated = session.run("""
            MATCH ()-[r]->()
            WHERE r.date IS NOT NULL
            RETURN count(r) AS dated
        """).single()["dated"]

        pre1950 = session.run("""
            MATCH ()-[r]->()
            WHERE r.date IS NOT NULL AND toInteger(substring(r.date, 0, 4)) < 1950
            RETURN count(r) AS cnt
        """).single()["cnt"]

        print(f"\n📅 TEMPORAL COVERAGE")
        print(f"   Edges with dates:        {dated:,} / {rels:,} ({100*dated//max(rels,1)}%)")
        print(f"   Pre-1950 edges:          {pre1950:,}")

        # Top chains
        if verbose:
            print(f"\n🏆 TOP 15 CHAINS (by domain count)")
            top_chains = session.run("""
                MATCH (a)-[r1]->(b)-[r2]->(c)
                WHERE a.name <> b.name AND b.name <> c.name AND a.name <> c.name
                WITH a, r1, b, r2, c,
                     size(coalesce(r1.domains,[])) + size(coalesce(r2.domains,[])) AS d
                ORDER BY d DESC
                LIMIT 15
                RETURN a.name AS a, type(r1) AS r1, r1.date AS d1,
                       b.name AS b, type(r2) AS r2, r2.date AS d2,
                       c.name AS c, d AS domains
            """).data()

            for ch in top_chains:
                d1 = f"({ch['d1']})" if ch['d1'] else ""
                d2 = f"({ch['d2']})" if ch['d2'] else ""
                print(f"   [{ch['domains']}d] {ch['a']} -{ch['r1']}{d1}→ "
                      f"\033[92m{ch['b']}\033[0m -{ch['r2']}{d2}→ {ch['c']}")

        # Domain breakdown
        if show_domains:
            print(f"\n🌍 TOP SOURCE DOMAINS IN GRAPH")
            domain_data = session.run("""
                MATCH ()-[r]->()
                UNWIND coalesce(r.domains, []) AS domain
                RETURN domain, count(*) AS cnt
                ORDER BY cnt DESC
                LIMIT 20
            """).data()
            for row in domain_data:
                print(f"   {row['domain']:<45} {row['cnt']:>5,} edges")

    # Recommendations
    print(f"\n💡 RECOMMENDATIONS")
    with driver.session() as session:
        max_d = session.run(
            "MATCH ()-[r]->() RETURN max(size(coalesce(r.domains,[]))) AS m"
        ).single()["m"] or 0

        chains_ready = session.run("""
            MATCH (a)-[r1]->(b)-[r2]->(c)
            WHERE a.name <> b.name AND b.name <> c.name AND a.name <> c.name
            WITH size(coalesce(r1.domains,[])) + size(coalesce(r2.domains,[])) AS d
            WHERE d >= 1
            RETURN count(*) AS cnt
        """).single()["cnt"]

    driver.close()

    # PostgreSQL stats
    if HAS_PG:
        pg = get_pg_stats()
        if 'error' not in pg:
            print(f"\n🗄️  POSTGRESQL (CRAWLED PAGES)")
            print(f"   Total pages:    {pg.get('total', 0):,}")
            print(f"   Processed:      {pg.get('processed', 0):,}")
            print(f"   Pending:        {pg.get('pending', 0):,}")
            if pg.get('top_domains'):
                print(f"   Top domains crawled:")
                for domain, cnt in pg['top_domains'][:5]:
                    print(f"     {domain:<40} {cnt:>5,}")

    # Recommendations
    print(f"\n💡 RECOMMENDATIONS")

    if max_d == 0:
        print("   ⚠ Graph is empty. Run: python seeder\\inject_infoboxes.py")
    elif max_d < 2:
        print("   ⚠ Graph is very sparse. Run the full seeding pipeline:")
        print("     python seeder\\inject_infoboxes.py")
        print("     python seeder\\inject_wikipedia.py")
        print("     python seeder\\inject_historical_corpus.py")
        print("     python seeder\\merge_entities.py")
    elif chains_ready == 0:
        print("   ⚠ No chains found. Run: python seeder\\merge_entities.py")
    elif max_d < 3:
        print("   ✓ Graph has data. Generate with relaxed thresholds:")
        print("     python generator\\query_engine.py --min-domains 1 --min-sources 1 --skip-verify")
        print("   → Then run more seeding to increase domain counts")
    elif max_d < 6:
        print("   ✓ Graph is growing. Generate with moderate thresholds:")
        print("     python generator\\query_engine.py --min-domains 2 --min-sources 2 --skip-verify")
        print("   → Keep crawler running to accumulate more cross-domain evidence")
    else:
        print("   ✓ Graph is production-ready. Generate with full quality gate:")
        print("     python generator\\query_engine.py --min-domains 6 --min-sources 6")

    print()


def main():
    parser = argparse.ArgumentParser(description="DeepQuest graph health monitor")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show top chains in detail")
    parser.add_argument("--domains", "-d", action="store_true",
                        help="Show domain breakdown")
    args = parser.parse_args()
    run_check(verbose=args.verbose, show_domains=args.domains)


if __name__ == "__main__":
    main()
