"""Rule-based signal extraction from field notes and call transcripts.

Output is evidence, never a verified payment. Every signal carries the phrase it relied on
and a confidence. Below the confidence floor the case goes to a human.
No LLM is used here, so the mapping from words to events is fully traceable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date, timedelta

# intent -> phrases (Latin script lowercased, plus Devanagari / Tamil / Kannada forms)
PHRASES: dict[str, list[str]] = {
    "do_not_contact": ["call mat", "phone mat", "do not call", "don't call", "dont call", "stop calling",
                       "कॉल मत", "फोन मत", "फ़ोन मत", "call pannadheenga", "கால் பண்ணாதீங்க"],
    "dispute": ["galat amount", "amount galat", "wrong amount", "not my loan", "mera loan nahi", "dispute",
                "fraud", "complaint", "shikayat", "extra charge", "zyada charge", "hisaab galat",
                "गलत", "शिकायत", "தவறான தொகை", "ತಪ್ಪು ಮೊತ್ತ"],
    "hardship": ["naukri chali", "job chali", "job loss", "lost my job", "naukri nahi", "hospital", "bimar",
                 "beemar", "accident", "guzar gaye", "death in", "paise nahi", "paisa nahi", "medical",
                 "fasal kharab", "crop fail", "kaam band", "नौकरी चली", "नौकरी नहीं", "अस्पताल", "बीमार",
                 "पैसे नहीं", "पैसा नहीं", "வேலை போச்சு", "மருத்துவமனை", "ಕೆಲಸ ಹೋಯಿತು", "ಆಸ್ಪತ್ರೆ"],
    "already_paid": ["de diya", "de diye", "de chuka", "de chuki", "jama kar", "jama kiya", "pay kar diya",
                     "payment kar diya", "payment ho gaya", "bhar diya", "chuka diya", "already paid",
                     "paid at branch", "paid in branch", "branch mein", "branch me de", "paid yesterday",
                     "दे दिया", "जमा कर", "भर दिया", "चुका दिया", "पेमेंट कर दिया", "கட்டிட்டேன்",
                     "கட்டிவிட்டேன்", "ಕಟ್ಟಿದ್ದೇನೆ", "ಕಟ್ಟಿದೆ"],
    "part_pay": ["thoda", "aadha", "adha", "half", "part payment", "partial", "kuch paise", "kuch amount",
                 "itna hi", "आधा", "थोड़ा", "थोडा", "कुछ पैसे", "பாதி", "ಅರ್ಧ"],
    "human": ["manager se baat", "insaan se", "agent se baat", "kisi se baat", "talk to a person",
              "talk to someone", "speak to a person", "human", "बात करा", "किसी से बात"],
    "pay_now": ["abhi pay", "abhi bhar", "abhi kar", "link bhej", "send link", "send the link", "pay now",
                "haan bhej", "bhej do", "bhej dijiye", "लिंक भेज", "अभी भर", "अभी कर", "भेज दो", "भेज दीजिए",
                "லிங்க் அனுப்புங்க", "ಲಿಂಕ್ ಕಳುಹಿಸಿ"],
}
PRIORITY = ["do_not_contact", "dispute", "hardship", "already_paid", "part_pay", "promise", "pay_now", "human"]

_DAY_PATTERNS = [
    r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:ko|tarikh|tareekh|taarikh|tarik|date|तारीख|को|ம் தேதி|தேதி|ನೇ ತಾರೀಖು)",
    r"(?:salary|pagaar|pagar|tankhwah|वेतन|सैलरी|சம்பளம்|ಸಂಬಳ)\D{0,12}(\d{1,2})",
    r"(\d{1,2})\s*(?:st|nd|rd|th)\b",
]
_RELATIVE = [(["parso", "day after tomorrow", "परसों"], 2), (["kal ", "kal.", "tomorrow", "कल", "நாளை", "ನಾಳೆ"], 1),
             (["agle hafte", "next week", "अगले हफ्ते", "अगले हफ़्ते"], 7)]
_PROMISE_WORDS = ["de dunga", "de dungi", "kar dunga", "kar dungi", "bhar dunga", "bhar dungi", "aayega",
                  "aayegi", "aa jayegi", "aa jayega", "will pay", "pay kar dunga", "दे दूंगा", "दे दूंगी",
                  "कर दूंगा", "आएगा", "आएगी", "salary", "pagaar", "सैलरी", "கட்டுறேன்", "ಕಟ್ಟುತ್ತೇನೆ"]


@dataclass
class Signal:
    intent: str            # one of PRIORITY, or "unclear" / "none"
    confidence: float
    evidence: str
    pay_date: str | None = None   # ISO date for a dated promise
    also: list[str] | None = None  # other intents that also matched

    def to_dict(self) -> dict:
        return asdict(self)


def _next_day_of_month(day: int, today: date) -> date | None:
    if not 1 <= day <= 31:
        return None
    for add in range(0, 62):
        d = today + timedelta(days=add)
        if d.day == day:
            return d
    return None


def find_pay_date(text: str, today: date) -> tuple[str | None, str | None]:
    t = text.lower()
    for words, add in _RELATIVE:
        for w in words:
            if w in t + " ":
                return (today + timedelta(days=add)).isoformat(), w.strip()
    for pat in _DAY_PATTERNS:
        m = re.search(pat, t)
        if m:
            d = _next_day_of_month(int(m.group(1)), today)
            if d:
                return d.isoformat(), m.group(0).strip()
    return None, None


def extract(text: str | None, today: date) -> Signal:
    if not text or not str(text).strip():
        return Signal("none", 0.0, "")
    t = " " + str(text).lower().strip() + " "
    hits: dict[str, str] = {}
    for intent, phrases in PHRASES.items():
        for p in phrases:
            if p in t:
                hits[intent] = p
                break
    pay_date, date_ev = find_pay_date(t, today)
    promise_word = next((w for w in _PROMISE_WORDS if w in t), None)
    if pay_date and "already_paid" not in hits:
        hits["promise"] = date_ev or promise_word or ""
    elif promise_word and not hits:
        # a promise with no date is not actionable on its own
        return Signal("unclear", 0.4, promise_word, None, ["promise_without_date"])
    if not hits:
        return Signal("unclear", 0.2, "")
    ordered = [i for i in PRIORITY if i in hits]
    top = ordered[0]
    others = ordered[1:]
    # compatible combinations do not reduce confidence
    compatible = {("part_pay", "promise"), ("part_pay", "pay_now"), ("promise", "pay_now"), ("hardship", "part_pay"),
                  ("hardship", "promise"), ("promise", "human"), ("pay_now", "human"), ("part_pay", "human")}
    conflict = any((top, o) not in compatible and (o, top) not in compatible for o in others)
    conf = 0.6 if conflict else 0.9
    return Signal(top, conf, hits[top], pay_date if top in ("promise", "part_pay", "hardship") else None,
                  others or None)
