"""Stage 3b, parts C and D: the login file is no evidence, setup's labels come from the sign-in
check, `join <id>` signs an account in again, a signed-out account is offered a sign-in, and
each account's sign-in links open in its own browser. Temp HOME, config, state and hub; fake
claude and fairshare first on PATH (proved in the base classes' setUp); no browser is opened and
no real `open` runs: the launcher scripts are only written and read."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from fairshare import browser, cli, hub
from fairshare.model import ConfigError, load_accounts

from test_honest import ORG, Fleet
from test_hub import LOGIN, Base, UserFolders


class NoLoginFile(Fleet):
    def test_an_account_signed_in_without_a_login_file_is_ranked_and_launched(self):
        d = self.add("a", orgId=ORG + "a")
        self.assertFalse(any(p.name == LOGIN for p in d.rglob("*")))
        self.assertIn("> a", self.go("status")[1])
        self.assertEqual(self.go("run")[0], 0)
        self.assertEqual(self.launched_dir(), str(d))

    def test_the_adapter_has_no_login_file_any_more(self):
        from fairshare.providers import base, claude
        self.assertFalse(hasattr(claude, "LOGIN_FILE"))
        self.assertFalse(hasattr(claude, "login_present"))
        self.assertFalse(hasattr(base.Provider, "login_present"))


class SignInAgain(Base):
    def setUp(self):
        super().setUp()
        self.config()
        self.person("a", "ann")
        self.person("b", "ben")
        self.assertEqual(self.go("join", "a"), 0, self.err)
        self.assertEqual(self.go("join", "b"), 0, self.err)
        self.folder = hub.account_dir(self.hub, "a")

    def sign_out(self, id="a"):
        (hub.account_dir(self.hub, id) / LOGIN).unlink()

    def test_join_of_a_joined_id_only_signs_it_in_again(self):
        self.sign_out()
        config, shared = self.cfg.read_text(), sorted(hub.shared_dir(self.hub).rglob("*"))
        self.assertEqual(self.go("join", "a"), 0, self.err)
        self.assertIn("Signing in a again", self.out)
        self.assertIn("Nothing else changes", self.out)
        self.assertEqual(self.calls("a")[-2:], ["auth login", "auth status --json"])
        self.assertEqual(self.cfg.read_text(), config)
        self.assertEqual(sorted(hub.shared_dir(self.hub).rglob("*")), shared)
        self.assertTrue((self.folder / LOGIN).exists())

    def test_join_again_that_fails_changes_nothing(self):
        self.sign_out()
        (self.ids / "a").unlink()                      # the fake's sign-in now fails
        config = self.cfg.read_text()
        self.assertEqual(self.go("join", "a"), 1)
        self.assertIn("the sign-in did not finish", self.err)
        self.assertEqual(self.cfg.read_text(), config)

    def test_join_again_as_another_accounts_login_says_one_quota(self):
        self.sign_out()
        self.person("a", "ben")
        self.assertEqual(self.go("join", "a"), 0)
        self.assertIn("same login as b: one quota", self.err)

    def test_run_a_on_a_signed_out_account_offers_the_sign_in_then_starts(self):
        self.sign_out()
        code = self.go("run", "-a", "a", tty=True, answers=["y"])
        self.assertEqual(code, 0, self.err)
        self.assertIn("a is signed out. Sign in now? [y/N]", self.err)
        self.assertEqual(self.calls("a")[-4:], ["auth status --json", "auth login",
                                                "auth status --json", ""])   # then the session
        self.assertEqual(self.env_of("a")["CLAUDE_CONFIG_DIR"], str(self.folder))   # started

    def test_run_a_refuses_with_no_or_no_terminal_naming_the_command(self):
        self.sign_out()
        for kw in ({"tty": True, "answers": ["n"]}, {"tty": True, "answers": [""]}, {}):
            with self.subTest(**{k: str(v) for k, v in kw.items()}):
                self.assertEqual(self.go("run", "-a", "a", **kw), 1)
                self.assertIn("a is not signed in; `fairshare join a` signs it in again", self.err)
                self.assertNotIn("auth login", self.calls("a")[2:])
                self.assertFalse((self.log / "env-a").exists())
        self.asked.assert_not_called()                      # no terminal: no question

    def test_a_skipped_signed_out_account_gets_one_hint_line(self):
        self.sign_out("b")
        self.assertEqual(self.go("run"), 0, self.err)
        self.assertIn("skipped b (not signed in)", self.err)
        hints = [l for l in self.err.splitlines() if "signs it in again" in l]
        self.assertEqual(hints, ["fairshare: `fairshare join b` signs it in again"])
        self.asked.assert_not_called()


class SetupLabels(UserFolders):
    def test_labels_come_from_the_sign_in_check_and_nothing_of_theirs_changes(self):
        self.assertEqual(self.setup_all(), 0, self.err)
        rows = {l.split()[1]: l for l in self.out.splitlines() if l.startswith("  ") and "~/" in l}
        self.assertTrue(rows["claude-old"].endswith("(signed out)"))
        self.assertTrue(rows["play"].endswith("(sign-in unknown)"))   # no state file: not run
        self.assertFalse(rows["work"].endswith(")"))
        self.assertFalse(rows["claude"].endswith(")"))
        self.assertFalse((self.log / "calls-.claude-play").exists())
        # the default folder is checked without the folder variable, the others with it
        self.assertNotIn("CLAUDE_CONFIG_DIR=", (self.log / "status-env-.claude").read_text())
        self.assertIn(f"CLAUDE_CONFIG_DIR={self.home}/.claude-work",
                      (self.log / "status-env-.claude-work").read_text())
        self.assert_untouched()

    def test_a_folder_already_joined_is_marked_and_skipped(self):
        self.assertEqual(self.setup_all(), 0, self.err)
        self.assertEqual(self.go("setup", answers=["4"]), 0, self.err)
        self.assertIn("(joined)", [l for l in self.out.splitlines() if " work " in l][0])
        self.assertIn("work is already joined; skipped", self.out)


LOCAL_STATE = {"profile": {"info_cache": {
    "Default": {"name": "Personal", "gaia_name": "should not be read", "avatar_icon": "x"},
    "Profile 3": {"name": "Work stuff"}}}, "os_crypt": {"encrypted_key": "never read"}}


class Browsers(unittest.TestCase):
    def test_spec_parsing(self):
        self.assertEqual(browser.parse("chrome:Profile 3"), ("chrome:", "Profile 3"))
        self.assertEqual(browser.parse("command:/usr/local/bin/b"), ("command:", "/usr/local/bin/b"))
        for bad in ("chrome:", "command:rel/b", "firefox", "", 3):
            with self.subTest(spec=bad), self.assertRaises(ValueError):
                browser.parse(bad)

    def test_profiles_come_from_local_state_folder_and_name_only(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            self.assertEqual(browser.chrome_profiles(home), [])
            f = home / browser.CHROME_LOCAL_STATE
            f.parent.mkdir(parents=True)
            f.write_text("{broken")
            self.assertEqual(browser.chrome_profiles(home), [])
            f.write_text(json.dumps(LOCAL_STATE))
            self.assertEqual(browser.chrome_profiles(home),
                             [("Default", "Personal"), ("Profile 3", "Work stuff")])

    def test_script_content(self):
        self.assertEqual(browser.script("chrome:Profile 3"),
                         "#!/bin/sh\nexec open -na 'Google Chrome' --args "
                         "--profile-directory='Profile 3' \"$1\"\n")
        self.assertEqual(browser.script("command:/opt/my b"), "#!/bin/sh\nexec '/opt/my b' \"$1\"\n")

    def test_the_launcher_is_written_755_and_rewritten_when_it_changes(self):
        with tempfile.TemporaryDirectory() as d:
            h = Path(d)
            self.assertIsNone(browser.ensure(h, "a", None))
            p = browser.ensure(h, "a", "chrome:Default")
            self.assertEqual(p, h / "browsers" / "a")
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o755)
            browser.ensure(h, "a", "command:/bin/echo")
            self.assertEqual(p.read_text(), browser.script("command:/bin/echo"))

    def test_a_bad_browser_line_is_a_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "accounts.toml"
            cfg.write_text('[[account]]\nid = "a"\nplan = "claude-pro"\nbrowser = "firefox"\n')
            with self.assertRaises(ConfigError) as e:
                load_accounts(cfg)
            self.assertIn("chrome:<profile folder>", str(e.exception))


class BrowserPerAccount(Base):
    def setUp(self):
        super().setUp()
        self.config()
        self.person("a", "ann")
        f = self.home / browser.CHROME_LOCAL_STATE
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps(LOCAL_STATE))
        self.launcher = self.hub / "browsers" / "a"

    def login_env(self, id="a"):
        p = self.log / f"login-env-{id}"
        return dict(l.split("=", 1) for l in p.read_text().splitlines() if "=" in l)

    def test_join_asks_once_and_sets_browser_for_the_sign_in_and_sessions(self):
        self.assertEqual(self.go("join", "a", tty=True, answers=["9", "3"]), 0, self.err)
        self.assertIn("  2  Chrome profile Personal", self.out)
        self.assertIn("  3  Chrome profile Work stuff", self.out)
        self.assertIn("  4  a command", self.out)
        self.assertEqual(load_accounts(self.cfg)[0][0].browser, "chrome:Profile 3")
        self.assertIn('browser = "chrome:Profile 3"', self.cfg.read_text())
        self.assertEqual(self.login_env()["BROWSER"], str(self.launcher))
        self.assertEqual(self.launcher.read_text(), browser.script("chrome:Profile 3"))
        self.launcher.unlink()                             # recreated by the next launch
        self.assertEqual(self.go("run", "-a", "a"), 0, self.err)
        self.assertEqual(self.env_of("a")["BROWSER"], str(self.launcher))
        self.assertTrue(self.launcher.exists())

    def test_a_command_and_the_default(self):
        self.assertEqual(self.go("join", "a", tty=True,
                                 answers=["4", "rel/path", "/usr/bin/true"]), 0, self.err)
        self.assertEqual(load_accounts(self.cfg)[0][0].browser, "command:/usr/bin/true")
        self.person("b", "ben")
        self.assertEqual(self.go("join", "b", tty=True, answers=[""]), 0, self.err)
        self.assertIsNone(load_accounts(self.cfg)[0][1].browser)
        self.assertNotIn("BROWSER", self.login_env("b"))
        self.assertFalse((self.hub / "browsers" / "b").exists())

    def test_not_asked_without_a_terminal(self):
        self.assertEqual(self.go("join", "a"), 0, self.err)
        self.asked.assert_not_called()
        self.assertIsNone(load_accounts(self.cfg)[0][0].browser)

    def test_signing_in_again_uses_the_accounts_browser(self):
        self.go("join", "a", tty=True, answers=["2"])
        (self.log / "login-env-a").unlink()
        self.assertEqual(self.go("join", "a"), 0, self.err)
        self.assertEqual(self.login_env()["BROWSER"], str(self.launcher))
        self.assertEqual(self.launcher.read_text(), browser.script("chrome:Default"))


if __name__ == "__main__":
    unittest.main()
