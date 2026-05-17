import asyncio
import asyncpg
import spacy
from bs4 import BeautifulSoup
import trafilatura
from urllib.parse import urlparse
import sys
import os
import re
import string
import logging
import redis.asyncio as redis
from dotenv import load_dotenv

# Ensure the parent directory is in the path to import graph
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from graph.schema import GraphManager

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()
PG_USER = os.getenv("PG_USER", "deepquest")
PG_PASSWORD = os.getenv("PG_PASSWORD", "deepquestpassword")
PG_DB = os.getenv("PG_DB", "deepquestdb")
PG_HOST = os.getenv("PG_HOST", "localhost")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Suppress noisy trafilatura and htmldate internal warnings
import warnings
warnings.filterwarnings("ignore", category=UserWarning)
logging.getLogger("trafilatura").setLevel(logging.ERROR)
logging.getLogger("htmldate").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)

# Load the NLP model — use lg for better NER accuracy on historical text
try:
    nlp = spacy.load("en_core_web_lg")
    logger.info("Loaded en_core_web_lg NLP model")
except OSError:
    try:
        nlp = spacy.load("en_core_web_md")
        logger.info("Loaded en_core_web_md NLP model (lg not found)")
    except OSError:
        logger.warning("en_core_web_lg/md not found, falling back to en_core_web_sm. Run: python -m spacy download en_core_web_lg")
        try:
            nlp = spacy.load("en_core_web_sm")
        except OSError:
            import subprocess
            subprocess.run(["python", "-m", "spacy", "download", "en_core_web_sm"])
            nlp = spacy.load("en_core_web_sm")

# Increase max length to accommodate large SEC filings and Gutenberg books
nlp.max_length = 3000000

# Controlled Verb Vocabulary
VERB_MAPPING = {
    "acquire": "ACQUIRED", "buy": "ACQUIRED", "purchase": "ACQUIRED", "takeover": "ACQUIRED",
    "found": "FOUNDED", "create": "FOUNDED", "start": "FOUNDED", "launch": "FOUNDED", "establish": "FOUNDED",
    "locate": "LOCATED_IN", "base": "LOCATED_IN", "headquarter": "LOCATED_IN",
    "hire": "HIRED", "employ": "HIRED", "appoint": "HIRED",
    "fire": "FIRED", "dismiss": "FIRED", "oust": "FIRED",
    "release": "RELEASED", "publish": "RELEASED", "announce": "RELEASED",
    "sue": "SUED", "litigate": "SUED",
    "fund": "FUNDED", "invest": "FUNDED", "back": "FUNDED",
    "build": "CREATED", "make": "CREATED", "produce": "CREATED", "develop": "CREATED",
    "write": "WROTE", "author": "WROTE", "compose": "WROTE",
    "win": "WON", "award": "WON",
    "declare": "ANNOUNCED", "say": "ANNOUNCED", "report": "ANNOUNCED",
    "serve": "SERVED_AS", "lead": "LED", "head": "HEADED",
    "chair": "CHAIRED", "direct": "DIRECTED", "govern": "GOVERNED",
    "preside": "PRESIDED", "resign": "RESIGNED",
    "succeed": "SUCCEEDED", "replace": "REPLACED",
    # Historical / political verbs
    "sign": "SIGNED", "ratify": "SIGNED", "enact": "SIGNED",
    "elect": "ELECTED", "appoint": "APPOINTED", "nominate": "APPOINTED",
    "defeat": "DEFEATED", "conquer": "CONQUERED", "capture": "CONQUERED",
    "invade": "INVADED", "occupy": "INVADED",
    "negotiate": "NEGOTIATED", "broker": "NEGOTIATED",
    "merge": "MERGED", "consolidate": "MERGED", "combine": "MERGED",
    "dissolve": "DISSOLVED", "abolish": "DISSOLVED", "disband": "DISSOLVED",
    "patent": "PATENTED", "invent": "INVENTED", "discover": "DISCOVERED",
    "open": "OPENED", "close": "CLOSED", "complete": "COMPLETED",
    "introduce": "INTRODUCED", "propose": "INTRODUCED",
    "oppose": "OPPOSED", "support": "SUPPORTED", "endorse": "SUPPORTED",
    "inherit": "INHERITED", "receive": "RECEIVED", "obtain": "RECEIVED",
    "sell": "SOLD", "transfer": "SOLD",
    "join": "JOINED", "leave": "LEFT", "quit": "LEFT",
    "move": "RELOCATED", "relocate": "RELOCATED",
    "expand": "EXPANDED", "grow": "EXPANDED",
    "reduce": "REDUCED", "cut": "REDUCED", "shrink": "REDUCED",
}

