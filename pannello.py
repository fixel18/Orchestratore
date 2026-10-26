"""Pannello dell'orchestratore: avvio dei servizi, Start/Stop, avanzamento, task in coda e timer.

- All'apertura controlla Codex CLI e Claude CLI e avvia il modello locale (Ollama) se è spento.
- Start: toglie un eventuale file STOP e avvia l'orchestratore in una finestra propria,
  indipendente dal pannello (chiudere il pannello NON ferma il lavoro).
- Stop: crea il file STOP; l'orchestratore si ferma al primo punto sicuro, senza lasciare
  lavoro a metà, e con Start riprende da dove era.
"""

from datetime import datetime
import ctypes
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import ttk
import urllib.request

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import monitor  # noqa: E402  calcolo dell'avanzamento, condiviso con la finestra semplice
from orch.history_view import create_history, update_history

AVVIO_ORCHESTRATORE = BASE / "avvia-orchestratore.cmd"
OLLAMA_APP = Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama app.exe"
CODEX = Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"
CLAUDE = Path.home() / "AppData" / "Roaming" / "npm" / "claude.cmd"
ICONA = BASE / "orchestratore.ico"
AGGIORNA_MS = 2000
PRIORITA = {"alta": 0, "media": 1, "bassa": 2}


def ollama_attivo():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/version", timeout=2) as risposta:
            return risposta.status == 200
    except OSError:
        return False


def avvia_staccato(comando):
    """Processo con una console propria, che sopravvive alla chiusura del pannello."""
    flag = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 7  # SW_SHOWMINNOACTIVE: parte ridotta a icona
    subprocess.Popen(comando, cwd=str(BASE), creationflags=flag, startupinfo=info, close_fds=True)


def task_non_eseguite():
    """In corso per prima, poi la coda nell'ordine in cui l'orchestratore la prende."""
    elenco = []
    stato = monitor.leggi_json(BASE / "stato.json", {})
    corrente = stato.get("task_corrente")
    if corrente:
        elenco.append(f"▶ {corrente.get('id')} — {corrente.get('titolo')}  (in corso)")
    voci = []
    try:
        from orch.tasks import scan_queue
        valide, errori = scan_queue(BASE / "coda")
        for t in valide:
            voci.append(((PRIORITA.get(t.priorita, 9), t.file.name.casefold()),
                         f"{t.id} — {t.titolo}  ({t.priorita})"))
        for percorso, motivo in errori:
            voci.append(((9, Path(percorso).name.casefold()), f"⚠ {Path(percorso).name}: fascicolo non valido"))
    except Exception:
        for p in sorted((BASE / "coda").glob("*.md")):
            voci.append(((9, p.name.casefold()), p.stem))
    elenco.extend(testo for _, testo in sorted(voci))
    return elenco


def righe_risorse(stato, percorso_impostazioni=None):
    """Controllore ed esecutori attivi con il budget usato in questa sessione."""
    impostazioni = monitor.leggi_json(percorso_impostazioni or BASE / "impostazioni.json", {})
    if not impostazioni:
        return ""
    budget = (stato.get("periodo") or {}).get("budget", {})
    utilizzo = stato.get("utilizzo_esecutori", {})
    c = impostazioni.get("controllore", {})
    righe = [f"Controllore: {c.get('strumento', '?')} {c.get('modello', '')} ({c.get('ragionamento', '')})"]
    esecutori = sorted(((n, e) for n, e in impostazioni.get("esecutori", {}).items() if e.get("attivo")),
                       key=lambda x: x[1].get("priorita", 9))
    for nome, e in esecutori:
        consumato = budget.get(nome, {}).get("consumato", 0)
        limite = e.get("limite_5h_percento")
        testo = f"{e.get('priorita')}. {nome} {e.get('modello', '')} ({e.get('ragionamento', '')}): "
        testo += f"usato {consumato:.0f}% di {limite}% in questa sessione" if limite else "senza limite"
        finestra = (utilizzo.get(nome) or {}).get("finestra_5h")
        if finestra is not None:
            testo += f" · finestra 5 ore al {float(finestra):.0f}%"
        righe.append(testo)
    return "\n".join(righe)


