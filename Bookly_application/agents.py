"""Three specialist agents, routed to by topic. Each is a small state machine ("procedure"):

  understand (nlu) -> check required info -> ask / clarify / call tool -> respond

Every turn returns a trace so the UI can show WHY the agent did what it did:
  MULTI-TURN  - agent is collecting a required detail before it can act
  CLARIFY     - request is ambiguous, so the agent asks instead of guessing
  TOOL        - agent called a backend tool (lookup, cancel, escalate to a human, ...)
  GUARDRAIL   - policy enforced in code (verification, confirmation, no-hallucination)
"""
import re

import nlu
import tools
from data import CURRENT_USER_EMAIL, ORDERS, POLICIES

CAPABILITIES_TEXT = (
    "I can help with a few things: check an order's status or tracking, start a return or refund, "
    "cancel an order or update its mailing address (as long as it hasn't shipped yet), or answer "
    "questions about shipping, returns, payments, passwords and more. What would you like help with?"
)


def default_state():
    """Every chat is already "logged in" as CURRENT_USER_EMAIL, so agents start out knowing
    (and never need to ask for or re-verify) whose account they're scoped to."""
    return {"email": CURRENT_USER_EMAIL, "verified": True}


class Trace:
    def __init__(self):
        self.events = []
        self.pending = False  # True = this specialist expects a specific follow-up reply

    def add(self, tag, text, detail=None):
        self.events.append({"tag": tag, "text": text, "detail": detail})

    def expect_reply(self):
        self.pending = True

    def tool(self, name, args, result):
        self.events.append({"tag": "TOOL", "text": f"{name}({', '.join(f'{k}={v!r}' for k, v in args.items())})",
                            "detail": result})
        return result


def nice_date(iso):
    from datetime import date
    return date.fromisoformat(iso).strftime("%a %b %d") if iso else "-"


def order_label(o):
    titles = ", ".join(i["title"] for i in o["items"])
    return f"{o['order_id']} - {titles} (placed {nice_date(o['placed_on'])})"


_CONTINUATION_BLOCKER_RE = re.compile(
    r"\b(orders|another order|different order|other order|which order|all my orders)\b")


