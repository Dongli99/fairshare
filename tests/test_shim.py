"""The `claude` shim, run for real under /bin/sh with a temp HOME and a fake claude."""
import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from fairshare import cli
from fairshare.providers import claude

SYS = "/usr/bin:/bin"          # head, grep; holds no claude


class Shim(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.shim_dir = self.tmp / "shim"
        # The real binary is a symlink, as the auto-updater leaves it.
        target = self.tmp / "versions" / "real-claude"
        target.parent.mkdir()
        target.write_text('#!/bin/sh\n'
                          '[ "$1" = auth ] && { echo \'{"loggedIn": true, "authMethod": "claude.ai"}\'; exit 0; }\n'
                          'echo "REAL cfg=${CLAUDE_CONFIG_DIR:-} args=$*"\n')
        target.chmod(0o755)
        self.real_dir = self.tmp / "bin"
        self.real_dir.mkdir()
        (self.real_dir / "claude").symlink_to(target)
        self.install(self.shim_dir)

    def install(self, d):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["shim", "install", "--dir", str(d)]), 0)
        return out.getvalue()

    def run_shim(self, path, **env):
        base = {"HOME": str(self.home), "PATH": path,
                "FAIRSHARE_CONFIG": str(self.tmp / "accounts.toml"),
                "FAIRSHARE_STATE": str(self.tmp / "state")}
        return subprocess.run([str(self.shim_dir / "claude"), "-p", "hi there"],
                              env={**base, **env}, capture_output=True, text=True, timeout=10)

    def test_install_prints_the_path_line(self):
        out = self.install(self.shim_dir)
        self.assertIn(f'export PATH="{self.shim_dir.resolve()}:$PATH"', out)
        self.assertTrue(os.access(self.shim_dir / "claude", os.X_OK))

    def test_config_dir_set_passes_through_untouched(self):
        r = self.run_shim(f"{self.shim_dir}:{self.real_dir}:{SYS}", CLAUDE_CONFIG_DIR="/acct/x")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "REAL cfg=/acct/x args=-p hi there")
        self.assertEqual(r.stderr, "")

    def test_never_reenters_a_shim(self):
        # The shim's dir twice (once spelled differently), and a second shim copy elsewhere.
        other = self.tmp / "other-shim"
        self.install(other)
        path = f"{self.shim_dir}:{self.shim_dir}/:{other}:{self.real_dir}:{SYS}"
        r = self.run_shim(path, CLAUDE_CONFIG_DIR="/acct/x")
        self.assertEqual(r.stdout.strip(), "REAL cfg=/acct/x args=-p hi there")

    def test_real_binary_missing_is_a_clear_error(self):
        r = self.run_shim(f"{self.shim_dir}:{SYS}", CLAUDE_CONFIG_DIR="/acct/x")
        self.assertEqual(r.returncode, 127)
        self.assertIn("no claude found on PATH", r.stderr)

    def account(self):
        """One account whose folder holds a login file (existence only; never opened)."""
        d = self.tmp / "acct-one"
        d.mkdir()
        (d / claude.LOGIN_FILE).write_text("")
        (self.tmp / "accounts.toml").write_text(
            f'[[account]]\nid = "one"\nplan = "claude-pro"\nconfig_dir = "{d}"\n')
        return d

    def test_unset_routes_through_fairshare_run(self):
        d = self.account()
        r = self.run_shim(f"{self.shim_dir}:{self.real_dir}:{SYS}")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"REAL cfg={d} args=-p hi there")
        self.assertIn("fairshare -> one", r.stderr)

    def test_routed_with_real_binary_missing_is_a_clear_error(self):
        self.account()
        r = self.run_shim(f"{self.shim_dir}:{SYS}")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no claude found on PATH", r.stderr)

    def test_python_lookup_skips_shims_like_the_shim_does(self):
        path = f"{self.shim_dir}:{self.real_dir}"
        self.assertEqual(claude.real_binary(path), str(self.real_dir / "claude"))
        with self.assertRaises(SystemExit):
            claude.real_binary(str(self.shim_dir))

    def test_install_refuses_to_overwrite_a_real_claude(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            cli.main(["shim", "install", "--dir", str(self.real_dir)])
        self.assertTrue((self.real_dir / "claude").is_symlink())


if __name__ == "__main__":
    unittest.main()
