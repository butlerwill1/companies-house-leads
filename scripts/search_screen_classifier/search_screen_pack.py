"""Evidence pack: a compact, readable extract of a filed annual report.

A whole filing is 30,000 to 60,000 characters, mostly accounting policy and
statutory wording. The screen question needs only what the business does, who
it sells to, how it wins work, and how it is structured. The pack is:

1. the principal-activity text, from the repo's fixed extractor, cut where the
   filing moves on to directors, the auditor or the results;
2. the filing's most informative lines, scored by keyword classes that bear on
   the question, in document order, with accounting-policy wording excluded;
3. any turnover-analysis lines, with the table rows that follow the heading.

Drafter and reviewer read the same pack, stored on the case as ``label_pack``.
Nothing here calls a model.
"""
from __future__ import annotations

import re

from companies_house_core.companies_house_extractor import parse_xhtml_narrative

PRINCIPAL_ACTIVITY_LIMIT = 900
LINES_LIMIT = 4200
TURNOVER_LIMIT = 1300
LINE_LIMIT = 520

# Classes of evidence. A line scores one point per class it touches, so a line
# that names both a product and its buyers outranks one that repeats a word.
_CLASSES: dict[str, re.Pattern[str]] = {
    "activity": re.compile(r"\b(princip(al|le)(ly)? (activit|engaged)|the (company|group) (is|was|are) (a|an|the|engaged|principally)|"
                           r"engaged in|operates?|provides?|provision of|supplies|supplier|manufactur|distribut|sells?\b)", re.I),
    "buyer": re.compile(r"\b(customers?|clients?|consumers?|patients?|guests?|members|visitors|students|pupils|residents|"
                        r"tenants|end.users?|private individuals|households?)\b", re.I),
    "channel": re.compile(r"\b(online|website|web.?site|e.?commerce|digital|app\b|internet|retail|shops?|stores?|branch(es)?|"
                          r"showrooms?|restaurants?|hotels?|clinics?|practices|pharmac|dealership|franchis|wholesale|"
                          r"trade counter|direct.to.consumer|d2c|marketplace)\b", re.I),
    "contract": re.compile(r"\b(tender|framework|procurement|commission(ed|ing)|nhs|local authorit|council|"
                           r"public sector|government|long.term contracts?|concession|pfi)\b", re.I),
    "structure": re.compile(r"\b(holding company|investment company|subsidiar|group|intra.?group|parent|"
                            r"rental income|investment propert|dormant|special purpose)\b", re.I),
    "market": re.compile(r"\b(competit|market|marketing|advertis|brand|repeat|referral|reputation|growth)\b", re.I),
}

_BOILERPLATE = re.compile(
    r"(companies act|true and fair|going concern|statement of directors|directors.? responsibilit|"
    r"accounting polic|financial reporting standard|frs 102|audit exemption|section 4\d\d|"
    r"registered (office|number)|bus:|core:|iso4217|xbrli:|the directors (who|are)|so far as each director|"
    r"approved by the (board|directors)|on behalf of the board|independent (auditor|accountant)|"
    r"critical accounting|useful economic li|impairment|deferred tax|corporation tax|dividends? (paid|proposed)|"
    r"recoverable amount|basic financial|derecognis|defined (contribution|benefit)|pension scheme|"
    r"investment property is carried|carrying (value|amount)|amortis|depreciat|fair value|discounted|"
    r"recognised (at|when|as|in)|measured at|net realisable|lease[sd]? |share.based|foreign currenc|"
    r"secr|streamlined energy|s172|section 172|"
    r"so far as each (person|director)|applications for employment by disabled|make judgements and accounting|"
    r"medium.sized (companies|groups) exemption|small companies exemption|qualifying third party|"
    r"information about matters of concern to employees|policy is to consult and discuss with employees|"
    r"employee involvement|prepared in accordance with the provisions|energy and carbon|"
    r"the directors present|to be appointed|general meeting|reappointed as auditor)",
    re.I,
)

