"""Business logic: auth, validation gate, posting + routing persistence, engagement, feedback loop."""
import json
import math
import re

from fastapi import HTTPException

from . import llm
from .db import db, get_threshold, jl, now
from .events import hub
from .routing import route
from .safety import (SPAM_BLOCK_AT, clean_text, detect_impersonation, detect_injection, effective_limit, limiter,
                     spam_score)
from .seed import hash_key
from .topics import SPECIFICITY, extract_topics

MAX_PENALTY = 0.30      # feedback can reduce a topic score by at most 30%  => never eliminates a match
PENALTY_STEP = 0.05     # per unit of ignore-streak
TRUST_MIN_ACTIVE = 0.10


def err(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


# ---------------------------------------------------------------- agents / auth
def row_to_profile(c, row) -> dict:
    subs = [r["topic"] for r in c.execute("SELECT topic FROM subscriptions WHERE agent_id=?", (row["id"],))]
    return {
        "id": row["id"], "name": row["company_name"], "industry": row["industry"],
        "manifesto": row["interest_manifesto"], "blocked": jl(row["blocked_industries"], []),
        "active": bool(row["active"]), "subs": subs, "trust": row["trust_score"],
        "interests": sorted(set(subs) | {t for t in extract_topics(row["interest_manifesto"]) if t != "internal"}),
        "autonomy_mode": row["autonomy_mode"], "is_system": bool(row["is_system"]),
    }


def load_profiles(c) -> list[dict]:
    rows = c.execute("SELECT * FROM agents WHERE is_system=0 ORDER BY id").fetchall()
    return [row_to_profile(c, r) for r in rows]


def authenticate(c, api_key: str | None):
    if not api_key:
        raise err(401, "missing_api_key", "X-API-Key header is required for this endpoint.")
    row = c.execute("SELECT * FROM agents WHERE api_key=?", (hash_key(api_key),)).fetchone()
    if not row:
        c.execute("INSERT INTO security_events(kind,agent_id,detail,created_at) VALUES('invalid_api_key',NULL,?,?)",
                  ("request with an unknown API key", now()))
        c.commit()
        raise err(401, "invalid_api_key", "API key is not recognised. Register an agent to obtain one.")
    if not row["active"]:
        raise err(403, "agent_inactive", "This agent is inactive or has been suspended.")
    return row


def security_event(c, kind: str, agent_id: int | None, detail: str, trust_delta: float = 0.0):
    c.execute("INSERT INTO security_events(kind,agent_id,detail,created_at) VALUES(?,?,?,?)",
              (kind, agent_id, detail[:300], now()))
    if agent_id and trust_delta:
        c.execute("UPDATE agents SET trust_score=MAX(0, MIN(1, trust_score+?)) WHERE id=?", (trust_delta, agent_id))
        c.execute("UPDATE agents SET active=0 WHERE id=? AND trust_score<? AND is_system=0",
                  (agent_id, TRUST_MIN_ACTIVE))


def enforce_rate_limit(c, agent):
    ok, retry = limiter.hit(f"agent:{agent['id']}", effective_limit(agent["trust_score"]))
    if not ok:
        security_event(c, "rate_limited", agent["id"], f"exceeded {effective_limit(agent['trust_score'])} writes/min", -0.005)
        c.commit()
        raise HTTPException(status_code=429, headers={"Retry-After": str(retry)}, detail={
            "code": "rate_limited", "message": f"Rate limit exceeded for this agent. Retry in {retry}s.",
            "retry_after": retry})


def validate_content(c, agent, text: str, kind: str, claimed_agent_id: int | None = None, min_len: int = 10) -> str:
    """The safety gate every piece of externally supplied content passes. Raises HTTPException."""
    aid = agent["id"]
    if claimed_agent_id is not None and claimed_agent_id != aid:
        security_event(c, "impersonation", aid, f"claimed agent_id={claimed_agent_id} with key of agent {aid}", -0.15)
        c.commit()
        raise err(403, "impersonation", "agent_id does not match the API key's agent. Impersonation attempt rejected.")
    t = clean_text(text, 2000)
    if len(t) < min_len:
        raise err(422, "content_too_short", f"{kind} must be at least {min_len} characters.")
    if len(t) > 1000:
        raise err(422, "content_too_long", f"{kind} must be at most 1000 characters.")
    inj = detect_injection(t)
    if inj:
        security_event(c, "prompt_injection", aid, inj, -0.10)
        c.commit()
        raise err(422, "prompt_injection", f"{kind} blocked: {inj}.")
    names = [r["company_name"] for r in c.execute("SELECT company_name FROM agents WHERE is_system=0")]
    imp = detect_impersonation(t, agent["company_name"], names)
    if imp:
        security_event(c, "impersonation", aid, imp, -0.15)
        c.commit()
        raise err(403, "impersonation", f"{kind} blocked: {imp}.")
    recent = [r["content"] for r in c.execute(
        "SELECT content FROM posts WHERE author_id=? ORDER BY id DESC LIMIT 20", (aid,))]
    recent += [r["content"] for r in c.execute(
        "SELECT content FROM comments WHERE agent_id=? ORDER BY id DESC LIMIT 20", (aid,))]
    score, reasons = spam_score(t, recent)
    if score >= SPAM_BLOCK_AT:
        security_event(c, "spam", aid, f"spam score {score:.2f}: {', '.join(reasons)}", -0.03)
        c.commit()
        raise err(422, "spam_detected", f"{kind} blocked as spam (score {score:.2f}: {', '.join(reasons) or 'heuristics'}).")
    return t


# ---------------------------------------------------------------- feedback loop
def fb_topics(profile: dict, topics: list[str]) -> list[str]:
    mine = [t for t in topics if t in profile["interests"]]
    specific = [t for t in mine if SPECIFICITY.get(t, 0) >= 0.3]
    return specific or mine


def penalty_for(streak: int) -> float:
    return min(MAX_PENALTY, PENALTY_STEP * max(0, streak))


def make_multiplier_fn(c):
    def fn(profile: dict, topics: list[str]) -> float:
        ts = fb_topics(profile, topics)
        if not ts:
            return 1.0
        pens = []
        for t in ts:
            row = c.execute("SELECT streak FROM topic_stats WHERE agent_id=? AND topic=?", (profile["id"], t)).fetchone()
            pens.append(penalty_for(row["streak"]) if row else 0.0)
        return 1.0 - sum(pens) / len(pens)
    return fn


def record_outcome(c, agent_id: int, topic: str, outcome: str, simulated: bool = False):
    """outcome: NOTIFIED | ENGAGED | IGNORED. Ignores raise the penalty streak; engagement relaxes it by 2."""
    c.execute("INSERT OR IGNORE INTO topic_stats(agent_id,topic) VALUES(?,?)", (agent_id, topic))
    sim = 1 if simulated else 0
    if outcome == "NOTIFIED":
        c.execute("UPDATE topic_stats SET notified=notified+1 WHERE agent_id=? AND topic=?", (agent_id, topic))
    elif outcome == "ENGAGED":
        c.execute("UPDATE topic_stats SET engaged=engaged+1, streak=MAX(0,streak-2) WHERE agent_id=? AND topic=?",
                  (agent_id, topic))
    elif outcome == "IGNORED":
        c.execute("UPDATE topic_stats SET ignored=ignored+1, streak=streak+1, simulated=simulated+? "
                  "WHERE agent_id=? AND topic=?", (sim, agent_id, topic))


# ---------------------------------------------------------------- posting + routing
def _author_profile(c, agent_row) -> dict:
    return row_to_profile(c, agent_row)


def create_post(c, agent, content: str, claimed_agent_id: int | None = None, routed: bool = True) -> dict:
    """Validates, stores and routes one post. Returns dict incl. jobs for background engagement."""
    enforce_rate_limit(c, agent)
    text = validate_content(c, agent, content, "Post", claimed_agent_id)
    topics = extract_topics(text)
    cur = c.execute("INSERT INTO posts(author_id,content,topics,routed,created_at) VALUES(?,?,?,?,?)",
                    (agent["id"], text, json.dumps(topics), 1 if (routed and not agent["is_system"]) else 0, now()))
    post_id = cur.lastrowid
    c.execute("UPDATE agents SET trust_score=MIN(1, trust_score+0.005) WHERE id=?", (agent["id"],))
    c.commit()
    result = {"post_id": post_id, "topics": topics, "notified": [], "funnel": None, "records": []}
    if agent["is_system"] or not routed:
        hub.publish("post_created", {"post_id": post_id, "author": agent["company_name"], "routed": False})
        return result

    author = _author_profile(c, agent)
    profiles = load_profiles(c)
    thr = get_threshold(c)
    out = route(text, author, profiles, thr, multiplier_for=make_multiplier_fn(c))
    for r in out["records"]:
        c.execute(
            "INSERT INTO routing_logs(post_id,agent_id,rule_pass,semantic_score,llm_score,base_score,feedback_multiplier,"
            "final_score,status,reason,engine,judged,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (post_id, r["agent_id"], int(r["rule_pass"]), r["semantic_score"], r["llm_score"], r["base_score"],
             r["feedback_multiplier"], r["final_score"], r["status"], r["reason"], r["engine"], int(r["judged"]), now()))
        if r["status"] == "NOTIFIED":
            prof = next(p for p in profiles if p["id"] == r["agent_id"])
            for t in fb_topics(prof, out["topics"]) or out["topics"]:
                record_outcome(c, r["agent_id"], t, "NOTIFIED")
    c.execute("UPDATE posts SET funnel=?, engine=?, topics=? WHERE id=?",
              (json.dumps(out["funnel"]), out["engine"], json.dumps(out["topics"]), post_id))
    c.commit()
    notified = [r for r in out["records"] if r["status"] == "NOTIFIED"]
    result.update(funnel=out["funnel"], records=out["records"], notified=notified, engine=out["engine"])
    hub.publish("post_created", {"post_id": post_id, "author": agent["company_name"], "routed": True,
                                 "content": text, "funnel": out["funnel"]})
    for r in notified:
        hub.publish("notified", {"post_id": post_id, "agent_id": r["agent_id"], "agent": r["agent"],
                                 "score": r["final_score"], "reason": r["reason"], "author": agent["company_name"]})
    return result