class BaseAgent:
    greeting = ""

    def __init__(self):
        self.s = default_state()
        self.history = []
        # Cross-turn memory of the last order this agent actually resolved and talked about.
        # Unlike self.s, this deliberately survives reset() so a short follow-up like "where is
        # it shipping to?" can still refer back to the order from a moment ago.
        self.last_order_id = None
        self._hint_order_id = None

    def reset(self):
        self.s = default_state()

    def handle(self, text: str, ext: dict | None = None, last_order_id: str | None = None):
        trace = Trace()
        self.history.append(text)
        self._hint_order_id = last_order_id
        if re.search(r"\b(start over|restart|reset chat)\b", text.lower()):
            self.s = default_state()
            return "No problem, let's start over. " + self.greeting, trace
        if ext is None:
            ext = nlu.extract(text)
        trace.add("NLU", f"Understood: {', '.join(f'{k}={v}' for k, v in ext.items() if v and k != 'source')}"
                  or "Understood: (no structured details)", {"source": ext["source"]})
        reply = self.step(text, ext, trace)
        return reply, trace

    # ---------- shared: identify and verify the order ----------
    @staticmethod
    def _pick_candidate(cands, text, ext):
        """Try to resolve one order out of several candidates from this message alone -
        either by item title mentioned in the text, or by ordinal/recency (e.g. "the last one")."""
        pick = next((oid for oid in cands
                     if any(i["title"].lower() in text.lower() for i in ORDERS[oid]["items"])), None)
        idx = ext.get("ordinal")
        if not pick and idx is not None and -len(cands) <= idx < len(cands):
            pick = cands[idx]
        return pick

    def resolve_order(self, text, ext, trace):
        """Returns (order, reply). If reply is not None, the agent must ask the customer something."""
        s = self.s
        if ext["email"]:
            if s.get("email") != ext["email"]:
                s["verified"] = False
            s["email"] = ext["email"]
        if ext["order_id"]:
            s["order_id"] = ext["order_id"]
            s.pop("candidates", None)

        # Pending clarification: customer is choosing between several orders
        if s.get("candidates") and not s.get("order_id"):
            cands = s["candidates"]
            pick = self._pick_candidate(cands, text, ext)
            if pick:
                s["order_id"] = pick
                s.pop("candidates")
                trace.add("CLARIFY", f"Customer resolved ambiguity -> {pick}")
            else:
                trace.add("CLARIFY", "Still ambiguous which order - asking again")
                return None, "Sorry, which one did you mean?\n" + "\n".join(
                    f"{n}. {order_label(tools.lookup_order(o))}" for n, o in enumerate(cands, 1))

        if s.get("order_id"):
            order = trace.tool("lookup_order", {"order_id": s["order_id"]}, tools.lookup_order(s["order_id"]))
            if order is None:
                bad = s.pop("order_id")
                trace.add("GUARDRAIL", "Order not found - not guessing, asking customer to re-check")
                return None, (f"I couldn't find an order with the number {bad}. Could you double-check it? "
                              "Order numbers look like BK-1001. You can also give me the email on the order.")
            if not s.get("email"):
                trace.add("MULTI-TURN", "Have order number, missing email -> asking to verify identity")
                return None, f"Thanks! To verify it's you, what's the email address on order {s['order_id']}?"
            if not s.get("verified"):
                ok = trace.tool("verify_customer", {"order_id": s["order_id"], "email": s["email"]},
                                tools.verify_customer(s["order_id"], s["email"]))
                if not ok:
                    s["email"] = None
                    trace.add("GUARDRAIL", "Email does not match order - no order details shared")
                    return None, ("That email doesn't match our records for this order, so I can't share its "
                                  "details. Could you double-check the email you used at checkout?")
                s["verified"] = True
            self.last_order_id = order["order_id"]
            return order, None

        if s.get("email"):
            orders = trace.tool("find_orders_by_email", {"email": s["email"]},
                                [o["order_id"] for o in tools.find_orders_by_email(s["email"])])
            if not orders:
                s["email"] = None
                return None, ("I couldn't find any orders under that email. Could you double-check it, "
                              "or share your order number (e.g. BK-1001)?")
            s["verified"] = True
            if len(orders) == 1:
                s["order_id"] = orders[0]
                return self.resolve_order(text, {"email": None, "order_id": None}, trace)

            pick = self._pick_candidate(orders, text, ext)
            if not pick and self._hint_order_id and self._hint_order_id in orders \
                    and not _CONTINUATION_BLOCKER_RE.search(text.lower()):
                pick = self._hint_order_id
                trace.add("CLARIFY", f"No new order specified - continuing with the order from "
                                     f"earlier in the conversation -> {pick}")
            if pick:
                s["order_id"] = pick
                return self.resolve_order(text, {"email": None, "order_id": None}, trace)

            s["candidates"] = orders
            trace.add("CLARIFY", f"{len(orders)} orders match this email - asking which one instead of guessing")
            return None, "I found a few orders on your account. Which one is this about?\n" + "\n".join(
                f"{n}. {order_label(tools.lookup_order(o))}" for n, o in enumerate(orders, 1))

        trace.add("MULTI-TURN", "Need an order number or email before I can look anything up")
        return None, ("I can help with that. What's your order number (e.g. BK-1001)? "
                      "If you don't have it handy, the email on your account works too.")


