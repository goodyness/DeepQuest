# DeepQuest V2 — Upgrade Roadmap

## What DeepQuest V1 Does (Current State)

A deterministic pipeline: crawl → NLP extract → Neo4j graph → generate questions.
No LLMs. Rule-based throughout. Produces multi-hop adversarial QA with 6+ verified sources.

**Current bottlenecks:**
- `en_core_web_sm` is too small and slow for historical text
- Extraction yield is ~0.04 triples/page (too low)
- 8-domain threshold means weeks of crawling before first question
- No feedback loop — bad pages keep getting crawled

---

## Similar Projects on GitHub / Research

### Closest to DeepQuest's goal:

| Project | What it does | Gap vs DeepQuest |
|---|---|---|
| **HotpotQA** ([github](https://github.com/hotpotqa/hotpotqa)) | 113k Wikipedia multi-hop QA pairs | Uses LLMs, Wikipedia only, no web crawl |
| **2WikiMultiHopQA** ([github](https://github.com/Alab-NII/2wikimultihop)) | Multi-hop QA from 2 Wikipedia articles | Static dataset, no live crawling |
| **MuSiQue** ([huggingface](https://huggingface.co/datasets/corag/multihopqa/viewer/musique)) | 22k multi-hop questions requiring 2-4 hops | LLM-generated, not deterministic |
| **MultiHop-RAG** ([github](https://github.com/yixuantt/MultiHop-RAG)) | RAG evaluation across documents | Evaluates retrieval, doesn't generate questions |
| **llm-qa-dataset-pipeline** ([github](https://github.com/gokhaneraslan/llm-qa-dataset-pipeline)) | Web crawl → LLM → QA dataset | Uses LLMs (Groq/Mistral), not deterministic |
| **Humanity's Last Exam** ([github](https://github.com/centerforaisafety/hle)) | Expert-level adversarial questions | Human-curated, not automated |

**Key finding:** No existing project combines (1) live web crawling, (2) deterministic NLP extraction, (3) knowledge graph storage, and (4) automated adversarial question generation with source verification. DeepQuest is genuinely novel in this combination.

The closest academic work is the **KGQA** family (HotpotQA, 2WikiMultiHop) but they all use static Wikipedia snapshots and LLMs. DeepQuest's deterministic + live-crawl approach is unique.

---

## Upgrade Roadmap

Upgrades are grouped by impact and effort. Start with **Tier 1** — these fix the core bottleneck.

---

### TIER 1 — Critical (Fix the extraction yield problem)

#### U1. Upgrade spaCy model: `en_core_web_sm` → `en_core_web_lg`
**Problem:** `en_core_web_sm` misses ~40% of named entities in historical text.
**Fix:** Switch to `en_core_web_lg` (GloVe vectors, much better NER accuracy).
**Cost:** ~700MB download, ~2x slower but still CPU-friendly.
**Impact:** Extraction yield likely doubles or triples immediately.
```powershell
python -m spacy download en_core_web_lg
```
Then change one line in `extractor/worker.py`:
```python
nlp = spacy.load("en_core_web_lg")
```

#### U2. Targeted seed injection — bypass the crawl wait
**Problem:** The crawler needs weeks to accumulate 8-domain chains organically.
**Fix:** Write a `seeder/inject_wikipedia.py` script that directly fetches
Wikipedia articles about known historical topics (Standard Oil, French Revolution,
Industrial Revolution, etc.), extracts triples, and injects them into Neo4j
with their Wikipedia URL as the source. This bypasses the crawler entirely for
known high-value content.
**Impact:** Could populate the graph with thousands of triples in hours, not weeks.

#### U3. Lower the generator's pre-filter threshold temporarily
**Problem:** `min_domains=8` means zero results until the graph is very dense.
**Fix:** Add a `--min-domains` CLI argument to the generator. Start at 2 to
verify the pipeline works end-to-end, then raise it as the graph grows.
**Impact:** Immediate — you'd see your first generated questions today.

#### U4. Fix the "skipping processed update" loop
**Problem:** Pages with empty content loop forever because they're never marked
processed. Already partially fixed but needs to be airtight.
**Fix:** Any page that reaches the extractor and produces empty content after
cleaning should ALWAYS be marked `processed=TRUE`, no exceptions.
**Impact:** Stops the terminal flooding and wasted CPU cycles.

---

### TIER 2 — High Impact (Better data quality)

#### U5. Wikipedia API direct ingestion
**Problem:** The crawler fetches Wikipedia HTML which trafilatura then strips.
A lot of structured data (infoboxes, dates, relationships) is lost.
**Fix:** Use the Wikipedia API (`https://en.wikipedia.org/api/rest_v1/`) to
fetch clean article text directly, bypassing HTML parsing entirely.
**Impact:** Much cleaner text, better NER results, faster processing.

#### U6. Wikidata integration for entity disambiguation
**Problem:** "JOHN SMITH" in one source and "J. SMITH" in another are stored
as different entities in Neo4j, fragmenting the graph.
**Fix:** After extraction, run entity names through the Wikidata API to get
canonical IDs. Merge entities that resolve to the same Wikidata QID.
**Impact:** Graph density increases dramatically without more crawling.
**Reference:** Wikidata API: `https://www.wikidata.org/w/api.php`

#### U7. Temporal chain scoring improvement
**Problem:** The generator currently scores chains by the earliest date found.
But a chain like `(1850 event) → (1920 event)` is more interesting than
`(1850 event) → (1851 event)`.
**Fix:** Score chains by the *span* between dates (longer span = more obscure
connection) in addition to historical depth.

#### U8. Structured data extraction from Wikipedia infoboxes
**Problem:** Wikipedia infoboxes contain exactly the kind of structured facts
DeepQuest needs (birth dates, positions held, company founded, etc.) but the
current extractor ignores them.
**Fix:** Add an infobox parser that extracts key-value pairs from Wikipedia
infoboxes and converts them directly to Neo4j edges without NLP.
**Impact:** High precision, zero NLP errors for structured facts.

---

### TIER 3 — Architecture (Bigger changes, bigger payoff)

#### U9. Replace spaCy with `en_core_web_trf` (transformer model)
**Problem:** `en_core_web_sm/lg` use statistical models. The transformer model
(`en_core_web_trf`, based on RoBERTa) has ~15% better NER accuracy.
**Cost:** Requires GPU or is ~10x slower on CPU. Not practical for continuous
extraction on a laptop.
**When to do:** When you have a machine with a GPU, or use a cloud VM for
extraction runs.

#### U10. Parallel extraction workers
**Problem:** The extractor is single-threaded per instance.
**Fix:** Already possible — just run 3-4 instances of `python extractor\worker.py`
simultaneously. Each picks up different pages from the PostgreSQL queue.
**Impact:** 3-4x throughput with zero code changes.

#### U11. Dedicated historical corpus ingestion pipeline
**Problem:** The crawler follows links randomly. Most pages are low-value.
**Fix:** Build a `corpus/` module that directly ingests known high-value
historical corpora:
- Project Gutenberg full texts (via their bulk download API)
- Chronicling America newspaper OCR text (via their API)
- Internet Archive book texts (via their S3-compatible API)
- SEC EDGAR filings (via their EDGAR full-text search API)
This bypasses the crawler entirely for known sources.

#### U12. Cross-source entity resolution pipeline
**Problem:** The same historical figure appears under different name variants
across sources ("Rockefeller", "J.D. Rockefeller", "John D. Rockefeller Sr.").
**Fix:** After extraction, run a post-processing step that clusters entity
names by string similarity (Levenshtein distance < 3) and merges them in Neo4j.
**Tools:** `rapidfuzz` library for fast fuzzy matching.

#### U13. Question quality scoring
**Problem:** The generator produces questions but has no way to rank them by
difficulty or interestingness.
**Fix:** Add a scoring function that rates questions by:
- Chain length (more hops = harder)
- Temporal span (wider date range = more obscure)
- Source domain diversity (more diverse = better verified)
- Entity obscurity (less common entity names = harder)

---

### TIER 4 — Research-grade (Long-term vision)

#### U14. N-hop chains (3+ hops)
**Current:** Generator only finds 2-hop chains (A→B→C).
**Upgrade:** Extend Cypher queries to find 3-hop chains (A→B→C→D).
**Impact:** Much harder questions, but requires much denser graph.
**Example:** "The company that acquired X, which was founded by Y, which
previously worked at Z — what was Z?"

#### U15. Contradiction detection
**Problem:** The graph may contain contradictory facts from different sources
(e.g., two sources disagree on a date).
**Fix:** Add a contradiction detector that flags edges where `date` values
from different sources differ by more than a threshold. These contradictions
are themselves interesting — they indicate disputed historical facts.

#### U16. Automatic benchmark evaluation
**Problem:** There's no way to measure whether the generated questions are
actually hard for AI systems.
**Fix:** Build an `evaluator/` module that submits generated questions to
public LLM APIs (GPT-4, Claude, Gemini) and records their answers. Questions
where all LLMs answer incorrectly are the most valuable benchmark items.

#### U17. Export to standard benchmark formats
**Fix:** Add an exporter that converts `question_generated/*.txt` files to
standard QA dataset formats:
- HotpotQA JSON format
- SQuAD format
- HuggingFace datasets format
This makes DeepQuest output directly usable by the research community.

---

## Recommended Execution Order

```
TODAY:
  U3 (--min-domains CLI) → see first questions immediately
  U1 (en_core_web_lg) → better extraction from existing pages

THIS WEEK:
  U2 (Wikipedia direct injection) → fill graph fast
  U4 (processing guard fix) → clean up terminal noise
  U10 (parallel extractors) → 3-4x throughput

NEXT MONTH:
  U5 (Wikipedia API) → cleaner text
  U6 (Wikidata disambiguation) → denser graph
  U8 (infobox extraction) → structured facts
  U11 (corpus ingestion) → bypass crawler for known sources
  U12 (entity resolution) → merge duplicate entities

LONG TERM:
  U9 (transformer NER) → when GPU available
  U14 (3-hop chains) → when graph is dense enough
  U16 (LLM evaluation) → when you have a question dataset to test
  U17 (benchmark export) → when ready to publish
```

---

## What Makes DeepQuest Unique vs Existing Work

Every existing multi-hop QA project either:
1. Uses LLMs to generate questions (hallucination risk, not deterministic)
2. Uses static Wikipedia snapshots (not live, not deep web)
3. Requires human annotation (not scalable)

DeepQuest is the only system that:
- Crawls the live web including archives, filings, and historical newspapers
- Extracts facts deterministically (no LLMs, no hallucination)
- Builds a knowledge graph from multi-source consensus
- Generates questions only when 6+ independent sources agree
- Verifies sources are actually accessible and contain the claimed facts

This is a genuinely novel approach worth developing further.
