# 文件翻譯 (document translation) — `mfp translate-doc`

> One 延伸工具, one file. Read this only when the task is this tool's;
> the core contract (`SKILL.md`) is what every task needs and is kept
> short so it can be. `mfp agent-guide --extension translatedoc` prints this.

## Translating a document

**A different verb, because a document is not a transcript.** `translate`
treats one line as one utterance, which is right for captions and wrong for
prose: a hard-wrapped paragraph reaches the model in fragments, blank lines
vanish, and a `## heading` comes back translated including its hashes.

```bash
mfp translate-doc "D:/notes/report.md" --from en --to zho_Hant --json
```

`{source, sourceLanguage, targetLanguage, lineCount, suspectLines,
clauseSplits, record, blocks, verbatimBlocks, sentences, engine}`. Takes
`.txt`, `.md`, `.markdown` — hand it a `.srt` and it tells you to use
`translate`, which keeps the timings. `record` names the
`<stem>.<flores>.translation.json` written beside the output; its `summary`
carries the block counts too.

`--from` is REQUIRED here and optional on `translate`. A transcript this
project wrote carries its language in its filename; an arbitrary document
does not, and guessing produces fluent output that is not a translation of
anything.

What never reaches the model: fenced and indented code, front matter,
tables, horizontal rules, inline code spans, link targets, autolinks and
bare URLs. Headings, list markers and blockquote markers are kept and
reattached. `verbatimBlocks` counts what was held back, so a run that
reports 0 on a document full of code did not parse it as Markdown — check
the suffix.

The output is refused rather than written if the block count changed, a
verbatim block did not survive byte-identical, or a paragraph that had words
came back empty. A refusal here is the tool catching a deletion, not a bug
to retry around.
