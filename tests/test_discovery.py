"""Discovery of the user's own folders, against fixture home dirs only."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        (self.home / ".claude-shared").mkdir()            # nothing in it: not an account
        (self.home / ".claude").mkdir()                   # default dir, not signed in
        (self.home / ".claude.json").write_text("{}")     # a file, not a dir
        # PATH holds no claude: a test must never run the real one.
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.home / "no-bin")})
        env.start()
        self.addCleanup(env.stop)
        self.assertIsNone(shutil.which("claude"))          # never the real one


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

    def test_a_signed_out_folder_with_shared_items_is_found_too(self):
        (self.home / ".claude-old").mkdir()
        (self.home / ".claude-old" / "history.jsonl").write_text("{}\n")
        (self.home / ".claude" / "projects").mkdir()
        found = {c.id: c.found for c in claude.discover(self.home)}
        self.assertEqual((found["claude-old"], found["claude"]), ("folder ~/.claude-old", "folder ~/.claude"))
        self.assertNotIn("claude-shared", found)                  # still nothing in it

    def test_empty_home_finds_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(claude.discover(Path(d)), [])


if __name__ == "__main__":
    unittest.main()
