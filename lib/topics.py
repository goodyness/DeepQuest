"""
Load seed topics from a text file (one per line, any domain).

Default file: seeder/topics.txt (relative to project root).
"""

from __future__ import annotations

import os
from pathlib import Path

# Project root = parent of lib/
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TOPICS_FILE = _PROJECT_ROOT / "seeder" / "topics.txt"


def normalize_topic(line: str) -> str:
    """
    Turn a line into a Wikipedia-style title (underscores for spaces).
    Returns empty string for comments/blank lines.
    """
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return ""
    # Allow inline comments: "CRISPR  # gene editing"
    if " #" in raw:
        raw = raw.split(" #", 1)[0].strip()
    return raw.replace(" ", "_")


def load_topics_file(path: str | Path) -> list[str]:
    """Read topics from a file; skip blanks and # comment lines."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Topics file not found: {path}")

    topics: list[str] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as f:
        for line in f:
            topic = normalize_topic(line)
            if not topic:
                continue
            key = topic.upper()
            if key in seen:
                continue
            seen.add(key)
            topics.append(topic)
    return topics


def resolve_topics(
    *,
    cli_topics: list[str] | None = None,
    topics_file: str | Path | None = None,
    default_topics: list[str] | None = None,
    limit: int | None = None,
    use_default_file: bool = True,
) -> list[str]:
    """
    Pick topic list in priority order:
      1. CLI --topics
      2. --topics-file (or default seeder/topics.txt if it exists)
      3. built-in default_topics list
    """
    if cli_topics:
        topics = [normalize_topic(t) or t.replace(" ", "_") for t in cli_topics]
        topics = [t for t in topics if t]
    else:
        path = Path(topics_file) if topics_file else None
        if path is None and use_default_file and DEFAULT_TOPICS_FILE.is_file():
            path = DEFAULT_TOPICS_FILE
        if path is not None and path.is_file():
            topics = load_topics_file(path)
        elif default_topics:
            topics = list(default_topics)
        else:
            topics = []

    if limit is not None and limit > 0:
        topics = topics[:limit]
    return topics


def add_topics_file_argument(parser, default: str | None = None) -> None:
    """Register --topics-file on an argparse parser."""
    help_path = str(DEFAULT_TOPICS_FILE) if DEFAULT_TOPICS_FILE.is_file() else "seeder/topics.txt"
    parser.add_argument(
        "--topics-file",
        type=str,
        default=default,
        help=f"Text file with one topic per line (default if present: {help_path})",
    )
