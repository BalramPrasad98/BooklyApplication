"""Understanding layer.

- Exact identifiers (order IDs, emails) are always pulled with regex: these have a fixed,
  unambiguous syntax (e.g. "BK-1001", "name@domain.com"), so there's nothing to "understand" -
  a pattern match is both deterministic and auditable, and an LLM call would be slower and no
  more correct.
- Everything fuzzy - return reason, yes/no, which item ("the 4th one"), greetings/smalltalk,
  and which topic a message is about - is understood by Claude in a single call per turn when
  ANTHROPIC_API_KEY is set. There is a keyword/regex fallback ONLY so the demo still runs fully
  offline ("mock mode") when no key is configured, or if a live call fails - it is never used to
  override a live model's judgement.
"""
import json
import os
import re
from datetime import date

from data import TODAY

ORDER_RE = re.compile(r"\b(?:BK[-\s]?(\d{4})|#?(1\d{3}))\b", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")

REASONS = {
    "damaged": ["damaged", "broken", "torn", "ripped", "bent", "water", "stained", "defective",
                "missing pages", "falling apart", "crushed"],
    "wrong_item": ["wrong", "incorrect", "not what i ordered", "different book", "wrong edition",
                   "sent me the wrong"],
    "changed_mind": ["changed my mind", "change my mind", "don't want", "dont want", "no longer",
                     "didn't like", "didnt like", "don't like", "not for me", "duplicate",
                     "bought twice", "gift", "don't need", "dont need"],
}
REASON_LABELS = {"damaged": "Damaged or defective", "wrong_item": "Wrong item received",
                 "changed_mind": "Changed my mind / no longer needed"}

# ---- offline-only fallback vocab (mock mode / LLM call failure) ----
YES = ["yes", "y", "yeah", "yep", "yup", "sure", "confirm", "correct", "go ahead", "please do",
       "ok", "okay", "do it", "sounds good", "please"]
NO = ["no", "n", "nope", "nah", "cancel", "don't", "dont", "not now", "stop"]
ORDINAL_WORDS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth", "tenth"]
GREETING_RE = re.compile(r"\b(hi|hello|hey|yo|howdy|good (morning|afternoon|evening))\b")
META_RE = re.compile(r"\b(what (do|can) you (do|help)|who are you|what are you|how (do|does) this work|"
                     r"what('s| is) this|help me understand|what (kind of |)(help|support) (do|can) you)\b")
_ORDER_INTENT_RE = re.compile(r"\b(where.{0,15}order|track|order.{0,15}status|status.{0,15}order|my order|"
                              r"my (latest|last|recent) order|order (update|info|information)|"
                              r"mailing address|shipping address|ship(?:ping)? to|"
                              r"where.{0,15}(shipping|sent|delivered)|delivery date|any orders)\b")
_RETURN_INTENT_RE = re.compile(r"\b(return (my|this|a|the)|start a return|refund (my|for))\b")
_CANCEL_INTENT_RE = re.compile(r"\b(cancel (my|this|the|an?)|don'?t want (it|this) anymore)\b")
_ADDRESS_INTENT_RE = re.compile(r"\b(change|update|edit|fix|wrong) .{0,20}(address|where it ships|shipping to)\b")

MODEL = os.environ.get("BOOKLY_MODEL", "claude-opus-4-7")
_client = None


def llm_enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _llm(system: str, user: str, max_tokens: int = 300) -> str | None:
    global _client
    if not llm_enabled():
        return None
    try:
        import anthropic
        _client = _client or anthropic.Anthropic()
        resp = _client.messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                       messages=[{"role": "user", "content": user}])
        return resp.content[0].text.strip()
    except Exception as e:  # never let the LLM take the demo down
        print(f"[LLM error, falling back to rules] {e}")
        return None


def _has_phrase(text: str, phrases) -> bool:
    return any(re.search(rf"\b{re.escape(p)}\b", text) for p in phrases)


# ---------------------------- offline fallback helpers ----------------------------
def _rule_yes_no(text: str):
    t = text.lower().strip(" .!")
    first = re.split(r"[\s,]+", t)[0] if t else ""
    if first in YES or t in YES or _has_phrase(t, ["go ahead", "please do", "do it", "sounds good"]):
        return "yes"
    if first in NO or t in NO:
        return "no"
    return None


