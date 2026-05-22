# How to Run DeepQuest

DeepQuest is designed to run continuously in the background. It consists of three main parts:
1. **The Infrastructure** (Databases running in Docker)
2. **The Background Workers** (Crawler and Extractor — run continuously)
3. **The Seeders** (Run on demand to inject known historical data)
4. **The Question Generator** (Run on demand)

> **Note on `verifier/worker.py`:** You do NOT run this manually. It is a library called automatically by the generator when you run `python generator\query_engine.py`. No separate terminal needed.

If your PC turns off or you need to restart the session, follow these steps to get everything running again.

---

### Step 1: Start the Databases (Docker)
Your databases (PostgreSQL, Redis, Neo4j) are managed by Docker.
If your PC restarts, open a PowerShell terminal in the `DeepQuest` folder and run:
```powershell
docker-compose up -d
```
*Note: If Docker Desktop is set to start on boot, the containers might already be running. Open Docker Desktop to check.*

### Step 2: Activate the Python Environment
Open a new PowerShell terminal in the `DeepQuest` folder and activate the virtual environment:
```powershell
.\venv\Scripts\activate
```

### Step 3: Start the Crawler (Runs Continuously)
In that same terminal, start the crawler. This continuously crawls the web, finding new pages and saving them to the database.
```powershell
python crawler\worker.py
```
*(Leave this terminal window open so it can keep running.)*

### Step 4: Start the Extractor (Runs Continuously)
Open **another** new PowerShell terminal, activate the environment, and start the extractor. This continuously reads downloaded pages, extracts relationships, and pushes them to the Neo4j graph.
```powershell
.\venv\Scripts\activate
python extractor\worker.py
```
*(Leave this terminal window open so it can keep running.)*

You can run multiple extractor instances in parallel for faster processing:
```powershell
# Terminal 2
python extractor\worker.py
# Terminal 3
python extractor\worker.py
```

---

## Seeding the Graph (Run on Demand)

The seeders inject known historical data directly into Neo4j without waiting for the crawler. Run these to bootstrap the graph quickly.

### Inject Wikipedia Articles
Fetches 80+ Wikipedia articles about historical topics and extracts triples:
```powershell
python seeder\inject_wikipedia.py
```

### Inject Wikipedia Infoboxes (High Precision)
Extracts structured infobox data (founded dates, CEOs, successors, etc.) directly into Neo4j edges — no NLP, zero noise:
```powershell
python seeder\inject_infoboxes.py
```

### Inject Historical Corpus (Chronicling America, Internet Archive, Open Library)
Searches historical archive APIs directly for newspaper articles, books, and documents about specific topics. This is the best source for deep pre-1950 historical facts:
```powershell
# Inject from all sources using default historical queries:
python seeder\inject_historical_corpus.py

# Use a specific source only:
python seeder\inject_historical_corpus.py --source chronicling
python seeder\inject_historical_corpus.py --source archive

# Custom query:
python seeder\inject_historical_corpus.py --query "standard oil rockefeller 1882"
```
Fetches each topic from Wikipedia, DBpedia, Britannica, Wikiwand, Archive.org, and more simultaneously, so each fact accumulates multiple source domains:
```powershell
python seeder\inject_multisource.py
```

### Detect Contradictions in the Graph
Scans the knowledge graph for facts where different sources disagree (date conflicts, multiple targets for the same relationship, temporal impossibilities). Contradicted facts are excluded from question generation automatically.

```powershell
# Run contradiction detection:
python seeder\detect_contradictions.py

# Show saved report:
python seeder\detect_contradictions.py --report

# Stricter threshold (flag if years differ by 1+):
python seeder\detect_contradictions.py --threshold 1
```

Results are saved to `evaluator/contradictions.json`.


Merges entity name variants ("SUPREME COURT", "THE SUPREME COURT", "U.S. SUPREME COURT" → one node) and consolidates parallel edges to increase domain counts:
```powershell
# Preview what would be merged:
python seeder\merge_entities.py --dry-run

# Apply the merges:
python seeder\merge_entities.py
```

**Recommended seeding workflow:**
```powershell
# Edit seeder/topics.txt first (one Wikipedia title per line, any subject)
python seeder\inject_infoboxes.py
python seeder\inject_wikipedia.py --topics-file seeder\topics.txt
python seeder\inject_historical_corpus.py
python seeder\inject_multisource.py --topics-file seeder\topics.txt
python seeder\enrich_sources.py          # find 6+ sources per edge
python seeder\merge_entities.py
```

### Topics file (`seeder/topics.txt`)

