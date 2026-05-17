"""
evaluator/benchmark.py — Automatic Benchmark Evaluation (U16)

Tests generated questions against public AI systems to measure difficulty.
Questions where AI answers incorrectly are the most valuable benchmark items.

Supported evaluators:
  - DuckDuckGo Instant Answer API (free, no auth)
  - Wikipedia search (checks if answer is findable via simple search)
  - Local scoring (checks if answer appears in top search results)

Usage:
    python evaluator/benchmark.py
    python evaluator/benchmark.py --dir question_generated
    python evaluator/benchmark.py --file question_generated/question_xyz.txt
    python evaluator/benchmark.py --report  (show summary of all scored questions)
"""

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import httpx

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepQuest_Evaluator")

RESULTS_FILE = "evaluator/benchmark_results.jsonl"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (compatible; DeepQuestEvaluator/1.0)',
    'Accept': 'application/json',
}

# ---------------------------------------------------------------------------
# Question parser
# ---------------------------------------------------------------------------

def parse_question_file(filepath: str) -> dict | None:
    """Parse a generated question .txt file into a structured dict."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        logger.error(f"Could not read {filepath}: {e}")
        return None

    result = {
        'file': filepath,
        'prompt': '',
        'answer': '',
        'sources': [],
        'explanation': '',
    }

    sections = re.split(r'\n(?=PROMPT:|SOURCES:|ANSWER:|EXPLANATION:|FACT_FANOUT:|SEARCH_TRAJECTORY:)', content)
    for section in sections:
        if section.startswith('PROMPT:'):
            result['prompt'] = section[7:].strip()
        elif section.startswith('ANSWER:'):
            result['answer'] = section[7:].strip()
        elif section.startswith('SOURCES:'):
            lines = section[8:].strip().split('\n')
            result['sources'] = [re.sub(r'^\d+\.\s*', '', l).strip() for l in lines if l.strip()]
        elif section.startswith('EXPLANATION:'):
            result['explanation'] = section[12:].strip()

    if not result['prompt'] or not result['answer']:
        return None
    return result


# ---------------------------------------------------------------------------
# Evaluators
# ---------------------------------------------------------------------------

async def evaluate_duckduckgo(question: dict, client: httpx.AsyncClient) -> dict:
    """
    Use DuckDuckGo Instant Answer API to check if the answer is findable.
    Returns evaluation result dict.
    """
    prompt = question['prompt']
    correct_answer = question['answer'].lower().strip()

    # Extract the core question (last sentence)
    sentences = prompt.split('.')
    query = sentences[-1].strip() if sentences else prompt[:200]
    # Remove question words to make it a search query
    query = re.sub(r'^(using|identify|name|what|who|which)\s+', '', query, flags=re.IGNORECASE)
    query = query[:200]

    result = {
        'evaluator': 'duckduckgo',
        'query': query,
        'ai_answer': None,
        'correct': False,
        'confidence': 'unknown',
        'raw_response': None,
    }

    try:
        r = await client.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            abstract = data.get("AbstractText", "").lower()
            answer_text = data.get("Answer", "").lower()
            heading = data.get("Heading", "").lower()

            result['raw_response'] = {
                'abstract': data.get("AbstractText", "")[:200],
                'answer': data.get("Answer", ""),
                'heading': data.get("Heading", ""),
            }

            # Check if correct answer appears in any response field
            answer_words = set(correct_answer.lower().split())
            for text in [abstract, answer_text, heading]:
                text_words = set(text.split())
                overlap = answer_words & text_words
                if len(overlap) >= max(1, len(answer_words) * 0.6):
                    result['ai_answer'] = heading or answer_text or abstract[:100]
                    result['correct'] = True
                    result['confidence'] = 'high' if answer_text else 'medium'
                    break

            if not result['correct']:
                result['ai_answer'] = heading or answer_text or "(no direct answer)"
                result['confidence'] = 'low'
    except Exception as e:
        logger.debug(f"DuckDuckGo evaluation failed: {e}")
        result['error'] = str(e)

    return result


async def evaluate_wikipedia_search(question: dict, client: httpx.AsyncClient) -> dict:
    """
    Check if the correct answer is the top Wikipedia search result for the prompt.
    If it is, the question is too easy (answer is directly searchable).
    """
    correct_answer = question['answer'].lower().strip()
    prompt_words = question['prompt'].lower().split()

    # Build a search query from key terms in the prompt (excluding the answer)
    # Take the most distinctive words
    stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to',
                  'for', 'of', 'with', 'by', 'from', 'is', 'was', 'were', 'be',
                  'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did',
                  'will', 'would', 'could', 'should', 'may', 'might', 'shall',
                  'using', 'identify', 'name', 'what', 'who', 'which', 'that',
                  'this', 'these', 'those', 'it', 'its', 'their', 'our', 'your',
                  'least', 'six', 'independent', 'sources', 'historical', 'records'}

    key_words = [w for w in prompt_words if w not in stop_words and len(w) > 3][:8]
    query = " ".join(key_words[:5])

    result = {
        'evaluator': 'wikipedia_search',
        'query': query,
        'ai_answer': None,
        'correct': False,
        'confidence': 'unknown',
    }

    try:
        r = await client.get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": 5,
                "format": "json",
            },
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            search_results = data.get("query", {}).get("search", [])
            top_titles = [r["title"].lower() for r in search_results[:3]]

            result['ai_answer'] = search_results[0]["title"] if search_results else "(no results)"

            # Check if correct answer appears in top results
            answer_words = set(correct_answer.split())
            for title in top_titles:
                title_words = set(title.split())
                overlap = answer_words & title_words
                if len(overlap) >= max(1, len(answer_words) * 0.5):
                    result['correct'] = True
                    result['confidence'] = 'high' if title == top_titles[0] else 'medium'
                    break
    except Exception as e:
        logger.debug(f"Wikipedia search evaluation failed: {e}")
        result['error'] = str(e)

    return result


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def compute_difficulty_score(evaluations: list[dict]) -> dict:
    """
    Compute a difficulty score from multiple evaluator results.

    Score interpretation:
      0.0 — AI answers correctly on all evaluators (too easy, not useful)
      0.5 — AI answers correctly on some evaluators (moderate difficulty)
      1.0 — AI fails all evaluators (hard, most valuable for benchmarking)
    """
    if not evaluations:
        return {'score': 0.5, 'label': 'unknown', 'details': []}

    correct_count = sum(1 for e in evaluations if e.get('correct', False))
    total = len(evaluations)
    fail_rate = 1.0 - (correct_count / total)

    if fail_rate >= 0.8:
        label = 'HARD — AI fails consistently (excellent benchmark item)'
    elif fail_rate >= 0.5:
        label = 'MEDIUM — AI partially correct (good benchmark item)'
    elif fail_rate >= 0.2:
        label = 'EASY — AI mostly correct (marginal benchmark value)'
    else:
        label = 'TRIVIAL — AI answers correctly (not useful for benchmarking)'

    return {
        'score': round(fail_rate, 2),
        'label': label,
        'correct_count': correct_count,
        'total_evaluators': total,
        'details': [
            {
                'evaluator': e.get('evaluator'),
                'correct': e.get('correct'),
                'ai_answer': e.get('ai_answer'),
                'confidence': e.get('confidence'),
            }
            for e in evaluations
        ],
    }


# ---------------------------------------------------------------------------
# Main evaluation loop
# ---------------------------------------------------------------------------

async def evaluate_question(question: dict, client: httpx.AsyncClient) -> dict:
    """Run all evaluators on a single question."""
    evaluations = []

    # Run evaluators
    ddg = await evaluate_duckduckgo(question, client)
    evaluations.append(ddg)
    await asyncio.sleep(1.0)

    wiki = await evaluate_wikipedia_search(question, client)
    evaluations.append(wiki)
    await asyncio.sleep(0.5)

    difficulty = compute_difficulty_score(evaluations)

    return {
        'file': question['file'],
        'answer': question['answer'],
        'prompt_preview': question['prompt'][:150] + '...',
        'source_count': len(question['sources']),
        'difficulty': difficulty,
        'evaluated_at': datetime.now().isoformat(),
    }


def save_result(result: dict):
    """Append result to the JSONL results file."""
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
    with open(RESULTS_FILE, 'a', encoding='utf-8') as f:
        f.write(json.dumps(result) + '\n')


def print_report():
    """Print a summary report of all benchmark results."""
    if not os.path.exists(RESULTS_FILE):
        print("No benchmark results found. Run evaluations first.")
        return

    results = []
    with open(RESULTS_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                results.append(json.loads(line.strip()))
            except Exception:
                pass

    if not results:
        print("No results in benchmark file.")
        return

    print(f"\n{'='*70}")
    print(f"DEEPQUEST BENCHMARK REPORT — {len(results)} questions evaluated")
    print(f"{'='*70}\n")

    # Sort by difficulty score (hardest first)
    results.sort(key=lambda r: r['difficulty']['score'], reverse=True)

    hard = [r for r in results if r['difficulty']['score'] >= 0.8]
    medium = [r for r in results if 0.5 <= r['difficulty']['score'] < 0.8]
    easy = [r for r in results if r['difficulty']['score'] < 0.5]

    print(f"HARD (AI fails):     {len(hard)} questions")
    print(f"MEDIUM (partial):    {len(medium)} questions")
    print(f"EASY (AI succeeds):  {len(easy)} questions")
    print()

    print("TOP 10 HARDEST QUESTIONS:")
    print("-" * 70)
    for r in results[:10]:
        score = r['difficulty']['score']
        answer = r['answer']
        preview = r['prompt_preview'][:80]
        sources = r['source_count']
        print(f"  Score: {score:.2f} | Answer: {answer} | Sources: {sources}")
        print(f"  Prompt: {preview}")
        print()


import asyncio


async def run_evaluation(question_files: list[str]):
    """Evaluate a list of question files."""
    logger.info(f"Evaluating {len(question_files)} questions...")

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True) as client:
        for i, filepath in enumerate(question_files):
            logger.info(f"[{i+1}/{len(question_files)}] {os.path.basename(filepath)}")

            question = parse_question_file(filepath)
            if not question:
                logger.warning(f"  Could not parse {filepath}")
                continue

            logger.info(f"  Answer: {question['answer']}")

            result = await evaluate_question(question, client)
            score = result['difficulty']['score']
            label = result['difficulty']['label']

            logger.info(f"  Difficulty: {score:.2f} — {label}")
            save_result(result)

    logger.info(f"\nEvaluation complete. Results saved to {RESULTS_FILE}")
    logger.info("Run with --report to see the full summary.")


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate generated questions against AI systems"
    )
    parser.add_argument(
        "--dir", type=str, default="question_generated",
        help="Directory containing question .txt files (default: question_generated)"
    )
    parser.add_argument(
        "--file", type=str, default=None,
        help="Evaluate a single question file"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print a summary report of all benchmark results"
    )
    args = parser.parse_args()

    if args.report:
        print_report()
        return

    if args.file:
        question_files = [args.file]
    else:
        question_dir = Path(args.dir)
        if not question_dir.exists():
            logger.error(f"Directory not found: {args.dir}")
            sys.exit(1)
        question_files = sorted(question_dir.glob("question_*.txt"))
        if not question_files:
            logger.error(f"No question files found in {args.dir}")
            sys.exit(1)

    asyncio.run(run_evaluation([str(f) for f in question_files]))


if __name__ == "__main__":
    main()