# ============================== 1. RETURNS / REFUNDS ==============================
class ReturnAgent(BaseAgent):
    """Collects the order, item(s) and reason, checks whether the return is actually eligible
    (check_return_eligibility - the same policy as the "Return policy" / "eBooks" knowledge-base
    entries: physical books within 30 days of delivery, eBooks only if defective), and only then
    hands it off to a human (escalate_to_human) to process. Eligibility is decided in code, not
    by the model; the refund amount itself is still left to the human."""
    greeting = "Hi! I can help you return an item. Which order is it from? (order number or email)"

    def step(self, text, ext, trace):
        s = self.s
        # --- waiting on a yes/no before handing off to a human ---
        if s.get("awaiting") == "confirm":
            if ext["yes_no"] == "yes":
                summary = (f"Return request - order {s['order_id']}, item(s): {', '.join(s['items'])}, "
                           f"reason: {nlu.REASON_LABELS[s['reason']]}.")
                if s.get("reason_detail"):
                    summary += f" Customer's description: \"{s['reason_detail']}\""
                t = trace.tool("escalate_to_human", {"summary": summary, "email": s.get("email")},
                               tools.escalate_to_human(summary, s.get("email")))
                self.reset()
                return (f"Done! I've sent this to our returns team (ticket {t['ticket_id']}).\n\n"
                        f"They'll email {s.get('email') or 'you'} {t['expected_response']} to confirm "
                        "the refund and send a return label.\n\nAnything else I can help with?")
            if ext["yes_no"] == "no":
                self.reset()
                trace.add("GUARDRAIL", "Customer declined - nothing sent")
                return "No problem, I haven't sent anything. Let me know if you change your mind."
            trace.expect_reply()
            return "Just to confirm - shall I send this to our returns team? (yes / no)"

        if ext["reason"] and not s.get("reason"):
            s["reason"] = ext["reason"]
            # The customer's own words, not just the category - so the human reviewer sees the
            # specifics (e.g. "water marks on multiple pages") instead of only "Damaged or defective".
            s["reason_detail"] = text.strip()

        # --- step 1: which order (multi-turn + clarify) ---
        order, reply = self.resolve_order(text, ext, trace)
        if reply:
            trace.expect_reply()
            return reply

        # --- step 1.5: return window - depends only on the order's delivery date, never on the
        # item or reason, so check it as soon as the order is known instead of collecting details
        # we couldn't act on anyway ---
        if not s.get("window_checked"):
            window = trace.tool("check_return_window", {"order_id": order["order_id"]},
                                tools.check_return_window(order["order_id"]))
            s["window_checked"] = True
            if not window["eligible"]:
                trace.add("GUARDRAIL", "Outside the return window - not asking for item/reason we can't act on")
                msg = f"I'm sorry - {window['reason']}"
                self.reset()
                return msg + "\n\nIs there anything else I can help with?"

        # --- step 2: which item(s) (clarify) ---
        if not s.get("items"):
            titles = [i["title"] for i in order["items"]]
            if len(titles) == 1:
                s["items"] = titles
            else:
                convo = " ".join(self.history).lower()
                picked = [t for t in titles if t.lower() in convo]
                idx = ext.get("ordinal") if s.get("awaiting") == "item" else None
                if s.get("awaiting") == "item" and re.search(r"\b(both|all)\b", text.lower()):
                    picked = titles
                elif not picked and idx is not None and -len(titles) <= idx < len(titles):
                    picked = [titles[idx]]
                if picked:
                    s["items"] = picked
                    s.pop("awaiting", None)
                else:
                    s["awaiting"] = "item"
                    trace.add("CLARIFY", f"Order has {len(titles)} items - asking which to return")
                    trace.expect_reply()
                    return (f"Order {order['order_id']} has {len(titles)} books. Which would you like to return?\n"
                            + "\n".join(f"{n}. {t}" for n, t in enumerate(titles, 1)) + "\n(or say 'both')")

        # --- step 3: reason (multi-turn, clarify if vague) ---
        if not s.get("reason"):
            if s.get("awaiting") == "reason":
                idx = ext.get("ordinal")
                keys = list(nlu.REASON_LABELS)
                if idx is not None and -3 <= idx < 3:
                    s["reason"] = keys[idx]
                else:
                    trace.add("CLARIFY", "Reason is vague - asking for the category so the human has the full picture")
                    trace.expect_reply()
                    return ("Thanks - which of these best describes it?\n"
                            + "\n".join(f"{n}. {l}" for n, l in enumerate(nlu.REASON_LABELS.values(), 1)))
            else:
                s["awaiting"] = "reason"
                trace.add("MULTI-TURN", "Have the item(s) - still need the return reason")
                trace.expect_reply()
                return f"Got it - {', '.join(s['items'])}.\n\nWhat's the reason for the return?"

        # --- step 4: eligibility - now that the item(s) and reason are known, this also catches
        # the eBook defect-only rule (the window itself was already cleared in step 1.5) ---
        if not s.get("eligibility_checked"):
            elig = trace.tool("check_return_eligibility",
                              {"order_id": s["order_id"], "items": s["items"], "reason": s["reason"]},
                              tools.check_return_eligibility(s["order_id"], s["items"], s["reason"]))
            s["eligibility_checked"] = True
            if not elig["eligible"]:
                trace.add("GUARDRAIL", "Not eligible for a return - telling the customer directly instead of escalating")
                msg = f"I'm sorry - {elig['reason']}"
                self.reset()
                return msg + "\n\nIs there anything else I can help with?"

        # --- step 5: summary + explicit confirmation before handing off to a human ---
        s["awaiting"] = "confirm"
        trace.add("GUARDRAIL", "Returns are always handled by a person - asking for confirmation before sending")
        trace.expect_reply()
        description = f"\n- Description: {s['reason_detail']}" if s.get("reason_detail") else ""
        return (f"Here's the summary:\n- Order: {order['order_id']}\n- Item(s): {', '.join(s['items'])}\n"
                f"- Reason: {nlu.REASON_LABELS[s['reason']]}{description}\n\n"
                "I'll pass this to our returns team to process the refund. Shall I send it? (yes / no)")


