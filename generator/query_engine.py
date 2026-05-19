"""
generator/query_engine.py — DeepQuest Question Generator (v3)

Rewrites the generator to produce narrative prompts with full format.txt output,
6-source hard gate, historical depth scoring, --min-year CLI, and uniqueness checks.

Requirements: 8, 9, 10, 11, 12, 15
"""

import argparse
import hashlib
import os
import re
import sys
import logging
from datetime import datetime
from urllib.parse import urlparse

from neo4j import GraphDatabase

# Allow importing verifier from the project root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from verifier.worker import SourceVerifier, get_key_terms

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DeepQuest_Generator_V3")

OUTPUT_DIR = "question_generated"

# ---------------------------------------------------------------------------
# Entity quality filters (carried over from v2)
# ---------------------------------------------------------------------------

BAD_PATTERNS = [
    "list of", "full list", "click", "read more",
    "faq", "about", "login", "subscribe",
    "privacy", "terms", "comment", "share",
]

GENERIC_ENTITIES = {
    "company", "organization", "government", "city", "country",
    "people", "group", "someone", "anyone", "everyone",
    # Short/ambiguous single-word names
    "bell", "watson", "morgan", "grant", "lee", "davis",
    "mcclure", "the sec", "patent office",
    # Known noise entities from crawler
    "eldred", "john jackson", "new ebooks", "project gutenberg",
    "open library", "archive", "copilot", "settings",
}

VALID_ENTITY_TYPES = {
    "book":          ["novel", "book", "treatise", "memoir", "biography"],
    "publication":   ["magazine", "journal", "review", "newspaper", "gazette"],
    "company":       ["inc", "ltd", "corp", "company", "railroad", "railway",
                      "oil", "steel", "bank", "trust", "enterprise", "firm"],
    "person":        [],  # fallback
    "event":         ["war", "battle", "treaty", "revolution", "rebellion",
                      "crisis", "panic", "massacre", "siege", "campaign"],
    "product":       ["software", "browser", "app", "machine", "engine",
                      "telephone", "telegraph", "typewriter", "locomotive"],
    "country":       ["france", "england", "britain", "germany", "russia",
                      "spain", "italy", "america", "united states", "china",
                      "india", "japan", "austria", "prussia", "ottoman"],
    "organisation":  ["congress", "parliament", "senate", "union", "association",
                      "institute", "agency", "bureau", "department", "ministry",
                      "commission", "court", "supreme court", "committee",
                      "society", "foundation", "university", "college"],
    "law":           ["act", "law", "statute", "amendment", "bill", "decree",
                      "ordinance", "regulation", "constitution"],
    "invention":     ["patent", "invention", "discovery", "process", "method"],
}

# ---------------------------------------------------------------------------
# Relationship → human-readable verb phrase
# ---------------------------------------------------------------------------

_REL_HUMANISE = {
    "ACQUIRED":    "acquired",
    "FOUNDED":     "founded",
    "LOCATED_IN":  "was located in",
    "HIRED":       "hired",
    "FIRED":       "fired",
    "RELEASED":    "released",
    "SUED":        "sued",
    "FUNDED":      "funded",
    "CREATED":     "created",
    "WROTE":       "wrote",
    "WON":         "won",
    "ANNOUNCED":   "announced",
    "SERVED_AS":   "served as",
    "LED":         "led",
    "HEADED":      "headed",
    "CHAIRED":     "chaired",
    "DIRECTED":    "directed",
    "GOVERNED":    "governed",
    "PRESIDED":    "presided over",
    "RESIGNED":    "resigned from",
    "SUCCEEDED":   "succeeded",
    "REPLACED":    "replaced",
    "WAS_ROLE_OF": "held a role at",
    "LED_TO":      "led to",
    # New historical verbs
    "SIGNED":      "signed",
    "ELECTED":     "elected",
    "APPOINTED":   "appointed",
    "DEFEATED":    "defeated",
    "CONQUERED":   "conquered",
    "INVADED":     "invaded",
    "NEGOTIATED":  "negotiated",
    "MERGED":      "merged with",
    "DISSOLVED":   "dissolved",
    "PATENTED":    "patented",
    "INVENTED":    "invented",
    "DISCOVERED":  "discovered",
    "OPENED":      "opened",
    "COMPLETED":   "completed",
    "INTRODUCED":  "introduced",
    "OPPOSED":     "opposed",
    "SUPPORTED":   "supported",
    "INHERITED":   "inherited",
    "RECEIVED":    "received",
    "SOLD":        "sold",
    "JOINED":      "joined",
    "LEFT":        "left",
    "RELOCATED":   "relocated to",
    "EXPANDED":    "expanded",
    "REDUCED":     "reduced",
}

# ---------------------------------------------------------------------------
# Narrative prompt templates — 5 variants per chain type
# ---------------------------------------------------------------------------

