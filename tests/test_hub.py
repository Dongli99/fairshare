"""Stage 3a: one assistant. The hub, the start check, join/leave, setup, capture by folder, and
uninstall. Temp HOME, config, state and hub; a temp PATH whose fake `claude` implements
`auth login/logout/status` (setUp proves the fakes come first); the real ones are never run. The
user's own folders must be byte-identical after everything."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from fairshare import cli, hub, session, store
from fairshare.model import (Capture, Snapshot, Window, load_accounts, with_account, with_hub,
                             without_account)
from fairshare.providers import claude

# Answers $FAKE_IDS/<folder name> as that folder's person: the same person is the same login.
FAKE_CLAUDE = r"""#!/bin/sh
[ "$1" = "--version" ] && { echo "9.9.9 (Fake Code)"; exit 0; }
dir=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
name=$(basename "$dir")
for last; do :; done
if [ "$last" = "/usage" ]; then     # the usage probe: $FAKE_USAGE's answer, else it fails
  [ -n "$FAKE_USAGE" ] && exec cat "$FAKE_USAGE"
  exit 1
fi
echo "$*" >> "$FAKE_LOG/calls-$name"
case "$1 $2" in
  "auth login")
    env > "$FAKE_LOG/login-env-$name"
    [ -f "$FAKE_IDS/$name" ] || exit 1
    : > "$dir/.credentials.json"
    [ -f "$dir/.claude.json" ] || printf '{"userID": "u-%s", "numStartups": 0}\n' "$name" > "$dir/.claude.json"
    exit 0 ;;
  "auth logout") rm -f "$dir/.credentials.json"; exit 0 ;;
  "auth status")
    env > "$FAKE_LOG/status-env-$name"
    if [ -f "$dir/.credentials.json" ]; then
      who=$(cat "$FAKE_IDS/$name")
      printf '{"loggedIn": true, "authMethod": "%s", "email": "%s@example.invalid", "orgId": "org-%s", "orgName": "Example Org", "planTier": "pro"}\n' "${FAKE_METHOD:-claude.ai}" "$who" "$who"
    else
      printf '{"loggedIn": false, "authMethod": "none"}\n'
    fi
    exit 0 ;;
esac
env > "$FAKE_LOG/env-$name"
exit 0
"""
ZSHRC = ('alias work="CLAUDE_CONFIG_DIR=~/.claude-work claude"\n'
         "alias play='CLAUDE_CONFIG_DIR=$HOME/.claude-play claude'\n")
LOGIN = ".credentials.json"          # the fake's own sign-in marker; fairshare never looks at it
SECRETS = ("s3cr3t-value", "mcp-secret-value", "example.invalid", "org-", "Example Org",
           "user-work")


def own_hooks(runner: str) -> dict:
    """fairshare's three hooks as `wire` writes them, running `runner`."""
    return {"StopFailure": [{"matcher": "rate_limit", "hooks": [
                {"type": "command", "command": f"{runner} hook stop-failure"}]}],
            "PostToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": f"{runner} hook post-tool"}]}],
            "UserPromptSubmit": [{"hooks": [
                {"type": "command", "command": f"{runner} hook prompt"}]}]}


OWN_HOOKS_TEXT = ('{"StopFailure": [{"matcher": "rate_limit", "hooks": [{"type": "command", '
                  '"command": "{r} hook stop-failure"}]}], "PostToolUse": [{"matcher": "*", '
                  '"hooks": [{"type": "command", "command": "{r} hook post-tool"}]}], '
                  '"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "{r} hook '
                  'prompt"}]}]}')


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def lines(p: Path) -> list[str]:
    return p.read_text().splitlines()


