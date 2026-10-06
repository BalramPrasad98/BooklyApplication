"""Tools the agent can call. These mock a real backend (order system, auth, ticketing).

Thesis: business policy (what's cancellable, return-eligible, verification, no-hallucination)
lives HERE, in code - never in the LLM. Return eligibility is enforced in check_return_eligibility,
mirroring the "Return policy" / "eBooks" knowledge-base entries; the refund amount itself is still
decided by a human after escalate_to_human.
"""
import random
import re
from datetime import date

import data

RETURN_WINDOW_DAYS = 30


def lookup_order(order_id: str):
    order = data.ORDERS.get(order_id)
    return None if order is None else {"order_id": order_id, **order}


def find_orders_by_email(email: str):
    # Sorted oldest -> newest so "the last one" / "the most recent one" means the same thing
    # whether the customer is scanning the displayed list or describing it in plain language.
    matches = [{"order_id": oid, **o} for oid, o in data.ORDERS.items() if o["email"] == email]
    return sorted(matches, key=lambda o: o["placed_on"])


def find_orders_by_delivery_date(email: str, op: str, date_iso: str) -> list:
    """Filter a customer's orders by delivery date - delivered_on once it's arrived, otherwise
    the estimated eta. ISO date strings ("YYYY-MM-DD") sort correctly as plain strings, so no
    date parsing is needed to compare them."""
    matches = []
    for o in find_orders_by_email(email):
        ref = o["delivered_on"] or o["eta"]
        if ref is None:
            continue
        if (op == "after" and ref > date_iso) or (op == "before" and ref < date_iso):
            matches.append(o)
    return matches


def verify_customer(order_id: str, email: str) -> bool:
    order = data.ORDERS.get(order_id)
    return bool(order and order["email"] == email)


def check_modifiable(order_id: str) -> dict:
    """Orders can only be cancelled or have their mailing address changed before they ship."""
    order = data.ORDERS[order_id]
    if order["status"] == "processing":
        return {"allowed": True}
    if order["status"] == "cancelled":
        return {"allowed": False, "reason": f"Order {order_id} is already cancelled."}
    if order["status"] == "shipped":
        return {"allowed": False, "reason": f"Order {order_id} has already shipped, so it can no longer be "
                                            "cancelled or re-addressed. It can still be returned once delivered."}
    return {"allowed": False, "reason": f"Order {order_id} has already been delivered, so it can no longer be "
                                        "cancelled or re-addressed. It can still be returned."}


def check_return_window(order_id: str) -> dict:
    """Whether the order is within the return window at all - this depends only on the order's
    delivery date, never on which item or why, so it's checked as soon as the order is known,
    before asking the customer for details we couldn't act on anyway."""
    order = data.ORDERS.get(order_id)
    if order is None:
        return {"eligible": False, "reason": f"I couldn't find order {order_id}."}
    if order["delivered_on"] is None:
        return {"eligible": False,
                "reason": f"Order {order_id} hasn't been delivered yet, so there's nothing to return yet."}
    days_since = (data.TODAY - date.fromisoformat(order["delivered_on"])).days
    if days_since > RETURN_WINDOW_DAYS:
        return {"eligible": False,
                "reason": f"Order {order_id} was delivered {days_since} days ago, which is outside our "
                          f"{RETURN_WINDOW_DAYS}-day return window."}
    return {"eligible": True}


def check_return_eligibility(order_id: str, items: list, reason: str) -> dict:
    """Full eligibility, same policy as the "Return policy" / "eBooks" knowledge-base entries:
    the order-level return window (check_return_window), plus - once the item(s) and reason are
    known - eBooks are only eligible if the file is defective, never for other reasons."""
    window = check_return_window(order_id)
    if not window["eligible"]:
        return window
    order = data.ORDERS[order_id]
    for title in items:
        item = next((i for i in order["items"] if i["title"] == title), None)
        if item and item["format"] == "ebook" and reason != "damaged":
            return {"eligible": False,
                    "reason": f"\"{title}\" is an eBook, and eBooks can only be returned if the file "
                              "is defective - not for other reasons."}
    return {"eligible": True}


def cancel_order(order_id: str) -> dict | None:
    order = data.ORDERS.get(order_id)
    if order is None:
        return None
    order["status"] = "cancelled"
    return {"order_id": order_id, "status": "cancelled"}


def update_mailing_address(order_id: str, new_address: str) -> dict | None:
    order = data.ORDERS.get(order_id)
    if order is None:
        return None
    order["mailing_address"] = new_address
    return {"order_id": order_id, "mailing_address": new_address}


def customer_exists(email: str) -> bool:
    return email in data.CUSTOMERS


def send_password_reset(email: str) -> dict:
    return {"sent": True, "email": email, "expires_in_minutes": 30}


def escalate_to_human(summary: str, email: str | None = None) -> dict:
    tid = f"TCK-{random.randint(1000, 9999)}"
    data.TICKETS[tid] = {"ticket_id": tid, "summary": summary, "email": email}
    return {"ticket_id": tid, "expected_response": "within 1 business day"}


def search_policies(query: str) -> list:
    """Keyword retrieval over the policy KB. Returns hits sorted by score, with matched keywords."""
    q = query.lower()
    hits = []
    for p in data.POLICIES:
        matched = [k for k in p["keywords"] if re.search(rf"\b{re.escape(k)}\b", q)]
        if matched:
            hits.append({"id": p["id"], "title": p["title"], "text": p["text"],
                         "score": len(matched), "matched": matched})
    return sorted(hits, key=lambda h: -h["score"])
