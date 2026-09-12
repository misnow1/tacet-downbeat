"""The runbook's quoted console messages, pinned to the script that emits them.

`docs/gameday.md` tells the operator what the mirror script's console lines mean
and what to do about each one. That table is read in a press box, by someone who
cannot go and check the source, so a message renamed in the Lua and left stale in
the runbook is worse than no table at all.

Both directions are asserted: every message below appears verbatim in the script
and verbatim in the runbook. Renaming one without the other fails here.
"""

import unittest
from pathlib import Path

from tacet import serve

REPO_ROOT = Path(__file__).resolve().parent.parent
MIRROR_SCRIPT = REPO_ROOT / "reaper" / "tacet_mirror.lua"
RUNBOOK = REPO_ROOT / "docs" / "gameday.md"

#: What the mirror script says at startup. Each of these decides whether the
#: operator has to do anything, so each is documented (docs/gameday.md, "The
#: mirror console").
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
        self.runbook = RUNBOOK.read_text(encoding="utf-8")

    def test_every_documented_message_is_one_the_script_emits(self) -> None:
        for message in STARTUP_MESSAGES:
            with self.subTest(message=message):
                self.assertIn(f'log("{message}', self.script)

    def test_every_documented_message_appears_in_the_runbook(self) -> None:
        for message in STARTUP_MESSAGES:
            with self.subTest(message=message):
                self.assertIn(message.strip(), self.runbook)

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
    """`docs/gameday.md` reproduces the startup banner, so it can go stale.

    The banner is the first thing read on the day and the runbook is what it is
    read against. A wording change on one side and not the other turns the
    check in step 3 into a comparison against something that no longer exists.
    """

    def setUp(self) -> None:
        self.runbook = RUNBOOK.read_text(encoding="utf-8")
        argv = ["--console-host", "10.0.0.5", "--dca", "3", "--log", "/games/g.jsonl"]
        argv += ["--queue", "/queue/tacet.tsv", "--reaper-host", "127.0.0.1"]
        self.lines = serve.startup_lines(serve.parser().parse_args(argv), None)

    def test_the_runbook_quotes_the_no_config_wording(self) -> None:
        self.assertIn(serve.NO_CONFIG, self.runbook)

    def test_every_label_in_the_banner_appears_in_the_runbook(self) -> None:
        # The labels are the column the operator reads down. Renaming one
        # without the runbook leaves them looking for a row that is not there.
        # A label row is two spaces then the label. Continuation lines under a
        # row are indented further, and the rules have no spaces at all.
        labels = [line.split()[0] for line in self.lines if line[:2] == "  " and line[2:3] != " "]
        for label in sorted(set(labels)):
            with self.subTest(label=label):
                self.assertIn(label, self.runbook)

    def test_the_runbook_documents_the_reused_log_warning(self) -> None:
        # The warning is only useful if the table says what to do about it.
        self.assertIn("this log already contains a recording", self.runbook)

    def test_the_runbook_reproduces_the_checklist_steps(self) -> None:
        for phrase in ("before kickoff", "tacet_mirror.lua running in Reaper", "arm when the band is in the stands"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, "\n".join(self.lines))
                self.assertIn(phrase, self.runbook)


if __name__ == "__main__":
    unittest.main()