def normalize_entity(ent_text):
    text = ent_text.lower().strip(string.punctuation)
    suffixes = [" inc.", " inc", " llc.", " llc", " corp.", " corp", " ltd.", " ltd", " company"]
    for s in suffixes:
        if text.endswith(s):
            text = text[:-len(s)]
    return text.strip()

GENERIC_ENTITIES = {
    "company", "organization", "government", "city", "country", "people", "group",
    "someone", "anyone", "everyone", "open library", "archive", "copilot", "settings",
    "wikipedia", "wikimedia", "internet archive", "library", "foundation",
    # Short/ambiguous names that produce bad questions
    "bell", "watson", "morgan", "grant", "lee", "davis", "smith", "jones",
    "brown", "white", "black", "green", "king", "scott", "hall", "hill",
    # Noise from web pages
    "mcclure", "the sec", "sec", "the act", "the law", "the bill",
    "the patent office", "patent office", "the court", "the congress",
    "the senate", "the house", "the president", "the government",
    "the company", "the firm", "the trust", "the union",
}

CAUSAL_CONNECTIVES = [
    "led to", "resulted in", "caused", "triggered",
    "prompted", "enabled", "followed by",
]

BOILERPLATE_BLOCKLIST = [
    "delivery", "shipping", "paperback", "hardcover", "kindle", "cart",
    "checkout", "comments", "login", "sign in", "subscribe", "newsletter",
    "previous", "next", "read more", "click here", "privacy policy",
    "terms of service", "copyright", "all rights reserved", "reply",
    "related articles", "author", "published", "categories", "tags"
]

_YEAR_RE = re.compile(r'^\d{4}$')
_DECADE_RE = re.compile(r'\bthe\s+(\d{4})\'?s\b|\b(\d{4})\'?s\b', re.IGNORECASE)

# Numerical fact extraction patterns
_PERCENT_RE = re.compile(r'\b(\d+\.?\d*)\s*(%|percent)\b', re.IGNORECASE)
_CURRENCY_RE = re.compile(
    r'([$£€¥])\s*(\d[\d,\.]*)\s*(billion|million|thousand|k|m|b)?\b',
    re.IGNORECASE
)
_ORDINAL_RE = re.compile(r'\b\d+(st|nd|rd|th)\b', re.IGNORECASE)
_YEAR_NUM_RE = re.compile(r'\b(1[0-9]{3}|20[0-2][0-9])\b')

def normalise_date(date_str: str) -> str:
    """
    Normalise a raw DATE entity string:
      - None  → None
      - 4-digit year (e.g. "1887") → returned as-is
      - Decade phrase (e.g. "the 1880s", "1880's") → "1880s"
      - Anything else → returned as-is
    """
    if date_str is None:
        return None
    if _YEAR_RE.match(date_str.strip()):
        return date_str.strip()
    m = _DECADE_RE.search(date_str)
    if m:
        decade = m.group(1) or m.group(2)
        return f"{decade}s"
    return date_str


