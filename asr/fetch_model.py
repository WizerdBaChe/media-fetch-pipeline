"""Fetch a model, on the far side of the same process boundary as `runner.py`.

**Why this is a separate script and not `mfp` code.** Downloading from the
Hugging Face Hub needs `huggingface_hub`, which is a faster-whisper
dependency and therefore already sitting in the engine's interpreter. Adding
it to `mfp` would add it to the ~107 MB installer, and the rule this project
has followed since Phase G is that a new Python dependency IS installer size.
So this runs the way `runner.py` runs: same interpreter, same `@asr` progress
lines on stderr, same single JSON object on stdout.

**Why not just let faster-whisper download it.** It already can -- that is
what `allowDownload` permits -- and the settings panel says why that is off
by default: 「沒有預告的大量下載跟當掉看起來一模一樣」. A 3 GB transfer with
no size announced, no progress and no way to stop is indistinguishable from a
hang. This exists to make the same transfer something a person can watch.

Resumable for free: `huggingface_hub` keeps partial files and continues from
them, so a cancelled or interrupted download costs only what it had not yet
fetched.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time

#: Emitted at most this often, so a fast link does not spend its time
#: writing progress lines instead of moving bytes.
REPORT_INTERVAL = 0.4

#: Same as `runner.py`: "this environment cannot do it" is a different
#: answer from "that transfer failed", and the two deserve different advice.
EXIT_UNUSABLE = 2


def say(message: str) -> None:
    """The human's channel."""
    print(message, file=sys.stderr, flush=True)


def progress(**fields) -> None:
    """The machine's channel, in `runner.py`'s dialect."""
    print("@asr " + json.dumps(fields, ensure_ascii=False),
          file=sys.stderr, flush=True)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def fail(message: str, *, unusable: bool = False) -> int:
    emit({"ok": False, "error": message})
    return EXIT_UNUSABLE if unusable else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", help="Hugging Face repo id")
    parser.add_argument("--dest", required=True,
                        help="The folder the model lands in")
    parser.add_argument("--measure", action="store_true",
                        help="Report the size and download nothing")
    args = parser.parse_args()

    try:
        from huggingface_hub import HfApi, snapshot_download
    except ImportError as exc:
        return fail(
            f"this interpreter cannot download models: {exc}. "
            f"huggingface_hub comes with faster-whisper, so an engine that "
            f"can transcribe can also do this",
            unusable=True,
        )

    # The size FIRST, always, and separately from the transfer. It is what
    # turns "3 GB is about to happen" from a thing the user finds out
    # afterwards into a thing they agreed to.
    try:
        info = HfApi().repo_info(args.repo, files_metadata=True)
    except Exception as exc:  # noqa: BLE001 - hub errors come in many shapes
        return fail(f"could not reach {args.repo}: {exc}")
    total = sum(getattr(f, "size", 0) or 0 for f in info.siblings)
    files = [f.rfilename for f in info.siblings]
    progress(phase="sized", bytes=total, files=len(files))
    say(f"{args.repo}: {total / 1e9:.2f} GB in {len(files)} files")
    if args.measure:
        emit({"ok": True, "repo": args.repo, "bytes": total, "files": files})
        return 0

    state = {"done": 0, "last": 0.0}

    class Reporter:
        """A tqdm-shaped object, because that is the hook the hub offers.

        Only `update` carries information we want; everything else is the
        protocol `huggingface_hub` expects to be able to call. Deliberately
        NOT a tqdm subclass: tqdm writes to the terminal, and this process's
        stderr is a structured channel somebody else is parsing.

        `get_lock` is not optional decoration. The hub downloads files in
        parallel and asks the bar class for the lock it will serialise the
        workers on; without it the transfer dies on
        `type object 'Reporter' has no attribute 'get_lock'` after the size
        has already been reported, which looks like a network failure and is
        not one.
        """

        _lock = threading.RLock()

        @classmethod
        def get_lock(cls):
            return cls._lock

        @classmethod
        def set_lock(cls, lock) -> None:
            cls._lock = lock

        def __init__(self, *args, **kwargs) -> None:
            # tqdm's first positional argument is an ITERABLE, and the hub
            # uses it both ways: a bare byte counter per file, and a wrapper
            # around the file list for the overall bar. Missing this raises
            # `'Reporter' object is not iterable` -- after four of six files
            # have already downloaded, which is a confusing place to fail.
            self.iterable = args[0] if args else kwargs.get("iterable")
            self.total = kwargs.get("total")
            # Whether THIS bar counts bytes. The hub makes two kinds: one per
            # file measuring bytes, and one over the file list measuring
            # files. Adding both into one counter would report 486,215,851
            # bytes of a 486,215,847-byte download -- a progress bar that
            # goes past 100% because two units were summed.
            self.counts_bytes = kwargs.get("unit") == "B"
            self.n = 0

        def __iter__(self):
            for item in self.iterable or ():
                yield item
                self.update(1)

        def update(self, n: int = 1) -> None:
            self.n += n
            if not self.counts_bytes:
                return
            state["done"] += n
            now = time.monotonic()
            if now - state["last"] < REPORT_INTERVAL:
                return
            state["last"] = now
            progress(phase="fetching", bytes=state["done"], total=total)

        def close(self) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

        # tqdm's API surface that the hub touches on some paths.
        def set_description(self, *_a, **_k) -> None:
            return None

        def refresh(self, *_a, **_k) -> None:
            return None

        def reset(self, total=None) -> None:
            self.total = total
            self.n = 0

    started = time.perf_counter()
    try:
        path = snapshot_download(
            args.repo,
            local_dir=args.dest,
            tqdm_class=Reporter,
            # The repository's own history is not the model. `.gitattributes`
            # and the card are small, but fetching them means the folder this
            # writes is not simply "the model", and `inspect_model` on the
            # other side is stricter about what a model folder looks like
            # than the Hub is about what a repo holds.
            ignore_patterns=["*.md", ".gitattributes"],
        )
    except KeyboardInterrupt:
        # Partial files stay on disk on purpose: the hub resumes from them,
        # so a cancelled 3 GB download does not have to start over.
        return fail("the download was cancelled; what arrived is kept and "
                    "a later attempt will continue from it")
    except Exception as exc:  # noqa: BLE001
        return fail(f"the download failed: {exc}")

    wall = time.perf_counter() - started
    progress(phase="fetching", bytes=total, total=total)
    say(f"downloaded {total / 1e9:.2f} GB in {wall:.0f}s")
    emit({"ok": True, "repo": args.repo, "path": str(path), "bytes": total,
          "wallSeconds": round(wall, 1)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