# ============================== 2. ORDERS (status, cancel, change address) ==============================
class OrderAgent(BaseAgent):
    """Everything about an order that our own systems can resolve directly, no human needed:
    checking status/tracking, cancelling, or updating the mailing address (while it hasn't
    shipped). Status is a plain read, so it's answered as soon as the order is resolved; cancel
    and change-address are writes, so they're gated by check_modifiable and need confirmation."""
    greeting = ("Hi! I can check an order's status, cancel it, or update its mailing address "
                "(as long as it hasn't shipped yet). What's the order number, or the email you ordered with?")
    ACTIONS = {"cancel_order": "cancel the order", "change_address": "update the mailing address"}

    def step(self, text, ext, trace):
        s = self.s
        if ext.get("intent") in self.ACTIONS:
            s["action"] = ext["intent"]

        # --- waiting on cancel confirmation ---
        if s.get("awaiting") == "confirm_cancel":
            if ext["yes_no"] == "yes":
                r = trace.tool("cancel_order", {"order_id": s["order_id"]}, tools.cancel_order(s["order_id"]))
                self.reset()
                return (f"Done - order {r['order_id']} is cancelled. If you were already charged, you'll see a "
                        "refund within 5-7 business days.\n\nAnything else I can help with?")
            if ext["yes_no"] == "no":
                self.reset()
                return "No problem, I haven't cancelled anything. Anything else I can help with?"
            trace.expect_reply()
            return "Just to confirm - shall I cancel this order? (yes / no)"

        # --- waiting on the new address text ---
        if s.get("awaiting") == "new_address":
            addr = text.strip()
            if ext["yes_no"] is not None or len(addr) < 8:
                trace.expect_reply()
                return "What should the new mailing address be? (street, city, state, zip)"
            s["new_address"] = addr
            s["awaiting"] = "confirm_address"
            trace.expect_reply()
            return (f"Just to confirm - update the mailing address on {s['order_id']} to:\n\"{addr}\"\n\n"
                    "Shall I save this? (yes / no)")

        # --- waiting on address-change confirmation ---
        if s.get("awaiting") == "confirm_address":
            if ext["yes_no"] == "yes":
                r = trace.tool("update_mailing_address", {"order_id": s["order_id"], "address": s["new_address"]},
                               tools.update_mailing_address(s["order_id"], s["new_address"]))
                self.reset()
                return (f"Done - the mailing address on {r['order_id']} is updated.\n\n"
                        "Anything else I can help with?")
            if ext["yes_no"] == "no":
                self.reset()
                return "No problem, I haven't changed anything. Anything else I can help with?"
            trace.expect_reply()
            return "Shall I save this new address? (yes / no)"

        # --- a cross-order question ("do any orders arrive after Oct 6?") rather than one
        # specific order - answered directly, without ever asking "which order?" ---
        if ext.get("date_filter") and not s.get("action") and not s.get("order_id") and not s.get("candidates"):
            flt = ext["date_filter"]
            matches = trace.tool(
                "find_orders_by_delivery_date", {"email": s["email"], "op": flt["op"], "date": flt["date"]},
                [o["order_id"] for o in tools.find_orders_by_delivery_date(s["email"], flt["op"], flt["date"])])
            self.reset()
            if not matches:
                return (f"None of your orders have a delivery date {flt['op']} {nice_date(flt['date'])}.\n\n"
                        "Anything else I can help with?")
            if len(matches) == 1:
                # Narrowed to exactly one order, same as resolve_order() does - so a follow-up
                # like "change the address on that" can refer back to it without repeating the ID.
                self.last_order_id = matches[0]
            lines = []
            for oid in matches:
                o = tools.lookup_order(oid)
                ref = o["delivered_on"] or o["eta"]
                verb = "delivered" if o["delivered_on"] else "expected"
                lines.append(f"- {order_label(o)} - {verb} {nice_date(ref)}")
            return (f"Here's what I found with a delivery date {flt['op']} {nice_date(flt['date'])}:\n"
                    + "\n".join(lines) + "\n\nAnything else I can help with?")

        # --- step 1: which order ---
        order, reply = self.resolve_order(text, ext, trace)
        if reply:
            trace.expect_reply()
            return reply

        # --- no cancel/address-change requested: a plain status read, no eligibility gate ---
        if not s.get("action"):
            return self._status_message(order)

        # --- step 2: eligibility - policy decided by code, not the LLM ---
        if not s.get("eligibility_checked"):
            elig = trace.tool("check_modifiable", {"order_id": order["order_id"]},
                              tools.check_modifiable(order["order_id"]))
            s["eligibility_checked"] = True
            if not elig["allowed"]:
                trace.add("GUARDRAIL", "Order can no longer be changed - not promising something we can't do")
                msg = f"I'm sorry - {elig['reason']}"
                self.reset()
                return msg

        if s["action"] == "cancel_order":
            s["awaiting"] = "confirm_cancel"
            trace.add("GUARDRAIL", "Write action - asking for explicit confirmation first")
            trace.expect_reply()
            titles = ", ".join(i["title"] for i in order["items"])
            return (f"Order {order['order_id']} ({titles}) hasn't shipped yet, so it can still be cancelled.\n\n"
                    "Shall I go ahead and cancel it? (yes / no)")

        # action == "change_address"
        s["awaiting"] = "new_address"
        trace.add("MULTI-TURN", "Need the new mailing address before confirming the change")
        trace.expect_reply()
        current = order.get("mailing_address", "on file")
        return (f"The current mailing address on {order['order_id']} is:\n\"{current}\"\n\n"
                "What should the new address be?")

    def _status_message(self, order):
        items = ", ".join(i["title"] for i in order["items"])
        st = order["status"]
        if st == "cancelled":
            msg = f"Order {order['order_id']} ({items}) was cancelled and you were not charged."
        elif st == "processing":
            msg = (f"Order {order['order_id']} ({items}) is being prepared and should ship within 1-2 business "
                   f"days to {order['mailing_address']}. Estimated delivery: {nice_date(order['eta'])}.")
        elif st == "shipped":
            msg = (f"Good news - order {order['order_id']} ({items}) shipped on {nice_date(order['shipped_on'])} "
                   f"via {order['carrier']} (tracking {order['tracking']}) to {order['mailing_address']}. "
                   f"It's expected to arrive {nice_date(order['eta'])}.")
        elif order["items"][0]["format"] == "ebook":
            msg = f"Order {order['order_id']} ({items}) is an eBook and is already in your Bookly library."
        else:
            msg = (f"Order {order['order_id']} ({items}) was delivered on {nice_date(order['delivered_on'])} "
                   f"to {order['mailing_address']}.")
        self.reset()
        return msg + "\n\nIs there another order I can check for you, or anything else?"


