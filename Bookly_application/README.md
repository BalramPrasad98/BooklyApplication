# Bookly Support Agent

A simple prototype of a customer-support agent for Bookly, a fictional online bookstore. It has a Python (Flask) backend and a single web chat, routed behind the scenes to three specialists:

1. **Orders** - status, tracking, cancelling, and changing the mailing address. Resolved directly against our own order data, no human involved.
2. **Return / Refund** - collects the order, item(s) and reason, checks eligibility (30-day window, eBooks only if defective), and only then hands off to a human via a support ticket; the refund amount itself is still left to the human.
3. **General questions** - shipping, policies, payments, password reset: anything about the Bookly website rather than a specific order, answered only from the knowledge base.

## Run it

**Mac:** double-click `Launch Bookly (Mac).command`. If macOS blocks it, right-click it, choose **Open**, then **Open** again.
**Windows:** double-click `Launch Bookly (Windows).bat`.
**Terminal (any OS):**

```bash
cd Bookly_application
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

The app opens at http://localhost:5050. Press Ctrl+C in the terminal to stop it. It needs Python 3.10 or later.

### Optional: use Claude

The app runs fully offline in **mock mode**, where understanding is handled by rules. To let Claude interpret free-form replies (for example, return reasons and yes/no answers) and write FAQ answers grounded in the policy text, copy `.env.example` to `.env` and add your `ANTHROPIC_API_KEY`. The badge in the header shows which mode is active.

## Demo scripts

Tags under each reply show **MULTI-TURN**, **TOOL**, **CLARIFY** and **GUARDRAIL**. Click "Why did the agent do this?" to see the full trace. The chat is always "logged in" as `balram@customer.com`, who has 5 demo orders.

| Specialist | Try typing | Multi-turn | Tool | Clarifying question |
|---|---|---|---|---|
| Orders | `Where is my order?` → `the first one` | Several orders on file, so the agent asks which one before answering | `find_orders_by_email`, `lookup_order` | 5 orders match, so the agent asks which |
| Orders | `cancel my order` → `BK-1005` → `yes` | Resolves the order, then confirms before writing | `check_modifiable`, `cancel_order` | - |
| Orders | `I need to change my address` → `BK-1005` → *(new address)* → `yes` | Resolves the order, collects the new address, then confirms | `check_modifiable`, `update_mailing_address` | - |
| Return / Refund | `I want to return a book` → `the second one` → `Educated` → `I have a problem with it` → `1` → `yes` | Collects order, item(s) and reason, checks eligibility, then confirms before handing off | `check_return_eligibility`, `escalate_to_human` | Which order? Which item? Vague reason → asks for the category |
| General | `How long does it take?` → `refunds` | Answers only from the knowledge base | `search_policies` | "How long" fits shipping *and* refunds, so the agent asks which |
| General | `I forgot my password` → `yes` | Collects/confirms email before sending | `customer_exists`, `send_password_reset` | - |

Also try:

- `Do you offer student discounts?`: not in the knowledge base, so the agent won't guess and offers a ticket.
- `BK-1003` + `wrong@example.com`: the email doesn't match the order, so no details are shared.
- `cancel BK-1003` (shipped) or `change the address on BK-1003`: `check_modifiable` refuses - it's already shipped.
- `return my Sapiens` (BK-1001, delivered 45+ days ago): `check_return_eligibility` refuses - outside the 30-day window.
- `return my Project Hail Mary ebook` for any reason other than a defect: eBooks can only be returned if the file is defective.

Click **Demo orders** in the header to see all test data.

## Architecture

```
Browser (static/index.html)  --POST /api/chat-->  app.py (Flask, per-session)
                                                     |
                                   agents.py  (one procedure / state machine per specialist)
                                     |  nlu.py   regex for IDs + emails; Claude (optional) for fuzzy meaning
                                     |  tools.py lookup_order, verify_customer, check_modifiable,
                                     |           cancel_order, update_mailing_address,
                                     |           check_return_eligibility, search_policies,
                                     |           send_password_reset, escalate_to_human
                                     |  data.py  mock orders, customers, policy knowledge base
```

**Thesis: the LLM owns the conversation; code owns the policy.**

- Order status is a plain read; cancelling or changing the address is gated by `check_modifiable` in `tools.py`, never decided by the model. Returns are gated by `check_return_eligibility` the same way - physical books within 30 days of delivery, eBooks only if defective - before ever being collected and handed off to a human (`escalate_to_human`), who still decides the refund amount.
- General answers come only from the knowledge base. If the knowledge base doesn't cover a question, the agent says so and offers a human.
- Write actions (cancel an order, save a new address, hand off a return, send a reset link) always require an explicit "yes".
- When a request is ambiguous (several orders, several items, a vague reason, a topic that fits more than one policy), the agent asks instead of guessing.

## Assumptions

- All backend systems are mocked. Order dates are generated relative to today, so the scenarios always behave the same.
- Identity is verified by matching order number and email. A real deployment would use authenticated sessions.
- Session memory is in-process only. It resets when the server restarts.
