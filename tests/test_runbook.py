"""The runbook's quoted messages, pinned to the code that emits them.

`docs/gameday.md` is the short runbook, and it links out: `docs/troubleshooting.md`
tells the operator what each message on the page, the terminal and the mirror
script's console means, and `docs/box.md` reproduces the startup banner. Those
are read in a press box, by someone who cannot go and check the source, so a
message renamed in the code and left stale in the docs is worse than no table at
all.

Both directions are asserted: every message below appears verbatim in the code
and verbatim in the doc that explains it. Renaming one without the other fails
here. So does a link between the docs that no longer lands anywhere.

PAGE_QUOTES (the page's script) and BOX_QUOTES (the box's Python) are read out
of their source and looked for in each doc that quotes them (#48). Every named
string in app.js is quoted or says why not. Every line the mirror script logs is
in troubleshooting.md. A new quoted message is one line in either registry.
"""

import re
import unittest
from pathlib import Path
from types import ModuleType

from tacet import annotations, app, arming, config, disk, provenance, reach, reaper, script_check, serve, state, web

REPO_ROOT = Path(__file__).resolve().parent.parent
MIRROR_SCRIPT = REPO_ROOT / "reaper" / "tacet_mirror.lua"
DOCS = REPO_ROOT / "docs"
RUNBOOK = DOCS / "gameday.md"
#: Where each message is explained now that gameday.md links out (#77).
TROUBLESHOOTING = DOCS / "troubleshooting.md"
BOX = DOCS / "box.md"
REAPER = DOCS / "reaper.md"
APP_JS = REPO_ROOT / "src" / "tacet" / "static" / "app.js"

#: Every line the mirror script logs, at startup and while running. Each of
#: these decides whether the operator has to do anything, so each is documented
#: (docs/troubleshooting.md, "The mirror console").
MIRROR_MESSAGES = (
    "mirroring ",
    "queue does not exist yet; waiting for the box to create it",
    "no remembered position in a queue with history; starting at its end",
    "queue is shorter than the stored offset; starting from the beginning",
    "no queue path given; not starting",
    "mirror stopped",
    "could not read this script to checksum it",
    "ignoring malformed queue line (",
    "no start for span ",
    "error handling line: ",
    "error polling queue: ",
)

#: Every `log("...` literal in the mirror script. It does not match the
#: definition, `local function log(message)`.
LUA_LOG = re.compile(r'\blog\("([^"]*)"')


class MirrorMessagesAreDocumented(unittest.TestCase):
    def setUp(self) -> None:
        self.script = MIRROR_SCRIPT.read_text(encoding="utf-8")
        self.troubleshooting = TROUBLESHOOTING.read_text(encoding="utf-8")

    def test_every_documented_message_is_one_the_script_emits(self) -> None:
        for message in MIRROR_MESSAGES:
            with self.subTest(message=message):
                self.assertIn(f'log("{message}', self.script)

    def test_every_documented_message_appears_in_the_runbook(self) -> None:
        for message in MIRROR_MESSAGES:
            with self.subTest(message=message):
                self.assertIn(message.strip(), self.troubleshooting)

    def test_the_script_logs_nothing_that_is_undocumented(self) -> None:
        """Guards the gap the other two cannot: a new message, at startup or
        later, that is never added to the table."""
        logged = LUA_LOG.findall(self.script)
        # Every call, plus the definition, so one that does not start with a
        # string literal cannot slip past the scan above.
        self.assertEqual(
            len(re.findall(r"\blog\(", self.script)),
            len(logged) + 1,
            "every log call must start with a string literal so the runbook test can read it",
        )
        self.assertGreaterEqual(len(logged), len(MIRROR_MESSAGES))
        for message in logged:
            with self.subTest(message=message):
                self.assertTrue(
                    any(message.startswith(known) for known in MIRROR_MESSAGES),
                    f"the script logs {message!r}, which is not in MIRROR_MESSAGES "
                    f"and so is probably not in the runbook either",
                )

    def test_the_console_lines_read_as_the_runbook_shows_them(self) -> None:
        found = re.search(r'ShowConsoleMsg\("([^"]*)" \.\. message', self.script)
        self.assertIsNotNone(found, "the script no longer prefixes its console lines")
        assert found is not None
        prefix = found[1]
        self.assertIn('.. " from byte " ..', self.script)
        for doc in (RUNBOOK, REAPER):
            with self.subTest(doc=doc.name):
                self.assertIn(f"{prefix}mirroring <path> from byte N", doc.read_text(encoding="utf-8"))
        self.assertIn(f"{prefix}mirror stopped", REAPER.read_text(encoding="utf-8"))


