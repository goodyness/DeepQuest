"""
evaluator/export.py — Export to Standard Benchmark Formats (U17)

Converts generated question .txt files to standard formats:
  - JSON (simple, human-readable)
  - CSV (spreadsheet-compatible)
  - HuggingFace datasets format (JSONL, compatible with datasets library)
  - SQuAD format (standard QA benchmark format)
  - HotpotQA format (multi-hop QA format)

Usage:
    python evaluator/export.py                          # export all, all formats
    python evaluator/export.py --format json            # JSON only
    python evaluator/export.py --format csv             # CSV only
    python evaluator/export.py --format huggingface     # HuggingFace JSONL
    python evaluator/export.py --format squad           # SQuAD JSON
    python evaluator/export.py --format hotpotqa        # HotpotQA JSON
    python evaluator/export.py --dir question_generated --out exports/
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepQuest_Exporter")

DEFAULT_INPUT_DIR = "question_generated"
DEFAULT_OUTPUT_DIR = "exports"

# ---------------------------------------------------------------------------
# Parser
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
        'id': Path(filepath).stem,
        'file': str(filepath),
        'prompt': '',
        'answer': '',
        'sources': [],
        'explanation': '',
        'fact_fanout': [],
        'search_trajectory': [],
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
        elif section.startswith('FACT_FANOUT:'):
            lines = section[12:].strip().split('\n')
            result['fact_fanout'] = [re.sub(r'^\d+\.\s*', '', l).strip() for l in lines if l.strip()]
        elif section.startswith('SEARCH_TRAJECTORY:'):
            lines = section[18:].strip().split('\n')
            result['search_trajectory'] = [re.sub(r'^\d+\.\s*', '', l).strip() for l in lines if l.strip()]

    if not result['prompt'] or not result['answer']:
        return None

    # Extract year from filename if present
    year_match = re.search(r'_(\d{4})\.txt$', filepath)
    result['year'] = int(year_match.group(1)) if year_match else None

    return result


def load_all_questions(input_dir: str) -> list[dict]:
    """Load all question files from a directory."""
    questions = []
    question_dir = Path(input_dir)
    if not question_dir.exists():
        logger.error(f"Directory not found: {input_dir}")
        return questions

    files = sorted(question_dir.glob("question_*.txt"))
    logger.info(f"Found {len(files)} question files in {input_dir}")

    for f in files:
        q = parse_question_file(str(f))
        if q:
            questions.append(q)
        else:
            logger.warning(f"Could not parse: {f.name}")

    logger.info(f"Successfully parsed {len(questions)} questions")
    return questions


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------

def export_json(questions: list[dict], output_path: str):
    """Export as a clean JSON array."""
    data = []
    for q in questions:
        data.append({
            'id': q['id'],
            'question': q['prompt'],
            'answer': q['answer'],
            'sources': q['sources'],
            'source_count': len(q['sources']),
            'explanation': q['explanation'],
            'fact_fanout': q['fact_fanout'],
            'search_trajectory': q['search_trajectory'],
            'year': q['year'],
        })

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    logger.info(f"JSON export: {output_path} ({len(data)} questions)")


def export_csv(questions: list[dict], output_path: str):
    """Export as CSV for spreadsheet use."""
    fieldnames = ['id', 'question', 'answer', 'source_count', 'sources', 'year', 'explanation']

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for q in questions:
            writer.writerow({
                'id': q['id'],
                'question': q['prompt'],
                'answer': q['answer'],
                'source_count': len(q['sources']),
                'sources': ' | '.join(q['sources']),
                'year': q['year'] or '',
                'explanation': q['explanation'][:200] + '...' if len(q['explanation']) > 200 else q['explanation'],
            })

    logger.info(f"CSV export: {output_path} ({len(questions)} questions)")


def export_huggingface(questions: list[dict], output_path: str):
    """
    Export as HuggingFace datasets JSONL format.
    Compatible with: datasets.load_dataset('json', data_files='...')
    """
    with open(output_path, 'w', encoding='utf-8') as f:
        for q in questions:
            record = {
                'id': q['id'],
                'question': q['prompt'],
                'answer': {
                    'text': [q['answer']],
                    'answer_start': [-1],  # not extractive
                },
                'context': q['explanation'],
                'supporting_facts': [
                    {'title': f"Source {i+1}", 'sent_id': 0}
                    for i in range(len(q['sources']))
                ],
                'sources': q['sources'],
                'type': 'multi-hop',
                'level': 'hard',
                'year': q['year'],
                'fact_fanout': q['fact_fanout'],
            }
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    logger.info(f"HuggingFace JSONL export: {output_path} ({len(questions)} questions)")


def export_squad(questions: list[dict], output_path: str):
    """
    Export in SQuAD v2 format.
    Note: DeepQuest questions are not extractive, so answer_start is -1.
    """
    squad_data = {
        "version": "DeepQuest-v1.0",
        "data": []
    }

    for q in questions:
        # Group by answer entity as "title"
        entry = {
            "title": q['answer'],
            "paragraphs": [
                {
                    "context": q['explanation'],
                    "qas": [
                        {
                            "id": q['id'],
                            "question": q['prompt'],
                            "answers": [
                                {
                                    "text": q['answer'],
                                    "answer_start": -1,
                                }
                            ],
                            "is_impossible": False,
                            "plausible_answers": [],
                            "sources": q['sources'],
                        }
                    ]
                }
            ]
        }
        squad_data["data"].append(entry)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(squad_data, f, indent=2, ensure_ascii=False)

    logger.info(f"SQuAD format export: {output_path} ({len(questions)} questions)")


def export_hotpotqa(questions: list[dict], output_path: str):
    """
    Export in HotpotQA format.
    Each question has supporting facts from multiple sources.
    """
    hotpot_data = []

    for q in questions:
        # Build supporting facts from fact_fanout
        supporting_facts = []
        for i, fact in enumerate(q['fact_fanout'][:5]):
            supporting_facts.append([f"Source {i+1}", 0])

        # Build context from sources
        context = []
        for i, source_url in enumerate(q['sources'][:5]):
            context.append([
                f"Source {i+1}",
                [q['fact_fanout'][i] if i < len(q['fact_fanout']) else source_url]
            ])

        record = {
            '_id': q['id'],
            'question': q['prompt'],
            'answer': q['answer'],
            'supporting_facts': supporting_facts,
            'context': context,
            'type': 'bridge',
            'level': 'hard',
            'year': q['year'],
        }
        hotpot_data.append(record)

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(hotpot_data, f, indent=2, ensure_ascii=False)

    logger.info(f"HotpotQA format export: {output_path} ({len(questions)} questions)")


def export_markdown(questions: list[dict], output_path: str):
    """Export as a readable Markdown document — good for sharing/publishing."""
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(f"# DeepQuest Benchmark Dataset\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d')}\n")
        f.write(f"Questions: {len(questions)}\n\n")
        f.write("---\n\n")

        for i, q in enumerate(questions, 1):
            year_str = f" ({q['year']})" if q['year'] else ""
            f.write(f"## Question {i}{year_str}\n\n")
            f.write(f"**PROMPT:**\n{q['prompt']}\n\n")
            f.write(f"**ANSWER:** `{q['answer']}`\n\n")
            f.write(f"**SOURCES ({len(q['sources'])}):**\n")
            for j, src in enumerate(q['sources'], 1):
                f.write(f"{j}. {src}\n")
            f.write(f"\n**EXPLANATION:**\n{q['explanation']}\n\n")
            if q['fact_fanout']:
                f.write(f"**FACT FANOUT:**\n")
                for fact in q['fact_fanout']:
                    f.write(f"- {fact}\n")
                f.write("\n")
            f.write("---\n\n")

    logger.info(f"Markdown export: {output_path} ({len(questions)} questions)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Export DeepQuest questions to standard benchmark formats"
    )
    parser.add_argument(
        "--dir", type=str, default=DEFAULT_INPUT_DIR,
        help=f"Input directory with question .txt files (default: {DEFAULT_INPUT_DIR})"
    )
    parser.add_argument(
        "--out", type=str, default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for exported files (default: {DEFAULT_OUTPUT_DIR})"
    )
    parser.add_argument(
        "--format",
        choices=["json", "csv", "huggingface", "squad", "hotpotqa", "markdown", "all"],
        default="all",
        help="Export format (default: all)"
    )
    args = parser.parse_args()

    questions = load_all_questions(args.dir)
    if not questions:
        logger.error("No questions to export.")
        sys.exit(1)

    os.makedirs(args.out, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    formats = {
        "json":        (export_json,        f"deepquest_{timestamp}.json"),
        "csv":         (export_csv,         f"deepquest_{timestamp}.csv"),
        "huggingface": (export_huggingface, f"deepquest_{timestamp}_hf.jsonl"),
        "squad":       (export_squad,       f"deepquest_{timestamp}_squad.json"),
        "hotpotqa":    (export_hotpotqa,    f"deepquest_{timestamp}_hotpotqa.json"),
        "markdown":    (export_markdown,    f"deepquest_{timestamp}.md"),
    }

    selected = list(formats.keys()) if args.format == "all" else [args.format]

    for fmt in selected:
        fn, filename = formats[fmt]
        output_path = os.path.join(args.out, filename)
        try:
            fn(questions, output_path)
        except Exception as e:
            logger.error(f"Export failed for {fmt}: {e}")

    logger.info(f"\nAll exports saved to: {args.out}/")


if __name__ == "__main__":
    main()