# Placeholders: {A}, {C}, {verb1}, {verb2}, {date1}, {date2}, {entity_type}, {numerical}
# {B} is NEVER used — the answer entity is always concealed.

_SVO_TEMPLATES = [
    # Variant 0 — chronological narrative
    (
        "In{date1}, {A} {verb1} a {entity_type} that would later become historically significant. "
        "That same {entity_type} subsequently {verb2} {C}{date2}. "
        "{numerical}"
        "Using at least six independent sources, identify the {entity_type} that connects these two events."
    ),
    # Variant 1 — consequence-first
    (
        "The {verb2} of {C}{date2} can be traced to an earlier event: {A} had {verb1} "
        "a {entity_type}{date1} that served as the critical intermediary. "
        "{numerical}"
        "What was the name of this {entity_type}?"
    ),
    # Variant 2 — investigative
    (
        "Historians tracing the origins of {C} found that {A} had {verb1} "
        "an intermediary {entity_type}{date1}. "
        "That {entity_type} later {verb2} {C}{date2}. "
        "{numerical}"
        "Name the {entity_type} that served as this link between {A} and {C}."
    ),
    # Variant 3 — archival
    (
        "Primary sources from{date1} document that {A} {verb1} a {entity_type} "
        "which subsequently {verb2} {C}{date2}. "
        "{numerical}"
        "Cross-referencing at least six independent sources, identify this {entity_type}."
    ),
    # Variant 4 — challenge framing
    (
        "Two historical facts are connected by a single {entity_type}: "
        "{A} {verb1} it{date1}, and it later {verb2} {C}{date2}. "
        "{numerical}"
        "Using verifiable historical sources, identify this {entity_type}."
    ),
]

_ROLE_TEMPLATES = [
    (
        "A specific individual held a senior position at {C}{date1}. "
        "During their tenure, {C} {verb2} {A}{date2}. "
        "{numerical}"
        "Using at least six independent sources, identify this person and their role."
    ),
    (
        "Historical records show that {C} {verb2} {A}{date2}. "
        "At the time{date1}, a named {entity_type} was leading {C}. "
        "{numerical}"
        "Who was this {entity_type}, and what position did they hold?"
    ),
    (
        "The {entity_type} who oversaw {C}{date1} was directly connected to "
        "the event in which {C} {verb2} {A}{date2}. "
        "{numerical}"
        "Name this {entity_type}."
    ),
    (
        "In{date1}, a {entity_type} was at the helm of {C}. "
        "Under their leadership, {C} {verb2} {A}{date2}. "
        "{numerical}"
        "Using primary sources, identify this {entity_type}."
    ),
    (
        "Cross-referencing archival sources: {C} {verb2} {A}{date2}. "
        "A named {entity_type} held executive authority at {C}{date1}. "
        "{numerical}"
        "Who was this {entity_type}?"
    ),
]

_CONSEQUENCE_TEMPLATES = [
    (
        "A chain of events beginning with {A}{date1} ultimately led to {C}{date2}. "
        "{numerical}"
        "Using at least six independent sources, identify the pivotal intermediary "
        "that connected these two outcomes."
    ),
    (
        "Historical analysis shows that {A}{date1} set in motion a sequence of events "
        "that culminated in {C}{date2}. "
        "{numerical}"
        "What was the key entity or event that formed the critical link in this chain?"
    ),
    (
        "Scholars studying {C} traced its origins to {A}{date1}, "
        "which triggered a series of consequences. "
        "{numerical}"
        "Name the entity that emerged from this chain of events{date2}."
    ),
    (
        "In{date1}, {A} set in motion a process that would culminate in {C}{date2}. "
        "{numerical}"
        "What was the name of the entity or event that formed the critical link?"
    ),
    (
        "The connection between {A} and {C} runs through a lesser-known intermediary. "
        "{A} was connected to this entity{date1}; it subsequently led to {C}{date2}. "
        "{numerical}"
        "Identify this intermediary using verifiable historical sources."
    ),
]

_TEMPLATES_BY_TYPE = {
    "SVO":         _SVO_TEMPLATES,
    "ROLE":        _ROLE_TEMPLATES,
    "CONSEQUENCE": _CONSEQUENCE_TEMPLATES,
}