# ============================== 3. GENERAL QUESTIONS ==============================
class GeneralAgent(BaseAgent):
    """Answers questions about the Bookly website/policies - shipping, returns, payments,
    passwords - grounded only in the policy knowledge base. No order lookups here: anything
    about a specific order belongs to OrderAgent or ReturnAgent."""
    greeting = "Hi! Ask me anything about shipping, policies, payments or your account."

    def step(self, text, ext, trace):
        s = self.s
        t = text.lower()

        # ----- smalltalk: greet back instead of treating it as an unanswerable FAQ -----
        if not s.get("mode") and ext.get("greeting"):
            trace.add("SMALLTALK", "Greeting detected - responding conversationally, not searching the KB")
            return ("Hello! How are you doing today? I'm here if you'd like to check an order, "
                    "start a return, or ask anything else about Bookly.")

        # ----- "what can you do?" - answer directly, this is true-by-construction, not a KB lookup -----
        if not s.get("mode") and ext.get("meta"):
            trace.add("SMALLTALK", "Capability question detected - describing what I can help with directly")
            return CAPABILITIES_TEXT

        # ----- answering the one clarifying follow-up before a retry ----
        if s.get("mode") == "faq_retry":
            combined = s["question"] + " " + text
            return self._search_kb(combined, trace, already_clarified=True)

        # ----- escalation offer -----
        if s.get("mode") == "escalate":
            if ext["yes_no"] == "yes" or ext["email"]:
                if not (ext["email"] or s.get("email")):
                    trace.add("MULTI-TURN", "Need an email so the human team can reply")
                    s["mode"] = "escalate"
                    s["want_ticket"] = True
                    trace.expect_reply()
                    return "Sure. What email should our team reply to?"
                email = ext["email"] or s.get("email")
                tk = trace.tool("escalate_to_human", {"summary": s["question"], "email": email},
                                tools.escalate_to_human(s["question"], email))
                self.reset()
                return f"Done - ticket {tk['ticket_id']} is open. Our team will email {email} {tk['expected_response']}."
            if s.get("want_ticket"):
                trace.expect_reply()
                return "What email should our team reply to?"
            if ext["yes_no"] == "no":
                self.reset()
                return "No problem. Anything else I can help with?"
            self.reset()  # treat as a new question

        # ----- password reset procedure -----
        if s.get("mode") == "password" or (not s.get("mode") and re.search(
                r"\b(password|log ?in|sign ?in|locked out|can'?t access)\b", t)):
            return self.password_flow(text, ext, trace)

        # ----- answering a clarification about which topic -----
        if s.get("mode") == "faq_clarify":
            opts = s["options"]
            idx = ext.get("ordinal")
            pick = None
            if idx is not None and -len(opts) <= idx < len(opts):
                pick = opts[idx]
            else:
                pick = next((o for o in opts if any(w in t for w in o["title"].lower().split())), None)
            if pick:
                trace.add("CLARIFY", f"Customer chose: {pick['title']}")
                q = s["question"] + f" (about {pick['title']})"
                self.reset()
                return self.answer(q, [pick], trace)
            self.reset()

        # ----- policy Q&A, grounded in the knowledge base -----
        return self._search_kb(text, trace, already_clarified=False)

    def _search_kb(self, question, trace, already_clarified):
        s = self.s
        hits = tools.search_policies(question)
        trace.tool("search_policies", {"query": question},
                   [{"id": h["id"], "score": h["score"], "matched": h["matched"]} for h in hits])
        if not hits:
            return self.no_answer(question, trace, already_clarified)
        top = [h for h in hits if h["score"] == hits[0]["score"]]
        if len(top) > 1 and all(set(h["matched"]) == set(top[0]["matched"]) for h in top):
            # Same vague phrase (e.g. "how long") matched several topics -> genuinely ambiguous
            trace.add("CLARIFY", f"'{', '.join(top[0]['matched'])}' fits {len(top)} topics - asking which one")
            s.update(mode="faq_clarify", options=top, question=question)
            trace.expect_reply()
            return ("Happy to help - just to make sure I give you the right answer, do you mean:\n"
                    + "\n".join(f"{n}. {h['title']}" for n, h in enumerate(top, 1)))
        return self.answer(question, top if len(top) > 1 else [hits[0]], trace, already_clarified)

    def answer(self, question, snippets, trace, already_clarified=False):
        ans = nlu.grounded_answer(question, snippets)
        if ans is None:
            return self.no_answer(question, trace, already_clarified)
        trace.add("GUARDRAIL", f"Answer grounded in KB: {', '.join(s['title'] for s in snippets)}")
        self.reset()
        return ans

    def no_answer(self, question, trace, already_clarified=False):
        if not already_clarified:
            # Try to actually understand the customer before ever offering a ticket.
            trace.add("CLARIFY", "No confident KB match - asking a clarifying question before offering a ticket")
            topics = ", ".join(p["title"] for p in POLICIES)
            followup = nlu.clarify_question(question, topics)
            self.s = {**default_state(), "mode": "faq_retry", "question": question}
            trace.expect_reply()
            return followup
        trace.add("GUARDRAIL", "Still no grounded answer after clarifying - offering a human")
        self.s = {**default_state(), "mode": "escalate", "question": question}
        trace.expect_reply()
        return ("I don't have verified information on that, and I'd rather not guess. "
                "Would you like me to open a ticket with our support team? (yes / no)")

    def password_flow(self, text, ext, trace):
        s = self.s
        s["mode"] = "password"
        if s.get("awaiting") == "confirm":
            if ext["yes_no"] == "yes":
                r = trace.tool("send_password_reset", {"email": s["email"]}, tools.send_password_reset(s["email"]))
                self.reset()
                return (f"Sent! Check {r['email']} for a reset link (valid for {r['expires_in_minutes']} minutes). "
                        "Don't forget to check your spam folder.")
            if ext["yes_no"] == "no":
                self.reset()
                return "Okay, I won't send anything. Anything else I can help with?"
            trace.expect_reply()
            return "Shall I send the reset link? (yes / no)"
        if ext["email"]:
            s["email"] = ext["email"]
        if not s.get("email"):
            trace.add("MULTI-TURN", "Password reset needs the account email")
            trace.expect_reply()
            return "I can send you a password reset link. What's the email address on your Bookly account?"
        exists = trace.tool("customer_exists", {"email": s["email"]}, tools.customer_exists(s["email"]))
        if not exists:
            s.pop("email")
            trace.expect_reply()
            return "I couldn't find an account with that email. Could you double-check it?"
        s["awaiting"] = "confirm"
        trace.add("GUARDRAIL", "Account action - confirming before sending")
        trace.expect_reply()
        return f"I found your account. Shall I send a password reset link to {s['email']}? (yes / no)"


