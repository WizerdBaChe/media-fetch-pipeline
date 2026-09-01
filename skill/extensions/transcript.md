# 逐字稿 (transcript) — `mfp transcript` and what acts on one

> One 延伸工具, one file. Read this only when the task is this tool's;
> the core contract (`SKILL.md`) is what every task needs and is kept
> short so it can be. `mfp agent-guide --extension transcript` prints this.

## Reading a video's words

`mfp transcript` hands back the captions as text. It is the answer to "what
does this video say", to "which part of it should I quote", and to a user who
has a video and wants the words out of it — none of which needed a stacked
image.

```bash
mfp transcript https://www.youtube.com/watch?v=... --from 3:50 --to 4:40
```

The argument takes any of four shapes and you do not have to decide which you
are holding: a post URL, a media file already on disk whose captions were
saved beside it, a media file whose words exist only as **sound**, or a
caption file. A URL is fetched once and cached under the output root, so
asking again costs no platform request.

```bash
mfp transcript "D:/voice/meeting.m4a" --out meeting.txt
```

That third shape is speech recognition, and it is the one with a cost. Any
container ffmpeg can decode is accepted — mp3, m4a, mp4, mov, wav, flac,
opus, wma — including what an iPhone hands over; only DRM-protected files are
out, and no tool here can open those. The result is written as an `.srt`
under `<outputRoot>/_captions/` and read back like any other caption file, so
a second run on the same file costs nothing and `mfp stack --subs` can quote
it with no special case.

**Order of preference, and it is not adjustable by accident:** a caption
track somebody WROTE beats a machine listening to the same audio, and it also
costs seconds instead of minutes. Recognition happens when no such track
exists — which for a local audio file is always.

| Flag | Meaning |
|---|---|
| `--list` | Report which caption tracks exist and stop. One metadata read, no download. Use this before `--sub-lang` when the video's language is in doubt. |
| `--sub-lang` | Which track. The default `orig` means **the language actually spoken**, not a machine translation of it — asking for `en` on a Mandarin video returns fluent English nobody said. Some videos do not report a language; then `orig` refuses and names this flag. |
| `--format` | `timed` (default) one line per caption with its timestamp; `text` paragraphs, for reading or pasting; `srt` the caption file itself. |
| `--from` / `--to` | Same meaning as on `mfp stack`: a caption still on screen when the window opens comes with it. |
| `--out` | Also write the rendered text to a file. |
| `--refresh` | Re-fetch instead of using the cached caption file — or transcribe again, for a file that was listened to. |
| `--recognize` | `auto` (default) listen only when no captions exist; `always` listen even when they do, for a video whose auto-generated track is worse than a fresh transcription; `never` refuse, and say so. |
| `--asr-lang` | Spoken language for recognition, e.g. `zh`. The default detects it from the audio; naming it stops a bilingual recording being labelled by its first sentence. |
| `--asr-model` | Override the configured recognition model for one run. |

**The timestamps in `timed` are `--from`'s own grammar.** That is the point of
the default: read the transcript, pick the passage, and hand the same strings
straight to `mfp stack --from ... --to ...`. The run prints that command on
stderr with the caption file already filled in, so quoting what you just read
costs no second fetch.

`--json` gives `{"transcript": {source, kind, language, lineCount, lines:
[{at, text}]}}`. **`kind` is worth reporting to the user**: `written` means a
human wrote these captions, `automatic` means the platform's machine
transcribed them, `recognized` means **this machine listened to the audio**,
and those are not the same evidence. Never present a `recognized` transcript
as a quotation of record without saying where it came from.

Exit 3 with `no_captions_available` means the video has no caption track at
all — not written, not automatic — and nothing here could listen to it
either. If the words are burned into the picture, `mfp stack --roi` reads
those instead; there is nothing here to retry.

Exit 6 with `asr_unavailable` means recognition is not set up on this
machine. It is a separate install by design: the engine and its model are
several GB against a ~107 MB product, so they are not shipped. This is a
setup step, not a failure, and everything else in this tool works without it.

```bash
mfp asr-status --json
```

`{ready, state, headline, detail, steps, engine, home, models}`. Exit 0 even
when nothing is set up: not being able to transcribe is a state, not a
failure of the question.

