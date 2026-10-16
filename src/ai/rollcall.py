"""Detect "the teacher called on me" moments in a lecture transcript.

Design (hybrid, agreed 2026-10-17):

  1. :func:`coarse_filter` — deterministic and offline.  Walks the ASR
     segments (``{start_ms, end_ms, text}``, produced by
     ``src/ai/transcriber.py``) and keeps only the ones that look like
     roll-call, a question, or a mention of this student's name / student id.
     Adjacent hits are merged into events and the list is capped, so the stage
     stays cheap even for a 135-minute lecture.
  2. :func:`judge` — sends **only those candidates** (never the whole
     transcript) through the already-configured LLM provider chain and asks
     for an A/B/C verdict per candidate: A = called on me, B = asked me a
     question, C = unrelated.

Hits are surfaced as a section at the top of the existing "documents
generated" email (``src/api/notifier.py``); nothing extra is sent when there
are no hits.  The whole module is inert unless ``STUDENT_NAME`` (a GitHub
Secret, comma-separated aliases) and/or ``StuId`` are set, so a deployment
that never configures it behaves exactly as before.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from html import escape

from src.runtime import config

CN_DIGITS = "零一二三四五六七八九"

# Merge candidates whose gap is at most this long (ms) into one event.
MERGE_GAP_MS = 20_000
# Per-candidate text handed to the LLM.
MAX_CANDIDATE_CHARS = 220
# How much of the previous segment travels along as context.
MAX_CONTEXT_CHARS = 80
# Hard cap on the merged candidate text.
MAX_MERGED_CHARS = 440

TAG_ROLLCALL = "点名/考勤"
TAG_QUESTION = "提问"
TAG_IDENTITY = "念到你的名字/学号"

_ROLLCALL_RE = re.compile(
    r"点名|点个名|点到|签到|签一下|签个到|考勤|点到名|点到谁|"
    r"谁没来|谁还没来|有没有来|到了吗|到了没|都到了|人齐|缺勤|应到|实到|"
    r"举手|举个手|"
    r"第[一二三四五六七八九十百\d]{1,3}(排|组|列|行|位)|"
    r"(这|那)一?(排|组|列|行)"
)

_QUESTION_RE = re.compile(
    r"提问|问一下|问个问题|回答一下|回答我|答一下|"
    r"(请|让)?你(来)?(回答|说一下|说说|讲讲|讲一下|读一下|念一下)|"
    r"谁(来|能|想)(回答|说|讲|答)|"
    r"(这位|那位|哪一位|哪一位同学|哪位同学|哪个同学)|"
    r"(后面|前面|旁边|中间|左边|右边|靠窗|靠门|最后一排|第一排)"
    r"(那位|这位|的同学|的男同学|的女同学)?|"
    r"(听懂了吗|会了吗|明白了吗|懂了吗|清楚了吗)|"
    r"(同学|同学你).{0,6}(说|讲|答|来|回答)|"
    r"(来|那).{0,3}(你|您).{0,4}(说|讲)"
)

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


# ── identity ────────────────────────────────────────────────────────────

@dataclass
class Identity:
    """Who "me" is, for matching purposes."""

    names: list[str] = field(default_factory=list)
    student_id: str = ""
    id_variants: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.names or self.id_variants)

    def describe(self) -> str:
        parts = []
        if self.names:
            parts.append("姓名/别名：" + "、".join(self.names))
        if self.student_id:
            parts.append(
                "学号：%s（后四位 %s）" % (self.student_id, self.student_id[-4:])
            )
        return "；".join(parts) if parts else "（未配置）"


def id_variants(student_id: str) -> list[str]:
    """Written forms a lecturer might read the student id out in."""
    sid = (student_id or "").strip()
    if not sid:
        return []
    out: list[str] = []
    if len(sid) >= 6:
        out.append(sid)
    tail = sid[-4:] if len(sid) >= 4 else sid
    for variant in (
        tail,
        " ".join(tail),
        "".join(CN_DIGITS[int(c)] if c.isdigit() else c for c in tail),
    ):
        if variant and variant not in out:
            out.append(variant)
    return out


def load_identity() -> Identity:
    names = list(config.STUDENT_NAMES)
    sid = (config.STUDENT_ID or "").strip()
    return Identity(names=names, student_id=sid, id_variants=id_variants(sid))


# ── stage 1: coarse filter ──────────────────────────────────────────────

@dataclass
class Candidate:
    index: int = 0
    start_ms: int = 0
    end_ms: int = 0
    text: str = ""
    context: str = ""
    reasons: list[str] = field(default_factory=list)
    verdict: str = ""
    score: int = 0


def _match(text: str, ident: Identity) -> tuple[list[str], int]:
    """Return (reason tags, score) for one segment."""
    reasons: list[str] = []
    score = 0

    hit_identity = [n for n in ident.names if n and n in text]
    for variant in ident.id_variants:
        if not variant or variant not in text:
            continue
        # A bare 4-digit tail that reads like a year ("2024") shows up in
        # ordinary speech constantly; only trust it next to a roll-call cue.
        if _YEAR_RE.match(variant) and not _ROLLCALL_RE.search(text):
            continue
        hit_identity.append(variant)
    if hit_identity:
        reasons.append(TAG_IDENTITY)
        score += 3

    if _ROLLCALL_RE.search(text):
        reasons.append(TAG_ROLLCALL)
        score += 2
    if _QUESTION_RE.search(text):
        reasons.append(TAG_QUESTION)
        score += 1
    return reasons, score


def _merge(cands: list[Candidate]) -> list[Candidate]:
    out: list[Candidate] = []
    for c in cands:
        if out and c.start_ms - out[-1].end_ms <= MERGE_GAP_MS:
            last = out[-1]
            if len(last.text) < MAX_MERGED_CHARS:
                last.text = (last.text + " " + c.text)[:MAX_MERGED_CHARS]
            last.end_ms = max(last.end_ms, c.end_ms)
            for r in c.reasons:
                if r not in last.reasons:
                    last.reasons.append(r)
            last.score += c.score
        else:
            out.append(c)
    return out


def _cap(cands: list[Candidate], cap: int) -> list[Candidate]:
    if len(cands) <= cap:
        return cands
    keep = sorted(cands, key=lambda c: (-c.score, c.start_ms))[:cap]
    keep.sort(key=lambda c: c.start_ms)
    return keep


def coarse_filter(segments, ident: Identity,
                  max_candidates: int | None = None) -> list[Candidate]:
    """Offline screen over ASR segments.  Returns merged, capped candidates."""
    if not ident.active:
        return []
    cap = max_candidates or config.ROLLCALL_MAX_CANDIDATES
    cands: list[Candidate] = []
    prev_text = ""
    for seg in segments or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        reasons, score = _match(text, ident)
        if reasons:
            cands.append(Candidate(
                start_ms=int(seg.get("start_ms") or 0),
                end_ms=int(seg.get("end_ms") or 0),
                text=text[:MAX_CANDIDATE_CHARS],
                context=prev_text[-MAX_CONTEXT_CHARS:],
                reasons=reasons,
                score=score,
            ))
        prev_text = text
    return _cap(_merge(cands), cap)


# ── stage 2: LLM verdict ────────────────────────────────────────────────

_JUDGE_SYSTEM = """你是课堂录音转写的复核助手。判断老师是否**点名或提问了某位特定同学**。

