"""Where yt-dlp and ffmpeg come from, and how to get them without asking the
user to find them.

The first barrier a new user hits is not this product at all: it is two
programs it does not ship. `doctor` has always been able to SAY they are
missing; nothing has ever been able to do anything about it. This module is
the "do something about it" half.

Three rules shape it.

**A managed copy is a copy this program put there, and nothing else.**
`%APPDATA%/media-fetch-pipeline/tools/` holds only files this module wrote,
each recorded in `installed.json` with the URL and the digest it came from.
Nothing is ever installed without the user pressing something -- a silent
100 MB transfer is indistinguishable from a hang, which is the same reason
`allowDownload` is off by default for models.

**Resolution order is configured -> managed -> PATH, and the winner is
reported.** The managed copy wins over PATH because it only exists if
somebody asked for it, so it is the most recent explicit act; an old yt-dlp
on PATH must not shadow the fresh one the user just fetched to fix exactly
that. Which one won is part of every status this module returns, because a
panel that says "已安裝" without saying WHICH copy answers the question
「我更新了 PATH 上那個，為什麼還是舊的」 with silence.

**Nothing is trusted because it came over HTTPS.** Every hop of every
redirect is checked against a host allowlist before the request leaves, and
every payload is compared against a digest the publisher published
separately. A download that cannot be verified is not installed. This is
the one place in the product that fetches an executable and then RUNS it,
so it is the one place where "probably fine" is not good enough.

What is deliberately NOT here: Chrome (we cannot install it, so the UI
links to it).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Sequence

from mfp.config import BinariesConfig, app_data_dir
from mfp.errors import MfpError, UsageError
from mfp.models import CamelModel

__all__ = [
    "MANAGED",
    "SOURCES",
    "ToolStatus",
    "install",
    "managed_dir",
    "resolve",
    "resolution",
    "set_overrides",
    "statuses",
]


#: Every host this module may talk to, and the only ones. A suffix entry
#: (leading dot) matches subdomains: GitHub redirects release downloads to a
#: rotating `*.githubusercontent.com` name, so pinning the exact host would
#: break the download the next time they rename it.
ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "github.com",
        "api.github.com",
        ".githubusercontent.com",
        "www.gyan.dev",
    }
)

_MAX_REDIRECTS = 6
_CHUNK = 1024 * 256
#: A payload larger than this is refused rather than written. ffmpeg is
#: ~106 MB and yt-dlp ~18 MB; half a gigabyte means the URL is no longer
#: pointing at what this table thinks it is.
_MAX_PAYLOAD_BYTES = 512 * 1024 * 1024

ProgressFn = Callable[[dict], None]


class ToolInstallError(MfpError):
    """A managed install could not be completed.

    Its own code rather than `MfpError`: the GUI shows a retry button and a
    manual download link for exactly this, and it must not offer them for
    an arbitrary internal failure.
    """

    error_code = "tool_install_failed"


@dataclass(frozen=True)
class Payload:
    """One way of getting one tool, and how to check what arrived.

    `checksum_url` is a separately-published file, never a value baked in
    here: a digest in this source file would pin a version that goes stale
    in a week, which is the whole reason these tools are not bundled.
    """

    #: Human-facing, Traditional Chinese: where these bytes come from.
    origin: str
    #: Resolves to (payload_url, sha256_hex, version_or_none).
    resolve: Callable[["_Fetcher"], tuple[str, str, str | None]]
    kind: Literal["exe", "zip"]


@dataclass(frozen=True)
class ManagedTool:
    """A tool this program can fetch, unpack and wire up by itself."""

    name: str
    #: The file names it puts in the managed directory. The FIRST one is the
    #: tool's own executable; the rest are companions that arrive with it
    #: (ffprobe rides in ffmpeg's zip and is useless without it).
    files: tuple[str, ...]
    #: What to run to make it say its version, and where a human goes to get
    #: it by hand when every automatic route has failed.
    version_argv_tail: tuple[str, ...]
    homepage: str
    #: Roughly how big the transfer is, so the UI can warn before starting.
    approx_bytes: int
    payloads: tuple[Payload, ...]


#: Every value `ToolStatus.source` can carry. One list, because there were
#: three prose copies of it and one of them was already short: the GUI's
#: `describeSource` never learned `system`, so Chrome -- the only tool that
#: reports it -- rendered as 「已安裝」, which is the exact sentence D-151
#: says may not stand alone. `test_wire_enums.py` reads this tuple and checks
#: every renderer against it, so the next value cannot be added in one place
#: only.
#:
#: `system` is not a fourth rung of the resolution order. It is what
#: `_chrome_status` answers, because Chrome is found by reading the registry
#: rather than by resolving a path, and nothing here can install it.
SOURCES: tuple[str, ...] = ("configured", "managed", "path", "system")


class ToolStatus(CamelModel):
    """What the GUI and `mfp tools` both render.

    Every field answers a question a stuck user actually asks: is it here,
    which copy is answering, what version, and can this program fix it.
    """

    name: str
    #: True when something answers -- managed, configured or on PATH.
    installed: bool
    #: Which copy won. One of `SOURCES`, or None when nothing answered.
    source: str | None = None
    path: str | None = None
    version: str | None = None
    #: False for Chrome and anything else we can only link to.
    manageable: bool = False
    approx_bytes: int | None = None
    homepage: str | None = None
    #: Where the managed copy came from, when there is one.
    installed_from: str | None = None
    installed_at: str | None = None


def _ytdlp_payload(fetcher: "_Fetcher") -> tuple[str, str, str | None]:
    """yt-dlp's own release, and the checksum file beside it.

    `latest/download/...` rather than the REST API on purpose: the API is
    rate-limited per IP and this is a machine that may be behind a shared
    address, while the redirect endpoint is not. The two requests could in
    principle straddle a release -- the digest would then not match and the
    install fails with "再試一次", which is the correct outcome and not a
    silent wrong binary.
    """
    base = "https://github.com/yt-dlp/yt-dlp/releases/latest/download"
    sums = fetcher.text(f"{base}/SHA2-256SUMS")
    digest = _digest_from_sums(sums, "yt-dlp.exe")
    return f"{base}/yt-dlp.exe", digest, None


def _gyan_ffmpeg_payload(fetcher: "_Fetcher") -> tuple[str, str, str | None]:
    """gyan.dev's release build -- the smaller of the two ffmpeg.org links.

    106 MB against BtbN's 163 MB, and it publishes `.sha256` and `.ver`
    beside the archive, so the version can be shown BEFORE the transfer
    starts rather than discovered by running the result.
    """
    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    digest = fetcher.text(f"{url}.sha256").strip().split()[0]
    version = fetcher.text(f"{url}.ver").strip() or None
    return url, digest, version


def _btbn_ffmpeg_payload(fetcher: "_Fetcher") -> tuple[str, str, str | None]:
    """The fallback ffmpeg, from the other build ffmpeg.org links.

    It exists because the primary is one person's domain. Bigger, and it
    costs an API call for the digest -- which is why it is second, not
    because it is worse.
    """
    release = fetcher.json("https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest")
    for asset in release.get("assets", []):
        if asset.get("name") == "ffmpeg-master-latest-win64-gpl.zip":
            digest = str(asset.get("digest") or "")
            if not digest.startswith("sha256:"):
                raise ToolInstallError("備用來源沒有提供可比對的檔案指紋，所以沒有下載。")
            return str(asset["browser_download_url"]), digest.split(":", 1)[1], None
    raise ToolInstallError("備用來源找不到 Windows 版的 ffmpeg 檔案。")


#: The tools this program can install by itself.
#:
#: Chrome is not here and must not be: installing a browser is not
#: something a media downloader gets to do to somebody's machine. The GUI
#: lists it with a link instead, which is `TOOL_LINKS` below.
MANAGED: dict[str, ManagedTool] = {
    "yt-dlp": ManagedTool(
        name="yt-dlp",
        files=("yt-dlp.exe",),
        version_argv_tail=("--version",),
        homepage="https://github.com/yt-dlp/yt-dlp/releases/latest",
        approx_bytes=18 * 1024 * 1024,
        payloads=(Payload(origin="yt-dlp 官方 GitHub 發行頁", resolve=_ytdlp_payload, kind="exe"),),
    ),
    "ffmpeg": ManagedTool(
        name="ffmpeg",
        # ffprobe rides in the same archive. It is not a separate tool and
        # must never become one: an ffmpeg without its probe is a stack
        # command that fails on its first question about a video.
        files=("ffmpeg.exe", "ffprobe.exe"),
        version_argv_tail=("-version",),
        homepage="https://ffmpeg.org/download.html#build-windows",
        approx_bytes=106 * 1024 * 1024,
        payloads=(
            Payload(origin="gyan.dev（ffmpeg.org 官網列出的 Windows 版本）",
                    resolve=_gyan_ffmpeg_payload, kind="zip"),
            Payload(origin="BtbN（ffmpeg.org 官網列出的另一個 Windows 版本）",
                    resolve=_btbn_ffmpeg_payload, kind="zip"),
        ),
    ),
}

#: Things the user must get themselves, with the one address that helps.
TOOL_LINKS: dict[str, str] = {
    "chrome": "https://www.google.com/chrome/",
}


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------

#: Config overrides, registered once by whoever loaded the config.
#:
#: A module-level value rather than a parameter threaded through every call
#: site, and that is a deliberate trade. `stack.py` builds ffmpeg command
#: lines in eight places, none of which has ever had a config object, so the
#: alternative was either eight new parameters or eight command lines that
#: keep ignoring `binaries.ffmpeg` -- which is what they did until now, and
#: is a defect in its own right. One process, one user, one config.
_overrides: BinariesConfig | None = None


def set_overrides(binaries: BinariesConfig | None) -> None:
    """Register the configured binary paths for every later `resolve`."""
    global _overrides
    _overrides = binaries


def _configured(name: str, binaries: BinariesConfig | None = None) -> str | None:
    chosen = binaries if binaries is not None else _overrides
    if chosen is None:
        return None
    field = {"yt-dlp": "yt_dlp", "ffmpeg": "ffmpeg", "ffprobe": "ffmpeg"}.get(name)
    if field is None:
        return None
    value = getattr(chosen, field, None)
    if not value:
        return None
    # ffprobe borrows ffmpeg's setting: they live in one directory and a
    # user who pointed at one has pointed at both. Pointing at ffmpeg.exe
    # and getting ffprobe from PATH is how a machine ends up mixing two
    # builds.
    if name == "ffprobe":
        sibling = Path(value).with_name("ffprobe.exe")
        return str(sibling) if sibling.is_file() else None
    return value


def managed_dir() -> Path:
    """`%APPDATA%/media-fetch-pipeline/tools`. Not created by reading it."""
    return app_data_dir() / "tools"


def _managed_file(name: str) -> Path:
    """Where a managed copy of `name` would live, whether or not it does."""
    stem = "ffmpeg.exe" if name == "ffmpeg" else f"{name}.exe"
    if name == "ffprobe":
        stem = "ffprobe.exe"
    return managed_dir() / stem


@dataclass(frozen=True)
class Resolved:
    path: str | None
    # Narrower than `SOURCES` on purpose: resolution walks three rungs, and
    # `system` is not one of them -- that value belongs to `_chrome_status`,
    # which builds a `ToolStatus` without ever resolving a path.
    source: str | None  # "configured" | "managed" | "path" | None


def resolution(name: str, *, binaries: BinariesConfig | None = None) -> Resolved:
    """Which copy of `name` this process will run, and where it came from.

    `binaries` names the configured paths explicitly, for a caller that
    already holds the config object it would otherwise be answering from.
    `set_overrides` exists for the callers that do NOT -- eight ffmpeg
    command lines in `stack.py` that never had one -- and passing the
    config here is strictly better than mutating that global from a call
    site, which would make the answer depend on who ran last.
    """
    configured = _configured(name, binaries)
    if configured:
        found = shutil.which(configured) or (configured if Path(configured).is_file() else None)
        if found:
            return Resolved(path=str(found), source="configured")
    managed = _managed_file(name)
    if managed.is_file():
        return Resolved(path=str(managed), source="managed")
    on_path = shutil.which(name)
    if on_path:
        return Resolved(path=str(on_path), source="path")
    return Resolved(path=None, source=None)


def resolve(name: str, *, binaries: BinariesConfig | None = None) -> str | None:
    """The absolute path to run, or None when nothing answers."""
    return resolution(name, binaries=binaries).path


# --------------------------------------------------------------------------
# The record of what was installed
# --------------------------------------------------------------------------

def _ledger_path() -> Path:
    return managed_dir() / "installed.json"


def _read_ledger() -> dict:
    try:
        return json.loads(_ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A missing or corrupt ledger costs a line of provenance in the UI
        # and nothing else. The files themselves are the state; this is a
        # note about them, and a note must never be able to break the thing
        # it describes.
        return {}


def _write_ledger(entries: dict) -> None:
    try:
        managed_dir().mkdir(parents=True, exist_ok=True)
        _ledger_path().write_text(
            json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

def _run_version(path: str, tail: Sequence[str]) -> str | None:
    try:
        proc = subprocess.run(
            [path, *tail],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15.0,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # `doctor`'s parsers, not a second pair. Two readers of the same output
    # drift, and the drift shows up as a settings panel and a dependency
    # table disagreeing about the version of the same file -- which is the
    # exact confusion this whole round exists to remove.
    from mfp.doctor import _parse_ffmpeg_version, _parse_first_line

    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    parse = _parse_ffmpeg_version if "ffmpeg" in Path(path).stem.lower() else _parse_first_line
    version = parse(text)
    return version[:120] if version else None


def _chrome_status(chrome: object | None) -> ToolStatus:
    """Chrome, answered by the one check that knows how to find it.

    NOT by `shutil.which`. Chrome installs into a versioned directory and
    puts nothing on PATH, so a PATH lookup reports 「沒安裝」 on a machine
    with Chrome open in front of the user -- which is the single most
    effective way to make a setup panel stop being believed. `doctor` has
    read the registry and the filesystem for this since M1; this row is that
    answer wearing the same shape as the rows beside it.
    """
    from mfp.config import ChromeConfig
    from mfp.doctor import check_chrome

    check = check_chrome(chrome if isinstance(chrome, ChromeConfig) else ChromeConfig())
    return ToolStatus(
        name="chrome",
        installed=check.found,
        source="system" if check.found else None,
        path=check.path,
        version=check.version,
        manageable=False,
        homepage=TOOL_LINKS["chrome"],
    )


def status(name: str, *, chrome: object | None = None) -> ToolStatus:
    """One tool, fully described. Runs the binary to read its version."""
    if name == "chrome":
        return _chrome_status(chrome)
    spec = MANAGED.get(name)
    found = resolution(name)
    ledger = _read_ledger().get(name, {}) if found.source == "managed" else {}
    version = (
        _run_version(found.path, spec.version_argv_tail)
        if found.path and spec is not None
        else None
    )
    return ToolStatus(
        name=name,
        installed=found.path is not None,
        source=found.source,
        path=found.path,
        version=version,
        manageable=spec is not None,
        approx_bytes=spec.approx_bytes if spec else None,
        homepage=spec.homepage if spec else TOOL_LINKS.get(name),
        installed_from=ledger.get("origin"),
        installed_at=ledger.get("installedAt"),
    )


def statuses(names: Sequence[str] | None = None, *,
             chrome: object | None = None) -> list[ToolStatus]:
    """Every tool the setup panel shows, in the order it shows them.

    The order is the order they matter in: without yt-dlp nothing downloads
    at all, without ffmpeg the good YouTube qualities cannot be joined, and
    Chrome only decides whether Instagram and Threads work.
    """
    return [status(name, chrome=chrome) for name in (names or [*MANAGED, "chrome"])]


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    if host in ALLOWED_HOSTS:
        return True
    return any(
        entry.startswith(".") and host.endswith(entry) for entry in ALLOWED_HOSTS
    )


def _digest_from_sums(text: str, filename: str) -> str:
    """Pull one file's digest out of a `sha256sum`-format listing."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            return parts[0].lower()
    raise ToolInstallError(f"發行方的檔案清單裡沒有 {filename}，所以沒有下載。")


