"""Finestra di avanzamento dell'orchestratore (sola lettura).

Ogni pochi secondi rilegge stato.json, la coda e lo storico della sessione: se nel
frattempo si aggiungono task in coda, totale e percentuale si ricalcolano
subito. Non scrive nulla e non comanda l'orchestratore.
"""

from datetime import datetime
import ctypes
import json
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from orch.live_activity import ActivityReader
from orch.history_view import create_history, update_history

BASE = Path(__file__).resolve().parent
AGGIORNA_MS = 3000
_activity_reader = ActivityReader()
FASI = {
    "idle": "in attesa di task", "preparazione": "preparazione", "controlli_iniziali": "controlli iniziali",
    "invio_codex": "Codex sta lavorando", "verifica": "verifiche (test)", "modello": "il modello locale decide",
    "claude": "Claude sta revisionando", "commit": "commit", "push": "push su GitHub", "scarto": "scarto",
    "chiusura": "chiusura della task", "attesa_limite": "in attesa dello sblocco dei crediti",
}


def leggi_json(path, predefinito):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return predefinito


def processo_vivo(pid):
    try:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    except (AttributeError, ValueError, TypeError):
        return False
    if not handle:
        return False
    codice = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(codice))
    ctypes.windll.kernel32.CloseHandle(handle)
    return bool(ok) and codice.value == 259  # STILL_ACTIVE


def orario(testo):
    try:
        return datetime.fromisoformat(testo).astimezone().strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return "?"


def descrivi_fase(stato, vivo=None):
    if vivo is False:
        return "in pausa" if stato.get("task_corrente") else "in attesa di task"
    if stato.get("fuori_fascia_dal"):
        return "fuori dalle fasce orarie: in pausa"
    fase = stato.get("fase", "idle")
    if fase == "invio_codex":
        return f"{str(stato.get('esecutore_corrente') or 'codex').capitalize()} sta lavorando"
    if fase == "claude":
        tool = stato.get("azione_esterna")
        if tool not in {"codex", "claude", "antigravity"}:
            tool = stato.get("controllore_corrente") or "controllore"
        return f"{str(tool).capitalize()} sta revisionando"
    return FASI.get(fase, fase)


def calcola():
    stato = leggi_json(BASE / "stato.json", {})
    periodo = stato.get("periodo") or {}
    task_periodo = periodo.get("task") or {}
    concluse = [t for t in task_periodo.values() if t.get("esito") in ("fatta", "scartata")]
    corrente = stato.get("task_corrente")
    if corrente and corrente.get("id") in {t.get("id") for t in concluse}:
        corrente = None
    in_coda = sorted(p.name for p in (BASE / "coda").glob("*.md"))
    totale = len(concluse) + (1 if corrente else 0) + len(in_coda)

    frazione = 0.0
    if corrente:
        pezzi = len(corrente.get("pezzi") or []) or 1
        frazione = min(stato.get("pezzi_completati", 0), pezzi) / pezzi
    percentuale = 100.0 * (len(concluse) + frazione) / totale if totale else 100.0

    lock = leggi_json(BASE / "orchestratore.lock", None)
    vivo = bool(lock) and processo_vivo(lock.get("pid", 0))

    eventi, attivita, storico = _activity_reader.read(BASE, stato)

    return {
        "stato": stato, "concluse": concluse, "corrente": corrente, "in_coda": in_coda,
        "totale": totale, "percentuale": percentuale, "vivo": vivo,
        "stop": (BASE / "STOP").exists(), "eventi": eventi,
        "attivita": attivita if vivo and not stato.get("fuori_fascia_dal") else "",
        "fatte": sum(1 for t in concluse if t.get("esito") == "fatta"),
        "scartate": sum(1 for t in concluse if t.get("esito") == "scartata"),
        "inizio_periodo": storico.get("inizio") or periodo.get("inizio"),
    }