def durata(secondi):
    secondi = max(0, int(secondi))
    giorni, resto = divmod(secondi, 86400)
    ore, resto = divmod(resto, 3600)
    minuti, sec = divmod(resto, 60)
    testo = f"{ore:02d}:{minuti:02d}:{sec:02d}"
    return f"{giorni} g {testo}" if giorni else testo


class Pannello:
    def __init__(self, root):
        self.root = root
        self.arresto_richiesto = False
        root.title("Orchestratore")
        root.geometry("760x650")
        root.minsize(460, 420)
        if ICONA.exists():
            try:
                root.iconbitmap(str(ICONA))
            except tk.TclError:
                pass
        stile = ttk.Style(root)
        stile.configure("Titolo.TLabel", font=("Segoe UI", 16, "bold"))
        stile.configure("Grande.TLabel", font=("Segoe UI", 14, "bold"))
        stile.configure("Testo.TLabel", font=("Segoe UI", 10))
        stile.configure("Timer.TLabel", font=("Consolas", 13, "bold"))
        stile.configure("Start.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 6))
        stile.configure("Stop.TButton", font=("Segoe UI", 11, "bold"), padding=(18, 6))
        stile.configure("Barra.Horizontal.TProgressbar", thickness=22)

        f = ttk.Frame(root, padding=14)
        f.pack(fill="both", expand=True)

        testa = ttk.Frame(f)
        testa.pack(fill="x")
        ttk.Label(testa, text="Orchestratore", style="Titolo.TLabel").pack(side="left")
        ttk.Button(testa, text="⚙", width=3, command=self.apri_impostazioni).pack(side="right", padx=(8, 0))
        self.timer = ttk.Label(testa, text="", style="Timer.TLabel")
        self.timer.pack(side="right")

        self.servizi = ttk.Label(f, text="Controllo i servizi…", style="Testo.TLabel")
        self.servizi.pack(anchor="w", pady=(4, 8))

        comandi = ttk.Frame(f)
        comandi.pack(fill="x")
        self.start = ttk.Button(comandi, text="▶  Start", style="Start.TButton", command=self.premi_start)
        self.start.pack(side="left")
        self.stop = ttk.Button(comandi, text="■  Stop", style="Stop.TButton", command=self.premi_stop)
        self.stop.pack(side="left", padx=8)
        self.menu_task = tk.Menubutton(comandi, text="Visualizza task ▾", relief="raised", padx=10, pady=6,
                                       font=("Segoe UI", 10))
        self.menu = tk.Menu(self.menu_task, tearoff=False, postcommand=self.riempi_menu)
        self.menu_task["menu"] = self.menu
        self.menu_task.pack(side="right")

        self.stato_testo = ttk.Label(f, text="", style="Testo.TLabel", wraplength=700, justify="left")
        self.stato_testo.pack(anchor="w", pady=(10, 4))

        # Sezione di avanzamento: compare quando l'orchestratore è in esecuzione.
        self.avanzamento = ttk.Frame(f)
        self.percentuale = ttk.Label(self.avanzamento, text="", style="Grande.TLabel")
        self.percentuale.pack(anchor="w")
        self.barra = ttk.Progressbar(self.avanzamento, style="Barra.Horizontal.TProgressbar", maximum=100)
        self.barra.pack(fill="x", pady=(4, 4))
        self.conteggio = ttk.Label(self.avanzamento, text="", style="Testo.TLabel")
        self.conteggio.pack(anchor="w")
        self.corrente = ttk.Label(self.avanzamento, text="", style="Testo.TLabel", wraplength=700, justify="left")
        self.corrente.pack(anchor="w", pady=(6, 0))
        self.fase = ttk.Label(self.avanzamento, text="", style="Testo.TLabel", wraplength=700, justify="left")
        self.fase.pack(anchor="w")
        self.risorse = ttk.Label(self.avanzamento, text="", style="Testo.TLabel", wraplength=700, justify="left")
        self.risorse.pack(anchor="w", pady=(4, 0))

        # Sezione storico e attività: espandibile, sfrutta tutta l'altezza rimanente
        self.storico = ttk.Frame(f)
        self.attivita = ttk.Label(self.storico, text="", style="Testo.TLabel", wraplength=700, justify="left")
        self.attivita.pack(anchor="w", pady=(8, 2))
        ttk.Label(self.storico, text="Storico della sessione", style="Testo.TLabel").pack(anchor="w", pady=(4, 2))
        self.registro = create_history(self.storico, root.cget("background"))

        ttk.Label(f, text="Chiudere questa finestra non ferma l'orchestratore: per fermarlo usa Stop.",
                  style="Testo.TLabel", foreground="#666").pack(side="bottom", anchor="w", pady=(6, 0))

        root.after(200, self.prepara_servizi)
        self.aggiorna()

    # --- servizi --------------------------------------------------------------
    def prepara_servizi(self):
        self.servizi_ok = {"codex": CODEX.exists(), "claude": CLAUDE.exists(), "ollama": ollama_attivo()}
        if not self.servizi_ok["ollama"] and OLLAMA_APP.exists():
            avvia_staccato([str(OLLAMA_APP)])
            self.attese_ollama = 0
            self.root.after(1500, self.attendi_ollama)
        self.mostra_servizi()

    def attendi_ollama(self):
        self.servizi_ok["ollama"] = ollama_attivo()
        self.attese_ollama += 1
        if not self.servizi_ok["ollama"] and self.attese_ollama < 40:
            self.root.after(1500, self.attendi_ollama)
        self.mostra_servizi()

    def mostra_servizi(self):
        s = self.servizi_ok
        segno = lambda ok: "✔" if ok else "✖"
        testo = (f"{segno(s['ollama'])} Modello locale (Ollama)   {segno(s['codex'])} Codex CLI   "
                 f"{segno(s['claude'])} Claude CLI")
        if not s["ollama"]:
            testo += "   — avvio del modello in corso…" if getattr(self, "attese_ollama", 40) < 40 else "   — modello non raggiungibile"
        self.servizi.config(text=testo)

    # --- comandi ---------------------------------------------------------------
    def in_esecuzione(self):
        lock = monitor.leggi_json(BASE / "orchestratore.lock", None)
        return bool(lock) and monitor.processo_vivo(lock.get("pid", 0))

    def apri_impostazioni(self):
        from impostazioni_gui import FinestraImpostazioni
        FinestraImpostazioni(self.root)

    def premi_start(self, automatico=False):
        if self.in_esecuzione():
            return
        stop = BASE / "STOP"
        if stop.exists():
            stop.unlink()
        self.arresto_richiesto = False
        comando = ["cmd.exe", "/c", str(AVVIO_ORCHESTRATORE)] + (["--automatico"] if automatico else [])
        avvia_staccato(comando)
        self.stato_testo.config(text="Avvio dell'orchestratore…")

    def premi_stop(self):
        if not self.in_esecuzione():
            return
        (BASE / "STOP").touch()
        self.arresto_richiesto = True

    def riempi_menu(self):
        self.menu.delete(0, "end")
        voci = task_non_eseguite()
        if not voci:
            self.menu.add_command(label="Nessuna task da eseguire", state="disabled")
        for i, voce in enumerate(voci, 1):
            self.menu.add_command(label=f"{i}. {voce}", state="disabled" if voce.startswith("▶") else "normal")

    # --- aggiornamento ---------------------------------------------------------
    def aggiorna(self):
        try:
            self.mostra()
        except Exception as exc:  # un file letto a metà non deve chiudere il pannello
            self.stato_testo.config(text=f"Lettura non riuscita, riprovo: {exc}")
        self.root.after(AGGIORNA_MS, self.aggiorna)

    def mostra(self):
        attivo = self.in_esecuzione()
        stop_presente = (BASE / "STOP").exists()
        self.start.state(["disabled"] if attivo else ["!disabled"])
        self.stop.state(["!disabled"] if attivo and not stop_presente else ["disabled"])

        if attivo:
            try:
                partito = datetime.fromtimestamp((BASE / "orchestratore.lock").stat().st_mtime)
                self.timer.config(text="⏱ " + durata((datetime.now() - partito).total_seconds()))
            except OSError:
                self.timer.config(text="")
            if stop_presente:
                testo = "⏸ Arresto richiesto: l'orchestratore finisce il passaggio in corso e si ferma."
            else:
                testo = "● In esecuzione"
            if not self.avanzamento.winfo_ismapped():
                if self.storico.winfo_ismapped():
                    self.avanzamento.pack(fill="x", before=self.storico)
                else:
                    self.avanzamento.pack(fill="x")
        else:
            self.timer.config(text="")
            if self.arresto_richiesto or stop_presente:
                testo = "■ Fermo. Premi Start per riprendere da dove si era fermato."
            else:
                testo = "■ Fermo. Premi Start per iniziare le task in coda."
            if self.avanzamento.winfo_ismapped():
                self.avanzamento.pack_forget()

        d = monitor.calcola()
        stato = d["stato"]
        self.stato_testo.config(text=f"{testo}   ·   da eseguire: {len(d['in_coda']) + (1 if d['corrente'] else 0)}")

        if attivo:
            self.barra["value"] = d["percentuale"]
            self.percentuale.config(text=f"{d['percentuale']:.0f}% completato")
            numero = len(d["concluse"]) + (1 if d["corrente"] else 0)
            if d["corrente"]:
                riga = f"Svolgendo la task {numero} di {d['totale']}"
            elif d["totale"]:
                riga = f"Task completate: {numero} di {d['totale']}"
            else:
                riga = "Nessuna task in questa sessione"
            self.conteggio.config(text=riga + f"  ·  fatte {d['fatte']}, scartate {d['scartate']}, in coda {len(d['in_coda'])}")
            if d["corrente"]:
                t = d["corrente"]
                self.corrente.config(text=f"In corso: {t.get('id')} — {t.get('titolo')}\n"
                                          f"Pezzo {stato.get('pezzo_corrente', '?')} di {len(t.get('pezzi') or [])}"
                                          f"  ·  rimandi {stato.get('rimandi_pezzo', 0)}"
                                          f"  ·  correzioni Claude {stato.get('bocciature', 0)}")
            else:
                self.corrente.config(text="Nessuna task in corso.")
            fase = monitor.descrivi_fase(stato, vivo=d["vivo"])
            if stato.get("fase") == "attesa_limite":
                fase += f" ({stato.get('strumento_attesa', '?')}) fino alle {monitor.orario(stato.get('attesa_fino_a'))}"
            self.fase.config(text=f"Fase: {fase}")
            self.risorse.config(text=righe_risorse(stato))

        self.attivita.config(text=d.get("attivita", ""))
        eventi = d.get("eventi") or []
        if attivo or eventi:
            if not self.storico.winfo_ismapped():
                self.storico.pack(fill="both", expand=True)
            update_history(self.registro, eventi)
        else:
            if self.storico.winfo_ismapped():
                self.storico.pack_forget()


if __name__ == "__main__":
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "OrchestratorePannello")
    if ctypes.windll.kernel32.GetLastError() == 183:  # già aperto
        raise SystemExit(0)
    radice = tk.Tk()
    pannello = Pannello(radice)
    if "--automatico" in sys.argv[1:]:
        # Aperto dall'attività di Windows all'accesso: parte solo se l'avvio automatico è attivo;
        # le fasce orarie le rispetta l'orchestratore stesso (--automatico).
        avvio = monitor.leggi_json(BASE / "impostazioni.json", {}).get("avvio_automatico", {})
        if avvio.get("attivo"):
            radice.after(4000, lambda: pannello.premi_start(automatico=True))
    radice.mainloop()
