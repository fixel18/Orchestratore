import unittest

from orch.queries import extract_query_notes, query_sort_key, query_task_lines, script_query_section


class QueryTests(unittest.TestCase):
    def test_extract_query_notes_through_end_of_notes_block(self):
        response = ("```text\nSTATO: FINITO\nNOTE: controllo manuale\n"
                    "QUERY DA ESEGUIRE SU SUPABASE: eseguire prima 158_a.sql\n"
                    "Poi 159_b.sql, verificare il risultato.\n```\nTesto fuori dal blocco")
        self.assertEqual(extract_query_notes(response),
                         "eseguire prima 158_a.sql\nPoi 159_b.sql, verificare il risultato.")
        self.assertEqual(extract_query_notes("STATO: FINITO\nNOTE: nulla"), "")
        self.assertEqual(extract_query_notes("NOTE: QUERY DA ESEGUIRE SU SUPABASE: nessuna"), "nessuna")
        self.assertEqual(extract_query_notes("QUERY DA ESEGUIRE SU SUPABASE:\n```"), "")

    def test_numeric_sort_and_unprefixed_files(self):
        self.assertEqual(sorted(["x.sql", "159_b.sql", "158_a.sql", "20_c.sql", "9_a.sql"],
                                key=query_sort_key),
                         ["9_a.sql", "20_c.sql", "158_a.sql", "159_b.sql", "x.sql"])

    def test_discard_never_lists_files_as_queries_to_execute(self):
        lines = "\n".join(query_task_lines({"esito": "scartata", "query_da_eseguire": ["158_bad.sql"],
                                           "note_query_codex": "SQL nello stash"}))
        self.assertIn("nessuna (task scartata: eventuali SQL sono nello stash, NON eseguirli)", lines)
        self.assertNotIn("158_bad.sql", lines)
        self.assertIn("SQL nello stash", lines)

    def test_script_order_across_tasks_and_projects_has_provenance_and_notes(self):
        tasks = [{"id": "001", "titolo": "Prima", "esito": "fatta", "progetto": "/repo-a",
                  "query_da_eseguire": ["sql/159_b.sql"], "note_query_codex": "Controllare B"},
                 {"id": "002", "titolo": "Seconda", "esito": "fatta", "progetto": "/repo-b",
                  "query_da_eseguire": ["sql/158_a.sql"], "note_query_codex": "Controllare A"}]
        section = script_query_section(tasks)
        self.assertLess(section.index("158_a.sql"), section.index("159_b.sql"))
        self.assertIn("task 001", section)
        self.assertIn("task 002", section)
        self.assertIn("Controllare A", section)
        self.assertIn("Controllare B", section)
