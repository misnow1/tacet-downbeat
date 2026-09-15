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
"""

import re
import unittest
from pathlib import Path

from tacet import annotations, config, serve

REPO_ROOT = Path(__file__).resolve().parent.parent
MIRROR_SCRIPT = REPO_ROOT / "reaper" / "tacet_mirror.lua"
DOCS = REPO_ROOT / "docs"
RUNBOOK = DOCS / "gameday.md"
#: Where each message is explained now that gameday.md links out (#77).
TROUBLESHOOTING = DOCS / "troubleshooting.md"
BOX = DOCS / "box.md"
REAPER = DOCS / "reaper.md"

#: What the mirror script says at startup. Each of these decides whether the
#: operator has to do anything, so each is documented (docs/troubleshooting.md,
#: "The mirror console").
STARTUP_MESSAGES = (
    "mirroring ",
    "queue does not exist yet; waiting for the box to create it",
    "no remembered position in a queue with history; starting at its end",
    "queue is shorter than the stored offset; starting from the beginning",
    "no queue path given; not starting",
    "mirror stopped",
)


class StartupMessagesAreDocumented(unittest.TestCase):
    def setUp(self) -> None:
        self.script = MIRROR_SCRIPT.read_text(encoding="utf-8")
        self.troubleshooting = TROUBLESHOOTING.read_text(encoding="utf-8")

    def test_every_documented_message_is_one_the_script_emits(self) -> None:
        for message in STARTUP_MESSAGES:
            with self.subTest(message=message):
                self.assertIn(f'log("{message}', self.script)

    def test_every_documented_message_appears_in_the_runbook(self) -> None:
        for message in STARTUP_MESSAGES:
            with self.subTest(message=message):
                self.assertIn(message.strip(), self.troubleshooting)

    def test_the_script_logs_nothing_at_startup_that_is_undocumented(self) -> None:
        """Guards the gap the other two cannot: a new startup message that is
        never added to the table."""
        startup = self.script.split("local function main()")[1]
        logged = [line.split('log("')[1].split('"')[0] for line in startup.splitlines() if 'log("' in line]
        for message in logged:
            with self.subTest(message=message):
                self.assertTrue(
                    any(message.startswith(known) for known in STARTUP_MESSAGES),
                    f"main() logs {message!r}, which is not in STARTUP_MESSAGES "
                    f"and so is probably not in the runbook either",
                )


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
        self.lines = serve.startup_lines(serve.parser().parse_args(argv), None)

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

    def test_the_runbook_documents_the_reused_log_warning(self) -> None:
        # The warning is only useful if the table says what to do about it.
        self.assertIn("this log already contains a recording", self.troubleshooting)

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