# A line that states outright what the business does is kept even when it
# touches only one evidence class.
_STRONG_ACTIVITY = re.compile(
    r"\b(princip(al|le)(ly)? (activit(y|ies)|engaged)|is (principally |mainly )?engaged in|"
    r"the (company|group|business) (is|was) (a|an|the) )", re.I)

_TURNOVER = re.compile(
    r"(turnover|revenue|sales).{0,40}(analys|class of business|geograph|by (market|activity|segment|destination))|"
    r"(class of business|geographical (market|area)|by activity)",
    re.I,
)

_ACTIVITY_END = re.compile(
    r"\s(Directors\b|Auditor\b|Statement of (directors|disclosure)|Results and dividends|Dividends\b|"
    r"Future developments|Going concern|Financial risk|Political)"
)
_ACTIVITY_HEADING = re.compile(r"^Principal activit(y|ies)\s+", re.I)


def _lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]


def _score(line: str) -> int:
    return sum(1 for pattern in _CLASSES.values() if pattern.search(line))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def principal_activity_text(xhtml: str) -> str:
    """The fixed extractor's principal-activity section, cut where the filing
    moves on to directors, the auditor or the results: the section runs on
    into administrative text that carries no evidence about the business."""
    payload = parse_xhtml_narrative(xhtml)
    section = (payload.get("sections") or {}).get("principal_activity") or {}
    text = re.sub(r"\s+", " ", section.get("text") or "").strip()
    text = _ACTIVITY_HEADING.sub("", text)
    cut = _ACTIVITY_END.search(text)
    return text[: cut.start()].strip() if cut else text


def build_pack(filed_text: str, principal_activity: str = "") -> str:
    """The evidence pack for one filing. ``filed_text`` is the whole filed
    document minus the auditor's report, one block per line."""
    parts: list[str] = []
    activity = principal_activity.strip()
    if activity and activity.lower() not in ("principal activities", "principal activity"):
        parts.append(f"[principal activity] {_clip(activity, PRINCIPAL_ACTIVITY_LIMIT)}")

    lines = _lines(filed_text)
    seen: set[str] = {activity.lower()} if activity else set()
    candidates: list[tuple[int, int, str]] = []
    turnover: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        key = line.lower()
        if key in seen or len(line) < 25:
            continue
        if _TURNOVER.search(line) and len(line) <= LINE_LIMIT:
            rows = [row for row in lines[index + 1:index + 9] if len(row) <= 90 and not _BOILERPLATE.search(row)]
            turnover.append((index, " ".join([line, *rows]) if len(line) <= 90 else line))
        if _BOILERPLATE.search(line):
            continue
        score = _score(line) + (2 if _STRONG_ACTIVITY.search(line) else 0)
        if score >= 2:
            candidates.append((score, index, line))
            seen.add(key)

    # Highest score first, earlier in the document breaking ties, then restored
    # to document order so the pack reads in the order the filing wrote it.
    chosen: list[tuple[int, str]] = []
    used = 0
    for score, index, line in sorted(candidates, key=lambda item: (-item[0], item[1])):
        clipped = _clip(line, LINE_LIMIT)
        if used + len(clipped) > LINES_LIMIT:
            continue
        chosen.append((index, clipped))
        used += len(clipped) + 1
    if chosen:
        parts.append("[key lines] " + " | ".join(line for _, line in sorted(chosen)))

    if turnover:
        block = " | ".join(_clip(line, 260) for _, line in turnover[:4])
        parts.append("[turnover analysis] " + _clip(block, TURNOVER_LIMIT))
    return "\n".join(parts)


_EVIDENCE_CLASSES = ("buyer", "channel", "contract")
_NON_LETTER_SHARE = 0.6
_TABLE_ROWS_AFTER_HEADING = 12


