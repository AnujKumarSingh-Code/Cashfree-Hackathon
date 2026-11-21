from datetime import date

import pytest

from app.signals import extract

T = date(2026, 11, 21)


@pytest.mark.parametrize("text,intent", [
    ("salary 5th ko aayega", "promise"), ("सैलरी 5 तारीख को आएगी", "promise"), ("kal de dunga", "promise"),
    ("branch mein de diya", "already_paid"), ("customer says already paid at branch", "already_paid"), ("कल ब्रांच में जमा कर दिया", "already_paid"),
    ("naukri chali gayi, paise nahi hai", "hardship"), ("नौकरी चली गई", "hardship"), ("வேலை போச்சு", "hardship"),
    ("amount galat hai", "dispute"), ("call mat karo", "do_not_contact"), ("haan link bhej do", "pay_now"),
    ("aadha de sakti hoon", "part_pay"), ("kisi se baat karni hai", "human")])
def test_intents(text, intent):
    s = extract(text, T)
    assert s.intent == intent and s.confidence >= 0.7 and s.evidence


def test_promise_date_is_the_next_occurrence():
    assert extract("salary 5th ko aayega", T).pay_date == "2026-12-05"
    assert extract("25 tarikh ko de dunga", T).pay_date == "2026-11-25"
    assert extract("kal de dunga", T).pay_date == "2026-11-22"


def test_promise_without_a_date_is_not_acted_on():
    s = extract("de dunga", T)
    assert s.intent == "unclear" and s.confidence < 0.7


@pytest.mark.parametrize("text", ["hmm ok", "hello?", "", "   ", None])
def test_unclear_or_empty_never_becomes_an_event(text):
    assert extract(text, T).intent in ("unclear", "none")


def test_already_paid_claim_is_never_read_as_a_promise():
    s = extract("5 tarikh ko branch mein de diya", T)
    assert s.intent == "already_paid" and s.pay_date is None


def test_safety_intents_outrank_payment_intents():
    assert extract("link bhej do lekin amount galat hai", T).intent == "dispute"
    assert extract("call mat karo, kal de dunga", T).intent == "do_not_contact"
