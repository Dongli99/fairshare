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


def ran(d: Path):
    """A folder the tool has run in: it holds its own state file."""
    d.mkdir(parents=True, exist_ok=True)
    (d / claude.STATE_FILE).write_text("{}")


class Home(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.home = Path(t.name)
        (self.home / ".zshrc").write_text(ZSHRC)
        (self.home / ".bashrc").write_text(BASHRC)
        ran(self.home / ".claude-alpha")
        (self.home / ".claude-beta").mkdir()             # alias only, no state file
        ran(self.home / ".claude-extra")                  # state file, no alias
        (self.home / ".claude-shared").mkdir()            # nothing in it: not an account
        (self.home / ".claude").mkdir()                   # default dir; HOME has no state file
        # PATH holds no claude: a test must never run the real one.
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "PATH": str(self.home / "no-bin")})
        env.start()
        self.addCleanup(env.stop)
        self.assertIsNone(shutil.which("claude"))          # never the real one


class Discover(Home):
    def test_finds_aliases_and_folders_with_their_own_state_file_only(self):
        found = {c.id: (c.config_dir, c.found) for c in claude.discover(self.home)}
        self.assertEqual(found, {
            "alpha": (self.home / ".claude-alpha", "alias in ~/.zshrc"),
            "beta": (self.home / ".claude-beta", "alias in ~/.zshrc"),
            "w": (Path("/abs/work"), "alias in ~/.zshrc"),
            "claude-extra": (self.home / ".claude-extra", "folder ~/.claude-extra"),
        })

    def test_the_default_dir_counts_with_homes_state_file_beside_it(self):
        self.assertNotIn("claude", {c.id for c in claude.discover(self.home)})
        (self.home / claude.STATE_FILE).write_text("{}")
        self.assertIn("claude", {c.id for c in claude.discover(self.home)})

    def test_shared_items_alone_do_not_make_an_account(self):
        shared = self.home / ".claude-sessions"           # e.g. a folder of shared sessions
        (shared / "sessions").mkdir(parents=True)
        (shared / "hooks").mkdir()
        (shared / "history.jsonl").write_text("{}\n")
        (self.home / ".claude" / "projects").mkdir()
        found = {c.id for c in claude.discover(self.home)}
        self.assertNotIn("claude-sessions", found)
        self.assertNotIn("claude", found)
        self.assertNotIn("claude-shared", found)

    def test_empty_home_finds_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(claude.discover(Path(d)), [])


if __name__ == "__main__":
    unittest.main()
