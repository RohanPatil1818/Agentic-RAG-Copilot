
"""Cache tests. No real Redis needed: fakeredis for the exact tier, a small fake for the semantic tier."""
import re
 
import fakeredis
import pytest
 
import app.services.semantic_cache as sc
 
 
class FakeSemantic:
    """Stands in for RedisVL SemanticCache; matches any question sharing a keyword, honouring the KB-version filter."""
    def __init__(self):
        self.rows = []
 
    def store(self, prompt, response, metadata, filters, ttl):
        self.rows.append({"prompt": prompt, "response": response, "metadata": metadata,
                          "version": filters["kb_version"], "vector_distance": 0.05})
 
    def check(self, prompt, num_results, filter_expression, return_fields):
        version = re.search(r"\{(.*)\}", str(filter_expression)).group(1)
        words = set(prompt.lower().split())
        for r in self.rows:
            if r["version"] == version and words & set(r["prompt"].lower().split()) & {"vpn"}:
                return [r]
        return []
 
 
@pytest.fixture(autouse=True)
def cache(monkeypatch):
    monkeypatch.setattr(sc, "_redis", fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(sc, "_semantic", FakeSemantic())
    monkeypatch.setattr(sc, "_down_until", 0.0)
    monkeypatch.setattr(sc.settings, "cache_enabled", True)
    monkeypatch.setattr(sc.settings, "cache_ttl_kb_seconds", 60)
    monkeypatch.setattr(sc.settings, "cache_ttl_web_seconds", 10)
    monkeypatch.setattr(sc.settings, "cache_breaker_seconds", 30)
 
 
def make_ask(source="private_kb"):
    calls = {"n": 0}
 
    def ask(question):
        calls["n"] += 1
        return {"answer": "Use the self-service portal.", "source_used": source,
                "citations": [{"title": "runbook.md"}], "trace": ["t"]}
    return ask, calls
 
 
def test_repeated_question_hits_exact_layer():
    ask, calls = make_ask()
    first = sc.ask_with_cache("How do I reset my VPN password?", ask)
    second = sc.ask_with_cache("  how do i reset my vpn password ", ask)   # different spacing, case, punctuation
    assert first["cache"]["status"] == "miss"
    assert second["cache"]["layer"] == "exact" and calls["n"] == 1
 
 
def test_hit_replays_original_trace_and_source():
    ask, _ = make_ask()
    sc.ask_with_cache("How do I reset my VPN password?", ask)
    hit = sc.ask_with_cache("How do I reset my VPN password?", ask)
    assert hit["trace"][0].startswith("Cache HIT")
    assert "↺ t" in hit["trace"]                    # original step replayed, marked as cached
    assert hit["source_used"] == "private_kb"       # the UI shows where the cached answer came from
 
 
def test_miss_trace_starts_with_cache_lookup():
    ask, _ = make_ask()
    miss = sc.ask_with_cache("How do I reset my VPN password?", ask)
    assert miss["trace"][0].startswith("Cache lookup")
 
 
def test_rephrased_question_hits_semantic_layer():
    ask, calls = make_ask()
    sc.ask_with_cache("How do I reset my VPN password?", ask)
    hit = sc.ask_with_cache("VPN password forgotten, what now?", ask)
    assert hit["cache"]["layer"] == "semantic" and calls["n"] == 1
    assert hit["citations"] == [{"title": "runbook.md"}]
 
 
def test_ingestion_makes_old_entries_unreachable():
    ask, calls = make_ask()
    sc.ask_with_cache("How do I reset my VPN password?", ask)
    sc.bump_kb_version()                                   # a document was ingested
    again = sc.ask_with_cache("How do I reset my VPN password?", ask)
    assert again["cache"]["status"] == "miss" and calls["n"] == 2
 
 
@pytest.mark.parametrize("source", ["direct", "insufficient_evidence"])
def test_non_cacheable_results_are_never_stored(source):
    ask, calls = make_ask(source)
    sc.ask_with_cache("hello there", ask)
    sc.ask_with_cache("hello there", ask)
    assert calls["n"] == 2
 
 
def test_redis_failure_never_breaks_the_answer(monkeypatch):
    class Broken:
        def __getattr__(self, name):
            raise ConnectionError("redis down")
    monkeypatch.setattr(sc, "_redis", Broken())
    ask, calls = make_ask()
    result = sc.ask_with_cache("How do I reset my VPN password?", ask)
    assert result["answer"] and calls["n"] == 1
    assert sc._breaker_open()                              # and it stops hammering Redis
 
 
def test_ttl_is_set_with_jitter():
    ask, _ = make_ask()
    sc.ask_with_cache("How do I reset my VPN password?", ask)
    ttl = sc._client().ttl(sc._exact_key(sc.kb_version(), "How do I reset my VPN password?"))
    assert 60 <= ttl <= 66                                  # 60 s base + up to 10% jitter
 

