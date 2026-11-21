"""Decision 31: fairshare's block of aliases in the user's shell files, and `run -a ID
<subcommand>` handed straight to the provider. Temp HOME, config, state and hub, and the fake
`claude` of test_hub; the real ones are never run."""
import os
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, shell
from fairshare.providers import claude
from test_hub import FAKE_CLAUDE, ZSHRC, UserFolders, write

BLOCK_WORK = ("\n# >>> fairshare aliases >>>\nalias work='fairshare run -a work'\n"
              "# <<< fairshare aliases <<<\n")
SRC = str(Path(__file__).resolve().parent.parent / "src")


class Block(unittest.TestCase):
    LINES = ["alias work='fairshare run -a work'", "alias play='fairshare run -a play'"]

    def test_add_then_remove_gives_back_every_byte(self):
        for text in ("", "x", "x\n", "a\r\nb\n", ZSHRC, ZSHRC + "# no newline at the end",
                     "\n\n"):
            with self.subTest(text=text):
                new = shell.with_block(text, self.LINES)
                self.assertTrue(new.startswith(text))
                self.assertEqual(shell.block_lines(new), self.LINES)
                self.assertEqual(shell.without_block(new), text)

    def test_a_block_already_there_changes_only_between_its_markers(self):
        before = shell.with_block(ZSHRC, self.LINES[:1])
        text = before + "alias later='ls'\n"
        new = shell.with_block(text, self.LINES)
        cut = before.index(shell.START) + len(shell.START) + 1
        self.assertEqual(new[:cut], text[:cut])
        self.assertTrue(new.endswith(f"{shell.END}\nalias later='ls'\n"))
        self.assertEqual(shell.block_lines(new), self.LINES)
        self.assertEqual(shell.without_block(new), ZSHRC + "alias later='ls'\n")
        self.assertEqual(shell.with_block(new, []), shell.without_block(new))

    def test_broken_markers_are_refused(self):
        for text in (f"{shell.START}\nalias a='b'\n", f"alias a='b'\n{shell.END}\n",
                     shell.with_block("", ["x"]) * 2):
            with self.subTest(text=text), self.assertRaises(ValueError):
                shell.with_block(text, ["alias a='b'"])

    def test_a_block_line_reads_back(self):
        line = shell.alias_line("cw", shell.run_words("work"))
        self.assertEqual(line, "alias cw='fairshare run -a work'")
        self.assertEqual(shell.entry(line), ("cw", "work"))
        self.assertEqual(shell.entry("alias x='ls -l'"), ("x", None))
        self.assertEqual(shell.entry("# a comment"), (None, None))


class Scan(unittest.TestCase):
    def test_every_alias_line_is_read_with_what_it_runs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            home = Path(t)
            write(home / ".zshrc", "\n".join([
                ZSHRC.rstrip("\n"),
                "alias again='CLAUDE_CONFIG_DIR=~/.claude-work claude'   # same folder",
                "alias opus=\"CLAUDE_CONFIG_DIR=$HOME/.claude-work claude --model opus\"",
                "alias envd='env CLAUDE_CONFIG_DIR=~/.e command claude'",
                "alias path='CLAUDE_CONFIG_DIR=~/.p /opt/bin/claude'",
                "alias more='CLAUDE_CONFIG_DIR=~/.m FOO=1 claude'",
                "alias npx='CLAUDE_CONFIG_DIR=~/.n npx something'",
                "alias claude='claude --verbose'", ""]))
            found = [(a.name, a.config_dir.relative_to(home).as_posix(), a.line, a.tool,
                      a.prefix, a.args) for a in claude.aliases(home)]
        self.assertEqual(found, [
            ("work", ".claude-work", 1, True, (), ()),
            ("play", ".claude-play", 2, True, (), ()),
            ("again", ".claude-work", 3, True, (), ()),
            ("opus", ".claude-work", 4, True, (), ("--model", "opus")),
            ("envd", ".e", 5, True, (), ()),
            ("path", ".p", 6, True, (), ()),
            ("more", ".m", 7, True, ("FOO=1",), ()),
            ("npx", ".n", 8, False, (), ())])


