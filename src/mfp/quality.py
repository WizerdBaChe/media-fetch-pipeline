"""What can be DETERMINED about a transcript by looking at it.

This module exists because of a structural gap, not because of a bug. Until
now the product steered the recognition engine with hints -- a language, a
script instruction -- and then wrote whatever came back. Steering a
statistical model is not verification: `initial_prompt` asked for Traditional
Chinese for months and, as it turned out, also deleted every English word
from code-switched speech (D-106). The product could say what it had ASKED
for. It could not say what it had GOT.

So the pipeline gains a stage between the engine and the file:

    audio -> recognise -> [inspect] -> caption file
                              |
                              +-> findings, reported and never enforced

Three properties define this stage, and each of them is a decision:

**It is pure, and it takes cues rather than an engine.** No model, no audio,
no subprocess. That is what lets it be tested exhaustively at unit level,
which is the whole point -- the thing it guards against is a defect that
survived 1,900 green tests.

**It reports; it never vetoes and it never rewrites. No exceptions.** A
transcript with a suspect passage is still worth having, and a check that can
refuse work will eventually refuse work it should not have (P-38).

This module was drafted WITH a `normalise` step, on the argument that folding
U+FE50 SMALL COMMA to U+FF0C FULLWIDTH COMMA is a lossless rendering fix that
needs no ruling. Checking the claim killed it: Unicode decomposes U+FE50 to
U+002C, the ASCII comma, not to the fullwidth one -- the small forms are
variants of ASCII punctuation, not of the CJK punctuation a Chinese reader
wants. So the target is a CHOICE, the transform is a content decision, and it
belongs with the open ruling about NLLB writing Latin punctuation into
Traditional Chinese rather than being smuggled in as housekeeping.

**It applies to any transcript, whatever produced it.** Recognition,
a platform caption track, a file the user pointed at. The properties are of
the ARTIFACT, so the check belongs where transcripts enter the product rather
than inside the engine wrapper.

What this stage CANNOT do is see deletion in the text. Measured 2026-08-28:
the transcript that had lost all 26 of its English terms came back fluent,
correctly punctuated, with normal timings and a clean engine self-report.
Deletion is visible only in the TIME dimension -- 1.43 characters per second
against 3.11 for the same audio read properly -- and that gap is real but too
narrow to rule on, because a slow deliberate speaker produces the same
number. So density is REPORTED with its baseline beside it, and the thing
that actually catches deletion is `tests/conformance/`, which runs the real
engine against audio whose content is known. A runtime check cannot know what
was said; a fixture can.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

__all__ = [
    "Finding",
    "SIMPLIFIED_PAIRS",
    "density_of",
    "inspect_cues",
    "inspect_run",
    "latin_words",
]


@dataclass(frozen=True)
class Finding:
    """One determinable fact about a transcript that a reader should know.

    `code` is machine-stable and travels to agents and to the GUI. `detail`
    is a finished Traditional Chinese sentence, following the same rule
    `AsrReadiness` follows: the layer that KNOWS the fact writes the sentence,
    so two surfaces cannot describe the same situation differently.

    `evidence` carries the numbers the sentence is based on. A rate published
    without the evidence for it is a number nobody can check.
    """

    code: str
    #: `warn` -- a reader should look at this before using the transcript.
    #: `note` -- true, worth knowing, not a problem by itself.
    severity: str
    detail: str
    #: Seconds into the transcript, when the finding has a place.
    at: float | None = None
    evidence: dict = field(default_factory=dict)


#: Simplified characters and the Traditional forms they correspond to.
#:
#: Stored as PAIRS rather than as a set of characters, and that is the
#: instrument checking itself. An earlier detector counted 音, 理 and 容 as
#: Simplified -- they are identical in both scripts -- and reported drift in
#: text that had none (P-48). A pair whose halves are equal is a bug that
#: `test_every_pair_is_a_real_pair` fails on, which a bare set could not
#: express.
#:
#: Deliberately partial: the highest-frequency characters where the two
#: scripts genuinely differ. A missing pair costs a missed finding, which is
#: the safe direction for a check that only ever reports.
SIMPLIFIED_PAIRS: tuple[tuple[str, str], ...] = (
    ("这", "這"), ("么", "麼"), ("说", "說"), ("时", "時"), ("对", "對"),
    ("样", "樣"), ("们", "們"), ("没", "沒"), ("经", "經"), ("过", "過"),
    ("还", "還"), ("应", "應"), ("实", "實"), ("体", "體"), ("点", "點"),
    ("开", "開"), ("关", "關"), ("问", "問"), ("题", "題"), ("种", "種"),
    ("业", "業"), ("务", "務"), ("语", "語"), ("识", "識"), ("长", "長"),
    ("录", "錄"), ("处", "處"), ("断", "斷"), ("续", "續"), ("来", "來"),
    ("讲", "講"), ("内", "內"), ("让", "讓"), ("确", "確"), ("认", "認"),
    ("结", "結"), ("数", "數"), ("据", "據"), ("显", "顯"), ("现", "現"),
    ("给", "給"), ("读", "讀"), ("写", "寫"), ("检", "檢"), ("声", "聲"),
    ("当", "當"), ("机", "機"), ("进", "進"), ("标", "標"), ("准", "準"),
    ("态", "態"), ("势", "勢"), ("层", "層"), ("级", "級"), ("组", "組"),
    ("织", "織"), ("单", "單"), ("双", "雙"), ("击", "擊"), ("谈", "談"),
    ("试", "試"), ("档", "檔"), ("钟", "鐘"), ("听", "聽"), ("转", "轉"),
    ("换", "換"), ("计", "計"), ("间", "間"), ("记", "記"), ("号", "號"),
    ("简", "簡"), ("个", "個"), ("为", "為"), ("发", "發"), ("场", "場"),
    ("边", "邊"), ("环", "環"), ("资", "資"), ("价", "價"), ("决", "決"),
    ("学", "學"), ("电", "電"), ("车", "車"), ("门", "門"), ("马", "馬"),
    ("鸟", "鳥"), ("鱼", "魚"), ("龙", "龍"), ("图", "圖"), ("书", "書"),
    ("专", "專"), ("产", "產"), ("质", "質"), ("华", "華"), ("国", "國"),
    ("会", "會"), ("术", "術"), ("与", "與"), ("师", "師"), ("张", "張"),
    ("员", "員"), ("头", "頭"), ("买", "買"), ("卖", "賣"), ("远", "遠"),
    ("连", "連"), ("选", "選"),
)

#: Just the Simplified halves, for scanning.
_SIMPLIFIED = frozenset(simp for simp, _trad in SIMPLIFIED_PAIRS)

#: CJK Compatibility Forms that a transcript has no business containing.
#: Whisper emitted 62 of them into one 100-second transcript under an
#: `initial_prompt` -- U+FE50 SMALL COMMA where the text wants U+FF0C
#: FULLWIDTH COMMA -- and several players render them as boxes.
#:
#: Identified by Unicode's own decomposition rather than by a hand-written
#: list, which is what stops this drifting: `<small>` and `<vertical>` are
#: the two classes, and membership is a fact about the character rather than
#: an opinion of this file. Frozen at import so the scan stays cheap.
def _compatibility_forms() -> frozenset[str]:
    found = set()
    for block_start, block_end in ((0xFE10, 0xFE1F), (0xFE30, 0xFE6B)):
        for point in range(block_start, block_end + 1):
            char = chr(point)
            if unicodedata.decomposition(char).startswith(("<small>", "<vertical>")):
                found.add(char)
    return frozenset(found)


_PRESENTATION_FORMS = _compatibility_forms()

#: A run of this many identical consecutive cues is degeneration rather than
#: speech. Three, because two is a person repeating themselves for emphasis.
#: Mirrors `REPEAT_RUN_ALERT` in `asr/runner.py`; kept here as well because
#: this module must work on a transcript the engine never touched.
REPEAT_RUN_ALERT = 3

#: Characters-per-second floors, BY LANGUAGE, below which a transcript is
#: sparse enough to mention.
#:
#: Only Chinese is here, and the omission is the point. The floor is a
#: measured baseline -- 3.11 c/s for a correct reading of a recording whose
#: damaged twin gave 1.43 -- and a language with no measured baseline gets its
#: density REPORTED and never judged. Ruling without a baseline is how a check
#: ends up firing on every slow, deliberate speaker.
DENSITY_FLOOR = {"zh": 2.0}

#: Below this share of the audio covered by cues, something is missing wholly
#: rather than partly. Generous: real recordings have silence, music and
#: pauses, and VAD legitimately removes them.
COVERAGE_FLOOR = 0.5

#: How far below `DENSITY_FLOOR` a transcript has to fall before sparseness
#: stops being a remark and becomes something to act on.
#:
#: Calibrated from BOTH sides rather than picked (2026-08-28):
#:
#:   0.31 chars/s -- the UAT recording where the VAD deleted 89% of the
#:                   audio. Catastrophic, and the old fixed `note` described
#:                   it as "maybe the speaker is slow".
#:   1.43 chars/s -- the D-106 recording read with every English term
#:                   silently removed. A real deletion, and deliberately
#:                   still a `note`: the honest-slow-speaker case sits right
#:                   beside it and the gap is narrow (see `_density`).
#:   3.11 chars/s -- the same recording read correctly. No finding.
#:
#: Half the floor (1.0) is the only line that puts 0.31 on one side and 1.43
#: on the other, which is what keeps this from re-judging a case the project
#: already ruled on.
DENSITY_ALARM_SHARE = 0.5

#: Below this share of the audio surviving voice-activity filtering, say so.
#: Not a verdict on its own and it must not become one: 3% is correct for a
#: half-hour of ambience with one minute of speech, and 11% was catastrophic
#: for a two-person conversation. The share cannot tell those apart -- what
#: it does is explain a sparse transcript that another check has already
#: flagged, which is why this is a `note` and `sparse-text` is the alarm.
VAD_KEPT_FLOOR = 0.5

#: Share of segments that needed a hotter retry before the run stops being a
#: difficult recording and becomes a failed decode.
#:
#: Two-sided again: 0/158 on the same audio read correctly, 29/152 (19%) on a
#: noisy-but-usable read, and 6/6 (100%) on the read where the content was
#: gone. Half separates the last from the other two.
FALLBACK_SHARE_ALARM = 0.5

#: Below this many segments, a share means nothing -- one retry out of one
#: segment is 100% and says nothing at all.
FALLBACK_MIN_SEGMENTS = 4

#: Below this share of a stretch's own windows agreeing with the language it
#: was decoded in, say which minutes those are. Mirrors
#: `CONTESTED_AGREEMENT` in `asr/runner.py` -- kept here as well because this
#: module must be able to rule on a plan it did not produce.
CONTESTED_AGREEMENT = 0.8

#: A run of this many consecutive cues that are nothing but the engine's own
#: instruction is the instruction having REPLACED the speech, rather than
#: having leaked into it once. Measured: the passage this was found on ran
#: eleven cues and 99 characters where the same audio without the instruction
#: carries 512 (spike-07 6). Three, matching `REPEAT_RUN_ALERT`, because the
#: two describe the same kind of event.
INSTRUCTION_CAPTURE_RUN = 3


def density_of(cues: list[dict]) -> tuple[float, float]:
    """`(chars_per_cue_second, cue_seconds)`.

    Measured against the time the CUES claim, not against the file's length:
    a recording that is half silence would otherwise read as sparse when it
    is merely quiet.
    """
    spoken = sum(
        max(0.0, float(c.get("end", 0)) - float(c.get("start", 0))) for c in cues
    )
    chars = sum(len(str(c.get("text", "")).strip()) for c in cues)
    return (chars / spoken if spoken > 0 else 0.0), spoken


def _instruction_clauses(instruction: str | None) -> list[str]:
    """The pieces of a standing instruction that could show up as a line.

    Split, because the leak was PARTIAL: the engine was told
    「繁體中文，英文詞彙保留英文。」 and wrote 「英文詞彙保留英文。」 into the
    transcript three times. Matching the whole string would have found
    nothing.

    Short pieces are dropped. A two-character clause would match ordinary
    speech and turn this into a check that fires on innocent transcripts,
    which is worse than not having it.
    """
    if not instruction:
        return []
    parts = re.split(r"[，,。．.、;；\s]+", instruction)
    return [p for p in (part.strip() for part in parts) if len(p) >= 4]


def _prompt_echo(cues: list[dict], *, instruction: str | None) -> list[Finding]:
    """The engine's own instruction, written out as if somebody had said it.

    `hotwords` is re-inserted into every window's prompt, so the model can
    and does emit it as content -- and the result is grammatical, correctly
    punctuated, and indistinguishable from speech to every other check here.
    Observed in a delivered `.srt` (UAT 2026-08-28): three consecutive cues
    reading 「英文詞彙保留英文。」 spanning four minutes of a design
    discussion.

    Ruled on the instruction the engine ACTUALLY received, which the runner
    now reports. Without a claim there is nothing to check against, exactly
    as with `expect_script`.

    Matched on a shared RUN of characters rather than on a whole clause,
    because the leak is not always verbatim. Real audio produced
    `中文詞彙保留英文。` -- a corrupted form of 「繁體中文，英文詞彙保留英文。」
    that contains NEITHER clause as a substring and sailed straight past the
    clause match (spike-07 6). A check defeated by the corruption of its own
    target is not a check.

    A long enough RUN of these cues is a different event and gets its own
    code. One echo is the instruction leaking into the transcript; eleven in
    a row is the instruction having replaced five minutes of a meeting, and
    the remedy is not the same -- so the finding does not pretend they are.
    """
    clauses = _instruction_clauses(instruction)
    if not clauses or not instruction:
        return []
    flags = [_is_instruction_echo(str(cue.get("text", "")), instruction, clauses)
             for cue in cues]
    hits = [cue for cue, flag in zip(cues, flags) if flag]
    if not hits:
        return []

    longest = 0
    run = 0
    run_at = 0.0
    at_longest = 0.0
    for cue, flag in zip(cues, flags):
        if flag:
            if run == 0:
                run_at = float(cue.get("start", 0.0))
            run += 1
            if run > longest:
                longest, at_longest = run, run_at
        else:
            run = 0

    if longest >= INSTRUCTION_CAPTURE_RUN:
        return [Finding(
            code="instruction-capture",
            severity="warn",
            detail=(
                f"從 {_clock(at_longest)} 起有連續 {longest} 句寫的是引擎自己的"
                f"設定指令，不是錄音的內容——那段話等於沒有被寫下來。"
                f"這是引擎在難解的段落把指令當成前文接下去寫，"
                f"通常可以把那一段拿掉指令重跑救回來。"
            ),
            at=at_longest,
            evidence={"run": longest, "count": len(hits),
                      "instruction": instruction, "clauses": clauses},
        )]
    return [Finding(
        code="prompt-echo",
        severity="warn",
        detail=(
            f"有 {len(hits)} 句字幕寫的是引擎自己的設定指令"
            f"（「{str(hits[0].get('text', '')).strip()[:14]}」），"
            f"不是錄音的內容。這通常表示那一段音訊引擎聽不出東西來。"
        ),
        at=float(hits[0].get("start", 0.0)),
        evidence={"count": len(hits), "instruction": instruction,
                  "clauses": clauses},
    )]


def _is_instruction_echo(text: str, instruction: str, clauses: list[str],
                         *, min_run: int = 6, max_chars: int = 24) -> bool:
    """Is this ONE cue the instruction rather than speech?

    Two ways in, and both are needed. A whole clause anywhere is the original
    leak. A shared run of `min_run` characters inside a cue short enough to
    be nothing but the instruction is the corrupted form -- which is what
    real audio actually produced.

    The length bound is load-bearing in the second case: a real sentence
    discussing 「英文詞彙保留英文」 is longer than an instruction cue, and a
    cue shorter than the run cannot carry evidence of one (「英文」 is a word
    people say).
    """
    if any(clause in text for clause in clauses):
        return True
    body = text.strip().strip("。.,，、！!？? ")
    if len(body) < min_run or len(body) > max_chars:
        return False
    return any(
        body[index:index + min_run] in instruction
        for index in range(0, len(body) - min_run + 1)
    )


def _language_drift(cues: list[dict], *, plan: list[dict] | None) -> list[Finding]:
    """A passage the engine says is Chinese, whose text has no Chinese in it.

    This is the check that would have caught the failure this whole plan
    exists for, from the artifact alone. The delivered transcript of a
    51-minute bilingual meeting carried 1255 cues and **zero** CJK characters
    while 72 of its 103 windows were Mandarin -- and every check in this
    module passed it, because each one asks about a property of the text and
    none of them had anything to compare the text WITH.

    Only Chinese is ruled on, and the asymmetry is deliberate rather than an
    omission. `zh` has a writing system a check can see; `en` and `ja` and
    `ko` do not separate that cleanly from each other or from a transcript
    full of technical terms, and a gate may only rule on what it can
    determine. Chinese is where the determination exists, so Chinese is
    where the ruling is.

    The reverse case -- an English stretch full of Chinese -- is caught by
    the same comparison from the other side and is reported the same way.
    """
    if not plan:
        return []
    findings: list[Finding] = []
    for stretch in plan:
        code = str(stretch.get("language") or "").split("-")[0]
        inside = _cues_in(cues, stretch)
        if not inside:
            continue
        text = "".join(str(cue.get("text", "")) for cue in inside)
        if not text.strip():
            continue
        has_cjk = any("一" <= char <= "鿿" for char in text)
        at = float(inside[0].get("start", 0.0))
        if code == "zh" and not has_cjk:
            findings.append(Finding(
                code="language-drift",
                severity="warn",
                detail=(
                    f"{_clock(stretch.get('start', 0.0))} 到 "
                    f"{_clock(stretch.get('end', 0.0))} 這段聽起來是中文，"
                    f"但寫出來的字裡面一個中文都沒有。這通常表示這段被用"
                    f"錯誤的語言辨識了，內容是編出來的而不是聽出來的。"
                ),
                at=at,
                evidence={"language": code, "cues": len(inside),
                          "start": float(stretch.get("start", 0.0)),
                          "end": float(stretch.get("end", 0.0))},
            ))
        elif code and code != "zh" and has_cjk:
            cjk = sum(1 for char in text if "一" <= char <= "鿿")
            if cjk / len(text) > 0.3:
                findings.append(Finding(
                    code="language-drift",
                    severity="warn",
                    detail=(
                        f"{_clock(stretch.get('start', 0.0))} 到 "
                        f"{_clock(stretch.get('end', 0.0))} 這段標的是 "
                        f"{code}，但寫出來的內容大部分是中文。"
                    ),
                    at=at,
                    evidence={"language": code, "cues": len(inside),
                              "cjkShare": round(cjk / len(text), 3)},
                ))
    return findings


def _contested(plan: list[dict] | None) -> list[Finding]:
    """Which minutes the language map itself was unsure about.

    A `note`, never a warning, and it must stay one: a bilingual passage is
    not a defect, it is a recording of two people who switch language faster
    than a 30-second window can follow. What is worth saying is WHICH
    minutes, so a reader knows where to check rather than distrusting the
    whole file.
    """
    if not plan:
        return []
    contested = [
        stretch for stretch in plan
        if stretch.get("agreement") is not None
        and float(stretch["agreement"]) < CONTESTED_AGREEMENT
    ]
    if not contested:
        return []
    spans = "、".join(
        f"{_clock(s.get('start', 0.0))}–{_clock(s.get('end', 0.0))}"
        for s in contested[:3]
    )
    return [Finding(
        code="contested-language",
        severity="note",
        detail=(
            f"有 {len(contested)} 段（{spans}）中英文交替得比較快，"
            f"辨識時只能整段挑一種語言。這幾段的可靠度比其他地方低。"
        ),
        at=float(contested[0].get("start", 0.0)),
        evidence={"stretches": [
            {"language": s.get("language"), "start": s.get("start"),
             "end": s.get("end"), "agreement": s.get("agreement")}
            for s in contested
        ]},
    )]


def inspect_run(health: dict | None, *, segments: int = 0) -> list[Finding]:
    """What the ENGINE's account of itself says, as findings.

    Separate from `inspect_cues` because the two answer different questions
    and one of them cannot be answered from the text at all. Deletion by
    voice-activity filtering leaves no trace in the transcript -- the words
    are simply not there, and what remains is fluent. The only witness is the
    engine's own report of how much audio it was handed.

    Findings rather than a second vocabulary: the GUI, the CLI and the agent
    surface already know how to render a `Finding`, and a parallel channel
    for "engine testimony" would be a second way to say the same thing.
    """
    if not health:
        return []
    findings: list[Finding] = []

    kept = health.get("vadSeconds")
    total = health.get("audioSeconds")
    if kept is not None and total:
        share = float(kept) / float(total)
        if share < VAD_KEPT_FLOOR:
            findings.append(Finding(
                code="vad-dropped-most",
                severity="note",
                detail=(
                    f"引擎只聽了這段音訊的 {share:.0%}"
                    f"（{float(kept):.0f} 秒 / {float(total):.0f} 秒），"
                    f"其餘被判定為非人聲而略過。"
                    f"如果錄音大部分是靜音或音樂，這是正常的；"
                    f"如果有人講話而且聲音偏小，被略過的就是那個人。"
                ),
                evidence={"vadSeconds": round(float(kept), 2),
                          "audioSeconds": round(float(total), 2),
                          "share": round(share, 3)},
            ))

    fallbacks = int(health.get("temperatureFallbacks") or 0)
    counted = int(health.get("segments") or segments or 0)
    if counted >= FALLBACK_MIN_SEGMENTS and fallbacks:
        share = fallbacks / counted
        if share >= FALLBACK_SHARE_ALARM:
            findings.append(Finding(
                code="most-windows-fell-back",
                severity="warn",
                detail=(
                    f"{counted} 個段落裡有 {fallbacks} 個是第一次解碼失敗、"
                    f"提高隨機度重試才出來的（{share:.0%}）。"
                    f"這種比例下的稿子每跑一次都會不一樣，"
                    f"不能當成這段錄音的內容。"
                ),
                evidence={"fallbacks": fallbacks, "segments": counted,
                          "share": round(share, 3)},
            ))
    return findings


def inspect_cues(
    cues: list[dict],
    *,
    duration: float | None = None,
    language: str | None = None,
    expect_script: str | None = None,
    instruction: str | None = None,
    plan: list[dict] | None = None,
) -> list[Finding]:
    """Everything determinable about this transcript, worst first.

    `duration` is the length of the audio, when known; without it the
    coverage check is skipped rather than guessed at. `expect_script` is the
    CLAIM being checked -- `trad` means the caller asked for Traditional, so
    Simplified output is a finding. Without a claim there is nothing to
    check against, and the script check does not run.

    `plan` is the engine's own account of which language it decoded each
    passage in. It is a CLAIM like the others, and having it is what lets
    this module rule on the failure it could not previously see: a stretch
    the engine says is Chinese whose cues contain no Chinese. Without a plan
    every check falls back to the single-language behaviour every earlier
    build had, so a transcript from any other source still gets inspected.
    """
    findings: list[Finding] = []
    if not cues:
        return findings

    findings += _script(cues, language=language, expect_script=expect_script,
                        plan=plan)
    findings += _language_drift(cues, plan=plan)
    findings += _contested(plan)
    findings += _prompt_echo(cues, instruction=instruction)
    findings += _punctuation(cues)
    findings += _repetition(cues)
    findings += _timeline(cues, duration=duration)
    findings += _density(cues, language=language, plan=plan)

    order = {"warn": 0, "note": 1}
    return sorted(findings, key=lambda f: (order.get(f.severity, 9), f.at or 0.0))


def _plan_languages(plan: list[dict] | None) -> set[str]:
    return {str(s.get("language") or "").split("-")[0] for s in (plan or [])}


def _cues_in(cues: list[dict], stretch: dict) -> list[dict]:
    """The cues belonging to one stretch, by the same midpoint rule the
    runner used to place them there."""
    start = float(stretch.get("start", 0.0))
    end = float(stretch.get("end", 0.0))
    inside = []
    for cue in cues:
        middle = (float(cue.get("start", 0.0)) + float(cue.get("end", 0.0))) / 2
        if start <= middle < end:
            inside.append(cue)
    return inside


def _script(
    cues: list[dict], *, language: str | None, expect_script: str | None,
    plan: list[dict] | None = None,
) -> list[Finding]:
    """Did the Traditional-Chinese request actually take?

    This is the check the whole module was built around. The request is a
    prompt, a prompt is a hint, and until now nothing anywhere asked whether
    the hint had worked -- which is why "the instruction survives a context
    reset" had to be measured by hand rather than being a property the
    product could report about its own output.

    A multi-language transcript is named `mul`, which is not `zh` -- so
    without the plan this check would silently stop running on exactly the
    files that gained the most new Chinese. It reads the plan when there is
    one, and only over the passages the engine says are Chinese: Simplified
    characters cannot appear in an English passage, and looking for them
    there would only find false positives.
    """
    if expect_script != "trad":
        return []
    if plan:
        if "zh" not in _plan_languages(plan):
            return []
        cues = [
            cue
            for stretch in plan
            if str(stretch.get("language") or "").split("-")[0] == "zh"
            for cue in _cues_in(cues, stretch)
        ]
    elif (language or "").split("-")[0] != "zh":
        return []
    hits: dict[str, int] = {}
    first_at: float | None = None
    for cue in cues:
        for char in str(cue.get("text", "")):
            if char in _SIMPLIFIED:
                hits[char] = hits.get(char, 0) + 1
                if first_at is None:
                    first_at = float(cue.get("start", 0.0))
    if not hits:
        return []
    total = sum(hits.values())
    sample = "".join(sorted(hits, key=lambda c: -hits[c])[:6])
    return [Finding(
        code="simplified-script",
        severity="warn",
        detail=(
            f"這份稿子要求繁體中文，但裡面有 {total} 個簡體字（例如 {sample}）。"
            f"通常是某一段特別難辨識，引擎在那裡把設定丟掉了。"
        ),
        at=first_at,
        evidence={"count": total, "distinct": len(hits), "sample": sample},
    )]


def _punctuation(cues: list[dict]) -> list[Finding]:
    """Compatibility-form punctuation the transcript should not contain.

    Reported and not repaired. Replacing ﹐ with ，looks like housekeeping and
    is not: Unicode decomposes ﹐ to the ASCII comma, so choosing the
    fullwidth one is a judgement about how Chinese text should be punctuated,
    which is the same open question as NLLB writing `.` into Traditional
    Chinese output. One ruling should settle both.
    """
    count = 0
    found: set[str] = set()
    for cue in cues:
        for char in str(cue.get("text", "")):
            if char in _PRESENTATION_FORMS:
                count += 1
                found.add(char)
    if not count:
        return []
    sample = "".join(sorted(found)[:5])
    return [Finding(
        code="presentation-punctuation",
        severity="note",
        detail=(
            f"有 {count} 個標點是「相容形式」的變體字（{sample}），"
            f"有些播放器會顯示成方框。目前不會自動改，因為換成哪一種標點"
            f"本身是個選擇。"
        ),
        evidence={"count": count, "sample": sample},
    )]


def _repetition(cues: list[dict]) -> list[Finding]:
    """The same line over and over: Whisper's long-audio degeneration.

    Re-derived from the TEXT here even though `asr/runner.py` already counts
    it, because this module must work on a transcript the engine never
    produced -- and because a check that can only run on our own output
    cannot be used to check somebody else's.
    """
    run = 0
    longest = 0
    text_of = ""
    at: float | None = None
    previous: str | None = None
    start = 0.0
    for cue in cues:
        text = str(cue.get("text", "")).strip()
        if not text:
            continue
        if text == previous:
            run += 1
        else:
            run, start = 1, float(cue.get("start", 0.0))
        if run > longest:
            longest, text_of, at = run, text, start
        previous = text
    if longest < REPEAT_RUN_ALERT:
        return []
    return [Finding(
        code="repeated-line",
        severity="warn",
        detail=(
            f"有一段連續出現 {longest} 行一模一樣的內容"
            f"「{text_of[:20]}」，這通常是引擎卡住而不是講者真的重複。"
        ),
        at=at,
        evidence={"run": longest, "text": text_of},
    )]


def _timeline(cues: list[dict], *, duration: float | None) -> list[Finding]:
    """Cues that do not describe a sane stretch of time.

    Two things, and both used to pass silently. `to_srt` repairs an end that
    precedes its start by moving the end -- correct output, no record that
    the engine produced nonsense. And a transcript that stops halfway through
    a two-hour file is a complete, parseable, plausible SRT.
    """
    findings: list[Finding] = []

    backwards = [
        c for c in cues
        if float(c.get("end", 0)) < float(c.get("start", 0))
    ]
    if backwards:
        findings.append(Finding(
            code="backwards-cue",
            severity="note",
            detail=(
                f"有 {len(backwards)} 句字幕的結束時間早於開始時間，"
                f"寫檔時已經自動補正。"
            ),
            at=float(backwards[0].get("start", 0.0)),
            evidence={"count": len(backwards)},
        ))

    if duration and duration > 0:
        last_end = max(float(c.get("end", 0)) for c in cues)
        covered = 0.0
        cursor = 0.0
        for cue in sorted(cues, key=lambda c: float(c.get("start", 0))):
            start = max(float(cue.get("start", 0)), cursor)
            end = float(cue.get("end", 0))
            covered += max(0.0, end - start)
            cursor = max(cursor, end)
        share = covered / duration
        tail = duration - last_end
        # A transcript that stops early is the failure worth naming, and it
        # is separable from a recording that is simply quiet in the middle.
        if tail > max(30.0, duration * 0.1):
            findings.append(Finding(
                code="stops-early",
                severity="warn",
                detail=(
                    f"稿子在 {_clock(last_end)} 就結束了，但音訊有 "
                    f"{_clock(duration)}，最後 {_clock(tail)} 沒有任何內容。"
                ),
                at=last_end,
                evidence={"lastEnd": round(last_end, 2),
                          "duration": round(duration, 2),
                          "tail": round(tail, 2)},
            ))
        elif share < COVERAGE_FLOOR:
            findings.append(Finding(
                code="thin-coverage",
                severity="note",
                detail=(
                    f"字幕只涵蓋了音訊的 {share:.0%}。"
                    f"如果這段錄音大部分是靜音或音樂，這是正常的。"
                ),
                evidence={"covered": round(covered, 2),
                          "duration": round(duration, 2),
                          "share": round(share, 3)},
            ))
    return findings


def _density(cues: list[dict], *, language: str | None,
             plan: list[dict] | None = None) -> list[Finding]:
    """Text per second of speech -- the only dimension deletion shows up in.

    Reported with the floor beside it, because the number means nothing
    alone. Measured 2026-08-28 on one recording read two ways: 3.11
    characters per second with the English intact, 1.43 with all 26 English
    terms silently removed. The gap is real and it is narrow, which is
    exactly why this is a `note` and not a verdict -- a slow, deliberate
    speaker produces the low number honestly.

    A language with no measured floor is not judged at all.

    On a plan, the rate is measured over the CHINESE stretches alone.
    Averaging a Chinese floor across English passages compares two things
    with different natural densities and produces a number that is nobody's:
    English at 15 characters per second would hide a Chinese passage that had
    been emptied.
    """
    if plan and "zh" in _plan_languages(plan):
        code = "zh"
        cues = [
            cue
            for stretch in plan
            if str(stretch.get("language") or "").split("-")[0] == "zh"
            for cue in _cues_in(cues, stretch)
        ]
    else:
        code = (language or "").split("-")[0]
    floor = DENSITY_FLOOR.get(code)
    if floor is None or not cues:
        return []
    rate, spoken = density_of(cues)
    if spoken <= 0 or rate >= floor:
        return []
    # The severity is the DISTANCE, not the fact of being under. A fixed
    # `note` described 0.31 against a floor of 2.0 -- six and a half times
    # under, on a transcript that had lost 89% of its content -- with the
    # words 「如果講話速度本來就慢，這是正常的」 (UAT 2026-08-28). The
    # measurement was right and the sentence was wrong about it.
    #
    # `DENSITY_ALARM_SHARE` is where that stops: see its definition for the
    # two-sided calibration, and note that the D-106 case at 1.43 stays a
    # `note` on purpose. This raises an alarm the old code could not raise;
    # it does not re-judge a case the project already ruled on.
    alarming = rate < floor * DENSITY_ALARM_SHARE
    return [Finding(
        code="sparse-text",
        severity="warn" if alarming else "note",
        detail=(
            f"平均每秒只有 {rate:.1f} 個字，"
            f"不到一般中文（約 {floor:.0f} 個以上）的 "
            f"{rate / floor:.0%}。這種密度不像是講話慢，"
            f"比較像是有整段內容沒有被寫出來。"
            if alarming else
            f"平均每秒只有 {rate:.1f} 個字（一般中文約 {floor:.0f} 個以上）。"
            f"如果講話速度本來就慢，這是正常的；如果不是，可能有內容沒有被寫出來。"
        ),
        evidence={"charsPerSecond": round(rate, 2), "floor": floor,
                  "spokenSeconds": round(spoken, 1),
                  "shareOfFloor": round(rate / floor, 3)},
    )]


def _clock(seconds: float) -> str:
    total = int(round(max(0.0, seconds)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _is_small_form(char: str) -> bool:
    """Used only by the test that keeps `_PRESENTATION_FORMS` honest."""
    return unicodedata.decomposition(char).startswith("<small>")


_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9'\-]*")


def latin_words(cues: list[dict]) -> list[str]:
    """Every run of Latin letters in the transcript.

    Not a check -- a measurement, used by the conformance runner to assert
    that code-switched English survived. It lives here rather than in the
    test so that the definition of "an English word in a Chinese transcript"
    has one home.
    """
    return [
        word
        for cue in cues
        for word in _LATIN_RUN.findall(str(cue.get("text", "")))
    ]
