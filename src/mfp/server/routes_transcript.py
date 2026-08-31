"""HTTP surface for `mfp transcript`.

Deliberately NOT a job. `stack` is minutes of ffmpeg and needs states, a
progress line and a stop button; a transcript is one metadata read and one
small download, and wrapping it in the job machinery would give the GUI a
second vocabulary to learn for work that is over before a spinner finishes
its first turn.

It is, however, blocking work that reaches the network, so the handlers are
plain `def` rather than `async def`: FastAPI runs a sync endpoint in a
threadpool, and a ten-second caption fetch on the event loop would stall the
SSE stream every other part of the GUI is listening to. (The neighbours in
`routes_stack` are `async def` because ffprobe and one frame grab are
sub-second; this one is not.)
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from mfp import transcript as tx
from mfp.errors import UsageError
from mfp.models import CamelModel


class TranscriptRequest(CamelModel):
    """One read's parameters, in the CLI's own vocabulary.

    `target` is whatever `mfp transcript` accepts -- a post URL, a video whose
    captions were saved beside it, or a caption file. The GUI passes a queue
    row's downloaded video and gets the sidecar for free; it passes a pasted
    URL and gets a fetch. Neither case needs the GUI to know which happened,
    which is why one field takes all three.
    """

    target: str
    sub_lang: str = "orig"
    start: str | None = None
    end: str | None = None
    refresh: bool = False
    #: `auto` | `always` | `never` -- see `transcript.RECOGNIZE_MODES`. The
    #: default is what makes a dropped-in mp3 work without the GUI having to
    #: know that an mp3 is different from a YouTube link.
    recognize: str = "auto"
    #: Spoken language for recognition. `auto` detects it.
    asr_language: str = "auto"
    asr_languages: str | None = None


class TranscriptLine(CamelModel):
    at: float
    text: str


class TranscriptResponse(CamelModel):
    #: The caption file this was read out of. The workspace hands it straight
    #: to `stack --subs` when the reader picks lines to quote, so the quote
    #: run costs no further platform request.
    source: str
    kind: str
    language: str | None = None
    title: str | None = None
    line_count: int
    lines: list[TranscriptLine]


class TracksRequest(CamelModel):
    url: str


class TracksResponse(CamelModel):
    title: str | None = None
    spoken_language: str | None = None
    written: list[str]
    #: Named separately from the rest because the rest is a hundred machine
    #: translations and this is the one that is actually the video.
    automatic_original: list[str]
    automatic_count: int


#: The SSE event name recognition progress travels under. Its own name
#: rather than `task`: a task is a queue row with an id, and this is a
#: request in flight that no row exists for.
ASR_EVENT = "asr"

#: Translation progress, on its own name. Not folded into `asr`: the two can
#: never be in flight for the same request, but they count DIFFERENT things
#: -- seconds of audio against lines of text -- and one reader trying to
#: interpret both would have to guess which unit had arrived.
MT_EVENT = "mt"


class TranslateRequest(CamelModel):
    """One translation, over a caption file that already exists.

    `source` is a caption file and never a media file. That is the ruling
    this endpoint is built on (2026-08-28): translating is a second thing a
    person asks for once they have a transcript in front of them, so there
    is deliberately no path from a URL or an mp3 to here. Anyone wanting
    both calls `/transcript` and then this, which also means the recognised
    text is on disk and readable before anything is built on top of it.
    """

    source: str
    #: ISO (`en`) or FLORES-200 (`eng_Latn`). Required: what to translate
    #: INTO is the one thing the caller opened this feature to say.
    target: str
    #: Source language. Read from the caption filename when this project
    #: wrote it, so the GUI does not have to ask twice.
    source_language: str | None = None


class TranslateResponse(CamelModel):
    source: str
    source_language: str
    target_language: str
    line_count: int
    engine: dict
    #: Which lines the engine appears to have cut short (1-based, as the GUI
    #: shows them). Empty is the normal case.
    #:
    #: Here because it was NOT here, and the audit found it: `translate_file`
    #: has computed this since the clause-splitting fix, the CLI prints it,
    #: and this route dropped it -- so the one reader who cannot see stderr
    #: was the one reader never told their translation might be incomplete.
    suspect_lines: list[int] = []


class TranslateDocRequest(CamelModel):
    """One document translated, in the CLI verb's own vocabulary.

    A DOCUMENT, not a transcript: `.txt`, `.md`, `.markdown`. It is its own
    verb rather than a flag on `/translate` for the reason D-131 gives --
    `translate` reads one line as one utterance, which is right for captions
    and wrong for prose three ways at once.
    """

    source: str
    target: str
    #: REQUIRED here and optional on `/translate`. A transcript this project
    #: wrote carries its language in its filename; a document carries
    #: nothing, and guessing produces fluent output that is not a
    #: translation of anything. The refusal comes from `translate_document`,
    #: which is where the sentence explaining it already lives.
    source_language: str | None = None


class TranslateDocResponse(CamelModel):
    source: str
    source_language: str
    target_language: str
    line_count: int
    engine: dict
    suspect_lines: list[int] = []
    #: The record written beside the document (D-135). Carried because the
    #: panel offers it: a translated document and 「這份是從哪來的」 are two
    #: files, and only one of them is worth reading twice.
    record: str | None = None
    #: What the structure claim is checkable against after the fact. A run
    #: reporting 0 verbatim blocks for a document full of code did not parse
    #: it as Markdown, and that is visible here rather than only on stderr.
    blocks: int = 0
    verbatim_blocks: int = 0
    sentences: int = 0


class CorrectRequest(CamelModel):
    """What COULD be corrected in a transcript that already exists.

    Reading only. There is no `apply` flag here and that is deliberate: the
    endpoint that computes proposals and the one that writes files are
    separate calls, because the user has to see the list in between. D-115
    built the version that decided and wrote in one step.
    """

    source: str
    #: Offer nothing but spellings already enrolled -- no phonetic guessing.
    exact_only: bool = False


class ProposalOut(CamelModel):
    cue: int
    start: int
    end: int
    was: str
    now: str
    term: str
    #: `exact` — a spelling the user enrolled. `phonetic` — this merely SOUNDS
    #: like a declared term, and the engine was unsure where it appears.
    tier: str
    #: The reading that matched, for a phonetic hit. Shown rather than hidden:
    #: "機板 and 基板 are both ji ban" is the entire argument for the
    #: substitution, and a reader who cannot see it cannot judge it.
    key: str = ""
    #: The engine's own probability over the characters being replaced, or
    #: null where the transcript was recognised before that was reported.
    confidence: float | None = None


class CorrectResponse(CamelModel):
    source: str
    #: The transcript as it stands, so the caller can render the before side
    #: without reading the file a second time.
    cues: list[dict]
    proposals: list[ProposalOut]
    #: The same thing as prose, ready to show. Built here rather than in the
    #: renderer so the CLI and the GUI cannot drift into two descriptions.
    diff: str
    #: False when `pypinyin` is absent: exact matches still work, phonetic
    #: ones silently cannot, and a UI that does not say so looks broken.
    phonetic_keys: bool
    glossary_entries: int


class ApplyRequest(CamelModel):
    source: str
    exact_only: bool = False
    #: Indices into the proposal list, as returned. Omitted means all of them.
    accepted: list[int] | None = None


class ApplyResponse(CamelModel):
    #: The file that was NOT written to. Named in the response because "your
    #: original is intact" is the reassurance this whole flow is built on.
    original: str
    written: dict[str, str]
    applied: int


class TidyRequest(CamelModel):
    """What a reading copy WOULD leave out. Reading only.

    Two calls rather than one, for the same reason `correct` is two: the
    user is between them. It matters more here, because this deletes.
    """

    source: str


class RemovalOut(CamelModel):
    #: 0-based into the cue list, as `tidy.Removal` carries it. The GUI adds
    #: one when it shows 「第 N 句」; the wire keeps the index.
    cue: int
    text: str
    start: float
    end: float


class TidyProposeResponse(CamelModel):
    source: str
    #: The transcript as it stands, so the caller can show what survives
    #: without reading the file again -- same reason `CorrectResponse`
    #: carries them.
    cues: list[dict]
    removals: list[RemovalOut]
    #: `{cues, removed, kept, removedChars, totalChars, share}`, computed in
    #: `tidy.summary` so the CLI, this route and the GUI cannot disagree.
    summary: dict
    #: How many terms are on the list. Zero means no removal is possible,
    #: and a UI that does not say so looks broken rather than empty.
    filler_terms: int


class TidyApplyRequest(CamelModel):
    source: str
    #: Indices into the removal list, as returned. Omitted means all of them.
    accepted: list[int] | None = None


class TidyApplyResponse(CamelModel):
    #: The file that was NOT written to, named for the same reason
    #: `ApplyResponse` names it: "your original is intact" is what this whole
    #: flow rests on.
    original: str
    written: dict[str, str]
    removed: int


class FillerResponse(CamelModel):
    path: str
    terms: list[str]


class FillerEditRequest(CamelModel):
    #: Terms to add or drop. Empty with `common` set is the "give me the
    #: usual ones" call.
    terms: list[str] = []
    #: Add the offered common list too. An explicit flag rather than a
    #: default, because nothing may be removed by a term the user did not
    #: put on their own list (D-116's rule).
    common: bool = False


class GlossaryEntryOut(CamelModel):
    term: str
    aliases: list[str] = []
    note: str = ""


class GlossaryResponse(CamelModel):
    path: str
    entries: list[GlossaryEntryOut]
    phonetic_keys: bool


class SaveTermRequest(CamelModel):
    """One entry, whole, as the glossary editor holds it.

    `original` is which entry is being replaced -- the term as it was BEFORE
    this edit, so the correct spelling itself can be corrected. Empty means
    this is a new entry.
    """

    original: str = ""
    term: str
    aliases: list[str] = []
    note: str = ""


class RemoveTermRequest(CamelModel):
    term: str


class EnrolRequest(CamelModel):
    """A term the user declares, and optionally the wrong form they just saw.

    The alias is the whole point. A correction made by hand once becomes an
    exact match next time, which is the only way this feature gets better.
    """

    term: str
    alias: str = ""


def _progress_publisher(request: Request, event: str = ASR_EVENT):
    """Forward the engine's progress records onto the event stream.

    This is the one thing that keeps the module docstring above honest.
    Reading a caption track is a second's work, so this endpoint was
    deliberately not a job -- but LISTENING to an hour of audio is minutes,
    and a synchronous request that says nothing for minutes is
    indistinguishable from a hung server. The events are the compromise:
    the client learns how far along it is without either side having to
    learn the job vocabulary.

    Coalesced on a fixed key, which reuses the broadcaster's 4 Hz throttle
    -- a segment can arrive every 30 ms and every one of them would
    otherwise be a frame on the wire.

    `dispatch` is what makes this safe. FastAPI runs a `def` endpoint in a
    threadpool, and the broadcaster's queues belong to the event loop;
    calling `publish` straight from here would touch an `asyncio.Queue`
    from the wrong thread. `app.state.dispatch` is the same
    `call_soon_threadsafe` the worker and the stack runner are handed, and
    its absence (a test app built without a lifespan) degrades to a direct
    call rather than to no progress at all.
    """
    broadcaster = getattr(request.app.state, "broadcaster", None)
    if broadcaster is None:
        return None
    dispatch = getattr(request.app.state, "dispatch", None) or (lambda fn: fn())

    def publish(record: dict) -> None:
        dispatch(lambda: broadcaster.publish(event, record, coalesce_key=event))

    return publish


def _translation_engine(config) -> tuple:
    """`(runtime, model)`, or the refusal that says which half is missing.

    Shared by both translate routes for the reason `cli._translation_runtime`
    is shared by both translate verbs: these two sentences are the only place
    a user learns that translation needs a SECOND model, and two copies of
    them is two chances to fix one of them.

    Both refusals are `TranslationUnavailable` and both are exit-6 shaped,
    but they are DIFFERENT sentences -- no engine is the setup step
    recognition also needs, and no translation model is a second, separate
    one that nothing else in this product requires. Collapsing them would
    send somebody to reinstall an engine they already have.
    """
    from mfp import asr, translate as mt
    from mfp.asr_models import readiness, resolve_translation_model

    runtime = asr.find_runtime(config.asr.python)
    if runtime is None:
        entry = readiness(config.asr, config.output_root).capability("translation")
        raise mt.TranslationUnavailable(entry.detail if entry else "還沒有設定引擎環境。")

    model = resolve_translation_model(config.asr, config.output_root)
    if model is None:
        raise mt.TranslationUnavailable(
            "還沒有設定翻譯模型。它是另外一種模型，跟語音辨識用的不是同一個；"
            "沒有它也不影響下載和逐字稿。到「設定 → 語音辨識」加一個即可。"
        )
    return runtime, model


def build_transcript_router() -> APIRouter:
    router = APIRouter(tags=["transcript"])

    @router.post("/transcript", response_model=TranscriptResponse)
    def read_transcript(request: Request, body: TranscriptRequest):
        config = request.app.state.config
        result = tx.load(
            body.target,
            output_root=config.output_root,
            sub_lang=body.sub_lang,
            start=tx.parse_timecode(body.start) if body.start else 0.0,
            end=tx.parse_timecode(body.end) if body.end else None,
            yt_dlp=config.binaries.yt_dlp,
            refresh=body.refresh,
            recognize=body.recognize,
            asr_config=config.asr,
            asr_language=body.asr_language,
            asr_languages=body.asr_languages,
            on_progress=_progress_publisher(request),
        )
        return TranscriptResponse(
            source=str(result.source),
            kind=result.kind,
            language=result.language,
            title=result.title,
            line_count=len(result.lines),
            lines=[TranscriptLine(at=line.at, text=line.text) for line in result.lines],
        )

    @router.post("/translate", response_model=TranslateResponse)
    def translate(request: Request, body: TranslateRequest):
        """Translate a transcript that already exists.

        `def`, not `async def`, for the same reason its neighbour above is:
        this blocks for as long as the model takes, and a thousand-line
        transcript on the event loop would stall the SSE stream every open
        GUI is listening to.

        The two refusals for a machine that cannot translate at all are in
        `_translation_engine`, which this shares with `/translate:doc`.
        """
        from mfp import runs, translate as mt

        config = request.app.state.config
        runtime, model = _translation_engine(config)

        # Before `workspace_for`, not inside `translate_file`. That call
        # CREATES an analysis folder for whatever it is handed, so a source
        # that is not there used to leave an empty one behind on its way to
        # the uncoded 500.
        source = mt.refuse_missing_source(Path(body.source).expanduser())
        outcome = mt.translate_file(
            source,
            # Beside the transcript it was translated FROM, in that
            # analysis's own folder -- never next to a file the user handed
            # us from somewhere else.
            out_dir=runs.workspace_for(config.output_root, source).root,
            python_exe=runtime,
            model_dir=model.path,
            target=body.target,
            source_language=body.source_language,
            device=config.asr.device,
            compute_type=config.asr.compute_type,
            on_progress=_progress_publisher(request, MT_EVENT),
        )
        return TranslateResponse(
            source=str(outcome.source),
            source_language=outcome.source_language,
            target_language=outcome.target_language,
            line_count=outcome.line_count,
            engine=outcome.engine,
            suspect_lines=[index + 1 for index in outcome.suspect_lines],
        )

    @router.post("/translate:doc", response_model=TranslateDocResponse)
    def translate_doc(request: Request, body: TranslateDocRequest):
        """Translate a document. Its own route, beside its sibling.

        Beside `/translate` rather than in a module of its own because what
        the two share is everything a ROUTE has to do -- the engine and model
        lookup, the two refusals, the progress event, the blocking-work
        reasoning above. What differs is segmentation and structure, and that
        lives one layer down in `mfp.translate_doc` (D-131).

        A NEW analysis folder every time, via `runs.open_run` and never
        `workspace_for`: a document is not derived from a transcript, so
        there is no earlier analysis it belongs in, and the first
        translation of it may already have been edited by hand.
        """
        from mfp import runs, translate_doc as td

        config = request.app.state.config
        runtime, model = _translation_engine(config)

        # Before `open_run`, which only ever CREATES: reaching it with a path
        # this verb will decline leaves an empty analysis folder behind for a
        # document that was never translated (P-64).
        source = td.refuse_unless_document(Path(body.source).expanduser())
        outcome = td.translate_document(
            source,
            out_dir=runs.open_run(
                config.output_root, source, stem=source.stem, kind="document"
            ).root,
            python_exe=runtime,
            model_dir=model.path,
            target=body.target,
            source_language=body.source_language,
            device=config.asr.device,
            compute_type=config.asr.compute_type,
            on_progress=_progress_publisher(request, MT_EVENT),
        )
        return TranslateDocResponse(
            source=str(outcome.source),
            source_language=outcome.source_language,
            target_language=outcome.target_language,
            line_count=outcome.line_count,
            engine=outcome.engine,
            suspect_lines=[index + 1 for index in outcome.suspect_lines],
            record=str(outcome.record) if outcome.record else None,
            blocks=outcome.blocks,
            verbatim_blocks=outcome.verbatim_blocks,
            sentences=outcome.sentences,
        )

    @router.post("/transcript:tracks", response_model=TracksResponse)
    def list_tracks(request: Request, body: TracksRequest):
        """What tracks exist, before committing to one.

        One metadata read and no download, so the workspace can offer a
        language list instead of making the reader discover by failure that
        `orig` could not be determined.
        """
        config = request.app.state.config
        found = tx.available_tracks(body.url, yt_dlp=config.binaries.yt_dlp)
        return TracksResponse(
            title=found["title"],
            spoken_language=found["spokenLanguage"],
            written=found["written"],
            automatic_original=found["automaticOriginal"],
            automatic_count=found["automaticCount"],
        )

    def _load(config, body) -> tuple:
        from mfp import correct as corrector
        from mfp.translate import read_cues

        source = Path(body.source).expanduser()
        cues = read_cues(source)
        store = corrector.Glossary.load(corrector.glossary_path(config.output_root))
        proposals = corrector.propose(
            cues, store, allow_phonetic=not body.exact_only)
        return source, cues, store, proposals

    @router.post("/correct:propose", response_model=CorrectResponse)
    def correct_propose(request: Request, body: CorrectRequest):
        """What COULD be changed. Nothing is written.

        Two calls rather than one because the user is between them. That is
        the whole shape of this feature: D-115 shipped the version that
        decided and applied in a single step, and every firing on real data
        damaged correct text.
        """
        from mfp import correct as corrector

        config = request.app.state.config
        source, cues, store, proposals = _load(config, body)
        return CorrectResponse(
            source=str(source),
            cues=cues,
            proposals=[ProposalOut(**p.as_dict()) for p in proposals],
            diff=corrector.diff(cues, proposals),
            phonetic_keys=corrector.pinyin_available(),
            glossary_entries=len(store.entries),
        )

    @router.post("/correct:apply", response_model=ApplyResponse)
    def correct_apply(request: Request, body: ApplyRequest):
        """Write the two copies. The original is not among them.

        The proposals are recomputed rather than carried in the request. A
        client that posted back an edited list could ask for a substitution
        the glossary does not contain, and the whitelist has to be the only
        source of what may be written -- an endpoint that accepts arbitrary
        replacements is not a whitelist, whatever it is called.
        """
        from mfp import correct as corrector, runs

        config = request.app.state.config
        source, cues, store, proposals = _load(config, body)
        accepted = body.accepted
        if accepted is not None:
            accepted = [i for i in accepted if 0 <= i < len(proposals)]
        written = corrector.write_pair(
            source, cues, proposals, glossary=store, accepted=accepted,
            # A `.srt` the user pointed at from their own folder gets a run
            # folder of its own rather than corrected copies dropped beside
            # a file that is not ours.
            out_dir=runs.workspace_for(config.output_root, source).root)
        return ApplyResponse(
            original=str(source),
            written={key: str(path) for key, path in written.items()},
            applied=len(proposals) if accepted is None else len(accepted),
        )

    def _tidy_load(config, source_text: str):
        from mfp import tidy as tidier
        from mfp.translate import read_cues

        source = Path(source_text).expanduser()
        cues = read_cues(source)
        fillers = tidier.FillerList.load(tidier.fillers_path(config.output_root))
        return source, cues, fillers, tidier.propose(cues, fillers)

    @router.post("/tidy:propose", response_model=TidyProposeResponse)
    def tidy_propose(request: Request, body: TidyRequest):
        """What a reading copy would leave out. Nothing is written."""
        from mfp import tidy as tidier

        config = request.app.state.config
        source, cues, fillers, removals = _tidy_load(config, body.source)
        return TidyProposeResponse(
            source=str(source),
            cues=cues,
            removals=[RemovalOut(**r.as_dict()) for r in removals],
            summary=tidier.summary(cues, removals),
            filler_terms=len(fillers),
        )

    @router.post("/tidy:apply", response_model=TidyApplyResponse)
    def tidy_apply(request: Request, body: TidyApplyRequest):
        """Write the reading copy. The original is not among the files.

        The removals are recomputed rather than carried in the request, for
        the reason `correct:apply` recomputes its proposals: a client that
        posted back an edited list could ask for a cue the filler list does
        not cover, and a whitelist that accepts arbitrary deletions is not a
        whitelist. `accepted` may only NARROW what the rules already offered.
        """
        from mfp import runs, tidy as tidier

        config = request.app.state.config
        source, cues, fillers, removals = _tidy_load(config, body.source)
        accepted = body.accepted
        if accepted is not None:
            accepted = [i for i in accepted if 0 <= i < len(removals)]
        written = tidier.write_pair(
            source, cues, removals, fillers=fillers, accepted=accepted,
            out_dir=runs.workspace_for(config.output_root, source).root,
        )
        return TidyApplyResponse(
            original=str(source),
            written={key: str(path) for key, path in written.items()},
            removed=len(removals) if accepted is None else len(accepted),
        )

    @router.get("/fillers", response_model=FillerResponse)
    def fillers(request: Request):
        from mfp import tidy as tidier

        config = request.app.state.config
        path = tidier.fillers_path(config.output_root)
        return FillerResponse(path=str(path),
                              terms=list(tidier.FillerList.load(path).terms))

    @router.post("/fillers:add", response_model=FillerResponse)
    def fillers_add(request: Request, body: FillerEditRequest):
        from mfp import tidy as tidier

        config = request.app.state.config
        path = tidier.fillers_path(config.output_root)
        store = tidier.FillerList.load(path)
        if body.common:
            store = store.add(*tidier.COMMON_FILLERS)
        store = store.add(*body.terms)
        store.save(path)
        return FillerResponse(path=str(path), terms=list(store.terms))

    @router.post("/fillers:remove", response_model=FillerResponse)
    def fillers_remove(request: Request, body: FillerEditRequest):
        """Drop terms. Nothing already written changes -- this only stops
        them being offered again."""
        from mfp import tidy as tidier

        config = request.app.state.config
        path = tidier.fillers_path(config.output_root)
        store = tidier.FillerList.load(path).remove(*body.terms)
        store.save(path)
        return FillerResponse(path=str(path), terms=list(store.terms))

    @router.get("/glossary", response_model=GlossaryResponse)
    def glossary(request: Request):
        from mfp import correct as corrector

        config = request.app.state.config
        path = corrector.glossary_path(config.output_root)
        store = corrector.Glossary.load(path)
        return GlossaryResponse(
            path=str(path),
            entries=[GlossaryEntryOut(term=e.term, aliases=list(e.aliases),
                                      note=e.note) for e in store.entries],
            phonetic_keys=corrector.pinyin_available(),
        )

    @router.post("/glossary:enrol", response_model=GlossaryResponse)
    def enrol(request: Request, body: EnrolRequest):
        """Record a term, and the wrong form it was seen as.

        This is the only way the feature improves. Everything else in it is
        fixed logic; the accuracy comes from what the user has told it.
        """
        from mfp import correct as corrector

        config = request.app.state.config
        path = corrector.glossary_path(config.output_root)
        store = corrector.Glossary.load(path).enrol(body.term, body.alias)
        store.save(path)
        return _glossary_report(path, store)

    @router.post("/glossary:update", response_model=GlossaryResponse)
    def update_term(request: Request, body: SaveTermRequest):
        """Rewrite one entry, or add one in full.

        The other half of `enrol`, and the one the feature was missing: a
        glossary that could only be appended to made its first typo permanent
        -- and this list is what the corrector is ALLOWED to write, so a wrong
        entry in it is a wrong word in every transcript afterwards.
        """
        from mfp import correct as corrector

        config = request.app.state.config
        path = corrector.glossary_path(config.output_root)
        term = body.term.strip()
        if not term:
            raise UsageError("要有一個正確的寫法才能存進詞庫")
        entry = corrector.Entry(
            term=term,
            aliases=tuple(
                dict.fromkeys(a.strip() for a in body.aliases if a.strip() and a.strip() != term)
            ),
            note=body.note.strip(),
        )
        store = corrector.Glossary.load(path).update(body.original, entry)
        store.save(path)
        return _glossary_report(path, store)

    @router.post("/glossary:remove", response_model=GlossaryResponse)
    def remove_term(request: Request, body: RemoveTermRequest):
        """Forget a term. Idempotent: the caller wanted it gone.

        Nothing on disk is touched but the glossary itself -- transcripts
        already corrected with this term keep their corrections, and their
        `corrections.json` still records the entry that produced them.
        """
        from mfp import correct as corrector

        config = request.app.state.config
        path = corrector.glossary_path(config.output_root)
        store = corrector.Glossary.load(path).remove(body.term)
        store.save(path)
        return _glossary_report(path, store)

    return router


def _glossary_report(path, store) -> "GlossaryResponse":
    """The whole list, after every mutation. One shape for four endpoints:
    a caller that had to merge a delta into its own copy would be a second
    place the glossary can be wrong about itself."""
    from mfp import correct as corrector

    return GlossaryResponse(
        path=str(path),
        entries=[
            GlossaryEntryOut(term=e.term, aliases=list(e.aliases), note=e.note)
            for e in store.entries
        ],
        phonetic_keys=corrector.pinyin_available(),
    )