`capabilities` holds one entry per thing this machine may or may not be able
to do — `recognition` and `translation` — and each carries its own `state`:
`no_engine` (no Python with faster-whisper), `engine_broken` (one that cannot
import it), `no_model` (engine fine, no weights of the right KIND in the
model folder). Report the entry's `detail` sentence rather than inventing an
instruction — it names the user's actual situation. Do not read the top-level
`ready` as "everything works": it is recognition alone, because translation
is opt-in and its absence breaks nothing.

`mfp asr-add <folder> --mode copy|move|link` brings a downloaded model in and
files it by what it IS; `mfp asr-use <name>` selects one that is already
there and writes it into the setting its KIND belongs in — recognition or
translation is read off the model, never asked for, because the folder
somebody named has already answered it. The desktop app has both under
設定 → 語音辨識, which is where a human should be sent.

## Translating a transcript

**A separate verb over a caption file that already exists.** There is no flag
on `transcript` that translates, and that is deliberate: translating is a
second decision, made after somebody has read the original.

```bash
mfp translate "D:/out/_captions/talk-1a2b3c4d.zh.srt" --to en --json
```

`{source, sourceLanguage, targetLanguage, lineCount, suspectLines,
clauseSplits, record, engine}`. The cue timings are kept, so the result goes
straight into `mfp stack --subs` like any other caption file. `record` names
a `<stem>.<flores>.translation.json` written beside the output, holding the
same facts for whoever finds the file later without this JSON in hand. `--to` takes an ISO code (`en`) or a FLORES-200 code
(`eng_Latn`); FLORES is what the models use and it distinguishes `zho_Hant`
from `zho_Hans`, which ISO does not.

Exit 6 with `asr_unavailable` here means the TRANSLATION model is missing —
a different, separate model from the recognition one. Read the message: it
says which of the two is not set up. Never translate a transcript the user
did not ask you to translate.

**Chinese output can lose a clause. Both verbs split sentences to avoid it,
and the mitigation is partial.** Measured 2026-08-30 against
`nllb-200-distilled-1.3B-ct2-int8`, the model this product installs:

| in | out (before the split) |
|---|---|
| `Previously it chose by bitrate alone, which was a tie on YouTube.` | 之前它只選取比特速率, |
| `Previously it chose by bitrate alone.` | 之前它只選用比特速率. |
| `It was a tie on YouTube.` | 在YouTube上有無分數. |

Each clause translates alone; joined by a comma, one disappears. Beam size,
`max_decoding_length`, `length_penalty` and `min_decoding_length` were each
tried and changed nothing — so both verbs now cut at clause boundaries and
send the pieces separately, which recovers them.

Two things this does NOT fix, and you must not present output as complete
without knowing them:

- **A sentence with no comma can still lose a phrase.** There is no boundary
  to cut at. Measured: `The downloader now prefers the video's original
  audio.` → 影片的原始音效更受歡迎. — the subject is gone.
- **It is a Chinese-target fault.** The same sentences into `fra_Latn`,
  `deu_Latn` and `jpn_Jpan` came back complete; `zho_Hant` is the worst and
  `zho_Hans` is between. `zho_Hant` is this product's default target.

Both translate verbs report this in `--json` as `suspectLines` (1-based) and
on stderr as `N line(s) may have lost a clause`. It fires on one fingerprint:
a source that ends a sentence and a translation that ends a clause.

**A quiet run is not a clean run.** An empty `suspectLines` does not mean the
translation is complete — a dropped MIDDLE clause ends correctly and the
check cannot see it. Report the ones it names, and say the source is the
record either way.

## Fixing misheard terms

Shows what it would change and writes nothing:

```bash
mfp correct "D:/out/逐字稿/talk_2026/字幕檔/talk.zh.srt"
```

Writes the corrected copy beside the original:

```bash
mfp correct "D:/out/逐字稿/talk_2026/字幕檔/talk.zh.srt" --apply --json
```

**Nothing can be substituted that is not in the user's glossary, and the
glossary starts empty.** That is structural, not a setting: the whitelist IS
the candidate set, so an empty one proposes nothing at all. The engine's own
confidence is a second and WEAK gate in series with it — about 60% precision
on its own — and is never a licence to write a word the user has not
declared.

This is how it learns:

```bash
mfp correct --enrol 基板=機板
mfp correct --forget 基板
mfp correct --list
```

