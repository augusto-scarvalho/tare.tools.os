"""EXP-18 shadow-mode merge-candidate detector (stdlib only, MEASURE-ONLY).

The reduce's string dedup key (title+category+evidence, exact) never sees
paraphrases: the same idea from 5 workers stays 5 findings. This module PROPOSES
candidate merge pairs as advisory data (manuscript prop. 22: semantic repair
proposes; a deterministic validator + owner commit). It mutates nothing in the
reduce -- the emit that calls it is additive and fail-open.

Two-stage lexical detection over a per-finding basis (title + category + all
evidence, lowercased, whitespace-collapsed):
  stage 1  cheap token-set Jaccard prefilter (drop obviously-unrelated pairs)
  stage 2  2-of-3 agreement across shingle Jaccard, shingle containment, and
           token TF-IDF cosine (corpus = every finding's basis in the wave)

Encoding-tolerant by contract: findings can carry mojibake'd non-ASCII corpus
text, so the basis is treated as-is (lowercased only, never re-encoded).
"""
from __future__ import annotations

import math
import re
from typing import Any

# Thresholds are the EXP-18 hypothesis under test -- pinned as constants so a
# scenario catches silent drift. Stage-1 prefilter, then 2-of-3 agreement.
T_PREFILTER_JACCARD = 0.15  # token-set Jaccard, stage 1
T_JACCARD = 0.35            # 3-word-shingle Jaccard
T_CONTAINMENT = 0.60        # 3-word-shingle containment |A n B| / min(|A|,|B|)
T_COSINE = 0.50             # token TF-IDF cosine
SHINGLE_K = 3
MAX_PAIRS = 50

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    # \w is unicode-aware in Python 3, so mojibake'd accented bytes still tokenize
    # as-is -- no re-encode (encoding-tolerant contract).
    return _WORD_RE.findall(text.lower())


def _basis(finding: dict[str, Any]) -> str:
    """Lowercased, whitespace-collapsed join of title + category + all evidence.

    Evidence is the load-bearing part -- paraphrases of one idea share their
    anchors there far more than in their (deliberately varied) titles.
    """
    parts = [str(finding.get("title", "")), str(finding.get("category", ""))]
    evidence = finding.get("evidence", [])
    if isinstance(evidence, list):
        parts.extend(str(e) for e in evidence)
    return " ".join(" ".join(parts).split()).lower()


def shingles(text: str, k: int = SHINGLE_K) -> set[tuple[str, ...]]:
    """Word k-shingles as a set of k-tuples. Texts shorter than k collapse to a
    single tuple of all their words so short findings stay comparable."""
    words = _tokens(text)
    if len(words) < k:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + k]) for i in range(len(words) - k + 1)}


def jaccard(a: set, b: set) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def containment(a: set, b: set) -> float:
    smaller = min(len(a), len(b))
    return len(a & b) / smaller if smaller else 0.0


def tfidf_cosine(i: int, j: int, corpus: list[str]) -> float:
    """TF-IDF cosine between corpus[i] and corpus[j]; IDF over the whole corpus.

    ponytail: rebuilds the corpus DF on every call -- O(corpus) per pair. Fine
    for a wave (tens of findings, <=50 kept pairs); precompute the DF once if a
    wave ever carries hundreds of findings.
    """
    docs = [_tokens(c) for c in corpus]
    n = len(docs)
    df: dict[str, int] = {}
    for doc in docs:
        for tok in set(doc):
            df[tok] = df.get(tok, 0) + 1

    def vec(doc: list[str]) -> dict[str, float]:
        tf: dict[str, int] = {}
        for tok in doc:
            tf[tok] = tf.get(tok, 0) + 1
        return {tok: c * (math.log((n + 1) / (df.get(tok, 0) + 1)) + 1) for tok, c in tf.items()}

    vi, vj = vec(docs[i]), vec(docs[j])
    dot = sum(vi[t] * vj[t] for t in (vi.keys() & vj.keys()))
    ni = math.sqrt(sum(v * v for v in vi.values()))
    nj = math.sqrt(sum(v * v for v in vj.values()))
    return dot / (ni * nj) if ni and nj else 0.0


