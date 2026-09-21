"""Pure deterministic rules used after the LLM extraction.

The model proposes values, but these helpers decide whether sensitive CRM
fields have explicit support in a source quote.  Keeping the rules in a small
dependency-free module also makes them easy to test without OpenAI/ML packages.
"""

from __future__ import annotations

import re


# Direct role words are always explicit.  The second and third alternatives
# cover an explicit intention to sell/implement our product.  ``наценить ...
# клиентам`` is included deliberately: it is a common noisy-hall transcription
# of a reseller statement and was observed in the acceptance screenshots.
PARTNER_RE = re.compile(
    r"(?:"
    r"\b(?:партн[её]р\w*|дистрибьютор\w*|реселлер\w*|интегратор\w*|"
    r"partner\w*|distributor\w*|reseller\w*|integrator\w*)\b"
    r"|\b(?:перепрода\w*|продава\w*|реализов\w*|внедря\w*|интегрир\w*)\b"
    r"[^.!?\n]{0,80}\b(?:наш\w*|ваш\w*)?\s*продукт\w*\b"
    r"|\bнацен\w*\b[^.!?\n]{0,80}\bпродукт\w*\b"
    r"[^.!?\n]{0,80}\bклиент\w*\b"
    r"|\b(?:resell\w*|sell\w*|implement\w*|integrat\w*)\b"
    r"[^.!?\n]{0,80}\b(?:our|your)?\s*product\w*\b"
    r")",
    re.IGNORECASE,
)


# A company/test label must never become a job title merely because it appears
# after a person's name.  We accept a position only when the evidence quote
# contains an actual role/title cue.
POSITION_CUE_RE = re.compile(
    r"(?:"
    r"\b(?:директор\w*|руководител\w*|начальник\w*|менеджер\w*|"
    r"специалист\w*|инженер\w*|аналитик\w*|консультант\w*|"
    r"президент\w*|основател\w*|владелец\w*|предпринимател\w*|"
    r"закуп\w*|продаж\w*)\b"
    r"|\b(?:ceo|cto|cfo|coo|cio|cmo|vp)\b"
    r"|\b(?:chief\s+[a-z-]+\s+officer|head\s+of|director|manager|"
    r"engineer|specialist|analyst|consultant|founder|owner|president|"
    r"procurement|sales)\b"
    r"|\b(?:должност\w*|работа\w*\s+(?:как|в\s+должности)|"
    r"position|works?\s+as)\b"
    r")",
    re.IGNORECASE,
)


HIGH_PRIORITY_RE = re.compile(
    r"\b(?:срочн\w*|важн\w*|критич\w*|высок\w*\s+приоритет\w*|"
    r"приоритет\w*\s+(?:высок\w*|high)|asap|urgent|high\s+priority)\b",
    re.IGNORECASE,
)
MEDIUM_PRIORITY_RE = re.compile(
    r"\b(?:средн\w*\s+приоритет\w*|приоритет\w*\s+средн\w*|"
    r"medium\s+priority|normal\s+priority)\b",
    re.IGNORECASE,
)
LOW_PRIORITY_RE = re.compile(
    r"\b(?:не\s+срочн\w*|низк\w*\s+приоритет\w*|"
    r"приоритет\w*\s+низк\w*|low\s+priority|not\s+urgent|no\s+rush)\b",
    re.IGNORECASE,
)


def position_quote_supports(quote: str) -> bool:
    return POSITION_CUE_RE.search(quote or "") is not None


def priority_quote_supports(priority: str | None, quote: str) -> bool:
    if priority == "High":
        # "не срочно" contains the word "срочно" and must win over it.
        return LOW_PRIORITY_RE.search(quote or "") is None and HIGH_PRIORITY_RE.search(
            quote or ""
        ) is not None
    if priority == "Medium":
        return MEDIUM_PRIORITY_RE.search(quote or "") is not None
    if priority == "Low":
        return LOW_PRIORITY_RE.search(quote or "") is not None
    return False


def explicit_high_priority_match(text: str) -> re.Match[str] | None:
    if LOW_PRIORITY_RE.search(text or ""):
        return None
    return HIGH_PRIORITY_RE.search(text or "")

