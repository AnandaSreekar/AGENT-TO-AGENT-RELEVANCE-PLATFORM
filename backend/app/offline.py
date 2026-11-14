"""OFFLINE DEMO MODE implementations: TF-IDF similarity, a deterministic rule-based judge standing in
for the LLM judge, and a template-based comment drafter. No network, fully reproducible."""
import math
import re

from sklearn.feature_extraction.text import TfidfVectorizer

from .topics import LABELS, SPECIFICITY, extract_topics, label_list


def _enrich(text: str) -> str:
    """Append topic tokens so that two texts about the same business topic overlap even with different wording."""
    toks = " ".join(f"topic_{t}" for t in extract_topics(text) if t != "internal")
    return f"{text} {toks} {toks}"


def tfidf_cosines(post_text: str, profiles: list[dict]) -> list[float]:
    """Cosine similarity between the post and each agent's manifesto (TF-IDF, unigrams+bigrams)."""
    docs = [_enrich(p["manifesto"]) for p in profiles] + [_enrich(post_text)]
    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), sublinear_tf=True)
    m = vec.fit_transform(docs)
    post = m[len(profiles)]
    sims = (m[: len(profiles)] @ post.T).toarray().ravel()
    return [float(x) for x in sims]


def judge(post_topics: list[str], profile: dict) -> tuple[float, str]:
    """Deterministic LLM-judge substitute. Combines topic *fit* (noisy-OR of specificity-weighted matches)
    with topic *coverage* (share of the post's weight that the agent cares about)."""
    scored = [t for t in post_topics if SPECIFICITY.get(t, 0) > 0]
    if not scored:
        return 0.05, "no business topic recognised in the post"
    interests = set(profile["interests"])
    matched = [t for t in scored if t in interests]
    fit = 1 - math.prod(1 - SPECIFICITY[t] for t in matched) if matched else 0.0
    total = sum(SPECIFICITY[t] for t in scored)
    cov = sum(SPECIFICITY[t] for t in matched) / total if total else 0.0
    score = round(0.5 * fit + 0.5 * cov, 4)
    core = [t for t in matched if SPECIFICITY[t] >= 0.3]
    if not matched:
        why = f"post is about {label_list(scored)}, none of which is in {profile['name']}'s interests"
    elif not core:
        why = (f"overlap is limited to generic topics ({label_list(matched)}); the post does not match "
               f"{profile['name']}'s business interests")
    else:
        why = f"post covers {label_list(scored)}; matches {profile['name']}'s interest in {label_list(core)}"
    return score, why


# ---------------- comment drafting ----------------
ENGAGE_MIN = 0.7  # offline engagement threshold on the judge score


def _domain(profile: dict) -> str:
    s = (profile["industry"] + " " + profile["manifesto"][:80]).lower()
    if "pay" in s or "fintech" in s:
        return "payments"
    if "bank" in s:
        return "banking"
    if "e-commerce" in s or "commerce" in s:
        return "ecommerce"
    if "auto" in s:
        return "automotive"
    if "food" in s:
        return "food"
    return "generic"


def _thing(post_topics: list[str]) -> str:
    for t, noun in (("food_delivery", "orders"), ("ecommerce", "purchases"), ("automotive", "bookings"),
                    ("financing", "EMI purchases")):
        if t in post_topics:
            return noun
    return "purchases"


def draft_comment(profile: dict, author: dict, content: str, post_topics: list[str]) -> str:
    me, other = profile["name"], author["name"]
    pct = re.search(r"(\d+)\s*%", content)
    fest = re.search(r"\b(diwali|dhanteras|navratri|dussehra|holi|eid|christmas|new year)\b", content, re.I)
    on = f" on {fest.group(1).title()}" if fest else ""
    this = f" this {fest.group(1).title()}" if fest else ""
    thing = _thing(post_topics)
    stack = f" — stackable with {other}'s {pct.group(1)}% offer" if pct else ""
    d = _domain(profile)
    if d == "payments":
        if "security" in post_topics and "promotions" not in post_topics:
            return f"{me} is happy to share fraud signals and align on tokenisation timelines with {other}."
        return f"Extra ₹50 cashback when you pay for {other} {thing} with {me} UPI{on}."
    if d == "banking":
        if "security" in post_topics and "promotions" not in post_topics:
            return f"{me} can support {other} with tokenised-card rollout guidance and fraud-prevention tooling."
        return (f"{me} credit and debit cardholders get an extra instant discount on {other} {thing}{this}{stack}; "
                f"no-cost EMI on larger baskets.")
    if d == "ecommerce":
        return f"{me} would love to cross-promote: bundle {other} vouchers with {me} festive purchases{on}."
    if d == "automotive":
        return f"{me} can offer {other} customers priority test rides and easy finance{this}."
    if d == "food":
        return f"{me} is interested in a collaboration with {other} on this and can share merchant-side insights."
    return f"{me} is interested in exploring a collaboration with {other} on this."


def decide_engagement(profile: dict, llm_score: float) -> tuple[bool, str]:
    if llm_score >= ENGAGE_MIN:
        return True, f"judge score {llm_score:.2f} ≥ {ENGAGE_MIN} engagement bar"
    return False, f"judge score {llm_score:.2f} below the {ENGAGE_MIN} engagement bar — relevant enough to see, not worth a reply"
