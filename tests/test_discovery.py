"""Account discovery and `fairshare init`, against fixture home dirs only."""
import contextlib
import io
import os
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli
from fairshare.model import load_accounts, render_config
from fairshare.providers import claude

ZSHRC = """\
export PATH="$HOME/bin:$PATH"
alias alpha="CLAUDE_CONFIG_DIR=~/.claude-alpha command claude"  # a comment
alias beta='CLAUDE_CONFIG_DIR=$HOME/.claude-beta claude'
alias claude="echo 'use alpha or beta'"
# alias old="CLAUDE_CONFIG_DIR=~/.old claude"
  alias w="env CLAUDE_CONFIG_DIR=/abs/work claude"
"""
BASHRC = 'alias again="CLAUDE_CONFIG_DIR=~/.claude-alpha claude"\n'


def login(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    f = d / claude.LOGIN_FILE
    f.write_text("secret")
    f.chmod(0)          # discovery may see it exists; opening it would fail the test


class Home(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.home = Path(t.name)
        (self.home / ".zshrc").write_text(ZSHRC)
        (self.home / ".bashrc").write_text(BASHRC)
        login(self.home / ".claude-alpha")
        (self.home / ".claude-beta").mkdir()             # alias only, no login file
        login(self.home / ".claude-extra")                # login file, no alias
        (self.home / ".claude-shared").mkdir()            # neither: not an account
        (self.home / ".claude").mkdir()                   # default dir, not signed in
        (self.home / ".claude.json").write_text("{}")     # a file, not a dir
        self.cfg = self.home / ".config/fairshare/accounts.toml"
        # PATH holds no claude: setup asks it who is signed in, and a test must never run the real one.
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "FAIRSHARE_CONFIG": str(self.cfg),
                                           "PATH": str(self.home / "no-bin")})
        env.start()
        self.addCleanup(env.stop)
        self.assertIsNone(shutil.which("claude"))          # never the real one

    def init(self, *args, answers=()):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch("builtins.input", side_effect=list(answers)):
            code = cli.main(["init", *args])
        return code, out.getvalue()


class Discover(Home):
    def test_finds_aliases_and_signed_in_dirs_only(self):
        found = {c.id: (c.config_dir, c.found) for c in claude.discover(self.home)}
        self.assertEqual(found, {
            "alpha": (self.home / ".claude-alpha", "alias in ~/.zshrc"),
            "beta": (self.home / ".claude-beta", "alias in ~/.zshrc"),
            "w": (Path("/abs/work"), "alias in ~/.zshrc"),
            "claude-extra": (self.home / ".claude-extra", "login file in ~/.claude-extra"),
        })

    def test_default_dir_counts_when_signed_in(self):
        login(self.home / ".claude")
        self.assertIn("claude", {c.id for c in claude.discover(self.home)})

    def test_empty_home_finds_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(claude.discover(Path(d)), [])


class Init(Home):
    def test_yes_writes_what_was_found_as_pro(self):
        code, out = self.init("--yes")
        self.assertEqual(code, 0)
        accounts, _ = load_accounts(self.cfg)
        self.assertEqual({a.id: (a.plan.name, a.config_dir) for a in accounts}, {
            "claude-extra": ("claude-pro", self.home / ".claude-extra"),
            "alpha": ("claude-pro", self.home / ".claude-alpha"),
            "beta": ("claude-pro", self.home / ".claude-beta"),
            "w": ("claude-pro", Path("/abs/work")),
        })
        self.assertIn('config_dir = "~/.claude-alpha"', self.cfg.read_text())
        self.assertIn("+++", out)                                  # the diff was shown

    def test_rerun_keeps_existing_settings_and_backs_up(self):
        self.cfg.parent.mkdir(parents=True)
        self.cfg.write_text('[policy]\nshort_hot_pct = 80.0\n\n[[account]]\nid = "main"\n'
                            'plan = "claude-max"\nconfig_dir = "~/.claude-alpha"\ncapacity = 6.0\n\n'
                            '[[account]]\nid = "gone"\nplan = "claude-pro"\nconfig_dir = "~/.gone"\n')
        before = self.cfg.read_text()
        self.assertEqual(self.init("--yes")[0], 0)
        d = tomllib.loads(self.cfg.read_text())
        self.assertEqual(d["policy"], {"short_hot_pct": 80.0})
        by_id = {a["id"]: a for a in d["account"]}
        self.assertEqual(by_id["main"], {"id": "main", "plan": "claude-max",
                                         "config_dir": "~/.claude-alpha", "capacity": 6.0})
        self.assertIn("gone", by_id)                     # configured, not found: still kept
        self.assertNotIn("alpha", by_id)                 # same dir as "main": not added twice
        backups = list(self.cfg.parent.glob("accounts.toml.bak-fairshare-*"))
        self.assertEqual([b.read_text() for b in backups], [before])

    def test_unchanged_config_is_not_rewritten(self):
        self.init("--yes")
        code, out = self.init("--yes")
        self.assertIn("already up to date", out)
        self.assertEqual(list(self.cfg.parent.glob("*.bak-fairshare-*")), [])

    def test_account_flags_replace_discovery(self):
        self.init("--yes", "--account", "a=~/.claude-alpha:claude-ultra", "--account", "b=/x:y:claude-pro")
        d = tomllib.loads(self.cfg.read_text())
        self.assertEqual(d["account"], [
            {"id": "a", "plan": "claude-ultra", "config_dir": "~/.claude-alpha"},
            {"id": "b", "plan": "claude-pro", "config_dir": "/x:y"}])

    def test_bad_input_writes_nothing(self):
        for flags in (["--account", "nodir"], ["--account", "a=/x:claude-nope"],
                      ["--account", "a=/x:claude-pro", "--account", "a=/y:claude-pro"],
                      ["--account", "../evil=/x:claude-pro"]):
            with self.subTest(flags=flags), self.assertRaises(SystemExit):
                self.init("--yes", *flags)
        self.assertFalse(self.cfg.exists())

    def test_interactive_edit_drop_and_confirm(self):
        # order shown: alpha, beta, claude-extra, w
        answers = ["work", "claude-max",    # rename alpha, change plan
                   "", "",                    # keep beta as found
                   "-",                       # drop claude-extra
                   "-",                       # drop w
                   "y"]
        code, out = self.init(answers=answers)
        self.assertEqual(code, 0)
        self.assertIn("alias in ~/.zshrc", out)
        d = tomllib.loads(self.cfg.read_text())
        self.assertEqual(d["account"], [
            {"id": "work", "plan": "claude-max", "config_dir": "~/.claude-alpha"},
            {"id": "beta", "plan": "claude-pro", "config_dir": "~/.claude-beta"}])

    def test_interactive_decline_writes_nothing(self):
        code, out = self.init(answers=["", "", "", "", "", "", "", "", "n"])
        self.assertEqual(code, 1)
        self.assertFalse(self.cfg.exists())


class Render(unittest.TestCase):
    def test_round_trips_through_load_accounts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.toml"
            p.write_text(render_config(
                [{"id": "x", "plan": "claude-max", "config_dir": "/c/x", "capacity": 5.5}],
                {"grace_hours": 2.0}))
            accounts, tuning = load_accounts(p)
            self.assertEqual((accounts[0].plan.capacity, tuning.grace_hours), (5.5, 2.0))


if __name__ == "__main__":
    unittest.main()