- One topic per line; lines starting with `#` are comments.
- Use normal English (`Los Angeles Lakers`) or underscores (`Los_Angeles_Lakers`).
- Inline notes: `CRISPR  # gene editing`
- If the file exists, seeders use it automatically when you omit `--topics`.
- Override: `--topics "Tesla, Inc." "Python"` or `--topics-file my_topics.txt`

---

### Step 5: Generate Questions (Run on Demand)
Whenever you want to generate adversarial questions, open a **third** PowerShell terminal, activate the environment, and run the generator:
```powershell
.\venv\Scripts\activate
python generator\query_engine.py
```

**Testing mode** (relaxed thresholds, no live URL verification — use while graph is still sparse):
```powershell
python generator\query_engine.py --min-domains 1 --min-sources 1 --skip-verify
```

**Production mode** (6 unique URLs = 6 different websites, live verification optional):
```powershell
python generator\query_engine.py --min-domains 6 --min-sources 6 --skip-verify
```

**Production + live URL check** (slower; many archive URLs fail fetch — use after graph is rich):
```powershell
python generator\query_engine.py --min-domains 6 --min-sources 6
```

**Auto-enrich weak chains** (generator calls enrich_sources on each chain before rejecting):
```powershell
python generator\query_engine.py --min-sources 6 --skip-verify --auto-enrich
```

**Historical questions only** (pre-2000 events):
```powershell
python generator\query_engine.py --min-year 2000 --min-domains 3 --min-sources 3 --skip-verify
```

The generator will query the Neo4j graph for multi-hop chains, build narrative prompts, optionally verify sources, and write full-format `.txt` files to `question_generated/`.

**The Source Verifier (`verifier/worker.py`) is called automatically by the generator — you never run it manually.**

---

## Why you are not getting 6 URLs yet

A published question needs **6 different netlocs** (e.g. `en.wikipedia.org`, `dbpedia.org`, `archive.org` — not six pages on Wikipedia).

### Root causes (most common first)

1. **Edges only have 1–2 domains** — Crawler + extractor add one URL per relationship. Multi-hop chains merge hop1 + hop2; if each hop has 2 domains you only have **4** unique sites, not 6.
2. **`enrich_sources.py` was not run** — This is the main tool that hunts for more URLs for the *same* `(A)-[REL]->(B)` edge. The scheduler now runs it; manual runs still help.
3. **`inject_multisource` does not guarantee one edge** — It fetches many sites per *topic*, but spaCy may extract *different* triples per site, so domains never stack on one edge.
4. **Live verification drops URLs** — Without `--skip-verify`, dead links, PDFs, and paywalls fail the fetch/keyword check even when the graph lists 6 URLs.
5. **Old bug (fixed)** — The generator counted concatenated domain *list length* (double-counting). It now counts **unique netlocs from source URLs**.

### Fix pipeline (run on Docker desktop, any domain — not only history)

```powershell
docker-compose up -d
.\venv\Scripts\activate

# 1) Bootstrap graph — edit seeder/topics.txt (one topic per line, any domain)
python seeder\inject_multisource.py --limit 30
python seeder\inject_wikipedia.py --limit 30
python seeder\merge_entities.py

# 2) Attach more URLs to existing edges (critical)
python seeder\enrich_sources.py --limit 300

# 3) Check readiness
python check_graph.py

# 4) Generate (graph-only gate first)
python generator\query_engine.py --min-domains 6 --min-sources 6 --skip-verify

# 5) If still sparse, let generator enrich per chain
python generator\query_engine.py --min-sources 6 --skip-verify --auto-enrich
```

### Health check

```powershell
python check_graph.py
```

Look for **Max domains on any edge: 6+** and **Chains with 6+ domains**. If max is 2–3, generation will stay at zero until `enrich_sources` runs.

---

## Automated Scheduler

Instead of running seeders and the generator manually, the scheduler does it automatically on a timer.

```powershell
# Run the full pipeline every 6 hours (default):
python scheduler.py

# Run every 2 hours:
python scheduler.py --interval 2

# Run once and exit (useful for testing):
python scheduler.py --once

# Only generate questions, skip seeding (when graph is already populated):
python scheduler.py --skip-inject --interval 1
```

The scheduler runs these steps in order each cycle:
1. Inject infoboxes
2. Inject Wikipedia articles
3. Inject historical corpus
4. Merge entities
5. Detect contradictions
6. Generate questions
7. Export results

Logs are saved to `scheduler.log`.

---

A browser-based dashboard that shows system status and lets you run seeders and the generator without typing commands.

**Install Flask first (one time):**
```powershell
pip install flask
```