#: What joins a message's fixed words to the value after it, stripped so the
#: runbook can quote `Console unreachable` for "Console unreachable: <why>".
JOINERS = " ,:("

#: Every top-level string constant in the page's script.
PAGE_STRING_CONSTANT = re.compile(r'^const ([A-Z][A-Z0-9_]*) = "', re.MULTILINE)
#: Families with tests of their own that read every member (#204, #167).
PAGE_FAMILIES = ("PROJECT_", "TRACKS_")
#: How many string constants the page names today; fewer means the scan broke.
NAMES_FLOOR = 59


def fixed_words(message: str) -> str:
    """The part of a message the runbook quotes verbatim: up to the first value
    filled in (`{`), without what joins it to that value. Raises when nothing is
    left, since an empty quote is found in any document."""
    words = message.split("{")[0].rstrip(JOINERS)
    if not words:
        raise ValueError(f"no fixed words before the first value in {message!r}")
    return words


def collapsed(text: str) -> str:
    """Whitespace collapsed: a quote wrapped across a line break still reads as one."""
    return " ".join(text.split())


def page_constant(script: str, name: str) -> str:
    """`const NAME = "...` in the page's script. For a constant built with `+`,
    the first literal, which is the head the runbook quotes."""
    found = re.search(rf'^const {re.escape(name)} = "([^"]*)"', script, re.MULTILINE)
    if found is None:
        raise LookupError(f"app.js defines no string constant {name}")
    return found[1]


#: (constant in app.js, the docs that quote its fixed words).
PAGE_QUOTES: tuple[tuple[str, tuple[Path, ...]], ...] = (
    ("TAP_FAILED", (TROUBLESHOOTING,)),
    ("TAP_UNTIMED", (TROUBLESHOOTING,)),
    ("RAMPING_BLOCKED", (RUNBOOK, BOX)),
    ("NOT_MOVED", (TROUBLESHOOTING,)),
    ("STORED_LEAD", (RUNBOOK, TROUBLESHOOTING)),
    ("STORED_ALREADY", (TROUBLESHOOTING,)),
    ("STORED_RELEASING", (RUNBOOK, TROUBLESHOOTING)),
    ("STORED_UNKNOWN", (RUNBOOK, TROUBLESHOOTING)),
    ("TARGET_NEXT_OPEN", (RUNBOOK, TROUBLESHOOTING)),
    ("TARGET_BLOCKED", (RUNBOOK, TROUBLESHOOTING)),
    ("EARLIER_RUN_HEADING", (RUNBOOK, TROUBLESHOOTING)),
    ("RECORDER_ERROR_PREFIX", (TROUBLESHOOTING,)),
    ("RECORDING_LINK_LOST", (TROUBLESHOOTING, REAPER)),
    ("RECORDING_NO_FEEDBACK", (RUNBOOK, TROUBLESHOOTING, BOX)),
    ("RECORDING_NOT_REPORTED", (RUNBOOK, TROUBLESHOOTING, REAPER)),
    ("RECORDING_ROLLING", (RUNBOOK, TROUBLESHOOTING)),
    ("PROVENANCE_DIRTY", (RUNBOOK, TROUBLESHOOTING, BOX)),
    ("PROVENANCE_UNKNOWN", (TROUBLESHOOTING, BOX)),
    ("CONSOLE_NOTHING_THERE", (RUNBOOK, TROUBLESHOOTING, BOX)),
    ("CONSOLE_NO_ANSWER", (RUNBOOK, TROUBLESHOOTING, BOX)),
    ("CONSOLE_NOT_CHECKED", (TROUBLESHOOTING, BOX)),
    ("PROJECT_SCRIPT_OLDER", (RUNBOOK, TROUBLESHOOTING, REAPER)),
    ("FADER_SEND_FAILED", (TROUBLESHOOTING, BOX)),
    ("SAVING_LOG_FAILED", (TROUBLESHOOTING,)),
    ("SAVING_MIRROR_FAILED", (TROUBLESHOOTING,)),
    ("SAVING_LOST_EARLIER", (TROUBLESHOOTING,)),
    ("LINK_DOWN", (TROUBLESHOOTING,)),
    ("LINK_CONNECTING", (TROUBLESHOOTING,)),
    ("LINK_SILENT", (TROUBLESHOOTING,)),
)