你会收到该学生的身份信息，以及若干候选转写片段。转写来自 ASR，人名常被识别成同音字或错字，请按上下文语义判断，不要只做字面匹配。

对每个候选编号给出一个判定，用单个大写字母：
A = 老师在点名 / 考勤 / 叫他回答，且对象**是这名学生**；
B = 老师向这名学生提问或请他回答（未必叫名字）；
C = 与这名学生无关（在叫别人、泛泛提问、只是顺带提到）。

严格只输出判定行，每行形如 `<编号> <字母>`，编号按输入顺序，一个都不能少。不要输出解释、标题、表格或任何其他文字。"""

_VERDICT_RE = re.compile(
    r"^\s*\[?\s*(\d{1,3})\s*\]?\s*[-—–:：.|、,，]?\s*([ABC])(?![A-Za-z])",
    re.M,
)


def build_judge_prompt(ident: Identity, cands: list[Candidate]) -> str:
    lines = ["学生身份：", "  " + ident.describe(), ""]
    lines.append("候选片段（共 %d 条，时间格式 mm:ss）：" % len(cands))
    for c in cands:
        ctx = " … 上文：%s" % c.context if c.context else ""
        lines.append("[%d] %s [%s]%s" % (c.index, mmss(c.start_ms),
                                         "+".join(c.reasons), ctx))
        lines.append("    本句：%s" % c.text)
    lines.append("")
    lines.append("请输出每条的判定：")
    return "\n".join(lines)


def judge(summarizer, cands: list[Candidate], ident: Identity,
          timeout: int = 180) -> tuple[dict[int, str], str]:
    """Ask the LLM chain for a verdict on each candidate.

    Returns ({candidate index: "A"|"B"|"C"}, model_used).  Raises when the
    chain fails — the caller decides whether to fall back to rules only.
    """
    if not cands or summarizer is None:
        return {}, ""
    # ``collect`` numbers the candidates before calling us, but ``judge`` is
    # also usable on its own — renumber so the prompt never shows the dataclass
    # default ``0``, which the ``1 <= idx <= len(cands)`` filter below would
    # then silently discard.
    for i, c in enumerate(cands, 1):
        c.index = i
    text, model = summarizer.complete(
        _JUDGE_SYSTEM, build_judge_prompt(ident, cands), timeout=timeout,
    )
    verdicts: dict[int, str] = {}
    for m in _VERDICT_RE.finditer(text or ""):
        idx = int(m.group(1))
        if 1 <= idx <= len(cands):
            verdicts[idx] = m.group(2)
    return verdicts, model


# ── orchestration ───────────────────────────────────────────────────────

def collect(summarizer, course_title: str, sub_title: str, date: str,
            segments, ident: Identity | None = None) -> list[dict]:
    """Run both stages for one lecture and return the hit dicts.

    Never raises: a detection failure must not cost us the summary.
    """
    ident = ident or load_identity()
    if not config.ROLLCALL_DETECT or not ident.active:
        return []
    try:
        cands = coarse_filter(segments, ident)
        if not cands:
            return []
        for i, c in enumerate(cands, 1):
            c.index = i

        source = "rule-only"
        if config.ROLLCALL_LLM and summarizer is not None:
            try:
                verdicts, model = judge(summarizer, cands, ident)
            except Exception as e:  # noqa: BLE001 - degraded, not fatal
                print("[RollCall] LLM judge failed (%s: %s); "
                      "falling back to rule-only hits"
                      % (type(e).__name__, e), flush=True)
                verdicts, model = {}, ""
            if verdicts:
                for c in cands:
                    c.verdict = verdicts.get(c.index, "C")
                hits = [c for c in cands if c.verdict in ("A", "B")]
                source = model or "llm"
            else:
                print("[RollCall] LLM judge returned no parsable verdicts; "
                      "falling back to rule-only hits", flush=True)
                hits = [c for c in cands if TAG_IDENTITY in c.reasons]
        else:
            hits = [c for c in cands if TAG_IDENTITY in c.reasons]

        out = [
            {
                "course_title": course_title,
                "sub_title": sub_title,
                "date": date,
                "start_ms": c.start_ms,
                "time": mmss(c.start_ms),
                "kind": c.verdict or "A",
                "reasons": list(c.reasons),
                "text": c.text,
                "source": source,
            }
            for c in hits
        ]
        print("[RollCall] %s %s: %d segment(s) → %d candidate(s) → %d hit(s) "
              "(%s)" % (course_title, sub_title, len(segments or []),
                        len(cands), len(out), source), flush=True)
        return out
    except Exception as e:  # noqa: BLE001 - degraded, not fatal
        print("[RollCall] detection failed (%s: %s)"
              % (type(e).__name__, e), flush=True)
        return []


# ── formatting helpers (used by src/api/notifier.py) ────────────────────

def mmss(ms: int) -> str:
    total = max(0, int(ms) // 1000)
    return "%02d:%02d" % (total // 60, total % 60)


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def kind_label(kind: str) -> str:
    return {"A": "点名", "B": "提问"}.get(kind, "疑似")


def _groups(hits: list[dict]) -> "OrderedDict[tuple, list[dict]]":
    groups: "OrderedDict[tuple, list[dict]]" = OrderedDict()
    for h in hits or []:
        key = (h.get("course_title") or "", h.get("sub_title") or "",
               (h.get("date") or "").strip())
        groups.setdefault(key, []).append(h)
    return groups


def format_plain(hits: list[dict], limit: int = 8) -> list[str]:
    """Plain-text lines for the notification email (empty when no hits)."""
    if not hits:
        return []
    out = ["", "=" * 48, "⚠ 本节点名 / 提问（可能叫到你）", "=" * 48]
    for (course, sub, date), items in _groups(hits).items():
        head = ("%s %s" % (course, sub)).strip()
        if date:
            head += "（%s）" % date
        out.append("【%s】" % head)
        for h in items[:limit]:
            out.append("  - %s %s 「%s」" % (
                kind_label(h.get("kind", "")), h.get("time", ""),
                _clip(h.get("text", ""), 60)))
        if len(items) > limit:
            out.append("  … 还有 %d 处" % (len(items) - limit))
        out.append("")
    out.append("（命中片段先由关键词规则粗筛，再交 LLM 逐条精判；"
               "时间轴来自录音转写，供你核对。）")
    return out


def format_html(hits: list[dict], limit: int = 8) -> str:
    """HTML block for the notification email (empty string when no hits)."""
    if not hits:
        return ""
    parts = ['<div class="callout"><h2>⚠ 本节点名 / 提问（可能叫到你）</h2>']
    for (course, sub, date), items in _groups(hits).items():
        head = escape(("%s %s" % (course, sub)).strip())
        if date:
            head += ' <span class="meta">(%s)</span>' % escape(date)
        parts.append("<p><strong>【%s】</strong></p><ul>" % head)
        for h in items[:limit]:
            parts.append(
                '<li><span class="tag">%s</span> '
                '<span class="meta">%s</span><br>「%s」</li>' % (
                    escape(kind_label(h.get("kind", ""))),
                    escape(h.get("time", "")),
                    escape(_clip(h.get("text", ""), 120)))
            )
        if len(items) > limit:
            parts.append('<li class="meta">… 还有 %d 处</li>'
                         % (len(items) - limit))
        parts.append("</ul>")
    parts.append('<div class="tip">命中片段先由关键词规则粗筛，再交 LLM 逐条精判；'
                 '时间轴来自录音转写，供你核对。</div></div>')
    return "".join(parts)