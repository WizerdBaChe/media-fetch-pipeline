---
name: media-fetch-pipeline
description: Download media from a public Instagram, Threads, YouTube, X or Bilibili post URL using the local `mfp` CLI. Use when the user pastes a post link and wants the images or video saved, asks to archive a post, or asks what quality is available before downloading. Not for accounts, feeds, stories, or anything behind a login.
---

# media-fetch-pipeline

A local CLI that reads a public post and saves its media. You call it; you do
not implement any of it. **Parse `stdout` only** — every human-readable line,
every progress bar and every warning goes to `stderr`, and mixing them is how
a parse silently succeeds on the wrong thing.

## When this applies

The user gives you a **single post URL** (or a few) and wants the media.
Supported: `instagram.com/p|reel/…`, `threads.com/@user/post/…` and its
`/share/…` links, `youtube.com`, `x.com`, `bilibili.com`, and — through
yt-dlp — most other video hosts.

**Not** supported, and not worth attempting a workaround for: profile pages,
feeds, Stories, anything private, anything requiring a login. A profile URL
exits 3 by design; that is the tool refusing to crawl an account, not a bug
to route around.

A platform can also be refused **before** any network call, when the local
setup cannot serve it -- currently a `yt-dlp` older than the floor in
`deps.lock.json`, which YouTube stopped working with in August 2026. Those
links come back in `report.blocked[]` with a machine-readable `reason`, and
the rest of the batch is queued as normal. Do not retry them and do not try
another URL shape: tell the user what `mfp doctor` says about `yt-dlp` and
that updating it lifts the block.

## The two calls

Probe first when the user wants to see what is available, or when you will
fetch more than once from the same post:

```bash
mfp probe https://www.instagram.com/p/SHORTCODE/ --json
```

Then fetch from the manifest the probe produced, which costs no second read
of the page:

```bash
mfp fetch --manifest ./probe.json --policy best --json
```

When the user just wants the file and there is nothing to choose, one call
does both:

```bash
mfp fetch https://www.instagram.com/p/SHORTCODE/ --json
```

Useful flags, all optional:

| Flag | Meaning |
|---|---|
| `--policy best` | Also `smallest`, or `max-height:1080`. **`max-height:N` compares the short side**, so `max-height:1080` includes a 1080×1920 Reel. |
| `--out D:/somewhere` | Override the configured output root. |
| `--select 0,2,5-7` | Which carousel items. **One post only** — the command refuses a multi-post run with exit 2. |
| `--dry-run` | Report the planned paths and chosen quality without transferring. Use this when the user asks "what would this download?" |
| `--allow-silent-video` | Only after a failure says so. When a post's manifest records **no audio track at all**, `fetch` refuses rather than write a file with no sound; this takes the picture anyway and reports `degradedReason`. Ask the user before using it — they are accepting a video with no sound. |
| `--audio-lang ja` | Take a specific language's audio when the video publishes several. **Leave it off unless the user asked for a dub**: the default is the language the video was recorded in, which is what anyone means by "download this video". |

## Reading the output

`mfp probe --json` prints one object with a `results` array, one row per URL,
each carrying a `manifest` or an `errorCode`. `mfp fetch --json` prints one
`FetchResult`:

- `posts[]` — one row per requested URL. **A URL that failed before it had
  any items appears only here**, so a post-level failure is invisible if you
  read `items` alone.
- `items[]` — one row per media file. `index` is the carousel slot and
  repeats across posts, so the identifying key is `(postId, index)`.
- `budget` — how many requests remain this hour.
- `stopReason` — `budget_exhausted`, `blocked`, or `user_cancelled` when the
  run stopped early.
- `degraded` / `degradedReason` on a manifest — the run got something weaker
  than asked for.
- `audio.language` / `audio.isOriginal` on an item — which language the saved
  sound is in, and whether it is the video's own. Both `null` on a video with
  one audio track, which is nearly all of them; `null` means the source said
  nothing, never "not a dub".

## What you must do with that

**Report `degradedReason` to the user in plain language.** It means they did
not get what they asked for. `ffmpeg_missing_progressive_only` means ffmpeg
is absent and the best already-muxed rendition was taken instead — tell them
installing ffmpeg would get the higher one. `no_audio_track_progressive_only`
means the higher rendition has no audio track this build can pair it with.
Since M10 pairs the audio stream at probe time this is now uncommon on
YouTube; when it does appear, it is a real gap in what the source offered,
not a ceiling this tool imposes.

**Say so when `audio.isOriginal` is `false`.** The user asked for a video and
got one whose sound is an AI dub in another language — a thing they cannot
tell from the filename, the subtitles, or the picture. One sentence naming
the language is enough. This is only ever the result of `--audio-lang`, so
if you did not pass it and see `false`, report it as a bug.

**Surface `stopReason` and `budget` verbatim.** Do not paraphrase a budget
number and do not decide on the user's behalf that it is fine.

