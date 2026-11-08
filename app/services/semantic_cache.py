
"""Two-tier answer cache for the Agentic-RAG graph.
 
L1 = exact match (normalised question hash, no embedding, ~1 ms)
L2 = semantic match (RedisVL SemanticCache, bge-small embedding + vector search)
 
Design rules:
  * Fail open: any Redis problem means "miss", never a failed request.
  * Entries are scoped to a KB version; ingesting a document bumps the version,
    which instantly orphans every older entry (they then expire via TTL).
  * Only answers grounded in the KB / web are cached; greetings and
    "insufficient evidence" answers are not.
"""
import hashlib
import json
import logging
import random
import re
import threading
import time
 
import redis
from prometheus_client import Counter, Histogram
from redisvl.extensions.cache.llm import SemanticCache
from redisvl.query.filter import Tag
from redisvl.utils.vectorize import CustomTextVectorizer
 
from app.core.config import get_settings
from app.rag.vectorstore import get_embeddings
 
logger = logging.getLogger(__name__)
settings = get_settings()
 
# ---- metrics ---------------------------------------------------------------
CACHE_REQUESTS = Counter("rag_cache_requests_total", "Cache lookups", ["outcome", "layer"])
CACHE_ERRORS = Counter("rag_cache_errors_total", "Cache errors (fail-open)", ["op"])
CACHE_STORES = Counter("rag_cache_stores_total", "Entries written", ["source"])
CACHE_LOOKUP_SECONDS = Histogram("rag_cache_lookup_seconds", "Cache lookup latency", ["layer"],
                                 buckets=(.002, .005, .01, .025, .05, .1, .25, .5, 1))
CACHE_DISTANCE = Histogram("rag_cache_hit_distance", "Cosine distance of semantic hits",
                           buckets=(.01, .02, .03, .04, .05, .06, .07, .08, .10, .12, .15))
REQUEST_SECONDS = Histogram("rag_request_seconds", "End-to-end /chat latency", ["cache"],
                            buckets=(.05, .1, .25, .5, 1, 2, 4, 8, 16, 32))
 
PROMPT_VERSION = "1"          # bump this whenever you edit the prompts in workflow.py

_redis = None
_semantic = None
_inflight: dict = {}          # question -> [lock, waiting count]  (single-flight)
_inflight_guard = threading.Lock()
_down_until = 0.0          # tiny circuit breaker: skip Redis for a while after a failure
 
 
def _client() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                      socket_timeout=0.25, socket_connect_timeout=0.25)
    return _redis
 
 
def _semantic_cache() -> SemanticCache:
    global _semantic
    if _semantic is None:
        vectorizer = CustomTextVectorizer(embed=lambda text: get_embeddings().embed_query(text))
        _semantic = SemanticCache(
            name="rag_answer_cache",
            distance_threshold=settings.cache_distance_threshold,
            ttl=max(settings.cache_ttl_kb_seconds, settings.cache_ttl_web_seconds),
            vectorizer=vectorizer,
            filterable_fields=[{"name": "kb_version", "type": "tag"},
                               {"name": "source_used", "type": "tag"}],
            redis_url=settings.redis_url,
            connection_kwargs={"socket_timeout": 0.25, "socket_connect_timeout": 0.25},
        )
    return _semantic
 
 
def _breaker_open() -> bool:
    return time.monotonic() < _down_until
 
 
def _trip(op: str, exc: Exception) -> None:
    global _down_until
    CACHE_ERRORS.labels(op).inc()
    _down_until = time.monotonic() + settings.cache_breaker_seconds
    logger.warning("Cache %s failed (%s); bypassing cache for %ss", op, exc, settings.cache_breaker_seconds)
 
 
# ---- helpers ---------------------------------------------------------------
def normalize(question: str) -> str:
    q = re.sub(r"\s+", " ", question.strip().lower())
    return q.rstrip("?!. ")
 
 
def _fingerprint() -> str:
    """Answers depend on the LLM and embedding model; changing either must orphan old entries."""
    return hashlib.sha1(f"{settings.groq_model}|{settings.embeddings_model}|{PROMPT_VERSION}".encode()).hexdigest()[:6]


def kb_version() -> str:
    # letters + digits only, so the value is safe inside a RediSearch tag filter
    return f"{_client().get('rag:kb_version') or '0'}x{_fingerprint()}"
 
 
def bump_kb_version() -> None:
    """Call after any ingestion. Older cache entries become unreachable immediately."""
    try:
        _client().incr("rag:kb_version")
    except Exception as exc:
        _trip("bump_version", exc)
 
 
def _ttl_for(source: str) -> int | None:
    base = {"private_kb": settings.cache_ttl_kb_seconds,
            "web_search": settings.cache_ttl_web_seconds}.get(source)
    if base is None:
        return None
    return int(base * random.uniform(1.0, 1.1))      # jitter: avoid mass expiry at one instant
 
 
def _exact_key(version: str, question: str) -> str:
    return f"rag:exact:{version}:{hashlib.sha256(normalize(question).encode()).hexdigest()}"
 
 
