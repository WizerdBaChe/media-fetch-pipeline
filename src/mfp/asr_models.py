"""Where recognition models live, and whether one of them is usable.

`asr.py` answers "can this machine listen"; this module answers the two
questions a person actually asks before that one becomes answerable: **where
do I put the model**, and **is the thing I just downloaded the right thing**.

Both were previously answered by a config file. `model_dir` defaulted to
`None`, which handed the question to faster-whisper's own Hugging Face cache
-- a directory nobody named, in a place nobody chose, holding 3 GB. And
`model` defaulted to the string `"large-v3"`, which is not a model but a
DOWNLOAD INSTRUCTION: it means "fetch Systran/faster-whisper-large-v3 from
the Hub", and on a machine that already had the weights it meant nothing at
all. Neither had a surface in the GUI, so the only way to change either was
to hand-edit JSON.

Three ideas hold this module up.

**A model home is a real directory with a name.** `default_model_home()`
resolves one from how the product was installed rather than from a constant,
because those are different machines: an installed build owns its own
program directory and can keep 3 GB beside itself, and a portable build owns
nothing but the output folder the user already chose (user ruling,
2026-08-28).

**Support is decided by reading bytes, not by trusting a name.** A
CTranslate2 model file opens with its binary version, then the length-
prefixed name of the spec that wrote it -- `WhisperSpec` for the thing we
can use, something else for a translation model that happens to share the
format. Three more facts come free: the mel-bin count in
`preprocessor_config.json` separates the large-v3 generation (128) from
everything before it (80), the length of `lang_ids` separates multilingual
from English-only, and the size on disk is the size on disk. None of it
loads the model, so the answer arrives in milliseconds instead of the
thirty seconds and 3 GB of RAM that a real load costs.

**What cannot be determined is reported, never ruled on** (P-38). A spec
name we do not recognise, or a binary version newer than any we have seen,
is a NOTE on a model that is otherwise fine -- not a refusal. The authority
on "will it load" is the load, and it happens later; this module's job is to
catch the cases where the answer is knowably no, and to say what is unknown
the rest of the time.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import sys
from pathlib import Path
from typing import Callable, Iterable, Literal

from mfp.errors import MfpError, UsageError
from mfp.models import CamelModel
from mfp.naming import sanitize_component

__all__ = [
    "MODEL_HOME_ENV",
    "PORTABLE_ENV",
    "InstallMode",
    "ModelReport",
    "default_model_home",
    "find_installed",
    "find_models",
    "install_model",
    "inspect_model",
    "installed_models",
    "link_probe",
    "KIND_LABEL",
    "CapabilityStatus",
    "ModelKind",
    "model_home",
    "models_of_kind",
    "probe_engine",
    "readiness",
    "resolve_model_argument",
    "resolve_translation_model",
]

#: Escape hatch and test seam, and the way the desktop shell could name a
#: home the sidecar has no way to derive. Same role `MFP_ASR_PYTHON` plays
#: for the interpreter.
MODEL_HOME_ENV = "MFP_MODEL_HOME"

#: Set by electron-builder's portable target, and by nothing else. A portable
#: build unpacks itself into `%TEMP%` on every launch, so the directory the
#: executable appears to live in is not a place anything may be kept -- which
#: is exactly why its presence, not its value, is what this module reads.
PORTABLE_ENV = "PORTABLE_EXECUTABLE_DIR"

#: The folder a portable or source build keeps models in, under the output
#: root. Leading underscore for the same reason `_captions` has one: it sorts
#: away from the post directories and reads as ours rather than as content.
PORTABLE_HOME_NAME = "_models"

#: What an installed build calls the same folder, beside its own executable.
INSTALLED_HOME_NAME = "models"

#: The two files a CTranslate2 model cannot be one without.
MODEL_BIN = "model.bin"
MODEL_CONFIG = "config.json"

#: The spec name written into `model.bin` by the Whisper converter.
WHISPER_SPEC = "WhisperSpec"

#: What a converted NLLB / M2M-100 / OPUS-MT model writes there instead.
#: CTranslate2's own name for an encoder-decoder translation model, and the
#: reason this module can tell the two KINDS apart by reading twelve bytes.
TRANSFORMER_SPEC = "TransformerSpec"

#: Spec name -> which capability the model serves. A spec that is not in
#: here is `unknown`: reported, never guessed at, and never handed to either
#: engine (P-38 -- the check can see the spec, so it can see that it does
#: not recognise it).
SPEC_KIND: dict[str, str] = {
    WHISPER_SPEC: "recognition",
    TRANSFORMER_SPEC: "translation",
}

#: What each kind is called on screen. Here rather than in the GUI because
#: the CLI prints the same word, and two vocabularies for one taxonomy is
#: how 「翻譯模型」 and 「不是語音辨識模型」 end up describing the same folder.
KIND_LABEL: dict[str, str] = {
    "recognition": "辨識模型",
    "translation": "翻譯模型",
    "unknown": "用途不明的模型",
}

#: One line per kind, for a reader who has never met either word.
KIND_WHAT: dict[str, str] = {
    "recognition": "把聲音變成文字",
    "translation": "把已經有的逐字稿換成另一種語言",
    "unknown": "這個程式看不出它是做什麼的",
}

#: Tokenizer files a translation model may carry, and what reading each one
#: costs this machine. Order is preference: `tokenizer.json` needs only
#: `tokenizers`, which faster-whisper already installs, so a model that has
#: it needs NOTHING added to the engine -- which is the difference between
#: "download a model" and "download a model and also install a package".
TOKENIZER_FILES: tuple[tuple[str, str, str], ...] = (
    ("tokenizer.json", "tokenizers", "不需要額外安裝套件"),
    ("sentencepiece.bpe.model", "sentencepiece", "需要在引擎環境安裝 sentencepiece"),
    ("source.spm", "sentencepiece", "需要在引擎環境安裝 sentencepiece"),
    ("spiece.model", "sentencepiece", "需要在引擎環境安裝 sentencepiece"),
)

#: FLORES-200 language codes, which is how NLLB names languages: three
#: lowercase letters, an underscore, then a four-letter script. Matched
#: rather than listed, because the point is to COUNT them and to find the
#: one the user asked for -- not to have an opinion about which exist.
_FLORES = re.compile(r"^[a-z]{3}_[A-Z][a-z]{3}$")

#: The highest CTranslate2 binary version this build has seen (CTranslate2
#: 4.7.1, measured 2026-08-28). A HIGHER number is not a refusal: it means
#: the model was converted by a newer CTranslate2 than we know about, which
#: the installed engine may well still read. It becomes a note, and the load
#: stays the authority.
#:
#: review-when: a CTranslate2 major release lands, or a model reports a
#: version above this and loads anyway.
KNOWN_BINARY_VERSION = 6

#: Hugging Face's on-disk cache layout. `download_root` points at a directory
#: of these, each holding `snapshots/<sha>/` with the real files, which is
#: what a user who has ever run faster-whisper already has and what they will
#: hand this tool when asked for "the model".
HUB_PREFIX = "models--"

#: Repository prefix -> what the model's own NAME uses instead.
#:
#: `Systran/faster-whisper-large-v3` IS `large-v3`, and matching the two lets
#: a config that says `large-v3` find the folder somebody downloaded instead
#: of going back to the Hub for 3 GB already on the disk.
#:
#: The distil row is a REPLACEMENT rather than a removal, and that is the
#: whole reason this is a mapping. Stripping `faster-distil-whisper-` down to
#: `large-v3` would make a config that says `large-v3` match
#: `faster-distil-whisper-large-v3` -- a different model, and an
#: English-only one, so a Chinese recording would come back as nonsense with
#: nothing anywhere reporting a substitution. Caught by
#: `test_a_configured_name_finds_the_folder_it_means`, which had the shape in
#: it because P-42 says to go looking for shapes.
#:
#: Longest first: `faster-distil-whisper-` also starts with nothing else
#: here, but `faster-whisper-` would win a shorter-first scan over any name
#: they ever share.
_REPO_PREFIXES = (
    ("faster-distil-whisper-", "distil-"),
    ("faster-whisper-", ""),
    ("whisper-", ""),
)

InstallMode = Literal["copy", "move", "link"]

ModelState = Literal[
    "ready",
    "unknown_kind",
    "needs_conversion",
    "incomplete",
    "not_a_model",
    "unreadable",
]

#: What a model is FOR. Read off the spec name, never guessed from the
#: folder. It became a first-class field when translation arrived: until
#: then "not a Whisper model" and "not a model" were the same sentence, and
#: a user who had downloaded a perfectly good translation model was told
#: their folder was wrong.
ModelKind = Literal["recognition", "translation", "unknown"]

#: Files that say "somebody converted a Whisper model with the transformers
#: library and stopped one step early". They are a DIFFERENT answer from
#: "this is not a model": the user has the right weights in the wrong
#: container, and one command fixes it.
_TRANSFORMERS_MARKERS = (
    "pytorch_model.bin",
    "model.safetensors",
    "flax_model.msgpack",
    "tf_model.h5",
)

#: How a partial `huggingface_hub` download looks on disk.
_PARTIAL_SUFFIXES = (".incomplete", ".part", ".tmp", ".download")


class ModelReport(CamelModel):
    """One directory, judged. Everything here was read, not assumed.

    `summary` and `notes` are Traditional Chinese and user-facing: this is
    the one contract in the project where the machine's finding and the
    sentence a person reads are produced together, because they were drifting
    apart -- the CLI said "could not load the 'large-v3' model" and the GUI
    said "沒有語音辨識引擎" about the same directory.
    """

    #: What to call it. The repository name for a Hub cache, the folder name
    #: otherwise -- never a hard-coded `large-v3`.
    name: str
    path: str
    state: ModelState
    #: The single question the setup panel asks. `state == "ready"`, hoisted
    #: so no reader has to know the state vocabulary to render a tick.
    #:
    #: It means "complete and loadable AS ITS KIND". A translation model is
    #: usable and cannot transcribe; asking whether a model is usable is
    #: therefore never enough on its own -- `kind` is the other half, and
    #: `find_installed` takes both.
    usable: bool
    #: What this model is for. The field the whole two-capability display
    #: hangs off, and the one thing a person has to be able to see at a
    #: glance about a folder they downloaded.
    kind: ModelKind = "unknown"
    #: `kind`, in the words shown on screen. Carried rather than mapped by
    #: the reader so the CLI and the GUI cannot drift into two vocabularies.
    kind_label: str = ""
    summary: str
    notes: list[str] = []
    size_bytes: int = 0
    layout: Literal["plain", "hub"] | None = None
    spec: str | None = None
    #: Which tokenizer file this model carries, and what reading it costs.
    #: Only translation models need one; recognition models carry their own
    #: inside the faster-whisper package. `None` on a translation model is a
    #: real defect and is reported as one.
    tokenizer: str | None = None
    #: The pip package needed to read that tokenizer, or `None` when the
    #: engine already has everything. This is the difference between
    #: "download a model" and "download a model AND install a package", and
    #: a user deserves to know which one they are signing up for.
    tokenizer_package: str | None = None
    #: The languages a translation model names in its own special-tokens
    #: file, FLORES-200 style (`zho_Hant`). Empty when the model does not
    #: say -- which is reported, not filled in.
    language_codes: list[str] = []
    spec_revision: int | None = None
    binary_version: int | None = None
    #: 128 on the large-v3 generation, 80 on everything before it.
    mel_bins: int | None = None
    #: How many languages the model was trained to recognise. 1 means an
    #: English-only build, which is a real and easily-missed foot-gun for a
    #: user whose recordings are Chinese.
    languages: int | None = None
    #: The folder name `install_model` would give it. `None` when there is
    #: nothing worth installing.
    suggested_name: str | None = None
    #: True when this entry is a junction rather than the model itself.
    #:
    #: The setting panel needs it for one reason and it is a safety one: a
    #: shortcut is the only thing removable from inside the app, because
    #: removing it deletes nothing. A real 3 GB folder is sent to Explorer,
    #: which has a recycle bin. Nothing else can tell the two apart -- a
    #: junction reports the same files, the same size and the same layout as
    #: what it points at, which is the entire point of a junction.
    is_link: bool = False


# --------------------------------------------------------------------------
# Where the models live
# --------------------------------------------------------------------------


def _install_dir() -> Path | None:
    """The directory the installed build was installed into, or `None`.

    In a packaged desktop build the sidecar is
    `<install>/resources/sidecar/mfp-sidecar.exe`, so the install directory
    is two parents up from the executable's own folder. `sys.executable` and
    not `sys._MEIPASS`: the onefile bootloader unpacks to `%TEMP%`, and a
    model written there would be deleted the moment the app closed.

    Returns `None` when nothing about the process says "installed", which
    covers a source checkout and covers being imported by a test.
    """
    if not getattr(sys, "frozen", False):
        return None
    if os.environ.get(PORTABLE_ENV):
        # Packaged, but into a self-extracting portable exe: `sys.executable`
        # is under `%TEMP%` and there is no install directory to speak of.
        return None
    here = Path(sys.executable).resolve().parent
    # `<install>/resources/sidecar` -> `<install>`. Guarded rather than
    # indexed: a different packaging layout must degrade to "no install
    # directory" instead of naming a directory two levels above wherever it
    # happens to be.
    if here.name == "sidecar" and here.parent.name == "resources":
        return here.parent.parent
    return None


def default_model_home(output_root: str | Path) -> Path:
    """Where models go when nobody has said otherwise.

    Ruled by the user 2026-08-28: an installed build keeps them beside
    itself, a portable build keeps them in the output folder. The reasoning
    is about what each build OWNS -- an installer asked for a directory and
    got one, and a portable exe was copied to a USB stick and owns nothing
    except the output root the user already chose.

    The environment wins over both, and comes first for the same reason it
    does in `asr.find_runtime`: a variable is set for one run, and a run is
    where an override belongs.
    """
    from_env = os.environ.get(MODEL_HOME_ENV)
    if from_env:
        return Path(from_env).expanduser()

    installed = _install_dir()
    if installed is not None:
        return installed / INSTALLED_HOME_NAME

    return Path(output_root).expanduser() / PORTABLE_HOME_NAME


def model_home(asr_config, output_root: str | Path) -> Path:
    """The environment, the configured home, or the default. Never `None`.

    `AsrConfig.model_dir` is the setting; it used to mean "faster-whisper's
    `download_root`" and now means "the folder the models are in", which is
    the same directory seen from the user's side rather than from the
    engine's.

    **The environment beats the setting**, matching `asr.find_runtime`
    exactly. It did not until 2026-08-28, and the bug was the same one that
    module already had and fixed: with `model_dir` set, exporting
    `MFP_MODEL_HOME` did nothing at all and said nothing about it -- a switch
    that silently does not switch. The reasoning is the same too: a variable
    is set for one shell and one run, a config file is the standing answer,
    and the standing answer is what an override is for.
    """
    from_env = os.environ.get(MODEL_HOME_ENV)
    if from_env:
        return Path(from_env).expanduser()
    configured = getattr(asr_config, "model_dir", None)
    if configured:
        return Path(configured).expanduser()
    return default_model_home(output_root)


def home_status(home: Path) -> dict:
    """Facts about the home directory a setup panel has to show anyway.

    Free space is here because it is the difference between a 3 GB copy that
    works and one that fills the system drive and fails halfway. Asking
    before starting is the whole of this module's contribution to that.
    """
    exists = home.is_dir()
    writable = _writable(home)
    free = None
    try:
        free = shutil.disk_usage(home if exists else _nearest_existing(home)).free
    except OSError:
        pass
    return {
        "path": str(home),
        "exists": exists,
        "writable": writable,
        "freeBytes": free,
    }


def _nearest_existing(path: Path) -> Path:
    for candidate in [path, *path.parents]:
        if candidate.is_dir():
            return candidate
    return Path(path.anchor or ".")


def _writable(directory: Path) -> bool:
    """Can we actually create something here?

    Answered by creating something, because `os.access` on Windows reports
    the read-only ATTRIBUTE rather than the ACL, and the case this exists for
    -- the user pointed the installer at `C:\\Program Files` and the app runs
    unelevated -- is precisely one that `os.access` calls writable.
    """
    target = directory if directory.is_dir() else _nearest_existing(directory)
    probe = target / ".mfp-write-probe"
    try:
        probe.mkdir(exist_ok=False)
    except OSError:
        return False
    try:
        probe.rmdir()
    except OSError:
        pass
    return True


# --------------------------------------------------------------------------
# Reading a model without loading it
# --------------------------------------------------------------------------


def _read_spec(model_bin: Path) -> tuple[int | None, str | None, int | None]:
    """`(binary_version, spec_name, spec_revision)` from the file header.

    The CTranslate2 serialisation opens with a little-endian `uint32`
    version, a `uint16` length, that many bytes of NUL-terminated spec name,
    and a `uint16` revision. Measured against
    `Systran/faster-whisper-large-v3` on 2026-08-28:
    `06 00 00 00 | 0c 00 | "WhisperSpec\\0" | 03 00`.

    Every failure returns `None`s rather than raising. A header we cannot
    parse is a fact about our reading, not a verdict on the file, and the
    caller downgrades to "unknown" instead of to "broken".
    """
    try:
        with model_bin.open("rb") as handle:
            head = handle.read(8)
            if len(head) < 6:
                return None, None, None
            version, name_len = struct.unpack("<IH", head[:6])
            if not 1 <= name_len <= 255:
                return version, None, None
            handle.seek(6)
            raw = handle.read(name_len)
            if len(raw) < name_len:
                return version, None, None
            name = raw.rstrip(b"\x00").decode("ascii", errors="replace")
            revision_bytes = handle.read(2)
            revision = (
                struct.unpack("<H", revision_bytes)[0]
                if len(revision_bytes) == 2
                else None
            )
            return version, name, revision
    except OSError:
        return None, None, None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _directory_bytes(directory: Path) -> int:
    total = 0
    try:
        for entry in directory.rglob("*"):
            try:
                if entry.is_file() and not entry.is_symlink():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _hub_repo_name(directory: Path) -> str | None:
    """`models--Systran--faster-whisper-large-v3` -> the repo's model part.

    The organisation is dropped from the NAME and kept in the notes: two
    orgs publishing `faster-whisper-large-v3` is a collision the home
    directory would have to resolve, and it has never happened -- whereas a
    folder called `models--Systran--faster-whisper-large-v3` shown to a user
    as the name of their model happens every time.
    """
    if not directory.name.startswith(HUB_PREFIX):
        return None
    parts = directory.name[len(HUB_PREFIX):].split("--")
    return parts[-1] if parts and parts[-1] else None


def _hub_snapshot(repo_dir: Path) -> Path | None:
    """The snapshot a Hub cache entry currently points at.

    `refs/main` names it; when that file is missing or names a snapshot that
    is not there, the newest snapshot directory that actually holds a
    `model.bin` wins. A cache with several snapshots and no ref is a normal
    state after an interrupted update, not an error.
    """
    snapshots = repo_dir / "snapshots"
    if not snapshots.is_dir():
        return None

    ref = repo_dir / "refs" / "main"
    if ref.is_file():
        try:
            named = snapshots / ref.read_text(encoding="utf-8").strip()
            if (named / MODEL_BIN).is_file():
                return named
        except OSError:
            pass

    candidates = [
        child
        for child in _safe_iterdir(snapshots)
        if child.is_dir() and (child / MODEL_BIN).is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda child: child.stat().st_mtime)


def _safe_iterdir(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir())
    except OSError:
        return []


def _suggested_name(name: str) -> str:
    return sanitize_component(name, fallback="whisper-model")


def normalize_model_name(name: str) -> str:
    """`Systran/faster-whisper-large-v3`, `faster-whisper-large-v3` and
    `large-v3` all reduce to the same key.

    Used only for MATCHING a configured name against installed folders. It
    never renames anything: the folder keeps whatever it was given, and the
    config keeps whatever the user typed.
    """
    key = name.strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if key.startswith(HUB_PREFIX):
        key = key[len(HUB_PREFIX):].split("--")[-1]
    for prefix, replacement in _REPO_PREFIXES:
        if key.startswith(prefix):
            key = replacement + key[len(prefix):]
            break
    return key.replace("_", "-")


def inspect_model(path: str | Path) -> ModelReport:
    """Judge ONE directory, resolving the Hub layout if that is what it is.

    Never raises for a bad path. The states are the six different sentences
    a person needs to hear, and "this threw an exception" is not one of them.

    `is_link` is stamped HERE rather than inside each branch because it is a
    property of the path that was asked about, not of whatever the judgement
    ended up being about -- for a Hub cache those are two different
    directories, and the one the user can remove is this one.
    """
    directory = Path(path).expanduser()
    report = _inspect(directory)
    try:
        return report.model_copy(update={"is_link": _is_link(directory)})
    except OSError:
        return report


def _inspect(directory: Path) -> ModelReport:

    if not directory.exists():
        return ModelReport(
            name=directory.name or str(directory),
            path=str(directory),
            state="not_a_model",
            usable=False,
            summary="找不到這個資料夾。",
            notes=["路徑可能被移動、改名，或所在的磁碟沒有接上。"],
        )
    if not directory.is_dir():
        return _report_single_file(directory)

    # A Hub cache entry: the real files are one `snapshots/<sha>/` down.
    if directory.name.startswith(HUB_PREFIX):
        snapshot = _hub_snapshot(directory)
        if snapshot is None:
            return ModelReport(
                name=_hub_repo_name(directory) or directory.name,
                path=str(directory),
                state="incomplete",
                usable=False,
                summary="這是一個下載到一半的模型快取，裡面沒有完整的模型檔。",
                notes=["把原本的下載跑完，或改選另一個資料夾。"],
                layout="hub",
            )
        return _report_model_dir(
            snapshot,
            name=_hub_repo_name(directory) or directory.name,
            layout="hub",
            origin=directory,
        )

    if (directory / MODEL_BIN).is_file():
        # A Hub SNAPSHOT, reached directly rather than through its wrapper.
        # Two ordinary paths land here: a scan reports the snapshot (that is
        # where the weights are) and the caller hands that same path back to
        # install, and a person browses one folder too far in the file
        # dialog. Both used to install a model named
        # `edaa852ec7e145841d8ffdb056a99866b5f0a478`, which is a directory
        # listing nobody can read -- and a `model` setting nobody can type.
        #
        # Recovered HERE rather than at the two call sites, because the
        # question "what is this model called" has one answer and this is
        # where it is worked out. Found by an end-to-end run against the real
        # cache on 2026-08-28; no unit test could see it, because every one
        # of them builds the folder and therefore names it.
        repo = _hub_repo_of_snapshot(directory)
        if repo is not None:
            return _report_model_dir(directory, name=repo, layout="hub")
        return _report_model_dir(directory, name=directory.name, layout="plain")

    return _report_non_model(directory)


def _hub_repo_of_snapshot(directory: Path) -> str | None:
    """`…/models--Org--Name/snapshots/<sha>` -> `Name`, else `None`.

    Structural, not a name guess: the answer is yes only when the two
    directories above this one are literally the Hub layout, so an ordinary
    folder that happens to be called something hex-looking is untouched.
    """
    parent = directory.parent
    if parent.name != "snapshots":
        return None
    return _hub_repo_name(parent.parent)


def _report_single_file(path: Path) -> ModelReport:
    """A file, not a folder. Almost always an OpenAI `.pt` checkpoint."""
    if path.suffix.lower() in (".pt", ".pth", ".bin", ".safetensors"):
        return ModelReport(
            name=path.stem,
            path=str(path),
            state="needs_conversion",
            usable=False,
            summary="這是一個還沒轉檔的模型權重檔，這個程式讀不了。",
            notes=[
                "本程式用的是 CTranslate2 格式（一個資料夾，裡面有 model.bin）。",
                "最省事的做法是直接下載已經轉好的版本，見下方「怎麼取得模型」。",
            ],
            size_bytes=_file_size(path),
        )
    return ModelReport(
        name=path.name,
        path=str(path),
        state="not_a_model",
        usable=False,
        summary="要選的是「資料夾」，不是單一檔案。",
        notes=["模型是一個資料夾，裡面會有 model.bin。"],
        size_bytes=_file_size(path),
    )


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _report_non_model(directory: Path) -> ModelReport:
    """No `model.bin` here. Say which of the several reasons it is."""
    children = _safe_iterdir(directory)
    if not children:
        return ModelReport(
            name=directory.name,
            path=str(directory),
            state="not_a_model",
            usable=False,
            summary="這個資料夾是空的。",
        )

    names = {child.name.lower() for child in children}
    if any(marker in names for marker in _TRANSFORMERS_MARKERS):
        return ModelReport(
            name=directory.name,
            path=str(directory),
            state="needs_conversion",
            usable=False,
            summary="這是 Hugging Face transformers 格式的模型，要先轉成 CTranslate2 才能用。",
            notes=[
                "轉換指令："
                "ct2-transformers-converter --model <這個資料夾> "
                "--output_dir <新資料夾> --quantization float16",
                "或直接下載已經轉好的版本，見下方「怎麼取得模型」。",
            ],
            size_bytes=_directory_bytes(directory),
        )

    if any(
        child.name.endswith(_PARTIAL_SUFFIXES)
        for child in children
        if child.is_file()
    ):
        return ModelReport(
            name=directory.name,
            path=str(directory),
            state="incomplete",
            usable=False,
            summary="這個資料夾裡是一份沒下載完的模型。",
            notes=["把原本的下載跑完再回來選一次。"],
            size_bytes=_directory_bytes(directory),
        )

    inner = [child for child in children if child.is_dir()]
    if inner:
        return ModelReport(
            name=directory.name,
            path=str(directory),
            state="not_a_model",
            usable=False,
            summary="這個資料夾本身不是模型，但裡面還有子資料夾。",
            notes=["可以往裡面再選一層，或按「掃描這個資料夾」讓程式自己找。"],
        )

    return ModelReport(
        name=directory.name,
        path=str(directory),
        state="not_a_model",
        usable=False,
        summary="這個資料夾裡沒有 model.bin，不是一個模型。",
    )


def _report_model_dir(
    directory: Path,
    *,
    name: str,
    layout: Literal["plain", "hub"],
    origin: Path | None = None,
) -> ModelReport:
    """The real work: a directory that HAS a `model.bin`, read and judged."""
    model_bin = directory / MODEL_BIN
    binary_version, spec, revision = _read_spec(model_bin)
    config = _read_json(directory / MODEL_CONFIG)
    preprocessor = _read_json(directory / "preprocessor_config.json")

    lang_ids = config.get("lang_ids")
    languages = len(lang_ids) if isinstance(lang_ids, list) else None
    mel_bins = preprocessor.get("feature_size")
    mel_bins = mel_bins if isinstance(mel_bins, int) else None
    size = _directory_bytes(directory)
    # `None` spec (an unreadable header) is treated as recognition, because
    # that is what this product's models overwhelmingly are and the load is
    # the authority anyway. It carries a note saying the header could not be
    # read, so the guess is visible rather than silent.
    kind = SPEC_KIND.get(spec or WHISPER_SPEC, "unknown")
    tokenizer, package, tokenizer_note = _tokenizer_of(directory)
    codes = _language_codes(directory) if kind == "translation" else []
    notes: list[str] = []

    common = {
        "name": name,
        "path": str(directory),
        "size_bytes": size,
        "layout": layout,
        "spec": spec,
        "spec_revision": revision,
        "binary_version": binary_version,
        "mel_bins": mel_bins,
        "languages": len(codes) if kind == "translation" and codes else languages,
        "kind": kind,
        "kind_label": KIND_LABEL[kind],
        "tokenizer": tokenizer,
        "tokenizer_package": package,
        "language_codes": codes,
    }

    # A CTranslate2 model whose spec we do not recognise is reported, not
    # assigned. It would load and then fail three layers from its cause,
    # which is the worst of both -- and this check CAN see that it does not
    # know, so saying so is the correct output (P-38).
    if kind == "unknown":
        return ModelReport(
            **common,
            state="unknown_kind",
            usable=False,
            summary=f"這是 CTranslate2 模型，但本程式不認得它的用途（{spec}）。",
            notes=[
                "認得的只有兩種：Whisper 語音辨識模型，以及 NLLB／M2M／OPUS-MT 這類翻譯模型。",
            ],
            suggested_name=None,
        )

    if not (directory / MODEL_CONFIG).is_file():
        return ModelReport(
            **common,
            state="incomplete",
            usable=False,
            summary="有 model.bin，但少了 config.json，這份模型不完整。",
            notes=["重新下載一次完整的模型資料夾。"],
            suggested_name=None,
        )

    if not _has_vocabulary(directory, kind):
        wanted = "、".join(_VOCABULARY_FILES[kind][:2])
        return ModelReport(
            **common,
            state="incomplete",
            usable=False,
            summary=f"有 model.bin，但少了字彙檔（{wanted}），這份模型不完整。",
            notes=["重新下載一次完整的模型資料夾。"],
            suggested_name=None,
        )

    # A translation model without a tokenizer is a real defect, and one that
    # would otherwise surface as a runner crash on the first translation.
    # Recognition models are exempt: faster-whisper carries Whisper's own
    # tokenizer inside the package, so the folder is not expected to have one.
    if kind == "translation" and tokenizer is None:
        return ModelReport(
            **common,
            state="incomplete",
            usable=False,
            summary="這是翻譯模型，但資料夾裡沒有斷詞檔，沒辦法用。",
            notes=[
                "翻譯模型需要 tokenizer.json（最省事）或 sentencepiece 的 .model 檔，"
                "從原本的下載頁面把它一起抓下來放進同一個資料夾即可。",
            ],
            suggested_name=None,
        )

    if spec is None:
        notes.append("讀不出模型檔的格式標記，先當成語音辨識模型；載入時才會知道結果。")
    if binary_version is not None and binary_version > KNOWN_BINARY_VERSION:
        notes.append(
            f"這份模型的格式版本是 {binary_version}，比本程式看過的（{KNOWN_BINARY_VERSION}）新；"
            "如果載入失敗，把引擎環境的 CTranslate2 升級即可。"
        )
    if kind == "recognition":
        if languages == 1:
            notes.append("這是「只聽英文」的版本（.en），中文錄音請換多語版本。")
        if mel_bins == 128:
            notes.append("large-v3 世代（128 頻帶）。")
        elif mel_bins == 80:
            notes.append("large-v3 之前的世代（80 頻帶）。")
    else:
        if tokenizer_note:
            notes.append(f"斷詞用 {tokenizer}，{tokenizer_note}。")
        if codes:
            for wanted, label in (("zho_Hant", "繁體中文"), ("zho_Hans", "簡體中文")):
                if wanted in codes:
                    notes.append(f"支援{label}（{wanted}）。")
        else:
            notes.append("讀不出它支援哪些語言，翻譯時要自己填語言代碼。")
    if origin is not None:
        notes.append(f"來自 Hugging Face 快取：{origin.name}")

    return ModelReport(
        **common,
        state="ready",
        usable=True,
        summary=_ready_summary(kind, size, common["languages"]),
        notes=notes,
        suggested_name=_suggested_name(name),
    )


def _tokenizer_of(directory: Path) -> tuple[str | None, str | None, str]:
    """`(filename, pip package needed, note)` for the first tokenizer found.

    Preference order is the COST order, not an accuracy one: `tokenizer.json`
    is read by `tokenizers`, which faster-whisper already installs, so a
    model carrying it needs nothing added to the engine at all. Verified
    2026-08-28 that `OpenNMT/nllb-200-distilled-1.3B-ct2-int8` ships exactly
    that file and does NOT ship a SentencePiece one.
    """
    for filename, package, note in TOKENIZER_FILES:
        if (directory / filename).is_file():
            return filename, (None if package == "tokenizers" else package), note
    return None, None, ""


def _language_codes(directory: Path) -> list[str]:
    """The languages a translation model names, from its special-tokens file.

    `special_tokens_map.json` is a few kilobytes and lists the 200 FLORES
    codes as additional special tokens; `tokenizer.json` also holds them and
    is 17 MB, which is not a file to parse every time a settings panel
    opens. Reading the small one is the whole reason this is cheap enough to
    do on every readiness check.

    An empty list means the model did not say, and that is reported rather
    than filled in with a guess about which languages a model supports.
    """
    for filename in ("special_tokens_map.json", "tokenizer_config.json"):
        data = _read_json(directory / filename)
        extra = data.get("additional_special_tokens")
        if not isinstance(extra, list):
            continue
        codes = [
            token
            for token in extra
            if isinstance(token, str) and _FLORES.match(token)
        ]
        if codes:
            return sorted(codes)
    return []


#: What CTranslate2 writes the vocabulary as, per kind. The two are
#: different files and this used to be one list -- which condemned a
#: perfectly good translation model for "missing" a Whisper file it was
#: never going to have. Found by `test_a_translation_model_needing_
#: sentencepiece_says_so_by_name`, which existed because the input shape
#: (a Marian-style folder) was one nothing had tried (P-42).
_VOCABULARY_FILES: dict[str, tuple[str, ...]] = {
    # `tokenizer.json` for current faster-whisper; the two `vocabulary.*`
    # forms for models somebody converted years ago. Accepting all three
    # keeps this check from being stricter than the engine it predicts.
    "recognition": ("tokenizer.json", "vocabulary.json", "vocabulary.txt"),
    # A sequence-to-sequence model has one shared vocabulary, or a pair.
    "translation": (
        "shared_vocabulary.json",
        "shared_vocabulary.txt",
        "source_vocabulary.json",
        "source_vocabulary.txt",
    ),
}


def _has_vocabulary(directory: Path, kind: str) -> bool:
    return any(
        (directory / candidate).is_file()
        for candidate in _VOCABULARY_FILES.get(kind, ())
    )


def _ready_summary(kind: str, size: int, languages: int | None) -> str:
    if kind == "translation":
        tongue = f"{languages} 種語言" if languages else "語言未知"
        return f"可以使用的翻譯模型（{tongue}，{human_bytes(size)}）。"
    tongue = "多語" if (languages or 0) > 1 else "只有英文"
    return f"可以使用的語音辨識模型（{tongue}，{human_bytes(size)}）。"


def human_bytes(size: int) -> str:
    """Sizes a person reads, not sizes a machine reports.

    Here rather than in the GUI because the CLI prints the same number, and
    two implementations of "3 GB" eventually disagree about what a GB is.
    """
    step = 1024.0
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < step or unit == "TB":
            return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} TB"


# --------------------------------------------------------------------------
# Finding models
# --------------------------------------------------------------------------


def find_models(path: str | Path, *, limit: int = 24) -> list[ModelReport]:
    """Every model at or under `path`, two levels deep at most.

    Two levels is not an arbitrary depth, it is the two layouts: a model
    directly (`<picked>/model.bin`), and a directory OF models -- which is
    what a Hugging Face cache root is, and what a person who keeps several
    models in one folder has. Going deeper would start walking a user's whole
    drive on a mis-click.

    A Hub cache entry counts as ONE level even though its files are two
    further down, because `inspect_model` resolves that on its own.
    """
    root = Path(path).expanduser()
    if not root.is_dir():
        return []

    direct = inspect_model(root)
    if direct.state in ("ready", "unknown_kind", "needs_conversion"):
        return [direct]

    found: list[ModelReport] = []
    for child in _safe_iterdir(root):
        if len(found) >= limit:
            break
        if not child.is_dir() or child.name.startswith("."):
            continue
        report = inspect_model(child)
        if report.state in ("ready", "unknown_kind", "needs_conversion", "incomplete"):
            found.append(report)
    return found


def models_of_kind(reports: Iterable[ModelReport], kind: ModelKind) -> list[ModelReport]:
    """The usable ones that serve `kind`.

    A one-line helper with a reason: `usable` alone was the filter
    everywhere until translation arrived, and every one of those places
    would otherwise have quietly started offering a translation model to the
    recogniser. Naming the pair makes the second half impossible to forget.
    """
    return [report for report in reports if report.usable and report.kind == kind]


def installed_models(home: Path) -> list[ModelReport]:
    """What is in the model home right now, usable ones first.

    Unusable entries are listed rather than hidden: a folder that looks like
    a model and is not is exactly the thing a person needs to be told about,
    and silently skipping it produces "I put it there and nothing happened".
    """
    if not home.is_dir():
        return []
    reports = [
        inspect_model(child)
        for child in _safe_iterdir(home)
        if child.is_dir() and not child.name.startswith(".")
    ]
    kept = [r for r in reports if r.state != "not_a_model"]
    return sorted(kept, key=lambda r: (not r.usable, r.name.casefold()))


def find_installed(
    home: Path, name: str, *, kind: ModelKind = "recognition"
) -> ModelReport | None:
    """The installed model a configured `name` refers to, or `None`.

    Exact folder match first, then the normalised one, so a config that still
    says `large-v3` -- the value every existing install has -- finds a folder
    called `faster-whisper-large-v3` instead of quietly going back to the Hub
    for weights that are already on the disk.

    `kind` is not optional in spirit, only in signature: a name that matches
    a model of the WRONG kind must not answer, or the recogniser is handed a
    translation model and fails several layers from the cause. It defaults to
    `recognition` because that is what every caller wanted before there was a
    second kind, and a default that changes behaviour silently would be worse
    than a keyword nobody forgets.
    """
    if not name or not home.is_dir():
        return None

    exact = home / name
    if exact.is_dir():
        report = inspect_model(exact)
        if report.usable and report.kind == kind:
            return report

    wanted = normalize_model_name(name)
    for report in models_of_kind(installed_models(home), kind):
        if normalize_model_name(report.name) == wanted:
            return report
    return None


def resolve_model_argument(asr_config, output_root: str | Path) -> tuple[str, str | None]:
    """`(model, model_dir)` as `asr.recognize` wants them.

    A local model becomes an absolute PATH, which faster-whisper loads
    directly and which cannot be mistaken for an instruction to download
    something. Only when no local model answers to the configured name does
    the name itself go through -- preserving exactly the behaviour every
    build before this one had, including the download that `allow_download`
    still gates.
    """
    home = model_home(asr_config, output_root)
    found = find_installed(home, getattr(asr_config, "model", "") or "", kind="recognition")
    if found is not None:
        return found.path, None
    return getattr(asr_config, "model", "large-v3"), str(home)


def resolve_translation_model(asr_config, output_root: str | Path) -> ModelReport | None:
    """The translation model to use, or `None`.

    No fallback to a name and no download: translation has no equivalent of
    faster-whisper's Hub resolution, so "not here" is the whole answer.
    Returning `None` rather than raising because the CALLER is what knows
    whether this was a translation request (an error) or a panel asking what
    is available (a fact).
    """
    home = model_home(asr_config, output_root)
    configured = getattr(asr_config, "translation_model", None) or ""
    if not configured:
        return None
    return find_installed(home, configured, kind="translation")


# --------------------------------------------------------------------------
# Putting a model into the home
# --------------------------------------------------------------------------


def link_probe(home: Path) -> tuple[bool, str]:
    """Can a directory junction be created in `home`? Answered by doing it.

    Junctions need NTFS on the link's side and refuse to point at a network
    location; both facts are knowable in principle and neither is knowable
    from a path string. Creating one and removing it is the positive control
    -- a capability check that never actually exercises the capability is how
    a greyed-out button ends up lying in both directions.

    The probe link is removed with `rmdir`, never `rmtree`: removing a
    junction must not walk into what it points at.
    """
    if os.name != "nt":
        return False, "捷徑（junction）只在 Windows 上提供。"

    target = home if home.is_dir() else _nearest_existing(home)
    source = target / ".mfp-link-probe-target"
    link = target / ".mfp-link-probe"
    try:
        source.mkdir(exist_ok=True)
    except OSError as exc:
        return False, f"無法在模型資料夾裡建立測試檔：{exc}"

    try:
        _create_junction(link, source)
    except OSError as exc:
        _remove_dir_quietly(source)
        return False, f"這個磁碟不支援捷徑（{exc}）。請改用複製或搬移。"
    finally:
        pass

    _remove_link_quietly(link)
    _remove_dir_quietly(source)
    return True, "可以建立捷徑。"


def _create_junction(link: Path, target: Path) -> None:
    """A directory junction, which needs no administrator rights.

    Deliberately NOT `os.symlink`: a directory symlink on Windows requires
    either an elevated process or Developer Mode, so it fails on exactly the
    ordinary machine this option exists for. A junction is the one link an
    unprivileged user can make, and it is enough -- faster-whisper opens the
    files through it without knowing it is there.
    """
    import _winapi

    _winapi.CreateJunction(str(Path(target).resolve()), str(link))


def _is_link(path: Path) -> bool:
    return path.is_symlink() or os.path.isjunction(path)


def _remove_link_quietly(link: Path) -> None:
    try:
        if _is_link(link):
            os.rmdir(link)
    except OSError:
        pass


def _remove_dir_quietly(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        pass


#: The models this product will fetch for you, and nothing else.
#:
#: A short, curated list rather than a search box over the Hub. Two reasons,
#: and neither is laziness: a wrong repo id here costs a multi-gigabyte
#: download that ends in 「這不是可以用的模型」, and every entry below was
#: checked against the Hub for existence, file list and size on 2026-08-28
#: rather than written from memory. The first guess for the translation model
#: did not exist at all.
#:
#: Both organisations are the authoritative ones: `Systran` is faster-
#: whisper's own, and `OpenNMT` is CTranslate2's. The NLLB entry matches the
#: folder this project has been running against, file for file.
#:
#: `bytes` is a DISPLAY figure, refreshed from the Hub before any transfer
#: starts -- it exists so the panel can say "about 3 GB" before the user has
#: committed to anything, not as a number anything depends on.
CATALOGUE: tuple[dict, ...] = (
    {
        "id": "faster-whisper-large-v3",
        "repo": "Systran/faster-whisper-large-v3",
        "kind": "recognition",
        "label": "large-v3",
        "detail": "最準，也最慢。有獨立顯示卡就選這個。",
        "bytes": 3_090_000_000,
        "recommended": True,
    },
    {
        "id": "faster-whisper-medium",
        "repo": "Systran/faster-whisper-medium",
        "kind": "recognition",
        "label": "medium",
        "detail": "準度和速度的折衷。顯示卡記憶體不夠或想快一點時用。",
        "bytes": 1_530_000_000,
        "recommended": False,
    },
    {
        "id": "faster-whisper-small",
        "repo": "Systran/faster-whisper-small",
        "kind": "recognition",
        "label": "small",
        "detail": "最小最快，準度明顯下降。用純 CPU 的機器才建議。",
        "bytes": 486_000_000,
        "recommended": False,
    },
    {
        "id": "nllb-200-distilled-1.3B-ct2-int8",
        "repo": "OpenNMT/nllb-200-distilled-1.3B-ct2-int8",
        "kind": "translation",
        "label": "NLLB-200 1.3B",
        "detail": "把逐字稿翻成別的語言時才需要。不影響聽寫。",
        "bytes": 1_400_000_000,
        "recommended": True,
    },
)


def catalogue_entry(model_id: str) -> dict | None:
    return next((e for e in CATALOGUE if e["id"] == model_id), None)


def download_model(
    model_id: str,
    home: Path,
    *,
    python_exe: Path,
    fetcher: Path,
    on_progress: Callable[[dict], None] | None = None,
    say: Callable[[str], None] | None = None,
) -> ModelReport:
    """Fetch one catalogue entry into `home`, and prove it arrived.

    The same shape as `install_model` and for the same reason: a download
    that reports success on a folder which is not a loadable model is the
    failure this module exists to prevent, so the last thing that happens is
    `inspect_model` looking at what actually landed.

    The transfer itself runs in the ENGINE's interpreter (`asr/fetch_model.py`
    explains why). This function owns everything on THIS side of that
    boundary: where it goes, whether it may go there, and whether what came
    back is a model.
    """
    from mfp import asr  # local: `asr` imports helpers from this module

    entry = catalogue_entry(model_id)
    if entry is None:
        raise UsageError(f"沒有這個可下載的模型：「{model_id}」。")
    say = say or (lambda _m: None)

    destination = home / entry["id"]
    if destination.exists():
        raise UsageError(
            f"模型資料夾裡已經有一個叫「{entry['id']}」的項目了。"
            f"要重新下載請先把舊的移走。"
        )
    home.mkdir(parents=True, exist_ok=True)
    if not _writable(home):
        raise MfpError(
            f"沒有權限寫入模型資料夾：{home}。"
            f"請在設定裡換一個位置，或用系統管理員身分執行。"
        )
    # Asked BEFORE the transfer rather than discovered at 90%. The margin is
    # for the partial files the hub writes beside the finished ones.
    free = _free_bytes(home)
    needed = int(entry["bytes"] * 1.15)
    if free is not None and free < needed:
        raise UsageError(
            f"這個磁碟剩下 {human_bytes(free)}，"
            f"這個模型大約需要 {human_bytes(needed)}（含下載暫存）。"
            f"請先清出空間，或在設定裡把模型資料夾換到別的磁碟。"
        )

    if not fetcher.is_file():
        raise MfpError(f"the packaged model downloader is missing at {fetcher}")

    payload = asr.run_sidecar(
        [str(python_exe), str(fetcher), entry["repo"], "--dest", str(destination)],
        say=say,
        on_progress=on_progress,
    )
    if not payload.get("ok"):
        _remove_tree_quietly(destination)
        raise MfpError(payload.get("error") or "下載失敗，而且沒有說明原因。")

    landed = inspect_model(destination)
    if not landed.usable:
        # Ours to undo: this function created the folder, so removing it on a
        # verification failure cannot destroy anything that was already
        # there. The same reasoning `install_model` applies to `copy`.
        _remove_tree_quietly(destination)
        raise MfpError(
            f"下載完成之後檢查沒有過：{landed.summary} 已把下載的內容清掉。"
        )
    return landed


def _free_bytes(directory: Path) -> int | None:
    try:
        return shutil.disk_usage(_nearest_existing(directory)).free
    except OSError:
        return None


def install_model(
    source: str | Path,
    home: Path,
    *,
    mode: InstallMode,
    name: str | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> ModelReport:
    """Put the model at `source` into `home`, and prove it arrived.

    The three modes are the user's, not ours (2026-08-28): copy leaves the
    original alone, move frees the disk, link costs nothing at all. Each has
    a consequence the caller is expected to have said out loud already --
    move in particular BREAKS whatever pointed at the original, which for a
    Hugging Face cache means the other program that downloaded it.

    Every mode ends with the destination being re-inspected. An install that
    reports success on a directory that is not a loadable model is the exact
    failure this module exists to prevent, and the only way to not have it is
    to look afterwards.
    """
    report = inspect_model(source)
    if not report.usable:
        raise UsageError(f"{report.summary} {' '.join(report.notes)}".strip())

    # `report.path` and not `source`: for a Hub cache the model is the
    # snapshot directory, and copying the wrapper would copy `blobs/` too --
    # on a symlinked cache that is the same 3 GB a second time.
    origin = Path(report.path)
    folder = _suggested_name(name or report.suggested_name or report.name)
    destination = home / folder

    if destination.exists():
        raise UsageError(
            f"模型資料夾裡已經有一個叫「{folder}」的項目了。"
            f"請換一個名稱，或先把舊的移走。"
        )
    try:
        if origin.resolve() == destination.resolve():
            raise UsageError("來源和目的地是同一個資料夾。")
    except OSError:
        pass

    home.mkdir(parents=True, exist_ok=True)
    if not _writable(home):
        raise MfpError(
            f"沒有權限寫入模型資料夾：{home}。"
            f"請在設定裡換一個位置，或用系統管理員身分執行。"
        )

    if mode == "link":
        _install_link(origin, destination)
    elif mode == "move":
        _install_move(origin, destination, report.size_bytes, on_progress)
    elif mode == "copy":
        _install_copy(origin, destination, report.size_bytes, on_progress)
    else:  # pragma: no cover - the API layer validates first
        raise UsageError(f"不認得的安裝方式：{mode}")

    landed = inspect_model(destination)
    if not landed.usable:
        # Only a COPY is ours to undo. A move already consumed the original
        # and a link is a pointer; deleting either on a verification failure
        # would turn a recoverable state into a lost one.
        if mode == "copy":
            _remove_tree_quietly(destination)
        raise MfpError(
            f"模型放進去之後檢查沒有過：{landed.summary} 已保留現場供檢查。"
            if mode != "copy"
            else f"模型複製過去之後檢查沒有過：{landed.summary} 已把複製的內容清掉。"
        )
    return landed


def _install_link(origin: Path, destination: Path) -> None:
    try:
        _create_junction(destination, origin)
    except OSError as exc:
        raise MfpError(
            f"建立捷徑失敗：{exc}。這個磁碟可能不支援，改用「複製」即可。"
        ) from exc


def _install_move(
    origin: Path,
    destination: Path,
    total: int,
    on_progress: Callable[[dict], None] | None,
) -> None:
    """Rename when it can, copy-then-delete when it cannot.

    `os.replace` across volumes raises rather than doing the slow thing
    silently, which is what makes the two paths distinguishable -- and the
    slow one needs the progress reporting that the instant one has no use
    for.
    """
    try:
        os.replace(origin, destination)
        _emit(on_progress, phase="moved", copied=total, total=total)
        return
    except OSError:
        pass
    _install_copy(origin, destination, total, on_progress)
    _remove_tree_quietly(origin)


def _install_copy(
    origin: Path,
    destination: Path,
    total: int,
    on_progress: Callable[[dict], None] | None,
) -> None:
    """Copy with a byte counter, because 3 GB is not an instant.

    Written out rather than `shutil.copytree` for that one reason: copytree
    reports nothing until it is finished, and a 3 GB copy that says nothing
    for two minutes is the same silence this whole feature exists to remove
    from the transcription path.
    """
    destination.mkdir(parents=True, exist_ok=False)
    copied = 0
    _emit(on_progress, phase="copy", copied=0, total=total)
    try:
        for entry in sorted(origin.rglob("*")):
            relative = entry.relative_to(origin)
            target = destination / relative
            if entry.is_dir() and not entry.is_symlink():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            copied += _copy_file(entry, target, copied, total, on_progress)
    except OSError as exc:
        _remove_tree_quietly(destination)
        raise MfpError(f"複製模型失敗：{exc}") from exc
    _emit(on_progress, phase="copy", copied=total, total=total)


#: 8 MiB. Large enough that a 3 GB copy is not 400,000 progress records,
#: small enough that the counter moves several times a second on a slow disk.
_CHUNK = 8 * 1024 * 1024


def _copy_file(
    source: Path,
    target: Path,
    already: int,
    total: int,
    on_progress: Callable[[dict], None] | None,
) -> int:
    written = 0
    with source.open("rb") as reader, target.open("wb") as writer:
        while True:
            chunk = reader.read(_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
            written += len(chunk)
            _emit(
                on_progress,
                phase="copy",
                copied=already + written,
                total=total,
                file=source.name,
            )
    shutil.copystat(source, target, follow_symlinks=False)
    return written


def _emit(on_progress: Callable[[dict], None] | None, **fields: object) -> None:
    if on_progress is not None:
        on_progress(dict(fields))


def _remove_tree_quietly(directory: Path) -> None:
    """Delete a directory WE created, never one we linked to.

    The guard is the point. `shutil.rmtree` walking into a junction would
    delete the user's original model out of the folder it still lives in,
    which is the single worst thing this module could do.
    """
    try:
        if _is_link(directory):
            os.rmdir(directory)
            return
        shutil.rmtree(directory, ignore_errors=True)
    except OSError:
        pass


def install_preflight(
    source: str | Path,
    home: Path,
    *,
    mode: InstallMode,
) -> list[str]:
    """What the user should be told BEFORE the button is pressed.

    Returns human sentences, in Traditional Chinese, and an empty list when
    there is nothing to warn about. Separate from `install_model` because a
    warning that arrives with the result is not a warning -- it is a report
    on something already done.
    """
    report = inspect_model(source)
    warnings: list[str] = []
    if not report.usable:
        return [report.summary, *report.notes]

    if mode in ("copy", "move"):
        try:
            free = shutil.disk_usage(_nearest_existing(home)).free
        except OSError:
            free = None
        if free is not None and free < report.size_bytes * 1.05:
            warnings.append(
                f"空間可能不夠：這份模型 {human_bytes(report.size_bytes)}，"
                f"目的地只剩 {human_bytes(free)}。"
            )

    if mode == "move":
        warnings.append("搬移之後，原本的位置就沒有這份模型了。")
        if report.layout == "hub":
            warnings.append(
                "這份模型來自其他程式的下載快取；搬走之後那個程式會找不到它，"
                "下次可能會重新下載一次。"
            )
    if mode == "link":
        warnings.append(
            "捷徑不會佔用額外空間，但原本的資料夾不能刪除或改名，否則辨識會失敗。"
        )
    return warnings


def describe_modes(home: Path, source: str | Path | None = None) -> list[dict]:
    """The three choices, each with its consequence and whether it is open.

    Built here rather than in the GUI because whether linking works is a
    property of the disk, not of the interface, and the interface must not
    be the thing that decides to offer an option that cannot work.
    """
    can_link, link_reason = link_probe(home)
    return [
        {
            "mode": "copy",
            "label": "複製一份過來",
            "detail": "原本的位置保持不動，這裡多一份。最安全，也最佔空間。",
            "available": True,
            "reason": None,
            "warnings": install_preflight(source, home, mode="copy") if source else [],
        },
        {
            "mode": "move",
            "label": "整個搬過來",
            "detail": "原本的位置不再有這份模型。不佔額外空間。",
            "available": True,
            "reason": None,
            "warnings": install_preflight(source, home, mode="move") if source else [],
        },
        {
            "mode": "link",
            "label": "建立捷徑（不搬也不複製）",
            "detail": "模型留在原地，這裡只放一個指過去的捷徑。完全不佔空間。",
            "available": can_link,
            "reason": None if can_link else link_reason,
            "warnings": install_preflight(source, home, mode="link") if source else [],
        },
    ]


def uninstall_entry(home: Path, name: str) -> str:
    """Remove ONE entry from the home, and only when it is a link.

    A real model directory is several gigabytes the user chose to put here,
    and deleting it from a settings panel is a click away from a mistake with
    no undo -- so the panel opens the folder instead and lets Explorer, which
    has a recycle bin, do it. A junction is the opposite: it holds nothing,
    and leaving a dead one behind is itself the defect.
    """
    target = home / name
    if not target.exists():
        raise UsageError(f"模型資料夾裡沒有「{name}」。")
    if not _is_link(target):
        raise UsageError(
            "這是實際存放的模型資料夾，不從這裡刪除。"
            "請按「開啟資料夾」自己刪，才有回收桶可以救。"
        )
    os.rmdir(target)
    return str(target)


def iter_home_names(home: Path) -> Iterable[str]:
    return (child.name for child in _safe_iterdir(home) if child.is_dir())


# --------------------------------------------------------------------------
# One answer to "can I use this yet"
# --------------------------------------------------------------------------


class SetupStep(CamelModel):
    """One thing left to do, in words plus a button the GUI can render.

    `action` is a NAME, never a path or a command. The renderer owns what a
    button does; this side owns what still has to happen, and keeping the
    two apart is what stops the settings panel from growing a second copy of
    the setup logic.
    """

    text: str
    #: "pick-engine" | "pick-model" | "choose-installed" | "guide" | None
    #:
    #: `pick-model` browses for a folder that is NOT here yet;
    #: `choose-installed` picks among the ones that already are. They were
    #: one name for a while, and the button that came out of it offered a
    #: 「瀏覽新資料夾」 dialog to somebody the sentence had just told to pick
    #: from a list. `open-home` was in this list and emitted by nothing.
    action: str | None = None


class EngineStatus(CamelModel):
    """The interpreter half. Absent is the NORMAL state, not a fault."""

    present: bool
    path: str | None = None
    version: str | None = None
    #: "environment" | "settings" | "beside-the-app" | None
    source: str | None = None
    #: A sentence when something is wrong with a named interpreter.
    problem: str | None = None


class HomeStatus(CamelModel):
    path: str
    exists: bool
    writable: bool
    free_bytes: int | None = None
    #: True when the path came from the settings rather than from the
    #: install-shape default, so the panel can say which it is showing.
    configured: bool = False


#: `model_unusable` used to be here and was produced by no branch: a folder
#: that cannot be loaded is not a capability state, it is a `ModelReport`
#: with `usable=False`, and the model list is where that gets said. A state
#: nothing can reach is a state nothing can be tested against.
ReadinessState = Literal[
    "ready",
    "no_engine",
    "engine_broken",
    #: Nothing of the right KIND is selected. `available` splits the two
    #: sentences this covers: 0 means "go and get one", more means "pick one".
    "no_model",
]


class CapabilityStatus(CamelModel):
    """One thing this machine can or cannot do, and what is missing for it.

    The type that answers the actual complaint behind this whole surface:
    a person who does not know what a model IS still has to be able to look
    at a list and see which of the things they wanted works, which does not,
    and what KIND of file the broken one is waiting for.

    So every field here is written for that reader. `label` is what the
    capability is called, `what` is what it does in one clause, `needs_kind`
    and `needs_label` say what sort of model it wants, and `active` is the
    one it is using. Nothing here requires knowing the word "CTranslate2".
    """

    #: "recognition" | "translation"
    id: str
    label: str
    #: One clause: 把聲音變成文字.
    what: str
    ready: bool
    state: ReadinessState
    headline: str
    detail: str
    steps: list[SetupStep] = []
    #: The model in use, when there is one.
    active: ModelReport | None = None
    #: What the settings name, so a selection that matches nothing can be
    #: shown as exactly that rather than as an absence.
    configured: str = ""
    #: The KIND of model this capability needs, and its screen label. The
    #: pair is what lets the panel say 「還缺一個翻譯模型」 rather than
    #: 「還缺一個模型」 -- which, with two kinds installed, is not an
    #: instruction anybody can follow.
    needs_kind: ModelKind = "recognition"
    needs_label: str = ""
    #: How many usable models of that kind are sitting in the home. Zero
    #: with a `no_model` state means "go and get one"; more than zero means
    #: "pick one", and those are different sentences.
    available: int = 0
    #: True when the product works fine without this capability, so a
    #: renderer can tell 「壞了」 from 「沒開通」. Required rather than
    #: defaulted, because the default was `True` for both and read by
    #: nobody -- a field that documents a guarantee while every caller
    #: silently accepts the wrong value is worse than no field. Recognition
    #: is False (`AsrReadiness.ready` is hoisted from it alone); translation
    #: is True (D-98: a machine with no translation model has not opted in,
    #: it is not broken).
    optional: bool


class AsrReadiness(CamelModel):
    """Everything the setup surface needs, and nothing it has to interpret.

    This type exists because the same question was previously answered in
    four incompatible dialects: `doctor` printed `[SKIP] asr: unknown (not
    found)`, the CLI raised prose naming `asr.python`, the GUI rendered a
    red line about `config.json`, and the settings panel showed a row
    labelled `asr` with no label at all. None of the four told a person what
    to do next.

    `capabilities` is the list; `ready` is the one boolean about
    RECOGNITION, hoisted because `doctor` and the 逐字稿 card each need
    exactly that and should not have to search a list for it.
    """

    #: Recognition only. Named narrowly on purpose: translation is opt-in and
    #: a machine with no translation model is not "not ready", it is a
    #: machine that has not opted in.
    ready: bool
    #: Every capability this surface governs, in the order a person meets
    #: them: you transcribe first, and only then is there something to
    #: translate.
    capabilities: list[CapabilityStatus] = []
    engine: EngineStatus
    home: HomeStatus
    #: Everything in the home, usable or not, of either kind. The list the
    #: panel renders with a type badge per row.
    models: list[ModelReport] = []
    allow_download: bool = False
    #: What is done to the sound before it is recognised: `none`, `level` or
    #: `denoise`. Reported so the panel can show the current choice, and
    #: because a transcript that came out badly is worth being able to
    #: explain by what it was run through.
    audio: str = "none"
    #: Whether a junction can be made in the home, so the third install mode
    #: is offered only when it can work.
    can_link: bool = False
    link_detail: str = ""

    def capability(self, capability_id: str) -> CapabilityStatus | None:
        """One entry by id. A method rather than a dict on the wire, because
        the ORDER of the list is meaningful to the reader and a mapping
        would have thrown it away."""
        for entry in self.capabilities:
            if entry.id == capability_id:
                return entry
        return None


def probe_engine(interpreter: Path, runner=None) -> tuple[str | None, str | None]:
    """`(version, problem)` for an interpreter we have already located.

    Asked of the package rather than inferred from the path, for the reason
    `doctor` gives: a venv is free to hold a different faster-whisper than
    the one somebody remembers installing.
    """
    import subprocess

    argv = [str(interpreter), "-c", "import faster_whisper as f; print(f.__version__)"]
    try:
        completed = (runner or _default_probe)(argv)
    except OSError as exc:
        return None, f"找不到或無法執行這個 Python：{exc}"
    except subprocess.TimeoutExpired:
        return None, "這個 Python 在時限內沒有回應。"
    if completed.returncode != 0:
        return None, "這個 Python 裡沒有安裝語音辨識引擎（faster-whisper）。"
    first = (completed.stdout or "").strip().splitlines()
    return (first[0].strip() if first else None), None


def _default_probe(argv: list[str]):
    import subprocess

    return subprocess.run(
        argv,
        capture_output=True,
        # DEVNULL for the reason every other spawn site in this project uses
        # it: the packaged sidecar has a thread blocked on its own stdin, and
        # a child that inherits that handle hangs for the full timeout.
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=20.0,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def readiness(asr_config, output_root: str | Path, *, runner=None) -> AsrReadiness:
    """The whole setup question, answered once, for every surface.

    Order matters and is the order a person would fix things in: without an
    engine the model is irrelevant, and with an engine but no model the
    engine is fine and the instruction is about weights. Reporting both
    problems at once produces the wall of text this replaces.
    """
    from mfp import asr  # local: `asr` may import this module's helpers

    configured_home = bool(getattr(asr_config, "model_dir", None))
    home = model_home(asr_config, output_root)
    status = home_status(home)
    home_report = HomeStatus(
        path=status["path"],
        exists=status["exists"],
        writable=status["writable"],
        free_bytes=status["freeBytes"],
        configured=configured_home,
    )

    interpreter, source, problem = asr.find_runtime_detailed(
        getattr(asr_config, "python", None)
    )
    version = None
    if interpreter is not None:
        version, problem = probe_engine(interpreter, runner)
    engine = EngineStatus(
        present=interpreter is not None and problem is None,
        path=str(interpreter) if interpreter is not None else None,
        version=version,
        source=source,
        problem=problem,
    )

    models = installed_models(home)
    allow_download = bool(getattr(asr_config, "allow_download", False))
    can_link, link_detail = link_probe(home) if home_report.writable else (False, "")

    recognition = _recognition_status(asr_config, home, models, engine, allow_download)
    translation = _translation_status(asr_config, home, models, engine)

    return AsrReadiness(
        ready=recognition.ready,
        capabilities=[recognition, translation],
        engine=engine,
        home=home_report,
        models=models,
        allow_download=allow_download,
        audio=str(getattr(asr_config, "audio", "none") or "none"),
        can_link=can_link,
        link_detail=link_detail,
    )


def _no_engine_steps() -> list[SetupStep]:
    return [
        SetupStep(text="指定一個裝好 faster-whisper 的 Python", action="pick-engine"),
        SetupStep(text="還沒有的話，看「怎麼準備語音辨識」", action="guide"),
    ]


def _recognition_status(
    asr_config,
    home: Path,
    models: list[ModelReport],
    engine: EngineStatus,
    allow_download: bool,
) -> CapabilityStatus:
    """Can this machine turn sound into words.

    The order of the checks is the order a person would fix things in:
    without an engine the model is irrelevant, and with an engine but no
    model the engine is fine and the instruction is about weights. Reporting
    both at once produces the wall of text this surface replaced.
    """
    configured = getattr(asr_config, "model", "") or ""
    active = find_installed(home, configured, kind="recognition")
    available = models_of_kind(models, "recognition")

    common = {
        "id": "recognition",
        "label": "語音辨識",
        "what": KIND_WHAT["recognition"],
        "active": active,
        "configured": configured,
        "needs_kind": "recognition",
        "needs_label": KIND_LABEL["recognition"],
        "available": len(available),
        "optional": False,
    }

    if engine.path is None:
        return CapabilityStatus(
            **common,
            ready=False,
            state="no_engine",
            headline="還不能聽寫",
            detail=(
                engine.problem
                or "還沒有指定語音辨識引擎。引擎是分開安裝的，因為它和模型加起來有好幾 GB，"
                "放進安裝檔會讓程式從 100 MB 變成好幾 GB。"
            ),
            steps=_no_engine_steps(),
        )

    if engine.problem is not None:
        return CapabilityStatus(
            **common,
            ready=False,
            state="engine_broken",
            headline="還不能聽寫",
            detail=engine.problem,
            steps=[
                SetupStep(text="換一個裝好 faster-whisper 的 Python", action="pick-engine"),
                SetupStep(text="看怎麼把引擎裝起來", action="guide"),
            ],
        )

    if active is not None:
        return CapabilityStatus(
            **common,
            ready=True,
            state="ready",
            headline="可以聽寫",
            detail=f"引擎與辨識模型都就緒，正在使用「{active.name}」。",
        )

    if available:
        return CapabilityStatus(
            **common,
            ready=False,
            state="no_model",
            headline="還不能聽寫",
            detail=(
                f"引擎已就緒，但設定裡指定的辨識模型「{configured}」不在模型資料夾裡。"
                f"資料夾裡有 {len(available)} 個可以用的辨識模型，選一個即可。"
            ),
            steps=[
                SetupStep(text="選一個已經在資料夾裡的辨識模型", action="choose-installed")
            ],
        )

    if allow_download:
        return CapabilityStatus(
            **common,
            ready=True,
            state="ready",
            headline="可以聽寫（第一次會先下載模型）",
            detail=(
                f"引擎已就緒，資料夾裡還沒有辨識模型。設定允許自動下載，"
                f"所以第一次聽寫會先下載「{configured}」，大約 3 GB。"
            ),
            steps=[SetupStep(text="想先下載好再用，可以自己抓一份放進來", action="guide")],
        )

    return CapabilityStatus(
        **common,
        ready=False,
        state="no_model",
        headline="還不能聽寫",
        detail="引擎已就緒，還差一個辨識模型。模型是一個資料夾，可以自己下載後加進來。",
        steps=[
            SetupStep(text="加入一個已經下載好的辨識模型", action="pick-model"),
            SetupStep(text="不知道去哪裡下載？看「怎麼取得模型」", action="guide"),
        ],
    )


def _translation_status(
    asr_config,
    home: Path,
    models: list[ModelReport],
    engine: EngineStatus,
) -> CapabilityStatus:
    """Can this machine turn a finished transcript into another language.

    Reported SEPARATELY and never folded into the recognition answer (user
    ruling 2026-08-28): translating is a second thing a person asks for
    after they already have a transcript, and a machine with no translation
    model is not broken -- it has not opted in. A single "ready" covering
    both would make every transcription-only setup look incomplete.

    It shares the interpreter, because CTranslate2 is what faster-whisper
    runs on and is therefore already installed wherever recognition works.
    That is the whole reason this capability costs a model and nothing else.
    """
    configured = getattr(asr_config, "translation_model", None) or ""
    active = find_installed(home, configured, kind="translation") if configured else None
    available = models_of_kind(models, "translation")

    common = {
        "id": "translation",
        "label": "翻譯",
        "what": KIND_WHAT["translation"],
        "active": active,
        "configured": configured,
        "needs_kind": "translation",
        "needs_label": KIND_LABEL["translation"],
        "available": len(available),
        "optional": True,
    }

    if engine.path is None or engine.problem is not None:
        return CapabilityStatus(
            **common,
            ready=False,
            state="no_engine" if engine.path is None else "engine_broken",
            headline="還不能翻譯",
            detail=(
                "翻譯跟語音辨識用同一個引擎環境，所以要先把上面那個引擎設定好。"
            ),
            steps=_no_engine_steps(),
        )

    if active is not None:
        languages = f"，支援 {len(active.language_codes)} 種語言" if active.language_codes else ""
        return CapabilityStatus(
            **common,
            ready=True,
            state="ready",
            headline="可以翻譯",
            detail=f"正在使用翻譯模型「{active.name}」{languages}。",
        )

    if available:
        return CapabilityStatus(
            **common,
            ready=False,
            state="no_model",
            headline="還不能翻譯",
            detail=(
                f"資料夾裡有 {len(available)} 個可以用的翻譯模型，但還沒有指定要用哪一個。"
            ),
            steps=[SetupStep(text="選一個翻譯模型", action="choose-installed")],
        )

    return CapabilityStatus(
        **common,
        ready=False,
        state="no_model",
        headline="還沒有翻譯功能",
        detail=(
            "翻譯需要另外一種模型——翻譯模型，跟語音辨識用的不是同一個。"
            "沒有它也不影響下載和逐字稿。"
        ),
        steps=[
            SetupStep(text="加入一個下載好的翻譯模型", action="pick-model"),
            SetupStep(text="不知道去哪裡下載？看「怎麼取得模型」", action="guide"),
        ],
    )
