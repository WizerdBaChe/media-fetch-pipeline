"""The translation half, in the same borrowed interpreter as recognition.

`asr/` is where scripts that run in the EXTERNAL engine environment live.
Recognition was the first of them and gave the directory its name; this is
the second, and the name is left alone rather than churned through
`mfp.spec`, `runner_path()` and a packaging change for no user-visible gain.

Like `runner.py`, this file is NOT part of the `mfp` package and must never
import from it. Unlike `runner.py`, it needs **nothing installed that
recognition did not already install**: CTranslate2 is what faster-whisper
runs on, and `tokenizers` is what faster-whisper reads Whisper's vocabulary
with. Verified on this machine 2026-08-28 -- ctranslate2 4.7.1 and tokenizers
0.22.2 present, transformers and sentencepiece absent -- and the tokenizer
order below is arranged around exactly that: the file a model is most likely
to ship (`tokenizer.json`, which `OpenNMT/nllb-200-distilled-1.3B-ct2-int8`
does ship and a SentencePiece file it does not) is the one that costs nothing.

The contract, which `src/mfp/translate.py` is the only reader of:

  stdout  ONE JSON object, at the end, and nothing else ever.
  stderr  Progress as `@mt <json>` lines, plus human text passed through.
  exit    0 on success. 2 for "this environment cannot translate" (missing
          package, missing model, unusable device) so the caller can tell an
          environment problem from a text it could not handle (1).

**The language codes are checked against the model before any work starts.**
NLLB names languages as FLORES-200 codes (`zho_Hant`), they are ordinary
tokens in the model's own vocabulary, and a code the model does not have
produces confident nonsense rather than an error. Asking first turns a
silently wrong transcript into one refusal naming the codes that do exist --
the same reason `runner.py` detects the language before choosing a prompt
instead of assuming one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

#: Exit code meaning "this environment cannot translate", kept distinct from
#: 1 ("that text could not be handled") so the two produce different advice.
EXIT_UNUSABLE = 2

#: End-of-sentence token. NLLB, M2M-100 and Marian all use it, and the source
#: sequence is `[src_lang] ...subwords... </s>` (OpenNMT's own CTranslate2
#: tutorial, read 2026-08-28). Getting this wrong does not raise -- it
#: degrades the translation -- which is why it is written down here with
#: where it came from.
EOS = "</s>"


def note(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def progress(**fields: object) -> None:
    print("@mt " + json.dumps(fields, ensure_ascii=False), file=sys.stderr, flush=True)


def emit(payload: dict) -> None:
    """The result, on stdout, FLUSHED.

    Same reason `asr/runner.py` flushes: stdout is block-buffered when it is
    a pipe, and CTranslate2's CUDA teardown can kill this process after the
    work is finished and correct. An unflushed buffer dies with it, and the
    caller sees an engine that produced no output.
    """
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def fail(message: str, *, unusable: bool = False) -> int:
    emit({"ok": False, "error": message})
    note(message)
    return EXIT_UNUSABLE if unusable else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Translate lines of text.")
    parser.add_argument("--model", required=True, help="A CTranslate2 translation model directory")
    parser.add_argument("--input", required=True,
                        help="JSON file holding {\"lines\": [...]}. A file rather "
                             "than argv because a transcript does not fit in a "
                             "command line")
    parser.add_argument("--from", dest="source_lang", required=True,
                        help="FLORES-200 code of the source, e.g. zho_Hant")
    parser.add_argument("--to", dest="target_lang", required=True,
                        help="FLORES-200 code of the target, e.g. eng_Latn")
    parser.add_argument("--device", default="auto", choices=["auto", "cuda", "cpu"])
    parser.add_argument("--compute-type", default="auto")
    parser.add_argument("--beam-size", type=int, default=4)
    parser.add_argument("--max-batch", type=int, default=16)
    return parser


def resolve_device(requested: str) -> tuple[str, str]:
    """`(device, why)`. Falls back to CPU rather than failing.

    Same rule recognition follows: translating slowly is a different outcome
    from not translating. Translation is also far lighter than recognition,
    so CPU is a genuinely usable answer here rather than a formality.
    """
    if requested == "cpu":
        return "cpu", "asked for"
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001 - any failure means "no CUDA"
        if requested == "cuda":
            raise RuntimeError(f"CUDA was asked for and is not usable: {exc}") from exc
        return "cpu", f"no usable CUDA ({exc})"
    if count > 0:
        return "cuda", f"{count} CUDA device(s)"
    if requested == "cuda":
        raise RuntimeError("CUDA was asked for and no CUDA device was found")
    return "cpu", "no CUDA device found"


class Pieces:
    """Text <-> subword tokens, however this model happens to carry them.

    Two implementations behind one shape. Which one ran is REPORTED in the
    result, because "it worked but needed a package you had to install" and
    "it worked with what was already here" are different facts about the
    user's machine, and the settings panel shows both.
    """

    def __init__(self, kind: str, encode, decode, has_token) -> None:
        self.kind = kind
        self.encode = encode
        self.decode = decode
        self.has_token = has_token


def load_pieces(model_dir: str) -> tuple[Pieces | None, str | None]:
    """`(pieces, problem)`. Preference is COST order, not accuracy order."""
    import os

    tokenizer_json = os.path.join(model_dir, "tokenizer.json")
    if os.path.isfile(tokenizer_json):
        try:
            from tokenizers import Tokenizer
        except ImportError as exc:
            return None, (
                f"this interpreter cannot import tokenizers ({exc}). "
                f"Install it with:  {sys.executable} -m pip install tokenizers"
            )
        tok = Tokenizer.from_file(tokenizer_json)

        def encode(text: str) -> list[str]:
            # `add_special_tokens=False` because the language token and the
            # EOS are added by us. A fast tokenizer's post-processor bakes in
            # whatever source language it was saved with, so letting it add
            # them would silently label every input as that language.
            return tok.encode(text, add_special_tokens=False).tokens

        def decode(tokens: list[str]) -> str:
            ids = [tok.token_to_id(t) for t in tokens]
            return tok.decode([i for i in ids if i is not None], skip_special_tokens=True)

        return Pieces("tokenizer.json", encode, decode, lambda t: tok.token_to_id(t) is not None), None

    for name in ("sentencepiece.bpe.model", "source.spm", "spiece.model"):
        path = os.path.join(model_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            import sentencepiece
        except ImportError as exc:
            return None, (
                f"this model's tokenizer is {name}, which needs sentencepiece "
                f"({exc}). Install it with:  {sys.executable} -m pip install "
                f"sentencepiece -- or use a model that ships tokenizer.json, "
                f"which needs nothing extra"
            )
        sp = sentencepiece.SentencePieceProcessor(model_file=path)

        return (
            Pieces(
                name,
                lambda text: sp.encode(text, out_type=str),
                lambda tokens: sp.decode(tokens),
                # SentencePiece has no notion of the added language tokens,
                # so it cannot answer -- and saying "unknown" is the correct
                # output for a check that cannot see the property.
                lambda _token: True,
            ),
            None,
        )

    return None, (
        "this model folder has no tokenizer file (tokenizer.json or a "
        "sentencepiece .model). Download it from the same page as the model "
        "and put it in the same folder"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        import ctranslate2
    except ImportError as exc:
        return fail(
            f"this interpreter cannot import ctranslate2 ({exc}). "
            f"Install it with:  {sys.executable} -m pip install ctranslate2",
            unusable=True,
        )

    try:
        with open(args.input, "r", encoding="utf-8") as handle:
            lines = json.load(handle).get("lines") or []
    except (OSError, json.JSONDecodeError) as exc:
        return fail(f"could not read the lines to translate: {exc}")
    if not lines:
        return fail("there was nothing to translate")

    pieces, problem = load_pieces(args.model)
    if pieces is None:
        return fail(problem or "no usable tokenizer", unusable=True)
    note(f"tokenizer: {pieces.kind}")

    # Before anything expensive. A language code the model does not carry is
    # not an error anywhere downstream -- it is a token that gets embedded as
    # unknown and produces fluent nonsense.
    for label, code in (("source", args.source_lang), ("target", args.target_lang)):
        if not pieces.has_token(code):
            return fail(
                f"this model does not know the {label} language code "
                f"{code!r}. NLLB models use FLORES-200 codes such as "
                f"zho_Hant, zho_Hans, eng_Latn, jpn_Jpan",
                unusable=True,
            )

    try:
        device, why = resolve_device(args.device)
    except RuntimeError as exc:
        return fail(str(exc), unusable=True)
    compute_type = args.compute_type if args.compute_type != "auto" else (
        "float16" if device == "cuda" else "int8"
    )
    note(f"device: {device} ({why}), compute type: {compute_type}")

    try:
        t0 = time.perf_counter()
        translator = ctranslate2.Translator(
            args.model, device=device, compute_type=compute_type
        )
    except Exception as exc:  # noqa: BLE001
        return fail(f"could not load the translation model: {exc}", unusable=True)
    note(f"model loaded in {time.perf_counter() - t0:.1f}s")
    progress(phase="loaded", total=len(lines))

    # `[src_lang] ...subwords... </s>` with `target_prefix=[[tgt_lang]]` is
    # the sequence OpenNMT's own CTranslate2 tutorial specifies for NLLB.
    source = [[args.source_lang] + pieces.encode(text) + [EOS] for text in lines]
    target_prefix = [[args.target_lang]] * len(source)

    out: list[str] = []
    t0 = time.perf_counter()
    # Batched by hand rather than handed over whole, for one reason: a
    # thousand-line transcript is minutes, and progress only exists if
    # something comes back before the end.
    for start in range(0, len(source), args.max_batch):
        chunk = source[start:start + args.max_batch]
        try:
            results = translator.translate_batch(
                chunk,
                target_prefix=target_prefix[start:start + len(chunk)],
                beam_size=args.beam_size,
                max_batch_size=args.max_batch,
            )
        except Exception as exc:  # noqa: BLE001
            return fail(f"the translation engine failed: {exc}")
        for result in results:
            tokens = list(result.hypotheses[0])
            if tokens and tokens[0] == args.target_lang:
                tokens.pop(0)
            out.append(pieces.decode(tokens).strip())
        progress(phase="line", done=len(out), total=len(lines))
    wall = time.perf_counter() - t0

    emit({
        "ok": True,
        "lines": out,
        "engine": {
            "name": "ctranslate2",
            "model": args.model,
            "tokenizer": pieces.kind,
            "device": device,
            "computeType": compute_type,
            "sourceLang": args.source_lang,
            "targetLang": args.target_lang,
            "wallSeconds": round(wall, 2),
        },
    })
    note(f"{len(out)} lines in {wall:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