def _select_best_date(date_candidates: list) -> str:
    """
    Given a list of raw DATE entity strings, return the most specific one:
      1. Prefer strings containing '-' (e.g. "1887-03-15")
      2. Otherwise prefer the longest string
      3. Fallback: first found
    Returns None if the list is empty.
    """
    if not date_candidates:
        return None
    # Priority 1: contains a hyphen (ISO-style or hyphenated date)
    with_hyphen = [d for d in date_candidates if '-' in d]
    if with_hyphen:
        return max(with_hyphen, key=len)
    # Priority 2: longest string
    return max(date_candidates, key=len)


def is_valid_entity(span, valid_entities):
    span_lower = span.lower()
    
    # Reject if it contains any UI/boilerplate text
    for boilerplate in BOILERPLATE_BLOCKLIST:
        if boilerplate in span_lower:
            return False
            
    span_tokens = set(span_lower.split())
    for ent in valid_entities:
        ent_tokens = set(ent.split())
        if ent_tokens & span_tokens:
            return True
    return False

def detect_context(sent_text):
    text = sent_text.lower()
    if any(x in text for x in ["click", "tab", "window", "login", "browser", "interface",
                                 "cart", "checkout", "shipping", "cookie", "javascript",
                                 "subscribe", "newsletter", "sign up", "sign in"]):
        return "UI"
    elif any(x in text for x in ["acquired", "founded", "merged", "ceo", "revenue",
                                   "profit", "stake", "buy", "purchase", "company",
                                   "firm", "industry", "market", "corporation", "trust",
                                   "monopoly", "railroad", "oil", "steel", "bank",
                                   "shareholder", "dividend", "stock", "merger"]):
        return "CORPORATE"
    elif any(x in text for x in ["born", "died", "married", "father", "mother", "son",
                                   "daughter", "wrote", "author", "composed", "published",
                                   "released", "won", "award", "prize", "appointed",
                                   "elected", "served", "president", "minister", "general",
                                   "colonel", "senator", "governor", "secretary"]):
        return "BIOGRAPHICAL"
    elif any(x in text for x in ["theory", "experiment", "discovery", "research",
                                   "scientific", "study", "analysis", "data", "results",
                                   "observation", "patent", "invention", "laboratory",
                                   "chemistry", "physics", "biology", "medicine"]):
        return "SCIENTIFIC"
    elif any(x in text for x in ["war", "battle", "treaty", "signed", "declared",
                                   "invaded", "conquered", "defeated", "surrendered",
                                   "revolution", "rebellion", "independence", "colony",
                                   "empire", "kingdom", "republic", "parliament",
                                   "congress", "senate", "law", "act", "amendment"]):
        return "HISTORICAL"
    elif any(x in text for x in ["built", "constructed", "opened", "completed",
                                   "established", "founded", "created", "launched",
                                   "introduced", "developed", "invented", "designed"]):
        return "CORPORATE"  # treat construction/creation as corporate-adjacent
    return "GENERAL"

