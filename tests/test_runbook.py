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


if __name__ == "__main__":
    unittest.main()
