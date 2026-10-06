"""Mock Bookly backend data: customers, orders and the policy knowledge base.

Dates are generated relative to today so the demo scenarios always behave the same
(e.g. one order is always inside the 30-day return window, one is always outside it).
"""
from datetime import date, timedelta

TODAY = date.today()


def days_ago(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


def days_ahead(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


CUSTOMERS = {
    "balram@customer.com": {"name": "Balram"},
}

# This is a single-user demo: the chat is always "logged in" as this customer, so agents
# never need to ask for or verify an email - every lookup is scoped to this account.
CURRENT_USER_EMAIL = "balram@customer.com"

DEFAULT_ADDRESS = "214 Oak Street, Austin, TX 73301"

ORDERS = {
    # Balram has five orders -> triggers "which order?" clarification when only email is given.
    # Order numbers increase with placed_on (higher number = placed more recently), and status
    # trends accordingly: the oldest order is long delivered, the newest hasn't shipped yet.
    "BK-1001": {
        "email": "balram@customer.com", "placed_on": days_ago(50), "status": "delivered",
        "carrier": "USPS", "tracking": "9400111202777", "shipped_on": days_ago(48),
        "eta": None, "delivered_on": days_ago(45), "mailing_address": DEFAULT_ADDRESS,
        "items": [{"title": "Sapiens", "format": "paperback", "price": 19.99}],
    },
    "BK-1002": {
        "email": "balram@customer.com", "placed_on": days_ago(18), "status": "delivered",
        "carrier": "USPS", "tracking": "9400111202555", "shipped_on": days_ago(16),
        "eta": None, "delivered_on": days_ago(13), "mailing_address": DEFAULT_ADDRESS,
        # Two items -> triggers "which item?" clarification on returns
        "items": [
            {"title": "Educated", "format": "hardcover", "price": 27.00},
            {"title": "Atomic Habits", "format": "hardcover", "price": 24.50},
        ],
    },
    "BK-1003": {
        "email": "balram@customer.com", "placed_on": days_ago(7), "status": "shipped",
        "carrier": "UPS", "tracking": "1Z84A9320391", "shipped_on": days_ago(4),
        "eta": days_ahead(2), "delivered_on": None, "mailing_address": DEFAULT_ADDRESS,
        "items": [{"title": "Dune", "format": "paperback", "price": 18.99}],
    },
    "BK-1004": {
        # eBook -> delivered instantly the day it was placed
        "email": "balram@customer.com", "placed_on": days_ago(3), "status": "delivered",
        "carrier": None, "tracking": None, "shipped_on": None,
        "eta": None, "delivered_on": days_ago(3), "mailing_address": DEFAULT_ADDRESS,
        "items": [{"title": "Project Hail Mary", "format": "ebook", "price": 14.99}],
    },
    "BK-1005": {
        # Most recent order, still processing (not shipped) -> the only order eligible to
        # cancel / change address
        "email": "balram@customer.com", "placed_on": days_ago(1), "status": "processing",
        "carrier": None, "tracking": None, "shipped_on": None,
        "eta": days_ahead(5), "delivered_on": None, "mailing_address": DEFAULT_ADDRESS,
        "items": [{"title": "The Midnight Library", "format": "paperback", "price": 16.99}],
    },
}

TICKETS: dict = {}   # created by escalate_to_human()

_DATE_FIELDS = ("placed_on", "shipped_on", "eta", "delivered_on")


def refresh_dates() -> None:
    """Keep ORDERS current if the server has been running across a day boundary.

    TODAY (and every date derived from it) is computed once at import time, so a
    long-lived process would otherwise show increasingly stale dates. This shifts
    every stored date by however many days have passed since the last refresh,
    which preserves the original relative offsets (and any mutations made during
    the demo, e.g. cancel_order/update_mailing_address) instead of recomputing
    everything from scratch.
    """
    global TODAY
    today = date.today()
    delta = (today - TODAY).days
    if delta == 0:
        return
    for order in ORDERS.values():
        for field in _DATE_FIELDS:
            val = order.get(field)
            if val:
                order[field] = (date.fromisoformat(val) + timedelta(days=delta)).isoformat()
    TODAY = today

# Knowledge base. The agent may ONLY answer general questions from this text.
POLICIES = [
    {
        "id": "shipping", "title": "Shipping times & costs",
        "keywords": ["shipping", "delivery", "deliver", "how long", "arrive", "express",
                     "standard shipping", "free shipping", "shipping cost"],
        "text": "Standard shipping takes 3-5 business days and costs $4.99 (free on orders over $35). "
                "Express shipping takes 1-2 business days and costs $12.99. Orders placed before 2pm ET "
                "ship the same business day.",
    },
    {
        "id": "international", "title": "International shipping",
        "keywords": ["international", "internationally", "abroad", "overseas", "outside the us",
                     "country", "canada", "uk", "customs"],
        "text": "Bookly ships to 40+ countries. International delivery takes 7-14 business days and costs "
                "a flat $14.99. Customs duties, if any, are paid by the recipient.",
    },
    {
        "id": "returns", "title": "Return policy",
        "keywords": ["return", "returns", "return policy", "send back", "exchange"],
        "text": "Physical books can be returned within 30 days of delivery in their original condition. "
                "Returns for damaged or wrong items are free; for other reasons a $3.99 return shipping fee "
                "is deducted from the refund. Start a return in the Return/Refund panel.",
    },
    {
        "id": "refund_timing", "title": "Refund timing",
        "keywords": ["refund", "refunds", "money back", "how long", "credited", "reimburse"],
        "text": "Refunds are issued to the original payment method within 5-7 business days after we "
                "receive the returned item. You'll get an email when it's processed.",
    },
    {
        "id": "ebooks", "title": "eBooks",
        "keywords": ["ebook", "ebooks", "e-book", "digital", "kindle", "download", "audiobook"],
        "text": "eBooks are delivered instantly to your Bookly library. eBooks cannot be returned once "
                "downloaded, unless the file is defective - in that case contact support within 30 days.",
    },
    {
        "id": "cancellation", "title": "Cancelling or changing an order",
        "keywords": ["cancel", "cancellation", "change my order", "modify", "change the address"],
        "text": "Orders can be cancelled or changed within 1 hour of being placed, or any time before "
                "they ship, from the Orders page of your account. Shipped orders can't be cancelled but "
                "can be returned.",
    },
    {
        "id": "payment", "title": "Payment methods",
        "keywords": ["pay", "payment", "credit card", "paypal", "apple pay", "gift card", "debit"],
        "text": "Bookly accepts Visa, Mastercard, Amex, PayPal, Apple Pay and Bookly gift cards.",
    },
    {
        "id": "password", "title": "Password reset",
        "keywords": ["password", "log in", "login", "sign in", "locked out"],
        "text": "You can reset your password from the sign-in page, or I can send a reset link to the "
                "email on your account.",
    },
    {
        "id": "contact", "title": "Contacting a human",
        "keywords": ["hours", "contact", "phone", "human", "agent", "speak to", "representative"],
        "text": "Bookly's human support team is available Monday-Friday, 9am-6pm ET, by email at "
                "help@bookly.example. I can also open a ticket for you.",
    },
]