# ============================== UNIFIED CHAT ROUTER ==============================
class UnifiedAgent:
    """Routes a single chat to whichever specialist (orders, returns, or general Q&A) fits the
    message, and keeps following up with the same specialist while it's mid-flow (asking for an
    order number, a reason, a yes/no confirmation, ...). Once a specialist finishes responding,
    the next message is free to be routed anywhere again.
    """
    def __init__(self):
        self.agents = {"orders": OrderAgent(), "return": ReturnAgent(), "general": GeneralAgent()}
        self.active = None
        # Which order the customer was last talking about, shared across specialists so a
        # follow-up like "where is it shipping to?" doesn't have to re-ask from scratch.
        self.last_order_id = None

    def _classify(self, ext):
        intent = ext.get("intent")
        if intent == "return":
            return "return"
        if intent in ("order_status", "cancel_order", "change_address"):
            return "orders"
        if intent == "other":
            return "general"
        return None

    def handle(self, text: str):
        if re.search(r"\b(start over|restart|reset chat)\b", text.lower()):
            for agent in self.agents.values():
                agent.s, agent.history, agent.last_order_id = default_state(), [], None
            self.active = None
            self.last_order_id = None
            trace = Trace()
            return ("No problem, let's start over. How can I help - your order status, "
                    "a return or refund, or something else?"), trace

        # Understand once per turn (Claude, when a key is configured) and reuse the result for
        # both routing and the specialist agent, instead of asking twice.
        ext = nlu.extract(text)

        topic = self._classify(ext)
        if topic:
            self.active = topic
        elif self.active is None:
            self.active = "orders" if ext.get("order_id") else "general"

        agent = self.agents[self.active]
        reply, trace = agent.handle(text, ext=ext, last_order_id=self.last_order_id)
        if agent.last_order_id:
            self.last_order_id = agent.last_order_id
        if not trace.pending:
            self.active = None
        return reply, trace


AGENTS = {"orders": OrderAgent, "return": ReturnAgent, "general": GeneralAgent}