def extract_role_relations(sent, date_entity):
    """
    Extract role/title relationships from a spaCy sentence span.

    Detects two patterns:
      Pattern A — Copular: "Marie Curie was the director of the Radium Institute"
        - Root or cop token whose lemma is "be"
        - nsubj child that is a PERSON NER entity  → person
        - attr child that is NOUN/PROPN             → title
        - prep→pobj grandchild that is an ORG NER  → org

      Pattern B — Appositive: "John Smith, CEO of Acme Corp"
        - For each PERSON NER span in the sentence
        - appos child of the entity's root token    → title token
        - prep→pobj grandchild of appos that is ORG → org

    Returns a list of (person_norm, role_title, org_norm, date_entity) tuples.
    Rejects extractions where either normalised name is in GENERIC_ENTITIES.
    """
    results = []

    # Build a quick lookup: token index → NER label for PERSON and ORG spans
    person_spans = {}  # token_index → span text  (for every token in a PERSON span)
    org_spans = {}     # token_index → span text  (for every token in an ORG span)
    for ent in sent.ents:
        if ent.label_ == "PERSON":
            for tok in ent:
                person_spans[tok.i] = ent.text
        elif ent.label_ == "ORG":
            for tok in ent:
                org_spans[tok.i] = ent.text

    # ── Pattern A: Copular ────────────────────────────────────────────────────
    for token in sent:
        if token.lemma_.lower() == "be" and token.dep_ in ("ROOT", "cop"):
            # The governing verb for a cop token is its head; for ROOT it is itself
            gov = token.head if token.dep_ == "cop" else token

            person_text = None
            title_text = None
            org_text = None

            for child in gov.children:
                if child.dep_ == "nsubj" and child.i in person_spans:
                    person_text = person_spans[child.i]
                elif child.dep_ == "attr" and child.pos_ in ("NOUN", "PROPN"):
                    title_text = child.text
                    # Look for prep → pobj chain under the attr token
                    for prep_child in child.children:
                        if prep_child.dep_ == "prep":
                            for pobj_child in prep_child.children:
                                if pobj_child.dep_ == "pobj" and pobj_child.i in org_spans:
                                    org_text = org_spans[pobj_child.i]

            if person_text and title_text and org_text:
                person_norm = normalize_entity(person_text)
                org_norm = normalize_entity(org_text)
                if person_norm not in GENERIC_ENTITIES and org_norm not in GENERIC_ENTITIES:
                    results.append((person_norm, title_text, org_norm, date_entity))

    # ── Pattern B: Appositive ─────────────────────────────────────────────────
    # Collect unique PERSON spans (by span text) to avoid duplicate processing
    seen_persons = set()
    for ent in sent.ents:
        if ent.label_ != "PERSON":
            continue
        if ent.text in seen_persons:
            continue
        seen_persons.add(ent.text)

        # The "root" token of the entity span (rightmost head inside the span)
        root_tok = ent.root

        for child in root_tok.children:
            if child.dep_ != "appos":
                continue
            title_text = child.text

            # Look for prep → pobj under the appositive token
            for prep_child in child.children:
                if prep_child.dep_ == "prep":
                    for pobj_child in prep_child.children:
                        if pobj_child.dep_ == "pobj" and pobj_child.i in org_spans:
                            org_text = org_spans[pobj_child.i]
                            person_norm = normalize_entity(ent.text)
                            org_norm = normalize_entity(org_text)
                            if (person_norm not in GENERIC_ENTITIES
                                    and org_norm not in GENERIC_ENTITIES):
                                results.append((person_norm, title_text, org_norm, date_entity))

    return results


_CURRENCY_SYMBOL_MAP = {'$': 'USD', '£': 'GBP', '€': 'EUR', '¥': 'JPY'}
_MULTIPLIER_MAP = {
    'billion': 1e9, 'b': 1e9,
    'million': 1e6, 'm': 1e6,
    'thousand': 1e3, 'k': 1e3,
}


def extract_numerical_facts(sent) -> list:
    """
    Extract numerical facts (percentages and currency amounts) from a spaCy Span.

    Returns a list of (verb_lemma, float_value, unit_str) tuples where:
      - verb_lemma is the lemma of the nearest VERB token in the sentence
      - float_value is the normalised numeric value
      - unit_str is "percent" or a currency code (USD/GBP/EUR/JPY)

    Positions that match _ORDINAL_RE or _YEAR_NUM_RE are excluded.
    """
    text = sent.text
    facts = []

    # Collect excluded character positions (ordinals and years)
    excluded_spans = set()
    for m in _ORDINAL_RE.finditer(text):
        excluded_spans.update(range(m.start(), m.end()))
    for m in _YEAR_NUM_RE.finditer(text):
        excluded_spans.update(range(m.start(), m.end()))

    # Build a list of (char_start, float_value, unit_str) candidates
    candidates = []

    for m in _PERCENT_RE.finditer(text):
        if m.start() in excluded_spans:
            continue
        try:
            value = float(m.group(1))
        except ValueError:
            continue
        candidates.append((m.start(), value, "percent"))

    for m in _CURRENCY_RE.finditer(text):
        if m.start() in excluded_spans:
            continue
        symbol = m.group(1)
        raw_num = m.group(2).replace(',', '')
        multiplier_str = (m.group(3) or '').lower()
        try:
            value = float(raw_num)
        except ValueError:
            continue
        multiplier = _MULTIPLIER_MAP.get(multiplier_str, 1.0)
        value *= multiplier
        currency_code = _CURRENCY_SYMBOL_MAP.get(symbol, symbol)
        candidates.append((m.start(), value, currency_code))

    if not candidates:
        return facts

    # Build a list of (char_start, verb_token) for all VERB tokens in the sentence
    verb_positions = []
    sent_start_char = sent.start_char
    for token in sent:
        if token.pos_ == "VERB":
            verb_positions.append((token.idx - sent_start_char, token))

    if not verb_positions:
        return facts

    # For each candidate, find the nearest VERB by character distance
    for char_pos, value, unit in candidates:
        nearest_verb = min(verb_positions, key=lambda vp: abs(vp[0] - char_pos))
        facts.append((nearest_verb[1].lemma_.lower(), value, unit))

    return facts


