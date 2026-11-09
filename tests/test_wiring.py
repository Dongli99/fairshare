"""Wiring: statusLine wrap/add, the rc block, apply and uninstall. Temp HOME only."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, store
from fairshare.model import Account, load_plan
from fairshare.providers import claude

FS = "fairshare"          # stands in for the real self-invocation in the pure tests
ZSHRC = """export EDITOR=vi
alias alpha="CLAUDE_CONFIG_DIR=~/.claude-alpha command claude"
alias claude="echo 'use alpha'"
"""
THEIRS = {"statusLine": {"type": "command", "command": "~/bin/line.sh 'a b'", "padding": 0},
          "theme": "dark"}


def acct(i, d):
    return Account(i, "claude", load_plan("claude-pro"), d)


class Pure(unittest.TestCase):
    home = Path("/h")
    shim = Path("/h/.local/share/fairshare/bin")

    def setUp(self):
        self.a = acct("a", self.home / ".a")      # has its own statusLine
        self.b = acct("b", self.home / ".b")      # has no settings.json
        self.files = {self.a.config_dir / "settings.json": json.dumps(THEIRS),
                      self.home / ".zshrc": ZSHRC}

    def wire(self, files, shell="/bin/zsh"):
        return claude.wire(files, [self.a, self.b], self.home, shell, FS, self.shim)

    def test_wraps_theirs_and_adds_plain_capture(self):
        out = self.wire(self.files)
        a = json.loads(out[self.a.config_dir / "settings.json"])
        self.assertEqual(a["theme"], "dark")
        self.assertEqual(a["statusLine"]["padding"], 0)
        self.assertTrue(a["statusLine"]["command"].startswith("fairshare capture a --then "))
        self.assertEqual(claude.capture_of(a["statusLine"]["command"]), ("a", "~/bin/line.sh 'a b'"))
        b = json.loads(out[self.b.config_dir / "settings.json"])
        self.assertEqual(b, {"statusLine": {"type": "command", "command": "fairshare capture b"}})

    def test_rc_block_and_alias_disabled(self):
        rc = self.wire(self.files)[self.home / ".zshrc"].splitlines()
        self.assertIn(claude.DISABLED + "alias claude=\"echo 'use alpha'\"", rc)
        self.assertIn('alias alpha="CLAUDE_CONFIG_DIR=~/.claude-alpha command claude"', rc)  # untouched
        self.assertEqual(rc[-2], 'export PATH="$HOME/.local/share/fairshare/bin:$PATH"')
        self.assertTrue(rc[-3].startswith(claude.BLOCK_BEGIN))
        self.assertEqual(rc[-1], claude.BLOCK_END)

    def test_block_goes_to_the_login_shells_rc(self):
        out = self.wire(self.files, shell="/usr/local/bin/bash")
        self.assertIn("export PATH", out[self.home / ".bashrc"])
        self.assertNotIn("export PATH", out[self.home / ".zshrc"])   # alias still disabled there

    def test_second_wire_changes_nothing(self):
        once = {**self.files, **self.wire(self.files)}
        self.assertEqual(self.wire(once), {})

    def test_existing_capture_is_left_alone(self):
        files = {self.a.config_dir / "settings.json": json.dumps(
            {"statusLine": {"type": "command", "command": "env PYTHONPATH=/x python3 -m fairshare.cli capture a"}})}
        self.assertNotIn(self.a.config_dir / "settings.json", self.wire(files))

    def test_unwire_restores_the_original(self):
        wired = {**self.files, **self.wire(self.files)}
        back = {**wired, **claude.unwire(wired, [self.a, self.b], self.home)}
        self.assertEqual(json.loads(back[self.a.config_dir / "settings.json"]), THEIRS)
        self.assertEqual(json.loads(back[self.b.config_dir / "settings.json"]), {})
        self.assertEqual(back[self.home / ".zshrc"], ZSHRC)

    def test_unexpected_layout_is_refused_not_overwritten(self):
        for bad in ('{"statusLine": "x"}', "[1]"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.wire({self.a.config_dir / "settings.json": bad})

    def test_block_missing_its_end_is_not_stripped(self):
        text = f"{claude.BLOCK_BEGIN}\nexport KEEP=1\n"
        out = claude.unwire({self.home / ".zshrc": text}, [], self.home)
        self.assertEqual(out, {})


class Cli(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.home = Path(t.name)
        self.cfg = self.home / ".config/fairshare/accounts.toml"
        self.cfg.parent.mkdir(parents=True)
        self.cfg.write_text('[[account]]\nid = "a"\nplan = "claude-pro"\nconfig_dir = "~/.a"\n\n'
                            '[[account]]\nid = "b"\nplan = "claude-pro"\nconfig_dir = "~/.b"\n')
        (self.home / ".a").mkdir()
        self.theirs = self.home / "line.sh"
        self.theirs.write_text('#!/bin/sh\ncat > "$HOME/seen.json"\necho THEIR LINE\n')
        self.theirs.chmod(0o755)
        self.a_settings = self.home / ".a/settings.json"
        self.a_settings.write_text(json.dumps(
            {"statusLine": {"type": "command", "command": str(self.theirs)}}, indent=2) + "\n")
        (self.home / ".zshrc").write_text(ZSHRC)
        self.env = {"HOME": str(self.home), "SHELL": "/bin/zsh", "FAIRSHARE_CONFIG": str(self.cfg),
                    "FAIRSHARE_STATE": str(self.home / "state")}
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)
        self.shim = self.home / ".local/share/fairshare/bin/claude"

    def cli(self, *args, answers=()):
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch("builtins.input", side_effect=list(answers)):
            code = cli.main(list(args))
        return code, out.getvalue()

    def snapshot(self):
        return {p: p.read_text() for p in self.home.rglob("*") if p.is_file()}

    def test_dry_run_changes_nothing(self):
        before = self.snapshot()
        code, out = self.cli("apply", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("+++ " + str(self.a_settings), out)
        self.assertIn("+++ " + str(self.home / ".zshrc"), out)

    def test_apply_asks_per_file_and_backs_up(self):
        # shim, then files in order: ~/.a/settings.json, ~/.b/settings.json, ~/.zshrc
        code, out = self.cli("apply", answers=["y", "y", "n", "y"])
        self.assertEqual(code, 0)
        self.assertTrue(claude.is_shim(str(self.shim)))
        self.assertFalse((self.home / ".b/settings.json").exists())         # declined
        self.assertIn("export PATH", (self.home / ".zshrc").read_text())
        self.assertEqual(len(list(self.home.glob(".a/settings.json.bak-fairshare-*"))), 1)
        self.assertEqual(len(list(self.home.glob(".zshrc.bak-fairshare-*"))), 1)

    def test_wrapped_statusline_records_and_shows_their_line(self):
        self.cli("apply", answers=["y", "y", "y", "y"])
        cmd = json.loads(self.a_settings.read_text())["statusLine"]["command"]
        payload = json.dumps({"rate_limits": {"seven_day": {"used_percentage": 40, "resets_at": 4e9}}})
        r = subprocess.run(cmd, shell=True, input=payload, capture_output=True, text=True,
                           env={**os.environ, "PATH": "/usr/bin:/bin"}, timeout=10)
        self.assertEqual(r.stdout, "THEIR LINE\n")                          # theirs, not ours
        self.assertEqual((self.home / "seen.json").read_text(), payload)     # same stdin
        self.assertEqual(store.load("a").windows["seven_day"].used_pct, 40)  # and recorded

    def test_uninstall_reverses_apply(self):
        before = {p: (self.home / p).read_text() for p in (".zshrc", ".a/settings.json")}
        self.cli("apply", answers=["y", "y", "y", "y"])
        code, out = self.cli("uninstall", answers=["y", "y", "y", "y"])
        self.assertEqual(code, 0)
        self.assertFalse(self.shim.exists())
        self.assertEqual((self.home / ".zshrc").read_text(), before[".zshrc"])
        self.assertEqual(json.loads(self.a_settings.read_text()),
                         json.loads(before[".a/settings.json"]))
        self.assertEqual(json.loads((self.home / ".b/settings.json").read_text()), {})
        self.assertTrue(self.cfg.exists())                                   # fairshare's own data kept

    def test_invalid_settings_changes_nothing(self):
        self.a_settings.write_text("{not json")
        before = self.snapshot()
        with self.assertRaises(SystemExit):
            self.cli("apply")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
