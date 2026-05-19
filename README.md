# DeepQuest

> **A deterministic adversarial historical QA generation engine**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python)](https://python.org)
[![Neo4j 5](https://img.shields.io/badge/Neo4j-5-green?style=flat-square&logo=neo4j)](https://neo4j.com)
[![PostgreSQL 15](https://img.shields.io/badge/PostgreSQL-15-blue?style=flat-square&logo=postgresql)](https://postgresql.org)
[![Redis 7](https://img.shields.io/badge/Redis-7-red?style=flat-square&logo=redis)](https://redis.io)
[![Docker](https://img.shields.io/badge/Docker-Compose-blue?style=flat-square&logo=docker)](https://docker.com)
[![License: All Rights Reserved](https://img.shields.io/badge/License-All%20Rights%20Reserved-red?style=flat-square)](./LICENSE)

**Author:** Adedamola Adediran  
**LinkedIn:** [linkedin.com/in/adediranadedamola](https://linkedin.com/in/adediranadedamola)  
**GitHub:** [@goodyness](https://github.com/goodyness)

---

## What is DeepQuest?

DeepQuest is a fully deterministic research and relationship-mining engine that crawls the deep web, builds a knowledge graph from extracted facts, and automatically generates adversarial multi-hop QA pairs — all without using any large language model.

It is designed to produce benchmark questions that are:

- **Uniquely answerable** — one correct answer, no ambiguity
- **Multi-hop** — require chaining 2+ facts across independent sources
- **Source-verified** — every fact must be corroborated by 6+ independent domains
- **Hallucination-proof** — no generative AI involved at any stage

The output is a structured QA dataset purpose-built to challenge and expose weaknesses in AI retrieval systems, RAG pipelines, and search engines.

---

## Why DeepQuest is Different

| Feature | DeepQuest | LLM-based generators |
|---|---|---|
| Uses generative AI | ✗ Never | ✓ Always |
| Deterministic output | ✓ Yes | ✗ No |
| Live web crawling | ✓ Yes | ✗ No |
| Source verification | ✓ 6+ domains required | ✗ Unverified |
| Hallucination risk | ✗ None | ✓ High |
| Multi-hop reasoning | ✓ Graph-traversal based | ✓ Probabilistic |
| Reproducible answers | ✓ Yes | ✗ No |

DeepQuest replaces probabilistic text generation with graph traversal, NLP extraction, and strict source consensus. A fact only enters the system if multiple independent sources agree on it.

---

## How It Works

DeepQuest runs as a 6-step engine cycle:

1. **Deep Crawling** — Recursively downloads pages from historical archives, regulatory filings, regional journalism, and other non-mainstream sources using async HTTP.
2. **Structured Extraction** — Parses each page with spaCy to extract Subject-Verb-Object triples deterministically (e.g. `StartupX → ACQUIRED → Firm Y`).
3. **Knowledge Graph** — Pushes extracted triples into Neo4j. When multiple sources report the same relationship, the edge's domain count increases.
4. **Pattern Detection** — Traverses the graph to find rare multi-hop chains that standard search engines miss.
5. **Cross-Validation** — Checks timeline consistency and flags contradictions between sources. Contradicted facts are excluded from generation.
6. **Template Generation** — Builds adversarial QA prompts from verified graph chains using rule-based templates — no LLM involved.

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| NLP extraction | spaCy (`en_core_web_lg`) |
| Knowledge graph | Neo4j 5 |
| Raw page storage | PostgreSQL 15 |
| URL frontier queue | Redis 7 |
| Async HTTP crawling | httpx |
| HTML content extraction | trafilatura |
| Web dashboard | Flask |
| Infrastructure | Docker / docker-compose |

---

## Project Structure

```
DeepQuest/
├── crawler/          # Async web crawler — downloads pages into PostgreSQL
├── extractor/        # spaCy NLP worker — extracts SVO triples into Neo4j
├── generator/        # Graph traversal engine — builds adversarial QA prompts
├── verifier/         # Source verifier — called automatically by the generator
├── seeder/           # On-demand data injection scripts
├── evaluator/        # Benchmark scoring and multi-format export
├── dashboard/        # Flask web dashboard for monitoring and control
├── graph/            # Neo4j schema definitions
├── db/               # PostgreSQL init SQL
├── exports/          # Generated exports (JSON, CSV, HuggingFace JSONL, etc.)
└── question_generated/  # Raw generated question files
```

---

## Quick Start

### Prerequisites

- Docker Desktop installed and running
- Python 3.11+
- Git

### 1. Clone the repository

```bash
git clone https://github.com/goodyness/DeepQuest.git
cd DeepQuest
```

### 2. Start the infrastructure

```powershell
docker-compose up -d
```

This starts PostgreSQL, Redis, and Neo4j in the background.

### 3. Install dependencies

```powershell
pip install -r requirements.txt
python -m spacy download en_core_web_lg
```

### 4. Activate the virtual environment

```powershell
.\venv\Scripts\activate
```

### 5. Start the crawler (runs continuously)

```powershell
python crawler\worker.py
```

### 6. Start the extractor (runs continuously, in a separate terminal)

```powershell
python extractor\worker.py
```

You can run multiple extractor instances in parallel for faster graph population.

### 7. Seed the knowledge graph

Run the seeders to bootstrap the graph with historical data immediately, without waiting for the crawler:

```powershell
python seeder\inject_infoboxes.py
python seeder\inject_wikipedia.py
python seeder\inject_historical_corpus.py
python seeder\inject_multisource.py
python seeder\enrich_sources.py      # finds 6+ sources per edge
python seeder\merge_entities.py
```

### 8. Generate questions

```powershell
# Testing mode (relaxed thresholds, no live URL verification):
python generator\query_engine.py --min-domains 1 --min-sources 1 --skip-verify

# Production mode (full 6-source gate with live verification):
python generator\query_engine.py --min-domains 6 --min-sources 6
```

Generated questions are written to `question_generated/` as `.txt` files.

---

## Seeder Tools

The seeders inject known historical data directly into Neo4j without waiting for the crawler. Each targets a different source type.

| Script | Description |
|---|---|
| `inject_wikipedia.py` | Fetches 80+ Wikipedia articles on historical topics and extracts NLP triples |
| `inject_infoboxes.py` | Extracts structured infobox data (dates, roles, successors) directly — no NLP, zero noise |
| `inject_historical_corpus.py` | Searches Chronicling America, Internet Archive, and Open Library APIs for pre-1950 historical facts |
| `inject_multisource.py` | Fetches each topic from Wikipedia, DBpedia, Britannica, Wikiwand, and Archive.org simultaneously to accumulate multi-domain evidence |
| `enrich_sources.py` | **The key to 6+ sources.** Takes existing graph edges with fewer than 6 source domains and actively searches Wikipedia, DBpedia, Archive.org, Open Library, Chronicling America, and Wikiwand for additional URLs confirming the same fact. Run this after all other seeders. |
| `merge_entities.py` | Merges entity name variants (e.g. "SUPREME COURT", "U.S. SUPREME COURT" → one node) and consolidates parallel edges |
| `detect_contradictions.py` | Scans the graph for facts where sources disagree; contradicted facts are excluded from generation automatically |

---

## Output Format

Each generated question is a structured `.txt` file with six sections:

```
PROMPT:
Historical records indicate that [Entity A] held a role at [Organisation].
This same organisation subsequently [Action] [Entity C]. Using at least six
independent sources, identify the organisation that connects these two events.

SOURCES:
1. https://source-domain-1.org/path/to/evidence
2. https://source-domain-2.org/path/to/evidence
3. https://source-domain-3.org/path/to/evidence
...

ANSWER:
[UNIQUE CORRECT ANSWER]

EXPLANATION:
The answer is [X]. Multiple independent sources confirm the following chain
of facts: [Entity A] → [Relationship 1] → [Organisation] → [Relationship 2]
→ [Entity C]. Each cited source independently corroborates at least one link
in this chain, establishing [X] as the unique entity satisfying both conditions.

FACT_FANOUT:
1. [Entity A] held a role at [Organisation] ([source 1])
2. [Organisation] is a [type] ([source 2])
3. [Organisation] [Action] [Entity C]
4. [Entity A] and [Entity C] are connected through [Organisation]
5. The relationship is documented in multiple independent sources

SEARCH_TRAJECTORY:
1. Search for '[Entity A] held a role at' to identify the connecting organisation.
2. Cross-reference results with historical archives (Internet Archive, Chronicling
   America, HathiTrust) to find primary sources.
3. For each candidate, verify whether it subsequently [Action] [Entity C].
4. Confirm the answer using at least 6 independent sources from different domains.
5. Check that no other entity satisfies both conditions to ensure uniqueness.
```

Questions can be exported to multiple formats via `python evaluator\export.py`:

- JSON, CSV
- HuggingFace JSONL (compatible with `datasets` library)
- SQuAD v2 format
- HotpotQA format
- Markdown document

---

## Dashboard

A browser-based dashboard provides real-time system monitoring and one-click controls.

```powershell
python dashboard\app.py
```

Open **http://localhost:5000** to view graph stats, crawl progress, question counts, and run seeders or the generator without typing commands.

---

## Automated Scheduler

The scheduler runs the full pipeline automatically on a timer:

```powershell
python scheduler.py                    # every 6 hours (default)
python scheduler.py --interval 2       # every 2 hours
python scheduler.py --once             # run once and exit
python scheduler.py --skip-inject --interval 1  # generate only
```

Logs are saved to `scheduler.log`.

---

## License

All Rights Reserved. See [LICENSE](./LICENSE) file.

This software may not be used, copied, modified, or distributed without explicit written permission from the author.

---

## Contact

**Adedamola Adediran**  
LinkedIn: [linkedin.com/in/adediranadedamola](https://linkedin.com/in/adediranadedamola)  
GitHub: [@goodyness](https://github.com/goodyness)