# Short lines that are financial-statement furniture: column headings, row labels
# and page furniture. A cell-per-line table makes up most of a filing's length.
_SHORT_ACCOUNTS_LABEL = re.compile(
    r"^(\W*(note|notes|total|at|as at|for the year|year ended|continued|\(continued\)|balance sheet|statement of|"
    r"profit|loss|net assets|net current|tangible|intangible|fixed assets|current assets|debtors|creditors|stocks?|"
    r"cash|bank|share capital|called up|reserves|retained|depreciation|amortisation|cost|carrying|opening|closing|"
    r"charge for|additions|disposals|transfers|eliminated|residual|interest|taxation|tax on|dividends?|deferred|"
    r"provisions?|accruals|prepayments|trade|other|group|company|page \d|\d)|.*\b(limited|ltd|plc|llp)\W*$)",
    re.I,
)
_SHORT_LINE = 60


# Where the accounting policies start, the filing has left its narrative: what
# follows is policies, tax, pensions, debtors and similar. Past that point only
# lines that still carry evidence are kept.
_NOTES_START = re.compile(r"^(accounting (convention|policies)|statement of accounting policies|basis of preparation)\b", re.I)
_NOTES_EVIDENCE = re.compile(
    r"\b(related part|parent|controlling party|subsidiar|principal activit|customers?|clients?|consumers?|patients?|"
    r"guests?|members|residents|tenants|website|online|retail|wholesale|franchis|concession|nhs|council|"
    r"local authorit|tender|framework)", re.I)


def _non_letter_share(line: str) -> float:
    letters = sum(ch.isalpha() for ch in line)
    return 1 - letters / max(len(line), 1)


def clean_full_text(filed_text: str) -> tuple[str, dict[str, int]]:
    """The whole filed report minus text that cannot bear on the question.

    Dropped: statutory and accounting-policy lines (the pack's boilerplate list)
    that touch none of the buyer, channel, contract or activity keyword groups,
    and numeric table rows, except the rows that follow a turnover-analysis
    heading, which name the classes of business. Everything else is kept, in
    order, so a reader sees the filing rather than a summary of it. The auditor's
    report is already gone (``filed_report_text``). Returns the text and a count
    of what was dropped, so the reduction is visible rather than assumed.
    """
    kept: list[str] = []
    dropped = {"boilerplate": 0, "numeric": 0, "labels": 0, "notes": 0, "front": 0}
    protect_until = -1
    notes_from: int | None = None
    lines = _lines(filed_text)
    # The company-information page (directors, addresses, auditor) precedes the
    # contents list and says nothing about the business.
    contents_at = next((i for i, line in enumerate(lines[:80]) if line.strip().lower() == "contents"), None)
    if contents_at is not None and contents_at > 4:
        dropped["front"] = contents_at - 1
        lines = lines[:1] + lines[contents_at:]
    for index, line in enumerate(lines):
        if _TURNOVER.search(line):
            protect_until = index + _TABLE_ROWS_AFTER_HEADING
        if _NOTES_START.match(line) and notes_from is None:
            notes_from = index
        if index <= protect_until:
            kept.append(line)
            continue
        if notes_from is not None and index >= notes_from and not _NOTES_EVIDENCE.search(line)                 and not _STRONG_ACTIVITY.search(line):
            dropped["notes"] += 1
            continue
        evidence = bool(_STRONG_ACTIVITY.search(line)) or any(_CLASSES[name].search(line) for name in _EVIDENCE_CLASSES)
        if _BOILERPLATE.search(line) and not evidence:
            dropped["boilerplate"] += 1
            continue
        if _non_letter_share(line) >= _NON_LETTER_SHARE and not evidence:
            dropped["numeric"] += 1
            continue
        if len(line) <= _SHORT_LINE and _SHORT_ACCOUNTS_LABEL.match(line) and not evidence:
            dropped["labels"] = dropped.get("labels", 0) + 1
            continue
        kept.append(line)
    return "\n".join(kept), dropped
