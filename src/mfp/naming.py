"""Filename sanitizer and output-path organizer (PSM Batch 1 core §8).

Security-critical: every path component derived from remote/untrusted data
(author name, caption, altText, post id) MUST pass through
`sanitize_component()`, and every resolved output path MUST pass through
`resolve_output_path()`'s containment check before any file is touched.
Treat every string this module receives as adversarial input (Phase 2 §4.2
entry point E2): `../` sequences, Windows reserved device names, control
characters, and pathological lengths are expected inputs, not edge cases.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path
from typing import Literal

from mfp.errors import PathEscape, PathTooLong

# --- sanitize_component() ---------------------------------------------------

_ALLOWED_PUNCTUATION = set("-_.()[] ")
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_MAX_COMPONENT_LENGTH = 80

_RUN_UNDERSCORE_RE = re.compile(r"_+")
_RUN_SPACE_RE = re.compile(r" +")


def _is_control_char(ch: str) -> bool:
    """C0 (U+0000-U+001F, U+007F) or C1 (U+0080-U+009F) control character."""
    cp = ord(ch)
    return cp <= 0x1F or cp == 0x7F or 0x80 <= cp <= 0x9F


def _is_allowed_char(ch: str) -> bool:
    """Unicode letters (incl. CJK), digits, space, and `-_.()[]`."""
    if ch in _ALLOWED_PUNCTUATION:
        return True
    category = unicodedata.category(ch)
    # "L*" = letters (Lu/Ll/Lt/Lm/Lo -- Lo covers CJK ideographs).
    # "N*" = numbers (Nd/Nl/No).
    return category[0] in ("L", "N")


def sanitize_component(raw: str | None, *, fallback: str) -> str:
    """Sanitize one path component derived from untrusted input.

    Implements the seven steps of PSM Batch 1 core §8, in order:
      1. NFC-normalize; strip control characters and directory separators.
      2. Allowlist unicode letters/digits/CJK/space/`-_.()[]`; else `_`.
      3. Collapse runs of `_`/space; strip leading/trailing dots and spaces.
      4. Reject Windows reserved device names (case-insensitive, with or
         without extension) by prefixing with `_`.
      5. Truncate to 80 characters.
      5b. Re-strip leading/trailing dots and spaces exposed by truncation.
      6. If empty after all of the above, return `fallback`.

    Step 5b exists because step 3's strip only sees the pre-truncation
    string: a cut landing exactly on a `.` or ` ` reintroduces one at the
    new end. Windows silently trims trailing dots/spaces from real
    filenames, so leaving one in the sanitized string would make the
    in-code path and the on-disk path diverge -- step 5b closes that gap.
    It does not replace step 3; both run, in order.

    `fallback` is caller-supplied (e.g. `"item_<index>"` or
    `"unknown_author"` per §8 step 6) since the right fallback depends on
    which field was being sanitized.
    """
    s = raw if raw is not None else ""

    # Step 1: NFC-normalize; strip control chars and directory separators.
    s = unicodedata.normalize("NFC", s)
    s = "".join(ch for ch in s if not _is_control_char(ch))
    s = s.replace("/", "").replace("\\", "")

    # Step 2: allowlist.
    s = "".join(ch if _is_allowed_char(ch) else "_" for ch in s)

    # Step 3: collapse runs; strip leading/trailing dots and spaces.
    s = _RUN_UNDERSCORE_RE.sub("_", s)
    s = _RUN_SPACE_RE.sub(" ", s)
    s = s.strip(". ")

    # Step 4: reserved device names, case-insensitive, with or without
    # extension -- match is on the component's stem (text before the first
    # dot), so "CON.jpg" and "CON.tar.gz" both match, "MyCON.txt" does not.
    stem = s.split(".", 1)[0]
    if stem.upper() in _RESERVED_DEVICE_NAMES:
        s = f"_{s}"

    # Step 5: truncate.
    s = s[:_MAX_COMPONENT_LENGTH]

    # Step 5b: re-strip leading/trailing dots/spaces exposed by truncation.
    # Note this can never turn a step-3-non-empty string into "": step 3
    # already guarantees the pre-truncation string starts and ends with a
    # non-dot/space character (or is itself empty, handled by step 6
    # below); truncation only removes from the tail, so that leading
    # character always survives into the truncated string, and a strip
    # from the edges can never reach past it.
    s = s.strip(". ")

    # Step 6: empty-after-sanitize fallback.
    if not s:
        return fallback

    return s


# --- filename sanitization ---------------------------------------------------

# Closed allowlist for the on-disk extension. PSM §5.4 TRAP-2: the
# extension is derived from `media_type` / the `efg` venncode_tag, both
# remote data -- never inferred from (and never allowed to carry through
# as) an arbitrary attacker-influenced string. Anything not on this list,
# including an absent or pathological extension, becomes "bin".
_ALLOWED_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "heic", "mp4", "m4a", "mp3", "json"})
_EXTENSION_FALLBACK = "bin"
_FILENAME_STEM_FALLBACK = "file"


def _sanitize_filename(raw: str) -> str:
    """Sanitize a filename that may itself carry remote-derived content.

    Splits `raw` on its last `.` into stem + extension. The stem is
    sanitized via `sanitize_component()` (so `"CON.jpg"`, `".."`, control
    characters, etc. are neutralized exactly as any other path component
    would be). The extension is validated case-insensitively against a
    closed allowlist; anything else becomes `"bin"`. A filename with no
    `.` at all is treated as an all-stem, no-extension name (extension
    falls back to `"bin"`).
    """
    if "." in raw:
        stem, _, ext = raw.rpartition(".")
    else:
        stem, ext = raw, ""

    safe_stem = sanitize_component(stem, fallback=_FILENAME_STEM_FALLBACK)
    safe_ext = ext.strip().lower()
    if safe_ext not in _ALLOWED_EXTENSIONS:
        safe_ext = _EXTENSION_FALLBACK

    return f"{safe_stem}.{safe_ext}"


# --- filename construction (PSM Batch 2 §10, ruled 2026-08-12) --------------

# The stem's post id is capped tighter than a directory component (80),
# because it appears a second time inside an already-long path. Real
# Instagram/Threads shortcodes are ~11 characters, so this never binds in
# practice; it exists to bound the worst case.
_MAX_STEM_ID_LENGTH = 32


def media_filename(
    post_id: str, index: int, ext: str, *, rung: int | None = None
) -> str:
    """Build `<postId>_<NN>[_<N>p].<ext>` (PSM Batch 2 §10).

    Concise by ruling: the date and author live in the directory path and
    the long text lives in `_info.txt`, so the filename carries only what
    makes a single file traceable on its own -- the post id -- plus its
    ordering and, for video, which quality rung it is.

    `rung` is appended ONLY for video, and only when known: re-fetching a
    Reel at a different rung must produce a distinct file rather than
    silently overwriting the earlier one. A `None`/`0` is omitted outright
    -- `_Nonep` or `_0p` in a filename is worse than no suffix.

    It is a RUNG, not a height, and the parameter was called `height` until
    2026-08-20 while the caller passed exactly that: a 1080x1920 Reel landed
    as `_1920p.mp4`, naming a rendition no menu offers. What belongs here is
    the number a person would say -- `policy.resolution_class`, the short
    side -- and the caller owns that conversion.
    """
    stem = sanitize_component(post_id, fallback=_FILENAME_STEM_FALLBACK)[:_MAX_STEM_ID_LENGTH]
    suffix = f"_{rung}p" if rung else ""
    return _sanitize_filename(f"{stem}_{index:02d}{suffix}.{ext}")


def manifest_filename() -> str:
    """The machine-readable sidecar. Fixed name: it is scoped by its own
    post directory, and a stable name keeps it greppable across the tree."""
    return "manifest.json"


def info_filename() -> str:
    """The human-readable sidecar (`_info.txt`, PSM Batch 2 §10)."""
    return "_info.txt"


# --- resolve_output_path() --------------------------------------------------

_MAX_FULL_PATH_LENGTH = 240
_HASH_FALLBACK_LENGTH = 12


def _hash_component(component: str) -> str:
    return hashlib.sha256(component.encode("utf-8")).hexdigest()[:_HASH_FALLBACK_LENGTH]


def _ensure_within_root(candidate: Path, root: Path) -> Path:
    """Raise `PathEscape` unless `candidate` resolves inside `root`.

    This is the final backstop (PSM §8): even though every remote-derived
    component (platform, author, post id, and now the filename -- see
    `_sanitize_filename`) is sanitized before reaching here, this check
    still runs unconditionally, independent of and in addition to that
    sanitization, as defense in depth.

    `os.path.realpath()` already normalizes Windows drive-letter case and
    strips a trailing separator, so `"d:/out"`, `"D:/out"` and `"D:/out/"`
    all compare equal (verified on win32, 2026-08-12). The prefix is built
    with `os.path.join(real_root, "")` rather than `real_root + os.sep`
    because a drive root realpaths to `"D:\\"` -- already separator-
    terminated -- and naive concatenation would yield `"D:\\\\"`, rejecting
    every legitimate path under an output root set to a bare drive.
    """
    real_candidate = os.path.realpath(candidate)
    real_root = os.path.realpath(root)
    root_prefix = os.path.join(real_root, "")
    if real_candidate != real_root and not real_candidate.startswith(root_prefix):
        raise PathEscape(f"resolved path escapes output root: {candidate!r}")
    return candidate


def resolve_output_path(
    output_root: str | Path,
    *,
    platform: str,
    author: str | None,
    date: str,
    post_id: str,
    filename: str,
    author_fallback: str = "unknown_author",
    post_fallback: str = "unknown_post",
) -> Path:
    """Resolve `<outputRoot>/<platform>/<author>/<date>_<postId>/<filename>`
    (layout per Phase 2 §7 U-2 / PSM §8), sanitizing every remote-derived
    component and enforcing containment inside `output_root`.

    `date` is caller-generated (already `YYYY-MM-DD`) and is not treated as
    untrusted. `platform`, `author`, and `post_id` are sanitized via
    `sanitize_component()`. `filename` (e.g. `"00.jpg"`, `"manifest.json"`)
    is ALSO untrusted, even though it looks internally generated: its
    extension is derived from remote `media_type`/venncode_tag data (PSM
    §5.4 TRAP-2), so it is routed through `_sanitize_filename()` (stem via
    `sanitize_component()`, extension via a closed allowlist).

    Raises `PathEscape` if the resolved path is not inside `output_root` --
    kept as an unconditional backstop even though sanitization now covers
    every input component.

    Length handling is the two-rung ladder of PSM Batch 1 §8.1 as revised
    2026-08-12:

      L0  the full layout above
      L1  the post-directory name (`<date>_<postId>`) replaced by a
          12-character hash of itself

    Rungs L2/L3 were withdrawn along with the self-describing filename that
    forced them: with the concise `<postId>_<NN>` shape the worst case is
    ~252 characters and L1 alone reaches ~173 against the 240 cap.

    If L1 still overflows, `PathTooLong` is raised. Truncating instead would
    risk two posts resolving to the same name, and one download silently
    overwriting another is worse than an error the user can act on.
    """
    root = Path(output_root)
    safe_platform = sanitize_component(platform, fallback="unknown_platform")
    safe_author = sanitize_component(author, fallback=author_fallback)
    safe_post_id = sanitize_component(post_id, fallback=post_fallback)
    safe_filename = _sanitize_filename(filename)
    post_dir_name = f"{date}_{safe_post_id}"

    candidate = root / safe_platform / safe_author / post_dir_name / safe_filename
    if len(str(candidate)) > _MAX_FULL_PATH_LENGTH:  # L1
        post_dir_name = _hash_component(post_dir_name)
        candidate = root / safe_platform / safe_author / post_dir_name / safe_filename
    if len(str(candidate)) > _MAX_FULL_PATH_LENGTH:
        raise PathTooLong(
            f"path is {len(str(candidate))} characters after hashing the post "
            f"directory, over the {_MAX_FULL_PATH_LENGTH} limit; "
            "choose a shorter output root"
        )

    return _ensure_within_root(candidate, root)


def path_degradation(output_root: str | Path, *, platform: str, author: str | None,
                     date: str, post_id: str, filename: str) -> Literal["L0", "L1"]:
    """Which rung `resolve_output_path()` would use, for `manifest.json`.

    The GUI shows a hashed folder name; without this the user has no way to
    learn why it looks like that (PSM Batch 2 §4.3 `pathDegradation`).
    """
    root = Path(output_root)
    candidate = (
        root
        / sanitize_component(platform, fallback="unknown_platform")
        / sanitize_component(author, fallback="unknown_author")
        / f"{date}_{sanitize_component(post_id, fallback='unknown_post')}"
        / _sanitize_filename(filename)
    )
    return "L0" if len(str(candidate)) <= _MAX_FULL_PATH_LENGTH else "L1"
