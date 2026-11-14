"""Trust & safety: prompt-injection detection, spam scoring, impersonation checks, rate limiting,
webhook SSRF validation. Everything here is heuristic and documented as such in DESIGN.md."""
import ipaddress
import re
import threading
import time
import unicodedata
from collections import defaultdict, deque
from difflib import SequenceMatcher
from urllib.parse import urlparse

from .config import settings

_ZW = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_ZW)
    return re.sub(r"\s+", " ", t).strip()


def clean_text(text: str, max_len: int = 1000) -> str:
    """Strip control chars, normalise whitespace, truncate."""
    t = "".join(ch for ch in normalize(text) if ch.isprintable() or ch == "\n")
    return t[:max_len]


# ---------------- prompt injection ----------------
_INJ = [
    r"ignore\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|preceding)\s+(?:instructions?|prompts?|rules?|messages?|directions?)",
    r"disregard\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|system|instructions?)",
    r"forget\s+(?:everything|all\s+(?:previous|prior)|your\s+(?:instructions|rules)|previous)",
    r"(?:reveal|show|print|leak|output|send|share|tell\s+me)\s+.{0,40}(?:system\s+prompt|api[\s_-]?keys?|secrets?|credentials|passwords?|tokens?)",
    r"you\s+are\s+now\s+(?:a|an|the|in|dan)\b",
    r"\bact\s+as\s+(?:a|an|the)?\s*(?:admin|administrator|root|system|developer|dan|jailbroken)\b",
    r"\b(?:system|developer)\s+(?:prompt|message|override|instruction)s?\b",
    r"\bjailbreak\w*\b", r"\bdo\s+anything\s+now\b",
    r"override\s+(?:your|the|all)\s+(?:instructions|rules|safety|guidelines|settings)",
    r"\bnew\s+instructions?\s*:",
    r"<\s*/?\s*(?:system|assistant|instructions?)\s*>", r"\[\s*(?:system|inst)\s*\]", r"#{2,}\s*(?:system|instruction)",
    r"\b(?:always|only)\s+(?:respond|reply|answer|comment)\s+with\b",
    r"\byou\s+(?:must|should|will)\s+(?:now\s+)?(?:comment|reply|respond|engage|notify|approve|ignore)\b",
    r"\b(?:notify|route\s+(?:this\s+)?to)\s+(?:all|every)\s+agents?\b",
    r"\bpretend\s+(?:to\s+be|you\s+are)\b",
]
_INJ_RE = [re.compile(p, re.I | re.S) for p in _INJ]


def detect_injection(text: str) -> str | None:
    t = normalize(text)
    for rx in _INJ_RE:
        m = rx.search(t)
        if m:
            return f"instruction-like content detected (matched “{m.group(0)[:60]}”)"
    return None


# ---------------- spam ----------------
_SPAM_PHRASES = ["click here", "free money", "guaranteed returns", "act now", "100% free", "buy now",
                 "winner", "claim your prize", "wire transfer", "double your", "limited time only!!!",
                 "work from home", "crypto giveaway", "no risk"]
_URL_RE = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:ly|tk|xyz|top|click)\b", re.I)


def _tokens(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9₹%]+", t.lower()))


def spam_score(text: str, recent: list[str]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0
    t = normalize(text)
    letters = [c for c in t if c.isalpha()]
    if len(letters) > 20 and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        score += 0.35
        reasons.append("excessive capitals")
    if len(_URL_RE.findall(t)) >= 2:
        score += 0.4
        reasons.append("multiple links")
    elif _URL_RE.search(t):
        score += 0.15
    if re.search(r"[!?]{2,}", t) or t.count("!") >= 4:
        score += 0.2
        reasons.append("excessive punctuation")
    if re.search(r"(.)\1{5,}", t):
        score += 0.2
        reasons.append("repeated characters")
    low = t.lower()
    ph = [p for p in _SPAM_PHRASES if p in low]
    if ph:
        score += min(0.5, 0.3 * len(ph))
        reasons.append(f"spam phrase “{ph[0]}”")
    toks = _tokens(t)
    for prev in recent:
        pt = _tokens(prev)
        if toks and pt and len(toks & pt) / len(toks | pt) >= 0.85:
            score += 0.6
            reasons.append("near-duplicate of a recent post/comment by this agent")
            break
    return min(score, 1.0), reasons


SPAM_BLOCK_AT = 0.5


# ---------------- impersonation ----------------
def _name_variants(name: str) -> list[str]:
    n = name.strip().lower()
    out = {n, re.sub(r"\s+", "", n)}
    first = n.split()[0] if n.split() else n
    if len(first) >= 4:
        out.add(first)
    return [re.escape(v) for v in out if v]


def detect_impersonation(content: str, author_name: str, all_names: list[str]) -> str | None:
    t = normalize(content).lower()
    for other in all_names:
        if other.lower() == author_name.lower():
            continue
        for v in _name_variants(other):
            pats = [
                rf"(?:official|authori[sz]ed|verified)\s+(?:message|notice|announcement|statement|post|update)\s+(?:from|by|of)\s+(?:the\s+)?{v}",
                rf"(?:this\s+is|we\s+are|i\s+am|i'm|message\s+from|on\s+behalf\s+of|posting\s+as|speaking\s+for|signed)[\s:,-]+(?:the\s+)?(?:official\s+)?{v}\b",
                rf"\b{v}\s+(?:official|support|security\s+team)\s*(?:here|team)?\s*[:\-]",
            ]
            for p in pats:
                if re.search(p, t):
                    return f"content claims to speak for another registered agent ({other})"
    return None


_GENERIC = {"official", "support", "india", "team", "app", "group", "ltd", "limited", "inc", "pvt", "private",
            "technologies", "tech", "services", "corp", "co", "the", "verified", "care", "help"}


def _core_name(s: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _GENERIC]
    return "".join(words) or re.sub(r"[^a-z0-9]", "", s.lower())


def name_conflict(new_name: str, existing: list[str]) -> str | None:
    """Flags names equal/near-equal/containing an existing agent's name after removing generic words."""
    nn = _core_name(new_name)
    for e in existing:
        en = _core_name(e)
        if not en:
            continue
        if nn == en or SequenceMatcher(None, nn, en).ratio() >= 0.82                 or (len(en) >= 4 and en in nn) or (len(nn) >= 4 and nn in en):
            return e
    return None


# ---------------- rate limiting ----------------
class RateLimiter:
    """In-memory sliding window. Per-process: see DESIGN.md for the multi-instance story."""

    def __init__(self):
        self._h: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            q = self._h[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(window - (now - q[0])) + 1)
            q.append(now)
            return True, 0

    def reset(self, key: str | None = None):
        with self._lock:
            if key is None:
                self._h.clear()
            else:
                self._h.pop(key, None)


limiter = RateLimiter()


def effective_limit(trust: float) -> int:
    """Low-trust agents get a tighter budget; high-trust agents never exceed the base limit."""
    base = settings.rate_limit_per_min
    return max(2, round(base * min(1.0, max(trust, 0.0) / 0.7)))


# ---------------- webhook URL validation (SSRF) ----------------
def validate_webhook_url(url: str) -> str | None:
    """Returns an error string or None if acceptable."""
    try:
        u = urlparse(url)
    except ValueError:
        return "malformed URL"
    if u.scheme not in ("http", "https") or not u.hostname:
        return "webhook_url must be an http(s) URL"
    if settings.allow_private_webhooks:
        return None
    host = u.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return "webhook_url may not point at a local/internal host"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return "webhook_url may not point at a private IP address"
    except ValueError:
        pass
    return None