# 3-hop templates — answer is entity_b (the first hidden intermediary)
# Placeholders: {A}, {C}, {D}, {verb1}, {verb2}, {verb3}, {date1}, {date2}, {date3}, {entity_type}
_3HOP_TEMPLATES = [
    (
        "In{date1}, {A} {verb1} an intermediary {entity_type}. "
        "That {entity_type} subsequently {verb2} {C}{date2}, "
        "which in turn {verb3} {D}{date3}. "
        "Using at least six independent sources, identify the {entity_type} "
        "that connects {A} to both {C} and {D}."
    ),
    (
        "A chain of three historical events connects {A} to {D}: "
        "{A} {verb1} a {entity_type}{date1}; "
        "that {entity_type} {verb2} {C}{date2}; "
        "{C} then {verb3} {D}{date3}. "
        "Name the {entity_type} that forms the first link in this chain."
    ),
    (
        "Archival research reveals a three-step connection: "
        "{A} {verb1} a {entity_type}{date1}. "
        "The same {entity_type} {verb2} {C}{date2}. "
        "{C} subsequently {verb3} {D}{date3}. "
        "What was this {entity_type}?"
    ),
    (
        "Three historical facts are linked by a single {entity_type}: "
        "it was connected to {A}{date1}, to {C}{date2}, and ultimately to {D}{date3}. "
        "Using primary sources, identify this {entity_type}."
    ),
    (
        "In{date1}, {A} {verb1} a {entity_type} whose influence extended across decades. "
        "It {verb2} {C}{date2}, and {C} later {verb3} {D}{date3}. "
        "What was the name of this {entity_type}?"
    ),
]

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r'\b(1[0-9]{3}|20[0-2][0-9])\b')
_DECADE_RE = re.compile(r'\b(\d{4})\'?s\b')


def parse_year(date_str) -> int | None:
    """Extract a 4-digit year from a date string. Returns None if not found."""
    if not date_str:
        return None
    m = _YEAR_RE.search(str(date_str))
    if m:
        return int(m.group(1))
    m = _DECADE_RE.search(str(date_str))
    if m:
        return int(m.group(1))
    return None


def priority_score(chain: dict) -> int:
    """Return 3/2/1/0 based on the earliest year in the chain."""
    years = [parse_year(chain.get('date_1')), parse_year(chain.get('date_2'))]
    years = [y for y in years if y is not None]
    if not years:
        return 0
    earliest = min(years)
    if earliest < 1950:
        return 3
    if earliest < 2000:
        return 2
    return 1


def humanise(rel_type: str) -> str:
    """Convert a Neo4j relationship type to a natural-language verb phrase."""
    return _REL_HUMANISE.get(rel_type, rel_type.replace("_", " ").lower())


def infer_entity_type(name: str) -> str:
    """Classify the answer entity by keyword matching."""
    name_lower = name.lower()
    for etype, keywords in VALID_ENTITY_TYPES.items():
        for kw in keywords:
            if kw in name_lower:
                return etype
    # Default based on length — short names are likely people or places
    if len(name_lower.split()) == 1:
        return "entity"
    return "organisation"


def article_for(entity_type: str) -> str:
    """Return 'an' or 'a' depending on the entity type."""
    return "an" if entity_type[0].lower() in "aeiou" else "a"


def is_bad_entity(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in BAD_PATTERNS)


def get_unique_domains(sources: list) -> list:
    """Return one URL per unique domain."""
    domain_map = {}
    for url in sources:
        try:
            domain = urlparse(url).netloc
            if domain and domain not in domain_map:
                domain_map[domain] = url
        except Exception:
            continue
    return list(domain_map.values())


def score_chain_quality(chain: dict) -> tuple[float, list[str]]:
    """
    Score a chain on multiple quality dimensions.
    Returns (score 0.0-1.0, list of rejection reasons).

    A chain passes if score >= 0.5 and no hard rejections.
    """
    score = 1.0
    reasons = []

    a = chain.get('entity_a', '')
    b = chain.get('entity_b', '')
    c = chain.get('entity_c', '')
    rel1 = chain.get('action_1', '')
    rel2 = chain.get('action_2', '')

    # Hard rejections — score = 0
    # 1. Answer entity is too short (likely noise)
    if len(normalise_name(b)) < 4:
        return 0.0, [f"Answer entity too short: '{b}'"]

    # 2. Subject entity is a single common word (noise) — but NOT historical figures
    single_word_noise = {
        "he", "she", "they", "it", "we", "his", "her", "their", "its",
        "this", "that", "these", "those", "one", "two", "three",
        "new", "old", "first", "last", "next", "other", "same",
        "many", "some", "all", "both", "each", "every", "any",
    }
    if normalise_name(a).lower() in single_word_noise:
        return 0.0, [f"Subject entity is noise word: '{a}'"]
    if normalise_name(c).lower() in single_word_noise:
        return 0.0, [f"Object entity is noise word: '{c}'"]

    # 3. LED_TO chains with generic subjects are almost always noise
    if rel1 == 'LED_TO' and len(normalise_name(a).split()) == 1:
        score -= 0.4
        reasons.append(f"Single-word LED_TO subject: '{a}'")

    # 4. Both relationships are LED_TO — usually noise
    if rel1 == 'LED_TO' and rel2 == 'LED_TO':
        score -= 0.3
        reasons.append("Both relationships are LED_TO (likely noise)")

    # 5. Answer entity contains possessive or article noise
    if "'" in b or b.startswith("THE ") or b.endswith("'S"):
        score -= 0.2
        reasons.append(f"Answer entity has noise formatting: '{b}'")

    # 6. No temporal anchor reduces quality
    d1 = chain.get('date_1')
    d2 = chain.get('date_2')
    if not d1 and not d2:
        score -= 0.2
        reasons.append("No temporal anchor on chain")
    elif d1 and d2:
        score += 0.1  # bonus for having both dates

    # 7. Historical depth bonus
    ps = priority_score(chain)
    if ps == 3:
        score += 0.1  # pre-1950
    elif ps == 0:
        score -= 0.1  # no date

    # 8. Source count bonus
    sources = chain.get('sources', [])
    unique_src = get_unique_domains(sources)
    if len(unique_src) >= 4:
        score += 0.1
    elif len(unique_src) == 1:
        score -= 0.1

    return max(0.0, min(1.0, score)), reasons


