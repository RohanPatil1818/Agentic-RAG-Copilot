
"""Pick the cache distance threshold from YOUR labelled question pairs.
 
Usage:  python scripts/calibrate_cache.py data/cache_pairs.json
pairs file: [{"a": "How do I reset my VPN password?", "b": "forgot vpn password, what now", "same": true},
             {"a": "reset my VPN password", "b": "reset my email password", "same": false}, ...]
Aim for 200+ pairs, with many near-miss NEGATIVES (same words, different intent).
"""
import json
import sys
 
import numpy as np
 
from app.rag.vectorstore import get_embeddings
 
 
def main(path: str) -> None:
    pairs = json.load(open(path, encoding="utf-8"))
    emb = get_embeddings()
    a = np.array(emb.embed_documents([p["a"] for p in pairs]))
    b = np.array(emb.embed_documents([p["b"] for p in pairs]))
    a /= np.linalg.norm(a, axis=1, keepdims=True)
    b /= np.linalg.norm(b, axis=1, keepdims=True)
    dist = 1 - (a * b).sum(axis=1)                       # cosine distance, same unit as RedisVL
    same = np.array([p["same"] for p in pairs])
 
    print(f"{'threshold':>9} {'precision':>9} {'recall':>7} {'false hits':>10}")
    best = None
    for t in np.arange(0.02, 0.25, 0.01):
        hit = dist <= t
        tp, fp = int((hit & same).sum()), int((hit & ~same).sum())
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / max(int(same.sum()), 1)
        print(f"{t:9.2f} {precision:9.3f} {recall:7.3f} {fp:10d}")
        if precision >= 0.98:
            best = t                                        # largest threshold that still keeps >=98% precision
    print("\nRecommended CACHE_DISTANCE_THRESHOLD:", f"{best:.2f}" if best else "none reaches 98% precision; use 0.05 and add exact-match only")
 
 
if __name__ == "__main__":
    main(sys.argv[1])
 

