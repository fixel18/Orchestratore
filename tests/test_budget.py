import unittest

from orch.budget import aggiorna_budget


def oss(percento, resets):
    return {"finestra_5h": percento, "resets_5h": resets, "settimana": None, "resets_settimana": None}


class BudgetTests(unittest.TestCase):
    def test_esempio_di_filippo_limite_30_partendo_da_80(self):
        # Claude era già all'80% per uso personale: la prima osservazione fissa solo la base.
        voce = {}
        aggiorna_budget(voce, oss(80, "w1"))
        self.assertEqual(voce["consumato"], 0)
        aggiorna_budget(voce, oss(100, "w1"))      # usa il 20% rimasto
        self.assertEqual(voce["consumato"], 20)
        aggiorna_budget(voce, oss(10, "w2"))       # nuova finestra di 5 ore: altro 10%
        self.assertEqual(voce["consumato"], 30)
        self.assertGreaterEqual(voce["consumato"], 30)  # limite 30% raggiunto: niente nuovi pezzi

    def test_stessa_finestra_conta_solo_gli_aumenti(self):
        voce = {}
        for valore in (10, 25, 25, 40):
            aggiorna_budget(voce, oss(valore, "w"))
        self.assertEqual(voce["consumato"], 30)

    def test_utilizzo_non_leggibile_segnalato_una_volta_per_finestra(self):
        voce = {}
        self.assertTrue(aggiorna_budget(voce, oss(None, "w")))
        self.assertFalse(aggiorna_budget(voce, oss(None, "w")))
        self.assertTrue(aggiorna_budget(voce, oss(None, "w2")))
        self.assertEqual(voce["consumato"], 0)

    def test_il_controllore_sposta_solo_la_base(self):
        voce = {}
        aggiorna_budget(voce, oss(20, "w"))
        aggiorna_budget(voce, oss(35, "w"), controllore=True)  # revisione con lo stesso strumento
        aggiorna_budget(voce, oss(40, "w"))
        self.assertEqual(voce["consumato"], 5)


if __name__ == "__main__":
    unittest.main()