**Start the dashboard:**
```powershell
python dashboard\app.py
```

Then open **http://localhost:5000** in your browser.

The dashboard shows:
- Graph node and relationship counts
- Max source domains on any edge (need 6+ for production)
- Pages crawled and pending extraction
- Questions generated
- Top graph chains available
- Recent generated questions
- Buttons to run all seeders and the generator

---

Export your generated questions to formats compatible with research tools and the HuggingFace ecosystem:

```powershell
# Export all formats at once:
python evaluator\export.py

# Export specific format:
python evaluator\export.py --format json
python evaluator\export.py --format csv
python evaluator\export.py --format huggingface   # JSONL for datasets library
python evaluator\export.py --format squad         # SQuAD v2 format
python evaluator\export.py --format hotpotqa      # HotpotQA format
python evaluator\export.py --format markdown      # Readable document
```

Exports are saved to the `exports/` folder with a timestamp in the filename.

To load in Python with HuggingFace datasets:
```python
from datasets import load_dataset
ds = load_dataset('json', data_files='exports/deepquest_YYYYMMDD_HHMMSS_hf.jsonl')
```

---

## Evaluating Question Difficulty (Benchmark Scoring)

After generating questions, you can automatically test them against AI systems to measure how hard they are. Questions where AI fails are your most valuable benchmark items.

```powershell
# Evaluate all questions in question_generated/:
python evaluator\benchmark.py

# Evaluate a single question:
python evaluator\benchmark.py --file question_generated\question_xyz.txt

# Show a summary report of all scored questions:
python evaluator\benchmark.py --report
```

The evaluator tests each question against DuckDuckGo Instant Answers and Wikipedia search, then assigns a difficulty score:
- **Score 1.0** — AI fails completely (excellent benchmark item)
- **Score 0.5** — AI partially correct (good benchmark item)
- **Score 0.0** — AI answers correctly (too easy, not useful)

Results are saved to `evaluator/benchmark_results.jsonl`.

---
To see how many nodes and relationships are in the graph:
```powershell
docker exec deepquest_neo4j cypher-shell -u neo4j -p deepquestpassword "MATCH (n) RETURN count(n) AS nodes;"
docker exec deepquest_neo4j cypher-shell -u neo4j -p deepquestpassword "MATCH ()-[r]->() RETURN count(r) AS rels, max(size(coalesce(r.domains,[]))) AS max_domains;"
```

To see the best chains available:
```powershell
docker exec deepquest_neo4j cypher-shell -u neo4j -p deepquestpassword "MATCH (a)-[r1]->(b)-[r2]->(c) RETURN a.name, type(r1), b.name, type(r2), c.name, size(coalesce(r1.domains,[])) + size(coalesce(r2.domains,[])) AS domains ORDER BY domains DESC LIMIT 10;"
```

---

## Checking Graph Health

Run the graph health monitor for a full picture of what's in your graph and what to do next:

```powershell
python check_graph.py              # summary report with recommendations
python check_graph.py --verbose    # show top chains in detail
python check_graph.py --domains    # show which source domains are contributing
```

This tells you exactly which generator command to run based on your current graph density.

---

## Expanding the Deep Search
The crawler will follow links endlessly, digging deeper into the internet. The longer you leave it running, the more cross-domain evidence accumulates for each fact. The seeders give you an immediate boost while the crawler builds up long-term depth.

---

## Wiping the Slate Clean
If you ever want to completely reset the system:

```powershell
docker exec deepquest_postgres psql -U deepquest -d deepquestdb -c "TRUNCATE TABLE pages RESTART IDENTITY;"
docker exec deepquest_redis redis-cli FLUSHALL
docker exec deepquest_neo4j cypher-shell -u neo4j -p deepquestpassword "MATCH (n) DETACH DELETE n;"
Remove-Item -Force verifier\verification.log -ErrorAction SilentlyContinue
```

*(After wiping, close and restart the Crawler and Extractor Python scripts so they pick up the fresh queue.)*

---

## Schema Migration
If you already have the database running with an older schema, add the new columns:

```powershell
docker exec deepquest_postgres psql -U deepquest -d deepquestdb -c "ALTER TABLE pages ADD COLUMN IF NOT EXISTS content_hash TEXT UNIQUE; ALTER TABLE pages ADD COLUMN IF NOT EXISTS content_type TEXT DEFAULT 'html'; ALTER TABLE pages ADD COLUMN IF NOT EXISTS final_url TEXT; CREATE INDEX IF NOT EXISTS idx_pages_content_hash ON pages(content_hash);"
```