class Aliases(UserFolders):
    def setUp(self):
        super().setUp()
        self.config()
        self.zshrc = self.home / ".zshrc"

    def join_work(self, *extra):
        self.assertEqual(self.go("join", "--from", "~/.claude-work", *extra, answers=["n"]), 0,
                         self.err)

    def backups(self) -> list[Path]:
        return sorted(self.home.glob(".zshrc.bak-fairshare-*"))

    def test_yes_writes_the_block_after_a_backup_and_remove_gives_back_every_byte(self):
        self.join_work()
        self.assertIn("~/.zshrc needs a yes on a terminal: not changed", self.out)  # join's offer
        self.assertEqual(self.zshrc.read_text(), ZSHRC)
        self.assertEqual(self.go("aliases", tty=True, answers=[""]), 0, self.err)
        self.assertEqual(self.zshrc.read_text(), ZSHRC + BLOCK_WORK)        # play: never joined
        self.assertIn("+alias work='fairshare run -a work'", self.out)
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.out)                 # no line of theirs shown
        self.assertIn("It overrides your own alias work (line 1) above it; your lines stay as "
                      "they are.\n", self.out)
        (backup,) = self.backups()
        self.assertEqual(backup.read_text(), ZSHRC)
        self.assertEqual(self.go("aliases", tty=True), 0, self.err)                # idempotent
        self.assertIn("nothing to change in your shell files", self.out)
        self.asked.assert_not_called()
        self.assertEqual(self.go("aliases", "--remove"), 0, self.err)              # no terminal
        self.assertIn("~/.zshrc needs a yes on a terminal: not changed. To apply it, run "
                      "`fairshare aliases --remove` on a terminal, or remove the lines from "
                      "`# >>> fairshare aliases >>>` to `# <<< fairshare aliases <<<`.", self.out)
        self.assertEqual(self.zshrc.read_text(), ZSHRC + BLOCK_WORK)
        self.assertEqual(self.go("aliases", "--remove", tty=True, answers=[""]), 0, self.err)
        self.assertEqual(self.zshrc.read_bytes(), ZSHRC.encode())
        self.assertEqual(len(self.backups()), 2)
        self.assertEqual(self.go("aliases", "--remove", tty=True), 0, self.err)
        self.assertIn("nothing to remove", self.out)

    def test_declined_leaves_the_file_and_makes_no_backup(self):
        self.join_work()
        self.assertEqual(self.go("aliases", tty=True, answers=["n"]), 0, self.err)
        self.assertIn("~/.zshrc not changed", self.out)
        self.assertEqual(self.zshrc.read_text(), ZSHRC)
        self.assertEqual(self.backups(), [])

    def test_no_terminal_prints_the_block_and_how(self):
        self.join_work()
        self.assertEqual(self.go("aliases"), 0, self.err)
        self.assertIn("~/.zshrc needs a yes on a terminal: not changed. To apply it, run "
                      "`fairshare aliases` on a terminal, or paste this at its end, replacing "
                      "any block of fairshare's there:\n" + BLOCK_WORK.lstrip("\n"), self.out + "\n")
        self.asked.assert_not_called()
        self.assertEqual(self.zshrc.read_text(), ZSHRC)

    def test_a_linked_or_unwritable_shell_file_is_printed_not_edited(self):
        self.join_work()
        target = write(self.home / "dotfiles/zshrc", ZSHRC)
        self.zshrc.unlink()
        self.zshrc.symlink_to(target)
        self.assertEqual(self.go("aliases", tty=True), 0, self.err)
        self.assertIn("~/.zshrc is a link (a dotfile manager's?): not changed. To apply it, "
                      "paste this", self.out)
        self.assertIn("alias work='fairshare run -a work'", self.out)
        self.asked.assert_not_called()
        self.assertEqual(target.read_text(), ZSHRC)
        self.assertTrue(self.zshrc.is_symlink())
        if os.geteuid() == 0:
            return                                      # root writes a read-only file anyway
        self.zshrc.unlink()
        write(self.zshrc, ZSHRC).chmod(0o444)
        self.addCleanup(self.zshrc.chmod, 0o644)
        self.assertEqual(self.go("aliases", tty=True), 0, self.err)
        self.assertIn("~/.zshrc cannot be written: not changed", self.out)
        self.asked.assert_not_called()
        self.assertEqual(self.zshrc.read_text(), ZSHRC)

    def test_a_renamed_account_is_found_by_its_folder(self):
        self.person("job", "alice")
        self.join_work("job")
        (entry,) = tomllib.loads(self.cfg.read_text())["account"]
        self.assertEqual(entry["from"], str((self.home / ".claude-work").resolve()))
        self.assertEqual(self.go("aliases", tty=True, answers=[""]), 0, self.err)
        self.assertEqual(self.zshrc.read_text(),
                         ZSHRC + BLOCK_WORK.replace("run -a work", "run -a job"))

    def test_an_account_joined_before_from_was_recorded_is_found_by_name(self):
        self.join_work()
        self.cfg.write_text("".join(l for l in self.cfg.read_text().splitlines(True)
                                    if not l.startswith("from = ")))
        self.assertEqual(self.go("aliases", tty=True, answers=[""]), 0, self.err)
        self.assertEqual(self.zshrc.read_text(), ZSHRC + BLOCK_WORK)
        # with a recorded folder, the name alone no longer matches
        self.zshrc.write_text(ZSHRC)
        self.cfg.write_text(self.cfg.read_text().replace(
            'plan = ', f'from = "{self.home / ".claude-elsewhere"}"\nplan = '))
        self.assertEqual(self.go("aliases", tty=True), 0, self.err)
        self.assertIn("nothing to change", self.out)

    def test_aliases_to_a_folder_never_joined_or_in_the_hub_are_left_alone(self):
        self.join_work()
        write(self.zshrc, ZSHRC + f"alias hubbed='CLAUDE_CONFIG_DIR={self.hub}/accounts/work "
                                  "claude'\nalias claude='claude --verbose'\n")
        self.assertEqual(self.go("aliases", tty=True, answers=["n"]), 0, self.err)
        self.assertIn("+alias work='fairshare run -a work'\n", self.out)
        self.assertNotIn("alias play=", self.out)
        self.assertNotIn("+alias hubbed", self.out)
        self.assertNotIn("+alias claude", self.out)

    def test_an_alias_with_arguments_is_left_alone_and_the_line_to_write_is_said(self):
        self.join_work()
        write(self.zshrc, ZSHRC + "alias wm='CLAUDE_CONFIG_DIR=~/.claude-work claude --model "
                                  "opus'\n")
        self.assertEqual(self.go("aliases", tty=True, answers=["n"]), 0, self.err)
        self.assertIn("alias wm (~/.zshrc, line 3) passes more than the folder: left alone; to "
                      "start work through fairshare, write it yourself: alias wm='fairshare run "
                      "-a work --model opus'\n", self.out)
        self.assertNotIn("+alias wm", self.out)

    def test_join_offers_the_new_accounts_line_into_the_block(self):
        self.join_work()
        self.go("aliases", tty=True, answers=[""])
        with mock.patch.object(cli, "_ask_browser", return_value=None):
            self.assertEqual(self.go("join", "--from", "~/.claude-play", tty=True,
                                     answers=["", ""]), 0, self.err)      # its name, then yes
        self.assertEqual(self.zshrc.read_text(), ZSHRC + BLOCK_WORK.replace(
            "# <<<", "alias play='fairshare run -a play'\n# <<<"))
        # a fresh account with no alias of its own gets one named after it, when that is free
        self.person("extra", "erin")
        self.assertEqual(self.go("join", "extra"), 0, self.err)
        self.assertIn("alias extra='fairshare run -a extra'\n", self.out)
        self.person("claude", "dave")                 # a name taken (the provider's command)
        self.assertEqual(self.go("join", "claude"), 0, self.err)
        self.assertIn("no alias added for claude, the name is taken", self.out)

    def test_join_without_a_block_or_an_alias_offers_nothing(self):
        self.person("extra", "erin")
        self.assertEqual(self.go("join", "extra"), 0, self.err)
        self.assertNotIn("alias", self.out)

    def test_doctor_warns_until_the_block_overrides_the_alias(self):
        self.join_work()
        self.go("doctor")
        self.assertIn("warn  claude: alias work in ~/.zshrc still starts Claude Code on your own "
                      "folder, not on work's in the hub (run `fairshare aliases`)\n", self.out)
        self.assertEqual(self.zshrc.read_text(), ZSHRC)
        self.go("aliases", tty=True, answers=[""])
        self.go("doctor")
        self.assertNotIn("warn", self.out)
        self.assertIn("pass  claude: your aliases work start the hub's accounts\n", self.out)

    def test_uninstall_names_the_block_and_its_undo(self):
        self.join_work()
        self.go("aliases", tty=True, answers=[""])
        self.assertEqual(self.go("uninstall"), 0)
        self.assertIn("Shell file ~/.zshrc: holds fairshare's block of aliases; `fairshare aliases "
                      "--remove` takes it out (with a backup beside it)\n", self.out)
        self.assertEqual(self.zshrc.read_text(), ZSHRC + BLOCK_WORK)