**Never loop-retry on exit 4 or exit 7.** Exit 4 means the platform is
actively blocking or rate-limiting this tool, and exit 7 means the local
pacing guard has spent its hour. Retrying either is more traffic at exactly
the moment more traffic is harmful, and the guard exists to keep the user's
access working. Report it, say when `budget.nextAllowedAt` is, and stop.

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | Every requested item succeeded | Report the paths |
| 1 | Partial — at least one item failed, successes kept | Report both halves; read `posts[]` for which |
| 2 | Usage error | Fix the command; the message says what is wrong |
| 3 | Unsupported or unparseable URL — **or a post that simply has no media** | Read `errorCode`. `unsupported_url` means the URL shape is not supported; `no_media_in_post` means the post was read fine and holds nothing to download (an X post with only text or images). Either way: do not try variations, do not retry |
| 4 | Blocked or rate-limited upstream | **Stop.** Report it. No retry |
| 5 | Upstream structure change | The site changed shape; this needs a code fix, not a retry. Say so. Narrower than it used to be: "the post has no video" moved to exit 3, so exit 5 no longer fires on an ordinary text-only post |
| 6 | Missing dependency | Name the missing tool from the message; `mfp doctor` lists all four |
| 7 | Local fetch budget exhausted | **Stop.** Report `budget.nextAllowedAt`. No retry |

A mixed batch — several URLs failing for different reasons — is exit 1, and
`posts[]` carries each cause.

## The 延伸工具 — named here, taught elsewhere

Everything above is what `mfp` **is**: a URL goes in, files come out, and the
tool can say where they went. Four other tools ship in the same program. They
are listed rather than explained, and the distinction is deliberate — this
file used to teach all nineteen verbs, which meant an agent asked to explain
one Instagram post had to read the entire acquisition contract first.

| Tool | Verbs | Read this when |
|---|---|---|
| 引用長圖 | `stack` | The user wants a quote image made from a video's subtitles |
| 貼文解說 | `brief`, `brief-save` | The user wants to know what is in a post's pictures |
| 逐字稿 | `transcript`, `translate`, `correct`, `tidy`, `asr-*` | The user wants a video's words as text, or wants to work on a transcript |
| 文件翻譯 | `translate-doc` | The user has a `.txt`/`.md` to translate |

```bash
mfp agent-guide --extension brief
```

prints one of them. There is no flag that prints them all: the whole point of
the split is that you read the one the task needs.

Two verbs belong to the store rather than to any tool, so they stay here.
Has this been analysed, and where did it go:

```bash
mfp analyzed "<url or file>" --json
```

Put this build's directory on PATH:

```bash
mfp install-path
```

## Before you blame the tool

```bash
mfp doctor --json
```

Five checks, with versions and paths. **yt-dlp**, **ffmpeg** and **chrome**
are required, and any one of them missing is exit 6. **javascript-runtime**
and **asr** are capabilities that degrade rather than prerequisites that
fail: their absence is still exit 0, because most of this tool works without
either.

`gallery-dl` is **not** among them and has not been checked since 2026-08-30
(D-138). Nothing in this product invokes it, so installing it changes no
behaviour whatsoever. Do not tell a user to install it, and do not read its
absence — anywhere, including in an older checklist that still asks for it —
as a fault.

## Where this file came from, and what is on the machine

An installed machine has the desktop app, `mfp-sidecar.exe` (the GUI's local
server -- its only subcommand is `serve`) and `mfp.exe`, the CLI above.

Two things are **offered** by the installer as separate tick-boxes and may
have been declined: putting `mfp.exe`'s directory on PATH, and copying this
file into a skills directory. Neither is guaranteed, and no agent's
configuration is ever written without one of those ticks or an explicit
`mfp agent-register`.

So check rather than assume. This prints the contract and, beside it, where
the command actually is -- `which` is `null` when the tool is installed but
not on PATH, which is a different problem from not being installed:

```bash
mfp agent-guide --json
```

If it is not on PATH, the install directory is recorded in the Windows
uninstall registry under `InstallLocation` (DisplayName `媒體擷取`), and the
CLI is at `<InstallLocation>\resources\sidecar\mfp.exe`. Call it by full
path; do not go looking for a Python source tree.

`mfp install-path` and `mfp agent-register` exist so that the user -- or you,
when the user asks you to set this up -- can fix either gap without
reinstalling. Both are reversible with `--remove` and both preview with
`--dry-run`. Do not run them unprompted: they write into the user's
environment and into other programs' configuration.

Two further verbs exist and are **not yours to call**. `mfp serve` is the
local server the desktop app starts for itself, and a second one competes for
the same port and the same queue file. `mfp capture` records network fixtures
for this project's own test suite. Neither does anything a user asked for.
With those two named, this file now covers every verb in `mfp --help` — so a
verb you find that is not described above is a sign this file is out of date,
not a feature to improvise with.

## Things this tool will not do, by ruling

Do not attempt to work around any of these, and do not offer to:

- **It never logs in.** No credentials, no session cookies, no account.
- **It never crawls an account or feed.** One post per URL.
- **There is no preview-image fallback.** If the real media cannot be read
  the command fails, deliberately: handing back a thumbnail and calling it
  success is worse than a failure the user can act on.
- **It paces itself.** The hourly budget is a bot-protection guard, not a
  quota to be maximised. Never suggest raising it to go faster.
