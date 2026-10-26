from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from orch.config import load_config
from orch.git_ops import GitError, GitOperations
from orch.git_paths import project_pathspecs


TEST_ROOT = Path(__file__).resolve().parent


class GitOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        path = self.base / "config.json"
        path.write_text("{}", encoding="utf-8")
        self.runner = Mock(return_value=Mock(returncode=0, stdout=b"", stderr=b""))
        self.logs = []
        self.git = GitOperations(load_config(path), runner=self.runner, logger=self.logs.append)

    def test_commit_stash_and_push_use_exact_argument_lists_without_force(self):
        self.git.add(self.base)
        self.git.commit(self.base, "001: Titolo")
        self.git.stash(self.base, "orchestratore: task 001 scartata")
        self.git.push(self.base, "produzione3")
        commands = [call.args[0] for call in self.runner.call_args_list]
        self.assertEqual(commands, [
            ["git", "add", "-A", *project_pathspecs(self.git.config)], ["git", "commit", "-m", "001: Titolo"],
            ["git", "stash", "push", "-u", "-m", "orchestratore: task 001 scartata",
             *project_pathspecs(self.git.config)],
            ["git", "push", "origin", "produzione3"]])
        self.assertEqual(self.runner.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")

    def test_dirty_uses_the_same_exclusions_as_add(self):
        self.assertFalse(self.git.dirty(self.base))
        status = self.runner.call_args.args[0]
        self.git.add(self.base)
        add = self.runner.call_args.args[0]
        self.assertEqual(status, ["git", "status", "--porcelain", "--untracked-files=all", *add[3:]])
        self.assertIn(":(exclude,glob)**/__pycache__/**", status)

    def test_custom_patterns_are_literal_arguments_and_empty_list_is_supported(self):
        from dataclasses import replace
        self.git.config = replace(self.git.config, escludi_dal_commit=["**/file con spazi.cache"])
        self.git.add(self.base)
        self.assertEqual(self.runner.call_args.args[0], [
            "git", "add", "-A", "--", ".", ":(exclude,glob)**/file con spazi.cache",
        ])
        self.git.config = replace(self.git.config, escludi_dal_commit=[])
        self.git.add(self.base)
        self.assertEqual(self.runner.call_args.args[0], ["git", "add", "-A", "--", "."])

    def test_missing_identity_has_clear_error_and_preserves_command_output(self):
        self.runner.return_value = Mock(returncode=128, stdout=b"", stderr=b"Author identity unknown: user.email")
        with self.assertRaisesRegex(GitError, "Identità git mancante"):
            self.git.commit(self.base, "001: Titolo")
        self.assertIn("Author identity unknown", self.logs[-1])

    def test_missing_origin_and_unborn_head_are_not_successful_values(self):
        self.runner.return_value = Mock(returncode=128, stdout=b"", stderr=b"missing")
        self.assertFalse(self.git.has_origin(self.base))
        self.assertIsNone(self.git.head(self.base))

    def test_commit_and_stash_metadata_support_crash_recovery(self):
        self.runner.return_value = Mock(returncode=0, stdout=b"hash\nparent\n001: Titolo\n", stderr=b"")
        self.assertEqual(self.git.commit_info(self.base), {"hash": "hash", "parents": ["parent"], "subject": "001: Titolo"})
        self.runner.return_value = Mock(returncode=0, stdout=b"hash\nstash@{0}\nOn branch: task scartata\n", stderr=b"")
        self.assertEqual(self.git.latest_stash(self.base)["name"], "stash@{0}")
        self.runner.return_value = Mock(returncode=0, stdout=b"", stderr=b"")
        self.assertIsNone(self.git.latest_stash(self.base))

    def test_commit_recovery_preserves_full_multiline_message_without_git_trailing_newline(self):
        message = "001: Titolo\n\nInclude modifiche preesistenti: documento.docx"
        self.runner.return_value = Mock(returncode=0, stdout=("hash\nparent\n" + message + "\n\n").encode(), stderr=b"")
        self.assertEqual(self.git.commit_info(self.base)["subject"], message)
        self.assertIn("--format=%H%n%P%n%B", self.runner.call_args.args[0])

    def test_preexisting_paths_include_rename_source_deleted_files_and_untracked_bytes(self):
        import hashlib
        (self.base / "nuovo nome.txt").write_bytes(b"contenuto utente")
        (self.base / "documento[1].docx").write_bytes(b"PK\x00dati")
        self.runner.return_value = Mock(returncode=0, stdout=(
            b"R  nuovo nome.txt\0vecchio nome.txt\0 D cancellato.txt\0?? documento[1].docx\0"), stderr=b"")
        changes = self.git.preexisting_changes(self.base)
        self.assertEqual(changes, {
            "nuovo nome.txt": hashlib.sha256(b"contenuto utente").hexdigest(),
            "vecchio nome.txt": None, "cancellato.txt": None,
            "documento[1].docx": hashlib.sha256(b"PK\x00dati").hexdigest(),
        })
        self.assertIn("-z", self.runner.call_args.args[0])
        self.assertEqual(self.runner.call_args.args[0][-len(project_pathspecs(self.git.config)):],
                         project_pathspecs(self.git.config))

    def test_stash_and_dirty_use_literal_preexisting_exclusions(self):
        paths = ["documento[1].docx", "nome con spazi.txt"]
        self.git.stash(self.base, "scarto", exclude_paths=paths)
        self.assertEqual(self.runner.call_args.args[0], [
            "git", "stash", "push", "-u", "-m", "scarto", *project_pathspecs(self.git.config),
            ":(exclude,literal)documento[1].docx", ":(exclude,literal)nome con spazi.txt"])
        self.git.dirty(self.base, exclude_paths=paths)
        self.assertEqual(self.runner.call_args.args[0][-2:],
                         [":(exclude,literal)" + name for name in paths])

    def test_added_queries_are_sorted_numerically_and_filtered_by_extension(self):
        self.runner.return_value = Mock(returncode=0, stdout=(
            b"supabase/queries/159_b.sql\0supabase/queries/10_ten.sql\0"
            b"supabase/queries/README.md\0supabase/queries/158_a.sql\0supabase/queries/9_nine.sql\0"), stderr=b"")
        self.assertEqual(self.git.added_queries(self.base, "before", "after"), [
            "supabase/queries/9_nine.sql", "supabase/queries/10_ten.sql",
            "supabase/queries/158_a.sql", "supabase/queries/159_b.sql"])
        self.assertEqual(self.runner.call_args.args[0], [
            "git", "diff", "--name-only", "--diff-filter=A", "-z", "before", "after",
            "--", ":(literal)supabase/queries"])

    def test_added_queries_support_custom_folder_and_initial_commit(self):
        from dataclasses import replace
        self.git.config = replace(self.git.config, cartella_query="sql con spazi")
        self.assertEqual(self.git.added_queries(self.base, None, "initial"), [])
        self.assertEqual(self.runner.call_args.args[0], [
            "git", "diff-tree", "--root", "--no-commit-id", "--name-only", "--diff-filter=A",
            "-r", "-z", "initial", "--", ":(literal)sql con spazi"])
