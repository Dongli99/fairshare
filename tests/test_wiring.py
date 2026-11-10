"""Wiring: the statusLine wrap/unwrap, apply and uninstall. Temp HOME and a temp PATH holding a
fake `fairshare` (and a fake `claude`) only: the real ones are never found."""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, store
from fairshare.model import Account, Capture, load_plan
from fairshare.providers import claude

FS = "fairshare"          # stands in for the installed command in the pure tests
THEIRS = {"statusLine": {"type": "command", "command": "~/bin/line.sh 'a b'", "padding": 0},
          "theme": "dark"}


def acct(i, d):
    return Account(i, "claude", load_plan("claude-pro"), d)


def older_command(account_id: str) -> str:
    """The command form earlier versions wrote: the Python that ran `apply`, by source path."""
    code = ("import sys; sys.path.insert(0, '/old/src'); "
            "from fairshare.cli import main; sys.exit(main())")
    return f"{shlex.quote('/usr/bin/python3')} -I -c {shlex.quote(code)} capture {account_id}"


class Pure(unittest.TestCase):
    home = Path("/h")

    def setUp(self):
        self.a = acct("a", self.home / ".a")      # has its own statusLine
        self.b = acct("b", self.home / ".b")      # has no settings.json
        self.files = {self.a.config_dir / "settings.json": json.dumps(THEIRS)}

    def wire(self, files, command=FS):
        return claude.wire(files, [self.a, self.b], command)

    def test_wraps_theirs_and_adds_plain_capture(self):
        out = self.wire(self.files)
        a = json.loads(out[self.a.config_dir / "settings.json"])
        self.assertEqual(a["theme"], "dark")
        self.assertEqual(a["statusLine"]["padding"], 0)
        self.assertTrue(a["statusLine"]["command"].startswith("fairshare capture a --then "))
        self.assertEqual(claude.capture_of(a["statusLine"]["command"]),
                         Capture("a", ("fairshare",), "~/bin/line.sh 'a b'"))
        b = json.loads(out[self.b.config_dir / "settings.json"])
        self.assertEqual(b, {"statusLine": {"type": "command", "command": "fairshare capture b"}})

    def test_only_the_settings_files_are_touched(self):
        self.assertEqual(set(self.wire(self.files)),
                         {self.a.config_dir / "settings.json", self.b.config_dir / "settings.json"})

    def test_second_wire_changes_nothing(self):
        once = {**self.files, **self.wire(self.files)}
        self.assertEqual(self.wire(once), {})

    def test_existing_capture_is_left_alone(self):
        files = {self.a.config_dir / "settings.json": json.dumps(
            {"statusLine": {"type": "command", "command": "env PYTHONPATH=/x python3 -m fairshare.cli capture a"}})}
        self.assertNotIn(self.a.config_dir / "settings.json", self.wire(files))

    def test_unwire_restores_the_original(self):
        wired = {**self.files, **self.wire(self.files)}
        back = {**wired, **claude.unwire(wired, [self.a, self.b])}
        self.assertEqual(json.loads(back[self.a.config_dir / "settings.json"]), THEIRS)
        self.assertEqual(json.loads(back[self.b.config_dir / "settings.json"]), {})

    def test_unwire_takes_out_the_older_command_form_too(self):
        cmd = older_command("a")
        self.assertEqual(claude.capture_of(cmd).account, "a")
        files = {self.a.config_dir / "settings.json": json.dumps(
            {"statusLine": {"type": "command", "command": cmd}, "theme": "dark"}),
            self.b.config_dir / "settings.json": json.dumps(
                {"statusLine": {"type": "command", "command": f"{cmd[:-1]}b --then 'mine --x'"}})}
        out = claude.unwire(files, [self.a, self.b])
        self.assertEqual(json.loads(out[self.a.config_dir / "settings.json"]), {"theme": "dark"})
        self.assertEqual(json.loads(out[self.b.config_dir / "settings.json"]),
                         {"statusLine": {"type": "command", "command": "mine --x"}})

    def test_unexpected_layout_is_refused_not_overwritten(self):
        for bad in ('{"statusLine": "x"}', "[1]"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.wire({self.a.config_dir / "settings.json": bad})

    def test_wired_finds_the_hook_for_that_account_only(self):
        text = self.wire(self.files)[self.b.config_dir / "settings.json"]
        self.assertEqual(claude.wired(text, "b"), Capture("b", ("fairshare",), None))
        for other in (claude.wired(text, "a"), claude.wired(None, "b"), claude.wired("{", "b"),
                      claude.wired(json.dumps(THEIRS), "a")):
            self.assertIsNone(other)


class Cli(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        for tool in ("fairshare", "claude"):
            (self.bin / tool).write_text("#!/bin/sh\nexit 0\n")
            (self.bin / tool).chmod(0o755)
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
        self.env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
                    "FAIRSHARE_CONFIG": str(self.cfg), "FAIRSHARE_STATE": str(self.home / "state")}
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)
        for tool in ("fairshare", "claude"):                       # never the real ones
            self.assertEqual(shutil.which(tool), str(self.bin / tool))

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

    def test_apply_writes_the_installed_command_by_absolute_path(self):
        self.cli("apply", answers=["y", "y"])
        for acct_id, d in (("a", ".a"), ("b", ".b")):
            cmd = json.loads((self.home / d / "settings.json").read_text())["statusLine"]["command"]
            self.assertTrue(cmd.startswith(f"{self.bin}/fairshare capture {acct_id}"), cmd)
            self.assertNotIn("python", cmd)
            self.assertNotIn("PATH", cmd)
        self.assertEqual(claude.capture_of(cmd), Capture("b", (f"{self.bin}/fairshare",), None))

    def test_a_relative_path_entry_is_made_absolute(self):
        os.environ["PATH"] = "bin:/usr/bin:/bin"
        with mock.patch.object(shutil, "which", return_value="bin/fairshare"):
            self.assertEqual(cli._installed(), os.path.abspath("bin/fairshare"))

    def test_apply_refuses_without_an_installed_fairshare(self):
        (self.bin / "fairshare").unlink()
        before = self.snapshot()
        with self.assertRaises(SystemExit) as e:
            self.cli("apply")
        self.assertIn("install it first", str(e.exception))
        self.assertEqual(self.snapshot(), before)

    def test_apply_asks_per_file_and_backs_up(self):
        # files in order: ~/.a/settings.json, ~/.b/settings.json
        code, out = self.cli("apply", answers=["y", "n"])
        self.assertEqual(code, 0)
        self.assertFalse((self.home / ".b/settings.json").exists())         # declined
        self.assertEqual(len(list(self.home.glob(".a/settings.json.bak-fairshare-*"))), 1)
        self.assertFalse((self.home / ".zshrc").exists())                    # no rc file written

    def test_wrapped_statusline_records_and_shows_their_line(self):
        self.cli("apply", answers=["y", "y"])
        cmd = json.loads(self.a_settings.read_text())["statusLine"]["command"]
        payload = json.dumps({"rate_limits": {"seven_day": {"used_percentage": 40, "resets_at": 4e9}}})
        (self.bin / "fairshare").unlink()      # the command runs by its path, not through PATH
        real = self.tmp / "real"
        real.write_text('#!/bin/sh\nexec "$FAIRSHARE_PY" -I -c "import sys; sys.path.insert(0, \'$FAIRSHARE_SRC\'); '
                        'from fairshare.cli import main; sys.exit(main())" "$@"\n')
        real.chmod(0o755)
        (self.bin / "fairshare").symlink_to(real)
        src = str(Path(cli.__file__).resolve().parent.parent)
        r = subprocess.run(cmd, shell=True, input=payload, capture_output=True, text=True,
                           env={**os.environ, "PATH": "/usr/bin:/bin", "FAIRSHARE_PY": sys.executable,
                                "FAIRSHARE_SRC": src}, timeout=20)
        self.assertEqual(r.stdout, "THEIR LINE\n")                          # theirs, not ours
        self.assertEqual((self.home / "seen.json").read_text(), payload)     # same stdin
        self.assertEqual(store.load("a").windows["seven_day"].used_pct, 40)  # and recorded

    def test_uninstall_reverses_apply(self):
        before = (self.home / ".a/settings.json").read_text()
        self.cli("apply", answers=["y", "y"])
        code, out = self.cli("uninstall", answers=["y", "y"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(self.a_settings.read_text()), json.loads(before))
        self.assertEqual(json.loads((self.home / ".b/settings.json").read_text()), {})
        self.assertTrue(self.cfg.exists())                                   # fairshare's own data kept

    def test_uninstall_needs_no_installed_fairshare(self):
        self.cli("apply", answers=["y", "y"])
        (self.bin / "fairshare").unlink()
        self.assertEqual(self.cli("uninstall", answers=["y", "y"])[0], 0)
        self.assertNotIn("capture", self.a_settings.read_text())

    def test_uninstall_unwraps_both_command_forms(self):
        new = f"{self.bin}/fairshare capture a --then {shlex.quote(str(self.theirs))}"
        for command in (new, older_command("a") + f" --then {shlex.quote(str(self.theirs))}"):
            with self.subTest(command=command):
                self.a_settings.write_text(json.dumps(
                    {"statusLine": {"type": "command", "command": command}}))
                self.assertEqual(self.cli("uninstall", answers=["y"])[0], 0)
                self.assertEqual(json.loads(self.a_settings.read_text()),
                                 {"statusLine": {"type": "command", "command": str(self.theirs)}})

    def test_invalid_settings_changes_nothing(self):
        self.a_settings.write_text("{not json")
        before = self.snapshot()
        with self.assertRaises(SystemExit):
            self.cli("apply")
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
