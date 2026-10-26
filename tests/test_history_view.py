"""Test unitari per orch.history_view senza avvio della GUI Tkinter."""

import inspect
from pathlib import Path
import sys
import unittest

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from orch.history_view import create_history, update_history


class FakeTextWidget:
    """Simulatore del widget tk.Text per testare il comportamento senza GUI."""

    def __init__(self, state="disabled", yview=(0.0, 1.0), content=""):
        self.state = state
        self._yview = tuple(yview)
        self.content = content
        self._history_lines = None
        self.see_calls = []
        self.yview_moveto_calls = []
        self.insert_calls = []
        self.delete_calls = []
        self.tags = {}
        self.state_history = [state]

    def yview(self):
        return self._yview

    def yview_moveto(self, fraction):
        self.yview_moveto_calls.append(fraction)
        self._yview = (fraction, min(1.0, fraction + 0.4))

    def see(self, index):
        self.see_calls.append(index)
        if index == "end":
            self._yview = (0.6, 1.0)

    def cget(self, item):
        if item == "state":
            return self.state
        raise KeyError(item)

    def config(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]
            self.state_history.append(self.state)

    def configure(self, **kwargs):
        self.config(**kwargs)

    def insert(self, index, text):
        if self.state == "disabled":
            raise RuntimeError("Impossibile inserire testo in un widget disabilitato")
        self.insert_calls.append((index, text))
        if index == "end":
            self.content += text
        elif index == "1.0":
            self.content = text + self.content
        else:
            number = int(index.split('.')[0])
            rows = self.content.splitlines(keepends=True)
            self.content = ''.join(rows[:number - 1]) + text + ''.join(rows[number - 1:])

    def delete(self, start, end):
        if self.state == "disabled":
            raise RuntimeError("Impossibile cancellare testo da un widget disabilitato")
        self.delete_calls.append((start, end))
        if start == "1.0" and end == "end":
            self.content = ""

    def tag_ranges(self, tag):
        ranges = self.tags.get(tag, [])
        flat = []
        for start, end in ranges:
            flat.extend([start, end])
        return tuple(flat)

    def tag_add(self, tag, start, end):
        if tag not in self.tags:
            self.tags[tag] = []
        self.tags[tag].append((start, end))

    def tag_remove(self, tag, start, end):
        if tag in self.tags:
            self.tags[tag] = []


