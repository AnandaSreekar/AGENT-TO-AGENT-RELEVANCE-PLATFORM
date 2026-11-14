"""Relevance Lab: evaluate three routing strategies on the labelled dataset with ONE methodology.

Methodology (identical for every strategy):
  * Evaluable unit = (post, recipient agent) pair, excluding each post's own author  -> 15 x 5 = 75 pairs.
  * Positive class = "should be notified" (hand label). Predicted positive = strategy notifies the agent.
  * precision = TP/(TP+FP), recall = TP/(TP+FN), F1 = harmonic mean.
  * spam rate = FP / notifications sent (share of notifications that were unwanted = 1 - precision).
  * missed matches = FN.
  * LLM calls = judge calls + one engagement/drafting call per notified agent (each notified agent
    asks its LLM whether/how to reply). Broadcast = 0 judge calls but N-1 engagement calls per post.
Strategy C scores are cached per source (demo / live), so the threshold slider is instant. The result's
`mode` is derived from which engine actually answered each call: it is 'live' only if every call was Gemini.
"""
import threading
import time
from datetime import datetime, timezone

from . import llm
from .eval_data import POSTS, labelled_pairs
from .routing import route
from .seed import seed_profiles

_cache: dict[str, dict] = {}          # "demo" | "live" -> scored dataset (with provenance)
_lock = threading.Lock()
_job_lock = threading.Lock()
progress = {"state": "idle", "done": 0, "total": len(POSTS), "message": "", "error": None}
EVAL_PAUSE = 1.0                       # seconds between posts during a live re-score (on top of the client's spacing)


def _score_one(profiles, by_name, i, author, content, offline_only, workers=None) -> dict:
    out = route(content, by_name[author], profiles, threshold=2.0,  # threshold>1: scores only
                offline_only=offline_only, workers=workers)
    return {"post_idx": i, "author": author, "content": content, "topics": out["topics"],
            "engine": out["engine"], "semantic_engine": out["semantic_engine"],
            "judge_calls": out["funnel"]["judge_calls"], "scores": {r["agent"]: r for r in out["records"]}}


def _provenance(posts: list[dict]) -> dict:
    """Which engines actually produced the numbers. 'live' only if EVERY call was answered by Gemini."""
    judged = [s for p in posts for s in p["scores"].values() if s["judged"]]
    live_j = sum(1 for s in judged if s["engine"] == "gemini")
    sem_posts = [p for p in posts if p["semantic_engine"]]       # posts where stage 2 actually ran
    live_s = sum(1 for p in sem_posts if p["semantic_engine"] == "gemini-embeddings")
    if judged and live_j == len(judged) and live_s == len(sem_posts):
        mode = "live"
    elif live_j == 0 and live_s == 0:
        mode = "demo"
    else:
        mode = "mixed"
    return {"mode": mode, "judge_calls_total": len(judged), "judge_calls_live": live_j,
            "judge_calls_offline": len(judged) - live_j, "semantic_live_posts": live_s, "semantic_total": len(sem_posts)}


def _demo_scores(refresh: bool = False) -> dict:
    """Offline (Demo Mode) scoring: deterministic, no network, always available."""
    with _lock:
        if refresh or "demo" not in _cache:
            profiles = seed_profiles()
            by_name = {p["name"]: p for p in profiles}
            posts = [_score_one(profiles, by_name, i, a, c, True) for i, (a, c, _p) in enumerate(POSTS, start=1)]
            _cache["demo"] = {"posts": posts, "provenance": _provenance(posts),
                              "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        return _cache["demo"]


def get_scores(source: str = "auto", refresh: bool = False) -> dict:
    if source in ("auto", "live") and "live" in _cache:
        return _cache["live"]
    return _demo_scores(refresh)


# ---------------------------------------------------------------- live re-score job (sequential, with progress)
def start_rescore() -> dict:
    with _job_lock:
        if progress["state"] == "running":
            return dict(progress)
        if not llm.live_available():
            info = llm.mode_info()
            why = ("GEMINI_API_KEY is not set" if not info["has_api_key"] else
                   f"Gemini is in cool-down ({info['cooldown_remaining']}s left; last error: {info['last_error']})")
            raise RuntimeError(why)
        progress.update(state="running", done=0, total=len(POSTS), message="Starting…", error=None)
        threading.Thread(target=_rescore_worker, daemon=True).start()
        return dict(progress)


def _rescore_worker():
    try:
        profiles = seed_profiles()
        by_name = {p["name"]: p for p in profiles}
        posts = []
        for i, (author, content, _p) in enumerate(POSTS, start=1):
            if not llm.live_available():
                raise RuntimeError("Gemini became unavailable mid-run (" + str(llm.mode_info()["last_error"]) +
                                   "). Partial results were discarded; nothing was saved as live.")
            progress["message"] = f"Scoring post {i}/{len(POSTS)} ({author}) with Gemini…"
            posts.append(_score_one(profiles, by_name, i, author, content, False, workers=1))
            progress["done"] = i
            time.sleep(EVAL_PAUSE)
        prov = _provenance(posts)
        if prov["mode"] == "demo":
            raise RuntimeError("Every Gemini call fell back to offline rules (" + str(llm.mode_info()["last_error"]) +
                               "). Not saved as live.")
        with _lock:
            _cache["live"] = {"posts": posts, "provenance": prov,
                              "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        progress.update(state="done", message="Live scoring finished." if prov["mode"] == "live" else
                        f"Finished, but {prov['judge_calls_offline']} judge call(s) fell back to offline rules — labelled mixed.")
    except Exception as e:  # noqa: BLE001
        progress.update(state="failed", error=str(e), message="Live re-score failed.")


def _metrics(tp: int, fp: int, fn: int, tn: int, judge_calls: int) -> dict:
    notified = tp + fp
    precision = tp / notified if notified else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn, "notifications": notified,
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "spam_rate": round(fp / notified, 4) if notified else 0.0,
        "missed_matches": fn,
        "judge_calls": judge_calls, "engagement_calls": notified, "llm_calls": judge_calls + notified,
    }