#: (module, constant in it, the docs that quote its fixed words).
BOX_QUOTES: tuple[tuple[ModuleType, str, tuple[Path, ...]], ...] = (
    (state, "WHY_ARMED_BY_OPERATOR", (TROUBLESHOOTING,)),
    (state, "WHY_STALLED", (TROUBLESHOOTING,)),
    (app, "RECORD_SEND_FAILED", (TROUBLESHOOTING,)),
    (serve, "REUSED_LOG_WARNING", (TROUBLESHOOTING,)),
    (serve, "CLOCK_RESTARTED_WARNING", (TROUBLESHOOTING,)),
    (serve, "TORN_WRITE_WARNING", (TROUBLESHOOTING,)),
    (script_check, "QUESTION", (RUNBOOK, TROUBLESHOOTING, BOX, REAPER)),
    (reach, "ANSWERED", (RUNBOOK, TROUBLESHOOTING, BOX)),
    (reach, "NOTHING_THERE", (RUNBOOK, TROUBLESHOOTING, BOX)),
)

#: String constants in app.js that the runbook does not quote, and why not.
PAGE_NOT_QUOTED: dict[str, str] = {
    "TAP_FAILED_ADVICE": "the advice after the reason; the runbook quotes the head, TAP_FAILED",
    "SVG_CLOSE": "markup, not words",
    "TARGET_SET_KEY": "a vocabulary key, not words",
    "MOVE_TAG_FALLBACK": "only for a move kind this page predates; a reload fixes it",
    "MOVE_LATE_WORD": "quoted as the readout's `s late`, in test_the_runbook_quotes_the_move_words",
    "RECORD_REASON_MISSING": "only from a box older than #163; a reload fixes it",
    "UNARMED_MORE": "the word in `and N more`, after a count",
    "LINK_STALE": "the tail after the link banner's head words",
    "TARGET_CHIP_WORD": "quoted with its level, in test_the_runbook_quotes_the_target_chip",
    "WAKE_HELD": "a reassurance, not a fault",
    "WAKE_ADVICE": "gameday.md Pre-flight names it as the Auto-Lock advice",
}


class QuotedMessagesExist(unittest.TestCase):
    """The words the docs quote are read out of the source, never typed here (#48)."""

    def setUp(self) -> None:
        self.script = APP_JS.read_text(encoding="utf-8")
        self.docs = {
            path: collapsed(path.read_text(encoding="utf-8")) for path in (RUNBOOK, TROUBLESHOOTING, BOX, REAPER)
        }
        self.box_source = "\n".join(
            path.read_text(encoding="utf-8") for path in (REPO_ROOT / "src" / "tacet").glob("*.py")
        )

    def test_every_page_message_the_runbook_quotes_exists(self) -> None:
        for name, docs in PAGE_QUOTES:
            quoted = fixed_words(page_constant(self.script, name))
            for doc in docs:
                with self.subTest(name=name, doc=doc.name):
                    self.assertIn(quoted, self.docs[doc])

    def test_every_quoted_page_constant_is_used(self) -> None:
        for name, _ in PAGE_QUOTES:
            with self.subTest(name=name):
                self.assertGreaterEqual(
                    len(re.findall(rf"\b{name}\b", self.script)), 2, f"{name} is declared and never used"
                )

    def test_every_box_message_the_runbook_quotes_exists(self) -> None:
        for module, name, docs in BOX_QUOTES:
            quoted = fixed_words(getattr(module, name))
            for doc in docs:
                with self.subTest(name=name, doc=doc.name):
                    self.assertIn(quoted, self.docs[doc])

    def test_every_quoted_box_constant_is_used(self) -> None:
        for _, name, _ in BOX_QUOTES:
            with self.subTest(name=name):
                self.assertGreaterEqual(
                    len(re.findall(rf"\b{name}\b", self.box_source)), 2, f"{name} is defined and never used"
                )

    def test_every_page_string_constant_is_quoted_or_says_why_not(self) -> None:
        names = PAGE_STRING_CONSTANT.findall(self.script)
        self.assertGreaterEqual(len(names), NAMES_FLOOR)
        quoted = {name for name, _ in PAGE_QUOTES}
        for name in names:
            with self.subTest(name=name):
                self.assertTrue(
                    name in quoted or name in PAGE_NOT_QUOTED or name.startswith(PAGE_FAMILIES),
                    f"app.js names {name}: add it to PAGE_QUOTES with the docs that quote it, "
                    f"or to PAGE_NOT_QUOTED with why not",
                )

    def test_every_excused_page_constant_still_exists_and_is_not_also_quoted(self) -> None:
        names = set(PAGE_STRING_CONSTANT.findall(self.script))
        quoted = {name for name, _ in PAGE_QUOTES}
        for name in PAGE_NOT_QUOTED:
            with self.subTest(name=name):
                self.assertIn(name, names)
                self.assertNotIn(name, quoted)

    def test_the_runbook_quotes_the_target_chip(self) -> None:
        word = page_constant(self.script, "TARGET_CHIP_WORD")
        for doc in (RUNBOOK, TROUBLESHOOTING):
            with self.subTest(doc=doc.name):
                self.assertRegex(self.docs[doc], rf"`{re.escape(word)} -?\d+(?:\.\d+)? dB")

    def test_the_banner_and_the_page_name_the_same_earlier_run_section(self) -> None:
        self.assertIn(page_constant(self.script, "EARLIER_RUN_HEADING"), serve.RETIRED_SPAN_NOTE)

    def test_the_runbook_quotes_the_note_length_refusal(self) -> None:
        length = annotations.MAX_DATA_TEXT + 1
        with self.assertRaises(annotations.DataError) as raised:
            annotations.operator_data({"text": "x" * length})
        quoted = str(raised.exception).replace(f" {length} ", " N ")
        self.assertIn(quoted, self.docs[TROUBLESHOOTING])

    def test_fixed_words_stops_at_the_first_value_and_its_joiner(self) -> None:
        self.assertEqual(fixed_words("Could not send the start ({error})."), "Could not send the start")
        self.assertEqual(fixed_words("Console unreachable: "), "Console unreachable")
        self.assertEqual(fixed_words("Sent before the box, "), "Sent before the box")
        with self.assertRaises(ValueError):
            fixed_words("{x} only")


