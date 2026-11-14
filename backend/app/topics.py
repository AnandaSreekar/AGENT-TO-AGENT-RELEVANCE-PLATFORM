"""Keyword topic taxonomy. Deterministic and explainable; used for Stage-1 subscription rules,
feedback bucketing, TF-IDF enrichment and the offline judge. This is deliberately simple —
see DESIGN.md for the limitations (no negation, no buyer/seller roles)."""
import re

_P = {
    "food_delivery": r"\b(food|restaurants?|dine[- ]?out|instamart|groceries|grocery|quick[- ]commerce|deliver(?:y|ies)|meals?)\b",
    "payments": r"\b(upi|payments?|pay|wallets?|qr|checkout|merchants?|transactions?|tokeni[sz]ation)\b",
    "banking": r"\b(banks?|banking|credit cards?|debit cards?|cards?|net[- ]banking|savings|rbi|credit lines?)\b",
    "financing": r"\b(emi|loans?|financ(?:e|ing)|down[- ]payment|interest rates?|processing fees?|pay[- ]later|credit lines?|instant credit)\b",
    "ecommerce": r"\b(e-?commerce|marketplace|sellers?|electronics|fashion|shopping|fulfil?lment|big billion|online retail|retail)\b",
    "automotive": r"\b(bikes?|motorcycles?|scooters?|two[- ]wheelers?|vehicles?|evs?|electric|test rides?|showrooms?|cars?|auto|automotive|mileage|chetak|pulsar)\b",
    "festive": r"\b(diwali|deepavali|dhanteras|festive|festivals?|navratri|dussehra|holi|eid|christmas|new year|onam|pongal)\b",
    "promotions": r"(\d+\s*%\s*(?:off|discount|cashback)|\b(?:discounts?|offers?|deals?|coupons?|cashback|rewards?|sale|free delivery|instant discount)\b)",
    "security": r"\b(fraud|phishing|scams?|security|kyc|rbi|compliance|tokeni[sz]ation|breach|norms)\b",
    "partnerships": r"\b(partner(?:s|ing|ship|ships)?|collaborat\w*|tie[- ]?ups?|co[- ]?brand\w*|onboard\w*|integrat\w*|co[- ]marketing|seeking)\b",
    "internal": r"\b(hiring|careers?|recruit\w*|engineers?|engineering blog|layoffs?|scaled our|recommendation system|apply on)\b",
}
PATTERNS = {k: re.compile(v, re.I) for k, v in _P.items()}

# How "specific" a topic is as evidence of relevance. Generic topics (festive/promotions) are shared by
# nearly every consumer-facing business, so matching on them alone must not be enough to notify.
SPECIFICITY = {
    "food_delivery": 0.6, "payments": 0.6, "banking": 0.6, "financing": 0.6, "ecommerce": 0.6,
    "automotive": 0.6, "security": 0.6, "partnerships": 0.3, "festive": 0.15, "promotions": 0.15,
    "internal": 0.0,
}
ALL_TOPICS = [t for t in PATTERNS if t != "internal"]
LABELS = {
    "food_delivery": "food delivery", "payments": "payments", "banking": "banking & cards",
    "financing": "financing / EMI", "ecommerce": "e-commerce", "automotive": "automotive",
    "festive": "festive season", "promotions": "offers & promotions", "security": "security & compliance",
    "partnerships": "partnerships", "internal": "internal / hiring",
}


def extract_topics(text: str) -> list[str]:
    """Topics present in text, ordered by number of keyword hits (desc)."""
    hits = {t: len(p.findall(text or "")) for t, p in PATTERNS.items()}
    return [t for t, n in sorted(hits.items(), key=lambda kv: -kv[1]) if n > 0]


def label_list(topics: list[str]) -> str:
    return ", ".join(LABELS.get(t, t) for t in topics) or "no recognised topic"