class Base(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.tmp = Path(t.name).resolve()
        self.home, self.bin, self.ids, self.log = (self.tmp / n for n in ("home", "bin", "ids", "log"))
        for d in (self.home, self.bin, self.ids, self.log):
            d.mkdir()
        write(self.bin / "claude", FAKE_CLAUDE).chmod(0o755)
        write(self.bin / "fairshare", "#!/bin/sh\nexit 0\n").chmod(0o755)
        self.cfg = self.tmp / "config" / "accounts.toml"
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAIRSHARE_CONFIG": str(self.cfg), "FAIRSHARE_STATE": str(self.tmp / "state"),
            "FAKE_IDS": str(self.ids), "FAKE_LOG": str(self.log)})
        env.start()
        self.addCleanup(env.stop)
        for k in [k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC")) or k == "BROWSER"]:
            del os.environ[k]                # restored by env.stop
        for k in [k for k in os.environ if k.startswith("FAIRSHARE_")
                  and k not in ("FAIRSHARE_CONFIG", "FAIRSHARE_STATE")]:
            del os.environ[k]              # a surrounding launcher's own
        for tool in ("claude", "fairshare"):                                 # never the real ones
            self.assertEqual(shutil.which(tool), str(self.bin / tool))
        p = mock.patch.object(cli, "_fairshare_argv", lambda: [str(self.bin / "fairshare")])
        p.start()                        # a background probe starts the fake, never a real one
        self.addCleanup(p.stop)
        self.hub = self.home / ".local/share/fairshare/hub"
        cwd = os.getcwd()                # launches run in HOME, where no trust question is asked
        os.chdir(self.home)
        self.addCleanup(os.chdir, cwd)

    def person(self, folder: str, who: str) -> None:
        """The login the fake's sign-in gives in the hub folder `folder`."""
        (self.ids / folder).write_text(who)

    def config(self, hub_path: Path | None = None) -> None:
        write(self.cfg, f'hub = "{hub_path or self.hub}"\n')

    def go(self, *argv, answers=(), tty=False):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(cli, "_interactive", return_value=tty), \
                mock.patch("builtins.input", side_effect=list(answers)) as asked:
            code = cli.main(list(argv))
        self.asked = asked
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def calls(self, folder: str) -> list[str]:
        p = self.log / f"calls-{folder}"
        return lines(p) if p.exists() else []

    def env_of(self, folder: str) -> dict:
        return dict(l.split("=", 1) for l in lines(self.log / f"env-{folder}") if "=" in l)

    def ids_in_config(self) -> list[str]:
        return [a.id for a in load_accounts(self.cfg)[0]]

    def assert_linked(self, account_id: str, hub_path: Path | None = None) -> None:
        acct = hub.account_dir(hub_path or self.hub, account_id)
        for item in claude.SHARED:
            name = item.rstrip("/")
            with self.subTest(account=account_id, item=name):
                self.assertTrue((acct / name).is_symlink())
                self.assertEqual(os.readlink(acct / name), f"../../shared/{name}")


class UserFolders(Base):
    """Four folders of the user's own: two by alias, one signed out, and the default one."""

    def setUp(self):
        super().setUp()
        h = self.home
        write(h / ".zshrc", ZSHRC)
        work, play, old, default = (h / n for n in (".claude-work", ".claude-play", ".claude-old",
                                                    ".claude"))
        for d in (work, play, default):
            write(d / LOGIN, "never opened").chmod(0)
        write(play / "settings.json", json.dumps(
            {"theme": "light", "statusLine": {"type": "command", "command": "~/bin/line.sh"}},
            indent=2) + "\n")
        write(work / "settings.json", json.dumps(
            {"theme": "dark", "env": {"SECRET_TOKEN": "s3cr3t-value"}}, indent=2) + "\n")
        write(default / "history.jsonl", '{"display": "d1"}\n')
        write(play / "history.jsonl", '{"display": "h2"}\n{"display": "h3"}\n')
        write(work / "history.jsonl", '{"display": "h1"}\n{"display": "h2"}\n')
        write(play / "projects/-p-one/s1.jsonl", "B\n")
        write(play / "projects/-p-two/s2.jsonl", "two\n")
        write(work / "projects/-p-one/s1.jsonl", "A\n")
        write(work / "projects/-p-one/memory/MEMORY.md", "remember\n")
        write(old / "projects/-p-old/s0.jsonl", "old\n")
        write(old / ".claude.json", "{}")
        write(play / "CLAUDE.md", "play rules\n")
        write(work / "CLAUDE.md", "work rules\n")
        write(work / "skills/x/SKILL.md", "skill\n")
        write(work / ".claude.json", json.dumps({
            "userID": "user-work", "oauthAccount": {"emailAddress": "w@example.invalid"},
            "hasCompletedOnboarding": True, "lastOnboardingVersion": "2.1.300",
            "numStartups": 99,
            "projects": {"/p/one": {"hasTrustDialogAccepted": True, "allowedTools": ["x"]},
                         "/p/two": {"hasTrustDialogAccepted": False}},
            "mcpServers": {"github": {"command": "gh-mcp", "env": {"GH_TOKEN": "mcp-secret-value"}}}}))
        write(h / ".claude.json", json.dumps({"mcpServers": {"local": {"command": "l"}},
                                              "userID": "user-default"}))
        for folder, who in (("claude", "dave"), ("claude-old", "carol"), ("play", "bob"),
                            ("work", "alice")):
            self.person(folder, who)
        self.before = self.user_bytes()

    def user_bytes(self) -> dict:
        """Every entry of the user's own: kind, bytes or link target, and mode."""
        out = {}
        for p in sorted(self.home.rglob("*")):
            rel = p.relative_to(self.home)
            if rel.parts[0] == ".local":                       # the default hub lives here
                continue
            if p.is_symlink():
                out[rel] = ("link", os.readlink(p))
            elif p.is_dir():
                out[rel] = ("dir", p.stat().st_mode)
            else:
                p.chmod(p.stat().st_mode | 0o400)
                out[rel] = ("file", p.read_bytes(), p.stat().st_mode)
                if rel.name == LOGIN:
                    p.chmod(0)
        return out

    def assert_untouched(self):
        self.assertEqual(self.user_bytes(), self.before)

    def setup_all(self):
        # shown as claude (the default folder), claude-old, play, work; `all` joins the signed-in
        # ones first: claude, work, then claude-old, play. Questions: the hub folder, which to
        # join, then copying state from ~/.claude.json (claude) and ~/.claude-work (work).
        return self.go("setup", answers=["", "all", "y", "y"])


class Setup(UserFolders):
    def test_end_to_end_four_accounts_one_assistant(self):
        self.assertEqual(self.setup_all(), 0, self.err)
        self.assertEqual(self.ids_in_config(), ["claude", "work", "claude-old", "play"])
        self.assertIn("The first folder joined decides the shared settings.json, CLAUDE.md, "
                      "keybindings.json; 'all' joins in this order: claude, work, claude-old, "
                      "play\n", self.out)
        self.assertEqual(load_accounts(self.cfg)[2], self.hub)
        self.assertIn("claude-old", [l for l in self.out.splitlines() if "(signed out)" in l][0])
        for i in self.ids_in_config():
            self.assert_linked(i)
            self.assertEqual(self.calls(i)[:2], ["auth login", "auth status --json"])
        shared = hub.shared_dir(self.hub)
        # history: every folder's lines, once each, in order
        self.assertEqual(lines(shared / "history.jsonl"),
                         ['{"display": "d1"}', '{"display": "h1"}', '{"display": "h2"}',
                          '{"display": "h3"}'])
        # transcripts: a clash keeps both, the later one named after its account
        self.assertEqual((shared / "projects/-p-one/s1.jsonl").read_text(), "A\n")
        self.assertEqual((shared / "projects/-p-one/s1.from-play.jsonl").read_text(), "B\n")
        for f in ("-p-two/s2.jsonl", "-p-old/s0.jsonl", "-p-one/memory/MEMORY.md"):
            self.assertTrue((shared / "projects" / f).is_file(), f)
        self.assertTrue((shared / "skills/x/SKILL.md").is_file())
        # settings: the first seed wins and is wired; a later difference is shown, not applied,
        # with the variable's value hidden and the file to edit to adopt it named
        s = json.loads((shared / "settings.json").read_text())
        self.assertEqual(s["theme"], "dark")
        self.assertEqual(s["statusLine"]["command"], f"{self.bin}/fairshare capture")
        self.assertIn("settings.json: differs from the hub's", self.out)
        self.assertIn('"SECRET_TOKEN": "(hidden)"', self.out)
        self.assertIn(f"to adopt {self.home}/.claude-play/settings.json instead, edit "
                      f"{shared}/settings.json, keeping fairshare's own entries\n", self.out)
        self.assertEqual((shared / "CLAUDE.md").read_text(), "work rules\n")
        self.assertIn("+play rules", self.out)
        self.assertIn(f"to adopt {self.home}/.claude-play/CLAUDE.md instead, edit "
                      f"{shared}/CLAUDE.md\n", self.out)
        # every account sees the same items through its links
        for i in self.ids_in_config():
            acct = hub.account_dir(self.hub, i)
            self.assertEqual((acct / "projects/-p-one/s1.jsonl").read_text(), "A\n")
            self.assertEqual((acct / "settings.json").read_text(),
                             (shared / "settings.json").read_text())
        # state: only trust, user MCP servers and onboarding came over; identity never
        work = json.loads((hub.account_dir(self.hub, "work") / ".claude.json").read_text())
        self.assertEqual(work["userID"], "u-work")                  # the account's own
        self.assertNotIn("oauthAccount", work)
        self.assertEqual(work["numStartups"], 0)
        self.assertEqual(work["projects"], {"/p/one": {"hasTrustDialogAccepted": True}})
        self.assertEqual(set(work["mcpServers"]), {"github"})
        self.assertEqual((work["hasCompletedOnboarding"], work["lastOnboardingVersion"]),
                         (True, "2.1.300"))
        default = json.loads((hub.account_dir(self.hub, "claude") / ".claude.json").read_text())
        self.assertEqual((set(default["mcpServers"]), default["userID"]), ({"local"}, "u-claude"))
        for secret in SECRETS:
            self.assertNotIn(secret, self.out + self.err)
        self.assertFalse(list(self.hub.glob("accounts/*/.claude.json.lock")))
        self.assert_untouched()

    def test_numbers_join_in_the_order_typed(self):
        self.assertEqual(self.go("setup", answers=["", "3 4", "y"]), 0, self.err)
        self.assertEqual(self.ids_in_config(), ["play", "work"])
        s = json.loads((hub.shared_dir(self.hub) / "settings.json").read_text())
        self.assertEqual(s["theme"], "light")                       # play's: joined first
        self.assertEqual(s["statusLine"]["command"],
                         f"{self.bin}/fairshare capture --then '~/bin/line.sh'")
        self.assertEqual((hub.shared_dir(self.hub) / "CLAUDE.md").read_text(), "play rules\n")
        self.assert_untouched()

    def test_picking_some_joins_those_only(self):
        self.assertEqual(self.go("setup", answers=["", "3"]), 0, self.err)
        self.assertEqual(self.ids_in_config(), ["play"])
        self.assert_untouched()

    def test_a_chosen_hub_folder_is_stored(self):
        elsewhere = self.tmp / "my hub"
        self.assertEqual(self.go("setup", answers=[str(elsewhere), ""]), 0, self.err)
        self.assertEqual(load_accounts(self.cfg)[2], elsewhere)
        self.assertTrue((elsewhere / "shared/settings.json").is_file())
        self.assertEqual(self.go("setup", answers=[""]), 0)          # asked no hub this time
        self.assertIn(f"Hub: {elsewhere}", self.out)

    def test_without_a_terminal_it_says_so_and_exits_2(self):
        self.assertEqual(self.go("setup", answers=[EOFError()]), 2)
        self.assertIn("no terminal", self.err)
        self.assertFalse(self.cfg.exists())

    def test_it_needs_an_installed_fairshare(self):
        (self.bin / "fairshare").unlink()
        with self.assertRaises(SystemExit) as e:
            self.go("setup")
        self.assertIn("install it first", str(e.exception))
        self.assertFalse(self.cfg.exists())

    def test_a_config_that_cannot_be_parsed_is_refused_not_replaced(self):
        write(self.cfg, "hub = \n")
        with self.assertRaises(SystemExit) as e:
            self.go("setup")
        self.assertIn("fix it or move it aside", str(e.exception))
        self.assertEqual(self.cfg.read_text(), "hub = \n")


class Join(UserFolders):
    def test_without_from_it_signs_in_a_fresh_account(self):
        self.config()
        self.person("account-1", "erin")
        self.person("account-2", "frank")
        self.assertEqual(self.go("join"), 0, self.err)
        self.assertEqual(self.go("join"), 0, self.err)
        self.assertEqual(self.ids_in_config(), ["account-1", "account-2"])
        self.assert_linked("account-1")
        self.assertEqual(json.loads((hub.shared_dir(self.hub) / "settings.json").read_text()),
                         {"statusLine": {"type": "command",
                                         "command": f"{self.bin}/fairshare capture",
                                         "refreshInterval": 60},
                          "hooks": own_hooks(f"{self.bin}/fairshare")})
        self.assertEqual(store.subscription_seen("account-1")[0], True)
        self.asked.assert_not_called()
        self.assert_untouched()

    def test_from_takes_the_alias_name_and_copies_only(self):
        self.config()
        self.assertEqual(self.go("join", "--from", "~/.claude-work", answers=["y"]), 0, self.err)
        self.assertEqual(self.ids_in_config(), ["work"])
        self.assertEqual((hub.shared_dir(self.hub) / "CLAUDE.md").read_text(), "work rules\n")
        self.assert_untouched()

    def test_the_running_sessions_and_their_keys_are_not_copied(self):
        """A folder whose sessions/ links to a registry kept elsewhere, holding a live session's
        record and key: the hub shares sessions/ but starts it empty."""
        registry = self.home / ".elsewhere/sessions"
        write(registry / "4242.json", '{"pid": 4242}')
        write(registry / "4242.abc.key", "session-key-value")
        os.symlink(registry, self.home / ".claude-work/sessions")
        self.before = self.user_bytes()
        self.config()
        self.assertEqual(self.go("join", "--from", "~/.claude-work", answers=["y"]), 0, self.err)
        self.assertEqual(list((hub.shared_dir(self.hub) / "sessions").iterdir()), [])
        self.assertTrue((hub.account_dir(self.hub, "work") / "sessions").is_symlink())
        self.assertNotIn("sessions/: copied", self.out)
        self.assert_untouched()

    def test_declining_the_state_copy_copies_none_of_it(self):
        self.config()
        self.person("w2", "alice")
        self.assertEqual(self.go("join", "w2", "--from", str(self.home / ".claude-work"),
                                 answers=["n"]), 0)
        state = json.loads((hub.account_dir(self.hub, "w2") / ".claude.json").read_text())
        self.assertEqual(state, {"userID": "u-w2", "numStartups": 0})

    def test_same_login_as_another_account_asks_before_keeping_it(self):
        self.config()
        self.person("play", "alice")                             # the same person as work
        self.go("join", "--from", "~/.claude-work", answers=["n"])
        self.assertEqual(self.go("join", "--from", "~/.claude-play", answers=["n"]), 1)
        self.assertIn("same login as work: one quota", self.out)
        self.assertEqual(self.ids_in_config(), ["work"])
        self.assertTrue(hub.account_dir(self.hub, "play").is_dir())      # nothing deleted
        self.assertEqual(self.go("join", "--from", "~/.claude-play", answers=["y"]), 0)
        self.assertEqual(self.ids_in_config(), ["work", "play"])
        for secret in SECRETS:
            self.assertNotIn(secret, self.out + self.err)

    def test_a_sign_in_that_fails_adds_nothing(self):
        self.config()
        self.assertEqual(self.go("join", "nobody"), 1)
        self.assertIn("sign-in did not finish", self.err)
        self.assertEqual(self.ids_in_config(), [])

    def test_a_login_that_is_not_a_subscription_adds_nothing(self):
        self.config()
        self.person("k", "kim")
        with mock.patch.dict(os.environ, {"FAKE_METHOD": "api_key"}):
            self.assertEqual(self.go("join", "k"), 1)
        self.assertIn("not a subscription login", self.err)
        self.assertEqual(self.ids_in_config(), [])

    def test_bad_input_is_refused(self):
        self.config()
        self.person("work", "alice")
        self.go("join", "work")
        for argv, why in ((["join", "work", "--from", "~/.claude-work"], "already joined"),
                          (["join", "--from", "~/.claude-work"], "already joined"),
                          (["join", "../x"], "account id"),
                          (["join", "--from", str(self.tmp / "nope")], "not a folder"),
                          (["join", "--from", str(self.hub / "shared")], "inside the hub")):
            with self.subTest(argv=argv):
                self.assertEqual(self.go(*argv), 2)
                self.assertIn(why, self.err)

    def test_no_config_says_run_setup(self):
        with self.assertRaises(SystemExit) as e:
            self.go("join")
        self.assertIn("fairshare setup", str(e.exception))

    def test_a_config_that_is_a_symlink_is_written_through(self):
        real = write(self.tmp / "dotfiles/accounts.toml", f'# mine\nhub = "{self.hub}"\n')
        self.cfg.parent.mkdir(parents=True)
        self.cfg.symlink_to(real)
        self.person("a", "ann")
        self.assertEqual(self.go("join", "a"), 0, self.err)
        self.assertTrue(self.cfg.is_symlink())
        self.assertTrue(real.read_text().startswith(f'# mine\nhub = "{self.hub}"\n'))
        self.assertEqual(self.ids_in_config(), ["a"])

    def test_the_state_file_is_written_under_the_providers_lock(self):
        self.config()
        lock = hub.account_dir(self.hub, "work") / claude.STATE_LOCK
        held = []
        real = store.write_with_backup

        def spy(path, text, backups=None):
            if path.name == claude.STATE_FILE:
                held.append(lock.is_dir())
            return real(path, text, backups)
        with mock.patch.object(store, "write_with_backup", spy):
            self.go("join", "--from", "~/.claude-work", answers=["y"])
        self.assertEqual(held, [True])
        self.assertFalse(lock.exists())

    def test_a_held_lock_is_waited_for_then_refused(self):
        lock = self.tmp / "x.lock"
        lock.mkdir()
        with self.assertRaises(OSError), store.held(lock, 0.2, stale=30):
            pass
        self.assertTrue(lock.is_dir())                           # someone else's: left alone

    def test_a_state_change_while_asking_is_shown_and_asked_again(self):
        self.config()
        dest = hub.account_dir(self.hub, "work") / claude.STATE_FILE
        answers = iter(["y", "y"])

        def answer(question):
            if not hasattr(answer, "done"):           # meanwhile, the account gets a server
                answer.done = True
                d = json.loads(dest.read_text())
                dest.write_text(json.dumps({**d, "mcpServers": {"github": {"command": "mine"}}}))
            return next(answers)
        with mock.patch.object(cli, "_yes", side_effect=answer) as asked:
            self.assertEqual(self.go("join", "--from", "~/.claude-work"), 0, self.err)
        self.assertEqual(asked.call_count, 2)
        state = json.loads(dest.read_text())
        self.assertEqual(state["mcpServers"], {"github": {"command": "mine"}})   # theirs kept
        self.assertTrue(state["hasCompletedOnboarding"])


class Leave(UserFolders):
    def setUp(self):
        super().setUp()
        write(self.cfg, f'# my accounts\nhub = "{self.hub}"\n')
        for i, who in (("a", "ann"), ("b", "ben")):
            self.person(i, who)
            self.go("join", i)

    def test_leave_stops_routing_and_keeps_the_folder_and_login(self):
        self.assertEqual(self.go("leave", "a"), 0, self.err)
        self.assertEqual(self.ids_in_config(), ["b"])
        self.assertTrue(self.cfg.read_text().startswith("# my accounts\n"))
        self.assertTrue((hub.account_dir(self.hub, "a") / LOGIN).exists())
        self.assertNotIn("auth logout", self.calls("a"))

    def test_logout_also_signs_it_out(self):
        self.assertEqual(self.go("leave", "b", "--logout"), 0, self.err)
        self.assertEqual(self.ids_in_config(), ["a"])
        self.assertEqual(self.calls("b")[-1], "auth logout")
        self.assertFalse((hub.account_dir(self.hub, "b") / LOGIN).exists())
        self.assertTrue(hub.account_dir(self.hub, "b").is_dir())
        self.assert_untouched()

    def test_a_failed_logout_changes_nothing(self):
        write(self.bin / "claude", '#!/bin/sh\nexit 3\n')
        self.assertEqual(self.go("leave", "b", "--logout"), 1)
        self.assertEqual(self.ids_in_config(), ["a", "b"])

    def test_an_unknown_id_lists_the_configured_ones(self):
        self.assertEqual(self.go("leave", "zed"), 2)
        self.assertIn("configured: a, b", self.err)


class Launch(UserFolders):
    def setUp(self):
        super().setUp()
        self.config()
        self.person("a", "ann")
        self.go("join", "a")

    def test_a_session_runs_with_the_accounts_hub_folder(self):
        for argv in ([], ["run", "-p", "hi"], ["run", "-a", "a"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.go(*argv), 0, self.err)
                self.assertEqual(self.env_of("a")["CLAUDE_CONFIG_DIR"],
                                 str(hub.account_dir(self.hub, "a")))
        self.assert_untouched()

    def test_the_start_check_runs_first_and_says_what_it_changed(self):
        acct = hub.account_dir(self.hub, "a")
        (acct / "skills").unlink()
        (acct / "skills").mkdir()
        write(acct / "skills/new/SKILL.md", "new\n")
        self.assertEqual(self.go("run"), 0, self.err)
        self.assertIn("fairshare: a: skills/ was a real folder: 1 file merged", self.err)
        self.assertTrue((hub.shared_dir(self.hub) / "skills/new/SKILL.md").is_file())
        self.assert_linked("a")

    def test_a_hub_that_cannot_be_repaired_starts_nothing(self):
        with mock.patch.object(hub, "check", side_effect=PermissionError("denied")), \
                mock.patch.object(session, "run_child") as child:
            self.assertEqual(self.go("run"), 1)
        child.assert_not_called()
        self.assertIn("not started", self.err)

    def test_moving_the_hub_keeps_every_link_valid(self):
        self.person("b", "ben")
        self.go("join", "b")
        moved = self.tmp / "moved" / "hub"
        moved.parent.mkdir()
        os.rename(self.hub, moved)
        self.cfg.write_text(self.cfg.read_text().replace(str(self.hub), str(moved)))
        for i in ("a", "b"):
            self.assertEqual(hub.check(moved, i, claude), [])           # nothing to repair
            self.assert_linked(i, moved)
            self.assertEqual(os.path.realpath(hub.account_dir(moved, i) / "projects"),
                             os.path.realpath(moved / "shared/projects"))
        self.assertEqual(self.go("run", "-a", "b"), 0, self.err)
        self.assertEqual(self.env_of("b")["CLAUDE_CONFIG_DIR"], str(moved / "accounts/b"))


class StartCheck(Base):
    """Every case of hub.check, on a hub in a temp folder."""

    def setUp(self):
        super().setUp()
        self.shared, self.acct = hub.shared_dir(self.hub), hub.account_dir(self.hub, "a")

    def check(self):
        return hub.check(self.hub, "a", claude)

    def backed_up(self, name):
        return sorted(self.hub.glob(f"backups/*-a/{name}"))

    def test_missing_links_are_created_in_one_line_and_folders_exist_first(self):
        notes = self.check()
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("a: linked projects, history.jsonl"))
        for item in claude.SHARED:
            if item.endswith("/"):
                self.assertTrue((self.shared / item).is_dir(), item)
        self.assertEqual(self.check(), [])
        self.assertFalse((self.hub / "backups").exists())

    def test_a_real_folder_is_merged_and_a_clash_keeps_both(self):
        self.check()
        write(self.shared / "projects/p/s.jsonl", "shared\n")
        write(self.shared / "projects/p/same.jsonl", "same\n")
        (self.acct / "projects").unlink()
        write(self.acct / "projects/p/s.jsonl", "mine\n")
        write(self.acct / "projects/p/same.jsonl", "same\n")
        write(self.acct / "projects/p/new.jsonl", "new\n")
        (notes,) = self.check()
        self.assertIn("a: projects/ was a real folder: 2 files merged", notes)
        self.assertEqual((self.shared / "projects/p/s.jsonl").read_text(), "shared\n")
        self.assertEqual((self.shared / "projects/p/s.from-a.jsonl").read_text(), "mine\n")
        self.assertEqual((self.shared / "projects/p/new.jsonl").read_text(), "new\n")
        (left,) = self.backed_up("projects")
        self.assertEqual((left / "p/same.jsonl").read_text(), "same\n")   # nothing deleted
        self.assertEqual(os.readlink(self.acct / "projects"), "../../shared/projects")

    def test_a_real_history_file_adds_the_lines_shared_lacks_in_order(self):
        self.check()
        write(self.shared / "history.jsonl", "L1\nL2")                  # no final newline
        (self.acct / "history.jsonl").unlink()
        write(self.acct / "history.jsonl", "L2\nL3\nL4\n")
        (notes,) = self.check()
        self.assertIn("2 lines added", notes)
        self.assertEqual(lines(self.shared / "history.jsonl"), ["L1", "L2", "L3", "L4"])
        self.assertEqual(self.backed_up("history.jsonl")[0].read_text(), "L2\nL3\nL4\n")

    def test_differing_settings_keep_the_shared_ones_and_show_the_difference(self):
        self.check()
        write(self.shared / "settings.json", '{"theme": "dark", "env": {"K": "secret-v"}}')
        (self.acct / "settings.json").unlink()
        write(self.acct / "settings.json", '{"theme": "light", "env": {"K": "secret-v"}}')
        (notes,) = self.check()
        self.assertIn("a: settings.json differed from the shared one, which is kept", notes)
        self.assertIn('-  "theme": "dark",', notes)
        self.assertIn('+  "theme": "light",', notes)
        self.assertNotIn("secret-v", notes)
        self.assertEqual(json.loads((self.shared / "settings.json").read_text())["theme"], "dark")
        self.assertIn("light", self.backed_up("settings.json")[0].read_text())
        self.assertEqual((self.acct / "settings.json").read_text(),
                         (self.shared / "settings.json").read_text())

    def test_an_identical_file_is_set_aside_and_relinked(self):
        self.check()
        write(self.shared / "CLAUDE.md", "rules\n")
        (self.acct / "CLAUDE.md").unlink()
        write(self.acct / "CLAUDE.md", "rules\n")
        (notes,) = self.check()
        self.assertIn("real copy of the shared one", notes)
        self.assertEqual(len(self.backed_up("CLAUDE.md")), 1)

    def test_a_file_the_hub_lacks_becomes_the_shared_one(self):
        self.check()
        (self.acct / "keybindings.json").unlink()
        write(self.acct / "keybindings.json", "[]\n")
        (notes,) = self.check()
        self.assertIn("it is now the shared one", notes)
        self.assertEqual((self.shared / "keybindings.json").read_text(), "[]\n")

    def test_a_foreign_link_is_backed_up_and_relinked(self):
        self.check()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        for name, target in (("skills", str(elsewhere)),
                             ("agents", str(self.shared / "agents"))):   # right place, absolute
            (self.acct / name).unlink()
            (self.acct / name).symlink_to(target)
        notes = self.check()
        self.assertEqual(len(notes), 2)
        self.assertIn(f"a: skills/ pointed to {elsewhere}", notes[0])
        (kept,) = self.backed_up("skills")
        self.assertEqual(os.readlink(kept), str(elsewhere))
        self.assert_linked("a")

    def test_a_folder_where_a_file_belongs_is_moved_aside(self):
        self.check()
        (self.acct / "settings.json").unlink()
        write(self.acct / "settings.json" / "x", "x")
        (notes,) = self.check()
        self.assertIn("was a folder", notes)
        self.assertEqual((self.backed_up("settings.json")[0] / "x").read_text(), "x")

    def test_moving_the_whole_hub_needs_no_repair(self):
        self.check()
        moved = self.tmp / "elsewhere-hub"
        os.rename(self.hub, moved)
        self.assertEqual(hub.check(moved, "a", claude), [])


class Capture_(Base):
    PAYLOAD = json.dumps({"rate_limits": {"seven_day": {"used_percentage": 40, "resets_at": 4e9}}})

    def setUp(self):
        super().setUp()
        write(self.cfg, f'hub = "{self.hub}"\n\n[[account]]\nid = "a"\nplan = "claude-pro"\n\n'
                        '[[account]]\nid = "b"\nplan = "claude-pro"\n')

    def capture(self, folder, *argv):
        with mock.patch.object(sys, "stdin", mock.Mock(buffer=io.BytesIO(self.PAYLOAD.encode()))), \
                mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(folder)}):
            return self.go("capture", *argv)

    def test_the_account_comes_from_the_sessions_folder(self):
        self.assertEqual(self.capture(hub.account_dir(self.hub, "b")), 0)
        self.assertEqual(store.load("b").windows["seven_day"].used_pct, 40)
        self.assertIsNone(store.load("a"))
        self.assertEqual(self.out, "b · 5h - · 7d 40%\n")

    def test_a_payload_without_usage_shows_the_saved_snapshot_and_its_time(self):
        # A session's first redraws carry no usage until its first exchange with the service.
        store.save("b", Snapshot(time.time() - 60, {"seven_day": Window(55, 4e9),
                                                    "five_hour": Window(90, time.time() - 1)}))
        self.PAYLOAD = json.dumps({"model": {}})
        self.assertEqual(self.capture(hub.account_dir(self.hub, "b")), 0)
        self.assertRegex(self.out, r"^b · 5h - · 7d 55% · as of \d\d:\d\d\n$")   # reset: unknown
        self.assertEqual(store.load("b").windows["seven_day"].used_pct, 55)      # nothing written

    def test_windows_shortest_first_and_a_tight_one_coloured_with_its_reset(self):
        now = time.time()
        self.PAYLOAD = json.dumps({"rate_limits": {
            "seven_day": {"used_percentage": 92, "resets_at": now + 7200},
            "five_hour": {"used_percentage": 23, "resets_at": now + 3600}}})
        self.assertEqual(self.capture(hub.account_dir(self.hub, "a")), 0)
        reset = cli._clock(now + 7200, now)
        self.assertEqual(self.out, f"a · 5h 23% · \033[33m7d 92% ({reset})\033[0m\n")

    def test_a_spent_window_is_red(self):
        self.PAYLOAD = json.dumps({"rate_limits": {"seven_day": {"used_percentage": 99,
                                                                 "resets_at": 4e9}}})
        self.assertEqual(self.capture(hub.account_dir(self.hub, "a")), 0)
        self.assertTrue(self.out.startswith("a · 5h - · \033[31m7d 99% ("), self.out)

    def test_with_nothing_saved_every_window_is_unknown(self):
        self.PAYLOAD = json.dumps({"model": {}})
        self.assertEqual(self.capture(hub.account_dir(self.hub, "a")), 0)
        self.assertEqual(self.out, "a · 5h - · 7d -\n")

    def test_outside_the_hub_nothing_is_recorded_and_their_line_still_runs(self):
        for argv in (["--then", "true"], ["a", "--then", "true"]):   # old form too
            with self.subTest(argv=argv):
                with mock.patch.object(subprocess, "run", wraps=subprocess.run) as ran:
                    self.assertEqual(self.capture(self.home / ".claude-work", *argv), 0)
                self.assertEqual(ran.call_args.args[0], "true")
                self.assertIsNone(store.load("a"))

    def test_the_hub_statusline_records_and_shows_their_line(self):
        self.config()
        write(self.home / "line.sh", '#!/bin/sh\ncat > "$HOME/seen.json"\necho THEIR LINE\n'
              ).chmod(0o755)
        write(hub.shared_dir(self.hub) / "settings.json", json.dumps(
            {"statusLine": {"type": "command", "command": str(self.home / "line.sh")}}))
        with contextlib.redirect_stdout(io.StringIO()):
            cli._wire_hub(claude, self.hub, str(self.bin / "fairshare"))
        write(self.cfg, self.cfg.read_text() + '\n[[account]]\nid = "a"\nplan = "claude-pro"\n')
        cmd = json.loads((hub.shared_dir(self.hub) / "settings.json").read_text())["statusLine"]["command"]
        (self.bin / "fairshare").unlink()      # the command runs by its path, not through PATH
        real = write(self.tmp / "real", '#!/bin/sh\nexec "$FAIRSHARE_PY" -I -c "import sys; '
                     'sys.path.insert(0, \'$FAIRSHARE_SRC\'); from fairshare.cli import main; '
                     'sys.exit(main())" "$@"\n')
        real.chmod(0o755)
        (self.bin / "fairshare").symlink_to(real)
        src = str(Path(cli.__file__).resolve().parent.parent)
        r = subprocess.run(cmd, shell=True, input=self.PAYLOAD, capture_output=True, text=True,
                           env={**os.environ, "PATH": "/usr/bin:/bin", "FAIRSHARE_PY": sys.executable,
                                "FAIRSHARE_SRC": src,
                                "CLAUDE_CONFIG_DIR": str(hub.account_dir(self.hub, "a"))},
                           timeout=20)
        self.assertEqual(r.stdout, "THEIR LINE\n")                          # theirs, not ours
        self.assertEqual((self.home / "seen.json").read_text(), self.PAYLOAD)
        self.assertEqual(store.load("a").windows["seven_day"].used_pct, 40)


