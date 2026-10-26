from datetime import datetime, timezone
import unittest

from orch import avvio_windows
from orch.fasce import in_fascia, prossimo_inizio


def ora(h, m=0):
    return datetime(2026, 10, 23, h, m, tzinfo=timezone.utc)


class FasceTests(unittest.TestCase):
    NOTTE = [{"da": "22:00", "a": "07:00"}]
    GIORNO = [{"da": "09:00", "a": "13:00"}, {"da": "14:30", "a": "18:00"}]

    def test_fascia_a_cavallo_della_mezzanotte(self):
        self.assertTrue(in_fascia(ora(23), self.NOTTE))
        self.assertTrue(in_fascia(ora(3), self.NOTTE))
        self.assertTrue(in_fascia(ora(22), self.NOTTE))
        self.assertFalse(in_fascia(ora(7), self.NOTTE))  # fine esclusa
        self.assertFalse(in_fascia(ora(12), self.NOTTE))

    def test_piu_fasce_nello_stesso_giorno(self):
        self.assertTrue(in_fascia(ora(10), self.GIORNO))
        self.assertFalse(in_fascia(ora(13, 30), self.GIORNO))
        self.assertTrue(in_fascia(ora(17, 59), self.GIORNO))

    def test_prossimo_inizio(self):
        self.assertEqual(prossimo_inizio(ora(12), self.NOTTE), ora(22))
        self.assertEqual(prossimo_inizio(ora(13, 30), self.GIORNO), ora(14, 30))
        self.assertEqual(prossimo_inizio(ora(19), self.GIORNO).day, 24)
        self.assertIsNone(prossimo_inizio(ora(19), []))


class FakeRun:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        out = self.outputs.pop(0) if self.outputs else ""
        return type("R", (), {"returncode": 0, "stdout": out, "stderr": ""})()


class AvvioWindowsTests(unittest.TestCase):
    def test_attivazione_registra_e_abilita_senza_eseguire_powershell_vero(self):
        run = FakeRun(["", "Ready"])
        self.assertTrue(avvio_windows.imposta_avvio_automatico(True, runner=run))
        script = run.calls[0][-1]
        self.assertIn("pannello.py", script)
        self.assertIn("--automatico", script)
        self.assertIn("-AtLogOn", script)
        self.assertIn("-RunLevel Limited", script)
        self.assertIn("Enable-ScheduledTask", script)

    def test_disattivazione(self):
        run = FakeRun(["", "Disabled"])
        self.assertFalse(avvio_windows.imposta_avvio_automatico(False, runner=run))
        self.assertIn("Disable-ScheduledTask", run.calls[0][-1])

    def test_attivita_assente(self):
        self.assertFalse(avvio_windows.stato_avvio_automatico(runner=FakeRun(["Assente"])))

    def test_errore_powershell(self):
        def run(args, **kwargs):
            return type("R", (), {"returncode": 1, "stdout": "", "stderr": "accesso negato"})()
        with self.assertRaisesRegex(RuntimeError, "accesso negato"):
            avvio_windows.stato_avvio_automatico(runner=run)


if __name__ == "__main__":
    unittest.main()