_CAUSAL_ENT_LABELS = {"PERSON", "ORG", "GPE", "EVENT", "NORP"}


def extract_consequence_chains(sent, date_entity) -> list:
    """
    Extract event-consequence chain tuples from a single spaCy sentence.

    Returns a list of (cause_norm, effect_norm, cause_date, effect_date) tuples
    where cause_date == date_entity and effect_date == None (we cannot reliably
    split dates between clauses).

    Satisfies Req 5 (Enriched Relationship Schema — Event-Consequence Chains).
    """
    # Enforce sentence length guard: 8–60 tokens
    if not (8 <= len(sent) <= 60):
        return []

    sent_text_lower = sent.text.lower()
    results = []

    for connective in CAUSAL_CONNECTIVES:
        if connective not in sent_text_lower:
            continue

        # Find the split point (first occurrence)
        idx = sent_text_lower.index(connective)
        left_text = sent.text[:idx]
        right_text = sent.text[idx + len(connective):]

        # Parse the two clauses with spaCy to get NER
        left_doc = nlp(left_text)
        right_doc = nlp(right_text)

        # Extract last qualifying entity from the left clause (cause)
        cause_ent = None
        for ent in left_doc.ents:
            if ent.label_ in _CAUSAL_ENT_LABELS:
                cause_ent = ent.text  # keep updating → last one wins

        # Extract first qualifying entity from the right clause (effect)
        effect_ent = None
        for ent in right_doc.ents:
            if ent.label_ in _CAUSAL_ENT_LABELS:
                effect_ent = ent.text
                break  # first one wins

        if cause_ent is None or effect_ent is None:
            continue

        cause_norm = normalize_entity(cause_ent)
        effect_norm = normalize_entity(effect_ent)

        # Reject generic or too-short entities
        if cause_norm in GENERIC_ENTITIES or effect_norm in GENERIC_ENTITIES:
            continue
        if len(cause_norm) <= 1 or len(effect_norm) <= 1:
            continue

        # Reject pronouns
        _pronouns = {"i", "us", "we", "he", "she", "it", "they", "you",
                     "this", "that", "these", "those", "who", "which"}
        if cause_norm in _pronouns or effect_norm in _pronouns:
            continue

        # Reject self-loops
        if cause_norm == effect_norm:
            continue

        results.append((cause_norm, effect_norm, date_entity, None))

    return results


