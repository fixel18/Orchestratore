from pathlib import Path
import tempfile
import unittest

from orch.tasks import TaskError, load_task, parse_task, scan_queue, select_next_task


ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(__file__).resolve().parent


def dossier(*, task_id="007", priority="media", push="si"):
    push_line = f"push: {push}\n" if push is not None else ""
    return (
        f"---\nid: {task_id}\ntitolo: Una task\n"
        "progetto: C:\\progetti\\app\nbranch: produzione3\n"
        f"priorita: {priority}\n{push_line}---\n"
        "\n## Contesto\nUn contesto.\n"
        "\n## Obiettivo\nUn obiettivo.\n"
        "\n## Passi\n1. Primo pezzo.\n   Continua qui.\n"
        "\n2. Secondo pezzo.\n"
        "\n## Verifica\npython -m py_compile app.py\n"
        "```powershell\npython -m unittest -v\n```\n"
        "\n## Fuori perimetro\n- Un altro progetto.\n"
    )


class TaskParserTests(unittest.TestCase):
    def test_complete_task(self):
        text = dossier()
        task = parse_task(text, "007-task.md")
        self.assertEqual(task.id, "007")
        self.assertEqual(task.titolo, "Una task")
        self.assertEqual(task.progetto, Path(r"C:\progetti\app"))
        self.assertEqual(task.branch, "produzione3")
        self.assertEqual(task.priorita, "media")
        self.assertTrue(task.push)
        self.assertEqual(task.file, Path("007-task.md"))
        self.assertEqual(task.fascicolo, text)
        self.assertEqual([p.numero for p in task.pezzi], [1, 2])
        self.assertEqual(task.pezzi[0].testo, "Primo pezzo.\n   Continua qui.")
        self.assertEqual(task.comandi_verifica, (
            "python -m py_compile app.py", "python -m unittest -v",
        ))
        self.assertIn("Un contesto.", task.sezioni["contesto"])
        self.assertIn("Un altro progetto.", task.sezioni["fuori perimetro"])

    def test_public_example_dossier(self):
        path = ROOT / "examples" / "001-esempio.md"
        task = load_task(path)
        self.assertEqual(task.id, "001")
        self.assertEqual(task.titolo, "Documentare un modulo")
        self.assertEqual(task.progetto, Path(r"C:\progetti\app"))
        self.assertEqual(task.branch, "main")
        self.assertEqual(task.priorita, "media")
        self.assertFalse(task.push)
        self.assertEqual([p.numero for p in task.pezzi], [1, 2])
        self.assertTrue(task.pezzi[0].testo.startswith("Leggere il modulo"))
        self.assertEqual(task.comandi_verifica, ("python -m unittest discover -s tests -v",))

    def test_push_default_and_explicit_values(self):
        for value, expected in ((None, True), ("SI", True), ("no", False)):
            with self.subTest(value=value):
                self.assertEqual(parse_task(dossier(push=value)).push, expected)

    def test_whitespace_case_bom_and_crlf(self):
        text = dossier(priority=" ALTA ", push=" No ")
        text = text.replace("id: 007", " ID : '007' ")
        text = text.replace("## Contesto", "  ##   cOnTeStO  ##")
        text = text.replace("## Fuori perimetro", "## FUORI   PERIMETRO  ")
        task = parse_task("\ufeff\n  " + text.replace("\n", "\r\n"))
        self.assertEqual(task.id, "007")
        self.assertEqual(task.priorita, "alta")
        self.assertFalse(task.push)
        self.assertEqual(len(task.pezzi), 2)

    def test_colons_and_quoted_values(self):
        text = dossier().replace("titolo: Una task", 'titolo: "Task: seconda parte"')
        task = parse_task(text)
        self.assertEqual(task.titolo, "Task: seconda parte")
        self.assertEqual(str(task.progetto), r"C:\progetti\app")

    def test_multiline_pieces_nested_lists_and_code(self):
        old = "1. Primo pezzo.\n   Continua qui.\n\n2. Secondo pezzo."
        new = (
            "1. Primo pezzo.\n   1. Lista annidata.\n   2. Ancora annidata.\n"
            "\n   ```markdown\n## Passi\n99. Codice, non un pezzo.\n   ```\n"
            "   Dopo il codice.\n\n2. Secondo pezzo.\n\n   Altro paragrafo."
        )
        task = parse_task(dossier().replace(old, new))
        self.assertEqual(len(task.pezzi), 2)
        self.assertIn("99. Codice, non un pezzo.", task.pezzi[0].testo)
        self.assertIn("1. Lista annidata.", task.pezzi[0].testo)
        self.assertTrue(task.pezzi[1].testo.endswith("Altro paragrafo."))

    def test_uniformly_indented_steps_and_parenthesis_markers(self):
        text = dossier().replace("1. Primo", "  1) Primo").replace("2. Secondo", "  2) Secondo")
        self.assertEqual(len(parse_task(text).pezzi), 2)

    def test_tilde_and_long_fences_and_h3_headings(self):
        text = dossier().replace("Un contesto.", (
            "### Un sottotitolo\n~~~~markdown\n## Passi\n~~~~\n"
            "````markdown\n```\n## Verifica\n````"
        ))
        task = parse_task(text)
        self.assertIn("### Un sottotitolo", task.sezioni["contesto"])
        self.assertEqual(len(task.pezzi), 2)
        self.assertEqual(len(task.comandi_verifica), 2)

    def test_multiple_command_blocks_and_blank_lines(self):
        text = dossier().replace(
            "python -m py_compile app.py\n```powershell\npython -m unittest -v\n```",
            "```powershell\n  python one.py  \n\n```\npython two.py\n~~~\npython three.py\n~~~",
        )
        self.assertEqual(parse_task(text).comandi_verifica, (
            "python one.py", "python two.py", "python three.py",
        ))

    def test_empty_verification_section(self):
        text = dossier().replace(
            "python -m py_compile app.py\n```powershell\npython -m unittest -v\n```", ""
        )
        self.assertEqual(parse_task(text).comandi_verifica, ())

    def test_missing_or_empty_required_metadata(self):
        for key in ("id", "titolo", "progetto", "branch", "priorita"):
            for empty in (False, True):
                with self.subTest(key=key, empty=empty):
                    lines = dossier().splitlines()
                    text = "\n".join(
                        (key + ":" if empty else "") if line.startswith(key + ":") else line
                        for line in lines
                    )
                    with self.assertRaisesRegex(TaskError, key):
                        parse_task(text)

    def test_malformed_front_matter(self):
        cases = (
            "", dossier().replace("---", "", 1), "---\nid: 001\n",
            dossier().replace("titolo: Una task", "titolo Una task"),
            dossier().replace("id: 007", "id: 007\nID: 008"),
        )
        for text in cases:
            with self.subTest(text=text[:60]):
                with self.assertRaises(TaskError):
                    parse_task(text, "non-valida.md")

    def test_invalid_priority_or_push(self):
        for text in (dossier(priority="urgente"), dossier(push="true"), dossier(push="")):
            with self.subTest(text=text[:100]):
                with self.assertRaises(TaskError):
                    parse_task(text)

    def test_missing_and_duplicate_sections(self):
        for section in ("Contesto", "Obiettivo", "Passi", "Verifica", "Fuori perimetro"):
            with self.subTest(section=section):
                with self.assertRaisesRegex(TaskError, "Sezioni mancanti"):
                    parse_task(dossier().replace("## " + section, "### " + section))
        with self.assertRaisesRegex(TaskError, "duplicata"):
            parse_task(dossier() + "\n## CONTESTO\nAncora.")

    def test_invalid_or_empty_steps(self):
        original = "1. Primo pezzo.\n   Continua qui.\n\n2. Secondo pezzo."
        for replacement in ("", "Testo libero.", "2. Salto.", "1. Uno.\n3. Tre.", "1. Uno.\n1. Due."):
            with self.subTest(replacement=replacement):
                with self.assertRaises(TaskError):
                    parse_task(dossier().replace(original, replacement))

    def test_load_missing_or_invalid_utf8(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as temp:
            path = Path(temp) / "task.md"
            with self.assertRaisesRegex(TaskError, "task.md"):
                load_task(path)
            path.write_bytes(b"\xff\xfe")
            with self.assertRaises(TaskError):
                load_task(path)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.queue = Path(self.temp.name)

    def write_task(self, name, priority):
        path = self.queue / name
        path.write_text(dossier(priority=priority), encoding="utf-8")
        return path

    def test_priority_then_filename_without_changes(self):
        self.write_task("000-bassa.md", "bassa")
        self.write_task("000-media.md", "media")
        self.write_task("020-alta.md", "alta")
        expected = self.write_task("010-alta.md", "alta")
        before = {p.name: p.read_bytes() for p in self.queue.iterdir()}
        selected = select_next_task(self.queue)
        self.assertEqual(selected.file, expected)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.queue.iterdir()})

    def test_empty_queue_and_ignored_files(self):
        (self.queue / ".gitkeep").touch()
        (self.queue / "appunti.txt").write_text("non una task", encoding="utf-8")
        (self.queue / "directory.md").mkdir()
        self.assertIsNone(select_next_task(self.queue))

    def test_uppercase_extension(self):
        path = self.write_task("001-task.MD", "media")
        self.assertEqual(select_next_task(self.queue).file, path)

    def test_invalid_tasks_are_returned_separately_and_do_not_block_queue(self):
        broken = self.queue / "rotta.md"
        broken.write_text("non valida", encoding="utf-8")
        invalid_utf8 = self.queue / "illeggibile.md"
        invalid_utf8.write_bytes(b"\xff")
        valid = self.write_task("valida.md", "alta")
        tasks, errors = scan_queue(self.queue)
        self.assertEqual([task.file for task in tasks], [valid])
        self.assertEqual([path for path, _ in errors], [invalid_utf8, broken])
        self.assertTrue(all(message for _, message in errors))
        self.assertIn("Front matter", errors[1][1])
        self.assertEqual(select_next_task(self.queue).file, valid)
        self.assertTrue(broken.exists())

    def test_scan_orders_valid_tasks_and_handles_only_invalid_files(self):
        low = self.write_task("000-bassa.md", "bassa")
        high = self.write_task("900-alta.md", "alta")
        self.assertEqual([t.file for t in scan_queue(self.queue)[0]], [high, low])
        low.unlink()
        high.unlink()
        (self.queue / "rotta.md").write_text("non valida", encoding="utf-8")
        tasks, errors = scan_queue(self.queue)
        self.assertEqual(tasks, [])
        self.assertEqual(len(errors), 1)
        self.assertIsNone(select_next_task(self.queue))

    def test_missing_queue_is_an_error(self):
        with self.assertRaisesRegex(TaskError, "Impossibile leggere la coda"):
            select_next_task(self.queue / "assente")


if __name__ == "__main__":
    unittest.main()
