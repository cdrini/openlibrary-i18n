"""Tests for scripts/translation_checks.py.

Synthetic catalogs only: no network, no dependence on the real locale/ files.
The known-answer controls each build a pair with one regression, assert the
check fails, remove the regression, and assert it passes.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest
from babel.messages.pofile import read_po

_ROOT = Path(__file__).parent.parent
_SCRIPT = _ROOT / "scripts" / "translation_checks.py"
_spec = importlib.util.spec_from_file_location("translation_checks", _SCRIPT)
br = importlib.util.module_from_spec(_spec)
sys.modules["translation_checks"] = br  # dataclasses resolve types via sys.modules
_spec.loader.exec_module(br)

TWO_FORMS = "nplurals=2; plural=(n != 1);"
PL_FORMS = ("nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && "
            "(n%100<10 || n%100>=20) ? 1 : 2);")
RU_FORMS = ("nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && "
            "(n%100<10 || n%100>=20) ? 1 : 2);")
ONE_FORM = "nplurals=1; plural=0;"


def _po_text(*entries: str, plural_forms: str | None = TWO_FORMS,
             language: str | None = "de") -> str:
    header = 'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n'
    # babel only writes Plural-Forms into the .mo when the catalog has a Language.
    if plural_forms and language:
        header += f'"Language: {language}\\n"\n'
    if plural_forms:
        header += f'"Plural-Forms: {plural_forms}\\n"\n'
    return header + "\n" + "\n\n".join(entries) + "\n"


def _cat(*entries: str, plural_forms: str | None = TWO_FORMS, language: str | None = "de"):
    text = _po_text(*entries, plural_forms=plural_forms, language=language)
    return read_po(BytesIO(text.encode()))


def _live(*entries: str):
    return br.live_keys_from_pot(_cat(*entries))


ENABLE = 'msgid "Enable"\nmsgstr "Activar"'
ENABLE_FUZZY = '#, fuzzy\nmsgid "Enable"\nmsgstr "Ejemplo"'
WAITING_ID = ('msgid "There is %(count)d person waiting for this book."\n'
              'msgid_plural "There are %(count)d people waiting for this book."')
WAITING_OL = (WAITING_ID + '\nmsgstr[0] "Czeka %(count)d osoba na tę książkę."'
              '\nmsgstr[1] "Czekają %(count)d osoby na tę książkę."')
WAITING_PL_EMPTY_3RD = WAITING_OL + '\nmsgstr[2] ""'
WAITING_PL_FULL = WAITING_OL + '\nmsgstr[2] "Czeka %(count)d osób na tę książkę."'


def _regressions(ol, baked, live, lang="xx"):
    return br.find_regressions(lang, ol, baked, live)


# ---------------------------------------------------------------------------
# Known-answer controls: red with the regression, green without it
# ---------------------------------------------------------------------------

class TestKnownAnswerControls:
    def test_fuzzy_on_baked_side_is_a_regression_then_fixed(self):
        live = _live('msgid "Enable"\nmsgstr ""')
        ol = _cat(ENABLE)
        red = _regressions(ol, _cat(ENABLE_FUZZY), live, "es")
        assert [(f.msgid, f.reason) for f in red] == [("Enable", "fuzzy")]
        assert _regressions(ol, _cat(ENABLE), live, "es") == []

    def test_empty_third_plural_form_is_a_regression_then_fixed(self):
        # openlibrary's pl had no Plural-Forms header (babel defaults to 2 forms);
        # this repo declares 3 and left the third empty.
        live = _live(WAITING_ID + '\nmsgstr[0] ""\nmsgstr[1] ""')
        ol = _cat(WAITING_OL, plural_forms=None)
        red = _regressions(ol, _cat(WAITING_PL_EMPTY_3RD, plural_forms=PL_FORMS), live, "pl")
        assert len(red) == 1
        assert red[0].reason == "empty-plural-form"
        assert 5 in red[0].n or 0 in red[0].n
        assert 1 not in red[0].n and 2 not in red[0].n
        green = _regressions(ol, _cat(WAITING_PL_FULL, plural_forms=PL_FORMS), live, "pl")
        assert green == []




# ---------------------------------------------------------------------------
# What openlibrary actually renders
# ---------------------------------------------------------------------------

class TestRendering:
    def test_write_mo_compiles_an_empty_plural_form_as_english(self):
        # The trap: the .mo value is non-empty, and it is the English msgid_plural.
        t = br.compile_catalog(_cat(WAITING_PL_EMPTY_3RD, plural_forms=PL_FORMS))
        s1 = "There is %(count)d person waiting for this book."
        s2 = "There are %(count)d people waiting for this book."
        assert t.ngettext(s1, s2, 5) == s2
        assert not br.renders_translation(t, None, s1, s2, 5)
        assert br.renders_translation(t, None, s1, s2, 2)

    def test_fuzzy_is_dropped_at_compile(self):
        t = br.compile_catalog(_cat(ENABLE_FUZZY))
        assert not br.renders_translation(t, None, "Enable", None, None)
        assert br.renders_translation(br.compile_catalog(_cat(ENABLE)), None, "Enable", None, None)

    def test_has_translation_agrees_with_the_compiled_catalog(self):
        # For translations that differ from the English, the catalog-state rule
        # and a real .mo lookup must give the same answer at every probed n.
        cases = [
            (_cat(WAITING_PL_EMPTY_3RD, plural_forms=PL_FORMS), WAITING_PL_EMPTY_3RD),
            (_cat(WAITING_PL_FULL, plural_forms=PL_FORMS), WAITING_PL_FULL),
            (_cat(WAITING_OL, plural_forms=None), WAITING_OL),
            (_cat(WAITING_OL, plural_forms=PL_FORMS), WAITING_OL),
        ]
        s1 = "There is %(count)d person waiting for this book."
        s2 = "There are %(count)d people waiting for this book."
        checked = 0
        for catalog, _ in cases:
            t = br.compile_catalog(catalog)
            message = catalog.get(s1)
            for n in range(30):
                assert br.has_translation(t, message, None, s1, s2, n) == br.renders_translation(
                    t, None, s1, s2, n), (catalog.plural_forms, n)
                checked += 1
        assert checked == 120

    def test_plural_forms_without_language_compile_as_two_forms(self):
        # So the plural rule must be read from the compiled catalog, not the .po.
        catalog = _cat(WAITING_PL_FULL, plural_forms=PL_FORMS, language=None)
        assert catalog.num_plurals == 3
        t = br.compile_catalog(catalog)
        assert {t.plural(n) for n in range(100)} == {0, 1}

    def test_plural_probes_cover_every_form_of_both_rules(self):
        ol = br.compile_catalog(_cat(plural_forms=None))
        baked = br.compile_catalog(_cat(plural_forms=PL_FORMS))
        probes = br.plural_probes(ol, baked)
        assert {baked.plural(n) for n in probes} == {0, 1, 2}
        assert {ol.plural(n) for n in probes} == {0, 1}


# ---------------------------------------------------------------------------
# Regression rules
# ---------------------------------------------------------------------------

class TestRegressions:
    def test_missing_entry(self):
        live = _live('msgid "Enable"\nmsgstr ""')
        [f] = _regressions(_cat(ENABLE), _cat(), live)
        assert f.reason == "missing"

    def test_empty_msgstr(self):
        live = _live('msgid "Enable"\nmsgstr ""')
        [f] = _regressions(_cat(ENABLE), _cat('msgid "Enable"\nmsgstr ""'), live)
        assert f.reason == "empty"

    def test_fuzzy_in_openlibrary_is_not_translated_there(self):
        live = _live('msgid "Enable"\nmsgstr ""')
        assert _regressions(_cat(ENABLE_FUZZY), _cat(), live) == []

    def test_only_live_msgids_count(self):
        live = _live('msgid "Something else"\nmsgstr ""')
        assert _regressions(_cat(ENABLE), _cat(), live) == []

    def test_keyed_on_msgctxt(self):
        entry = 'msgctxt "button"\nmsgid "Enable"\nmsgstr "Activar"'
        live = _live('msgctxt "button"\nmsgid "Enable"\nmsgstr ""')
        # Same msgid without the context does not satisfy it.
        [f] = _regressions(_cat(entry), _cat(ENABLE), live)
        assert f.msgctxt == "button"
        assert _regressions(_cat(entry), _cat(entry), live) == []

    def test_plural_with_fewer_msgstrs_than_forms(self):
        live = _live(WAITING_ID + '\nmsgstr[0] ""\nmsgstr[1] ""')
        # Two msgstrs under a three-form rule: read_po pads the third with "".
        [f] = _regressions(_cat(WAITING_OL, plural_forms=None),
                           _cat(WAITING_OL, plural_forms=PL_FORMS, language="pl"), live)
        assert f.reason == "empty-plural-form"
        assert 5 in f.n or 0 in f.n

    def test_translation_identical_to_english_is_reported_not_gating(self):
        live = _live('msgid "PR"\nmsgstr ""')
        [f] = _regressions(_cat('msgid "PR"\nmsgstr "PR"'),
                           _cat('#, fuzzy\nmsgid "PR"\nmsgstr "Anterior"'), live)
        assert f.visible is False and f.gating is False

    def test_visible_regression_is_flagged_visible(self):
        live = _live('msgid "Enable"\nmsgstr ""')
        [f] = _regressions(_cat(ENABLE), _cat(ENABLE_FUZZY), live)
        assert f.visible is True


# ---------------------------------------------------------------------------
# Placeholder rules
# ---------------------------------------------------------------------------

DAYS_ID = 'msgid "Waiting for 1 day"\nmsgid_plural "Waiting for %(count)d days"'


def _placeholders(baked, live=None):
    live = live if live is not None else br.live_keys_from_pot(baked)
    return br.find_placeholder_defects("xx", baked, live)


class TestPlaceholders:
    def test_singular_mismatch(self):
        [f] = _placeholders(_cat('msgid "by %(name)s"\nmsgstr "par %(nom)s"'))
        assert f.kind == "placeholder"

    def test_positional_order_and_count(self):
        assert _placeholders(_cat('msgid "%s of %d"\nmsgstr "%d von %s"'))
        assert _placeholders(_cat('msgid "%s and %s"\nmsgstr "%s"'))
        assert _placeholders(_cat('msgid "%s of %d"\nmsgstr "%s von %d"')) == []

    def test_named_placeholders_may_repeat_and_reorder(self):
        entry = 'msgid "%(a)s and %(b)s"\nmsgstr "%(b)s, %(a)s, %(a)s"'
        assert _placeholders(_cat(entry)) == []

    def test_percent_literal_is_not_a_placeholder(self):
        assert _placeholders(_cat('msgid "100%% done"\nmsgstr "100 %% fait"')) == []

    def test_fuzzy_and_empty_are_skipped(self):
        assert _placeholders(_cat('#, fuzzy\nmsgid "by %(name)s"\nmsgstr "par"')) == []
        assert _placeholders(_cat('msgid "by %(name)s"\nmsgstr ""')) == []

    def test_form_selected_only_by_one_may_match_the_singular(self):
        entry = DAYS_ID + '\nmsgstr[0] "Warten seit 1 Tag"\nmsgstr[1] "Warten seit %(count)d Tagen"'
        assert _placeholders(_cat(entry)) == []

    def test_other_forms_must_match_msgid_plural(self):
        entry = DAYS_ID + '\nmsgstr[0] "Čekám 1 den"\nmsgstr[1] "Čekám 1 den"\nmsgstr[2] ""'
        [f] = _placeholders(_cat(entry, plural_forms=PL_FORMS))
        assert "msgstr[1]" in f.reason

    def test_single_form_language_must_carry_the_count(self):
        # openlibrary-i18n#15: with nplurals=1 the only form is shown for every n.
        entry = DAYS_ID + '\nmsgstr[0] "1日待っています"'
        [f] = _placeholders(_cat(entry, plural_forms=ONE_FORM))
        assert 5 in f.n or 2 in f.n
        ok = DAYS_ID + '\nmsgstr[0] "%(count)d日待っています"'
        assert _placeholders(_cat(ok, plural_forms=ONE_FORM)) == []

    def test_form_zero_shared_with_twenty_one(self):
        entry = (DAYS_ID + '\nmsgstr[0] "Ожидает 1 день"\nmsgstr[1] "Ожидает %(count)d дня"'
                 '\nmsgstr[2] "Ожидает %(count)d дней"')
        [f] = _placeholders(_cat(entry, plural_forms=RU_FORMS))
        assert 21 in f.n
        fixed = entry.replace("Ожидает 1 день", "Ожидает %(count)d день")
        assert _placeholders(_cat(fixed, plural_forms=RU_FORMS)) == []

    def test_only_live_msgids(self):
        entry = 'msgid "by %(name)s"\nmsgstr "par %(nom)s"'
        assert _placeholders(_cat(entry), live=set()) == []

    def test_form_selected_only_by_zero_may_drop_the_count(self):
        ar_forms = ("nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : "
                    "n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5);")
        forms = ['"لا أيام"', '"يوم واحد"', '"يومان"', '"%(count)d أيام"',
                 '"%(count)d يومًا"', '"%(count)d يوم"']
        entry = DAYS_ID + "".join(f"\nmsgstr[{i}] {f}" for i, f in enumerate(forms))
        # n=2 selects msgstr[2] alone, and "two days" without the count is a defect
        # under the rule; only the n=0 form is exempt.
        [f] = _placeholders(_cat(entry, plural_forms=ar_forms, language="ar"))
        assert "msgstr[2]" in f.reason and "msgstr[0]" not in f.reason

class TestRendersEnglish:

    def test_plural_msgid_in_context_is_looked_up_in_context(self):
        # The baked side has the plural only without the context openlibrary asks for.
        ctx_entry = 'msgctxt "queue"\n' + WAITING_OL
        live = _live('msgctxt "queue"\n' + WAITING_ID + '\nmsgstr[0] ""\nmsgstr[1] ""')
        [f] = _regressions(_cat(ctx_entry), _cat(WAITING_OL), live)
        assert f.msgctxt == "queue"

    def test_singular_in_context_reports_the_baked_entry_in_context(self):
        ol = _cat('msgctxt "button"\nmsgid "Enable"\nmsgstr "Activar"')
        baked = _cat('msgctxt "button"\n' + ENABLE_FUZZY, ENABLE)
        live = _live('msgctxt "button"\nmsgid "Enable"\nmsgstr ""')
        [f] = _regressions(ol, baked, live)
        assert f.reason == "fuzzy" and f.head_msgstr == "Ejemplo"

    def test_form_only_the_baked_rule_selects_is_probed(self):
        # openlibrary: 2 forms; baked: 3 forms whose middle form is empty.
        live = _live(WAITING_ID + '\nmsgstr[0] ""\nmsgstr[1] ""')
        baked = WAITING_ID + ('\nmsgstr[0] "Czeka %(count)d osoba na tę książkę."\nmsgstr[1] ""'
                              '\nmsgstr[2] "Czeka %(count)d osób na tę książkę."')
        [f] = _regressions(_cat(WAITING_OL, plural_forms=None),
                           _cat(baked, plural_forms=PL_FORMS, language="pl"), live)
        assert 2 in f.n and 5 not in f.n


class TestInstallRejections:
    """openlibrary's install_translations() refuses a .po that would crash a render."""

    CRASH = 'msgid "by %(name)s"\nmsgstr "par %(nom)s"'

    def test_fuzzy_crash_does_not_reject(self):
        assert br.install_rejections(_po_text("#, fuzzy\n" + self.CRASH).encode()) == []
        assert br.install_rejections(_po_text(self.CRASH).encode())

    def test_placeholder_free_msgid_is_formatted_with_an_empty_dict(self):
        # Jinja's newstyle gettext applies % variables even when there are none.
        assert br.install_rejections(_po_text('msgid "Done"\nmsgstr "Hecho %(x)s"').encode())
        assert br.install_rejections(_po_text('msgid "100%% done"\nmsgstr "100%% hecho"').encode()) == []

    def test_positional_placeholder_under_named_arguments_renders_a_dict(self):
        # "%s" % {"name": 1} does not raise; it shows "{'name': 1}" on the page.
        assert br.install_rejections(_po_text('msgid "by %(name)s"\nmsgstr "por %s"').encode())

    def test_plural_rule_that_fails_to_load_rejects(self):
        text = _po_text('msgid "Enable"\nmsgstr "Activar"',
                        plural_forms="nplurals=2; plural=(n / 0);", language="es")
        assert br.install_rejections(text.encode())


