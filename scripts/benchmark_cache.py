
"""Measure real cache numbers against a running app.
 
Start cold first:   docker compose exec redis redis-cli INCR rag:kb_version
                    (do NOT use FLUSHALL: it also deletes the search index the cache needs)
Run (from project root):  python scripts/benchmark_cache.py http://localhost:8000
Edit QUESTIONS / PARAPHRASES: use 30+ real questions from your audit log, and a reworded twin of each.
"""
import statistics
import sys
 
import requests
 
QUESTIONS = [
    "How do I reset my VPN password?",
    "What is the laptop setup process for a new joiner?",
    "How do I set up MFA on my phone?",
]
PARAPHRASES = [
    "I forgot my VPN password, what should I do?",
    "How is a new employee's laptop configured?",
    "Steps to enable multi-factor authentication on my mobile",
]
 
 
def phase(base: str, name: str, questions: list[str]) -> None:
    times, hits, layers = [], 0, {}
    for q in questions:
        r = requests.post(f"{base}/api/chat", json={"question": q}, timeout=120)
        r.raise_for_status()
        body = r.json()
        times.append(body.get("latency_ms") or 0)
        cache = body.get("cache") or {}
        if cache.get("status") == "hit":
            hits += 1
            layers[cache["layer"]] = layers.get(cache["layer"], 0) + 1
    times.sort()
    p95 = times[min(len(times) - 1, int(0.95 * len(times)))]
    print(f"{name:<12} n={len(times):<3} hits={hits:<3} p50={statistics.median(times):>7.0f} ms  p95={p95:>7.0f} ms  layers={layers}")
 
 
if __name__ == "__main__":
    base = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
    phase(base, "cold", QUESTIONS)              # all misses: your real pipeline latency
    phase(base, "warm", QUESTIONS)              # same text: exact hits
    phase(base, "reworded", PARAPHRASES)        # semantic hits (or misses if the threshold is strict)