class Finestra:
    def __init__(self, root):
        self.root = root
        root.title("Orchestratore — avanzamento")
        root.geometry("760x650")
        root.minsize(420, 320)
        stile = ttk.Style(root)
        stile.configure("Grande.TLabel", font=("Segoe UI", 15, "bold"))
        stile.configure("Medio.TLabel", font=("Segoe UI", 10))
        stile.configure("Barra.Horizontal.TProgressbar", thickness=22)

        cornice = ttk.Frame(root, padding=14)
        cornice.pack(fill="both", expand=True)
        self.percentuale = ttk.Label(cornice, text="—", style="Grande.TLabel")
        self.percentuale.pack(anchor="w")
        self.barra = ttk.Progressbar(cornice, style="Barra.Horizontal.TProgressbar", maximum=100)
        self.barra.pack(fill="x", pady=(6, 4))
        self.conteggio = ttk.Label(cornice, text="", style="Medio.TLabel")
        self.conteggio.pack(anchor="w")
        self.corrente = ttk.Label(cornice, text="", style="Medio.TLabel", wraplength=520, justify="left")
        self.corrente.pack(anchor="w", pady=(8, 0))
        self.fase = ttk.Label(cornice, text="", style="Medio.TLabel", wraplength=520, justify="left")
        self.fase.pack(anchor="w")
        self.salute = ttk.Label(cornice, text="", style="Medio.TLabel")
        self.salute.pack(anchor="w", pady=(2, 8))

        self.attivita = ttk.Label(cornice, text="", style="Medio.TLabel", wraplength=520, justify="left")
        self.attivita.pack(anchor="w", pady=(0, 8))
        ttk.Label(cornice, text="Storico della sessione", style="Medio.TLabel").pack(anchor="w")
        self.registro = create_history(cornice, root.cget("background"))
        self.aggiorna()

    def aggiorna(self):
        try:
            dati = calcola()
            self.mostra(dati)
        except Exception as exc:  # la finestra non deve mai chiudersi per un file letto a metà
            self.salute.config(text=f"Lettura non riuscita, riprovo: {exc}")
        self.root.after(AGGIORNA_MS, self.aggiorna)

    def mostra(self, d):
        stato = d["stato"]
        self.barra["value"] = d["percentuale"]
        self.percentuale.config(text=f"{d['percentuale']:.0f}% completato")
        numero = len(d["concluse"]) + (1 if d["corrente"] else 0)
        if d["corrente"]:
            testo = f"Svolgendo la task {numero} di {d['totale']}"
        elif d["totale"]:
            testo = f"Task completate: {numero} di {d['totale']}"
        else:
            testo = "Nessuna task in questa sessione"
        dettagli = f"  ·  fatte {d['fatte']}, scartate {d['scartate']}, in coda {len(d['in_coda'])}"
        if d["inizio_periodo"]:
            dettagli += f"  ·  sessione dal {orario(d['inizio_periodo'])}"
        self.conteggio.config(text=testo + dettagli)

        if d["corrente"]:
            t = d["corrente"]
            pezzi = len(t.get("pezzi") or [])
            self.corrente.config(text=f"In corso: {t.get('id')} — {t.get('titolo')}\n"
                                      f"Pezzo {stato.get('pezzo_corrente', '?')} di {pezzi}"
                                      f"  ·  rimandi {stato.get('rimandi_pezzo', 0)}"
                                      f"  ·  correzioni Claude {stato.get('bocciature', 0)}")
        else:
            prossima = d["in_coda"][0] if d["in_coda"] else "nessuna"
            self.corrente.config(text=f"Nessuna task in corso. Prossima in coda: {prossima}")

        fase = descrivi_fase(stato, vivo=d["vivo"])
        if stato.get("fase") == "attesa_limite":
            fase += f" ({stato.get('strumento_attesa', '?')}) fino alle {orario(stato.get('attesa_fino_a'))}"
        self.fase.config(text=f"Fase: {fase}")
        self.attivita.config(text=d.get("attivita", ""))

        if d["stop"]:
            salute = "⏸ File STOP presente: l'orchestratore si ferma al primo punto sicuro."
        elif d["vivo"]:
            salute = "● Orchestratore attivo"
        else:
            salute = "○ Orchestratore NON attivo — avvialo con avvia-orchestratore.cmd"
        self.salute.config(text=salute + f"   ·   aggiornato alle {datetime.now():%H:%M:%S}")

        update_history(self.registro, d["eventi"])


if __name__ == "__main__":
    # Una sola finestra alla volta: un secondo avvio esce subito.
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "OrchestratoreMonitorAvanzamento")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        raise SystemExit(0)
    radice = tk.Tk()
    Finestra(radice)
    radice.mainloop()