def candidate_pairs(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Advisory merge-candidate pairs. Deterministic order, capped at MAX_PAIRS.

    A pair is a candidate iff it clears the stage-1 token-set prefilter AND at
    least 2 of the 3 stage-2 signals cross threshold.
    """
    bases = [_basis(f) for f in findings]
    titles = [str(f.get("title", "")) for f in findings]
    token_sets = [set(_tokens(b)) for b in bases]
    shingle_sets = [shingles(b) for b in bases]
    rows: list[dict[str, Any]] = []
    n = len(findings)
    for a in range(n):
        for b in range(a + 1, n):
            if jaccard(token_sets[a], token_sets[b]) < T_PREFILTER_JACCARD:
                continue
            jac = jaccard(shingle_sets[a], shingle_sets[b])
            con = containment(shingle_sets[a], shingle_sets[b])
            cos = tfidf_cosine(a, b, bases)
            agree = int(jac >= T_JACCARD) + int(con >= T_CONTAINMENT) + int(cos >= T_COSINE)
            if agree >= 2:
                rows.append({
                    "i": a, "j": b, "titleI": titles[a], "titleJ": titles[b],
                    "scores": {"jaccard": round(jac, 4), "containment": round(con, 4), "cosine": round(cos, 4)},
                    "agree": agree,
                })
    rows.sort(key=lambda r: (
        -r["agree"],
        -(r["scores"]["jaccard"] + r["scores"]["containment"] + r["scores"]["cosine"]),
        r["i"], r["j"],
    ))
    return rows[:MAX_PAIRS]


def _self_check() -> None:
    # The EXP-15 manual-truth pair: same idea (mock m4_status_html's real HTTP
    # server), deliberately varied titles, one shared evidence anchor. MUST be
    # detected. A clearly-distinct finding MUST NOT pair with anything.
    shared_evidence = [
        "m4_status_html boots a real HTTP server costing 59-73s per gate run",
        "the render output can be validated against a mock instead of a live server",
    ]
    truth_a = {
        "title": "OPCAO-3: Mock HTTP para m4_status_html elimina 59-73s de servidor real",
        "category": "performance",
        "evidence": shared_evidence,
    }
    truth_b = {
        "title": "Analogy: flight simulator vs wind tunnel - mock vs real for m4_status_html (59-73s HTTP server)",
        "category": "performance",
        "evidence": shared_evidence,
    }
    distinct = {
        "title": "Add rate limiting to the login endpoint",
        "category": "security",
        "evidence": [
            "brute-force attempts on POST /login are unthrottled",
            "use a token-bucket limiter keyed per client IP address",
        ],
    }
    pairs = candidate_pairs([truth_a, truth_b, distinct])
    hit = [p for p in pairs if {p["i"], p["j"]} == {0, 1}]
    assert hit and hit[0]["agree"] >= 2, f"truth pair not detected: {pairs}"
    assert not any(2 in {p["i"], p["j"]} for p in pairs), f"distinct finding paired: {pairs}"

    # thresholds pinned (drift = failed self-check)
    assert (T_PREFILTER_JACCARD, T_JACCARD, T_CONTAINMENT, T_COSINE) == (0.15, 0.35, 0.60, 0.50)
    # metric sanity
    assert jaccard({1, 2}, {2, 3}) == 1 / 3
    assert containment({1, 2}, {1, 2, 3, 4}) == 1.0
    print(f"OK merge_candidates self-check: truth pair agree={hit[0]['agree']} "
          f"scores={hit[0]['scores']}, distinct silent, thresholds pinned")


if __name__ == "__main__":
    _self_check()
