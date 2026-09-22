"""Turn a captured page into a fixture that is safe to commit.

The problem this solves: `extract.py` needs a real page to be right or wrong
about, but a real Instagram page is somebody's post — their handle, their
caption, their face — plus signed CDN URLs. Committing that verbatim puts
another person's content in the repository forever.

The observation that makes a redacted fixture *sufficient*: the extractor
never reads any of that. It reads key names, nesting, `media_type`, candidate
counts, the presence of `oh=`/`oe=`, and the DASH manifest's structure. So the
identifying values can all be replaced as long as the shape survives.

"As long as the shape survives" is not a claim to be trusted — it is checked,
in **both** directions. `verify_redaction` runs the extractor over the two
versions and refuses the redacted copy unless it produces the same items and
variant counts (a fixture that changed what it tests is worse than no
fixture) **and** unless none of the original's signature values survive.

The second half was missing at first, and its absence is instructive: with
only the shape check, a redaction that changed *nothing at all* scored a
perfect pass. Measured on the first two real captures — 110 of 126 signature
values carried straight through, reported as verified.

Deliberately operating on the raw text rather than parse-and-reserialize: the
page carries each media URL **twice**, once clean in the JSON and once
entity-encoded in the DOM attributes, and that second copy is what TRAP-4 is
about. Reserializing the JSON would drop it, and the fixture would quietly
stop being able to prove that the entity-mangled form is rejected.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from mfp.adapters.instagram.extract import extract_items, locate_post_node

#: JSON keys whose values identify a person or reproduce their words.
#: `accessibility_caption` is here even though `extract.py` reads it: the
#: tests assert that a note is present, never what it says.
REDACTED_KEYS: tuple[str, ...] = (
    "username",
    "full_name",
    "fullName",
    "biography",
    "caption",
    "text",
    "accessibility_caption",
    "alt_text",
    "profile_pic_url",
    "profile_pic_url_hd",
    "owner",
    "email",
    "phone_number",
)

#: A fixed, decodable `oe=` so `expiresAt` still parses in the fixture.
#: 2030-01-01T00:00:00Z, comfortably in the future so a redacted fixture does
#: not start failing an expiry check on some future test run.
PLACEHOLDER_OE = "70DC1880"

#: Matches a CDN URL in BOTH forms it appears in on the page: plain in the
#: DOM attributes, and with every `/` escaped as `\/` inside the script JSON
#: (TRAP-1). The first version of this pattern required a literal `//` and
#: excluded backslashes from the tail, so it silently skipped every URL in
#: the JSON -- which is where nearly all of them live. Measured on the first
#: two real captures (2026-08-16): 110 of 126 signature values survived into
#: the "redacted" copy, and the self-check passed anyway, because the
#: extractor unescapes before reading and therefore saw an unchanged shape.
#: The tail accepts ANY backslash escape, not just `\/`. Instagram writes
#: `&` as `&` and `%` as `%` inside the JSON, and a tail that only
#: understood `\/` stopped dead at the first of those -- rewriting the path
#: while leaving the rest of the query, `oh=` included, untouched. That is
#: how 94 signature values survived a pass that had already been "fixed"
#: once (2026-08-16).
#: The tail crosses `\/` and `\uXXXX`, because Instagram writes `&` as
#: `&` and `%` as `%` inside a URL -- but NOT `<`, `>` or
#: `"`, which are the escaped `<`, `>` and `"` that delimit whatever
#: contains the URL. Accepting every escape indiscriminately made the match
#: run straight through `</BaseURL>` and eat the rest of the DASH
#: manifest: the leak went to zero and the Reel lost 4 of its 7 variants
#: (measured 2026-08-16, caught by the shape half of the same check).
_MEDIA_HOST_RE = re.compile(
    r"https?:(?:\\?/){2}[A-Za-z0-9.\-]*(?:cdninstagram\.com|fbcdn\.net)"
    r"(?:\\/|\\u00(?!3[CcEe]|22)[0-9A-Fa-f]{2}|[^\s\"'<>)\\])*"
)

#: Every form of `&` the page uses to separate query parameters.
_PARAM_SEPARATOR_RE = re.compile(r"&amp;|\\u0026|&")

#: Every `oh=` value in the source. Used by the leak check below, in both the
#: escaped and plain forms.
_SIGNATURE_VALUE_RE = re.compile(r"[?&](?:amp;)?oh=([A-Za-z0-9_\-]{6,})")

#: Keys whose VALUES are identifiers that also occur elsewhere in the page --
#: in the canonical URL, the `og:` tags, profile links, alt text. Redacting
#: them only where the key names them leaves the handle trivially
#: recoverable: measured on the first real capture, all 17 usernames survived
#: a pass that had replaced 102 values.
IDENTIFIER_KEYS: tuple[str, ...] = ("username", "full_name", "fullName")

#: Below this length a global replace risks hitting unrelated text.
MIN_IDENTIFIER_LENGTH = 4

#: `"key": "value"` with JSON escaping, value captured lazily so an escaped
#: quote inside it does not end the match early.
def _key_value_re(key: str) -> re.Pattern[str]:
    return re.compile(rf'("{re.escape(key)}"\s*:\s*)"((?:[^"\\]|\\.)*)"')


@dataclass(frozen=True)
class RedactionReport:
    urls_rewritten: int
    values_replaced: int
    items_before: int
    items_after: int
    variants_before: int
    variants_after: int
    #: Signature values from the original still present in the redacted copy.
    #: Must be zero. This half of the check was missing at first and its
    #: absence is exactly why the leak went unnoticed: the shape check passed
    #: because the extractor unescapes before reading, so it saw an unchanged
    #: page -- unchanged being the problem.
    secrets_leaked: int = 0

    #: Whether the post the page canonicalises to can still be located, in
    #: each copy. The shape check for a page with no media: at zero items
    #: `items_before == items_after` is `0 == 0` and proves nothing, so
    #: without this a text-only page had no observable shape at all and was
    #: refused on that basis (`items_before > 0`, until 2026-09-11). That
    #: refusal is why no capture of the commonest Threads post -- a text one
    #: -- has ever been committed, and why the class stayed invisible to the
    #: suite right up until it produced a stranger's video (P-88).
    post_located_before: bool = True
    post_located_after: bool = True

    @property
    def preserved(self) -> bool:
        """Safe to commit: the shape survived AND the identity did not.

        "The shape" has to mean something at zero items, or the gate is
        answering a question it cannot see. For a page with media that is the
        item and variant counts; for a page without, it is whether the post
        is still findable. Both are the same claim -- the redacted copy
        extracts what the original did.

        The last clause is the other half and is not symmetry: a page with no
        items AND no post has nothing for either check to be about, so every
        equality above holds trivially. Committing that is committing an
        empty fixture, which passes forever and tests nothing. Something has
        to be there before "it survived" can be said at all.
        """
        observable = self.items_before > 0 or self.post_located_before
        return (
            observable
            and self.items_before == self.items_after
            and self.variants_before == self.variants_after
            and self.post_located_before == self.post_located_after
            and self.secrets_leaked == 0
        )


def _placeholder_url(url: str) -> str:
    """Rewrite a CDN URL, keeping every property the extractor tests.

    Kept: scheme, host, extension, the parameter names and their order, and a
    decodable `oe=`. Replaced: the path, and every parameter value. The
    separator style is preserved as found, so the entity-encoded copy stays
    entity-encoded and goes on proving that TRAP-4 is rejected.
    """
    if "/v/redacted/" in url or r"\/v\/redacted\/" in url:
        # Already scrubbed. Re-running must not churn the fixture: the digest
        # is derived from the URL, so a second pass would rewrite every line
        # and make the diff of a re-capture unreadable.
        return url

    # The JSON copy arrives with every `/` escaped (TRAP-1). Work on the
    # plain form and put the escaping back, so the file stays valid JSON and
    # the two copies of the same asset are both rewritten.
    escaped = "\\/" in url
    plain = url.replace("\\/", "/") if escaped else url

    found = _PARAM_SEPARATOR_RE.search(plain)
    separator = found.group(0) if found else "&"
    head, _, query = plain.partition("?")
    digest = hashlib.sha1(plain.encode("utf-8")).hexdigest()[:12]
    extension = head.rsplit(".", 1)[-1] if "." in head.rsplit("/", 1)[-1] else "jpg"
    scheme_host = head.split("/")[0] + "//" + head.split("/")[2]
    rebuilt = f"{scheme_host}/v/redacted/{digest}.{extension}"

    def restore(text: str) -> str:
        return text.replace("/", r"\/") if escaped else text

    if not query:
        return restore(rebuilt)

    parts = []
    for pair in _PARAM_SEPARATOR_RE.split(query):
        name, _, _value = pair.partition("=")
        if not name:
            continue
        if name.endswith("oe"):  # covers `oe` and the mangled `amp;oe`
            parts.append(f"{name}={PLACEHOLDER_OE}")
        elif name.endswith("oh"):
            parts.append(f"{name}=00_REDACTED")
        elif name.endswith("stp"):
            # Kept verbatim, and this is the rule this whole module is built
            # on rather than an exception to it: `stp` is what the CDN did to
            # the image -- a crop box in relative coordinates and a resize
            # token, `c0.140.1122.1122a_dst-jpg_e35_s640x640_tt6` -- and the
            # extractor READS it (`parse_stp`), to split crops from
            # renditions and to recover a size the candidate does not
            # declare. Blanking it to `x` destroyed exactly that: the
            # committed carousel fixture reports 13 renditions per item where
            # the page has 6 renditions and 7 crops, and the same page's
            # candidates carry no `width` at all, so nothing else could tell
            # them apart. `verify_redaction` did not catch it because at
            # capture time (2026-08-16) the extractor had no crop split to
            # lose, and the wrong number has been the pinned expectation
            # since -- P-76 again, one layer down from where it was found.
            # Identity: none. A pixel box is not a person.
            parts.append(pair)
        else:
            parts.append(f"{name}=x")
    return restore(rebuilt) + "?" + separator.join(parts)


def redact_html(html: str) -> tuple[str, int, int]:
    """Return `(redacted, urls_rewritten, values_replaced)`.

    **Identity is preserved as a RELATION and destroyed as a value.** Every
    distinct handle gets its own `[user-N]`; the handle itself never survives.
    Until 2026-09-11 they all collapsed onto one literal `redacted_user`, and
    that is not a weaker fixture, it is a fixture that answers a question
    WRONGLY: `user == reply_to_author` became true for every node on the
    page, so the committed copy of the Threads share-link fixture reports the author's own
    continuation chain as **29 posts** where the raw page has 4. A test
    written against it would have gone green while measuring nothing --
    a false positive, which is worse than the absence
    `CLAUDE.md` already warns redacted fixtures for.

    `N` is the ordinal of first appearance, deliberately not a digest of the
    handle. A digest is what `_placeholder_url` uses and the reason there is
    diff stability across a re-capture, which matters for a file that gets
    re-captured. It is the wrong trade here: a handle is a permanent public
    identifier, so a digest of one is a value anybody holding a guess can
    confirm, and `count_leaked_secrets` only ever checks signature values --
    it would not notice. Churn on re-capture is the price and it is cheap.

    The brackets are load-bearing and were `user_N` for an afternoon:
    Instagram and Threads handles are letters, digits, `.` and `_`, so
    `user_1` is a handle somebody may actually own -- and `[user-1]` is one
    nobody can. A placeholder that a real value can equal is a placeholder
    `collect_identifiers` skips over on the next pass, which is both a leak
    and the reason `redact_and_verify` stopped being idempotent.
    """
    urls = 0

    def replace_url(match: re.Match[str]) -> str:
        nonlocal urls
        urls += 1
        return _placeholder_url(match.group(0))

    redacted = _MEDIA_HOST_RE.sub(replace_url, html)

    # Collect identifiers BEFORE blanking them, so they can then be erased
    # everywhere else they appear.
    identifiers = collect_identifiers(redacted)
    pseudonyms = {value: pseudonym(n) for n, value in enumerate(identifiers, start=1)}

    values = 0
    # The identifier keys are left for the sweep below: blanking them here
    # would erase the handle before it could be given a pseudonym, and the
    # relation between two nodes with the same author would go with it.
    for key in REDACTED_KEYS:
        if key in IDENTIFIER_KEYS:
            continue

        def replace_value(match: re.Match[str]) -> str:
            nonlocal values
            values += 1
            return f'{match.group(1)}"[redacted]"'

        redacted = _key_value_re(key).sub(replace_value, redacted)

    # Longest first. Handles nest -- `bob` is a prefix of `bobby` -- and a
    # first-appearance order would rewrite the short one inside the long one,
    # leaving `[user-1]by` on the page: a handle that leaked in pieces and a
    # relation quietly attached to the wrong person. The NUMBERS still come
    # from first appearance; only the order of substitution changes.
    for identifier in sorted(pseudonyms, key=len, reverse=True):
        occurrences = redacted.count(identifier)
        if occurrences:
            values += occurrences
            redacted = redacted.replace(identifier, pseudonyms[identifier])

    # Whatever is still standing under an identifier key is blanked, and this
    # is not belt-and-braces -- it is the only thing covering a name SHORTER
    # than `MIN_IDENTIFIER_LENGTH`. Such a value cannot be pseudonymised,
    # because a global replace of `JC` would hit unrelated text, so before
    # this loop existed a two-letter `full_name` walked straight into git
    # (measured on both real captures, 2026-09-11: "JC" and "KK"). Skipping
    # the identifier keys above buys the pseudonym its handle; it does not
    # buy the value an exemption.
    for key in IDENTIFIER_KEYS:

        def blank_identity(match: re.Match[str]) -> str:
            nonlocal values
            if is_placeholder(match.group(2)) or not match.group(2):
                return match.group(0)
            values += 1
            return f'{match.group(1)}"[redacted]"'

        redacted = _key_value_re(key).sub(blank_identity, redacted)

    return redacted, urls, values


def pseudonym(ordinal: int) -> str:
    """The stand-in for the `ordinal`-th distinct handle on a page.

    `[` and `]` are not legal in an Instagram or Threads handle, which is the
    whole reason for the shape: no real value can equal this one, so
    `is_placeholder` cannot mistake a person for a placeholder or the other
    way round.
    """
    return f"[user-{ordinal}]"


#: Every value redaction writes in place of an identity. Anchored, because a
#: substring match would exempt a handle that merely CONTAINS one.
_PLACEHOLDER_RE = re.compile(r"^(?:\[redacted\]|\[user-\d+\])$")


def is_placeholder(value: str) -> bool:
    """True for something redaction already wrote. Not an identity."""
    return _PLACEHOLDER_RE.match(value) is not None


def collect_identifiers(html: str) -> list[str]:
    """Handles and names that must not survive anywhere in the fixture.

    In order of first appearance, and a list rather than a set because the
    pseudonyms are assigned by position: a set would number them differently
    on every run and make the fixture's diff meaningless.

    Values redaction itself wrote are skipped, and that is what makes a
    second pass a no-op rather than a renumbering -- `redact_and_verify` is
    run on an already-committed fixture every time one is rebuilt from raw.
    """
    found: list[str] = []
    seen: set[str] = set()
    for key in IDENTIFIER_KEYS:
        for _, value in _key_value_re(key).findall(html):
            if len(value) >= MIN_IDENTIFIER_LENGTH and not is_placeholder(value):
                if value not in seen:
                    seen.add(value)
                    found.append(value)
    return found


def verify_redaction(original: str, redacted: str) -> RedactionReport:
    """Check that redaction changed nothing the extractor can observe.

    This is the whole basis for trusting a scrubbed fixture. Without it the
    fixture might be testing a shape the redaction invented.
    """
    before = extract_items(original)
    after = extract_items(redacted)
    return RedactionReport(
        urls_rewritten=0,
        values_replaced=0,
        items_before=len(before),
        items_after=len(after),
        variants_before=sum(len(item.variants) for item in before),
        variants_after=sum(len(item.variants) for item in after),
        secrets_leaked=count_leaked_secrets(original, redacted),
        post_located_before=locate_post_node(original) is not None,
        post_located_after=locate_post_node(redacted) is not None,
    )


def count_leaked_secrets(original: str, redacted: str) -> int:
    """How many of the original's signature values survive the redaction.

    The direction the first version of this module never checked. "The
    extractor still sees the same thing" and "the secrets are gone" are two
    different claims, and only the first was being made -- so a redaction
    that changed nothing at all would have been reported as a success.
    """
    originals = {
        value
        for value in _SIGNATURE_VALUE_RE.findall(original)
        if value != "00_REDACTED"
    }
    leaked = sum(1 for value in originals if value in redacted)
    # Identifiers count too: a fixture whose signatures are scrubbed but
    # whose author's handle is still readable has not been anonymised, it has
    # only been made harder to download from.
    leaked += sum(1 for name in collect_identifiers(original) if name in redacted)
    return leaked


def redact_and_verify(html: str) -> tuple[str, RedactionReport]:
    """Redact, then refuse to vouch for the result unless it still extracts
    identically. The caller decides what to do with a report that says no."""
    redacted, urls, values = redact_html(html)
    report = verify_redaction(html, redacted)
    return redacted, RedactionReport(
        urls_rewritten=urls,
        values_replaced=values,
        items_before=report.items_before,
        items_after=report.items_after,
        variants_before=report.variants_before,
        variants_after=report.variants_after,
        secrets_leaked=report.secrets_leaked,
        # Carried, not defaulted. Both fields default to True, so dropping
        # them here made every report from this function claim a post was
        # located in both copies -- including the ones where none was. This
        # is the only path `capture` uses, so the zero-item half of
        # `preserved` would have been decided by a default (2026-09-11).
        post_located_before=report.post_located_before,
        post_located_after=report.post_located_after,
    )


__all__ = [
    "PLACEHOLDER_OE",
    "REDACTED_KEYS",
    "RedactionReport",
    "collect_identifiers",
    "count_leaked_secrets",
    "is_placeholder",
    "pseudonym",
    "redact_and_verify",
    "redact_html",
    "verify_redaction",
]
