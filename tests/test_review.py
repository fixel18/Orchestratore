from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from orch.config import load_config
from orch.git_paths import project_pathspecs
from orch.review import (
    MAX_UNTRACKED_BYTES, ReviewDiffError, build_claude_prompt, collect_review_diff,
    truncate_review_text,
)
from orch.verification import VerificationResult


TEST_ROOT = Path(__file__).resolve().parent


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        config_path = self.base / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        self.config = load_config(config_path)

    def git(self, diff=b"diff --git a/app.py b/app.py\n+modifica\n", names=()):
        return patch("orch.review.subprocess.run", side_effect=[
            Mock(returncode=0, stdout=diff, stderr=b""),
            Mock(returncode=0, stdout="\0".join(names).encode("utf-8") + b"\0", stderr=b""),
        ])

    def test_tracked_diff_untracked_names_and_text_content(self):
        (self.base / "nuovo con spazi.py").write_text("print('città')\n", encoding="utf-8")
        with self.git(names=("nuovo con spazi.py",)) as git:
            diff = collect_review_diff(self.config, self.base)
        self.assertIn("+modifica", diff)
        self.assertIn('"nuovo con spazi.py"', diff)
        self.assertIn("print('città')", diff)
        self.assertEqual(git.call_args_list[0].args[0], [
            "git", "diff", "--no-ext-diff", "--no-textconv", "HEAD", *project_pathspecs(self.config),
            ":(glob,exclude,icase)**/.env*", ":(glob,exclude,icase).env*",
        ])
        self.assertEqual(git.call_args_list[1].args[0], [
            "git", "ls-files", "--others", "--exclude-standard", "-z",
            *project_pathspecs(self.config),
            ":(glob,exclude,icase)**/.env*", ":(glob,exclude,icase).env*",
        ])
        self.assertEqual(git.call_args_list[0].kwargs["cwd"], str(self.base))

    def test_review_uses_literal_exclusions_for_tracked_and_untracked_files(self):
        with self.git() as git:
            collect_review_diff(self.config, self.base, exclude_paths=["utente[1].docx"])
        for call in git.call_args_list:
            self.assertIn(":(exclude,literal)utente[1].docx", call.args[0])

    def test_user_context_is_outside_diff_and_touched_files_are_reviewable(self):
        folder = self.base / "regole"
        folder.mkdir()
        (folder / "prompt-verifica-claude.md").write_text("Regole", encoding="utf-8")
        prompt = build_claude_prompt(self.config, "Fascicolo", 1, "Pezzo", "Risposta", [], "DIFF",
                                     preexisting_paths=["utente.docx"], touched_preexisting_paths=["toccato.txt"])
        context, diff = prompt.split("# DIFF PER LA REVISIONE", 1)
        self.assertIn("Modifiche dell'utente, non da revisionare: utente.docx", context)
        self.assertIn("File preesistenti toccati durante la task, da revisionare: toccato.txt", context)
        self.assertEqual(diff.strip(), "DIFF")

    def test_env_names_are_filtered_before_any_file_open_or_stat(self):
        # Sono solo nomi nella risposta git finta: nessun file .env* viene creato.
        names = (".env", ".env.example", "sub/.env.local", ".ENV.SECRET", ".env-dir/file.py")
        with self.git(names=names):
            with patch.object(Path, "open", side_effect=AssertionError("Non leggere file riservati")):
                with patch.object(Path, "stat", side_effect=AssertionError("Non accedere ai file riservati")):
                    # Path.resolve su Windows può effettuare stat del progetto: mock mirato.
                    with patch.object(Path, "resolve", return_value=self.base):
                        diff = collect_review_diff(self.config, self.base)
        for name in names:
            self.assertNotIn(name, diff)

    def test_binary_invalid_utf8_controls_and_large_files_are_omitted(self):
        (self.base / "binario.bin").write_bytes(b"a\0b")
        (self.base / "encoding.bin").write_bytes(b"\xff\xfe")
        (self.base / "controlli.bin").write_bytes(b"\x01\x02\x03")
        (self.base / "grande.txt").write_bytes(b"x" * (MAX_UNTRACKED_BYTES + 1))
        with self.git(names=("binario.bin", "encoding.bin", "controlli.bin", "grande.txt")):
            diff = collect_review_diff(self.config, self.base)
        self.assertIn("file binario", diff)
        self.assertIn("oltre 200 KB", diff)
        self.assertNotIn("x" * 100, diff)

    def test_large_file_is_not_opened(self):
        (self.base / "grande.txt").write_bytes(b"x" * (MAX_UNTRACKED_BYTES + 1))
        with self.git(names=("grande.txt",)):
            with patch.object(Path, "open", side_effect=AssertionError("Non aprire file grandi")):
                self.assertIn("oltre 200 KB", collect_review_diff(self.config, self.base))

    def test_paths_outside_project_or_missing_files_are_not_read(self):
        with self.git(names=("../esterno.py", "C:/esterno.py", "/esterno.py", "mancante.py")):
            with patch.object(Path, "open", side_effect=AssertionError("Non aprire percorsi esterni")):
                diff = collect_review_diff(self.config, self.base)
        self.assertIn("percorso non sicuro", diff)
        self.assertIn("file non disponibile", diff)

    def test_symbolic_links_are_not_opened_without_creating_real_links(self):
        with self.git(names=("link.py",)):
            with patch.object(Path, "is_symlink", return_value=True):
                with patch.object(Path, "open", side_effect=AssertionError("Non seguire link")):
                    self.assertIn("link o percorso non sicuro", collect_review_diff(self.config, self.base))

    def test_git_errors_and_timeouts_are_explicit(self):
        for error in (OSError("Git assente"), subprocess.TimeoutExpired("git", 20)):
            with self.subTest(error=error):
                with patch("orch.review.subprocess.run", side_effect=error):
                    with self.assertRaises(ReviewDiffError):
                        collect_review_diff(self.config, self.base)
        with patch("orch.review.subprocess.run", return_value=Mock(returncode=128, stderr=b"bad HEAD")):
            with self.assertRaisesRegex(ReviewDiffError, "128"):
                collect_review_diff(self.config, self.base)

    def test_diff_is_truncated_without_reading_more_untracked_content(self):
        with self.git(diff=b"+" * 70000, names=("non-leggere.py",)):
            with patch.object(Path, "open", side_effect=AssertionError("Budget esaurito")):
                diff = collect_review_diff(self.config, self.base)
        self.assertEqual(len(diff), 60000)
        self.assertIn("TRONCATO", diff)

    def test_prompt_contains_all_inputs_and_summary_without_full_command_output(self):
        folder = self.base / "regole"
        folder.mkdir()
        (folder / "prompt-verifica-claude.md").write_text("Istruzioni revisore", encoding="utf-8")
        verification = VerificationResult("test", 1, False, "OUTPUT COMPLETO DA NON INVIARE", ("2: ERROR test",), None, 1)
        prompt = build_claude_prompt(
            self.config, "Fascicolo intero", 4, "Integra Claude", "Ultima risposta",
            [verification], "DIFF COMPLETO",
        )
        for expected in ("Istruzioni revisore", "Fascicolo intero", "PEZZO 4", "Integra Claude",
                         "Ultima risposta", "2: ERROR test", '"exit_code": 1', "DIFF COMPLETO"):
            self.assertIn(expected, prompt)
        self.assertNotIn("OUTPUT COMPLETO DA NON INVIARE", prompt)

    def test_total_prompt_has_sixty_thousand_character_limit(self):
        folder = self.base / "regole"
        folder.mkdir()
        (folder / "prompt-verifica-claude.md").write_text("Regole", encoding="utf-8")
        prompt = build_claude_prompt(self.config, "f" * 61000, 4, "pezzo", "risposta", [], "diff")
        self.assertEqual(len(prompt), 60000)
        self.assertIn("TRONCATO", prompt)
        self.assertTrue(prompt.startswith("Regole"))

    def test_truncation_boundary_and_marker(self):
        self.assertEqual(truncate_review_text("breve", 100), "breve")
        self.assertEqual(truncate_review_text("a" * 100, 100), "a" * 100)
        cut = truncate_review_text("a" * 101, 100)
        self.assertEqual(len(cut), 100)
        self.assertIn("TRONCATO", cut)
        with self.assertRaises(ValueError):
            truncate_review_text("a", 2)


if __name__ == "__main__":
    unittest.main()