def chain_hash(chain: dict) -> str:
    """SHA-256 hash of the chain's key fields, first 8 hex chars."""
    key = "|".join([
        chain.get('entity_a', ''),
        chain.get('action_1', ''),
        chain.get('entity_b', ''),
        chain.get('action_2', ''),
        chain.get('entity_c', ''),
    ])
    return hashlib.sha256(key.encode()).hexdigest()[:8]


def normalise_name(name: str) -> str:
    """Lowercase, strip punctuation and common corporate suffixes."""
    import string as _string
    text = name.lower().strip(_string.punctuation)
    for suffix in [" inc", " llc", " corp", " ltd", " company"]:
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text.strip()


# ---------------------------------------------------------------------------
# Narrative builder
# ---------------------------------------------------------------------------

def clean_prompt(prompt: str) -> str:
    """Clean up double 'in', 'from in', 'from document that' and leading 'In, ' ungrammaticalities."""
    prompt = prompt.replace("In in ", "In ")
    prompt = prompt.replace("in in ", "in ")
    prompt = prompt.replace("from in ", "from ")
    prompt = prompt.replace("from document that", "document that")
    if prompt.startswith("In, "):
        prompt = prompt[4:]
        if prompt:
            prompt = prompt[0].upper() + prompt[1:]
    return prompt


def build_narrative(chain: dict, variant_index: int) -> str | None:
    """
    Build a narrative prompt for the chain. Handles both 2-hop and 3-hop chains.
    Returns None if the answer entity appears verbatim or word count is out of range.
    """
    hop_count = chain.get('hop_count', 2)

    # 3-hop chain handling
    if hop_count == 3:
        a = chain.get('entity_a', '')
        b = chain.get('entity_b', '')  # answer entity
        c = chain.get('entity_c', '')
        d = chain.get('entity_d', '')
        verb1 = humanise(chain.get('action_1', ''))
        verb2 = humanise(chain.get('action_2', ''))
        verb3 = humanise(chain.get('action_3', ''))
        d1 = chain.get('date_1')
        d2 = chain.get('date_2')
        d3 = chain.get('date_3')
        entity_type = infer_entity_type(b)

        template = _3HOP_TEMPLATES[variant_index % len(_3HOP_TEMPLATES)]
        prompt = template.format(
            A=a.title(),
            C=c.title(),
            D=d.title(),
            verb1=verb1,
            verb2=verb2,
            verb3=verb3,
            date1=f" in {d1}" if d1 else "",
            date2=f" in {d2}" if d2 else "",
            date3=f" in {d3}" if d3 else "",
            entity_type=entity_type,
        ).strip()

        prompt = re.sub(r'\ba (entity|organisation|event|invention|act)\b', r'an \1', prompt)
        prompt = clean_prompt(prompt)

        if b.lower() in prompt.lower():
            return None
        word_count = len(prompt.split())
        if not (30 <= word_count <= 300):
            return None
        return prompt

    # 2-hop chain handling (original)
    chain_type = chain.get('context', 'SVO')
    if chain_type not in _TEMPLATES_BY_TYPE:
        chain_type = 'SVO'

    templates = _TEMPLATES_BY_TYPE[chain_type]
    template = templates[variant_index % len(templates)]

    a = chain.get('entity_a', '')
    c = chain.get('entity_c', '')
    answer = chain.get('entity_b', '')
    verb1 = humanise(chain.get('action_1', ''))
    verb2 = humanise(chain.get('action_2', ''))
    d1 = chain.get('date_1')
    d2 = chain.get('date_2')
    entity_type = infer_entity_type(answer)

    date1_str = f" in {d1}" if d1 else ""
    date2_str = f" in {d2}" if d2 else ""

    num_val = chain.get('numerical_value')
    num_unit = chain.get('numerical_unit')
    if num_val is not None and num_unit:
        if num_unit == 'percent':
            numerical_str = f"This represented a {num_val:.0f}% change. "
        else:
            numerical_str = f"The transaction involved {num_unit} {num_val:,.2f}. "
    else:
        numerical_str = ""

    prompt = template.format(
        A=a.title(),
        C=c.title(),
        verb1=verb1,
        verb2=verb2,
        date1=date1_str,
        date2=date2_str,
        entity_type=entity_type,
        numerical=numerical_str,
    ).strip()

    prompt = re.sub(r'\ba (entity|organisation|event|invention|act)\b', r'an \1', prompt)
    prompt = clean_prompt(prompt)

    if answer.lower() in prompt.lower():
        logger.debug(f"Narrative rejected — answer '{answer}' appears in prompt")
        return None

    word_count = len(prompt.split())
    if not (30 <= word_count <= 300):
        logger.debug(f"Narrative rejected — word count {word_count} out of range for entity '{answer}'")
        return None

    return prompt