class TheBannerMatchesTheRunbook(unittest.TestCase):
    """`docs/box.md` reproduces the startup banner, so it can go stale.

    The banner is the first thing read on the day and the runbook is what it is
    read against. A wording change on one side and not the other turns the
    check in step 3 into a comparison against something that no longer exists.
    """

    def setUp(self) -> None:
        self.box = BOX.read_text(encoding="utf-8")
        self.troubleshooting = TROUBLESHOOTING.read_text(encoding="utf-8")
        argv = ["--console-host", "10.0.0.5", "--dca", "3", "--log", "/games/g.jsonl"]
        argv += ["--queue", "/queue/tacet.tsv", "--reaper-host", "127.0.0.1"]
        clean = provenance.Provenance(
            source=provenance.Source.CHECKOUT,
            commit="0123456789abcdef0123456789abcdef01234567",
            branch="main",
            detached=False,
            dirty=False,
            untracked=0,
            worktree=False,
            path="/checkout",
            error=None,
        )
        answered = reach.Check(reach.Reach.ANSWERED, reach.Trigger.STARTUP, "10.0.0.5", 1.0, rtt_ms=0.4)
        current = script_check.ScriptStatus((script_check.ROW_CURRENT,), "0badf00d")
        self.lines = serve.startup_lines(
            serve.parser().parse_args(argv), None, code=clean, console_check=answered, script=current
        )
        self.gameday = RUNBOOK.read_text(encoding="utf-8")
        self.script = (REPO_ROOT / "src" / "tacet" / "static" / "app.js").read_text(encoding="utf-8")

    def test_the_runbook_quotes_not_a_checkout(self) -> None:
        self.assertIn(provenance.NOT_A_CHECKOUT, self.box)
        self.assertIn(provenance.NOT_A_CHECKOUT, self.troubleshooting)

    def test_the_runbook_documents_the_code_warnings(self) -> None:
        self.assertIn(serve.DIRTY_WARNING, self.troubleshooting)
        self.assertIn(serve.UNKNOWN_CODE_WARNING, self.troubleshooting)

    def _chip_words(self, name: str) -> str:
        found = re.search(rf'const {name} = "([^"]+)";', self.script)
        assert found is not None
        return found.group(1)

    def test_the_banner_and_the_page_name_the_same_chip(self) -> None:
        self.assertEqual(self._chip_words("PROVENANCE_DIRTY"), serve.PAGE_DIRTY_CHIP)

    def test_the_runbook_quotes_the_chip_words(self) -> None:
        dirty = self._chip_words("PROVENANCE_DIRTY")
        unknown = self._chip_words("PROVENANCE_UNKNOWN")
        self.assertIn(dirty, self.gameday)
        self.assertIn(dirty, self.troubleshooting)
        self.assertIn(unknown, self.troubleshooting)

    def test_the_runbook_quotes_every_ping_row(self) -> None:
        # #73: the words after the `ping` label in the banner.
        for words in (reach.ANSWERED, reach.NO_ANSWER, reach.NOTHING_THERE, reach.COULD_NOT_CHECK):
            with self.subTest(words):
                self.assertIn(words, self.box)
                self.assertIn(words, self.troubleshooting)

    def test_the_runbook_documents_the_nothing_there_warning(self) -> None:
        self.assertIn(serve.NOTHING_THERE_WARNING, self.troubleshooting)

    def test_the_runbook_quotes_the_console_chip_words(self) -> None:
        for name in ("CONSOLE_NOTHING_THERE", "CONSOLE_NO_ANSWER", "CONSOLE_NOT_CHECKED"):
            with self.subTest(name):
                self.assertIn(self._chip_words(name), self.troubleshooting)
        self.assertIn(self._chip_words("CONSOLE_NOTHING_THERE"), self.gameday)

    def test_the_runbook_quotes_every_project_chip_word(self) -> None:
        # #204: every PROJECT_* constant in the page's script is in troubleshooting.
        names = re.findall(r"^const (PROJECT_[A-Z_]+) = ", self.script, flags=re.MULTILINE)
        self.assertGreaterEqual(len(names), 8)
        for name in names:
            with self.subTest(name):
                self.assertIn(self._chip_words(name), self.troubleshooting)

    def test_gameday_quotes_the_saved_and_unsaved_chips(self) -> None:
        self.assertIn(self._chip_words("PROJECT_SAVED"), self.gameday)
        self.assertIn(self._chip_words("PROJECT_UNSAVED"), self.gameday)

    def test_gameday_quotes_the_older_script_chip(self) -> None:
        # #223
        self.assertIn(self._chip_words("PROJECT_SCRIPT_OLDER"), self.gameday)
        self.assertIn(script_check.QUESTION.strip(), self.gameday)

    def test_the_runbook_quotes_every_tracks_chip_word(self) -> None:
        # #167: every TRACKS_ constant in the page's script is in troubleshooting.
        names = re.findall(r"^const (TRACKS_[A-Z_]+) = ", self.script, flags=re.MULTILINE)
        self.assertGreaterEqual(len(names), 11)
        for name in names:
            with self.subTest(name):
                self.assertIn(self._chip_words(name), self.troubleshooting)

    def test_gameday_quotes_the_tracks_chip(self) -> None:
        for name in ("TRACKS_ARMED", "TRACKS_UNARMED", "TRACKS_UNKNOWN"):
            with self.subTest(name):
                self.assertIn(self._chip_words(name), self.gameday)

    def test_the_pilot_name_is_in_the_sentence_that_says_it(self) -> None:
        self.assertIn(arming.PILOT_NAME, self._chip_words("TRACKS_PILOTS"))

    def test_reaper_md_tells_the_operator_to_keep_the_pilot_name(self) -> None:
        reaper_md = REAPER.read_text(encoding="utf-8")
        self.assertIn(f"keep it exactly `{arming.PILOT_NAME}`", reaper_md)

    def test_box_md_quotes_every_script_row_and_note(self) -> None:
        names = [n for n in dir(script_check) if n.startswith(("ROW_", "NOT_REPLACED_", "NOT_CHECKED_", "NOTE_"))]
        self.assertGreaterEqual(len(names), 18)
        for name in names:
            words = getattr(script_check, name).split("{")[0]
            with self.subTest(name):
                self.assertIn(words, self.box)

    def test_troubleshooting_explains_the_script_rows_that_need_action(self) -> None:
        for words in (
            script_check.ROW_DIFFERS.split("{")[0],
            script_check.ROW_MISSING.split("{")[0],
            script_check.NOT_CHECKED_NO_FOLDER.split("{")[0],
            script_check.NOTE_COULD_NOT.split("{")[0],
            script_check.QUESTION.strip(),
        ):
            with self.subTest(words):
                self.assertIn(words, self.troubleshooting)

    def test_the_fix_on_the_day_no_longer_promises_157(self) -> None:
        self.assertNotIn("#157 will make", self.gameday)

    def test_the_runbook_quotes_the_no_config_wording(self) -> None:
        self.assertIn(serve.NO_CONFIG, self.troubleshooting)
        self.assertIn(serve.NO_CONFIG, self.box)

    def test_every_label_in_the_banner_appears_in_the_runbook(self) -> None:
        # The labels are the column the operator reads down. Renaming one
        # without the runbook leaves them looking for a row that is not there.
        # A label row is two spaces then the label. Continuation lines under a
        # row are indented further, and the rules have no spaces at all.
        labels = [line.split()[0] for line in self.lines if line[:2] == "  " and line[2:3] != " "]
        for label in sorted(set(labels)):
            with self.subTest(label=label):
                self.assertIn(label, self.box)

    def test_the_runbook_documents_the_carried_over_spans_warning(self) -> None:
        self.assertIn("still open from an earlier run", "\n".join(self._banner_with_an_open_span()))
        self.assertIn("still open from an earlier run", self.troubleshooting)

    def _banner_with_an_open_span(self) -> list[str]:
        event = annotations.lookup("q4")
        span = annotations.Entry(
            seq=1,
            event=event.key,
            category=str(event.category),
            kind=str(event.kind),
            label=event.label,
            wall="2026-09-12T15:31:00+00:00",
            monotonic=0.0,
            span_id=annotations.span_id_for(event.key, 1),
            phase=annotations.PHASE_START,
        )
        argv = ["--console-host", "10.0.0.5", "--dca", "3", "--log", "/games/g.jsonl"]
        return serve.startup_lines(serve.parser().parse_args(argv), None, open_spans=[span])

    def test_the_runbook_quotes_every_disk_refusal(self) -> None:
        # Quoted up to the first value filled in, or the first colon.
        for message in (
            disk.NOT_SET,
            disk.CHANNELS_REQUIRED,
            disk.NOT_MOUNTED,
            disk.SHORT,
            disk.LOG_SHORT,
            disk.BAD_CHANNELS,
            disk.BAD_HOURS,
        ):
            with self.subTest(message[:40]):
                quoted = message.split(":")[0].split("{")[0].strip()
                self.assertIn(quoted, self.troubleshooting)

    def test_the_runbook_quotes_the_recorder_unreachable_line(self) -> None:
        # #172: shown under the Recording panel when a send to Reaper fails. The
        # text lives in the page's script, so check both ends.
        script = (REPO_ROOT / "src" / "tacet" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('"Reaper unreachable: "', script)
        self.assertIn("Reaper unreachable:", self.troubleshooting)

    def test_the_runbook_quotes_the_stale_tap_refusal(self) -> None:
        # Quoted up to the first value filled in.
        self.assertIn(app.STALE_REFUSAL.split("{")[0].strip(), self.troubleshooting)

    def test_the_runbook_quotes_the_target_refusal_and_the_log_entry(self) -> None:
        # #9: quoted up to the first value filled in, which must not be empty
        # or the assertion below would hold of any document.
        quoted = app.TARGET_NOT_A_PRESET.split("{")[0].strip()
        self.assertTrue(quoted)
        self.assertIn(quoted, self.troubleshooting)
        self.assertIn(f"`{app.TARGET_SET}`", self.troubleshooting)

    def test_the_runbook_quotes_every_unknown_level_refusal(self) -> None:
        # #107: quoted up to the semicolon, which is where each says what to do
        # about it and the runbook says that on its own.
        for message in (state.UNKNOWN_LEVEL_ARM, state.UNKNOWN_LEVEL_MOVE, state.LEVEL_ALREADY_KNOWN):
            with self.subTest(message[:40]):
                self.assertIn(message.split(";")[0], self.troubleshooting)

    def test_the_runbook_names_the_belief_buttons_as_the_page_labels_them(self) -> None:
        # #107: the two buttons in the readout gap. Read from the page's own
        # markup rather than typed twice, and against the runbook with its
        # whitespace collapsed: a label split across a line break once hid a
        # stale one from a plain search.
        runbook = " ".join((DOCS / "gameday.md").read_text(encoding="utf-8").split())
        for button in ("btn-close-now", "btn-report-ready"):
            with self.subTest(button=button):
                found = re.search(rf'<button id="{button}">([^<]+)</button>', web.PAGE)
                self.assertIsNotNone(found, f"{button} is not on the page")
                assert found is not None
                self.assertIn(f"**{found[1]}**", runbook)

    def test_the_runbook_quotes_the_greyed_buttons_note(self) -> None:
        # #107: the line in the fader column that says why four buttons are
        # grey. It lives in the operator page's script, so it is read out of
        # there as text (as the Lua script is above) rather than typed twice,
        # and the runbook is compared with its whitespace collapsed.
        script = (REPO_ROOT / "src" / "tacet" / "static" / "app.js").read_text(encoding="utf-8")
        found = re.search(r'^const RAMPING_BLOCKED = "([^"]+)";$', script, re.MULTILINE)
        self.assertIsNotNone(found, "app.js no longer defines RAMPING_BLOCKED")
        assert found is not None
        runbook = " ".join((DOCS / "gameday.md").read_text(encoding="utf-8").split())
        self.assertIn(f"`{found[1]}`", runbook)

    def test_the_runbook_quotes_the_greyed_target_note(self) -> None:
        # #128: the sentence under the target segments while they are greyed,
        # read out of app.js like RAMPING_BLOCKED above.
        script = (REPO_ROOT / "src" / "tacet" / "static" / "app.js").read_text(encoding="utf-8")
        found = re.search(r'^const TARGET_BLOCKED = "([^"]+)";$', script, re.MULTILINE)
        self.assertIsNotNone(found, "app.js no longer defines TARGET_BLOCKED")
        assert found is not None
        runbook = " ".join((DOCS / "gameday.md").read_text(encoding="utf-8").split())
        self.assertIn(found[1], runbook)

    def test_the_runbook_quotes_the_arm_stand_down_question_copy(self) -> None:
        # #19: the page's own translation of the box's prompt kinds (PROMPT_COPY
        # in app.js), read out of there rather than typed twice, the same
        # discipline as RAMPING_BLOCKED above.
        script = (REPO_ROOT / "src" / "tacet" / "static" / "app.js").read_text(encoding="utf-8")
        found = re.search(r"const PROMPT_COPY = \{(.*?)\n\};", script, re.DOTALL)
        self.assertIsNotNone(found, "app.js no longer defines PROMPT_COPY")
        assert found is not None
        # Values only - not the "stand-down" / "arm" keys, which are not copy.
        sentences = re.findall(r'(?:known|unknown|accept):\s*"([^"]+)"', found[1])
        self.assertTrue(sentences)
        runbook = " ".join((DOCS / "gameday.md").read_text(encoding="utf-8").split())
        for sentence in sentences:
            with self.subTest(sentence=sentence[:40]):
                self.assertIn(sentence, runbook)
        self.assertIn("Not yet", runbook)

    def test_the_runbook_quotes_the_move_words(self) -> None:
        # #154: the tags and the lateness word the page shows while a move is in
        # flight, read out of app.js rather than typed twice.
        script = (REPO_ROOT / "src" / "tacet" / "static" / "app.js").read_text(encoding="utf-8")
        tags = re.search(r'const MOVE_TAGS = \{fade: "([^"]+)", ride: "([^"]+)"\};', script)
        late = re.search(r'const MOVE_LATE_WORD = "([^"]+)";', script)
        self.assertIsNotNone(tags, "app.js no longer defines MOVE_TAGS this way")
        self.assertIsNotNone(late, "app.js no longer defines MOVE_LATE_WORD")
        assert tags is not None and late is not None
        runbook = (DOCS / "gameday.md").read_text(encoding="utf-8")
        for word in (tags[1], tags[2]):
            with self.subTest(word=word):
                self.assertIn(f"`{word}`", runbook.replace("**", "`"))
        # The lateness is quoted in the readout's own words: "... - 3s late".
        phrase = f"s {late[1]}`"
        self.assertIn(phrase, runbook)
        self.assertIn(phrase, (DOCS / "troubleshooting.md").read_text(encoding="utf-8"))

    def test_the_runbook_and_troubleshooting_use_the_duty_chip_words(self) -> None:
        # #19: ARMED / STOOD DOWN, read out of dutyChip in app.js rather than
        # typed twice.
        script = (REPO_ROOT / "src" / "tacet" / "static" / "app.js").read_text(encoding="utf-8")
        found = re.search(r'duty\.armed \? "([^"]+)" : "([^"]+)"', script)
        self.assertIsNotNone(found, "app.js no longer builds the duty chip this way")
        assert found is not None
        armed, stood_down = found[1], found[2]
        for doc in ("gameday.md", "troubleshooting.md"):
            text = (DOCS / doc).read_text(encoding="utf-8")
            with self.subTest(doc=doc):
                self.assertIn(armed, text)
                self.assertIn(stood_down, text)

    def test_no_doc_still_bolds_an_older_wording_of_the_ready_report(self) -> None:
        # Presence is not enough: the stale label sat in a second paragraph
        # while the table row was right. Every bold "It's at ..." in the two
        # operator docs has to be the page's label.
        found = re.search(r'<button id="btn-report-ready">([^<]+)</button>', web.PAGE)
        assert found is not None
        for doc in ("gameday.md", "troubleshooting.md"):
            text = " ".join((DOCS / doc).read_text(encoding="utf-8").split())
            for bolded in re.findall(r"\*\*(It's at[^*]*)\*\*", text):
                with self.subTest(doc=doc, bolded=bolded):
                    self.assertEqual(bolded, found[1])

    def test_the_runbook_quotes_every_record_refusal(self) -> None:
        # #163: the line under the Recording panel says why the record button is
        # grey, in these words, and the operator reads it in a press box.
        for name in dir(reaper):
            if not name.startswith("RECORD_REFUSED_"):
                continue
            with self.subTest(name):
                self.assertIn(getattr(reaper, name), self.troubleshooting)

    def test_the_page_tests_quote_every_record_refusal(self) -> None:
        # The JS tests carry their own copies of the texts, which would drift.
        source = (REPO_ROOT / "tests" / "test_app_js.mjs").read_text(encoding="utf-8")
        for name in dir(reaper):
            if not name.startswith("RECORD_REFUSED_"):
                continue
            with self.subTest(name):
                self.assertIn(getattr(reaper, name), source.replace('"\n    + "', ""))

    def test_every_record_refusal_is_one_this_test_knows_about(self) -> None:
        names = {name for name in dir(reaper) if name.startswith("RECORD_REFUSED_")}
        self.assertEqual(len(names), 7)

    def test_the_runbook_quotes_the_stopped_writer(self) -> None:
        # Quoted up to the semicolon; the page puts "Log not saving: " before it.
        self.assertIn(annotations.WRITER_STOPPED.split(";")[0], self.troubleshooting)

    def test_the_runbook_quotes_the_missing_log_refusal(self) -> None:
        # Quoted up to the example, which the runbook writes out on its own.
        self.assertIn(serve.LOG_REQUIRED.split(":")[0], self.troubleshooting)

    def test_the_runbook_quotes_the_retired_log_key_refusal(self) -> None:
        stale = "capture.log is no longer a config key"
        with self.assertRaises(config.ConfigError) as caught:
            config.values_from_mapping({"capture": {"log": "~/games/g.jsonl"}})
        self.assertIn(stale, str(caught.exception))
        self.assertIn(stale, self.troubleshooting)

    def test_the_runbook_quotes_the_port_range_refusal(self) -> None:
        with self.assertRaises(config.ConfigError) as caught:
            config.values_from_mapping({"console": {"port": 499000}})
        self.assertIn(config.PORT_RANGE, str(caught.exception))
        self.assertIn(config.PORT_RANGE, self.troubleshooting)

    def test_the_runbook_reproduces_the_checklist_steps(self) -> None:
        for phrase in ("before kickoff", "tacet_mirror.lua running in Reaper", "arm when the band is in the stands"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, "\n".join(self.lines))
                self.assertIn(phrase, self.box)


#: A Markdown heading needs a space after its hashes; `#13).` at the start of a
#: wrapped line is an issue number, not a heading.
HEADING = re.compile(r"^#+\s+(.+)$", re.MULTILINE)

#: `[text](target.md)` or `[text](target.md#anchor)`, relative links only.
DOC_LINK = re.compile(r"\]\(([\w.-]+\.md)(?:#([\w.-]+))?\)")


def github_anchor(heading: str) -> str:
    """The id GitHub gives a Markdown heading: lower case, punctuation other
    than hyphens dropped, spaces to hyphens."""
    kept = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return kept.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {github_anchor(match[1]) for match in HEADING.finditer(text)}


class TheRunbookLinksLand(unittest.TestCase):
    """gameday.md is short because it links out (#77). A link to a renamed doc
    or heading reads fine and goes nowhere, which in a press box is a dead end."""

    def test_the_runbook_links_to_each_detail_doc(self) -> None:
        runbook = RUNBOOK.read_text(encoding="utf-8")
        for doc in (REAPER, BOX, TROUBLESHOOTING):
            with self.subTest(doc=doc.name):
                self.assertIn(f"]({doc.name}", runbook)

    def test_every_link_between_the_docs_lands(self) -> None:
        for source in (RUNBOOK, REAPER, BOX, TROUBLESHOOTING):
            for target, anchor in DOC_LINK.findall(source.read_text(encoding="utf-8")):
                with self.subTest(source=source.name, target=target, anchor=anchor):
                    path = DOCS / target
                    self.assertTrue(path.is_file(), f"{source.name} links to {target}, which does not exist")
                    if anchor:
                        self.assertIn(anchor, anchors(path), f"{source.name} links to a heading {target} lacks")

    def test_the_anchor_rule_matches_github(self) -> None:
        self.assertEqual(github_anchor(" tacet.toml"), "tacettoml")
        self.assertEqual(github_anchor(" During the game: which button when"), "during-the-game-which-button-when")

    def test_an_issue_number_at_the_start_of_a_line_is_not_a_heading(self) -> None:
        self.assertEqual({m[1] for m in HEADING.finditer("# Title\n#13). Every feed\n## Sub\n")}, {"Title", "Sub"})


if __name__ == "__main__":
    unittest.main()