def run_engagement(post_id: int, agent_id: int) -> None:
    """Background task: the notified agent decides whether to engage and drafts a comment."""
    try:
        with db() as c:
            log = c.execute("SELECT * FROM routing_logs WHERE post_id=? AND agent_id=?", (post_id, agent_id)).fetchone()
            if not log or log["status"] != "NOTIFIED":
                return
            post = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
            arow = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            author = row_to_profile(c, c.execute("SELECT * FROM agents WHERE id=?", (post["author_id"],)).fetchone())
            profile = row_to_profile(c, arow)
            topics = jl(post["topics"], [])
            res = llm.decide_and_draft(profile, author, post["content"], topics, log["llm_score"] or 0.0)
            ts = fb_topics(profile, topics) or topics
            if res["engage"] and res["comment"]:
                status = "PENDING" if arow["autonomy_mode"] == "approval" else "PUBLISHED"
                src = "llm" if res["engine"] == "gemini" else "rule-based"
                cur = c.execute("INSERT INTO comments(post_id,agent_id,content,status,source,created_at) VALUES(?,?,?,?,?,?)",
                                (post_id, agent_id, res["comment"], status, src, now()))
                note = " Comment queued for human approval." if status == "PENDING" else " Comment posted."
                c.execute("UPDATE routing_logs SET status='ENGAGED', reason=reason||? WHERE id=?",
                          (f" Engaged ({res['reason']}).{note}", log["id"]))
                for t in ts:
                    record_outcome(c, agent_id, t, "ENGAGED")
                hub.publish("comment_pending" if status == "PENDING" else "comment_created", {
                    "post_id": post_id, "comment_id": cur.lastrowid, "agent_id": agent_id,
                    "agent": profile["name"], "content": res["comment"], "source": src})
            else:
                c.execute("UPDATE routing_logs SET status='IGNORED', reason=reason||? WHERE id=?",
                          (f" Declined to engage ({res['reason']}).", log["id"]))
                for t in ts:
                    record_outcome(c, agent_id, t, "IGNORED")
                hub.publish("ignored", {"post_id": post_id, "agent_id": agent_id, "agent": profile["name"],
                                        "reason": res["reason"]})
    except Exception as e:  # noqa: BLE001 - a failed draft must never break the platform
        try:
            with db() as c:
                c.execute("INSERT INTO security_events(kind,agent_id,detail,created_at) VALUES('engagement_error',?,?,?)",
                          (agent_id, f"{type(e).__name__}: {str(e)[:150]}", now()))
        except Exception:  # noqa: BLE001
            pass


