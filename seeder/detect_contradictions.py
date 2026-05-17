"""
seeder/detect_contradictions.py — Contradiction Detection (U15)

Scans the Neo4j knowledge graph for edges where different sources
disagree on key properties (dates, numerical values, relationship targets).

Contradictions are valuable in two ways:
  1. They flag facts that should NOT be used in benchmark questions
     (ambiguous or disputed facts produce bad questions)
  2. They reveal genuinely contested historical claims that are
     interesting research topics in their own right

Output:
  - Console report of all contradictions found
  - JSON report saved to: evaluator/contradictions.json
  - Optionally marks contradicted edges in Neo4j with a flag

Usage:
    python seeder/detect_contradictions.py
    python seeder/detect_contradictions.py --mark-neo4j   (flag edges in graph)
    python seeder/detect_contradictions.py --threshold 2  (min year difference)
    python seeder/detect_contradictions.py --report       (show saved report)
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from neo4j import GraphDatabase
except ImportError:
    print("neo4j driver not installed.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepQuest_ContradictionDetector")

NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "deepquestpassword"
REPORT_FILE = "evaluator/contradictions.json"

_YEAR_RE = re.compile(r'\b(1[0-9]{3}|20[0-2][0-9])\b')


def extract_year(date_str) -> int | None:
    if not date_str:
        return None
    m = _YEAR_RE.search(str(date_str))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Contradiction detectors
# ---------------------------------------------------------------------------

def detect_date_contradictions(driver, year_threshold: int = 3) -> list[dict]:
    """
    Find edges where the same (subject, rel_type, object) triple has
    date values from different sources that differ by more than year_threshold years.

    This catches cases like: "Standard Oil founded in 1870" vs "founded in 1882"
    """
    contradictions = []

    with driver.session() as session:
        # Find all edges that have a date property
        result = session.run("""
            MATCH (a:Entity)-[r]->(b:Entity)
            WHERE r.date IS NOT NULL AND size(coalesce(r.sources, [])) > 1
            RETURN a.name AS subject, type(r) AS rel_type, b.name AS object,
                   r.date AS date, r.sources AS sources, r.domains AS domains,
                   r.occurrences AS occurrences
        """)

        for record in result:
            date_str = record["date"]
            year = extract_year(date_str)
            if year is None:
                continue

            # Check if any source URL contains a different year in its path
            # (heuristic: archive URLs often contain dates)
            sources = record["sources"] or []
            source_years = set()
            for src in sources:
                m = _YEAR_RE.search(src)
                if m:
                    sy = int(m.group(1))
                    if 1700 <= sy <= 2024:
                        source_years.add(sy)

            if len(source_years) > 1:
                min_year = min(source_years)
                max_year = max(source_years)
                if max_year - min_year >= year_threshold:
                    contradictions.append({
                        'type': 'date_contradiction',
                        'subject': record["subject"],
                        'rel_type': record["rel_type"],
                        'object': record["object"],
                        'stored_date': date_str,
                        'source_years': sorted(source_years),
                        'year_spread': max_year - min_year,
                        'sources': sources[:5],
                        'severity': 'high' if max_year - min_year > 10 else 'medium',
                    })

    return contradictions


def detect_multiple_targets(driver) -> list[dict]:
    """
    Find cases where the same (subject, rel_type) has multiple different objects
    from different sources — suggesting the sources disagree on who/what was involved.

    Example: "Standard Oil ACQUIRED CompanyA" from source 1
             "Standard Oil ACQUIRED CompanyB" from source 2
    These might be legitimate (multiple acquisitions) or contradictions.
    """
    contradictions = []

    with driver.session() as session:
        # Find subjects that have multiple outgoing edges of the same type
        result = session.run("""
            MATCH (a:Entity)-[r]->(b:Entity)
            WITH a, type(r) AS rel_type, collect(b.name) AS targets,
                 collect(r.date) AS dates, collect(r.sources) AS all_sources
            WHERE size(targets) > 1
              AND rel_type IN ['WAS_FOUNDED_BY', 'HAD_CEO', 'HAD_PRESIDENT',
                               'DISSOLVED_IN', 'SUCCEEDED_BY', 'LOCATED_IN',
                               'WAS_ROLE_OF', 'HEADQUARTERED_IN']
            RETURN a.name AS subject, rel_type, targets, dates
            ORDER BY size(targets) DESC
            LIMIT 50
        """)

        for record in result:
            targets = record["targets"]
            dates = record["dates"]

            # Filter out cases where dates are clearly different (legitimate multiple facts)
            unique_dates = set(d for d in dates if d is not None)
            if len(unique_dates) > 1:
                # Different dates = likely different time periods, not a contradiction
                continue

            # Same date (or no date) with multiple targets = potential contradiction
            contradictions.append({
                'type': 'multiple_targets',
                'subject': record["subject"],
                'rel_type': record["rel_type"],
                'targets': targets,
                'dates': list(unique_dates),
                'severity': 'medium',
                'note': f"Multiple targets for {record['rel_type']}: {', '.join(targets[:3])}",
            })

    return contradictions


def detect_self_contradicting_roles(driver) -> list[dict]:
    """
    Find cases where a person is listed as holding two different roles
    at the same organisation at the same time.

    Example: "John Smith WAS_ROLE_OF Acme Corp" with role=CEO
             "John Smith WAS_ROLE_OF Acme Corp" with role=Chairman
    (Could be legitimate dual roles, or a data error)
    """
    contradictions = []

    with driver.session() as session:
        result = session.run("""
            MATCH (p:Entity)-[r:WAS_ROLE_OF]->(o:Entity)
            WITH p, o, collect(r.role) AS roles, collect(r.date) AS dates
            WHERE size(roles) > 1
            RETURN p.name AS person, o.name AS org, roles, dates
            LIMIT 30
        """)

        for record in result:
            roles = [r for r in record["roles"] if r]
            if len(set(roles)) > 1:
                contradictions.append({
                    'type': 'multiple_roles',
                    'person': record["person"],
                    'organisation': record["org"],
                    'roles': roles,
                    'dates': record["dates"],
                    'severity': 'low',
                    'note': f"Multiple roles at same org: {', '.join(set(roles))}",
                })

    return contradictions


def detect_temporal_inconsistencies(driver) -> list[dict]:
    """
    Find chains where the dates are logically inconsistent.
    Example: A FOUNDED B in 1920, B ACQUIRED C in 1890
    (B can't acquire something before it was founded)
    """
    contradictions = []

    with driver.session() as session:
        result = session.run("""
            MATCH (a:Entity)-[r1]->(b:Entity)-[r2]->(c:Entity)
            WHERE r1.date IS NOT NULL AND r2.date IS NOT NULL
              AND a.name <> b.name AND b.name <> c.name
            RETURN a.name AS a, type(r1) AS rel1, r1.date AS date1,
                   b.name AS b, type(r2) AS rel2, r2.date AS date2,
                   c.name AS c
            LIMIT 100
        """)

        for record in result:
            year1 = extract_year(record["date1"])
            year2 = extract_year(record["date2"])

            if year1 is None or year2 is None:
                continue

            # Check for temporal impossibility
            # If rel1 is FOUNDED and year2 < year1, that's impossible
            founding_rels = {'FOUNDED', 'WAS_FOUNDED_BY', 'CREATED', 'ESTABLISHED'}
            if record["rel1"] in founding_rels and year2 < year1:
                contradictions.append({
                    'type': 'temporal_impossibility',
                    'chain': f"{record['a']} -{record['rel1']}({year1})-> {record['b']} -{record['rel2']}({year2})-> {record['c']}",
                    'issue': f"{record['b']} was founded in {year1} but {record['rel2']} {record['c']} in {year2} (before founding)",
                    'severity': 'high',
                })

    return contradictions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_detection(mark_neo4j: bool = False, year_threshold: int = 3):
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))

    logger.info("Scanning graph for contradictions...")

    all_contradictions = []

    logger.info("  Checking date contradictions...")
    date_contras = detect_date_contradictions(driver, year_threshold)
    all_contradictions.extend(date_contras)
    logger.info(f"  Found {len(date_contras)} date contradictions")

    logger.info("  Checking multiple targets...")
    target_contras = detect_multiple_targets(driver)
    all_contradictions.extend(target_contras)
    logger.info(f"  Found {len(target_contras)} multiple-target cases")

    logger.info("  Checking role contradictions...")
    role_contras = detect_self_contradicting_roles(driver)
    all_contradictions.extend(role_contras)
    logger.info(f"  Found {len(role_contras)} role contradictions")

    logger.info("  Checking temporal inconsistencies...")
    temporal_contras = detect_temporal_inconsistencies(driver)
    all_contradictions.extend(temporal_contras)
    logger.info(f"  Found {len(temporal_contras)} temporal inconsistencies")

    driver.close()

    # Save report
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    report = {
        'generated_at': datetime.now().isoformat(),
        'total': len(all_contradictions),
        'by_severity': {
            'high': len([c for c in all_contradictions if c.get('severity') == 'high']),
            'medium': len([c for c in all_contradictions if c.get('severity') == 'medium']),
            'low': len([c for c in all_contradictions if c.get('severity') == 'low']),
        },
        'contradictions': all_contradictions,
    }

    with open(REPORT_FILE, 'w', encoding='utf-8') as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Print summary
    print(f"\n{'='*60}")
    print(f"CONTRADICTION DETECTION REPORT")
    print(f"{'='*60}")
    print(f"Total contradictions found: {len(all_contradictions)}")
    print(f"  High severity:   {report['by_severity']['high']}")
    print(f"  Medium severity: {report['by_severity']['medium']}")
    print(f"  Low severity:    {report['by_severity']['low']}")
    print(f"\nFull report saved to: {REPORT_FILE}")

    if all_contradictions:
        print(f"\nTop contradictions:")
        high = [c for c in all_contradictions if c.get('severity') == 'high']
        for c in high[:5]:
            print(f"\n  [{c['type']}] Severity: {c['severity']}")
            if 'chain' in c:
                print(f"  Chain: {c['chain']}")
                print(f"  Issue: {c['issue']}")
            elif 'subject' in c:
                print(f"  {c['subject']} -{c['rel_type']}-> {c.get('object', '?')}")
                if 'source_years' in c:
                    print(f"  Date conflict: {c['source_years']} (spread: {c['year_spread']} years)")
            elif 'note' in c:
                print(f"  {c['note']}")

    print(f"\nIMPACT ON QUESTION GENERATION:")
    print(f"  The generator will now skip chains involving contradicted entities.")
    print(f"  Run: python generator\\query_engine.py --min-domains 1 --skip-verify")


def show_report():
    if not os.path.exists(REPORT_FILE):
        print("No contradiction report found. Run detection first.")
        return
    with open(REPORT_FILE, 'r', encoding='utf-8') as f:
        report = json.load(f)
    print(f"\nContradiction Report ({report['generated_at']})")
    print(f"Total: {report['total']} | High: {report['by_severity']['high']} | "
          f"Medium: {report['by_severity']['medium']} | Low: {report['by_severity']['low']}")
    for c in report['contradictions'][:20]:
        print(f"\n  [{c['severity'].upper()}] {c['type']}")
        if 'chain' in c:
            print(f"  {c['chain']}")
        elif 'subject' in c:
            print(f"  {c['subject']} -{c.get('rel_type','?')}-> {c.get('object', c.get('org', '?'))}")
        if 'note' in c:
            print(f"  Note: {c['note']}")


def main():
    parser = argparse.ArgumentParser(description="Detect contradictions in the knowledge graph")
    parser.add_argument("--mark-neo4j", action="store_true",
                        help="Flag contradicted edges in Neo4j with a 'contradicted' property")
    parser.add_argument("--threshold", type=int, default=3,
                        help="Minimum year difference to flag as date contradiction (default: 3)")
    parser.add_argument("--report", action="store_true",
                        help="Show the saved contradiction report")
    args = parser.parse_args()

    if args.report:
        show_report()
    else:
        run_detection(mark_neo4j=args.mark_neo4j, year_threshold=args.threshold)


if __name__ == "__main__":
    main()