class HistoryViewTests(unittest.TestCase):
    def test_create_history_interface(self):
        sig = inspect.signature(create_history)
        self.assertIn("parent", sig.parameters)
        self.assertIn("background", sig.parameters)

    def test_initial_population_replaces_and_scrolls_to_end_if_at_bottom(self):
        w = FakeTextWidget(state="disabled", yview=(0.0, 1.0))
        lines = ["10:00 Task 1", "10:01 Task 2"]

        update_history(w, lines)

        self.assertEqual(w.delete_calls, [("1.0", "end")])
        self.assertEqual(w.insert_calls, [("1.0", "10:00 Task 1\n10:01 Task 2")])
        self.assertEqual(w.see_calls, ["end"])
        self.assertEqual(w._history_lines, lines)
        self.assertEqual(w.state, "disabled")

    def test_no_work_when_unchanged(self):
        w = FakeTextWidget(state="disabled", yview=(0.0, 1.0))
        lines = ["Evento 1", "Evento 2"]
        update_history(w, lines)

        insert_count = len(w.insert_calls)
        delete_count = len(w.delete_calls)
        see_count = len(w.see_calls)

        # Seconda chiamata con le stesse righe
        update_history(w, ["Evento 1", "Evento 2"])

        self.assertEqual(len(w.insert_calls), insert_count)
        self.assertEqual(len(w.delete_calls), delete_count)
        self.assertEqual(len(w.see_calls), see_count)

    def test_prefix_appends_only_new_lines_and_follows_when_at_bottom(self):
        w = FakeTextWidget(state="disabled", yview=(0.0, 1.0))
        update_history(w, ["Riga 1", "Riga 2"])

        w.delete_calls.clear()
        w.insert_calls.clear()
        w.see_calls.clear()

        # Aggiunta di una terza riga (prefisso rispettato)
        update_history(w, ["Riga 1", "Riga 2", "Riga 3"])

        self.assertEqual(w.delete_calls, [])  # nessuna cancellazione
        self.assertEqual(w.insert_calls, [("end", "\nRiga 3")])
        self.assertEqual(w.see_calls, ["end"])
        self.assertEqual(w._history_lines, ["Riga 1", "Riga 2", "Riga 3"])
        self.assertEqual(w.content, "Riga 1\nRiga 2\nRiga 3")

    def test_prefix_append_no_scroll_stealing_when_scrolled_up(self):
        w = FakeTextWidget(state="disabled", yview=(0.2, 0.7))  # utente ha fatto scroll in alto
        update_history(w, ["Riga 1", "Riga 2"])

        w.see_calls.clear()
        w.insert_calls.clear()
        # Viewport ancora scrollato in alto
        w._yview = (0.2, 0.7)

        update_history(w, ["Riga 1", "Riga 2", "Riga 3"])

        self.assertEqual(w.insert_calls, [("end", "\nRiga 3")])
        # see('end') NON deve essere chiamato se l'utente è scrollato in alto
        self.assertEqual(w.see_calls, [])
        self.assertEqual(w._yview, (0.2, 0.7))

    def test_prefix_append_preserves_selection(self):
        w = FakeTextWidget(state="disabled", yview=(0.0, 1.0))
        update_history(w, ["Riga 1", "Riga 2"])

        # Utente seleziona una porzione di testo
        w.tag_add("sel", "1.0", "1.4")
        self.assertEqual(w.tag_ranges("sel"), ("1.0", "1.4"))

        update_history(w, ["Riga 1", "Riga 2", "Riga 3"])

        # La selezione deve essere preservata
        self.assertEqual(w.tag_ranges("sel"), ("1.0", "1.4"))

    def test_non_prefix_replaces_all_lines(self):
        w = FakeTextWidget(state="disabled", yview=(0.0, 1.0))
        update_history(w, ["Vecchio 1", "Vecchio 2"])

        w.delete_calls.clear()
        w.insert_calls.clear()

        # Righe completamente diverse
        update_history(w, ["Nuovo 1", "Nuovo 2"])

        self.assertEqual(w.delete_calls, [("1.0", "end")])
        self.assertEqual(w.insert_calls, [("1.0", "Nuovo 1\nNuovo 2")])
        self.assertEqual(w._history_lines, ["Nuovo 1", "Nuovo 2"])

    def test_replace_preserves_viewport_when_scrolled_up(self):
        w = FakeTextWidget(state="disabled", yview=(0.3, 0.8))  # non a fondo
        update_history(w, ["Vecchio 1", "Vecchio 2"])

        w.yview_moveto_calls.clear()
        w.see_calls.clear()
        w._yview = (0.35, 0.85)

        update_history(w, ["Completamente diverso"])

        self.assertEqual(w.see_calls, [])
        self.assertIn(0.35, w.yview_moveto_calls)

    def test_empty_lines_handling(self):
        w = FakeTextWidget(state="disabled", yview=(0.0, 1.0))
        update_history(w, [])
        self.assertEqual(w._history_lines, [])
        self.assertEqual(w.content, "")

        # Seconda chiamata con [] non fa lavoro
        w.delete_calls.clear()
        update_history(w, [])
        self.assertEqual(w.delete_calls, [])

    def test_cli_group_grows_before_later_events_without_clearing_history(self):
        w = FakeTextWidget(state="disabled", yview=(0.2, 0.7))
        update_history(w, ['Avvio', 'Codex\nLeggo il codice', 'Conversazione salvata'])
        w.tag_add('sel', '1.0', '1.5')
        w.delete_calls.clear()
        w.insert_calls.clear()
        w.yview_moveto_calls.clear()
        update_history(w, ['Avvio', 'Codex\nLeggo il codice\nEseguo i test', 'Conversazione salvata'])
        self.assertEqual(w.delete_calls, [])
        self.assertEqual(w.insert_calls, [('4.0', 'Eseguo i test\n')])
        self.assertEqual(w.content, 'Avvio\nCodex\nLeggo il codice\nEseguo i test\nConversazione salvata')
        self.assertEqual(w.tag_ranges('sel'), ('1.0', '1.5'))
        self.assertEqual(w.yview_moveto_calls, [])
        self.assertEqual(w.see_calls, [])


if __name__ == "__main__":
    unittest.main()
