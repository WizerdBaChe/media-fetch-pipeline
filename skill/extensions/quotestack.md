# 引用長圖 (quotestack) — `mfp stack`

> One 延伸工具, one file. Read this only when the task is this tool's;
> the core contract (`SKILL.md`) is what every task needs and is kept
> short so it can be. `mfp agent-guide --extension quotestack` prints this.

## Making a stacked quote image

`mfp stack` turns a video into one tall image: the first moment keeps the
whole frame, and every line after it contributes only its subtitle band. It
takes a local file, or a post URL — a URL is fetched through the same
pipeline and budget `fetch` uses, so it is a second thing to do with a
download, never a second way to pull one.

It needs to be told where the words come from, and there are only two
answers. Pass exactly one:

```bash
mfp stack video.mp4 --subs captions.srt --from 3:50 --to 4:40 --json
```

`--subs` when the words exist as text: a caption file (`.srt`/`.vtt`), a
plain transcript (`.txt`), **or a post URL to fetch the captions from** —
which is how a video already on disk reaches its own caption track. Fetched
captions are saved beside the video and reused on the next run, so tuning a
window costs no further platform requests. Use `--offset` when the video is
a clip cut from the source the captions were timed against.

```bash
mfp stack reel.mp4 --roi 1300:1545 --json
```

`--roi TOP:BOTTOM` when the subtitles are already burned into the picture.
**This is required on that path, not an optional refinement.** Automatic
band detection was tried three ways against a real clip and missed every
time; the command refuses rather than guessing. Add `--preview` to get a
frame with the band drawn on it, and check that before trusting a long
image built on those bounds.

**`--roi` is in frame pixels, so it belongs to one video at one
resolution.** Carrying a band from another clip is the commonest way to
waste a run: on a window longer than two minutes the command samples eight
frames first and refuses in seconds when none of them holds subtitle-bright
pixels in those rows, naming `--subs` as the other path — a video whose
captions are a separate track has nothing to detect.

**Every pass is bounded by `--from`/`--to`, and scanning time is
proportional to the window.** Give one on a long video; the whole file is
the default and it is rarely what was wanted.

Anything in the look — font size, lines per strip, transcript block length,
band padding — is reachable with `--set KEY=VALUE` (repeatable). An unknown
key answers with the whole list and its defaults, which is also how to
discover them.

Reading the result: `strips` is how many made it into the image,
`droppedBlank` is how many detected changes turned out to be gaps between
subtitles rather than subtitles, and **`truncated` is how many further
strips `--max-strips` cut off** — report that number, it means the image
stops early. Exit 3 means the file was read fine and holds nothing to stack
(no video stream, no caption lines in the window, no text in the band);
exit 2 means the invocation needs fixing.
