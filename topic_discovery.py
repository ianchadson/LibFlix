"""Provider-neutral topic discovery planning, normalization, and ranking.

The module is intentionally free of Flask and network state.  ``app.py`` owns
transport, caching, and circuit breakers; this module turns bounded provider
responses into canonical Open Library work cards.
"""

from __future__ import annotations

import html as htmlmod
import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any, Iterable, Protocol, Sequence

from nyt_bestsellers import match_nyt_bestseller


EXPANSION_VERSION = "topic-v3"
RANKER_VERSION = "rrf-v5"
RRF_K = 60
MAX_PROVIDER_RECORDS = 100
MAX_TEXT = 500
MAX_DESCRIPTION = 2_000
RESULT_DESCRIPTION_MAX = MAX_DESCRIPTION
MAX_LIST_VALUES = 64

ISBN_RE = re.compile(r"^(?:\d{9}[\dX]|97[89]\d{10})$")
WORK_KEY_RE = re.compile(r"^(?:/works/)?(OL\d+W)$", re.IGNORECASE)


# The aliases are deliberately small, deterministic, and versioned.  They are
# query-planning hints, not a curated result list; provider evidence still has
# to pass the local relevance gate.
TOPIC_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "focus": ("focus", "attention", "deep work", "distraction"),
    "meditation": ("meditation", "mindfulness", "contemplative practice"),
    "mindfulness": ("mindfulness", "meditation", "present moment"),
    "startups": ("startups", "entrepreneurship", "lean startup", "venture creation"),
    "startup": ("startups", "entrepreneurship", "lean startup", "venture creation"),
    "productivity": ("productivity", "time management", "getting things done"),
    "habits": ("habits", "behavior change", "self control"),
    "sleep": ("sleep", "sleep science", "insomnia"),
    "anxiety": ("anxiety", "anxiety disorders", "stress management"),
    "depression": ("depression", "mood disorders", "mental health"),
    "mental health": ("mental health", "psychological well being", "emotional health"),
    "leadership": ("leadership", "executive management", "organizational leadership"),
    "management": ("management", "organizational behavior", "business management"),
    "marketing": ("marketing", "brand management", "consumer behavior"),
    "sales": ("sales", "selling", "sales management"),
    "investing": ("investing", "investments", "portfolio management"),
    "finance": ("finance", "personal finance", "financial literacy"),
    "economics": ("economics", "economic theory", "political economy"),
    "psychology": ("psychology", "human behavior", "cognitive psychology"),
    "philosophy": ("philosophy", "philosophical thought", "ethics"),
    "creativity": ("creativity", "creative thinking", "innovation"),
    "writing": ("writing", "authorship", "creative writing"),
    "communication": ("communication", "interpersonal communication", "public speaking"),
    "relationships": ("relationships", "interpersonal relations", "love"),
    "parenting": ("parenting", "child development", "parent and child"),
    "health": ("health", "wellness", "preventive health"),
    "fitness": ("fitness", "physical fitness", "exercise"),
    "nutrition": ("nutrition", "diet", "healthy eating"),
    "technology": ("technology", "technological innovation", "computers"),
    "artificial intelligence": ("artificial intelligence", "machine learning", "ai"),
    "ai": ("artificial intelligence", "machine learning", "ai"),
    "climate change": ("climate change", "global warming", "climate science"),
    "science": ("science", "scientific discovery", "popular science"),
    "history": ("history", "world history", "civilization"),
    "biography": ("biography", "autobiography", "memoir"),
}