def add_comment(c, agent, post_id: int, content: str, claimed_agent_id: int | None = None) -> dict:
    enforce_rate_limit(c, agent)
    post = c.execute("SELECT id FROM posts WHERE id=?", (post_id,)).fetchone()
    if not post:
        raise err(404, "post_not_found", f"Post {post_id} does not exist.")
    text = validate_content(c, agent, content, "Comment", claimed_agent_id)
    cur = c.execute("INSERT INTO comments(post_id,agent_id,content,status,source,created_at) VALUES(?,?,?,?,?,?)",
                    (post_id, agent["id"], text, "PUBLISHED", "manual", now()))
    log = c.execute("SELECT * FROM routing_logs WHERE post_id=? AND agent_id=?", (post_id, agent["id"])).fetchone()
    if log and log["status"] == "NOTIFIED":  # a human/agent replied manually: counts as engagement
        c.execute("UPDATE routing_logs SET status='ENGAGED', reason=reason||' Engaged via manual comment.' WHERE id=?",
                  (log["id"],))
        prof = row_to_profile(c, agent)
        for t in fb_topics(prof, jl(c.execute("SELECT topics FROM posts WHERE id=?", (post_id,)).fetchone()["topics"], [])):
            record_outcome(c, agent["id"], t, "ENGAGED")
    hub.publish("comment_created", {"post_id": post_id, "comment_id": cur.lastrowid, "agent_id": agent["id"],
                                    "agent": agent["company_name"], "content": text, "source": "manual"})
    return {"id": cur.lastrowid, "post_id": post_id, "agent": agent["company_name"], "content": text,
            "status": "PUBLISHED", "source": "manual"}


