"""Deterministic weighted Personalized PageRank without a heavy graph dependency."""
from __future__ import annotations


def pagerank(edges, personalization=None, alpha=0.85, tolerance=1e-9, max_iterations=100):
    nodes = sorted(edges)
    if not nodes:
        return {}
    weights = {n: max(0.0, (personalization or {}).get(n, 0.0)) for n in nodes}
    total = sum(weights.values())
    p = {n: weights[n] / total if total else 1 / len(nodes) for n in nodes}
    rank = dict(p)
    outgoing = {n: sum(w for target, w in edges[n].items() if target in p and w > 0) for n in nodes}
    for _ in range(max_iterations):
        dangling = sum(rank[n] for n in nodes if not outgoing[n])
        updated = {n: (1 - alpha + alpha * dangling) * p[n] for n in nodes}
        for source in nodes:
            if outgoing[source]:
                for target, weight in edges[source].items():
                    if target in updated and weight > 0:
                        updated[target] += alpha * rank[source] * weight / outgoing[source]
        error = sum(abs(updated[n] - rank[n]) for n in nodes)
        rank = updated
        if error < tolerance:
            break
    # A bounded iteration result remains a valid deterministic approximate rank.
    return rank
