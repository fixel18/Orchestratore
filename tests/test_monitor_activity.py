import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import monitor
from orch.live_activity import ActivityReader

TEST_ROOT = Path(__file__).resolve().parent


class MonitorActivityTests(unittest.TestCase):
    def test_phase_names_actual_provider_in_both_roles(self):
        self.assertEqual(monitor.descrivi_fase({'fase': 'invio_codex', 'esecutore_corrente': 'claude'}),
                         'Claude sta lavorando')
        self.assertEqual(monitor.descrivi_fase({'fase': 'claude', 'azione_esterna': 'codex'}),
                         'Codex sta revisionando')
        self.assertEqual(monitor.descrivi_fase({'fase': 'claude', 'controllore_corrente': 'codex'}),
                         'Codex sta revisionando')
        self.assertEqual(monitor.descrivi_fase({'task_corrente': {'id': '015'}}, vivo=False), 'in pausa')
        self.assertEqual(monitor.descrivi_fase({'fase': 'invio_codex', 'fuori_fascia_dal': '2026-10-24'}),
                         'fuori dalle fasce orarie: in pausa')

    def test_calcola_keeps_full_session_while_stopped(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as folder:
            base = Path(folder)
            events = [{'event_id': str(i), 'at': f'2026-10-23T12:00:{i:02}',
                       'messaggio': 'Passaggio completo ' * 20 + str(i)} for i in range(24)]
            state = {'periodo': {'inizio': '2026-10-24T11:00:00'},
                     'ultimo_periodo': {'inizio': '2026-10-23T11:00:00', 'eventi': events}}
            (base / 'stato.json').write_text(json.dumps(state), encoding='utf-8')
            with patch.object(monitor, 'BASE', base), patch.object(monitor, '_activity_reader', ActivityReader()):
                data = monitor.calcola()
        self.assertEqual(len(data['eventi']), 24)
        self.assertGreater(len(data['eventi'][-1]), 140)
        self.assertEqual(data['inizio_periodo'], '2026-10-23T11:00:00')
        self.assertFalse(data['vivo'])
        self.assertEqual(data['attivita'], '')


if __name__ == '__main__':
    unittest.main()