`--enrol TERM=WRONG` names the right term and a wrong one you actually saw,
and records both spellings, so correcting a term by hand
once makes the next run match it exactly. `--forget` only stops a term being
proposed again; it never revisits anything already written. `--exact-only`
turns off phonetic guessing and offers enrolled spellings alone. None of
these need a transcript — glossary housekeeping has no file to speak of.

`--apply` writes the corrected transcript and a record of every substitution
into the analysis folder the transcript belongs to, and **never touches the
original**. Before writing, it asserts that the cue count and every timestamp
are unchanged: substitution is visible in a diff, but deletion is the class
nothing downstream can see, so the check happens here. The whole set of files
gets ONE serial, so a record can never be filed under a different serial than
the transcript it describes.

Report what it proposes rather than applying on the user's behalf. The order
— propose, disclose, then write — is the feature: the version that decided
and applied in one step damaged correct text on real data every time it
fired (D-115), which is why this one prints a diff and stops.

Note that the diff goes to **stderr** and the JSON to stdout, like everywhere
else here. The desktop app has the same thing under the transcript as
智慧校正, which is where a human should be sent.

## Making a reading copy (filler removal)

Shows what it would remove and writes nothing:

```bash
mfp tidy "D:/out/逐字稿/talk_2026/字幕檔/talk.zh.srt"
```

Writes the three files:

```bash
mfp tidy "D:/out/逐字稿/talk_2026/字幕檔/talk.zh.srt" --apply --json
```

Drops cues that are **nothing but** filler — 嗯, 好, 好好好, OK好 — and
keeps everything else. It never matches inside a sentence: 角度調**好**的話,
一個**好**像, 那個凹凸鏡 all survive, because the cue must be entirely
consumed by declared terms. Measured on a real 158-cue transcript: 41 removed
(26%), zero false positives.

**Nothing is removed that is not on the user's own list**, and the list starts
empty. `mfp tidy --add-common` fills it with the usual Chinese and English
fillers; `--add <詞>` / `--forget <詞>` / `--list` manage it. An empty list
removes nothing — that is structural, not a setting.

`--apply` writes `<stem>.tidy.srt`, `<stem>.tidy.txt` and `<stem>.tidy.json`
beside the transcript and never touches the original. The `.json` records
every removed cue with its timings, and the original can be rebuilt from the
tidied copy plus that record — which `apply` asserts before writing anything.

`{source, fillers, summary:{cues,removed,kept,share}, removals[], applied,
written}`. Report `summary.removed` and `summary.share` to the user: they are
being handed a shorter transcript and should know how much shorter.

What it does NOT do: remove a filler **inside** a sentence. 那個 appears 28
times in that transcript and every one is a real demonstrative; 好 is content
in seven of its 18 embedded uses. Deciding those needs to know what a word is
doing, which needs a tagger this build does not have. Do not ask for it with
a longer list — the whole-cue rule is what makes the list safe.

## Doing both in one run

The two above are stages of one pipeline, not rival features. Either verb
takes the other as a flag, and both spellings produce byte-identical output:

```bash
mfp correct "…/talk.zh.srt" --apply --tidy
mfp tidy    "…/talk.zh.srt" --apply --correct
```

One set of files, named for the stages that ran:
`<stem>.corrected.tidy.srt`, `.txt`, `.json` and `.diff.txt`. No intermediate
copy is written.

**Do not chain the two verbs by hand to get this.** It appears to work and
costs three things: the same content acquires two names depending on which
order you ran them in, seven files land where four were wanted, and the two
records end up in different coordinate systems — run tidy first and the
corrections record numbers the cues of the TIDIED file, which is not a file
the user has. Worse, each verb proves its own record can rebuild its INPUT,
and in a chain that input is the intermediate, so 「the record can rebuild
the original」 quietly stops being true.

The stage order is a property of the pipeline: correction runs first because
it cannot move a cue, which is what keeps both stage records numbered in the
source transcript's own cues (`cueSpace: "source"` in the record says so).
Asking for them the other way round changes nothing. Before writing, the
whole composition is reversed and must reproduce the original transcript
exactly, or nothing is written at all (`refine_refused`).

The desktop app has both under the transcript as one panel with two
checkboxes, which is where a human should be sent.