def _rule_ordinal(text: str):
    t = text.lower()
    if re.search(r"\b(last|most recent\w*|latest|newest)\b", t):
        return -1
    if re.search(r"\b(oldest|earliest)\b", t):
        return 0
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", t)  # "4th", "1st", ...
    if m:
        return int(m.group(1)) - 1
    for idx, word in enumerate(ORDINAL_WORDS):
        if re.search(rf"\b{word}\b", t):
            return idx
    m = re.search(r"\b(\d{1,2})\b", t)  # bare "4"
    if m:
        return int(m.group(1)) - 1
    return None


def _rule_intent(t: str):
    if _RETURN_INTENT_RE.search(t):
        return "return"
    if _CANCEL_INTENT_RE.search(t):
        return "cancel_order"
    if _ADDRESS_INTENT_RE.search(t):
        return "change_address"
    if _ORDER_INTENT_RE.search(t):
        return "order_status"
    return None


def rule_extract(text: str) -> dict:
    """Pure keyword/regex understanding - used only in mock mode or if a live LLM call fails."""
    t = text.lower()
    reason = next((r for r, kws in REASONS.items() if _has_phrase(t, kws)), None)
    return {"reason": reason, "yes_no": _rule_yes_no(text), "ordinal": _rule_ordinal(text),
            "greeting": bool(GREETING_RE.search(t)), "meta": bool(META_RE.search(t)),
            "intent": _rule_intent(t)}


INTENTS = ("order_status", "return", "cancel_order", "change_address", "other")


# ---------------------------- LLM understanding ----------------------------
def _llm_understand(text: str) -> dict | None:
    raw = _llm(
        "You analyze one customer-support chat message for an online bookstore called Bookly. "
        "Reply with ONLY a JSON object (no prose, no markdown fences) with these keys:\n"
        '"reason": one of "damaged", "wrong_item", "changed_mind", or null - why the customer wants to '
        "return an item, ONLY if explicitly stated in THIS message.\n"
        '"yes_no": "yes", "no", or null - whether this message confirms or declines a yes/no question. '
        "Use null if the message isn't simply confirming/declining.\n"
        '"ordinal": an integer or null - if the message refers to a SPECIFIC item by its position among a '
        "set of orders/items - either because a numbered list was just shown to them (e.g. \"the 4th one\", "
        '"the second book", "number 2"), OR because they describe which one with a recency/order phrase '
        '(e.g. "the last one", "my most recent order", "the latest one", "the first one I ordered", "the '
        'oldest order") even if no list has been shown yet - return its 0-based index (first=0, second=1, '
        '...). Use -1 for "last"/"most recent"/"latest"/"newest" - lists are always shown oldest-first, so '
        'the last position IS the most recent one. Use 0 for "first"/"oldest"/"earliest". Use null if no '
        "such choice is being made.\n"
        '"greeting": true if the message is primarily a greeting or smalltalk (e.g. "hi", "hello", "how are '
        'you") with no other request, else false.\n'
        '"meta": true if the customer is asking what you (the assistant) can do, who/what you are, or how '
        "this chat works - a question about your own capabilities, not about an order or a store policy. "
        "Else false.\n"
        '"intent": one of "order_status" (asking about an existing order\'s status, tracking, or what '
        "address it's shipping/shipped to - any inquiry about an order, not just where it currently stands), "
        '"return" (wants to send an item back for a refund), "cancel_order" (wants to stop/cancel an order '
        'before it ships), "change_address" (wants to CHANGE/UPDATE the shipping or mailing address on an '
        'order - not just asking what it currently is, that\'s "order_status"), "other" (anything else, '
        "including general policy questions), or null - ONLY set this when the message "
        "CLEARLY and newly expresses one of these on its own. Use null if the message is just a follow-up "
        "detail (an order number, an email, a yes/no, an item choice) that doesn't by itself express a new "
        "topic.\n"
        "Never invent information that isn't present in the message.",
        text, max_tokens=180)
    if raw is None:
        return None
    try:
        j = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        return None
    return {
        "reason": j.get("reason") if j.get("reason") in REASONS else None,
        "yes_no": j.get("yes_no") if j.get("yes_no") in ("yes", "no") else None,
        "ordinal": j.get("ordinal") if isinstance(j.get("ordinal"), int) else None,
        "greeting": bool(j.get("greeting")),
        "meta": bool(j.get("meta")),
        "intent": j.get("intent") if j.get("intent") in INTENTS else None,
    }


