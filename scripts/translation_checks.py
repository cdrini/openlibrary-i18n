#!/usr/bin/env python3
"""
Checks that a translation change is safe to deploy, judged the way openlibrary
renders it: olbase installs each locale/<lang>/messages.po with
install_translations() and compiles it with babel's write_mo(), so each catalog
here is compiled with write_mo() and loaded with babel.support.Translations.

  ./i18n check-translations [--base REF] [<lang> ...]

Per language, the change fails on:
  rejected     openlibrary's install check (check_po_file) would refuse the file,
               so none of its translations would ship.
  placeholder  a non-fuzzy msgstr form whose %-placeholders differ from the
               English it stands in for at the n values that select it.
  regression   with --base: a string the base renders translated would render in
               English (deleted, fuzzy, emptied, a plural form emptied, or the
               English text itself). Translating a string is never required; only
               losing a translation is.
Only msgids in messages.pot count; translations no template requests are never
rendered.

Languages default to those whose messages.po differs from --base, or every
language without --base. Exit status: 0 clean, 1 findings, 2 the check could not
run.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from babel.messages.catalog import Catalog, Message
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po
from babel.support import Translations

REPO_ROOT = Path(__file__).resolve().parent.parent

PLURAL_PROBE_LIMIT = 1000

# The n values plural entries are always checked and described at, whatever the
# catalogs' plural rules. Covers every CLDR distinction up to n % 100 and n % 10.
DETAIL_NS = tuple(range(0, 112))

_FALLBACK = "\x00translation-checks-fallback\x00"

_CFMT_RE = re.compile(
    r"""
    %(?:
        (?:\([a-zA-Z_][a-zA-Z0-9_]*?\))?
        (?:[-+0 #]{0,5})
        (?:\d+|\*)?
        (?:\.(?:\d+|\*))?
        (?:h|l|ll|w|I|I32|I64)?
        [cCdiouxXeEfgGaAnpsSZ]
    )
    |%%
    """,
    re.VERBOSE,
)


class CheckError(Exception):
    """The check could not produce a result; never report this as a pass."""


# ---------------------------------------------------------------------------
# Rendering: compile and load exactly as openlibrary does
# ---------------------------------------------------------------------------

class _Fallback:
    """Returned by Translations when the catalog has no usable entry."""

    def gettext(self, message):
        return _FALLBACK

    def ngettext(self, msgid1, msgid2, n):
        return _FALLBACK

    def pgettext(self, context, message):
        return _FALLBACK

    def npgettext(self, context, msgid1, msgid2, n):
        return _FALLBACK


def compile_catalog(catalog: Catalog) -> Translations:
    """write_mo() with openlibrary's arguments, loaded the way openlibrary loads it."""
    buf = BytesIO()
    write_mo(buf, catalog)
    buf.seek(0)
    translations = Translations(buf)
    translations.add_fallback(_Fallback())
    return translations


def _lookup(t: Translations, msgctxt, msgid, msgid_plural, n) -> str:
    """The .mo value openlibrary's ugettext/ungettext would get, or _FALLBACK."""
    if msgid_plural is None:
        return t.pgettext(msgctxt, msgid) if msgctxt else t.gettext(msgid)
    if msgctxt:
        return t.npgettext(msgctxt, msgid, msgid_plural, n)
    return t.ngettext(msgid, msgid_plural, n)


def renders_translation(t: Translations, msgctxt, msgid, msgid_plural, n) -> bool:
    """
    True when openlibrary would show text other than the English source.

    openlibrary uses `translations.ugettext(s) or s` and, for plurals,
    `translations.ungettext(...)` falling back to English on a falsy value.
    write_mo() also fills an empty plural form with the English msgid or
    msgid_plural, so a non-empty .mo string can still be English.
    """
    value = _lookup(t, msgctxt, msgid, msgid_plural, n)
    return bool(value) and value not in (_FALLBACK, msgid, msgid_plural)


def has_translation(
    t: Translations, message: Message | None, msgctxt, msgid, msgid_plural, n,
) -> bool:
    """
    True when the compiled catalog holds a translation the translator wrote.

    Presence comes from the .mo lookup, so whatever write_mo() dropped (fuzzy,
    empty, a singular/plural shape the caller does not ask for) is absent. The
    .po `message` is consulted only to recognise a plural form write_mo() filled
    with the English, since that text is otherwise indistinguishable from a
    translation identical to the English.
    """
    value = _lookup(t, msgctxt, msgid, msgid_plural, n)
    if not value or value == _FALLBACK:
        return False
    if msgid_plural is not None and message is not None and not isinstance(message.string, str):
        form = t.plural(n)
        if form >= len(message.string) or not message.string[form]:
            return False
    return True


def plural_probes(*translations: Translations) -> list[int]:
    """The smallest n selecting each plural form, under every catalog's rule."""
    probes = {1}
    for t in translations:
        seen = set()
        for n in range(PLURAL_PROBE_LIMIT):
            form = t.plural(n)
            if form not in seen:
                seen.add(form)
                probes.add(n)
    return sorted(probes)


def forms_selected(catalog: Catalog) -> dict[int, list[int]]:
    """plural form index -> the n values in [0, PLURAL_PROBE_LIMIT) selecting it."""
    t = compile_catalog(catalog)
    forms: dict[int, list[int]] = {}
    for n in range(PLURAL_PROBE_LIMIT):
        forms.setdefault(t.plural(n), []).append(n)
    return forms


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

@dataclass
class Finding:
    kind: str
    lang: str
    msgctxt: str | None
    msgid: str
    msgid_plural: str | None = None
    reason: str = ""
    n: list[int] = field(default_factory=list)
    # False when the base msgstr is identical to the English, so losing it
    # changes nothing on the page; such a regression does not gate.
    visible: bool = True
    gating: bool = True
    base_msgstr: str | list[str] | None = None
    head_msgstr: str | list[str] | None = None
    # What exactly is wrong: "n=5" for a plural regressing at n=5,
    # "n=21:%(count)d" for a placeholder defect at n=21, and so on.
    detail: list[str] = field(default_factory=list)


def _split_id(message: Message) -> tuple[str, str | None]:
    if isinstance(message.id, (list, tuple)):
        return message.id[0], message.id[1]
    return message.id, None


def _jsonable(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    return value


def _why_english(message: Message | None, n: int | None) -> str:
    if message is None:
        return "missing"
    if message.fuzzy:
        return "fuzzy"
    if isinstance(message.string, (list, tuple)):
        if not any(message.string):
            return "empty"
        # read_po pads a short msgstr list to nplurals, so an absent form is an empty one.
        return "empty-plural-form"
    if not message.string:
        return "empty"
    if n is not None:
        return "not-plural"
    return "english-copy"


def find_regressions(
    lang: str,
    base: Catalog,
    head: Catalog,
    live_keys: set[tuple[str | None, str]],
) -> list[Finding]:
    """Live strings `base` renders translated that `head` would render in English."""
    base_t = compile_catalog(base)
    head_t = compile_catalog(head)
    probes = sorted(set(plural_probes(base_t, head_t)) | set(DETAIL_NS))
    findings = []
    for message in base:
        if not message.id:
            continue
        msgid, msgid_plural = _split_id(message)
        if (message.context, msgid) not in live_keys:
            continue
        ns = [None] if msgid_plural is None else probes
        head_message = head.get(msgid, context=message.context)
        ctx = message.context

        def goes_english(n):
            if not has_translation(base_t, message, ctx, msgid, msgid_plural, n):
                return False
            if not has_translation(head_t, head_message, ctx, msgid, msgid_plural, n):
                return True
            # A non-fuzzy msgstr that is the English text: base showed a
            # translation and head shows English.
            return (renders_translation(base_t, ctx, msgid, msgid_plural, n)
                    and not renders_translation(head_t, ctx, msgid, msgid_plural, n))

        lost = [n for n in ns if goes_english(n)]
        if not lost:
            continue
        visible = any(
            renders_translation(base_t, ctx, msgid, msgid_plural, n)
            and not renders_translation(head_t, ctx, msgid, msgid_plural, n)
            for n in lost
        )
        findings.append(Finding(
            kind="regression",
            lang=lang,
            msgctxt=message.context,
            msgid=msgid,
            msgid_plural=msgid_plural,
            reason=_why_english(head_message, lost[0]),
            n=[n for n in lost if n is not None],
            visible=visible,
            gating=visible,
            base_msgstr=_jsonable(message.string),
            head_msgstr=_jsonable(head_message.string) if head_message else None,
            detail=["singular"] if msgid_plural is None else [f"n={n}" for n in lost],
        ))
    return findings


def placeholder_signature(s: str) -> tuple[frozenset[str], tuple[str, ...]]:
    """Named specifiers as a set; positional ones in order, since order binds arguments."""
    specs = [m.group(0) for m in _CFMT_RE.finditer(s) if m.group(0) != "%%"]
    return (
        frozenset(x for x in specs if x.startswith("%(")),
        tuple(x for x in specs if not x.startswith("%(")),
    )


def _sig_text(sig: tuple[frozenset[str], tuple[str, ...]]) -> str:
    named, positional = sig
    return " ".join(sorted(named) + list(positional)) or "none"


def find_placeholder_defects(
    lang: str,
    catalog: Catalog,
    live_keys: set[tuple[str | None, str]],
) -> list[Finding]:
    """
    Every non-empty form of a non-fuzzy msgstr must carry the placeholders of
    the English it replaces. A plural form selected only by n=1 may match either
    msgid or msgid_plural; a form selected by any other n replaces msgid_plural,
    so it must match msgid_plural. With nplurals=1 the single form is selected by
    every n, which is the case #15 describes. A form selected only by n=0 may
    also carry no placeholders, as Arabic's zero form does ("no books").
    """
    forms = forms_selected(catalog)
    form_at = {n: form for form, ns in forms.items() for n in ns}
    no_placeholders = (frozenset(), ())
    findings = []
    for message in catalog:
        if not message.id or message.fuzzy or not message.string:
            continue
        msgid, msgid_plural = _split_id(message)
        if (message.context, msgid) not in live_keys:
            continue
        if msgid_plural is None:
            if isinstance(message.string, str) and (
                placeholder_signature(message.string) != placeholder_signature(msgid)
            ):
                findings.append(Finding(
                    kind="placeholder", lang=lang, msgctxt=message.context, msgid=msgid,
                    reason="msgstr placeholders differ from msgid",
                    head_msgstr=message.string,
                    detail=[f"singular:{_sig_text(placeholder_signature(message.string))}"],
                ))
            continue
        strings = message.string if isinstance(message.string, (list, tuple)) else (message.string,)
        singular_sig = placeholder_signature(msgid)
        plural_sig = placeholder_signature(msgid_plural)
        bad_forms = []
        bad_n: list[int] = []
        for index, form in enumerate(strings):
            if not form:
                continue
            selected_by = forms.get(index, [])
            allowed = {plural_sig} if set(selected_by) - {1} else {singular_sig, plural_sig}
            if set(selected_by) == {0}:
                allowed.add(no_placeholders)
            if placeholder_signature(form) not in allowed:
                bad_forms.append(index)
                bad_n.extend(n for n in selected_by[:3])
        if bad_forms:
            findings.append(Finding(
                kind="placeholder", lang=lang, msgctxt=message.context, msgid=msgid,
                msgid_plural=msgid_plural,
                reason="msgstr[%s] placeholders differ from msgid_plural"
                       % ",".join(map(str, bad_forms)),
                n=sorted(set(bad_n)),
                head_msgstr=list(strings),
                detail=[
                    f"n={n}:{_sig_text(placeholder_signature(strings[form_at[n]]))}"
                    for n in DETAIL_NS if form_at.get(n) in bad_forms
                ],
            ))
    return findings


def live_keys_from_pot(pot: Catalog) -> set[tuple[str | None, str]]:
    return {(m.context, _split_id(m)[0]) for m in pot if m.id}


def _format_args(message: Message):
    """
    Stand-in arguments shaped like the ones the msgid expects at runtime. A msgid
    with no placeholders gets {}: Jinja's newstyle gettext applies `% variables`
    even when there are none. Mirrors openlibrary#13070 (26b45e8) _format_args.
    """
    if not message.python_format:
        return {}
    ids = message.id if isinstance(message.id, (list, tuple)) else [message.id]
    names: set[str] = set()
    positional = 0
    for msgid in ids:
        pieces = [m.group(0) for m in _CFMT_RE.finditer(str(msgid)) if m.group(0) != "%%"]
        names.update(p[2:p.index(")")] for p in pieces if p.startswith("%("))
        positional = max(positional, sum(1 for p in pieces if not p.startswith("%(")))
    if names:
        return dict.fromkeys(names, 1)
    if positional:
        return (1,) * positional
    return {}


def install_rejections(data: bytes) -> list[str]:
    """
    Why openlibrary's install_translations() would refuse this .po, leaving the
    language out of the build: it does not parse, compile or load the
    way load_translations() loads it, or a compiled (non-fuzzy) msgstr raises when
    formatted, or renders a dict's repr through a positional placeholder. Mirrors
    check_po_file at 26b45e8; line numbers are dropped so two runs compare equal.
    """
    try:
        catalog = read_po(BytesIO(data), abort_invalid=True)
        mo = BytesIO()
        write_mo(mo, catalog)
        mo.seek(0)
        translations = Translations(mo)
        for n in range(PLURAL_PROBE_LIMIT):
            translations.plural(n)
    except Exception as e:
        return [f"does not parse/compile/load: {type(e).__name__}: {e}"]
    errors = []
    for message in catalog:
        if not message.id or message.fuzzy:
            continue
        args = _format_args(message)
        strings = message.string if isinstance(message.string, (list, tuple)) else [message.string]
        for msgstr in strings:
            if not msgstr:
                continue
            try:
                msgstr % args
            except (TypeError, ValueError, KeyError) as e:
                errors.append(f"{msgstr!r}: {type(e).__name__}: {e}")
                continue
            # "%s" % {...} does not raise; it renders the dict's repr.
            if isinstance(args, dict) and any(
                m.group(0) != "%%" and not m.group(0).startswith("%(")
                for m in _CFMT_RE.finditer(msgstr)
            ):
                errors.append(f"{msgstr!r}: positional placeholder where the msgid has none")
    return errors



# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True)


def base_catalog_bytes(base: str, lang: str, root: Path = REPO_ROOT) -> bytes | None:
    """locale/<lang>/messages.po at `base`, or None if it has no such file."""
    proc = _git(root, "show", f"{base}:locale/{lang}/messages.po")
    return proc.stdout if proc.returncode == 0 else None


def changed_languages(base: str, root: Path = REPO_ROOT) -> list[str]:
    """Languages whose messages.po differs between `base` and the working tree."""
    paths = []
    for args in (("diff", "--name-only", base, "--", "locale/"),
                 ("ls-files", "--others", "--exclude-standard", "--", "locale/")):
        proc = _git(root, *args)
        if proc.returncode:
            raise CheckError(f"git {args[0]}: {proc.stderr.decode().strip()}")
        paths += proc.stdout.decode().split()
    return sorted({p.split("/")[1] for p in paths if p.endswith("/messages.po")})


def all_languages(root: Path = REPO_ROOT) -> list[str]:
    return sorted(p.name for p in (root / "locale").iterdir() if (p / "messages.po").exists())


def _parse(data: bytes, what: str) -> Catalog:
    try:
        return read_po(BytesIO(data))
    except Exception as e:
        raise CheckError(f"cannot parse {what}: {e}") from e


def check_language(lang: str, live: set, base: str | None, root: Path = REPO_ROOT) -> list[Finding]:
    path = root / "locale" / lang / "messages.po"
    base_bytes = base_catalog_bytes(base, lang, root) if base else None
    if not path.exists():
        if base_bytes is None:
            return []
        return [Finding(kind="removed", lang=lang, msgctxt=None, msgid="",
                        reason="locale removed; its translations would no longer ship")]
    data = path.read_bytes()
    rejections = install_rejections(data)
    if rejections:
        return [Finding(kind="rejected", lang=lang, msgctxt=None, msgid="",
                        reason=f"openlibrary's install check refuses this file: {r}")
                for r in rejections]
    head = _parse(data, f"{lang}/messages.po")
    findings = find_placeholder_defects(lang, head, live)
    if base_bytes is not None:
        base_catalog = _parse(base_bytes, f"{lang}/messages.po at {base}")
        findings += [f for f in find_regressions(lang, base_catalog, head, live) if f.gating]
    return findings


def format_finding(f: Finding) -> str:
    ctx = f"[{f.msgctxt}] " if f.msgctxt else ""
    n = f" n={f.n[:8]}" if f.n else ""
    msgid = f": {ctx}{f.msgid!r}" if f.msgid else ""
    return f"{f.kind:<12} {f.lang:<4} {f.reason}{n}{msgid}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check-translations",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("langs", nargs="*", help="languages to check (default: see above)")
    parser.add_argument("--base", metavar="REF",
                        help="git ref the change is compared with, e.g. origin/main")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root

    try:
        pot_path = root / "messages.pot"
        if not pot_path.exists():
            raise CheckError(f"{pot_path} not found")
        live = live_keys_from_pot(_parse(pot_path.read_bytes(), "messages.pot"))
        if not live:
            raise CheckError("messages.pot has no msgids")
        if args.base and _git(root, "rev-parse", "--verify", f"{args.base}^{{commit}}").returncode:
            raise CheckError(f"unknown base {args.base}")
        if args.langs:
            langs = args.langs
        elif args.base:
            langs = changed_languages(args.base, root)
        else:
            langs = all_languages(root)
        findings = [f for lang in langs for f in check_language(lang, live, args.base, root)]
    except CheckError as e:
        print(f"check-translations: ERROR, the check did not run: {e}", file=sys.stderr)
        return 2

    for f in findings:
        print(format_finding(f))
    against = f" against {args.base}" if args.base else ""
    print(f"check-translations: {len(findings)} finding(s) in {', '.join(langs) or 'no languages'}"
          f"{against}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
