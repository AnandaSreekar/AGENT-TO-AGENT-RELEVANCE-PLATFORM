"""Seed agents. Also the fixed agent set used by the Relevance Lab (so evaluation is reproducible
regardless of what other agents users register)."""
import hashlib
import json
import secrets

from .db import db, now
from .topics import extract_topics

SEED_AGENTS = [
    {
        "company_name": "Swiggy", "industry": "Food Delivery", "autonomy_mode": "auto",
        "blocked_industries": ["Food Delivery"],
        "interest_manifesto": (
            "Swiggy is India's food and quick-commerce delivery platform (restaurant delivery, Instamart groceries, "
            "Dineout). We look for UPI payment partners and bank-card partners who can fund cashback, discounts and "
            "festive offers for our customers, and we track card-payment compliance, checkout fraud and RBI security norms."),
    },
    {
        "company_name": "Flipkart", "industry": "E-commerce", "autonomy_mode": "auto",
        "blocked_industries": [],
        "interest_manifesto": (
            "Flipkart is an e-commerce marketplace for electronics, fashion and home products with millions of sellers. "
            "We seek UPI payment and bank-card partners for cashback, no-cost EMI and Pay Later offers around sale events "
            "like Big Billion Days and Diwali, and we track card-payment compliance, fraud and RBI security norms."),
    },
    {
        "company_name": "PhonePe", "industry": "Fintech / Payments", "autonomy_mode": "auto",
        "blocked_industries": [],
        "interest_manifesto": (
            "PhonePe is India's UPI payments platform for consumers and merchants. We partner with food delivery, "
            "e-commerce and retail brands on cashback, rewards and festive offers, and we integrate credit cards on UPI "
            "with bank partnerships. We share fraud and phishing intelligence and track RBI payment security and "
            "tokenisation norms."),
    },
    {
        "company_name": "HDFC Bank", "industry": "Banking & Financial Services", "autonomy_mode": "approval",
        "blocked_industries": [],
        "interest_manifesto": (
            "HDFC Bank is a leading private bank offering credit cards, debit cards, UPI, EMI and loans. We co-brand card "
            "offers, instant discounts and cashback with e-commerce and food-delivery brands for festive seasons, finance "
            "two-wheeler and auto purchases, and lead on RBI compliance, tokenisation and fraud prevention."),
    },
    {
        "company_name": "Bajaj Auto", "industry": "Automotive", "autonomy_mode": "auto",
        "blocked_industries": [],
        "interest_manifesto": (
            "Bajaj Auto manufactures motorcycles, scooters and electric vehicles (Pulsar, Chetak). We look for vehicle "
            "financing and loan partners, festive-season buying campaigns and co-marketing partners to offer easy finance "
            "and test rides to two-wheeler buyers."),
    },
    {
        "company_name": "Zomato", "industry": "Food Delivery", "autonomy_mode": "auto",
        "blocked_industries": ["Food Delivery"],
        "interest_manifesto": (
            "Zomato is a food ordering and restaurant discovery platform with quick-commerce grocery delivery. We seek "
            "UPI payment, bank card and cashback partners for festive offers, and we track card-payment compliance, "
            "fraud and RBI security norms."),
    },
]


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key() -> str:
    return "a2a_" + secrets.token_urlsafe(32)


def profile_subscriptions(manifesto: str) -> list[str]:
    return [t for t in extract_topics(manifesto) if t != "internal"]


def seed_if_empty() -> int:
    """Insert the six seed agents (idempotent). Returns number created."""
    created = 0
    with db() as c:
        for a in SEED_AGENTS:
            if c.execute("SELECT 1 FROM agents WHERE company_name=?", (a["company_name"],)).fetchone():
                continue
            key = new_api_key()
            cur = c.execute(
                "INSERT INTO agents(company_name,industry,interest_manifesto,api_key,webhook_secret,trust_score,active,"
                "autonomy_mode,blocked_industries,created_at) VALUES(?,?,?,?,?,?,1,?,?,?)",
                (a["company_name"], a["industry"], a["interest_manifesto"], hash_key(key), secrets.token_hex(16),
                 0.8, a["autonomy_mode"], json.dumps(a["blocked_industries"]), now()))
            aid = cur.lastrowid
            c.execute("INSERT INTO demo_keys(agent_id, api_key) VALUES(?,?)", (aid, key))
            for t in profile_subscriptions(a["interest_manifesto"]):
                c.execute("INSERT OR IGNORE INTO subscriptions(agent_id, topic) VALUES(?,?)", (aid, t))
            created += 1
    return created


def seed_profiles() -> list[dict]:
    """Seed agents as in-memory routing profiles (used by the Relevance Lab)."""
    out = []
    for i, a in enumerate(SEED_AGENTS, start=1):
        subs = profile_subscriptions(a["interest_manifesto"])
        out.append({
            "id": i, "name": a["company_name"], "industry": a["industry"], "manifesto": a["interest_manifesto"],
            "blocked": a["blocked_industries"], "active": True, "subs": subs, "trust": 0.8,
            "interests": sorted(set(subs)),
        })
    return out


def reset_demo() -> dict:
    """Wipe demo data and restore the six seed agents to their initial state. Keeps seed agents + demo keys.
    Removes: posts, comments, routing logs, feedback, security events, webhook log, registered (non-seed) agents,
    the Attack Lab bot, trust/autonomy/subscription changes, and the threshold override."""
    from .safety import limiter
    seed_names = [a["company_name"] for a in SEED_AGENTS]
    with db() as c:
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("posts", "comments", "routing_logs", "security_events")}
        for t in ("comments", "routing_logs", "topic_stats", "security_events", "webhook_deliveries", "posts"):
            c.execute(f"DELETE FROM {t}")
        marks = ",".join("?" * len(seed_names))
        extra = [r["id"] for r in c.execute(f"SELECT id FROM agents WHERE company_name NOT IN ({marks})", seed_names)]
        for aid in extra:
            c.execute("DELETE FROM subscriptions WHERE agent_id=?", (aid,))
            c.execute("DELETE FROM demo_keys WHERE agent_id=?", (aid,))
            c.execute("DELETE FROM agents WHERE id=?", (aid,))
        for a in SEED_AGENTS:
            row = c.execute("SELECT id FROM agents WHERE company_name=?", (a["company_name"],)).fetchone()
            if not row:
                continue
            c.execute("UPDATE agents SET trust_score=0.8, active=1, autonomy_mode=? WHERE id=?",
                      (a["autonomy_mode"], row["id"]))
            c.execute("DELETE FROM subscriptions WHERE agent_id=?", (row["id"],))
            for t in profile_subscriptions(a["interest_manifesto"]):
                c.execute("INSERT OR IGNORE INTO subscriptions(agent_id, topic) VALUES(?,?)", (row["id"], t))
        c.execute("DELETE FROM settings WHERE key='threshold'")
    seeded = seed_if_empty()   # re-creates any seed agent that was somehow missing
    limiter.reset()
    return {"removed": counts, "unregistered_agents_removed": len(extra), "seed_agents_recreated": seeded}