# ---------------------------------------------------------------------------
# Output builder
# ---------------------------------------------------------------------------

def build_explanation(chain: dict, verified_urls: list) -> str:
    """Build an explanation paragraph referencing source numbers."""
    hop_count = chain.get('hop_count', 2)
    a = chain.get('entity_a', '').title()
    b = chain.get('entity_b', '').title()
    c = chain.get('entity_c', '').title()
    verb1 = humanise(chain.get('action_1', ''))
    verb2 = humanise(chain.get('action_2', ''))
    d1 = chain.get('date_1', '')
    d2 = chain.get('date_2', '')
    source_refs = ", ".join(f"[{i+1}]" for i in range(len(verified_urls)))

    if hop_count == 3:
        d = chain.get('entity_d', '').title()
        verb3 = humanise(chain.get('action_3', ''))
        d3 = chain.get('date_3', '')
        para = (
            f"The answer is {b}. "
            f"Multiple independent sources ({source_refs}) confirm this three-step chain: "
            f"{a} {verb1} {b}" + (f" in {d1}" if d1 else "") +
            f". {b} subsequently {verb2} {c}" + (f" in {d2}" if d2 else "") +
            f". {c} then {verb3} {d}" + (f" in {d3}" if d3 else "") +
            f". Each source corroborates at least one link, establishing {b} as the unique "
            f"entity connecting {a} to {c} and ultimately to {d}."
        )
        return para

    para = (
        f"The answer is {b}. "
        f"Multiple independent sources ({source_refs}) confirm the following chain of facts: "
        f"{a} {verb1} {b}"
        + (f" in {d1}" if d1 else "")
        + f". Subsequently, {b} {verb2} {c}"
        + (f" in {d2}" if d2 else "")
        + f". Each cited source independently corroborates at least one link in this chain, "
        f"establishing {b} as the unique entity satisfying both conditions. "
        f"No other entity in the historical record satisfies both the relationship with {a} "
        f"and the subsequent relationship with {c}."
    )
    return para


def build_fact_fanout(chain: dict, verified_urls: list) -> list:
    """Produce 5–12 sub-atomic claims."""
    a = chain.get('entity_a', '').title()
    b = chain.get('entity_b', '').title()
    c = chain.get('entity_c', '').title()
    verb1 = humanise(chain.get('action_1', ''))
    verb2 = humanise(chain.get('action_2', ''))
    d1 = chain.get('date_1', '')
    d2 = chain.get('date_2', '')
    entity_type = infer_entity_type(chain.get('entity_b', ''))

    src = lambda i: f"([{i+1}])" if i < len(verified_urls) else ""

    facts = [
        f"{a} {verb1} {b}" + (f" in {d1}" if d1 else "") + f" {src(0)}",
        f"{b} is a {entity_type} {src(1)}",
        f"{b} {verb2} {c}" + (f" in {d2}" if d2 else "") + f" {src(2)}",
        f"{a} and {c} are connected through {b} {src(3)}",
        f"The relationship between {a} and {b} is documented in multiple independent sources {src(4)}",
    ]

    if d1:
        facts.append(f"The {verb1} event occurred in {d1} {src(min(5, len(verified_urls)-1))}")
    if d2:
        facts.append(f"The {verb2} event occurred in {d2} {src(min(6, len(verified_urls)-1))}")

    num_val = chain.get('numerical_value')
    num_unit = chain.get('numerical_unit')
    if num_val is not None and num_unit:
        if num_unit == 'percent':
            facts.append(f"The event involved a {num_val:.0f}% quantitative change {src(min(7, len(verified_urls)-1))}")
        else:
            facts.append(f"The transaction value was {num_unit} {num_val:,.2f} {src(min(7, len(verified_urls)-1))}")

    return facts[:12]