class _Fetcher:
    """An httpx client that refuses to talk to anywhere it was not told to.

    The check is on the REQUEST hook rather than the response, so a redirect
    to somewhere unexpected never leaves this machine at all -- checking the
    response would mean the connection has already been made.
    """

    def __init__(self, client=None) -> None:
        import httpx

        def guard(request) -> None:
            if not _host_allowed(request.url.host):
                raise ToolInstallError(
                    f"下載被擋下來了：{request.url.host} 不在允許的來源清單裡。"
                )

        self._client = client or httpx.Client(
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            timeout=httpx.Timeout(connect=15.0, read=60.0, write=30.0, pool=15.0),
            headers={"User-Agent": "media-fetch-pipeline"},
            event_hooks={"request": [guard]},
        )

    def __enter__(self) -> "_Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def text(self, url: str) -> str:
        response = self._client.get(url)
        response.raise_for_status()
        return response.text

    def json(self, url: str) -> dict:
        response = self._client.get(url, headers={"Accept": "application/vnd.github+json"})
        response.raise_for_status()
        return response.json()

    def download(self, url: str, dest: Path, *, on_progress: ProgressFn | None,
                 tool: str) -> str:
        """Stream to `dest`, hashing on the way. Returns the sha256 hex."""
        digest = hashlib.sha256()
        written = 0
        with self._client.stream("GET", url) as response:
            response.raise_for_status()
            raw_total = response.headers.get("content-length")
            total = int(raw_total) if raw_total and raw_total.isdigit() else None
            if total is not None and total > _MAX_PAYLOAD_BYTES:
                raise ToolInstallError("這個檔案比預期大得多，已經中止下載。")
            with dest.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK):
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if written > _MAX_PAYLOAD_BYTES:
                        raise ToolInstallError("這個檔案比預期大得多，已經中止下載。")
                    if on_progress is not None:
                        on_progress(
                            {
                                "tool": tool,
                                "phase": "downloading",
                                "bytes": written,
                                "total": total,
                            }
                        )
        return digest.hexdigest()


# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------

def _place_exe(payload: Path, spec: ManagedTool) -> list[Path]:
    target = managed_dir() / spec.files[0]
    _replace(payload, target)
    return [target]


def _place_zip(payload: Path, spec: ManagedTool) -> list[Path]:
    """Take exactly the files we asked for out of the archive.

    `ZipFile.extract` is not used anywhere here on purpose: it honours the
    path stored INSIDE the archive, which is how an archive gets to choose
    where on this disk it lands. Every destination below is built from our
    own directory plus a name from `spec.files`.
    """
    wanted = {name.lower(): name for name in spec.files}
    placed: list[Path] = []
    with zipfile.ZipFile(payload) as archive:
        chosen: dict[str, str] = {}
        for member in archive.namelist():
            if member.endswith("/"):
                continue
            base = member.rsplit("/", 1)[-1].lower()
            if base in wanted and base not in chosen:
                chosen[base] = member
        missing = [wanted[key] for key in wanted if key not in chosen]
        if missing:
            raise ToolInstallError(
                f"下載的壓縮檔裡沒有 {'、'.join(missing)}，可能是來源改版了。"
            )
        staging = payload.parent
        for base, member in chosen.items():
            temp = staging / f"_{base}"
            with archive.open(member) as source, temp.open("wb") as handle:
                shutil.copyfileobj(source, handle, _CHUNK)
            target = managed_dir() / wanted[base]
            _replace(temp, target)
            placed.append(target)
    return placed