def extract_all(text):
    """
    Extract SVO triples, role relations, numerical facts, and consequence chains
    from text. Returns a dict with keys:
      - 'svo': list of (subj, verb, obj, context, date) tuples
      - 'roles': list of (person, role_title, org, date) tuples
      - 'consequences': list of (cause, effect, cause_date, effect_date) tuples
    """
    if len(text) > 2500000:
        text = text[:2500000]

    svo_list = []
    role_relations = []
    consequence_chains = []
    chunk_size = 100000

    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        doc = nlp(chunk)

        for sent in doc.sents:
            if len(sent) < 5 or len(sent) > 60:
                continue

            context_type = detect_context(sent.text)
            if context_type == "GENERAL" or context_type == "UI":
                continue

            date_candidates = []
            valid_entities = {}

            for ent in sent.ents:
                if ent.label_ in ["PERSON", "ORG", "GPE"]:
                    norm = normalize_entity(ent.text)
                    if len(norm) > 1:
                        valid_entities[norm] = ent.label_
                elif ent.label_ == "DATE":
                    date_candidates.append(ent.text)

            date_entity = normalise_date(_select_best_date(date_candidates))

            if not valid_entities:
                continue

            # Role/title extraction
            role_relations.extend(extract_role_relations(sent, date_entity))

            # Consequence chain extraction (uses its own 8-60 token guard)
            consequence_chains.extend(extract_consequence_chains(sent, date_entity))

            # SVO extraction (only for sentences <= 40 tokens for precision)
            if len(sent) > 40:
                continue

            for token in sent:
                if token.pos_ == "VERB":
                    is_negated = any(child.dep_ == "neg" for child in token.children)
                    if is_negated:
                        continue

                    verb_lemma = token.lemma_.lower()
                    normalized_verb = VERB_MAPPING.get(verb_lemma)
                    if not normalized_verb:
                        continue

                    subject = None
                    object_ = None
                    agent = None

                    for child in token.children:
                        if child.dep_ == "nsubj":
                            subject = " ".join([t.text for t in child.subtree if t.dep_ != "punct"]).strip()
                        elif child.dep_ == "nsubjpass":
                            object_ = " ".join([t.text for t in child.subtree if t.dep_ != "punct"]).strip()
                        elif child.dep_ == "agent":
                            for grandchild in child.children:
                                if grandchild.dep_ == "pobj":
                                    agent = " ".join([t.text for t in grandchild.subtree if t.dep_ != "punct"]).strip()
                        elif child.dep_ in ["dobj", "pobj", "iobj", "attr"]:
                            object_ = " ".join([t.text for t in child.subtree if t.dep_ != "punct"]).strip()

                    if agent and object_:
                        subject = agent

                    if subject and object_:
                        subj_norm = normalize_entity(subject)
                        obj_norm = normalize_entity(object_)

                        if subj_norm in GENERIC_ENTITIES or obj_norm in GENERIC_ENTITIES:
                            continue

                        subj_valid = is_valid_entity(subject, valid_entities)
                        obj_valid = is_valid_entity(object_, valid_entities)

                        pronouns = ["i", "you", "he", "she", "it", "we", "they", "that", "this",
                                    "everyone", "someone", "anyone", "these", "those", "which",
                                    "who", "whom", "whose", "their", "its"]
                        is_pronoun = subj_norm in pronouns or obj_norm in pronouns

                        if not is_pronoun and subj_valid and obj_valid and subj_norm != obj_norm:
                            if 2 < len(subj_norm) < 100 and 2 < len(obj_norm) < 100:
                                if len(subj_norm.split()) > 5 or len(obj_norm.split()) > 5:
                                    continue
                                svo_list.append((subj_norm, normalized_verb, obj_norm, context_type, date_entity))

    return {
        'svo': list(set(svo_list)),
        'roles': list(set(role_relations)),
        'consequences': list(set(consequence_chains)),
    }


# Keep extract_svo as a backward-compatible alias
def extract_svo(text):
    return extract_all(text)['svo']

def clean_html(raw_html):
    if not raw_html:
        return ""
    # Use trafilatura to extract main content, ignoring navigation, ads, and boilerplate
    text = trafilatura.extract(raw_html, include_comments=False, include_tables=False, no_fallback=False)
    if not text:
        return ""
    # Clean up whitespace
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    text = '\n'.join(chunk for chunk in chunks if chunk)
    return text

