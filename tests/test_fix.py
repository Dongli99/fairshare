"""Stage 3b, part B: a moved hub stops a launch instead of being rebuilt, and `doctor --fix`
finds it again, relinks, rewires a stale usage hook and offers to rename the old path in the
shared settings. Same setup as test_hub (temp HOME, config, state, hub; fake claude and
fairshare first on PATH, proved in Base.setUp)."""
import contextlib
import io
import json
import os
from pathlib import Path
from unittest import mock

from fairshare import hub, store
from fairshare.model import load_accounts

from test_hub import LOGIN, Base


class Moved(Base):
    def setUp(self):
        super().setUp()
        self.config()
        self.person("a", "ann")
        usage = mock.patch.dict(os.environ, {"FAKE_USAGE": str(
            Path(__file__).parent / "fixtures/claude-2.1.322/usage-week-hot.json")})
        usage.start()                    # doctor's usage probe line passes
        self.addCleanup(usage.stop)
        self.assertEqual(self.go("join", "a"), 0, self.err)
        self.settings = hub.shared_dir(self.hub) / "settings.json"
        self.new = self.tmp / "elsewhere" / "hub"

    def move(self):
        self.new.parent.mkdir(parents=True)
        os.rename(self.hub, self.new)

    def assert_not_rebuilt(self):
        self.assertFalse(self.hub.exists())

    def test_a_launch_stops_and_builds_nothing_at_the_old_path(self):
        self.move()
        for argv in (["run"], ["run", "-a", "a"], ["status"]):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as e:
                    self.go(*argv)
                self.assertEqual(str(e.exception), f"fairshare: no hub at {self.hub}; if you "
                                 "moved it, run `fairshare doctor --fix`")
                self.assert_not_rebuilt()
        self.assertEqual(self.calls("a"), ["auth login", "auth status --json"])   # join's only

    def test_a_missing_account_folder_also_stops_the_launch(self):
        os.rename(hub.account_dir(self.hub, "a"), self.tmp / "aside")
        from fairshare import cli
        from fairshare.model import load_accounts as la
        acct = la(self.cfg)[0][0]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli._launch(type("V", (), {"account": acct, "reason": "r"})(), [])
        self.assertIn("no hub at", err.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(hub.account_dir(self.hub, "a").exists())

    def test_doctor_without_fix_says_so_and_runs_no_sign_in_check(self):
        self.move()
        self.assertEqual(self.go("doctor"), 1)
        self.assertIn("FAIL  no hub at the configured path; if you moved it, run `fairshare doctor "
                      "--fix`", self.out)
        self.assert_not_rebuilt()

    def test_fix_with_hub_points_the_config_at_it_and_ends_fixed(self):
        self.move()
        self.assertEqual(self.go("doctor", "--fix", "--hub", str(self.new)), 0, self.out)
        self.assertEqual(load_accounts(self.cfg)[2], self.new)
        self.assertIn(f"fixed  hub: now {self.new} (was {self.hub})", self.out)
        self.assertTrue(self.cfg.read_text().startswith(f'hub = "{self.new}"\n'))
        self.assert_not_rebuilt()
        self.assert_linked("a", self.new)
        self.assertEqual(self.go("run"), 0, self.err)        # and it launches from there
        self.assertEqual(self.env_of("a")["CLAUDE_CONFIG_DIR"], str(hub.account_dir(self.new, "a")))

    def test_fix_asks_where_the_hub_is_on_a_terminal(self):
        self.move()
        code = self.go("doctor", "--fix", tty=True,
                       answers=[str(self.tmp / "nope"), str(self.new)])
        self.assertEqual(code, 0, self.out)
        self.assertIn("holds no shared/ and accounts/ folders", self.out)
        self.assertEqual(load_accounts(self.cfg)[2], self.new)

    def test_without_a_terminal_or_hub_it_lists_what_is_left_and_exits_1(self):
        self.move()
        self.assertEqual(self.go("doctor", "--fix"), 1)
        self.assertIn("FAIL   no hub at", self.out)
        self.assertEqual(load_accounts(self.cfg)[2], self.hub)
        self.asked.assert_not_called()
        self.assert_not_rebuilt()

    def test_a_folder_that_is_not_a_hub_is_refused(self):
        self.move()
        (self.tmp / "nothub").mkdir()
        self.assertEqual(self.go("doctor", "--fix", "--hub", str(self.tmp / "nothub")), 1)
        self.assertEqual(load_accounts(self.cfg)[2], self.hub)

    def hook_naming_the_old_hub(self):
        d = json.loads(self.settings.read_text())
        d.setdefault("hooks", {})["SessionStart"] = [{"hooks": [
            {"type": "command", "command": f"{self.hub}/shared/start.sh --x"}]}]
        d["other"] = f"{self.hub}2/kept"               # a different folder: never renamed
        self.settings.write_text(json.dumps(d, indent=2) + "\n")
        return self.settings.read_text()

    def test_the_old_path_in_the_shared_settings_is_renamed_after_a_yes(self):
        before = self.hook_naming_the_old_hub()
        self.move()
        code = self.go("doctor", "--fix", "--hub", str(self.new), tty=True, answers=["y"])
        self.assertEqual(code, 0, self.out)
        self.assertIn(f'-            "command": "{self.hub}/shared/start.sh --x"', self.out)
        self.assertIn(f'+            "command": "{self.new}/shared/start.sh --x"', self.out)
        self.assertIn("hooks.SessionStart[0].hooks[0].command", self.out)
        after = (hub.shared_dir(self.new) / "settings.json").read_text()
        self.assertEqual(after, before.replace(f"{self.hub}/shared", f"{self.new}/shared"))
        self.assertEqual(len(list((self.new / "backups").glob("*-settings/settings.json"))), 1)

    def test_without_a_yes_the_old_path_is_listed_and_left(self):
        before = self.hook_naming_the_old_hub()
        self.move()
        for kw in ({"tty": True, "answers": ["n"]}, {}):
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                self.assertEqual(self.go("doctor", "--fix", "--hub", str(self.new), **kw), 1)
                self.assertIn("settings.json still names the old hub in "
                              "hooks.SessionStart[0].hooks[0].command", self.out)
                self.assertEqual((hub.shared_dir(self.new) / "settings.json").read_text(), before)
                self.cfg.write_text(f'hub = "{self.hub}"\n' + self.cfg.read_text().split("\n", 1)[1])

    def test_a_stale_usage_hook_is_rewired_with_its_diff(self):
        d = json.loads(self.settings.read_text())
        d["statusLine"]["command"] = f"{self.tmp}/gone/fairshare capture --then 'my line'"
        self.settings.write_text(json.dumps(d, indent=2) + "\n")
        self.assertEqual(self.go("doctor"), 1)
        self.assertEqual(self.go("doctor", "--fix"), 0, self.out)
        self.assertIn(f'-    "command": "{self.tmp}/gone/fairshare capture --then \'my line\'"',
                      self.out)
        self.assertIn(f'+    "command": "{self.bin}/fairshare capture --then \'my line\'"', self.out)
        self.assertIn("fixed  usage hook: updated", self.out)
        self.assertEqual(json.loads(self.settings.read_text())["statusLine"]["command"],
                         f"{self.bin}/fairshare capture --then 'my line'")

    def test_fix_runs_the_start_check_and_reports_each_repair(self):
        link = hub.account_dir(self.hub, "a") / "skills"
        link.unlink()
        self.assertEqual(self.go("doctor", "--fix"), 0, self.out)
        self.assertIn("fixed  a: linked skills", self.out)
        self.assertTrue(link.is_symlink())

    def test_nothing_to_fix_changes_nothing(self):
        before = {p: p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()
                  and self.log not in p.parents}
        self.assertEqual(self.go("doctor", "--fix"), 0, self.out)
        self.assertNotIn("fixed", self.out)
        self.assertEqual({p: p.read_bytes() for p in self.tmp.rglob("*") if p.is_file()
                          and self.log not in p.parents}, before)

    def test_an_account_signed_out_after_the_move_is_named_with_the_command(self):
        self.move()
        (hub.account_dir(self.new, "a") / LOGIN).unlink()       # as a lost keychain entry would
        self.assertEqual(self.go("doctor", "--fix", "--hub", str(self.new)), 1)
        self.assertIn("a is signed out after the move; `fairshare join a` signs it in again",
                      self.out)

    def test_hub_without_fix_is_a_usage_error(self):
        self.assertEqual(self.go("doctor", "--hub", "/x"), 2)