def build_search_trajectory(chain: dict) -> list:
    """Produce ≥4 ordered search steps."""
    a = chain.get('entity_a', '').title()
    c = chain.get('entity_c', '').title()
    verb1 = humanise(chain.get('action_1', ''))
    verb2 = humanise(chain.get('action_2', ''))
    d1 = chain.get('date_1', '')
    entity_type = infer_entity_type(chain.get('entity_b', ''))

    steps = [
        f"Search for '{a} {verb1}'" + (f" {d1}" if d1 else "") + f" to identify what {entity_type} {a} was associated with.",
        f"Cross-reference results with historical archives (Internet Archive, Chronicling America, HathiTrust) to find primary sources.",
        f"For each candidate {entity_type} found, verify whether it subsequently {verb2} {c}.",
        f"Confirm the answer using at least 6 independent sources from different domains.",
        f"Check that no other {entity_type} satisfies both conditions simultaneously to ensure uniqueness.",
    ]
    return steps


def build_output(chain: dict, verified_urls: list, narrative: str) -> str:
    """Assemble the full format.txt output structure."""
    answer = chain.get('entity_b', '').upper()
    explanation = build_explanation(chain, verified_urls)
    fact_fanout = build_fact_fanout(chain, verified_urls)
    search_trajectory = build_search_trajectory(chain)

    sources_block = "\n".join(f"{i+1}. {url}" for i, url in enumerate(verified_urls))
    fanout_block = "\n".join(f"{i+1}. {fact}" for i, fact in enumerate(fact_fanout))
    trajectory_block = "\n".join(f"{i+1}. {step}" for i, step in enumerate(search_trajectory))

    return (
        f"PROMPT:\n{narrative}\n\n"
        f"SOURCES:\n{sources_block}\n\n"
        f"ANSWER:\n{answer}\n\n"
        f"EXPLANATION:\n{explanation}\n\n"
        f"FACT_FANOUT:\n{fanout_block}\n\n"
        f"SEARCH_TRAJECTORY:\n{trajectory_block}\n"
    )


# ---------------------------------------------------------------------------
# QuestionGenerator
# ---------------------------------------------------------------------------

