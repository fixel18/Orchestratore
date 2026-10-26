from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from orch.live_activity import ActivityReader, activity_metadata, clean_text, public_messages, session_history

TEST_ROOT = Path(__file__).resolve().parent


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / 'log').mkdir()
        self.reader = ActivityReader()
        self.state = {'periodo': {'inizio': '2026-10-23T23:00:00', 'task_ids': ['015'],
            'eventi': []}, 'task_corrente': {'id': '015'},
            'fase': 'invio_codex', 'azione_esterna': 'codex'}

    def logfile(self, tool='codex', stamp='20261024-001000', label='015'):
        suffix = 'jsonl' if tool == 'codex' else 'response.json'
        return self.base / 'log' / f'{label}-{stamp}-{"a" * 32}.{suffix}'

    def write(self, path, *events):
        with path.open('ab') as stream:
            for event in events:
                stream.write((json.dumps(event, ensure_ascii=False) + '\n').encode('utf-8'))

    def message(self, text):
        return {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}}

    def test_full_session_across_midnight_and_no_message_truncation(self):
        text = 'Verifica completa ' * 50
        self.state['periodo']['eventi'] = [{'event_id': str(i), 'at': f'2026-10-23T23:10:{i:02}',
            'task': {'id': '015'}, 'messaggio': text + str(i)} for i in range(30)]
        self.write(self.logfile(), self.message('Ora leggo il codice'))
        lines, activity, _ = self.reader.read(self.base, self.state)
        self.assertEqual(len(lines), 31)
        self.assertIn(text + '29', '\n'.join(lines))
        self.assertIn('Ora leggo il codice', activity)
        self.assertIn('23/10', lines[0])
        self.assertIn('24/10', lines[-1])

    def test_partial_utf8_lines_are_read_once_on_later_refresh(self):
        path = self.logfile()
        data = (json.dumps(self.message('Leggo la città'), ensure_ascii=False) + '\n').encode('utf-8')
        split = data.index('à'.encode('utf-8')) + 1
        path.write_bytes(data[:split])
        self.assertEqual(self.reader.read(self.base, self.state)[0], [])
        with path.open('ab') as stream:
            stream.write(data[split:])
        lines = self.reader.read(self.base, self.state)[0]
        self.assertIn('città', '\n'.join(lines))
        self.assertEqual(self.reader.read(self.base, self.state)[0], lines)

    def test_reader_ignores_stale_directory_size_and_appends(self):
        path = self.logfile()
        self.write(path, self.message('Prima'))
        self.reader.read(self.base, self.state)
        self.write(path, self.message('Seconda'))
        with patch.object(Path, 'stat', side_effect=AssertionError('No directory size for tailing')):
            cursor = self.reader._read(path, 'codex')
        self.assertEqual(cursor.messages, ['Prima', 'Seconda'])

    def test_truncated_log_resets_cursor(self):
        path = self.logfile()
        self.write(path, self.message('Vecchio messaggio piuttosto lungo'))
        self.reader.read(self.base, self.state)
        path.write_text(json.dumps(self.message('Nuovo')), encoding='utf-8')
        self.assertEqual(self.reader._read(path, 'codex').messages, ['Nuovo'])

    def test_claude_public_text_tools_and_final_repeat(self):
        path = self.logfile('claude')
        self.write(path, {'type': 'system', 'subtype': 'init', 'permissionMode': 'acceptEdits'},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'thinking', 'thinking': 'PRIVATE'},
                {'type': 'text', 'text': 'Controllo i componenti'},
                {'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'npm test', 'secret': 'PRIVATE'}}]}},
            {'type': 'result', 'result': 'Controllo i componenti'})
        self.state.update(azione_esterna='claude')
        lines, activity, _ = self.reader.read(self.base, self.state)
        text = '\n'.join(lines)
        self.assertNotIn('PRIVATE', text)
        self.assertEqual(text.count('Controllo i componenti'), 1)
        self.assertIn('Esecuzione dei test', text)
        self.assertIn('Claude (esecutore)', activity)

    def test_whitelist_excludes_reasoning_and_command_output(self):
        excluded = [
            ('codex', {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'PRIVATE'}}),
            ('claude', {'type': 'stream_event', 'event': {'delta': {'thinking': 'PRIVATE'}}}),
            ('claude', {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'PRIVATE'}]}}),
            ('codex', {'type': 'item.completed', 'item': []}),
        ]
        for tool, event in excluded:
            self.assertEqual(public_messages(event, tool), [])
        text = public_messages({'type': 'item.completed', 'item': {'type': 'command_execution',
            'command': 'npm test PRIVATE', 'aggregated_output': 'PRIVATE', 'exit_code': 0}}, 'codex')
        self.assertIn('test', text[0])
        self.assertNotIn('PRIVATE', str(text))

    def test_legacy_json_without_newline_and_redaction(self):
        path = self.logfile('claude')
        path.write_text(json.dumps({'result': 'Verifica OK', 'is_error': False}), encoding='utf-8')
        self.assertIn('Verifica OK', '\n'.join(self.reader.read(self.base, self.state)[0]))
        self.assertEqual(clean_text('Pubblico <think>PRIVATE</think> fatto'), 'Pubblico  fatto')
        self.assertNotIn('PRIVATE', clean_text('password=PRIVATE Bearer PRIVATE api_key: PRIVATE'))

    def test_filters_old_newer_and_other_task_logs(self):
        self.state['periodo']['fine'] = '2026-10-24T01:00:00'
        for stamp, task in [('20261023-220000', '015'), ('20261024-020000', '015'), ('20261024-001000', '099')]:
            self.write(self.logfile(stamp=stamp, label=task), self.message('Excluded'))
        self.assertEqual(self.reader.read(self.base, self.state)[0], [])

    def test_previous_session_kept_after_stop_and_cache_reset_on_new_start(self):
        previous = dict(self.state['periodo'])
        previous['eventi'] = [{'event_id': 'one', 'messaggio': 'Sessione conclusa'}]
        self.state.update(ultimo_periodo=previous, periodo={'inizio': '2026-10-24T01:00:00'})
        self.assertEqual(session_history(self.state)[0], previous)
        self.write(self.logfile(), self.message('Vecchio'))
        self.assertIn('Vecchio', '\n'.join(self.reader.read(self.base, self.state)[0]))
        self.state['periodo']['eventi'] = [{'messaggio': 'Nuovo Start'}]
        text = '\n'.join(self.reader.read(self.base, self.state)[0])
        self.assertIn('Nuovo Start', text)
        self.assertNotIn('Vecchio', text)

    def test_roles_metadata_distinguishes_codex_reviewer(self):
        path = self.logfile()
        activity_metadata(path.with_suffix('.activity.json'), 'codex', 'controllore')
        self.write(path, self.message('Revisiono le modifiche'))
        self.state.update(fase='claude')
        lines, activity, _ = self.reader.read(self.base, self.state)
        self.assertIn('Codex (controllore)', '\n'.join(lines))
        self.assertIn('Codex (controllore)', activity)


if __name__ == '__main__':
    unittest.main()