def _replace(source: Path, target: Path) -> None:
    """Move `source` onto `target`, saying something useful when it is busy."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
    except PermissionError as exc:
        # Windows will not replace a running executable. This is not a
        # mysterious failure -- it means a download or a stack job is using
        # it right now -- and the message has to say so, because "permission
        # denied" sends people looking for an administrator they do not need.
        raise ToolInstallError(
            f"{target.name} 正在被使用中，沒辦法換掉。請等佇列裡的工作跑完，或關掉程式再試一次。"
        ) from exc
    except OSError as exc:
        raise ToolInstallError(f"寫入 {target.name} 失敗：{exc}") from exc


def install(
    name: str,
    *,
    on_progress: ProgressFn | None = None,
    fetcher: _Fetcher | None = None,
) -> ToolStatus:
    """Fetch, verify and wire up one managed tool.

    Every source in the spec is tried in order and the last failure is the
    one reported: a fallback that swallows the primary's reason leaves the
    user reading about a host they never chose.
    """
    spec = MANAGED.get(name)
    if spec is None:
        raise UsageError(f"「{name}」不是這個程式可以自己安裝的工具。")

    staging = managed_dir() / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    try:
        for payload_spec in spec.payloads:
            owned = fetcher is None
            client = fetcher or _Fetcher()
            try:
                if on_progress is not None:
                    on_progress({"tool": name, "phase": "resolving",
                                 "detail": payload_spec.origin})
                url, expected, version = payload_spec.resolve(client)
                temp = staging / f"{name}.part"
                actual = client.download(url, temp, on_progress=on_progress, tool=name)
                if actual.lower() != expected.lower():
                    temp.unlink(missing_ok=True)
                    raise ToolInstallError(
                        "下載回來的檔案跟發行方公布的指紋對不上，已經刪掉沒有安裝。"
                        "通常再試一次就好；如果一直這樣，請改用手動下載。"
                    )
                if on_progress is not None:
                    on_progress({"tool": name, "phase": "installing",
                                 "detail": version or ""})
                placed = (
                    _place_exe(temp, spec)
                    if payload_spec.kind == "exe"
                    else _place_zip(temp, spec)
                )
                temp.unlink(missing_ok=True)
                _record(name, origin=payload_spec.origin, url=url,
                        sha256=expected, version=version, files=placed)
                break
            except ToolInstallError as exc:
                failures.append(f"{payload_spec.origin}：{exc.detail or exc}")
            except Exception as exc:  # transport, zip, disk
                failures.append(f"{payload_spec.origin}：{exc}")
            finally:
                if owned:
                    client.close()
        else:
            raise ToolInstallError("；".join(failures) or "下載失敗，而且沒有說明原因。")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    final = status(name)
    if not final.installed or final.version is None:
        # Installed and unable to run is its own failure, and finding it out
        # HERE beats finding it out when a download fails three screens
        # later. Windows Defender quarantining yt-dlp.exe is the common
        # cause and it is the first thing the message names.
        raise ToolInstallError(
            f"{name} 已經放好了，但執行不起來。最常見的原因是防毒軟體把它隔離了—"
            f"請到防毒軟體的隔離區把 {managed_dir()} 底下的檔案放行，再按一次重新檢查。"
        )
    if on_progress is not None:
        on_progress({"tool": name, "phase": "done", "detail": final.version or ""})
    return final


def _record(name: str, *, origin: str, url: str, sha256: str,
            version: str | None, files: Sequence[Path]) -> None:
    entries = _read_ledger()
    entries[name] = {
        "origin": origin,
        "url": url,
        "sha256": sha256,
        "version": version,
        "installedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": [str(path) for path in files],
    }
    _write_ledger(entries)


def remove(name: str) -> ToolStatus:
    """Delete the managed copy. PATH and configured copies are untouched.

    Here because a managed directory nothing can empty is a directory that
    only grows, and because the honest answer to 「我想改用自己裝的那個」 is
    a button rather than an instruction to find `%APPDATA%`.
    """
    spec = MANAGED.get(name)
    if spec is None:
        raise UsageError(f"「{name}」不是這個程式管理的工具。")
    for filename in spec.files:
        try:
            (managed_dir() / filename).unlink(missing_ok=True)
        except OSError as exc:
            raise ToolInstallError(f"刪不掉 {filename}：{exc}") from exc
    entries = _read_ledger()
    entries.pop(name, None)
    _write_ledger(entries)
    return status(name)