def _confusion(pred_fn, rows) -> tuple[int, int, int, int]:
    tp = fp = fn = tn = 0
    for r in rows:
        p = pred_fn(r)
        if r["label"] and p:
            tp += 1
        elif r["label"] and not p:
            fn += 1
        elif not r["label"] and p:
            fp += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def run_eval(threshold: float, refresh: bool = False, source: str = "auto") -> dict:
    data = get_scores(source, refresh)
    profiles = {p["name"]: p for p in seed_profiles()}
    rows = [r for r in labelled_pairs() if r["label"] is not None]
    post_topics = {p["post_idx"]: [t for t in p["topics"] if t != "internal"] for p in data["posts"]}
    scores = {p["post_idx"]: p["scores"] for p in data["posts"]}
    total_judge_calls = sum(p["judge_calls"] for p in data["posts"])

    def pred_a(r):  # broadcast to everyone but the author
        return True

    def pred_b(r):  # keyword-only: post topic tags intersect the agent's topic subscriptions
        return bool(set(post_topics[r["post_idx"]]) & set(profiles[r["agent"]]["subs"]))

    def pred_c_at(thr):
        def f(r):
            s = scores[r["post_idx"]][r["agent"]]
            return bool(s["judged"] and s["final_score"] >= thr)
        return f

    strategies = [
        {"key": "A", "name": "Broadcast-to-all", **_metrics(*_confusion(pred_a, rows), judge_calls=0)},
        {"key": "B", "name": "Keyword-only (topic tag match)", **_metrics(*_confusion(pred_b, rows), judge_calls=0)},
        {"key": "C", "name": "Three-stage pipeline (ours)",
         **_metrics(*_confusion(pred_c_at(threshold), rows), judge_calls=total_judge_calls)},
    ]
    sweep = []
    for i in range(0, 21):
        thr = round(i * 0.05, 2)
        m = _metrics(*_confusion(pred_c_at(thr), rows), judge_calls=total_judge_calls)
        sweep.append({"threshold": thr, "precision": m["precision"], "recall": m["recall"], "f1": m["f1"],
                      "notifications": m["notifications"]})
    best = max(sweep, key=lambda s: (s["f1"], -abs(s["threshold"] - 0.6)))

    pairs = []
    pc = pred_c_at(threshold)
    for r in labelled_pairs():
        out = {"post_idx": r["post_idx"], "author": r["author"], "agent": r["agent"], "label": r["label"]}
        if r["label"] is None:
            out.update(A=None, B=None, C=None, final_score=None)
        else:
            s = scores[r["post_idx"]][r["agent"]]
            out.update(A=pred_a(r), B=pred_b(r), C=pc(r), final_score=s["final_score"],
                       semantic=s["semantic_score"], llm=s["llm_score"], judged=s["judged"])
            out["outcome"] = ("TP" if r["label"] and out["C"] else "FN" if r["label"] else "FP" if out["C"] else "TN")
        pairs.append(out)

    return {
        "threshold": threshold,
        "mode": data["provenance"]["mode"],            # live | mixed | demo -- computed from what really answered
        "provenance": data["provenance"],
        "scored_at": data["scored_at"],
        "has_live_scores": "live" in _cache,
        "live_available": llm.live_available(),
        "rescore": dict(progress),
        "dataset": {"posts": len(POSTS), "agents": 6, "evaluated_pairs": len(rows),
                    "positive_pairs": sum(1 for r in rows if r["label"])},
        "strategies": strategies,
        "sweep": sweep,
        "best_f1_threshold": best["threshold"],
        "posts": [{"post_idx": p["post_idx"], "author": p["author"], "content": p["content"],
                   "topics": p["topics"], "expected": sorted(POSTS[p["post_idx"] - 1][2])} for p in data["posts"]],
        "pairs": pairs,
        "methodology": __doc__.strip(),
    }