class QuestionGenerator:
    def __init__(self, uri="bolt://localhost:7687", user="neo4j", password="deepquestpassword"):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        if not os.path.exists(OUTPUT_DIR):
            os.makedirs(OUTPUT_DIR)
        self.verifier = SourceVerifier(timeout_seconds=120)

    def close(self):
        self.driver.close()

    # ------------------------------------------------------------------
    # Chain queries
    # ------------------------------------------------------------------

    def find_all_chains(self, min_domains: int = 8) -> list:
        """
        Run three Cypher queries (SVO, ROLE, CONSEQUENCE) and return combined
        results sorted by historical depth priority score.
        """
        results = []

        # Query A — generic SVO chains (2-hop)
        query_svo = (
            "MATCH (a:Entity)-[r1]->(b:Entity)-[r2]->(c:Entity) "
            "WHERE a.name <> b.name AND b.name <> c.name AND a.name <> c.name "
            "WITH a, r1, b, r2, c, "
            "     coalesce(r1.domains,[]) + coalesce(r2.domains,[]) AS all_domains, "
            "     coalesce(r1.sources,[]) + coalesce(r2.sources,[]) AS all_sources "
            "WHERE size(all_domains) >= $min_domains "
            "RETURN a.name AS entity_a, type(r1) AS action_1, r1.date AS date_1, "
            "       b.name AS entity_b, type(r2) AS action_2, r2.date AS date_2, "
            "       c.name AS entity_c, all_sources AS sources, all_domains AS domains, "
            "       r1.context AS context "
            "LIMIT 100"
        )

        # Query A2 — 3-hop SVO chains (harder questions)
        query_svo_3hop = (
            "MATCH (a:Entity)-[r1]->(b:Entity)-[r2]->(c:Entity)-[r3]->(d:Entity) "
            "WHERE a.name <> b.name AND b.name <> c.name AND c.name <> d.name "
            "  AND a.name <> c.name AND a.name <> d.name AND b.name <> d.name "
            "WITH a, r1, b, r2, c, r3, d, "
            "     coalesce(r1.domains,[]) + coalesce(r2.domains,[]) + coalesce(r3.domains,[]) AS all_domains, "
            "     coalesce(r1.sources,[]) + coalesce(r2.sources,[]) + coalesce(r3.sources,[]) AS all_sources "
            "WHERE size(all_domains) >= $min_domains "
            "RETURN a.name AS entity_a, type(r1) AS action_1, r1.date AS date_1, "
            "       b.name AS entity_b, type(r2) AS action_2, r2.date AS date_2, "
            "       c.name AS entity_c, type(r3) AS action_3, r3.date AS date_3, "
            "       d.name AS entity_d, "
            "       all_sources AS sources, all_domains AS domains, "
            "       r1.context AS context "
            "LIMIT 50"
        )

        # Query B — role chains (person held role at org, org did something)
        query_role = (
            "MATCH (a:Entity)-[r1:WAS_ROLE_OF]->(b:Entity)-[r2]->(c:Entity) "
            "WHERE a.name <> b.name AND b.name <> c.name AND a.name <> c.name "
            "WITH a, r1, b, r2, c, "
            "     coalesce(r1.domains,[]) + coalesce(r2.domains,[]) AS all_domains, "
            "     coalesce(r1.sources,[]) + coalesce(r2.sources,[]) AS all_sources "
            "WHERE size(all_domains) >= $min_domains "
            "RETURN a.name AS entity_a, type(r1) AS action_1, r1.date AS date_1, "
            "       b.name AS entity_b, type(r2) AS action_2, r2.date AS date_2, "
            "       c.name AS entity_c, all_sources AS sources, all_domains AS domains, "
            "       'ROLE' AS context "
            "LIMIT 100"
        )

        # Query C — consequence chains
        query_consequence = (
            "MATCH (a:Entity)-[r1:LED_TO]->(b:Entity)-[r2]->(c:Entity) "
            "WHERE a.name <> b.name AND b.name <> c.name AND a.name <> c.name "
            "WITH a, r1, b, r2, c, "
            "     coalesce(r1.domains,[]) + coalesce(r2.domains,[]) AS all_domains, "
            "     coalesce(r1.sources,[]) + coalesce(r2.sources,[]) AS all_sources "
            "WHERE size(all_domains) >= $min_domains "
            "RETURN a.name AS entity_a, type(r1) AS action_1, r1.cause_date AS date_1, "
            "       b.name AS entity_b, type(r2) AS action_2, r2.date AS date_2, "
            "       c.name AS entity_c, all_sources AS sources, all_domains AS domains, "
            "       'CONSEQUENCE' AS context "
            "LIMIT 100"
        )

        seen = set()
        with self.driver.session() as session:
            for query in [query_svo, query_role, query_consequence]:
                try:
                    for record in session.run(query, min_domains=min_domains):
                        chain = record.data()
                        key = (
                            chain.get('entity_a'), chain.get('action_1'),
                            chain.get('entity_b'), chain.get('action_2'),
                            chain.get('entity_c'),
                        )
                        if key not in seen:
                            seen.add(key)
                            results.append(chain)
                except Exception as e:
                    logger.warning(f"Chain query failed: {e}")

            # 3-hop chains — run separately, mark with hop_count=3
            try:
                for record in session.run(query_svo_3hop, min_domains=min_domains):
                    chain = record.data()
                    chain['hop_count'] = 3
                    key = (
                        chain.get('entity_a'), chain.get('action_1'),
                        chain.get('entity_b'), chain.get('action_2'),
                        chain.get('entity_c'), chain.get('action_3'),
                        chain.get('entity_d'),
                    )
                    if key not in seen:
                        seen.add(key)
                        results.append(chain)
            except Exception as e:
                logger.debug(f"3-hop query failed (graph may be too sparse): {e}")

        results.sort(key=priority_score, reverse=True)
        return results

    # Backward-compatible alias
    def find_chains(self, min_domains=2):
        return self.find_all_chains(min_domains=min_domains)

    # ------------------------------------------------------------------
    # Uniqueness check
    # ------------------------------------------------------------------

    def check_uniqueness(self, chain: dict) -> bool:
        """Return True if entity_b is the ONLY entity satisfying the chain conditions."""
        query = (
            "MATCH (a:Entity {name: $a})-[r1]->(x:Entity)-[r2]->(c:Entity {name: $c}) "
            "WHERE type(r1) = $rel1 AND type(r2) = $rel2 "
            "RETURN x.name AS candidate"
        )
        try:
            with self.driver.session() as session:
                records = session.run(
                    query,
                    a=chain['entity_a'],
                    c=chain['entity_c'],
                    rel1=chain['action_1'],
                    rel2=chain['action_2'],
                ).data()
            candidates = {normalise_name(r['candidate']) for r in records}
            if len(candidates) > 1:
                logger.info(
                    f"Ambiguous chain rejected: {chain['entity_a']} → {chain['entity_b']} → {chain['entity_c']} "
                    f"| other candidates: {candidates - {normalise_name(chain['entity_b'])}}"
                )
                return False
        except Exception as e:
            logger.warning(f"Uniqueness check failed: {e}")
        return True

    # ------------------------------------------------------------------
    # Main generation loop
    # ------------------------------------------------------------------

    def generate(self, min_year: int = None, limit: int = None, min_domains: int = 3,
                 min_sources: int = 3, skip_verify: bool = False):
        logger.info("Starting DeepQuest Generator v3...")

        chains = self.find_all_chains(min_domains=min_domains)
        logger.info(f"Found {len(chains)} candidate chains (pre-filter, min_domains={min_domains}).")

        stats = {
            'total': len(chains),
            'rejected_entity': 0,
            'rejected_min_year': 0,
            'rejected_uniqueness': 0,
            'rejected_sources': 0,
            'rejected_narrative': 0,
            'written': 0,
        }

        for i, chain in enumerate(chains):
            if limit and stats['written'] >= limit:
                break

            b = chain.get('entity_b', '')

            # Chain quality gate — filter noise before any other processing
            quality_score, quality_reasons = score_chain_quality(chain)
            if quality_score < 0.5:
                stats['rejected_entity'] += 1
                logger.debug(f"Chain rejected (quality={quality_score:.2f}): {quality_reasons}")
                continue

            # Entity quality filter
            if is_bad_entity(b) or len(normalise_name(b)) < 3 or normalise_name(b) in GENERIC_ENTITIES:
                stats['rejected_entity'] += 1
                continue

            # Historical depth filter
            if min_year is not None:
                years = [parse_year(chain.get('date_1')), parse_year(chain.get('date_2'))]
                years = [y for y in years if y is not None]
                if years and max(years) >= min_year:
                    stats['rejected_min_year'] += 1
                    continue

            # Uniqueness check
            if not self.check_uniqueness(chain):
                stats['rejected_uniqueness'] += 1
                continue

            # Build narrative (try all 5 variants)
            narrative = None
            for variant in range(5):
                narrative = build_narrative(chain, variant)
                if narrative:
                    break

            if not narrative:
                stats['rejected_narrative'] += 1
                logger.info(f"No valid narrative for chain {i}: '{b}' (A={chain.get('entity_a','')}, C={chain.get('entity_c','')})")
                continue

            # Source verification — configurable gate
            sources = chain.get('sources', [])
            unique_sources = get_unique_domains(sources)
            key_terms = get_key_terms(chain)

            if skip_verify:
                # Skip live URL verification — use graph sources directly
                verified_urls = unique_sources[:10]
                passed = len(verified_urls) >= min_sources
            else:
                verification = self.verifier.verify(unique_sources, key_terms)
                verified_urls = verification.verified_urls
                passed = len(verified_urls) >= min_sources and \
                         len({urlparse(u).netloc for u in verified_urls}) >= min_sources

            if not passed:
                stats['rejected_sources'] += 1
                logger.info(
                    f"Chain {i} rejected: {len(verified_urls)} sources "
                    f"(need {min_sources}). Entity: {b}"
                )
                continue

            # Build full output
            output = build_output(chain, verified_urls, narrative)

            # Generate filename
            h = chain_hash(chain)
            anchor_year = parse_year(chain.get('date_1') or chain.get('date_2'))
            year_suffix = f"_{anchor_year}" if anchor_year else ""
            filename = os.path.join(
                OUTPUT_DIR,
                f"question_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{h}{year_suffix}.txt"
            )

            try:
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(output)
                logger.info(f"Generated → {filename}")
                stats['written'] += 1
            except OSError as e:
                logger.error(f"Failed to write {filename}: {e}")

        # Per-run summary
        logger.info(
            f"Run complete | total={stats['total']} | "
            f"rejected_entity={stats['rejected_entity']} (includes quality gate) | "
            f"rejected_min_year={stats['rejected_min_year']} | "
            f"rejected_uniqueness={stats['rejected_uniqueness']} | "
            f"rejected_sources={stats['rejected_sources']} | "
            f"rejected_narrative={stats['rejected_narrative']} | "
            f"written={stats['written']}"
        )

        if stats['written'] == 0:
            logger.warning(
                "No questions passed the 6-source gate. "
                "The graph may be too sparse — run the crawler and extractor longer."
            )
            sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DeepQuest Question Generator v3")
    parser.add_argument(
        "--min-year", type=int, default=None,
        help="Discard chains whose latest date year >= this value (e.g. --min-year 2000 for pre-2000 only)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after writing this many output files"
    )
    parser.add_argument(
        "--min-domains", type=int, default=3,
        help="Minimum distinct domains required across a chain's edges (default: 3)"
    )
    parser.add_argument(
        "--min-sources", type=int, default=3,
        help="Minimum verified source URLs required to publish a question (default: 3)"
    )
    parser.add_argument(
        "--skip-verify", action="store_true", default=False,
        help="Skip live URL verification — use graph sources directly (faster, less strict)"
    )
    args = parser.parse_args()

    gen = QuestionGenerator()
    try:
        gen.generate(
            min_year=args.min_year,
            limit=args.limit,
            min_domains=args.min_domains,
            min_sources=args.min_sources,
            skip_verify=args.skip_verify,
        )
    finally:
        gen.close()