class BareAndUninstall(UserFolders):
    def test_bare_fairshare_with_no_config(self):
        self.assertEqual(self.go(), 2)                                     # no terminal
        self.assertIn("run `fairshare setup`", self.err)
        self.assertEqual(self.go(tty=True, answers=["n"]), 2)
        self.assertFalse(self.cfg.exists())
        self.assertEqual(self.go(tty=True, answers=["", "", ""]), 0, self.err)   # yes, default hub,
        self.assertTrue(self.cfg.exists())                                 # join none
        self.assert_untouched()

    def test_uninstall_changes_nothing_and_says_how(self):
        self.setup_all()
        everything = {p: (p.read_bytes() if p.is_file() and not p.is_symlink() else None)
                      for p in self.tmp.rglob("*") if p.name != LOGIN}
        self.assertEqual(self.go("uninstall"), 0)
        self.assertEqual({p: (p.read_bytes() if p.is_file() and not p.is_symlink() else None)
                          for p in self.tmp.rglob("*") if p.name != LOGIN}, everything)
        self.assertIn(f"Hub: {self.hub}", self.out)
        self.assertIn("accounts: claude, claude-old, play, work", self.out)
        self.assertIn("pipx uninstall fairshare", self.out)
        self.assertIn(f"one shared folder, {self.hub / 'shared'}", self.out)
        self.assertIn("history.jsonl: append the lines my file lacks", self.out)
        self.assertIn("~/.claude-work", self.out)
        self.assertIn("State (usage snapshots, session records and checkpoint notes): ", self.out)
        self.assertIn("  each stays signed in (on macOS, one keychain entry per folder) when the "
                      "hub is deleted; `fairshare leave --logout <id>` signs one out first\n",
                      self.out)
        olds = sorted(p.name for p in self.cfg.parent.glob("accounts.toml.bak-fairshare-*"))
        self.assertTrue(olds)                                   # each join wrote one
        self.assertIn(f"  backups of it, beside it: {', '.join(olds)}\n", self.out)
        prompt = self.out[self.out.index("fairshare kept my"):]
        self.assertNotIn("sessions/", prompt)                   # join never copies it
        self.assertIn("projects/", prompt)
        self.assertIn("except skills/fairshare/\n  (fairshare's own skill)", prompt)
        self.assert_untouched()

    def test_uninstall_with_no_config(self):
        self.assertEqual(self.go("uninstall"), 0)
        self.assertIn("(none)", self.out)