# Public browse taxonomy. These are discovery starting points rather than
# curated result lists, and every query is exercised by the weekly benchmark.
BROWSE_TOPIC_GROUPS = (
    {
        "name": "Focus & wellbeing",
        "short_name": "Focus",
        "icon": "focus",
        "description": "Direct attention, protect energy, and work with intention.",
        "topics": (
            {"name": "Focus", "query": "focus", "description": "Attention, concentration, and deep work"},
            {"name": "Meditation", "query": "meditation", "description": "Practice, awareness, and calm"},
            {"name": "Productivity", "query": "productivity", "description": "Do meaningful work with less friction"},
            {"name": "Habits", "query": "habits", "description": "Build patterns that last"},
            {"name": "Sleep", "query": "sleep", "description": "Rest, recovery, and sleep science"},
        ),
    },
    {
        "name": "Mind & relationships",
        "short_name": "Mind",
        "icon": "connection",
        "description": "Understand yourself and relate to other people more clearly.",
        "topics": (
            {"name": "Anxiety", "query": "anxiety", "description": "Understand worry and regain agency"},
            {"name": "Depression", "query": "depression", "description": "Research, experience, and recovery"},
            {"name": "Mental health", "query": "mental health", "description": "Emotional health and resilience"},
            {"name": "Psychology", "query": "psychology", "description": "Behavior, cognition, and motivation"},
            {"name": "Relationships", "query": "relationships", "description": "Connection, conflict, and intimacy"},
        ),
    },
    {
        "name": "Build & lead",
        "short_name": "Build",
        "icon": "build",
        "description": "Create companies, lead teams, and make ideas travel.",
        "topics": (
            {"name": "Startups", "query": "startups", "description": "From early insight to durable company"},
            {"name": "Leadership", "query": "leadership", "description": "Direction, judgment, and responsibility"},
            {"name": "Management", "query": "management", "description": "Teams, systems, and execution"},
            {"name": "Marketing", "query": "marketing", "description": "Positioning, brands, and demand"},
            {"name": "Sales", "query": "sales", "description": "Trust, negotiation, and closing"},
        ),
    },
    {
        "name": "Money & work",
        "short_name": "Money",
        "icon": "value",
        "description": "Make clearer decisions about capital, craft, and communication.",
        "topics": (
            {"name": "Investing", "query": "investing", "description": "Long-term thinking and capital"},
            {"name": "Finance", "query": "finance", "description": "Money, markets, and financial literacy"},
            {"name": "Economics", "query": "economics", "description": "Incentives, systems, and trade-offs"},
            {"name": "Communication", "query": "communication", "description": "Speak, listen, and persuade clearly"},
            {"name": "Writing", "query": "writing", "description": "Craft, clarity, and creative practice"},
        ),
    },
    {
        "name": "Health & life",
        "short_name": "Health",
        "icon": "vitality",
        "description": "Care for the body, family, and a more creative life.",
        "topics": (
            {"name": "Parenting", "query": "parenting", "description": "Raising children with perspective"},
            {"name": "Health", "query": "health", "description": "Evidence, prevention, and wellbeing"},
            {"name": "Fitness", "query": "fitness", "description": "Strength, movement, and exercise"},
            {"name": "Nutrition", "query": "nutrition", "description": "Food, health, and useful evidence"},
            {"name": "Creativity", "query": "creativity", "description": "Generate and develop better ideas"},
        ),
    },
    {
        "name": "Science & ideas",
        "short_name": "Science",
        "icon": "science",
        "description": "Understand progress, nature, and possible futures.",
        "topics": (
            {"name": "Technology", "query": "technology", "description": "Innovation and technological change"},
            {"name": "Artificial intelligence", "query": "artificial intelligence", "description": "AI, machine learning, and society"},
            {"name": "Climate change", "query": "climate change", "description": "Climate science and possible futures"},
            {"name": "Science", "query": "science", "description": "Discovery and the natural world"},
            {"name": "Philosophy", "query": "philosophy", "description": "Meaning, ethics, and how to live"},
        ),
    },
)

BROWSE_TOPIC_QUERIES = tuple(
    topic["query"]
    for group in BROWSE_TOPIC_GROUPS
    for topic in group["topics"]
)
FEATURED_TOPIC_QUERIES = (
    "focus",
    "meditation",
    "startups",
    "artificial intelligence",
    "psychology",
    "investing",
    "habits",
    "creativity",
)


# Inventaire can search semantic Wikidata subject claims.  Unknown topics simply
# skip this path; text search remains an optional, tightly gated supplement.
INVENTAIRE_SUBJECT_CLAIMS: dict[str, tuple[str, ...]] = {
    "focus": ("wdt:P921=wd:Q6501338",),  # attention
    "meditation": ("wdt:P921=wd:Q108458", "wdt:P921=wd:Q341045"),
    "mindfulness": ("wdt:P921=wd:Q341045", "wdt:P921=wd:Q108458"),
    "startups": ("wdt:P921=wd:Q129238", "wdt:P921=wd:Q3908516"),
    "startup": ("wdt:P921=wd:Q129238", "wdt:P921=wd:Q3908516"),
}

WIKIDATA_LANGUAGE_CODES = {
    "wd:Q1860": "eng",   # English
    "wd:Q7850": "chi",   # Chinese
    "wd:Q9192": "chi",   # Mandarin Chinese
    "wd:Q727694": "chi", # Standard Chinese
}


TOPIC_PREFIXES = (
    "books about ",
    "best books about ",
    "best books on ",
    "learn about ",
    "topic ",
)


class DiscoveryProvider(Protocol):
    """Minimal provider contract used by alternate transports and tests."""

    name: str

    def search(self, plan: "QueryPlan", *, language: str, limit: int) -> "ProviderPage": ...


@dataclass(frozen=True)
class QueryPlan:
    raw_query: str
    display_query: str
    intent: str
    identifier_type: str
    identifier: str
    queries: tuple[str, ...]
    tokens: tuple[str, ...]
    expansion_version: str = EXPANSION_VERSION
    inventaire_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryCandidate:
    provider: str
    provider_id: str
    native_rank: int
    query_rank: int
    title: str
    authors: tuple[str, ...] = ()
    work_key: str = ""
    isbns: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    description: str = ""
    published_year: int | None = None
    cover_id: int | None = None
    cover_hash: str = ""
    archive_id: str = ""
    ratings_count: int = 0
    ratings_average: float = 0.0
    readinglog_count: int = 0
    osp_count: int = 0
    edition_count: int = 0
    popularity: float = 0.0
    fiction: bool | None = None
    source_url: str = ""
    semantic_terms: tuple[str, ...] = ()
    nyt_rank: int = 0
    nyt_weeks_at_number_one: int = 0
    nyt_list_names: tuple[str, ...] = ()
    nyt_published_date: str = ""


@dataclass(frozen=True)
class ProviderPage:
    provider: str
    query: str
    query_rank: int
    candidates: tuple[DiscoveryCandidate, ...] = ()
    available: bool = True
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryResult:
    candidate: DiscoveryCandidate
    score: float
    reasons: tuple[str, ...]
    sources: tuple[str, ...]
    ranking_sources: tuple[str, ...] = ()


