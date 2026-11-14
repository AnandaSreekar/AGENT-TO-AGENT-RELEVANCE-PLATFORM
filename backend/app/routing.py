"""Three-stage relevance routing (pure function; persistence lives in services.py).

Stage 1  rule filter     author / inactive / blocked industry / topic subscription  (free, exact)
Stage 2  semantic score  Gemini embeddings (fallback TF-IDF) cosine, rank the survivors (cheap)
Stage 3  LLM judge       only the top-K semantic candidates get an LLM relevance score + reason (expensive)

final = (0.25*semantic + 0.75*llm) * feedback_multiplier * sender_trust_multiplier   (judged agents)
final = 0.5*semantic * multipliers                                                   (never judged => can't notify)
Notify iff judged AND final >= threshold.
"""
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import llm
from .config import settings
from .topics import extract_topics, label_list

SEM_CUTOFF = 0.20
W_SEM, W_LLM = 0.25, 0.75
UNJUDGED_DISCOUNT = 0.5


def rule_filter(profile: dict, author: dict, post_topics: list[str]) -> tuple[bool, str]:
    if profile["id"] == author["id"]:
        return False, "author of the post (an agent is never notified about its own post)"
    if not profile["active"]:
        return False, "agent is inactive"
    blocked = {b.strip().lower() for b in profile.get("blocked", [])}
    if author["industry"].strip().lower() in blocked:
        return False, f"blocks posts from the {author['industry']} industry (same-industry competitor rule)"
    if post_topics == ["internal"]:
        return False, "post is internal/hiring content with no business topic"
    if post_topics and profile["subs"]:
        if not set(post_topics) & set(profile["subs"]):
            return False, (f"post topics ({label_list(post_topics)}) do not overlap with its subscriptions "
                           f"({label_list(profile['subs'])})")
    elif not post_topics:
        return True, "post has no recognised topic, so the subscription rule does not apply"
    return True, "passes rule filter"


def route(post_text: str, author: dict, profiles: list[dict], threshold: float,
          multiplier_for: Callable[[dict, list[str]], float] | None = None,
          top_k: int | None = None, workers: int | None = None, offline_only: bool = False) -> dict:
    """Score every agent for one post. Returns {'records': [...], 'funnel': {...}, 'topics': [...], 'engine': str}."""
    top_k = top_k or settings.llm_top_k
    topics = extract_topics(post_text)
    sender_mult = 1.0 if author.get("trust", 0.7) >= 0.5 else 0.5 + author.get("trust", 0.7)

    recs: dict[int, dict] = {}
    for p in profiles:
        ok, why = rule_filter(p, author, topics)
        recs[p["id"]] = {
            "agent_id": p["id"], "agent": p["name"], "rule_pass": ok, "rule_reason": why,
            "semantic_cos": None, "semantic_score": None, "llm_score": None, "judged": False,
            "base_score": None, "feedback_multiplier": 1.0, "final_score": 0.0,
            "status": "REJECTED", "reason": "", "judge_reason": None, "engine": None,
        }
    passed = [p for p in profiles if recs[p["id"]]["rule_pass"]]

    # ---- stage 2: semantic
    sem_engine = "tfidf"
    if passed:
        cos, sem_engine = llm.semantic_cosines(post_text, passed, offline_only)
        for p, c in zip(passed, cos):
            r = recs[p["id"]]
            r["semantic_cos"] = round(c, 4)
            r["semantic_score"] = round(llm.normalize_semantic(c, sem_engine), 4)
    ranked = sorted(passed, key=lambda p: -recs[p["id"]]["semantic_score"])
    candidates = [p for p in ranked if recs[p["id"]]["semantic_score"] >= SEM_CUTOFF][:top_k]

    # ---- stage 3: LLM judge (parallel)
    judge_engines = set()
    if candidates:
        # <=2 judge calls in flight (free-tier friendly); the Gemini client additionally spaces requests out
        n_workers = max(1, min(workers or settings.gemini_max_concurrency, len(candidates)))
        with ThreadPoolExecutor(max_workers=n_workers) as ex:
            results = list(ex.map(lambda p: llm.judge(p, author, post_text, topics, offline_only), candidates))
        for p, res in zip(candidates, results):
            r = recs[p["id"]]
            r["llm_score"] = round(res["score"], 4)
            r["judge_reason"] = res["reason"]
            r["judged"] = True
            r["engine"] = res["engine"]
            judge_engines.add(res["engine"])

    # ---- final scores + explanations
    for p in profiles:
        r = recs[p["id"]]
        mult = 1.0
        if r["rule_pass"] and multiplier_for:
            mult = multiplier_for(p, topics)
        r["feedback_multiplier"] = round(mult, 4)
        who = f"{p['name']} was not notified about {author['name']}'s post"
        if not r["rule_pass"]:
            r["final_score"] = 0.0
            r["reason"] = ("Not applicable: " if p["id"] == author["id"] else f"{who} because it was filtered out: ") + r["rule_reason"] + "."
            continue
        sem = r["semantic_score"]
        if r["judged"]:
            base = W_SEM * sem + W_LLM * r["llm_score"]
        else:
            base = UNJUDGED_DISCOUNT * sem
        r["base_score"] = round(base, 4)
        r["final_score"] = round(min(1.0, base * mult * sender_mult), 4)
        mnote = f" (feedback x{mult:.2f})" if abs(mult - 1.0) > 1e-6 else ""
        if r["judged"] and r["final_score"] >= threshold:
            r["status"] = "NOTIFIED"
            r["reason"] = (f"{p['name']} was notified: {r['judge_reason']}. "
                           f"Final score {r['final_score']:.2f} ≥ threshold {threshold:.2f}{mnote}.")
        elif r["judged"]:
            r["reason"] = (f"{who} because {r['judge_reason']}. "
                           f"Final score {r['final_score']:.2f} < threshold {threshold:.2f}{mnote}.")
        else:
            r["reason"] = (f"{who}: semantic similarity {sem:.2f} was too weak to justify an LLM judgement "
                           f"(cutoff {SEM_CUTOFF}, top-{top_k} candidates only). Final score {r['final_score']:.2f}.")

    records = sorted(recs.values(), key=lambda r: (-r["final_score"], r["agent"]))
    judged = sum(1 for r in records if r["judged"])
    notified = sum(1 for r in records if r["status"] == "NOTIFIED")
    others = len(profiles) - 1
    funnel = {
        "total_agents": len(profiles),
        "author_excluded": 1,
        "rule_filtered": len(profiles) - 1 - len(passed),
        "rule_passed": len(passed),
        "semantic_candidates": len(candidates),
        "semantic_dropped": len(passed) - len(candidates),
        "llm_evaluated": judged,
        "notified": notified,
        "judge_calls": judged,
        "engagement_calls": notified,
        "llm_calls_pipeline": judged + notified,
        "llm_calls_broadcast": others,
        "llm_calls_saved": others - (judged + notified),
        "drafts_avoided": others - notified,
        "threshold": threshold,
    }
    engine = f"{sem_engine}+{'/'.join(sorted(judge_engines)) or 'no-judge'}"
    for r in records:
        r["engine"] = r["engine"] or sem_engine
    return {"records": records, "funnel": funnel, "topics": topics, "engine": engine,
            "semantic_engine": sem_engine if passed else None,  # None: stage 2 never ran for this post
            "judge_engines": sorted(judge_engines)}