class Pure(unittest.TestCase):
    THEIRS = '{\n  "theme": "dark",\n  "statusLine": {"type": "command", "command": "~/l.sh \'a b\'", "padding": 0}\n}\n'

    def test_wire_wraps_theirs_and_keeps_every_other_byte(self):
        new = claude.wire(self.THEIRS, "/bin/fairshare")
        self.assertEqual(new, self.THEIRS.replace('"~/l.sh \'a b\'"',
                                                  '"/bin/fairshare capture --then \'~/l.sh \'\\"\'\\"\'a b\'\\"\'\\"\'\'"')
                         .replace('"padding": 0}', '"padding": 0, "refreshInterval": 60}')
                         .replace('}\n}\n', '},\n  "hooks": '
                                  + OWN_HOOKS_TEXT.replace("{r}", "/bin/fairshare") + '\n}\n'))
        self.assertEqual(claude.wired(new), Capture(("/bin/fairshare",), "~/l.sh 'a b'"))
        self.assertIsNone(claude.wire(new, "/bin/fairshare"))             # already wired

    def test_wire_adds_a_hook_where_there_is_none(self):
        self.assertEqual(claude.wire('{\n  "theme": "dark"\n}\n', "fairshare"),
                         '{\n  "theme": "dark",\n  "statusLine": {"type": "command", "command": '
                         '"fairshare capture", "refreshInterval": 60},\n  "hooks": '
                         + OWN_HOOKS_TEXT.replace("{r}", "fairshare") + '\n}\n')
        self.assertEqual(json.loads(claude.wire(None, "f")),
                         {"statusLine": {"type": "command", "command": "f capture",
                                         "refreshInterval": 60},
                          "hooks": own_hooks("f")})
        self.assertEqual(json.loads(claude.wire("{}", "f"))["statusLine"]["command"], "f capture")

    def test_an_older_capture_is_replaced_and_keeps_what_it_wrapped(self):
        older = ("/usr/bin/python3 -I -c 'import sys; sys.path.insert(0, \"/old/src\"); "
                 "from fairshare.cli import main' capture work --then 'mine --x'")
        for cmd, then in ((older, "mine --x"), ("/old/fairshare capture work", None)):
            with self.subTest(cmd=cmd):
                new = claude.wire(json.dumps({"statusLine": {"type": "command", "command": cmd}}), "/new/fairshare")
                self.assertEqual(claude.wired(new), Capture(("/new/fairshare",), then))

    def test_unexpected_layouts_are_refused(self):
        for bad in ('{"statusLine": "x"}', "[1]", "{not json"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                claude.wire(bad, "f")

    def test_merge_state_takes_only_trust_mcp_and_onboarding(self):
        src = {"userID": "x", "oauthAccount": {"a": 1}, "hasCompletedOnboarding": True,
               "projects": {"/t": {"hasTrustDialogAccepted": True, "allowedTools": ["y"]},
                            "/f": {"hasTrustDialogAccepted": False}},
               "mcpServers": {"s": {"command": "1"}, "mine": {"command": "theirs"}}}
        dest = {"userID": "own", "mcpServers": {"mine": {"command": "mine"}},
                "projects": {"/t": {"allowedTools": ["z"]}}}
        new, what = claude.merge_state(dest, src)
        self.assertEqual(new, {"userID": "own", "hasCompletedOnboarding": True,
                               "mcpServers": {"mine": {"command": "mine"}, "s": {"command": "1"}},
                               "projects": {"/t": {"allowedTools": ["z"], "hasTrustDialogAccepted": True}}})
        self.assertEqual(what, ["trust for 1 project", "MCP servers s", "onboarding (hasCompletedOnboarding)"])
        self.assertEqual(claude.merge_state(new, src)[1], [])

    def test_the_default_folders_state_file_is_beside_it_in_home(self):
        home = Path("/h")
        self.assertEqual(claude.state_file(home / ".claude", home), home / ".claude.json")
        self.assertEqual(claude.state_file(home / ".claude-x", home), home / ".claude-x/.claude.json")

    def test_config_edits_touch_only_their_own_lines(self):
        text = '# top\nhub = "/h"\n\n[policy]\ngrace_hours = 2.0\n\n# first\n[[account]]\nid = "a"\nplan = "claude-pro"\n\n[[account]]\nid = "b"\nplan = "claude-max"  # mine\n'
        added = with_account(text, {"id": "c", "plan": "claude-pro"})
        self.assertTrue(added.startswith(text))
        self.assertEqual(without_account(added, "c"), text + "\n")
        self.assertEqual(without_account(text, "b"),
                         '# top\nhub = "/h"\n\n[policy]\ngrace_hours = 2.0\n\n# first\n[[account]]\nid = "a"\nplan = "claude-pro"\n\n')
        self.assertEqual(with_hub("", "/x"), 'hub = "/x"\n')
        self.assertEqual(with_hub(text, "/h"), text)
        self.assertEqual(with_hub(text, "/x"), text.replace('hub = "/h"', 'hub = "/x"'))
        self.assertTrue(with_hub("# c\n", "/x").startswith('hub = "/x"\n'))
        for bad in (lambda: with_account(text, {"id": "a", "plan": "claude-pro"}),
                    lambda: with_account(text, {"id": "../e", "plan": "claude-pro"}),
                    lambda: with_account(text, {"id": "d", "plan": "nope"}),
                    lambda: without_account(text, "zed")):
            with self.assertRaises(ValueError):
                bad()


if __name__ == "__main__":
    unittest.main()