def normalize_text(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = re.sub(r"[^\w\u3400-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _contains_phrase(text: str, phrase: str) -> bool:
    """Match normalized terms on token boundaries, never raw substrings."""
    text = normalize_text(text)
    phrase = normalize_text(phrase)
    if not text or not phrase:
        return False
    # CJK text is commonly unsegmented, so whitespace boundaries would reject
    # valid compounds such as "冥想入门" for the topic "冥想".
    if re.search(r"[\u3400-\u9fff]", phrase):
        return phrase in text
    return bool(re.search(rf"(?:^| ){re.escape(phrase)}(?:$| )", text))


def normalize_isbn(value: Any) -> str:
    value = re.sub(r"[^0-9X]", "", str(value or "").upper())
    return value if ISBN_RE.fullmatch(value) else ""


def normalize_work_key(value: Any) -> str:
    match = WORK_KEY_RE.fullmatch(str(value or "").strip())
    return f"/works/{match.group(1).upper()}" if match else ""


def _bounded_text(value: Any, limit: int = MAX_TEXT) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("en") or next(
            (item for item in value.values() if isinstance(item, str)),
            "",
        )
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _bounded_description(value: Any, limit: int = MAX_DESCRIPTION) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("en") or list(value.values())
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    candidates: list[str] = []
    for item in list(values)[:16]:
        if not isinstance(item, str):
            continue
        boundary_issues = len(re.findall(r"\b[a-z]{4,}(?=[A-Z][a-z])|[.!?](?=[A-Za-z])", item))
        if boundary_issues >= 3:
            continue
        text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", item))
        text = re.sub(r"(?<=[.!?])(?=[A-Z])", " ", text)
        text = re.sub(r"(?<=[!?])(?=[a-z])", " ", text)
        text = re.sub(r"\b([a-z]{4,})([A-Z][a-z])", r"\1 \2", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            candidates.append(text)
    return max(candidates, key=len, default="")[:limit]


def _bounded_strings(value: Any, *, limit: int = MAX_LIST_VALUES) -> tuple[str, ...]:
    if value is None:
        return ()
    values = value if isinstance(value, (list, tuple, set)) else (value,)
    output: list[str] = []
    seen: set[str] = set()
    for item in list(values)[:limit]:
        text = _bounded_text(item)
        marker = normalize_text(text)
        if not text or not marker or marker in seen:
            continue
        seen.add(marker)
        output.append(text)
    return tuple(output)


def _safe_int(value: Any, maximum: int = 10_000_000) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return min(max(number, 0), maximum)


def _safe_float(value: Any, maximum: float = 10_000_000.0) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return min(max(number, 0.0), maximum)


def _archive_identifier(value: Any) -> str:
    values = value if isinstance(value, (list, tuple)) else (value,)
    for candidate in list(values)[:16]:
        identifier = str(candidate or "").strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", identifier):
            return identifier
    return ""


def _inventaire_cover_hash(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("url")
    match = re.fullmatch(
        r"(?:https://inventaire\.io)?/img/entities/([a-f0-9]{40})",
        str(value or "").strip(),
    )
    return match.group(1) if match else ""


def _year(value: Any) -> int | None:
    match = re.search(r"\b(1[4-9]\d{2}|20\d{2}|2100)\b", str(value or ""))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1400 <= year <= date.today().year + 1 else None


def _strip_topic_prefix(query: str) -> str:
    normalized = normalize_text(query)
    for prefix in TOPIC_PREFIXES:
        if normalized.startswith(prefix):
            return normalized[len(prefix):].strip()
    return normalized


def plan_topic_query(query: str, intent: str | None = None) -> QueryPlan:
    raw = re.sub(r"\s+", " ", str(query or "")).strip()[:200]
    normalized = normalize_text(raw)
    identifier = normalize_isbn(raw)
    identifier_type = "isbn" if identifier else ""
    if not identifier:
        work_key = normalize_work_key(raw)
        if work_key:
            identifier_type, identifier = "work", work_key.rsplit("/", 1)[-1]

    requested_intent = str(intent or "").strip().casefold()
    if requested_intent not in {"topic", "identity"}:
        requested_intent = ""
    stripped = _strip_topic_prefix(raw)
    quoted = len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}
    has_topic_prefix = stripped != normalized
    known_topic = stripped in TOPIC_EXPANSIONS
    token_count = len(stripped.split())
    detected = "topic" if (
        not identifier_type
        and not quoted
        and not re.search(r"\s+by\s+", normalized)
        and (known_topic or has_topic_prefix)
    ) else "identity"
    selected_intent = requested_intent or detected

    display = stripped if selected_intent == "topic" and stripped else normalized
    expansions = (display, *TOPIC_EXPANSIONS.get(display, ()))
    # Raw topic plus at most two deterministic expansions keeps provider fanout
    # bounded. Duplicate normalized aliases are removed without changing order.
    planned: list[str] = []
    for expansion in expansions:
        safe = normalize_text(expansion)[:80]
        if safe and safe not in planned:
            planned.append(safe)
        if len(planned) >= 3:
            break
    if not planned and display:
        planned = [display[:80]]
    claims = INVENTAIRE_SUBJECT_CLAIMS.get(display, ())[:2]
    return QueryPlan(
        raw_query=raw,
        display_query=display or normalized,
        intent=selected_intent,
        identifier_type=identifier_type,
        identifier=identifier,
        queries=tuple(planned),
        tokens=tuple(display.split()),
        inventaire_claims=tuple(claims),
    )


def build_openlibrary_request(
    plan: QueryPlan,
    query_rank: int,
    *,
    language: str = "en",
    limit: int = 50,
) -> tuple[str, dict[str, Any]]:
    query = plan.queries[min(max(query_rank, 0), len(plan.queries) - 1)]
    fields = (
        "key,title,author_name,cover_i,language,subject,description,"
        "first_publish_year,ratings_count,ratings_average,readinglog_count,"
        "osp_count,edition_count,isbn,ia"
    )
    params: dict[str, Any] = {
        "subject": query,
        "fields": fields,
        "limit": min(max(int(limit), 1), MAX_PROVIDER_RECORDS),
        "page": 1,
    }
    if language in {"en", "cn", "zh"}:
        params["lang"] = "zh" if language in {"cn", "zh"} else "en"
    return "/search.json", params


def build_inventaire_request(
    plan: QueryPlan,
    query_rank: int,
    *,
    language: str = "en",
    limit: int = 20,
) -> tuple[str, list[tuple[str, str]], bool]:
    bounded_limit = str(min(max(int(limit), 1), 40))
    params = [("types", "works")]
    if language in {"cn", "zh"}:
        params.append(("lang", "zh"))
    elif language == "en":
        params.append(("lang", "en"))
    params.append(("limit", bounded_limit))
    if query_rank < len(plan.inventaire_claims):
        params.append(("claim", plan.inventaire_claims[query_rank]))
        return "/search", params, True
    # Claim-backed plans reserve the final rank for a literal text search. It
    # must use what the reader entered, not the last semantic expansion.
    query = (
        plan.display_query
        if plan.inventaire_claims
        else plan.queries[min(max(query_rank, 0), len(plan.queries) - 1)]
    )
    params.append(("search", query))
    return "/search", params, False


def _infer_fiction(subjects: Sequence[str]) -> bool | None:
    normalized = {normalize_text(subject) for subject in subjects}
    fiction_markers = {
        "fiction", "novels", "fantasy fiction", "science fiction", "historical fiction",
        "romance fiction", "mystery fiction", "juvenile fiction",
    }
    nonfiction_markers = {
        "nonfiction", "biography", "autobiography", "self help", "business",
        "psychology", "history", "science", "philosophy",
    }
    if "novel" in normalized or any(
        value == marker or (marker != "fiction" and marker in value)
        for value in normalized
        for marker in fiction_markers
        if "nonfiction" not in value
    ):
        return True
    if any(
        value.endswith(" fiction") and "nonfiction" not in value
        for value in normalized
    ):
        return True
    if any(marker in value for value in normalized for marker in nonfiction_markers):
        return False
    return None


def parse_openlibrary_payload(
    payload: dict[str, Any] | None,
    query: str,
    query_rank: int = 0,
) -> ProviderPage:
    if not isinstance(payload, dict):
        return ProviderPage("openlibrary", query, query_rank, available=False)
    records = payload.get("docs")
    if not isinstance(records, list):
        return ProviderPage("openlibrary", query, query_rank, available=False)
    candidates: list[DiscoveryCandidate] = []
    semantic_term = normalize_text(query)
    for native_rank, record in enumerate(records[:MAX_PROVIDER_RECORDS], start=1):
        if not isinstance(record, dict):
            continue
        work_key = normalize_work_key(record.get("key"))
        title = _bounded_text(record.get("title"))
        authors = _bounded_strings(record.get("author_name"), limit=12)
        if not work_key or not title or not authors:
            continue
        cover_id = _safe_int(record.get("cover_i"), maximum=2_000_000_000) or None
        subjects = _bounded_strings(record.get("subject"), limit=MAX_LIST_VALUES)
        isbns = tuple(
            value for value in dict.fromkeys(
                normalize_isbn(item) for item in _bounded_strings(record.get("isbn"), limit=32)
            ) if value
        )
        languages = tuple(normalize_text(item) for item in _bounded_strings(record.get("language"), limit=12))
        candidates.append(DiscoveryCandidate(
            provider="openlibrary",
            provider_id=work_key.rsplit("/", 1)[-1],
            native_rank=native_rank,
            query_rank=max(query_rank, 0),
            title=title,
            authors=authors,
            work_key=work_key,
            isbns=isbns,
            languages=languages,
            subjects=subjects,
            description=_bounded_description(record.get("description")),
            published_year=_year(record.get("first_publish_year")),
            cover_id=cover_id,
            archive_id=_archive_identifier(record.get("ia")),
            ratings_count=_safe_int(record.get("ratings_count")),
            ratings_average=min(_safe_float(record.get("ratings_average")), 5.0),
            readinglog_count=_safe_int(record.get("readinglog_count")),
            osp_count=_safe_int(record.get("osp_count")),
            edition_count=_safe_int(record.get("edition_count")),
            fiction=_infer_fiction(subjects),
            source_url=f"https://openlibrary.org{work_key}",
            # Open Library subject results still need visible title/subject/
            # description evidence. This prevents unrelated popularity filler
            # from inheriting relevance merely because the provider returned it.
            semantic_terms=(),
        ))
    return ProviderPage("openlibrary", query, query_rank, tuple(candidates))


def _inventaire_description(record: dict[str, Any]) -> str:
    description = record.get("description") or record.get("descriptions")
    return _bounded_description(description)


def _author_from_description(description: str) -> tuple[str, ...]:
    match = re.search(r"\b(?:book|work|novel|memoir)\s+(?:written\s+)?by\s+([^,;()]{2,100})", description, re.I)
    if not match:
        match = re.search(r"\bby\s+([^,;()]{2,100})", description, re.I)
    return (_bounded_text(match.group(1), 100),) if match else ()


def _claim_values(claims: dict[str, Any], property_name: str) -> tuple[Any, ...]:
    value = claims.get(property_name, ()) if isinstance(claims, dict) else ()
    if not isinstance(value, (list, tuple)):
        value = (value,)
    return tuple(value[:MAX_LIST_VALUES])


def parse_inventaire_payload(
    payload: dict[str, Any] | None,
    query: str,
    query_rank: int = 0,
    *,
    semantic: bool = False,
) -> ProviderPage:
    if not isinstance(payload, dict):
        return ProviderPage("inventaire", query, query_rank, available=False)
    records = payload.get("results")
    if not isinstance(records, list):
        return ProviderPage("inventaire", query, query_rank, available=False)
    candidates: list[DiscoveryCandidate] = []
    semantic_term = normalize_text(query)
    for native_rank, record in enumerate(records[:MAX_PROVIDER_RECORDS], start=1):
        if not isinstance(record, dict):
            continue
        claims = record.get("claims") if isinstance(record.get("claims"), dict) else {}
        work_key = ""
        for value in _claim_values(claims, "wdt:P648"):
            work_key = normalize_work_key(value)
            if work_key:
                break
        # Inventaire edition IDs (OL...M) and unresolved native cards are not
        # safe for LibFlix's work-detail/download route.
        if not work_key:
            continue
        labels = record.get("labels")
        label = record.get("label")
        if not label and isinstance(labels, dict):
            label = labels.get("en") or labels.get("zh") or next(
                (item for item in labels.values() if isinstance(item, str)),
                "",
            )
        title = _bounded_text(label or next(iter(_claim_values(claims, "wdt:P1476")), ""))
        if not title:
            continue
        description = _inventaire_description(record)
        date_value = next(iter(_claim_values(claims, "wdt:P577")), "")
        raw_languages = _claim_values(claims, "wdt:P407")
        languages = tuple(dict.fromkeys(
            WIKIDATA_LANGUAGE_CODES.get(value, "other")
            for value in raw_languages
            if value
        ))
        candidates.append(DiscoveryCandidate(
            provider="inventaire",
            provider_id=_bounded_text(record.get("uri") or record.get("id"), 120),
            native_rank=native_rank,
            query_rank=max(query_rank, 0),
            title=title,
            authors=(
                _bounded_strings(record.get("authors"), limit=12)
                or _author_from_description(description)
            ),
            work_key=work_key,
            languages=languages,
            description=description,
            published_year=_year(date_value),
            cover_hash=_inventaire_cover_hash(record.get("image")),
            popularity=_safe_float(record.get("popularity") or record.get("_popularity")),
            source_url=f"https://inventaire.io/entity/{record.get('uri')}" if record.get("uri") else "",
            semantic_terms=(semantic_term,) if semantic and semantic_term else (),
        ))
    warnings = _bounded_strings(payload.get("warnings"), limit=8)
    return ProviderPage("inventaire", query, query_rank, tuple(candidates), warnings=warnings)


def apply_nyt_bestseller_signals(
    pages: Iterable[ProviderPage],
    index: Any,
) -> list[ProviderPage]:
    """Annotate canonical candidates with exact NYT number-one history matches."""

    enriched = []
    for page in pages:
        if page.provider != "openlibrary" or not page.available:
            enriched.append(page)
            continue
        candidates = []
        for candidate in page.candidates:
            signal = match_nyt_bestseller(
                index,
                isbns=candidate.isbns,
                title=candidate.title,
                authors=candidate.authors,
            )
            if not signal:
                candidates.append(candidate)
                continue
            candidates.append(replace(
                candidate,
                nyt_rank=_safe_int(signal.get("rank"), 100),
                nyt_weeks_at_number_one=_safe_int(
                    signal.get("weeks_at_number_one")
                ),
                nyt_list_names=_bounded_strings(
                    signal.get("list_names"),
                    limit=8,
                ),
                nyt_published_date=_bounded_text(
                    signal.get("published_date"),
                    10,
                ),
            ))
        enriched.append(replace(page, candidates=tuple(candidates)))
    return enriched


def _identity_key(candidate: DiscoveryCandidate) -> tuple[str, ...] | None:
    if candidate.work_key:
        return ("work", candidate.work_key.casefold())
    if candidate.isbns:
        return ("isbn", candidate.isbns[0])
    title = normalize_text(candidate.title)
    author = normalize_text(candidate.authors[0] if candidate.authors else "")
    return ("title_author", title, author) if title and author else None


def _merge_candidates(primary: DiscoveryCandidate, other: DiscoveryCandidate) -> DiscoveryCandidate:
    preferred = primary
    if other.provider == "openlibrary" and primary.provider != "openlibrary":
        preferred = other
    def choose_tuple(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*first, *second)))[:MAX_LIST_VALUES]
    return replace(
        preferred,
        authors=choose_tuple(preferred.authors, other.authors),
        work_key=preferred.work_key or other.work_key,
        isbns=choose_tuple(preferred.isbns, other.isbns),
        languages=choose_tuple(preferred.languages, other.languages),
        subjects=choose_tuple(preferred.subjects, other.subjects),
        description=preferred.description or other.description,
        published_year=preferred.published_year or other.published_year,
        cover_id=preferred.cover_id or other.cover_id,
        cover_hash=preferred.cover_hash or other.cover_hash,
        archive_id=preferred.archive_id or other.archive_id,
        ratings_count=max(preferred.ratings_count, other.ratings_count),
        ratings_average=max(preferred.ratings_average, other.ratings_average),
        readinglog_count=max(preferred.readinglog_count, other.readinglog_count),
        osp_count=max(preferred.osp_count, other.osp_count),
        edition_count=max(preferred.edition_count, other.edition_count),
        popularity=max(preferred.popularity, other.popularity),
        fiction=preferred.fiction if preferred.fiction is not None else other.fiction,
        semantic_terms=choose_tuple(preferred.semantic_terms, other.semantic_terms),
        nyt_rank=min(
            (rank for rank in (preferred.nyt_rank, other.nyt_rank) if rank),
            default=0,
        ),
        nyt_weeks_at_number_one=max(
            preferred.nyt_weeks_at_number_one,
            other.nyt_weeks_at_number_one,
        ),
        nyt_list_names=choose_tuple(
            preferred.nyt_list_names,
            other.nyt_list_names,
        )[:8],
        nyt_published_date=max(
            preferred.nyt_published_date,
            other.nyt_published_date,
        ),
    )


def _topic_evidence(candidate: DiscoveryCandidate, plan: QueryPlan) -> tuple[float, list[str]]:
    title = normalize_text(candidate.title)
    description = normalize_text(candidate.description)
    subjects = [(subject, normalize_text(subject)) for subject in candidate.subjects]
    combined = " ".join((title, description, *(value for _, value in subjects)))

    if plan.display_query in {"meditation", "mindfulness"}:
        fictional_senses = (
            "murder", "detective fiction", "crime novel", "mystery novel",
        )
        practice_signals = (
            "meditation practice", "mindfulness practice", "meditation guide",
            "insight meditation", "guided meditation",
        )
        if (
            any(_contains_phrase(combined, sense) for sense in fictional_senses)
            and not any(
                _contains_phrase(combined, signal)
                for signal in practice_signals
            )
        ):
            return 0.0, []

    if plan.display_query == "focus":
        # The Open Library subject is highly polysemous. Reject unrelated
        # research-method, automobile, linguistics, and programming senses,
        # while preserving a book that also carries genuine attention signals.
        subject_text = " ".join(value for _, value in subjects)
        focus_context = " ".join((title, subject_text))
        if "concentration camp" in combined or "concentration camps" in combined:
            return 0.0, []
        unrelated_senses = (
            "focus group",
            "focus groups",
            "focused group",
            "focused groups",
            "focus automobile",
            "ford focus",
            "focus linguistics",
            "focus computer program language",
            "focus theatre",
            "focus on the family",
            "family focus inc",
        )
        attention_signals = (
            "attention",
            "deep work",
            "distraction",
            "concentration",
            "mindfulness",
            "cognitive psychology",
            "self control",
            "productivity",
        )
        if (
            any(_contains_phrase(focus_context, sense) for sense in unrelated_senses)
            and not any(
                _contains_phrase(focus_context, signal)
                for signal in attention_signals
            )
        ):
            return 0.0, []

    if plan.display_query == "productivity":
        industrial_senses = (
            "well productivity",
            "oil well",
            "petroleum engineering",
            "work measurement",
            "industrial engineering",
            "manufacturing productivity",
        )
        personal_signals = (
            "time management",
            "personal efficiency",
            "self help",
            "work habits",
            "procrastination",
            "attention",
            "goal setting",
            "knowledge work",
        )
        if (
            any(_contains_phrase(combined, sense) for sense in industrial_senses)
            and not any(
                _contains_phrase(combined, signal)
                for signal in personal_signals
            )
        ):
            return 0.0, []

    if plan.display_query == "communication":
        clinical_senses = (
            "communication disorders",
            "communicative disorders",
        )
        if any(_contains_phrase(combined, sense) for sense in clinical_senses):
            return 0.0, []
        technical_senses = (
            "interstellar communication",
            "animal communication",
            "data communication",
            "telecommunication",
            "communications engineering",
            "communication systems",
        )
        human_signals = (
            "interpersonal communication",
            "public speaking",
            "human communication",
            "business communication",
            "communication skills",
            "conversation",
            "persuasion",
            "rhetoric",
        )
        if (
            any(_contains_phrase(combined, sense) for sense in technical_senses)
            and not any(
                _contains_phrase(combined, signal)
                for signal in human_signals
            )
        ):
            return 0.0, []

    if plan.display_query in {"startup", "startups"} and any(
        _contains_phrase(combined, sense)
        for sense in ("juvenile literature", "children s literature", "for kids")
    ):
        return 0.0, []

    if plan.display_query == "investing":
        unrelated_senses = (
            "capital punishment",
            "death row",
            "juvenile justice",
            "operations research",
            "industrial engineering",
            "manufacturing",
        )
        financial_signals = (
            "portfolio",
            "personal finance",
            "financial markets",
            "investment strategy",
            "asset allocation",
            "securities",
            "stocks",
            "bonds",
        )
        if (
            any(_contains_phrase(combined, sense) for sense in unrelated_senses)
            and not any(
                _contains_phrase(combined, signal)
                for signal in financial_signals
            )
        ):
            return 0.0, []

    if plan.display_query == "habits" and _contains_phrase(
        title,
        "habits of the heart",
    ):
        return 0.0, []

    if plan.display_query == "mental health" and any(
        _contains_phrase(combined, sense)
        for sense in (
            "capital punishment", "death row", "insanity law",
            "trials murder", "eligible for execution",
        )
    ):
        return 0.0, []

    if plan.display_query == "writing" and _contains_phrase(
        title,
        "writing women in",
    ):
        return 0.0, []

    if plan.display_query == "health" and _contains_phrase(
        combined,
        "health sciences information sources",
    ):
        return 0.0, []

    evidence = 0.0
    matched_subject = ""
    matched_terms: list[str] = []
    original = normalize_text(plan.display_query)
    for index, term in enumerate(plan.queries):
        normalized_term = normalize_text(term)
        if not normalized_term:
            continue
        weight = 1.0 if index == 0 else 0.72
        term_score = 0.0
        if title == normalized_term:
            term_score = max(term_score, 9.0)
        elif _contains_phrase(title, normalized_term):
            term_score = max(term_score, 6.5)
        for raw_subject, subject in subjects:
            if subject == normalized_term:
                term_score = max(term_score, 7.5)
                matched_subject = matched_subject or raw_subject
            elif _contains_phrase(subject, normalized_term):
                term_score = max(term_score, 5.0)
                matched_subject = matched_subject or raw_subject
        if _contains_phrase(description, normalized_term):
            term_score = max(term_score, 3.0)
        if normalized_term in candidate.semantic_terms:
            term_score = max(term_score, 5.5)
        if term_score:
            evidence += term_score * weight
            matched_terms.append(normalized_term)

    # Multi-word user topics also get token coverage, but one accidental token
    # can never pass the gate by itself.
    if len(plan.tokens) > 1:
        combined_tokens = set(combined.split())
        covered = sum(1 for token in plan.tokens if token in combined_tokens)
        coverage = covered / len(plan.tokens)
        if coverage >= 2 / 3:
            evidence += 4.0 * coverage
        elif not matched_terms:
            return 0.0, []
    if not matched_terms and not _contains_phrase(combined, original):
        return 0.0, []
    if matched_subject:
        labels = [f"Subject: {matched_subject}"]
    elif matched_terms:
        labels = [f"Related: {matched_terms[0]}"]
    else:
        labels = []
    return evidence, labels


def _quality_score(candidate: DiscoveryCandidate) -> float:
    score = 0.0
    score += min(math.log1p(candidate.readinglog_count) * 0.9, 8.0)
    score += min(math.log1p(candidate.osp_count) * 0.7, 5.0)
    score += min(math.log1p(candidate.ratings_count) * 0.55, 4.0)
    if candidate.ratings_count >= 3:
        score += max(candidate.ratings_average - 2.5, 0) * 0.9
    score += min(math.log1p(candidate.edition_count) * 0.18, 1.3)
    score += min(math.log1p(candidate.popularity) * 0.25, 1.5)
    score += 0.45 if candidate.cover_id else 0.0
    return score


def _nyt_quality_score(candidate: DiscoveryCandidate) -> float:
    if candidate.nyt_rank:
        # Historical number-one status remains a bounded tiebreaker inside the
        # primary relevance tier and can never admit an off-topic book.
        weeks_signal = min(
            math.log1p(candidate.nyt_weeks_at_number_one) * 0.16,
            0.55,
        )
        return min(1.65 + weeks_signal, 2.2)
    return 0.0


def _result_reasons(
    candidate: DiscoveryCandidate,
    evidence_labels: Sequence[str],
    sources: Sequence[str],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if candidate.nyt_rank:
        reasons.append("NYT #1 bestseller")
    if evidence_labels:
        label = re.sub(r"\s+", " ", str(evidence_labels[0])).strip()
        if label:
            prefix, separator, value = label.partition(":")
            if separator:
                reasons.append(f"{prefix.title()}: {value.strip()[:42].title()}")
            else:
                reasons.append(f"Related: {label[:42].title()}")
    discovery_sources = set(sources).intersection({"openlibrary", "inventaire"})
    if len(discovery_sources) > 1:
        reasons.append("Matched by multiple sources")
    if candidate.readinglog_count >= 500:
        reasons.append("Widely read")
    elif candidate.osp_count >= 20:
        reasons.append("Frequently assigned")
    elif candidate.ratings_count >= 20 and candidate.ratings_average >= 4:
        reasons.append("Highly rated")
    return tuple(dict.fromkeys(reasons))[:2]


def merge_topic_candidates(
    pages: Iterable[ProviderPage],
    plan: QueryPlan,
    *,
    limit: int = 96,
    author_cap: int = 2,
    require_openlibrary: bool = True,
) -> list[DiscoveryResult]:
    groups: dict[tuple[str, ...], DiscoveryCandidate] = {}
    group_sources: dict[tuple[str, ...], set[str]] = {}
    group_rrf: dict[tuple[str, ...], float] = {}
    provider_weights = {"openlibrary": 1.0, "inventaire": 0.7}
    for page in pages:
        if not page.available:
            continue
        for candidate in page.candidates[:MAX_PROVIDER_RECORDS]:
            key = _identity_key(candidate)
            if not key:
                continue
            if require_openlibrary and not candidate.work_key:
                continue
            groups[key] = _merge_candidates(groups[key], candidate) if key in groups else candidate
            group_sources.setdefault(key, set()).add(candidate.provider)
            weight = provider_weights.get(candidate.provider, 0.5)
            query_weight = 1.0 / (1.0 + 0.2 * max(candidate.query_rank, 0))
            group_rrf[key] = group_rrf.get(key, 0.0) + (
                weight * query_weight / (RRF_K + max(candidate.native_rank, 1))
            )

    ranked: list[tuple[float, float, float, str, DiscoveryResult]] = []
    for key, candidate in groups.items():
        evidence, labels = _topic_evidence(candidate, plan)
        if evidence <= 0:
            continue
        source_set = set(group_sources.get(key, ()))
        discovery_sources = source_set.intersection({"openlibrary", "inventaire"})
        consensus = 1.4 if len(discovery_sources) > 1 else 0.0
        sources = tuple(sorted(source_set))
        ranking_sources = ("nyt_wikipedia",) if candidate.nyt_rank else ()
        secondary = (
            min(_quality_score(candidate), 12.0)
            + _nyt_quality_score(candidate)
            + consensus
            + group_rrf.get(key, 0.0) * 100
        )
        # Relevance is the primary sort tier. Half-point buckets keep tiny
        # arithmetic differences between equally relevant books from defeating
        # materially stronger quality evidence.
        evidence_tier = round(evidence * 2) / 2
        score = evidence * 100 + secondary
        result = DiscoveryResult(
            candidate=candidate,
            score=score,
            reasons=_result_reasons(candidate, labels, sources),
            sources=sources,
            ranking_sources=ranking_sources,
        )
        ranked.append((
            evidence_tier,
            secondary,
            evidence,
            normalize_text(candidate.title),
            result,
        ))

    ranked.sort(key=lambda item: (
        -item[0], -item[1], -item[2], item[3], item[4].candidate.work_key,
    ))
    selected: list[DiscoveryResult] = []
    author_counts: dict[str, int] = {}
    seen_title_authors: set[tuple[str, str]] = set()
    for _, _, _, _, result in ranked:
        author = normalize_text(result.candidate.authors[0] if result.candidate.authors else "")
        normalized_title = re.sub(
            r"^(?:the|a|an) ",
            "",
            normalize_text(result.candidate.title),
        )
        title_author = (normalized_title, author)
        if author and title_author in seen_title_authors:
            continue
        if author and author_counts.get(author, 0) >= max(author_cap, 1):
            continue
        selected.append(result)
        if author:
            author_counts[author] = author_counts.get(author, 0) + 1
            seen_title_authors.add(title_author)
        if len(selected) >= min(max(limit, 1), 200):
            break
    return selected


def candidate_to_book(result: DiscoveryResult) -> dict[str, Any]:
    candidate = result.candidate
    book = {
        "title": candidate.title,
        "author": candidate.authors[0] if candidate.authors else "",
        "ol_key": candidate.work_key,
        "cover_id": candidate.cover_id,
        "cover_hash": candidate.cover_hash,
        "archive_id": candidate.archive_id,
        "cover_url": "",
        "published_year": candidate.published_year,
        "languages": list(candidate.languages),
        # Semantic provider claims are also useful recommendation seeds when
        # the mapped work has no Open Library subjects available yet.
        "subjects": list((candidate.subjects or candidate.semantic_terms)[:8]),
        "description": _bounded_description(candidate.description, RESULT_DESCRIPTION_MAX),
        "fiction": candidate.fiction,
        "reasons": list(result.reasons),
        "reason": result.reasons[0] if result.reasons else "",
        "sources": list(result.sources),
        "ranking_sources": list(result.ranking_sources),
        "score": round(result.score, 4),
    }
    if candidate.nyt_rank:
        book["nyt_number_one"] = {
            "rank": candidate.nyt_rank,
            "weeks_at_number_one": candidate.nyt_weeks_at_number_one,
            "lists": list(candidate.nyt_list_names),
            "latest_issue_date": candidate.nyt_published_date,
        }
    return book


def filter_topic_results(
    results: Sequence[DiscoveryResult],
    *,
    book_type: str = "any",
    language: str = "current",
    current_language: str = "en",
    published: str = "any",
    sort: str = "best",
    author_cap: int | None = None,
) -> list[DiscoveryResult]:
    wanted_language = {
        "en": {"eng", "en"},
        "cn": {"chi", "zho", "zh", "cn"},
    }.get(current_language if language == "current" else language)
    recent_cutoff = date.today().year - 10
    output: list[DiscoveryResult] = []
    for result in results:
        candidate = result.candidate
        if book_type == "fiction" and candidate.fiction is not True:
            continue
        if book_type == "nonfiction":
            if candidate.fiction is True:
                continue
            # Inventaire text search does not expose enough type metadata to
            # distinguish a topical novel from nonfiction. Keep semantically
            # claimed works, but do not let raw title matches defeat an
            # explicit nonfiction filter.
            if (
                candidate.fiction is None
                and candidate.provider == "inventaire"
                and not candidate.semantic_terms
            ):
                continue
        if wanted_language:
            if not candidate.languages:
                if language in {"en", "cn"}:
                    continue
            elif not wanted_language.intersection(
                normalize_text(item) for item in candidate.languages
            ):
                continue
        if published == "recent" and (
            not candidate.published_year or candidate.published_year < recent_cutoff
        ):
            continue
        if published == "classic" and (
            not candidate.published_year or candidate.published_year > 1990
        ):
            continue
        output.append(result)
    if sort == "newest":
        output.sort(key=lambda item: (-(item.candidate.published_year or 0), -item.score, normalize_text(item.candidate.title)))
    if author_cap is not None:
        selected: list[DiscoveryResult] = []
        author_counts: dict[str, int] = {}
        cap = max(int(author_cap), 1)
        for result in output:
            author = normalize_text(
                result.candidate.authors[0] if result.candidate.authors else ""
            )
            if author and author_counts.get(author, 0) >= cap:
                continue
            selected.append(result)
            if author:
                author_counts[author] = author_counts.get(author, 0) + 1
        output = selected
    return output