# ---------------------------------------------------------------- read models
def post_view(c, p, with_comments: bool = True) -> dict:
    comments = []
    if with_comments:
        for cm in c.execute("SELECT c.*, a.company_name FROM comments c JOIN agents a ON a.id=c.agent_id "
                            "WHERE post_id=? AND status='PUBLISHED' ORDER BY c.id", (p["id"],)):
            comments.append({"id": cm["id"], "agent_id": cm["agent_id"], "agent": cm["company_name"],
                             "content": cm["content"], "source": cm["source"], "created_at": cm["created_at"]})
    logs = c.execute("SELECT l.status, l.final_score, a.company_name FROM routing_logs l JOIN agents a ON a.id=l.agent_id "
                     "WHERE post_id=?", (p["id"],)).fetchall()
    return {
        "id": p["id"], "author_id": p["author_id"], "author": p["company_name"], "content": p["content"],
        "topics": jl(p["topics"], []), "funnel": jl(p["funnel"]), "engine": p["engine"], "created_at": p["created_at"],
        "notified": [l["company_name"] for l in logs if l["status"] in ("NOTIFIED", "ENGAGED", "IGNORED")],
        "not_notified": [l["company_name"] for l in logs if l["status"] == "REJECTED"],
        "comments": comments,
    }


def stats(c) -> dict:
    thr = get_threshold(c)
    one = lambda q, *a: c.execute(q, a).fetchone()[0]  # noqa: E731
    by_status = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM routing_logs GROUP BY status")}
    sec = {r["kind"]: r["n"] for r in c.execute("SELECT kind, COUNT(*) n FROM security_events GROUP BY kind")}
    agents = [dict(r) for r in c.execute(
        "SELECT id, company_name, trust_score, active, autonomy_mode FROM agents WHERE is_system=0 ORDER BY id")]

    feedback = []
    for s in c.execute("SELECT t.*, a.company_name FROM topic_stats t JOIN agents a ON a.id=t.agent_id ORDER BY a.id, t.topic"):
        n_resp = s["engaged"] + s["ignored"]
        mult = 1.0 - penalty_for(s["streak"])
        # before/after: average score of this agent's routed posts on this topic, without vs with feedback
        base, adj = [], []
        for l in c.execute("SELECT l.base_score, l.final_score, p.topics FROM routing_logs l JOIN posts p ON p.id=l.post_id "
                           "WHERE l.agent_id=? AND l.rule_pass=1 AND l.base_score IS NOT NULL", (s["agent_id"],)):
            if s["topic"] in jl(l["topics"], []):
                base.append(l["base_score"])
                adj.append(l["final_score"])
        before = sum(base) / len(base) if base else None
        feedback.append({
            "agent_id": s["agent_id"], "agent": s["company_name"], "topic": s["topic"],
            "notified": s["notified"], "engaged": s["engaged"], "ignored": s["ignored"],
            "simulated_ignores": s["simulated"],
            "engagement_rate": round(s["engaged"] / n_resp, 3) if n_resp else None,
            "ignore_streak": s["streak"], "multiplier": round(mult, 3),
            "score_change_pct": round((mult - 1) * 100, 1),
            "avg_score_before": round(before, 3) if before is not None else None,
            "avg_score_logged_after": round(sum(adj) / len(adj), 3) if adj else None,
            "projected_score_after": round(before * mult, 3) if before is not None else None,
            "would_notify_before": sum(1 for b in base if b >= thr),
            "would_notify_after": sum(1 for b in base if b * mult >= thr),
        })
    return {
        "llm": llm.mode_info(),
        "threshold": thr,
        "counts": {
            "agents": len(agents), "posts": one("SELECT COUNT(*) FROM posts p JOIN agents a ON a.id=p.author_id WHERE a.is_system=0"),
            "comments": one("SELECT COUNT(*) FROM comments WHERE status='PUBLISHED'"),
            "pending_approvals": one("SELECT COUNT(*) FROM comments WHERE status='PENDING'"),
            "routing_decisions": one("SELECT COUNT(*) FROM routing_logs"),
            "webhook_failures": one("SELECT COUNT(*) FROM webhook_deliveries WHERE status='FAILED'"),
        },
        "routing_by_status": by_status,
        "security_events": sec,
        "agents": agents,
        "feedback": feedback,
        "feedback_policy": {
            "max_penalty_pct": MAX_PENALTY * 100, "step_pct_per_ignore": PENALTY_STEP * 100,
            "engagement_relaxes_streak_by": 2,
            "note": "Penalty multiplies the topic score; it is capped so a relevant match is never eliminated.",
        },
    }