class PassThrough(UserFolders):
    """`run -a ID <subcommand>`: the provider's own command on the account's hub folder, with
    nothing of the launcher around it."""

    def setUp(self):
        super().setUp()
        self.config()
        self.assertEqual(self.go("join", "--from", "~/.claude-work", answers=["n"]), 0, self.err)
        write(self.bin / "claude-fake", FAKE_CLAUDE).chmod(0o755)
        write(self.bin / "claude", "#!/bin/sh\n"
              'case "$1" in agents|daemon)\n'
              '  echo "$*|$CLAUDE_CONFIG_DIR|${FAIRSHARE_LAUNCHER:-none}" > "$FAKE_LOG/pass"\n'
              '  printf \'[{"sessionId": "s1", "status": "idle"}]\\n\'; exit 7 ;;\n'
              "esac\n"
              f'exec "{self.bin}/claude-fake" "$@"\n').chmod(0o755)
        for f in self.log.iterdir():
            f.unlink()

    def fairshare(self, *argv) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-c", "import sys; from fairshare import cli; "
                               "sys.exit(cli.main())", *argv], capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": SRC}, stdin=subprocess.DEVNULL)

    def test_stdout_and_exit_status_are_the_providers_and_no_launcher_runs(self):
        for argv in (["agents", "--json"], ["daemon", "status"]):
            with self.subTest(argv=argv):
                r = self.fairshare("run", "-a", "work", *argv)
                self.assertEqual((r.returncode, r.stdout, r.stderr),
                                 (7, '[{"sessionId": "s1", "status": "idle"}]\n', ""))
                self.assertEqual((self.log / "pass").read_text(),
                                 f"{' '.join(argv)}|{self.hub / 'accounts/work'}|none\n")
        self.assertEqual(self.calls("work"), [])                    # no sign-in check, no probe
        self.assertFalse((self.tmp / "state" / "sessions").exists()
                         and any((self.tmp / "state" / "sessions").iterdir()))

    def test_without_an_account_it_refuses_naming_a(self):
        for argv in (["run", "agents", "--json"], ["agents", "--json"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.go(*argv), 2)
                self.assertIn("fairshare: `claude agents` is about one account's folder; name "
                              "the account: fairshare run -a ID agents --json\n", self.err)
                self.assertFalse((self.log / "pass").exists())

    def test_an_unknown_account_is_refused(self):
        self.assertEqual(self.go("run", "-a", "nope", "agents"), 2)
        self.assertIn("no account 'nope' (configured: work)", self.err)
