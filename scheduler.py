"""
scheduler.py — Automated Pipeline Scheduler

Runs the full DeepQuest pipeline on a schedule:
  1. Inject infoboxes (high-precision structured facts)
  2. Inject Wikipedia articles
  3. Inject historical corpus (Chronicling America, Archive.org)
  4. Merge duplicate entities
  5. Detect contradictions
  6. Generate questions

Designed to run continuously in the background alongside the crawler and extractor.

Usage:
    python scheduler.py                    # run every 6 hours
    python scheduler.py --interval 2       # run every 2 hours
    python scheduler.py --once             # run once and exit
    python scheduler.py --skip-inject      # only generate, skip seeding
"""

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('scheduler.log', encoding='utf-8'),
    ]
)
logger = logging.getLogger("DeepQuest_Scheduler")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable


def run_step(name: str, args: list[str], timeout: int = 600) -> bool:
    """Run a pipeline step. Returns True on success."""
    logger.info(f"▶ Starting: {name}")
    start = time.time()
    try:
        result = subprocess.run(
            args,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.time() - start
        if result.returncode == 0:
            logger.info(f"✓ Completed: {name} ({elapsed:.0f}s)")
            # Log last few lines of output
            output_lines = (result.stdout + result.stderr).strip().split('\n')
            for line in output_lines[-3:]:
                if line.strip():
                    logger.info(f"  {line.strip()}")
            return True
        else:
            logger.warning(f"✗ Failed: {name} (exit {result.returncode}, {elapsed:.0f}s)")
            error_lines = result.stderr.strip().split('\n')
            for line in error_lines[-3:]:
                if line.strip():
                    logger.warning(f"  {line.strip()}")
            return False
    except subprocess.TimeoutExpired:
        logger.error(f"✗ Timeout: {name} (>{timeout}s)")
        return False
    except Exception as e:
        logger.error(f"✗ Error: {name}: {e}")
        return False


def run_pipeline(skip_inject: bool = False, min_domains: int = 2,
                 min_sources: int = 2, skip_verify: bool = True):
    """Run the full pipeline once."""
    logger.info("=" * 60)
    logger.info(f"Pipeline run started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    steps_run = 0
    steps_ok = 0

    if not skip_inject:
        # Step 1: Inject infoboxes (fast, high precision)
        steps_run += 1
        if run_step("Inject Infoboxes", [PYTHON, "seeder/inject_infoboxes.py"], timeout=300):
            steps_ok += 1

        # Step 2: Inject Wikipedia articles
        steps_run += 1
        if run_step("Inject Wikipedia", [PYTHON, "seeder/inject_wikipedia.py"], timeout=600):
            steps_ok += 1

        # Step 3: Inject historical corpus (limited to 10 results per query to be fast)
        steps_run += 1
        if run_step("Inject Historical Corpus",
                    [PYTHON, "seeder/inject_historical_corpus.py", "--limit", "5"],
                    timeout=600):
            steps_ok += 1

        # Step 4: Merge duplicate entities
        steps_run += 1
        if run_step("Merge Entities", [PYTHON, "seeder/merge_entities.py"], timeout=120):
            steps_ok += 1

        # Step 5: Detect contradictions
        steps_run += 1
        if run_step("Detect Contradictions",
                    [PYTHON, "seeder/detect_contradictions.py"], timeout=120):
            steps_ok += 1

    # Step 6: Generate questions
    gen_args = [
        PYTHON, "generator/query_engine.py",
        "--min-domains", str(min_domains),
        "--min-sources", str(min_sources),
    ]
    if skip_verify:
        gen_args.append("--skip-verify")

    steps_run += 1
    if run_step("Generate Questions", gen_args, timeout=300):
        steps_ok += 1

    # Step 7: Export results
    steps_run += 1
    if run_step("Export Questions", [PYTHON, "evaluator/export.py"], timeout=60):
        steps_ok += 1

    logger.info(f"\nPipeline complete: {steps_ok}/{steps_run} steps succeeded")
    logger.info(f"Next run in {_next_run_str}")

    return steps_ok, steps_run


_next_run_str = "N/A"


def main():
    global _next_run_str

    parser = argparse.ArgumentParser(description="DeepQuest automated pipeline scheduler")
    parser.add_argument("--interval", type=float, default=6.0,
                        help="Hours between pipeline runs (default: 6)")
    parser.add_argument("--once", action="store_true",
                        help="Run once and exit")
    parser.add_argument("--skip-inject", action="store_true",
                        help="Skip seeding steps, only generate questions")
    parser.add_argument("--min-domains", type=int, default=2,
                        help="Min domains for generator (default: 2)")
    parser.add_argument("--min-sources", type=int, default=2,
                        help="Min sources for generator (default: 2)")
    parser.add_argument("--no-skip-verify", action="store_true",
                        help="Enable live URL verification (slower)")
    args = parser.parse_args()

    interval_seconds = args.interval * 3600
    skip_verify = not args.no_skip_verify

    logger.info("DeepQuest Scheduler started")
    logger.info(f"Interval: every {args.interval} hours")
    logger.info(f"Skip inject: {args.skip_inject}")
    logger.info(f"Min domains: {args.min_domains} | Min sources: {args.min_sources}")
    logger.info(f"Skip verify: {skip_verify}")

    if args.once:
        _next_run_str = "N/A (--once mode)"
        run_pipeline(
            skip_inject=args.skip_inject,
            min_domains=args.min_domains,
            min_sources=args.min_sources,
            skip_verify=skip_verify,
        )
        return

    while True:
        next_run_time = time.time() + interval_seconds
        _next_run_str = datetime.fromtimestamp(next_run_time).strftime('%Y-%m-%d %H:%M:%S')

        run_pipeline(
            skip_inject=args.skip_inject,
            min_domains=args.min_domains,
            min_sources=args.min_sources,
            skip_verify=skip_verify,
        )

        logger.info(f"Sleeping until {_next_run_str}...")
        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("Scheduler stopped by user.")
            break


if __name__ == "__main__":
    main()
