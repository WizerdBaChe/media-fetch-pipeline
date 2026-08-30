"""Seed the queue with one task per state, so the UI can be checked by eye.

Why this exists: M2/M3 acquisition is frozen, so nothing can legitimately move
a task into DOWNLOADING, PAUSED, EXPIRED or COMPLETED. Without seeded rows the
UAT items about how those rows render are simply not executable.

It writes `queue.json` directly rather than going through the API, on purpose:
the API has no endpoint for setting progress bytes, and adding one for testing
would put a fake-data surface into the production contract. Writing the state
file is the same thing UAT item 8 asks you to do by hand -- just correct.

Tasks are built with the real `Task` model, so a seed that stops matching the
schema is a build error here rather than a mystery in the UI.

Two steps, and the reason is INV-4: a task recorded as PROBING or DOWNLOADING
is demoted to PAUSED the moment the queue loads, because the process that owned
it is gone. So those two states cannot be seeded through the file at all --
measured, not assumed. They are reached instead by starting the server and
transitioning through the real API, which also proves the transitions are legal.
Seeded progress bytes survive the transition, so the promoted rows show
realistic speed and ETA rather than zeroes.

    scripts\\seed-demo.bat            seed (refuses to clobber a non-empty queue)
    scripts\\seed-demo.bat --force    seed anyway, after backing up
    scripts\\seed-demo.bat --promote  after the server is up: PAUSED -> DOWNLOADING
    scripts\\seed-demo.bat --clean    restore the most recent backup
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mfp.queue import QueueFile, Task, TaskProgress  # noqa: E402
from mfp.serve import default_queue_path  # noqa: E402

BACKUP_SUFFIX = ".seed-backup"


def _iso(offset_seconds: float = 0.0) -> str:
    return datetime.fromtimestamp(time.time() + offset_seconds, timezone.utc).isoformat()


def _task(
    index: int,
    state: str,
    *,
    author: str,
    post_id: str,
    platform: str = "instagram",
    progress: TaskProgress | None = None,
    policy: str | None = None,
    pinned: bool = False,
    error_code: str | None = None,
    degradation: str = "L0",
    output_dir: str | None = None,
) -> Task:
    return Task(
        id=f"seed-{index:02d}",
        state=state,
        platform=platform,
        post_id=post_id,
        canonical_url=f"https://www.instagram.com/p/{post_id}/",
        source_url=f"https://www.instagram.com/p/{post_id}/",
        author=author,
        policy=policy,
        policy_pinned=pinned,
        progress=progress or TaskProgress(),
        error_code=error_code,
        path_degradation=degradation,
        output_dir=output_dir,
        created_at=_iso(-3600 + index * 60),
        updated_at=_iso(-60),
        completed_at=_iso(-120) if state == "COMPLETED" else None,
    )


#: Seeded as PAUSED, promoted to DOWNLOADING by --promote. See the module
#: docstring: INV-4 makes a seeded DOWNLOADING impossible.
PROMOTE_IDS = ("seed-04", "seed-05", "seed-06")


def build_tasks() -> list[Task]:
    """One row per reachable state, plus the special renderings."""
    mb = 1024 * 1024
    return [
        _task(1, "PARSED", author="aomori_kenkanko", post_id="SEEDparsed1"),
        _task(
            2, "PARSED", author="jerry.chu5257", post_id="SEEDprobing", platform="threads",
            progress=TaskProgress(items_total=13),
        ),
        _task(
            3, "READY", author="ready_account", post_id="SEEDready01",
            progress=TaskProgress(items_total=4, bytes_total=48 * mb),
        ),
        _task(
            4, "PAUSED", author="downloading_one", post_id="SEEDdown001",
            progress=TaskProgress(
                bytes_done=int(32.6 * mb), bytes_total=48 * mb,
                bytes_per_sec=4.2 * mb, eta_seconds=8, items_done=9, items_total=13,
            ),
        ),
        _task(
            5, "PAUSED", author="downloading_two", post_id="SEEDdown002",
            progress=TaskProgress(
                bytes_done=int(1.4 * mb), bytes_total=int(6.7 * mb),
                bytes_per_sec=1.8 * mb, eta_seconds=42, items_done=1, items_total=1,
            ),
        ),
        # No byte total: the bar must render indeterminate, not 0%.
        _task(
            6, "PAUSED", author="unknown_size", post_id="SEEDdown003",
            progress=TaskProgress(bytes_done=900_000, bytes_per_sec=700_000),
        ),
        _task(
            7, "PAUSED", author="paused_account", post_id="SEEDpaused1",
            progress=TaskProgress(
                bytes_done=int(12 * mb), bytes_total=int(30 * mb),
                bytes_per_sec=0, items_done=2, items_total=6,
            ),
        ),
        _task(8, "EXPIRED", author="expired_account", post_id="SEEDexpired", error_code="link_expired"),
        _task(
            9, "COMPLETED", author="done_account", post_id="SEEDdone001",
            progress=TaskProgress(
                bytes_done=int(22 * mb), bytes_total=int(22 * mb),
                items_done=13, items_total=13,
            ),
            output_dir="D:/output/MediaGrabbed/instagram/done_account/2026-08-01_SEEDdone001",
        ),
        _task(10, "FAILED", author="failed_account", post_id="SEEDfailed1", error_code="media_transfer_failed"),
        # An errorCode with no row in the §11 table: must be named, not blank.
        _task(11, "FAILED", author="unknown_error", post_id="SEEDunknown", error_code="totally_new_code"),
        _task(12, "CANCELLED", author="cancelled_account", post_id="SEEDcancel1"),
        # Pinned override: must show 已自訂 and ignore global policy changes.
        _task(
            13, "READY", author="pinned_account", post_id="SEEDpinned1",
            policy="max-height:720", pinned=True,
            progress=TaskProgress(items_total=2, bytes_total=9 * mb),
        ),
        # Hashed folder name (L1): must carry the 資料夾名稱已縮短 note.
        _task(
            14, "COMPLETED", author="very_long_path_account", post_id="SEEDlongpath",
            degradation="L1",
            progress=TaskProgress(bytes_done=5 * mb, bytes_total=5 * mb, items_done=1, items_total=1),
            output_dir="D:/output/MediaGrabbed/instagram/very_long_path_account/a1b2c3d4e5f6",
        ),
    ]


def promote(base_url: str) -> int:
    """Move the seeded PAUSED rows to DOWNLOADING through the real API."""
    import httpx

    promoted = 0
    try:
        with httpx.Client(base_url=base_url, timeout=5.0) as client:
            client.get("/v1/health").raise_for_status()
            for task_id in PROMOTE_IDS:
                response = client.post(
                    f"/v1/queue/{task_id}:transition", json={"to": "DOWNLOADING"}
                )
                if response.status_code == 200:
                    promoted += 1
                    print(f"  {task_id} -> DOWNLOADING")
                else:
                    body = response.json() if response.content else {}
                    print(f"  {task_id} skipped: {body.get('errorCode', response.status_code)}")
    except Exception as exc:  # noqa: BLE001 -- any failure means "server not up"
        print(f"could not reach {base_url}: {exc}")
        print("  start it first: scripts\\dev-start.bat")
        return 1

    print(f"promoted {promoted} task(s). They will demote back to PAUSED on restart (INV-4).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo tasks for manual UI acceptance")
    parser.add_argument("--force", action="store_true", help="overwrite a non-empty queue (after backing it up)")
    parser.add_argument("--clean", action="store_true", help="restore the most recent seed backup")
    parser.add_argument("--promote", action="store_true", help="with the server running, move seeded rows to DOWNLOADING")
    parser.add_argument("--base-url", default="http://127.0.0.1:47821", help="running server (default: %(default)s)")
    parser.add_argument("--path", type=Path, default=None, help="queue.json to write (default: %%APPDATA%%)")
    args = parser.parse_args()

    if args.promote:
        return promote(args.base_url)

    path = args.path or default_queue_path()
    backup = path.with_name(path.name + BACKUP_SUFFIX)

    if args.clean:
        if backup.exists():
            shutil.move(str(backup), str(path))
            print(f"restored {path} from {backup.name}")
        elif path.exists():
            path.unlink()
            print(f"no backup found; removed the seeded {path}")
        else:
            print("nothing to clean")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        existing = path.read_text(encoding="utf-8")
        looks_empty = '"tasks": []' in existing or existing.strip() in ("", "{}")
        if not looks_empty and not args.force:
            print(f"refusing to overwrite a non-empty queue at {path}")
            print("  re-run with --force (the current file is backed up first)")
            return 1
        shutil.copy2(path, backup)
        print(f"backed up existing queue to {backup.name}")

    tasks = build_tasks()
    path.write_text(
        QueueFile(tasks=tasks).model_dump_json(by_alias=True, indent=2), encoding="utf-8"
    )

    print(f"seeded {len(tasks)} tasks into {path}")
    print("  1. restart `mfp serve` (or scripts\\dev-start.bat) to load them")
    print("  2. scripts\\seed-demo.bat --promote   <- gives you DOWNLOADING rows")
    print("  undo with: scripts\\seed-demo.bat --clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
