"""The setup surface for speech recognition.

Every endpoint here exists because the same job could previously only be
done by editing `%APPDATA%/media-fetch-pipeline/config.json` by hand. That
is not a supported way to configure a desktop application; it was simply the
only one, and the GUI's contribution was to render the resulting failure as
red text naming the field the user was supposed to have known about.

Two things separate these routes from the rest of the API.

**They mutate the config, and they persist it.** `PUT /v1/config` already
does that for the settings panel, and these could have been folded into it
-- but "point this at a Python and tell me whether that worked" is a
question, not an assignment, and answering it with the whole config object
would leave the caller to work out what changed and whether it helped. Each
route here returns the READINESS afterwards, which is the only thing the
caller wanted to know.

**Installing a model is minutes of disk I/O.** It is still not a job, for
the reason `routes_transcript` gives: the job vocabulary is a second thing
to learn, and this is one button. Progress travels the same way recognition
progress does -- an SSE event, coalesced -- so a 3 GB copy is visibly moving
without either side learning a new noun.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request

from mfp import asr_models
from mfp.errors import UsageError
from mfp.models import CamelModel

#: The SSE event a model install reports progress on. Named for the work,
#: not for the endpoint, so a future second installer can share it.
INSTALL_EVENT = "asrInstall"


class ScanRequest(CamelModel):
    path: str


class ScanResponse(CamelModel):
    """What was found, and -- when nothing was -- why.

    Both halves are needed. A scan that returns an empty list and no reason
    is the developer-facing failure this whole change is about: the user
    picked a folder, the panel said nothing, and the only way to learn that
    they had picked the parent of the model was to guess it.
    """

    models: list[asr_models.ModelReport]
    #: The verdict on the picked folder itself, always present, so the panel
    #: can explain an empty `models` in the folder's own terms.
    diagnosis: asr_models.ModelReport
    modes: list[dict]


class InstallRequest(CamelModel):
    path: str
    mode: Literal["copy", "move", "link"]
    #: Folder name in the model home. Defaults to the model's own.
    name: str | None = None


class SelectRequest(CamelModel):
    name: str


class PathRequest(CamelModel):
    path: str


class InstallResponse(CamelModel):
    model: asr_models.ModelReport
    readiness: asr_models.AsrReadiness


def _field_for(kind: str) -> str:
    """Which `AsrConfig` field holds the selection for a model of this kind.

    One place, because the mapping is the safety property: `asr.model` is
    what the recogniser loads, and putting a translation model there is a
    silent way to break transcribing for somebody who thought they were
    adding a feature.
    """
    return "translation_model" if kind == "translation" else "model"


def _readiness(request: Request) -> asr_models.AsrReadiness:
    config = request.app.state.config
    return asr_models.readiness(config.asr, config.output_root)


def _persist(request: Request, asr_config) -> asr_models.AsrReadiness:
    """Replace the `asr` section, save, and report the new verdict.

    `model_copy(update=...)` rather than mutation: `app.state.config` is
    handed out by `GET /v1/config` and read by the Electron main process for
    the containment check, and a half-updated object visible to either of
    them is a race nobody would find twice.
    """
    config = request.app.state.config
    updated = config.model_copy(update={"asr": asr_config})
    request.app.state.config = updated
    saver = getattr(request.app.state, "save_config", None)
    if saver is not None:
        saver(updated)
    return asr_models.readiness(updated.asr, updated.output_root)


def _progress_publisher(request: Request):
    """Same shape as `routes_transcript._progress_publisher`, same reasons.

    Duplicated rather than shared because the two differ in the one place
    that matters -- the event name -- and a helper parameterised on that
    would be four lines of indirection over four lines of code.
    """
    broadcaster = getattr(request.app.state, "broadcaster", None)
    if broadcaster is None:
        return None
    dispatch = getattr(request.app.state, "dispatch", None) or (lambda fn: fn())

    def publish(record: dict) -> None:
        dispatch(
            lambda: broadcaster.publish(
                INSTALL_EVENT, record, coalesce_key=INSTALL_EVENT
            )
        )

    return publish


def build_asr_router() -> APIRouter:
    router = APIRouter(tags=["asr"], prefix="/asr")

    @router.get("/readiness", response_model=asr_models.AsrReadiness)
    def get_readiness(request: Request) -> asr_models.AsrReadiness:
        """Can this machine transcribe, and if not, what is left to do.

        A `def` rather than `async def`: it probes an interpreter with a
        subprocess and touches the disk, and neither belongs on the event
        loop that every open GUI is streaming from.
        """
        return _readiness(request)

    @router.post("/models:scan", response_model=ScanResponse)
    def scan_models(request: Request, body: ScanRequest) -> ScanResponse:
        config = request.app.state.config
        home = asr_models.model_home(config.asr, config.output_root)
        found = asr_models.find_models(body.path)
        source = found[0].path if len(found) == 1 and found[0].usable else None
        return ScanResponse(
            models=found,
            diagnosis=asr_models.inspect_model(body.path),
            modes=asr_models.describe_modes(home, source),
        )

    @router.post("/models:install", response_model=InstallResponse)
    def install(request: Request, body: InstallRequest) -> InstallResponse:
        config = request.app.state.config
        home = asr_models.model_home(config.asr, config.output_root)
        installed = asr_models.install_model(
            body.path,
            home,
            mode=body.mode,
            name=body.name,
            on_progress=_progress_publisher(request),
        )
        # Selected as a consequence of installing it, and only when nothing
        # usable was selected before -- and into the field its KIND belongs
        # in. Adding a second model must not silently switch the one a person
        # had already chosen; adding the FIRST one and then making them pick
        # it is a step with exactly one answer. Writing a translation model
        # into `model` would break recognition, which is why the field is
        # read off the model rather than off the request.
        field = _field_for(installed.kind)
        asr_config = config.asr
        if asr_models.find_installed(home, getattr(config.asr, field) or "",
                                     kind=installed.kind) is None:
            asr_config = config.asr.model_copy(update={field: installed.name})
        return InstallResponse(model=installed, readiness=_persist(request, asr_config))

    @router.get("/catalogue")
    def catalogue(request: Request) -> dict:
        """What can be fetched, and which of it is already here.

        `installed` is answered on THIS side rather than left to the panel:
        the model home is the server's to know, and a panel deciding for
        itself whether a folder counts would be a second implementation of
        `find_installed` with its own opinion about lenient name matching.
        """
        config = request.app.state.config
        home = asr_models.model_home(config.asr, config.output_root)
        rows = []
        for entry in asr_models.CATALOGUE:
            found = asr_models.find_installed(home, entry["id"], kind=entry["kind"])
            rows.append({**entry, "installed": found is not None})
        return {"models": rows}

    @router.post("/models:download", response_model=InstallResponse)
    def download(request: Request, body: SelectRequest) -> InstallResponse:
        """Fetch one catalogue entry. `name` is its id.

        Long-running and reported the whole way: the reason `allowDownload`
        is off by default is that a silent multi-gigabyte transfer looks
        exactly like a hang, and a button that reproduced that would be no
        better than the setting it replaces.
        """
        from mfp import asr

        config = request.app.state.config
        home = asr_models.model_home(config.asr, config.output_root)
        interpreter, _, problem = asr.find_runtime_detailed(config.asr.python)
        if interpreter is None:
            raise UsageError(
                problem or "還沒有設定語音辨識引擎，所以沒有可以用來下載的環境。"
            )

        installed = asr_models.download_model(
            body.name,
            home,
            python_exe=interpreter,
            fetcher=asr.fetch_model_path(),
            on_progress=_progress_publisher(request),
        )
        # Selected on arrival under the same rule `models:install` follows:
        # adding the FIRST usable model of a kind and then making somebody
        # pick it is a step with exactly one answer, and adding a second must
        # not silently replace the one they already chose.
        field = _field_for(installed.kind)
        asr_config = config.asr
        if asr_models.find_installed(home, getattr(config.asr, field) or "",
                                     kind=installed.kind) is None:
            asr_config = config.asr.model_copy(update={field: installed.name})
        return InstallResponse(model=installed, readiness=_persist(request, asr_config))

    @router.post("/models:select", response_model=asr_models.AsrReadiness)
    def select(request: Request, body: SelectRequest) -> asr_models.AsrReadiness:
        """Use this model, for whatever it turns out to be for.

        The caller does not say which capability it means, and must not: the
        folder already answers that, and a request that could name the wrong
        field is a request that can silently disable transcription.
        """
        config = request.app.state.config
        home = asr_models.model_home(config.asr, config.output_root)
        for kind in ("recognition", "translation"):
            found = asr_models.find_installed(home, body.name, kind=kind)
            if found is not None:
                return _persist(
                    request,
                    config.asr.model_copy(update={_field_for(kind): found.name}),
                )
        raise UsageError(f"模型資料夾裡沒有可用的「{body.name}」。")

    @router.post("/models:remove", response_model=asr_models.AsrReadiness)
    def remove(request: Request, body: SelectRequest) -> asr_models.AsrReadiness:
        """Only ever a shortcut. `uninstall_entry` refuses anything else."""
        config = request.app.state.config
        home = asr_models.model_home(config.asr, config.output_root)
        asr_models.uninstall_entry(home, body.name)
        return _readiness(request)

    @router.post("/engine", response_model=asr_models.AsrReadiness)
    def set_engine(request: Request, body: PathRequest) -> asr_models.AsrReadiness:
        """Point at a Python, and say immediately whether it can do the job.

        Validated BEFORE it is saved. Storing a path that cannot import the
        engine and reporting the failure at the next transcription is how the
        old config-file route behaved, and the delay between the action and
        its consequence is most of why that was unusable.
        """
        candidate = Path(body.path).expanduser()
        if not candidate.is_file():
            raise UsageError(f"找不到這個檔案：{candidate}")
        _version, problem = asr_models.probe_engine(candidate)
        if problem is not None:
            raise UsageError(problem)
        config = request.app.state.config
        return _persist(request, config.asr.model_copy(update={"python": str(candidate)}))

    @router.post("/home", response_model=asr_models.AsrReadiness)
    def set_home(request: Request, body: PathRequest) -> asr_models.AsrReadiness:
        """Move the model home. Does NOT move the models.

        Deliberate: the models may be junctions, they may be 3 GB each, and
        the user may be pointing this at a folder that already has them. The
        panel says which models the new home contains the moment this
        returns, which is the honest version of "did that do what I meant".
        """
        target = Path(body.path).expanduser()
        if target.exists() and not target.is_dir():
            raise UsageError(f"這不是一個資料夾：{target}")
        config = request.app.state.config
        return _persist(request, config.asr.model_copy(update={"model_dir": str(target)}))

    @router.post("/download", response_model=asr_models.AsrReadiness)
    def set_download(request: Request, body: SelectRequest) -> asr_models.AsrReadiness:
        """Toggle `allow_download`. `name` carries "on"/"off".

        A switch rather than a hidden default, because the thing it permits
        is a 3 GB transfer, and an unannounced multi-gigabyte download is
        indistinguishable from a hang -- which is why it is off to begin
        with.
        """
        wanted = body.name.strip().lower() in ("on", "true", "1", "yes")
        config = request.app.state.config
        return _persist(
            request, config.asr.model_copy(update={"allow_download": wanted})
        )

    @router.post("/audio", response_model=asr_models.AsrReadiness)
    def set_audio(request: Request, body: SelectRequest) -> asr_models.AsrReadiness:
        """What to do to the sound before recognising it. `name` is the mode.

        Validated HERE rather than trusted, because the value is handed
        straight to the engine's argv: an unknown mode would make the runner
        exit on an argparse error, which reaches the reader as "the engine
        could not start" -- a sentence about the wrong thing entirely.
        """
        wanted = body.name.strip().lower()
        if wanted not in ("none", "level", "denoise"):
            raise UsageError(f"不認得的音訊處理方式「{body.name}」。")
        config = request.app.state.config
        return _persist(request, config.asr.model_copy(update={"audio": wanted}))

    return router
