"""What code the box is running (#157): the pure parts, the probe, and real git.

The probe never runs against the checkout the tests run in. A fake runner stands
in for git almost everywhere; the real-git tests build a temporary repository
with an isolated configuration.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from tacet import provenance as prov

COMMIT = "0123456789abcdef0123456789abcdef01234567"
HEADERS = f"# branch.oid {COMMIT}\0# branch.head main\0# branch.upstream origin/main\0# branch.ab +0 -0\0"
CLEAN = HEADERS.encode()
DETACHED = f"# branch.oid {COMMIT}\0# branch.head (detached)\0".encode()
INITIAL = b"# branch.oid (initial)\0# branch.head main\0"
HASH = "a" * 40
ORDINARY = f"1 .M N... 100644 100644 100644 {HASH} {HASH} src/a.py\0"
STAGED = f"1 M. N... 100644 100644 100644 {HASH} {HASH} docs/a.md\0"
UNMERGED = f"u UU N... 100644 100644 100644 100644 {HASH} {HASH} {HASH} src/b.py\0"


def _renamed(original: str) -> str:
    return f"2 R. N... 100644 100644 100644 {HASH} {HASH} R100 c.txt\0{original}\0"


def _dirty(*records: str) -> bytes:
    return (HEADERS + "".join(records)).encode()


class FakeGit:
    def __init__(
        self,
        output: bytes | None = None,
        *,
        returncode: int = 0,
        stderr: bytes = b"",
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self._output = CLEAN if output is None else output
        self._returncode = returncode
        self._stderr = stderr
        self._raises = raises

    def run(self, args: Sequence[str], *, timeout: float) -> prov.GitOutput:
        self.calls.append((list(args), timeout))
        if self._raises is not None:
            raise self._raises
        return prov.GitOutput(self._returncode, self._output, self._stderr)


class TestParseStatus(unittest.TestCase):
    def test_a_clean_branch_reads_its_commit_and_branch(self):
        status = prov.parse_status(CLEAN)
        self.assertEqual(status, prov.Status(COMMIT, "main", False, False, 0))

    def test_upstream_and_ahead_behind_headers_are_ignored(self):
        self.assertFalse(prov.parse_status(CLEAN).dirty)

    def test_a_detached_head_has_no_branch_and_says_detached(self):
        status = prov.parse_status(DETACHED)
        self.assertTrue(status.detached)
        self.assertIsNone(status.branch)
        self.assertEqual(status.commit, COMMIT)

    def test_a_repository_with_no_commits_has_no_commit(self):
        status = prov.parse_status(INITIAL)
        self.assertIsNone(status.commit)
        self.assertEqual(status.branch, "main")

    def test_an_unstaged_change_is_dirty(self):
        self.assertTrue(prov.parse_status(_dirty(ORDINARY)).dirty)

    def test_a_staged_change_is_dirty(self):
        self.assertTrue(prov.parse_status(_dirty(STAGED)).dirty)

    def test_an_unmerged_path_is_dirty(self):
        self.assertTrue(prov.parse_status(_dirty(UNMERGED)).dirty)

    def test_a_rename_is_dirty_and_its_original_path_is_not_read_as_a_record(self):
        for original in ("? src/x.py", "1 x", "garbage"):
            status = prov.parse_status(_dirty(_renamed(original)))
            self.assertTrue(status.dirty)
            self.assertEqual(status.untracked, 0)

    def test_an_untracked_file_under_src_is_dirty(self):
        status = prov.parse_status(_dirty("? src/new.py\0"))
        self.assertTrue(status.dirty)
        self.assertEqual(status.untracked, 1)

    def test_an_untracked_file_elsewhere_is_counted_and_not_dirty(self):
        status = prov.parse_status(_dirty("? junk.txt\0", "? notes/x.txt\0"))
        self.assertFalse(status.dirty)
        self.assertEqual(status.untracked, 2)

    def test_an_unrecognised_record_is_refused(self):
        with self.assertRaises(ValueError):
            prov.parse_status(_dirty("! ignored.txt\0"))

    def test_output_without_the_branch_headers_is_refused(self):
        for output in (b"", ORDINARY.encode(), b"# branch.oid abc\0", b"# branch.head main\0"):
            with self.assertRaises(ValueError):
                prov.parse_status(output)


class _TreeCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


class TestProbe(_TreeCase):
    def test_no_dot_git_is_not_a_checkout_and_git_is_never_asked(self):
        git = FakeGit()
        result = prov.probe(self.root, runner=git)
        self.assertEqual(result, prov.Provenance.not_a_checkout())
        self.assertEqual(result.source, prov.Source.NOT_A_CHECKOUT)
        self.assertEqual(git.calls, [])

    def test_a_dot_git_directory_is_the_main_checkout(self):
        (self.root / prov.GIT_DIR_NAME).mkdir()
        result = prov.probe(self.root, runner=FakeGit())
        self.assertIs(result.worktree, False)
        self.assertEqual(result.source, prov.Source.CHECKOUT)

    def test_a_dot_git_file_is_a_linked_worktree(self):
        (self.root / prov.GIT_DIR_NAME).write_text("gitdir: /elsewhere\n")
        self.assertIs(prov.probe(self.root, runner=FakeGit()).worktree, True)

    def test_it_asks_git_once_with_the_exact_command_and_the_timeout(self):
        (self.root / prov.GIT_DIR_NAME).mkdir()
        git = FakeGit()
        prov.probe(self.root, runner=git)
        self.assertEqual(git.calls, [(prov.status_args(self.root), prov.GIT_TIMEOUT_SECONDS)])
        args = git.calls[0][0]
        for flag in ("--no-optional-locks", "-z", "--porcelain=v2", "--branch"):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index("-C") + 1], str(self.root))

    def _unknown(self, git: FakeGit) -> prov.Provenance:
        (self.root / prov.GIT_DIR_NAME).mkdir()
        result = prov.probe(self.root, runner=git)
        self.assertEqual(result.source, prov.Source.UNKNOWN)
        self.assertEqual(result.path, str(self.root))
        self.assertIs(result.worktree, False)
        return result

    def test_git_missing_is_unknown_and_says_so(self):
        result = self._unknown(FakeGit(raises=prov.GitUnavailableError(prov.GIT_NOT_FOUND)))
        self.assertEqual(result.error, prov.GIT_NOT_FOUND)

    def test_a_timeout_is_unknown_and_names_the_limit(self):
        message = prov.GIT_TIMED_OUT.format(seconds=prov.GIT_TIMEOUT_SECONDS)
        result = self._unknown(FakeGit(raises=prov.GitUnavailableError(message)))
        self.assertEqual(result.error, message)
        self.assertIn("5s", message)

    def test_a_failing_git_is_unknown_with_its_first_stderr_line(self):
        stderr = b"\nfatal: detected dubious ownership in repository at '/x'\nmore\n"
        result = self._unknown(FakeGit(returncode=128, stderr=stderr))
        self.assertEqual(
            result.error,
            prov.GIT_FAILED.format(code=128, detail="fatal: detected dubious ownership in repository at '/x'"),
        )

    def test_unreadable_output_is_unknown(self):
        result = self._unknown(FakeGit(b"nonsense\0"))
        assert result.error is not None
        self.assertTrue(result.error.startswith(prov.GIT_UNREADABLE.split("{")[0]))

    def test_any_other_exception_is_unknown_not_a_crash(self):
        result = self._unknown(FakeGit(raises=RuntimeError("boom")))
        self.assertEqual(result.error, prov.PROBE_FAILED.format(error="RuntimeError: boom"))

    def test_a_dirty_checkout_records_everything(self):
        (self.root / prov.GIT_DIR_NAME).mkdir()
        result = prov.probe(self.root, runner=FakeGit(_dirty(ORDINARY, "? junk.txt\0")))
        self.assertEqual(
            result,
            prov.Provenance(
                source=prov.Source.CHECKOUT,
                commit=COMMIT,
                branch="main",
                detached=False,
                dirty=True,
                untracked=1,
                worktree=False,
                path=str(self.root),
                error=None,
            ),
        )


class TestSubprocessGit(unittest.TestCase):
    def test_a_missing_executable_is_unavailable(self):
        with self.assertRaises(prov.GitUnavailableError) as caught:
            prov.SubprocessGit("tacet-no-such-git-157").run([], timeout=5)
        self.assertEqual(str(caught.exception), prov.GIT_NOT_FOUND)

    def test_a_timeout_is_unavailable_and_says_how_long(self):
        with self.assertRaises(prov.GitUnavailableError) as caught:
            prov.SubprocessGit(sys.executable).run(["-c", "import time; time.sleep(10)"], timeout=0.2)
        self.assertEqual(str(caught.exception), prov.GIT_TIMED_OUT.format(seconds=0.2))

    def test_it_returns_exit_code_and_both_streams(self):
        code = "import sys; sys.stdout.write('a'); sys.stderr.write('b'); sys.exit(3)"
        out = prov.SubprocessGit(sys.executable).run(["-c", code], timeout=10)
        self.assertEqual(out, prov.GitOutput(3, b"a", b"b"))


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TestAgainstARealRepository(_TreeCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        # No user or system configuration can reach these repositories.
        self.env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(self.root / "none"),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "-q", "-b", "main")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("x = 1\n")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "a",
        )

    def git(self, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=self.repo, env=self.env, check=True, capture_output=True, text=True)
        return done.stdout.strip()

    def probe(self, root: Path) -> prov.Provenance:
        # The real subprocess runner, under the isolated environment the test built.
        old = dict(os.environ)
        os.environ.update(self.env)

        def restore() -> None:
            os.environ.clear()
            os.environ.update(old)

        self.addCleanup(restore)
        return prov.probe(root)

    def test_a_clean_main_checkout(self):
        result = self.probe(self.repo)
        self.assertEqual(result.source, prov.Source.CHECKOUT)
        self.assertEqual(result.commit, self.git("rev-parse", "HEAD"))
        self.assertEqual((result.branch, result.dirty, result.worktree), ("main", False, False))

    def test_a_dirty_linked_worktree_on_a_branch(self):
        wt = self.root / "wt"
        self.git("worktree", "add", "-q", str(wt), "-b", "fix")
        (wt / "src" / "a.py").write_text("x = 2\n")
        result = self.probe(wt)
        self.assertEqual((result.branch, result.dirty, result.worktree), ("fix", True, True))

    def test_a_detached_head(self):
        self.git("checkout", "-q", "--detach")
        result = self.probe(self.repo)
        self.assertIs(result.detached, True)
        self.assertIsNone(result.branch)
        self.assertIn(prov.DETACHED, result.summary())


class TestDescribing(unittest.TestCase):
    def _checkout(self, **changes: object) -> prov.Provenance:
        base = prov.Provenance(
            source=prov.Source.CHECKOUT,
            commit=COMMIT,
            branch="main",
            detached=False,
            dirty=False,
            untracked=0,
            worktree=False,
            path="/checkout",
            error=None,
        )
        return dataclasses.replace(base, **changes)  # type: ignore[arg-type]

    def test_the_summary_says_branch_and_short_commit(self):
        self.assertEqual(self._checkout().summary(), "main @ 0123456")

    def test_the_summary_says_worktree_detached_and_no_commits(self):
        self.assertEqual(self._checkout(branch="157-fix", worktree=True).summary(), "157-fix @ 0123456 (worktree)")
        self.assertEqual(self._checkout(branch=None, detached=True).summary(), "detached HEAD @ 0123456")
        self.assertEqual(self._checkout(commit=None).summary(), "main @ no commits yet")

    def test_the_summary_adds_uncommitted_changes_when_dirty(self):
        self.assertEqual(self._checkout(dirty=True).summary(), "main @ 0123456, uncommitted changes")

    def test_not_a_checkout_says_exactly_that(self):
        self.assertEqual(prov.NOT_A_CHECKOUT, "not a git checkout")
        self.assertEqual(prov.Provenance.not_a_checkout().summary(), "not a git checkout")

    def test_unknown_says_why(self):
        result = prov.Provenance.unknown("git exploded", path=Path("/c"), worktree=False)
        self.assertEqual(result.summary(), "unknown - git exploded")

    def test_as_data_is_flat_json_safe_and_complete(self):
        data = self._checkout(dirty=True).as_data()
        self.assertEqual(
            set(data),
            {"source", "commit", "branch", "detached", "dirty", "untracked", "worktree", "path", "error"},
        )
        self.assertEqual(data["commit"], COMMIT)
        self.assertEqual(data["source"], "checkout")
        json.dumps(data, allow_nan=False)

    def test_as_snapshot_carries_only_what_the_page_needs(self):
        snap = self._checkout(dirty=True, worktree=True, branch="fix").as_snapshot()
        self.assertEqual(
            snap, {"source": "checkout", "dirty": True, "where": "fix @ 0123456 (worktree)", "error": None}
        )

    def test_the_short_commit_is_seven_characters(self):
        self.assertEqual(self._checkout().short_commit, COMMIT[:7])
        self.assertEqual(prov.SHORT_COMMIT_LENGTH, 7)

    def test_the_checkout_root_is_the_src_layout_root(self):
        self.assertTrue((prov.CHECKOUT_ROOT / "src" / "tacet" / "provenance.py").is_file())
        self.assertTrue((prov.CHECKOUT_ROOT / "pyproject.toml").is_file())

    def test_every_word_is_ascii(self):
        words = [
            prov.NOT_A_CHECKOUT,
            prov.DETACHED,
            prov.NO_COMMITS,
            prov.WORKTREE,
            prov.DIRTY,
            prov.UNKNOWN_PREFIX,
            prov.GIT_NOT_FOUND,
            prov.GIT_TIMED_OUT,
            prov.GIT_COULD_NOT_RUN,
            prov.GIT_FAILED,
            prov.GIT_UNREADABLE,
            prov.PROBE_FAILED,
        ]
        for word in words:
            self.assertTrue(word.isascii(), word)


if __name__ == "__main__":
    unittest.main()