_RECENT_ORDINAL_RE = re.compile(r"\b(most recent\w*|latest|newest|last (?:one|order|purchase))\b")
_OLDEST_ORDINAL_RE = re.compile(r"\b(oldest|earliest|first (?:one|order|purchase))\b")


def _deterministic_recency(text: str):
    """"Most recent"/"latest"/"oldest" etc are a fixed, unambiguous vocabulary - same rationale
    as order-id/email regex - so they're extracted deterministically rather than left purely to
    the LLM, whose sampling is otherwise observed to flip between -1 and null for this exact
    phrasing from one call to the next."""
    t = text.lower()
    if _RECENT_ORDINAL_RE.search(t):
        return -1
    if _OLDEST_ORDINAL_RE.search(t):
        return 0
    return None


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_DATE_RE = re.compile(
    r"\b(" + "|".join(_MONTHS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b", re.I)
_AFTER_RE = re.compile(r"\b(after|since|later than)\b", re.I)
_BEFORE_RE = re.compile(r"\b(before|by|until|earlier than)\b", re.I)


def _extract_date_filter(text: str):
    """"after/before <calendar date>" is a fixed enough pattern (comparator word + month + day)
    to read deterministically - same rationale as order IDs and "most recent"/"oldest": there's
    nothing fuzzy about a date, so no need for an LLM round-trip to parse one."""
    m = _DATE_RE.search(text)
    if not m:
        return None
    month = _MONTHS[m.group(1).lower()]
    day = int(m.group(2))
    year = int(m.group(3)) if m.group(3) else TODAY.year
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if _BEFORE_RE.search(text.lower()):
        return {"op": "before", "date": d.isoformat()}
    if _AFTER_RE.search(text.lower()):
        return {"op": "after", "date": d.isoformat()}
    return None


def extract(text: str) -> dict:
    """Returns {order_id, email, reason, yes_no, ordinal, intent, greeting, date_filter, source}."""
    m = ORDER_RE.search(text)
    e = EMAIL_RE.search(text)
    base = {"order_id": f"BK-{m.group(1) or m.group(2)}" if m else None,
            "email": e.group(0).lower() if e else None,
            "date_filter": _extract_date_filter(text)}

    if llm_enabled():
        llm_out = _llm_understand(text)
        if llm_out is not None:
            result = {**base, **llm_out, "source": "llm"}
            if result["ordinal"] is None:
                result["ordinal"] = _deterministic_recency(text)
            return result

    # Mock mode (no API key) or the live call failed: deterministic fallback so the demo
    # still runs fully offline. Never consulted when a live LLM answer came back above.
    out = {**base, **rule_extract(text), "source": "rules"}
    return out


def clarify_question(question: str, topics: str) -> str:
    """One short, friendly clarifying question - used before ever offering to open a ticket, so the
    agent tries to actually understand the customer instead of defaulting to an escalation."""
    raw = _llm(
        "A customer asked a support question for an online bookstore called Bookly, but it didn't clearly "
        "match any documented policy topic. Ask ONE short, friendly clarifying question to better understand "
        f"what they need - optionally mention 1-2 of these topics if they seem relevant: {topics}. Do not "
        "answer the question yet, and do not mention opening a support ticket or escalating. Reply with ONLY "
        "the clarifying question, no preamble.",
        question, max_tokens=80)
    if raw:
        return raw.strip().strip('"')
    return ("I want to make sure I point you to the right info - could you tell me a bit more about what "
            f"you need help with? For example: {topics}.")


def grounded_answer(question: str, snippets: list) -> str | None:
    """Answer ONLY from KB snippets. Returns None if the snippets don't cover the question."""
    if not llm_enabled():
        return " ".join(s["text"] for s in snippets)
    kb = "\n".join(f"[{s['title']}] {s['text']}" for s in snippets)
    raw = _llm(
        "You are Bookly's friendly support agent. Answer the customer's question using ONLY the policy "
        "excerpts provided. Be concise (2-3 sentences). If the excerpts do not contain the answer, reply "
        "with exactly NO_ANSWER. Never invent prices, timelines or policies.",
        f"Policy excerpts:\n{kb}\n\nCustomer question: {question}")
    if raw is None:
        return " ".join(s["text"] for s in snippets)
    return None if "NO_ANSWER" in raw else raw