async def process_pages():
    # Retry connecting to Neo4j and Postgres on startup
    graph = None
    while graph is None:
        try:
            graph = GraphManager()
        except Exception as e:
            logger.error(f"Could not connect to Neo4j: {e} — retrying in 10s")
            await asyncio.sleep(10)

    conn = None
    while conn is None:
        try:
            conn = await asyncpg.connect(user=PG_USER, password=PG_PASSWORD, database=PG_DB, host=PG_HOST)
        except Exception as e:
            logger.error(f"Could not connect to PostgreSQL: {e} — retrying in 10s")
            await asyncio.sleep(10)

    logger.info("Extractor Worker started. Polling for unprocessed pages...")

    while True:
        try:
            rows = await conn.fetch(
                "SELECT id, url, raw_html FROM pages WHERE processed = FALSE LIMIT 10"
            )
        except Exception as e:
            logger.error(f"DB fetch error: {e} — reconnecting in 10s")
            await asyncio.sleep(10)
            try:
                conn = await asyncpg.connect(user=PG_USER, password=PG_PASSWORD, database=PG_DB, host=PG_HOST)
            except Exception:
                pass
            continue

        if not rows:
            await asyncio.sleep(5)
            continue

        for row in rows:
            page_id = row['id']
            url = row['url']
            raw_html = row['raw_html']

            # Skip known binary/delivery URLs immediately — mark processed and move on
            _skip_url_patterns = (
                '/ebooks/send/', '/ebooks/download/', '/download/',
                '/compress/', '/stream/', '/serve/',
                '.epub', '.kf8', '.mobi', '.epub3',
                '.zip', '.gz', '.torrent', '.jp2', '_raw_jp2',
                '.tiff', '.tif', '.djvu', '.marc',
                '.txt.utf', '.txt.utf-8', 'utf-8',
                'change.org', 'forms.gle', 'docs.google.com',
                'forms.google', 'accounts.google',
            )
            if any(pat in url.lower() for pat in _skip_url_patterns):
                await conn.execute("UPDATE pages SET processed = TRUE WHERE id = $1", page_id)
                continue

            logger.info(f"Extracting relationships from: {url}")
            text = clean_html(raw_html)

            if not text:
                # Mark as processed so it never loops again — empty content is a dead end
                await conn.execute("UPDATE pages SET processed = TRUE WHERE id = $1", page_id)
                logger.debug(f"Empty content for page {page_id} ({url}) — marked processed")
                continue

            # Skip pages with fewer than 100 words of clean text
            if len(text.split()) < 100:
                await conn.execute("UPDATE pages SET processed = TRUE WHERE id = $1", page_id)
                logger.debug(f"Page {page_id} too short ({len(text.split())} words) — marked processed")
                continue

            domain = urlparse(url).netloc
            try:
                extracted = extract_all(text)
            except Exception as e:
                logger.error(f"Extraction error for page {page_id} ({url}): {e}")
                continue

            svo_triples = extracted['svo']
            role_relations = extracted['roles']
            consequence_chains = extracted['consequences']

            svo_written = 0
            try:
                if svo_triples:
                    graph.push_svo(svo_triples, url, domain)
                    svo_written = len(svo_triples)
                if role_relations:
                    graph.push_role_relations(role_relations, url, domain)
                if consequence_chains:
                    graph.push_consequence_chains(consequence_chains, url, domain)
            except Exception as e:
                logger.error(f"Neo4j write error for page {page_id}: {e}")

            logger.info(
                f"Page {page_id} | url={url} | "
                f"svo={svo_written} | roles={len(role_relations)} | "
                f"consequences={len(consequence_chains)} | "
                f"written_to_neo4j={svo_written}"
            )

            try:
                await conn.execute("UPDATE pages SET processed = TRUE WHERE id = $1", page_id)
            except Exception as e:
                logger.error(f"Could not mark page {page_id} as processed: {e}")

    graph.close()
    await conn.close()

if __name__ == "__main__":
    try:
        asyncio.run(process_pages())
    except KeyboardInterrupt:
        logger.info("Extractor stopped.")
