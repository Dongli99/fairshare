"""Stage 3b, part A: folder trust at launch. Same fleet as test_honest (temp HOME, config, state
and hub; fake claude and fairshare first on PATH, proved in Fleet.setUp); launches are recorded,
not made. Each test works in a project folder under the temp dir, by its canonical path."""
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from fairshare import store
from fairshare.providers import claude

from test_honest import ORG, Fleet

TRUST = claude.STATE_TRUST


class Trust(Fleet):
    def setUp(self):
        super().setUp()
        self.add("a", orgId=ORG + "a")
        self.add("b", orgId=ORG + "b")
        self.add("c", orgId=ORG + "c")
        self.snap("a", five=(10, 1), seven=(10, 50))          # a ranks first
        self.snap("b", five=(10, 1), seven=(40, 50))
        self.snap("c", five=(10, 1), seven=(50, 50))
        self.here = (self.tmp / "work" / "project")
        self.here.mkdir(parents=True)
        os.chdir(self.here)
        self.here = Path(os.getcwd())                           # the canonical spelling

    def state(self, id) -> Path:
        return self.tmp / "hub/accounts" / id / claude.STATE_FILE

    def give(self, id, data):
        self.state(id).write_text(json.dumps(data))

    def trust_of(self, id, path=None):
        if not self.state(id).exists():
            return None
        e = json.loads(self.state(id).read_text()).get("projects", {}).get(str(path or self.here))
        return e and e.get(TRUST)

    def trusted(self, *ids):
        for i in ids:
            self.give(i, {"projects": {str(self.here): {TRUST: True}}})

    def test_nothing_lacking_asks_nothing(self):
        self.trusted("a", "b", "c")
        code, _, err = self.go("run", tty=True)
        self.assertEqual(code, 0)
        self.asked.assert_not_called()
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/a"))

    def test_the_starting_account_lacking_asks_and_all_trusts_every_one_lacking(self):
        self.trusted("b")
        code, _, err = self.go("run", tty=True, answers=["a"])
        self.assertEqual(code, 0, err)
        self.assertIn("will be able to read, edit and execute files in " + str(self.here), err)
        self.assertIn("[a] trust it for all accounts lacking it (a, c)", err)
        self.assertIn("[o] only a", err)
        self.assertIn("[n] no, exit", err)
        self.assertEqual([self.trust_of(i) for i in "abc"], [True, True, True])
        self.assertEqual(len([l for l in err.splitlines() if "now trusts" in l]), 2)
        self.launched.assert_called_once()

    def test_only_trusts_the_starting_account_and_is_remembered(self):
        code, _, err = self.go("run", tty=True, answers=["o"])
        self.assertEqual(code, 0, err)
        self.assertEqual([self.trust_of(i) for i in "abc"], [True, None, None])
        self.assertTrue(store.trust_only(self.here))
        code, _, err = self.go("run", tty=True)           # a trusts it; "only" stands
        self.assertEqual(code, 0)
        self.asked.assert_not_called()

    def test_others_lacking_ask_without_no_and_only_writes_nothing(self):
        self.trusted("a")
        code, _, err = self.go("run", tty=True, answers=["n", "o"])   # n is not offered
        self.assertEqual(code, 0, err)
        self.assertEqual(self.asked.call_count, 2)
        self.assertIn("[o] only the accounts that already trust it", err)
        self.assertNotIn("[n] no, exit", err)
        self.assertEqual([self.trust_of(i) for i in "bc"], [None, None])
        self.assertTrue(store.trust_only(self.here))
        self.launched.assert_called_once()

    def test_a_remembered_only_yields_when_the_launch_lands_on_an_account_lacking_it(self):
        self.trusted("a")
        store.remember_trust_only(self.here)
        self.go("run", tty=True)
        self.asked.assert_not_called()
        self.snap("b", five=(10, 1), seven=(1, 50))          # now b ranks first, and lacks it
        code, _, err = self.go("run", tty=True, answers=["o"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.launched_dir(), str(self.tmp / "hub/accounts/b"))
        self.assertTrue(self.trust_of("b"))
        self.assertIsNone(self.trust_of("c"))

    def test_no_exits_1_and_writes_nothing(self):
        for answer in ("n", EOFError()):
            with self.subTest(answer=answer):
                code, _, err = self.go("run", tty=True, answers=[answer])
                self.assertEqual(code, 1)
                self.launched.assert_not_called()
                self.assertEqual([self.trust_of(i) for i in "abc"], [None, None, None])
                self.assertFalse(store.trust_only(self.here))

    def test_enter_or_anything_else_asks_again(self):
        code, _, err = self.go("run", tty=True, answers=["", "yes", "x", "a"])
        self.assertEqual(code, 0)
        self.assertEqual(self.asked.call_count, 4)
        self.assertTrue(all(self.trust_of(i) for i in "abc"))

    def test_run_a_asks_about_that_account_only_and_remembers_nothing(self):
        self.trusted("a")
        code, _, err = self.go("run", "-a", "b", tty=True, answers=["y"])
        self.assertEqual(code, 0, err)
        self.assertIn("[y] trust it for b", err)
        self.assertEqual([self.trust_of("b"), self.trust_of("c")], [True, None])
        self.assertFalse(store.trust_only(self.here))
        code, _, err = self.go("run", "-a", "c", tty=True, answers=["o", "n"])
        self.assertEqual(code, 1)
        self.assertEqual(self.asked.call_count, 2)         # o is not an answer here
        self.assertIsNone(self.trust_of("c"))
        self.go("run", "-a", "a", tty=True)                  # a trusts it: no question
        self.asked.assert_not_called()

    def test_in_home_nothing_is_asked_or_written(self):
        os.chdir(self.tmp / "home")
        code, _, err = self.go("run", tty=True)
        self.assertEqual(code, 0)
        self.asked.assert_not_called()
        self.assertIn("In HOME, fairshare does not ask; Claude Code's own per-session question "
                      "applies", err)
        self.assertFalse(any(self.state(i).exists() for i in "abc"))

    def test_a_folder_inside_home_is_asked_about_as_usual(self):
        sub = self.tmp / "home" / "code"
        sub.mkdir()
        os.chdir(sub)
        self.go("run", tty=True, answers=["a"])
        self.assertTrue(self.trust_of("a", Path(os.getcwd())))

    def test_without_a_terminal_nothing_is_asked_or_written(self):
        code, _, _ = self.go("run")
        self.assertEqual(code, 0)
        self.asked.assert_not_called()
        self.assertFalse(any(self.state(i).exists() for i in "abc"))
        self.launched.assert_called_once()

    def test_trust_on_a_parent_counts(self):
        for i in "abc":
            self.give(i, {"projects": {str(self.here.parent): {TRUST: True}}})
        self.go("run", tty=True)
        self.asked.assert_not_called()

    def test_the_write_keeps_every_other_key_and_value_backs_up_and_reads_back(self):
        other = {"userID": "u-1", "numStartups": 7, "ratio": 1.0, "big": 12345678901234567890,
                 "nested": {"x": [1, {"y": None}], "s": "é"},
                 "projects": {"/elsewhere": {TRUST: True, "allowedTools": ["Bash"]},
                              str(self.here): {"allowedTools": ["Read"], TRUST: False}}}
        self.give("a", other)
        before = self.state("a").read_text()
        self.go("run", "-a", "a", tty=True, answers=["y"])
        after = json.loads(self.state("a").read_text())
        want = json.loads(before)
        want["projects"][str(self.here)][TRUST] = True
        self.assertEqual(after, want)
        backups = list((self.tmp / "hub/backups").glob("*-trust-a/" + claude.STATE_FILE))
        self.assertEqual([b.read_text() for b in backups], [before])

    def test_a_missing_state_file_is_created_holding_only_that_entry(self):
        self.go("run", "-a", "a", tty=True, answers=["y"])
        self.assertEqual(json.loads(self.state("a").read_text()),
                         {"projects": {str(self.here): {TRUST: True}}})

    def test_a_file_that_cannot_be_read_is_refused_and_left_byte_for_byte(self):
        self.state("a").write_text("{not json")
        code, _, err = self.go("run", "-a", "a", tty=True, answers=["y"])
        self.assertEqual(code, 0)                          # Claude Code asks for itself
        self.assertIn("a: trust not written", err)
        self.assertEqual(self.state("a").read_text(), "{not json")

    def test_a_held_lock_times_out_and_writes_nothing(self):
        lock = self.tmp / "hub/accounts/a" / claude.STATE_LOCK
        lock.mkdir()
        with mock.patch.object(claude, "STATE_LOCK_WAIT", 0.3):
            code, _, err = self.go("run", "-a", "a", tty=True, answers=["y"])
        self.assertIn("a: trust not written", err)
        self.assertIn("held by someone else", err)
        self.assertFalse(self.state("a").exists())
        self.assertTrue(lock.is_dir())                     # someone else's: left alone

    def test_a_left_over_lock_is_taken_over(self):
        lock = self.tmp / "hub/accounts/a" / claude.STATE_LOCK
        lock.mkdir()
        old = time.time() - claude.STATE_LOCK_STALE - 5
        os.utime(lock, (old, old))
        self.go("run", "-a", "a", tty=True, answers=["y"])
        self.assertTrue(self.trust_of("a"))
        self.assertFalse(lock.exists())

    def test_a_write_that_does_not_read_back_is_reported(self):
        real = store.write_with_backup

        def wrong(path, text, backups=None):
            return real(path, text.replace("true", "false"), backups)
        with mock.patch.object(store, "write_with_backup", wrong):
            code, _, err = self.go("run", "-a", "a", tty=True, answers=["y"])
        self.assertIn("a: trust not written", err)
        self.assertIn("did not read back as written", err)
        self.assertNotIn("now trusts", err)

    def test_the_lock_is_held_while_writing(self):
        lock = self.tmp / "hub/accounts/a" / claude.STATE_LOCK
        seen, real = [], store.write_with_backup

        def spy(path, text, backups=None):
            seen.append(lock.is_dir())
            return real(path, text, backups)
        with mock.patch.object(store, "write_with_backup", spy):
            self.go("run", "-a", "a", tty=True, answers=["y"])
        self.assertEqual(seen, [True])


class GitRule(unittest.TestCase):
    """Claude Code's lookup rule, on a fake layout (verified live on 2.1.322, see the
    architecture's public-surface table)."""

    def setUp(self):
        import tempfile
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.w = Path(t.name).resolve()
        self.repo = self.w / "repo"
        (self.repo / ".git" / "worktrees" / "wt").mkdir(parents=True)
        (self.repo / "sub" / "deeper").mkdir(parents=True)
        (self.repo / ".git" / "worktrees" / "wt" / "commondir").write_text("../..\n")
        self.wt = self.w / "wt"
        (self.wt / "wsub").mkdir(parents=True)
        (self.wt / ".git").write_text(f"gitdir: {self.repo}/.git/worktrees/wt\n")
        (self.w / "plain" / "deeper").mkdir(parents=True)

    def test_outside_a_repository_every_parent_counts_and_the_folder_is_written(self):
        p = self.w / "plain" / "deeper"
        self.assertEqual(claude.trust_paths(p)[:3], [p, p.parent, self.w])
        self.assertEqual(claude.trust_key(p), p)

    def test_inside_a_repository_the_walk_stops_at_its_root_which_is_written(self):
        p = self.repo / "sub" / "deeper"
        self.assertEqual(claude.trust_paths(p), [p, p.parent, self.repo])
        self.assertEqual(claude.trust_key(p), self.repo)
        self.assertNotIn(self.w, claude.trust_paths(self.repo))

    def test_a_worktree_also_counts_the_main_repositorys_root_which_is_written(self):
        p = self.wt / "wsub"
        self.assertEqual(claude.trust_paths(p), [p, self.wt, self.repo])
        self.assertEqual(claude.trust_key(p), self.repo)

    def test_trusts_follows_the_rule(self):
        cfg = self.w / "cfg"
        cfg.mkdir()

        def trusting(*paths):
            (cfg / claude.STATE_FILE).write_text(json.dumps(
                {"projects": {str(p): {TRUST: True} for p in paths}}))
        trusting(self.w)
        self.assertTrue(claude.trusts(cfg, self.w / "plain" / "deeper"))
        self.assertFalse(claude.trusts(cfg, self.repo / "sub"))      # parent above the root
        self.assertFalse(claude.trusts(cfg, self.wt))
        trusting(self.repo / "sub")
        self.assertTrue(claude.trusts(cfg, self.repo / "sub" / "deeper"))
        self.assertFalse(claude.trusts(cfg, self.repo))
        trusting(self.repo)
        self.assertTrue(claude.trusts(cfg, self.wt / "wsub"))
        (cfg / claude.STATE_FILE).write_text("[1]")
        self.assertIsNone(claude.trusts(cfg, self.repo))
        (cfg / claude.STATE_FILE).unlink()
        self.assertFalse(claude.trusts(cfg, self.repo))


class Copied(unittest.TestCase):
    def test_join_may_carry_the_chrome_consent_after_asking(self):
        src = {"hasCompletedClaudeInChromeOnboarding": True, "claudeInChromeDefaultEnabled": False,
               "oauthAccount": {"emailAddress": "x@example.invalid"}}
        new, what = claude.merge_state({"userID": "mine"}, src)
        self.assertEqual(new, {"userID": "mine", "hasCompletedClaudeInChromeOnboarding": True,
                               "claudeInChromeDefaultEnabled": False})
        self.assertEqual(what, ["Claude in Chrome consent (hasCompletedClaudeInChromeOnboarding, "
                                "claudeInChromeDefaultEnabled)"])


class DoctorState(Fleet):
    def test_the_trust_field_and_a_readable_state_file_are_checked(self):
        self.add("a", orgId=ORG + "a")
        state = self.tmp / "hub/accounts/a" / claude.STATE_FILE
        for text, line in (
                (json.dumps({"projects": {"/p": {TRUST: True}}}),
                 "pass  a: state file reads, trust field found"),
                (json.dumps({"projects": {"/p": {"hasTrustAccepted2": True}}}),
                 "FAIL  a: trust field not found; Claude Code may have renamed it"),
                (json.dumps({"numStartups": 1}), "pass  a: state file reads"),
                ("{bad", "FAIL  a: state file cannot be read (JSONDecodeError)")):
            with self.subTest(line=line):
                state.write_text(text)
                self.assertIn(line, self.go("doctor")[1].splitlines())


if __name__ == "__main__":
    unittest.main()