# ---------------------------------------------------------------------------
# check-translations, run on a git repo laid out like this one
# ---------------------------------------------------------------------------

FR_FORMS = "nplurals=2; plural=(n > 1);"
DAYS_POT = DAYS_ID + '\nmsgstr[0] ""\nmsgstr[1] ""'


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


def _write(repo: Path, lang: str, entries: list[str], plural_forms=TWO_FORMS, language="de"):
    (repo / "locale" / lang).mkdir(parents=True, exist_ok=True)
    (repo / "locale" / lang / "messages.po").write_text(
        _po_text(*entries, plural_forms=plural_forms, language=language))


def _repo(tmp_path, pot: list[str], base: dict, **kw) -> Path:
    """A repo whose committed HEAD holds `base`, ready for the working tree to change."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "messages.pot").write_text(_po_text(*pot))
    for lang, entries in base.items():
        _write(repo, lang, entries, **kw)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _check(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_SCRIPT), "--root", str(repo), *args],
                          capture_output=True, text=True)


class TestCheckTranslations:
    def test_lost_translation_fails_then_passes_once_restored(self, tmp_path):
        repo = _repo(tmp_path, ['msgid "Enable"\nmsgstr ""'], {"es": [ENABLE]})
        _write(repo, "es", [ENABLE_FUZZY])
        red = _check(repo, "--base", "HEAD")
        assert red.returncode == 1, red.stdout
        assert "regression" in red.stdout and "'Enable'" in red.stdout
        _write(repo, "es", [ENABLE])
        assert _check(repo, "--base", "HEAD").returncode == 0

    def test_untranslated_string_is_not_a_finding(self, tmp_path):
        repo = _repo(tmp_path, ['msgid "Enable"\nmsgstr ""', 'msgid "New"\nmsgstr ""'],
                     {"es": [ENABLE]})
        _write(repo, "es", [ENABLE, 'msgid "New"\nmsgstr ""'])
        out = _check(repo, "--base", "HEAD")
        assert out.returncode == 0, out.stdout

    def test_msgstr_that_is_the_english_text_is_a_regression(self, tmp_path):
        repo = _repo(tmp_path, ['msgid "Borrow"\nmsgstr ""'],
                     {"es": ['msgid "Borrow"\nmsgstr "Prestar"']})
        _write(repo, "es", ['msgid "Borrow"\nmsgstr "Borrow"'])
        out = _check(repo, "--base", "HEAD")
        assert out.returncode == 1 and "english-copy" in out.stdout

    def test_translation_identical_to_english_may_be_lost(self, tmp_path):
        entry = 'msgid "Add a new series"\nmsgstr "%s"'
        repo = _repo(tmp_path, [entry % ""], {"es": [entry % "Add a new series"]})
        _write(repo, "es", ["#, fuzzy\n" + entry % "Añadir un nuevo rol"])
        assert _check(repo, "--base", "HEAD").returncode == 0

    def test_placeholder_defect_fails_without_a_base(self, tmp_path):
        repo = _repo(tmp_path, ['msgid "by %(name)s"\nmsgstr ""'], {})
        _write(repo, "fr", ['msgid "by %(name)s"\nmsgstr "par"'])
        out = _check(repo)
        assert out.returncode == 1 and "placeholder" in out.stdout

    def test_render_crash_rejects_the_locale(self, tmp_path):
        crash = 'msgid "by %(name)s"\nmsgstr "par %(nom)s"'
        repo = _repo(tmp_path, ['msgid "by %(name)s"\nmsgstr ""'], {})
        _write(repo, "fr", [crash])
        out = _check(repo)
        assert out.returncode == 1 and "rejected" in out.stdout

    def test_crash_on_a_plural_form(self, tmp_path):
        repo = _repo(tmp_path, [DAYS_POT], {})
        _write(repo, "fr", [DAYS_ID + '\nmsgstr[0] "1 jour"\nmsgstr[1] "%(nombre)d jours"'],
               plural_forms=FR_FORMS, language="fr")
        assert _check(repo).returncode == 1

    def test_unparseable_file_is_a_rejection(self, tmp_path):
        repo = _repo(tmp_path, [ENABLE], {"es": [ENABLE]})
        (repo / "locale" / "es" / "messages.po").write_text(
            _po_text('msgid "a"\nthis is not po\nmsgstr "b"'))
        out = _check(repo, "--base", "HEAD")
        assert out.returncode == 1 and "rejected" in out.stdout

    def test_removed_locale_fails(self, tmp_path):
        repo = _repo(tmp_path, [ENABLE], {"es": [ENABLE]})
        (repo / "locale" / "es" / "messages.po").unlink()
        out = _check(repo, "--base", "HEAD")
        assert out.returncode == 1 and "removed" in out.stdout

    def test_new_locale_is_checked_without_a_base_version(self, tmp_path):
        repo = _repo(tmp_path, ['msgid "Enable"\nmsgstr ""'], {"es": [ENABLE]})
        _write(repo, "zz", ['msgid "Enable"\nmsgstr ""'])
        out = _check(repo, "--base", "HEAD")
        assert out.returncode == 0, out.stdout
        assert "in zz against HEAD" in out.stdout

    def test_only_changed_languages_are_checked_against_a_base(self, tmp_path):
        bad = 'msgid "by %(name)s"\nmsgstr "par"'
        repo = _repo(tmp_path, ['msgid "by %(name)s"\nmsgstr ""', ENABLE],
                     {"fr": [bad], "es": [ENABLE]})
        _write(repo, "es", [ENABLE, 'msgid "by %(name)s"\nmsgstr ""'])
        out = _check(repo, "--base", "HEAD")
        assert out.returncode == 0, out.stdout
        assert _check(repo).returncode == 1  # every language, fr included

    @pytest.mark.parametrize("breakage", ["no_pot", "bad_base"])
    def test_a_check_that_cannot_run_exits_2_not_0(self, tmp_path, breakage):
        repo = _repo(tmp_path, [ENABLE], {"es": [ENABLE]})
        if breakage == "no_pot":
            (repo / "messages.pot").unlink()
            out = _check(repo)
        else:
            out = _check(repo, "--base", "no-such-ref")
        assert out.returncode == 2, (out.stdout, out.stderr)
        assert "did not run" in out.stderr

    def test_i18n_subcommand_delegates(self):
        out = subprocess.run([sys.executable, str(_ROOT / "i18n"), "check-translations", "--help"],
                             capture_output=True, text=True, cwd=_ROOT)
        assert out.returncode == 0 and "check-translations" in out.stdout
