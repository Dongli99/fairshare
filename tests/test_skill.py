"""Stage 5b, part E: fairshare's skill in the hub. Temp HOME, config, state and hub (test_hub's
Base: fakes first on PATH, inherited session variables cleared)."""
import contextlib
import io

from fairshare import cli, hub
from fairshare.providers import claude

from test_hub import Base, write


class Shipped(Base):
    def test_the_shipped_text_is_a_skill_named_fairshare(self):
        dest, text = claude.skill(self.tmp / "shared")
        self.assertEqual(dest, self.tmp / "shared/skills/fairshare/SKILL.md")
        lines = text.splitlines()
        self.assertEqual(lines[0], "---")
        front = lines[1:lines.index("---", 1)]
        self.assertIn("name: fairshare", front)
        self.assertTrue(any(l.startswith("description: ") for l in front))
        self.assertTrue(claude.own_skill(text))
        for words in ("fairshare worker", "fairshare watch", "fairshare checkpoint",
                      "--request", "fairshare trust", "fairshare pickup <its session id>",
                      "subagent"):
            self.assertIn(words, text)

    def test_recognised_by_its_name_only(self):
        for text, own in (("---\nname: fairshare\n---\n", True),
                          ('---\ndescription: x\nname: "fairshare"\n---\nbody\n', True),
                          ("---\nname: other\n---\nname: fairshare\n", False),
                          ("---\ndescription: x\n---\nname: fairshare\n", False),
                          ("name: fairshare\n", False), ("", False)):
            with self.subTest(text=text):
                self.assertEqual(claude.own_skill(text), own)


class InHub(Base):
    def setUp(self):
        super().setUp()
        write(self.cfg, f'hub = "{self.hub}"\n\n[[account]]\nid = "a"\nplan = "claude-pro"\n')
        hub.check(self.hub, "a", claude)
        self.dest, self.text = claude.skill(hub.shared_dir(self.hub))
        self.mine = write(hub.shared_dir(self.hub) / "skills/mine/SKILL.md",
                          "---\nname: mine\n---\nmine\n")

    def doctor_line(self):
        self.go("doctor")
        return next(l for l in self.out.splitlines() if "skill in the hub" in l)

    def test_installed_in_one_line_by_fix_and_doctor_passes(self):
        self.assertEqual(self.doctor_line(), "FAIL  claude: skill in the hub is missing (run "
                                             "`fairshare doctor --fix`)")
        self.go("doctor", "--fix")
        self.assertNotIn("+name: fairshare", self.out)                 # new: no diff
        self.assertIn(f"fixed  skill: installed in {self.dest}\n", self.out)
        self.assertEqual(self.dest.read_text(), self.text)
        self.assertEqual(self.doctor_line(), "pass  claude: skill in the hub is current")
        self.assertEqual(self.mine.read_text(), "---\nname: mine\n---\nmine\n")   # untouched

    def test_setup_and_join_install_it_through_the_same_wiring(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._wire_hub(claude, self.hub, str(self.bin / "fairshare"))
        self.assertEqual(out.getvalue().splitlines()[-1], f"skill: installed in {self.dest}")
        self.assertNotIn("+name: fairshare", out.getvalue())
        self.assertEqual(self.dest.read_text(), self.text)
        self.assertIsNone(cli._install_skill(claude, self.hub))         # current: nothing to do

    def test_an_outdated_copy_is_refreshed_and_backed_up(self):
        write(self.dest, "---\nname: fairshare\ndescription: old\n---\nold text\n")
        self.assertIn("is outdated", self.doctor_line())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            note = cli._install_skill(claude, self.hub)
        self.assertIn("-description: old", out.getvalue())
        self.assertTrue(note.startswith("updated"), note)
        self.assertEqual(self.dest.read_text(), self.text)
        backups = list((self.hub / "backups").glob("*-skill/SKILL.md"))
        self.assertEqual(backups[0].read_text(), "---\nname: fairshare\ndescription: old\n---\n"
                                                 "old text\n")

    def test_another_skill_of_that_folder_is_never_touched(self):
        theirs = "---\nname: something-else\n---\ntheirs\n"
        write(self.dest, theirs)
        self.assertEqual(self.doctor_line(), "FAIL  claude: skill in the hub is not fairshare's: "
                                             "fairshare/ holds another skill")
        self.go("doctor", "--fix")
        self.assertEqual(self.dest.read_text(), theirs)
        self.assertIn("left alone", cli._install_skill(claude, self.hub))
        self.assertEqual(self.dest.read_text(), theirs)

    def test_every_account_sees_it_through_its_skills_link(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cli._install_skill(claude, self.hub)
        seen = hub.account_dir(self.hub, "a") / "skills/fairshare/SKILL.md"
        self.assertEqual(seen.read_text(), self.text)