# ---- public API ------------------------------------------------------------
def lookup(question: str) -> dict | None:
    """Return a cached result dict (with 'cache' metadata) or None."""
    if not settings.cache_enabled or _breaker_open():
        return None
    try:
        version = kb_version()
 
        t = time.perf_counter()
        raw = _client().get(_exact_key(version, question))
        CACHE_LOOKUP_SECONDS.labels("exact").observe(time.perf_counter() - t)
        if raw:
            CACHE_REQUESTS.labels("hit", "exact").inc()
            return _build(json.loads(raw), "exact", 0.0)
 
        t = time.perf_counter()
        hits = _semantic_cache().check(
            prompt=question,
            num_results=1,
            filter_expression=(Tag("kb_version") == version),
            return_fields=["response", "metadata", "vector_distance", "inserted_at", "prompt"],
        )
        CACHE_LOOKUP_SECONDS.labels("semantic").observe(time.perf_counter() - t)
        if hits:
            hit = hits[0]
            meta = hit.get("metadata") or {}
            if isinstance(meta, str):
                meta = json.loads(meta)
            payload = {**meta, "answer": hit["response"]}
            dist = float(hit.get("vector_distance", 0.0))
            CACHE_DISTANCE.observe(dist)
            logger.info("semantic hit q=%r matched=%r distance=%.3f", question, hit.get("prompt"), dist)
            CACHE_REQUESTS.labels("hit", "semantic").inc()
            return _build(payload, "semantic", dist)
 
        CACHE_REQUESTS.labels("miss", "none").inc()
        return None
    except Exception as exc:                      # fail open
        _trip("lookup", exc)
        CACHE_REQUESTS.labels("error", "none").inc()
        return None
 
 
def store(question: str, result: dict, latency_ms: float) -> None:
    source = result.get("source_used", "")
    ttl = _ttl_for(source)
    if not settings.cache_enabled or ttl is None or not result.get("answer") or _breaker_open():
        return
    try:
        version = kb_version()
        payload = {"answer": result["answer"], "source_used": source,
                   "citations": result.get("citations", []), "trace": result.get("trace", []),
                   "created_at": time.time(), "original_latency_ms": round(latency_ms)}
        _client().set(_exact_key(version, question), json.dumps(payload), ex=ttl)
        _semantic_cache().store(
            prompt=question,
            response=result["answer"],
            metadata={k: v for k, v in payload.items() if k != "answer"},
            filters={"kb_version": version, "source_used": source},
            ttl=ttl,
        )
        CACHE_STORES.labels(source).inc()
    except Exception as exc:
        _trip("store", exc)
 
 
def _build(payload: dict, layer: str, distance: float) -> dict:
    age = max(0, int(time.time() - payload.get("created_at", time.time())))
    return {
        "answer": payload["answer"],
        "source_used": payload.get("source_used", "private_kb"),
        "citations": payload.get("citations", []),
        "trace": [f"Cache HIT ({layer}, distance={distance:.3f}, age={age}s) → skipped retrieval + LLM calls"]
                 + [f"↺ {t}" for t in payload.get("trace", [])],      # replay the original steps, marked as cached
        "current_query": None,
        "cache": {"status": "hit", "layer": layer, "distance": round(distance, 4), "age_seconds": age,
                  "saved_latency_ms": payload.get("original_latency_ms")},
    }
 
 
def _exact_only(question: str) -> dict | None:
    """Cheap re-check used by requests that waited behind an identical in-flight question."""
    if not settings.cache_enabled or _breaker_open():
        return None
    try:
        raw = _client().get(_exact_key(kb_version(), question))
        if raw:
            CACHE_REQUESTS.labels("coalesced", "exact").inc()
            return _build(json.loads(raw), "exact", 0.0)
    except Exception as exc:
        _trip("lookup", exc)
    return None


def ask_with_cache(question: str, ask_fn) -> dict:
    """Drop-in wrapper around workflow.ask()."""
    t0 = time.perf_counter()
    hit = lookup(question)
    if hit:
        REQUEST_SECONDS.labels("hit").observe(time.perf_counter() - t0)
        hit["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        return hit
    # Single-flight: identical questions arriving together share ONE agent run.
    key = normalize(question)
    with _inflight_guard:
        entry = _inflight.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            hit = _exact_only(question)              # a sibling request may have just filled the cache
            if hit:
                REQUEST_SECONDS.labels("hit").observe(time.perf_counter() - t0)
                hit["latency_ms"] = round((time.perf_counter() - t0) * 1000)
                return hit
            result = ask_fn(question)
            elapsed = time.perf_counter() - t0
            store(question, result, elapsed * 1000)
    finally:
        with _inflight_guard:
            entry[1] -= 1
            if entry[1] == 0:
                _inflight.pop(key, None)
    REQUEST_SECONDS.labels("miss").observe(elapsed)
    result["cache"] = {"status": "miss"}
    if settings.cache_enabled:
        result["trace"] = ["Cache lookup → no match, running full agent"] + list(result.get("trace", []))
    result["latency_ms"] = round(elapsed * 1000)
    return result
 



