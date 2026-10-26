"""Finestra Impostazioni del pannello (rotellina ⚙).

La logica che non dipende dalla grafica (tendine dipendenti, fasce, costruzione dei valori)
sta in funzioni pure testabili; il salvataggio passa sempre da orch.impostazioni.salva,
che valida tutto prima di scrivere.
"""

from pathlib import Path
import re
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
from orch import impostazioni as imp  # noqa: E402
from orch.catalogo import catalogo  # noqa: E402

STRUMENTI = ("codex", "claude", "antigravity")
NOMI = {"codex": "Codex (ChatGPT)", "claude": "Claude", "antigravity": "Antigravity"}
_ORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


# --- logica pura ---------------------------------------------------------------
def modelli_per(strumento, cat):
    return [m["id"] for m in cat.get(strumento, [])]


def livelli_per(strumento, modello, cat):
    for m in cat.get(strumento, []):
        if m["id"] == modello:
            return list(m["livelli"])
    return []


def livello_predefinito(strumento, modello, cat, attuale=None):
    livelli = livelli_per(strumento, modello, cat)
    if attuale in livelli:
        return attuale
    for m in cat.get(strumento, []):
        if m["id"] == modello and m.get("predefinito") in livelli:
            return m["predefinito"]
    return "medium" if "medium" in livelli else (livelli[0] if livelli else "")


def collegato(strumento, cat):
    """Antigravity (o uno strumento senza modelli nel catalogo) non è ancora collegato."""
    return bool(cat.get(strumento))


def fascia_valida(da, a):
    return bool(_ORA.match(da or "")) and bool(_ORA.match(a or "")) and da != a


def limite_da_testo(testo):
    """None = usa quanto serve; altrimenti intero 1–100 (ValueError se non valido)."""
    valore = int(str(testo).strip())
    if not 1 <= valore <= 100:
        raise ValueError("Il limite deve essere tra 1 e 100")
    return valore


def costruisci_valori(spegnimento, avvio_attivo, modalita, fasce, controllore, esecutori):
    return {
        "spegnimento_automatico": bool(spegnimento),
        "avvio_automatico": {"attivo": bool(avvio_attivo), "modalita": modalita,
                             "fasce": [{"da": da, "a": a} for da, a in fasce]},
        "controllore": dict(controllore),
        "esecutori": {nome: dict(valori) for nome, valori in esecutori.items()},
    }


# --- finestra ----------------------------------------------------------------
class FinestraImpostazioni:
    def __init__(self, parent, percorso=BASE / "impostazioni.json", al_salvataggio=None):
        self.percorso = Path(percorso)
        self.al_salvataggio = al_salvataggio
        self.cat = catalogo(BASE)
        try:
            self.valori = imp.carica(self.percorso)
        except imp.ImpostazioniError as exc:
            messagebox.showwarning("Impostazioni", f"Impostazioni non valide, uso i valori predefiniti:\n{exc}")
            self.valori = imp.predefinite()
        self.win = tk.Toplevel(parent)
        self.win.title("Impostazioni")
        self.win.transient(parent)
        self.win.resizable(False, False)
        f = ttk.Frame(self.win, padding=12)
        f.pack(fill="both", expand=True)
        self._spegnimento(f)
        self._avvio(f)
        self._controllore(f)
        self._esecutori(f)
        self._account(f)
        pulsanti = ttk.Frame(f)
        pulsanti.pack(fill="x", pady=(10, 0))
        ttk.Button(pulsanti, text="Annulla", command=self.win.destroy).pack(side="right")
        ttk.Button(pulsanti, text="Salva", command=self.salva).pack(side="right", padx=6)
        self.win.grab_set()

    # 1. spegnimento
    def _spegnimento(self, f):
        box = ttk.LabelFrame(f, text="Spegnimento", padding=8)
        box.pack(fill="x")
        self.v_spegni = tk.BooleanVar(value=self.valori["spegnimento_automatico"])
        ttk.Checkbutton(box, variable=self.v_spegni,
                        text="Spegni il PC a fine coda o con un limite settimanale (report finale prima)").pack(anchor="w")

    # 2. avvio automatico
    def _avvio(self, f):
        avvio = self.valori["avvio_automatico"]
        box = ttk.LabelFrame(f, text="Avvio automatico all'accensione del PC", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.v_avvio = tk.BooleanVar(value=avvio["attivo"])
        ttk.Checkbutton(box, variable=self.v_avvio, text="Apri il pannello e avvia le task all'accesso",
                        command=self._aggiorna_avvio).pack(anchor="w")
        self.v_modalita = tk.StringVar(value=avvio["modalita"])
        self.r_sempre = ttk.Radiobutton(box, text="Sempre, a qualsiasi ora", value="sempre",
                                        variable=self.v_modalita, command=self._aggiorna_avvio)
        self.r_fasce = ttk.Radiobutton(box, text="Solo nelle fasce orarie:", value="fasce",
                                       variable=self.v_modalita, command=self._aggiorna_avvio)
        self.r_sempre.pack(anchor="w", padx=18)
        self.r_fasce.pack(anchor="w", padx=18)
        riga = ttk.Frame(box)
        riga.pack(fill="x", padx=36)
        self.lista_fasce = tk.Listbox(riga, height=3, width=18, exportselection=False)
        self.lista_fasce.pack(side="left")
        for fascia in avvio["fasce"]:
            self.lista_fasce.insert("end", f"{fascia['da']} – {fascia['a']}")
        comandi = ttk.Frame(riga)
        comandi.pack(side="left", padx=8)
        self.e_da = ttk.Entry(comandi, width=6)
        self.e_a = ttk.Entry(comandi, width=6)
        self.e_da.insert(0, "22:00")
        self.e_a.insert(0, "07:00")
        ttk.Label(comandi, text="da").grid(row=0, column=0)
        self.e_da.grid(row=0, column=1)
        ttk.Label(comandi, text="a").grid(row=0, column=2, padx=(4, 0))
        self.e_a.grid(row=0, column=3)
        self.b_aggiungi = ttk.Button(comandi, text="Aggiungi", command=self._aggiungi_fascia)
        self.b_togli = ttk.Button(comandi, text="Togli selezionata", command=self._togli_fascia)
        self.b_aggiungi.grid(row=1, column=0, columnspan=2, sticky="we", pady=(4, 0))
        self.b_togli.grid(row=1, column=2, columnspan=2, sticky="we", pady=(4, 0), padx=(4, 0))
        self._aggiorna_avvio()

    def _aggiorna_avvio(self):
        attivo = self.v_avvio.get()
        fasce = attivo and self.v_modalita.get() == "fasce"
        for w in (self.r_sempre, self.r_fasce):
            w.state(["!disabled"] if attivo else ["disabled"])
        for w in (self.e_da, self.e_a, self.b_aggiungi, self.b_togli):
            w.state(["!disabled"] if fasce else ["disabled"])
        self.lista_fasce.config(state="normal" if fasce else "disabled")

    def _aggiungi_fascia(self):
        da, a = self.e_da.get().strip(), self.e_a.get().strip()
        if not fascia_valida(da, a):
            messagebox.showerror("Fascia oraria", "Scrivi gli orari come HH:MM (es. 22:00 e 07:00), diversi tra loro.",
                                 parent=self.win)
            return
        self.lista_fasce.insert("end", f"{da} – {a}")

    def _togli_fascia(self):
        for i in reversed(self.lista_fasce.curselection()):
            self.lista_fasce.delete(i)

    def _fasce(self):
        return [tuple(p.strip() for p in voce.split("–")) for voce in self.lista_fasce.get(0, "end")]

    # 3. controllore
    def _controllore(self, f):
        c = self.valori["controllore"]
        box = ttk.LabelFrame(f, text="Controllore (uno solo: rivede ogni pezzo)", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.c_strumento = ttk.Combobox(box, state="readonly", width=16, values=[NOMI[s] for s in STRUMENTI])
        self.c_modello = ttk.Combobox(box, state="readonly", width=22)
        self.c_livello = ttk.Combobox(box, state="readonly", width=10)
        for i, (testo, w) in enumerate((("Strumento", self.c_strumento), ("Modello", self.c_modello),
                                        ("Ragionamento", self.c_livello))):
            ttk.Label(box, text=testo).grid(row=0, column=i * 2, sticky="w", padx=(0 if i == 0 else 8, 4))
            w.grid(row=0, column=i * 2 + 1)
        self.c_nota = ttk.Label(box, text="", foreground="#a05a00")
        self.c_nota.grid(row=1, column=0, columnspan=6, sticky="w")
        self.c_strumento.set(NOMI[c["strumento"]])
        self._riempi(self.c_strumento, self.c_modello, self.c_livello, self.c_nota, c["modello"], c["ragionamento"])
        self.c_strumento.bind("<<ComboboxSelected>>", lambda e: self._riempi(
            self.c_strumento, self.c_modello, self.c_livello, self.c_nota))
        self.c_modello.bind("<<ComboboxSelected>>", lambda e: self._riempi_livelli(
            self._strumento(self.c_strumento), self.c_modello, self.c_livello))

    def _strumento(self, combo):
        return {v: k for k, v in NOMI.items()}[combo.get()]

    def _riempi(self, c_strumento, c_modello, c_livello, nota, modello=None, livello=None):
        strumento = self._strumento(c_strumento)
        modelli = modelli_per(strumento, self.cat)
        c_modello["values"] = modelli
        if not modelli:
            c_modello.set("da collegare")
            c_livello["values"] = []
            c_livello.set("")
            nota.config(text="Antigravity non è collegato: premi Accedi nella sezione Account, "
                             "poi chiudi e riapri le Impostazioni.")
            return
        nota.config(text="")
        c_modello.set(modello if modello in modelli else modelli[0])
        self._riempi_livelli(strumento, c_modello, c_livello, livello)

    def _riempi_livelli(self, strumento, c_modello, c_livello, livello=None):
        livelli = livelli_per(strumento, c_modello.get(), self.cat)
        c_livello["values"] = livelli
        if not livelli:  # Antigravity: il livello è già nel nome del modello
            c_livello.set("")
            c_livello.state(["disabled"])
            return
        c_livello.state(["!disabled"])
        c_livello.set(livello_predefinito(strumento, c_modello.get(), self.cat, livello or c_livello.get()))

    # 4. esecutori
    def _esecutori(self, f):
        box = ttk.LabelFrame(f, text="Esecutori (scrivono il codice; si passa al successivo quando uno finisce i crediti)",
                             padding=8)
        box.pack(fill="x", pady=(8, 0))
        for col, testo in enumerate(("Attivo", "Modello", "Ragionamento", "Priorità", "Limite sulle 5 ore (per sessione)")):
            ttk.Label(box, text=testo).grid(row=0, column=col, sticky="w", padx=4)
        self.righe = {}
        supporto = self.cat.get("limite_percentuale_supportato", {})
        for r, nome in enumerate(STRUMENTI, start=1):
            e = self.valori["esecutori"][nome]
            riga = {"attivo": tk.BooleanVar(value=e["attivo"] and collegato(nome, self.cat)),
                    "limite_on": tk.BooleanVar(value=e["limite_5h_percento"] is not None),
                    "limite": tk.StringVar(value=str(e["limite_5h_percento"] or 30))}
            chk = ttk.Checkbutton(box, text=NOMI[nome], variable=riga["attivo"])
            chk.grid(row=r, column=0, sticky="w", padx=4, pady=2)
            modello = ttk.Combobox(box, state="readonly", width=20, values=modelli_per(nome, self.cat))
            livello = ttk.Combobox(box, state="readonly", width=9)
            priorita = ttk.Combobox(box, state="readonly", width=3, values=["1", "2", "3"])
            priorita.set(str(e["priorita"]))
            modello.grid(row=r, column=1, padx=4)
            livello.grid(row=r, column=2, padx=4)
            priorita.grid(row=r, column=3, padx=4)
            riga.update(modello=modello, livello=livello, priorita=priorita)
            if collegato(nome, self.cat):
                modello.set(e["modello"] if e["modello"] in modelli_per(nome, self.cat) else modelli_per(nome, self.cat)[0])
                self._riempi_livelli(nome, modello, livello, e["ragionamento"])
                modello.bind("<<ComboboxSelected>>", lambda ev, n=nome, m=modello, l=livello: self._riempi_livelli(n, m, l))
            else:
                chk.state(["disabled"])
                modello.set("da collegare")
                modello.state(["disabled"])
                livello.state(["disabled"])
            limite_box = ttk.Frame(box)
            limite_box.grid(row=r, column=4, sticky="w", padx=4)
            if supporto.get(nome):
                ttk.Radiobutton(limite_box, text="Usa quanto serve", value=False, variable=riga["limite_on"]).pack(side="left")
                ttk.Radiobutton(limite_box, text="Limite", value=True, variable=riga["limite_on"]).pack(side="left", padx=(6, 0))
                ttk.Spinbox(limite_box, from_=1, to=100, width=4, textvariable=riga["limite"]).pack(side="left")
                ttk.Label(limite_box, text="%").pack(side="left")
            else:
                riga["limite_on"].set(False)
                ttk.Label(limite_box, text="solo attivo/disattivo (utilizzo non leggibile)").pack(side="left")
            self.righe[nome] = riga

    # 5. account
    def _account(self, f):
        from orch import account
        self.account = account
        box = ttk.LabelFrame(f, text="Account (accesso dei programmi usati dall'orchestratore)", padding=8)
        box.pack(fill="x", pady=(8, 0))
        self.righe_account = {}
        for r, nome in enumerate(STRUMENTI):
            ttk.Label(box, text=NOMI[nome], width=16).grid(row=r, column=0, sticky="w", pady=2)
            stato = ttk.Label(box, text="controllo…", width=58)
            stato.grid(row=r, column=1, sticky="w")
            pulsante = ttk.Button(box, text="…", width=12, state="disabled")
            pulsante.grid(row=r, column=2, padx=(6, 0))
            self.righe_account[nome] = (stato, pulsante)
        ttk.Button(box, text="Aggiorna stato", command=self._aggiorna_account).grid(
            row=len(STRUMENTI), column=2, pady=(6, 0), padx=(6, 0))
        ttk.Label(box, text="Accedi apre una finestra con il login del programma: completalo nel browser, "
                            "poi premi Aggiorna stato.", foreground="#666").grid(
            row=len(STRUMENTI), column=0, columnspan=2, sticky="w", pady=(6, 0))
        self._aggiorna_account()

    def _aggiorna_account(self):
        for stato, pulsante in self.righe_account.values():
            stato.config(text="controllo…")
            pulsante.config(text="…", state="disabled")

        def lavoro():
            risultati = {}
            for nome in STRUMENTI:
                try:
                    risultati[nome] = self.account.stato(nome)
                except Exception as exc:  # comando lento o assente: lo si mostra, non si chiude la finestra
                    risultati[nome] = exc
            try:
                self.win.after(0, lambda: self._mostra_account(risultati))
            except tk.TclError:
                pass  # finestra già chiusa

        threading.Thread(target=lavoro, daemon=True).start()

    def _mostra_account(self, risultati):
        for nome, valore in risultati.items():
            stato, pulsante = self.righe_account[nome]
            if isinstance(valore, Exception):
                stato.config(text=f"stato non leggibile: {valore}")
                pulsante.config(text="Accedi", state="normal", command=lambda n=nome: self._accedi(n))
                continue
            if valore.collegato is True:
                testo = "✔ " + valore.dettaglio
                if not collegato(nome, self.cat):  # appena collegato: le tendine si caricano alla riapertura
                    testo += " — riapri le Impostazioni per sceglierlo"
                stato.config(text=testo, foreground="#1b7a35")
                if valore.puo_uscire:
                    pulsante.config(text="Esci", state="normal", command=lambda n=nome: self._esci(n))
                else:  # Antigravity: l'uscita si fa dall'app
                    pulsante.config(text="Apri app", state="normal", command=lambda n=nome: self._apri_app(n))
            else:
                segno = "✖ " if valore.collegato is False else "• "
                stato.config(text=segno + valore.dettaglio, foreground="#a02020" if valore.collegato is False else "#a05a00")
                testo = "Accedi"
                pulsante.config(text=testo, state="normal" if valore.puo_accedere else "disabled",
                                command=lambda n=nome: self._accedi(n))

    def _accedi(self, nome):
        try:
            self.account.accedi(nome)
        except Exception as exc:
            messagebox.showerror("Accesso", f"Impossibile avviare l'accesso a {NOMI[nome]}: {exc}", parent=self.win)

    def _apri_app(self, nome):
        try:
            import subprocess
            subprocess.Popen([str(self.account.ANTIGRAVITY)], close_fds=True)
        except Exception as exc:
            messagebox.showerror("Antigravity", f"Impossibile aprire l'app: {exc}", parent=self.win)

    def _esci(self, nome):
        if not messagebox.askyesno("Esci", f"Vuoi davvero scollegare {NOMI[nome]}? "
                                   "L'orchestratore non potrà usarlo finché non accedi di nuovo.", parent=self.win):
            return
        try:
            self.account.esci(nome)
        except Exception as exc:
            messagebox.showerror("Esci", f"Logout di {NOMI[nome]} non riuscito: {exc}", parent=self.win)
        self._aggiorna_account()

    # salvataggio
    def salva(self):
        try:
            esecutori = {}
            for nome, riga in self.righe.items():
                limite = limite_da_testo(riga["limite"].get()) if riga["limite_on"].get() else None
                ok = collegato(nome, self.cat)
                esecutori[nome] = {"attivo": riga["attivo"].get() and ok,
                                   "modello": riga["modello"].get() if ok else "",
                                   "ragionamento": riga["livello"].get() if ok else "",
                                   "priorita": int(riga["priorita"].get()), "limite_5h_percento": limite}
            strumento = self._strumento(self.c_strumento)
            controllore = {"strumento": strumento,
                           "modello": self.c_modello.get() if collegato(strumento, self.cat) else "",
                           "ragionamento": self.c_livello.get() if collegato(strumento, self.cat) else ""}
            valori = costruisci_valori(self.v_spegni.get(), self.v_avvio.get(), self.v_modalita.get(),
                                       self._fasce(), controllore, esecutori)
            if strumento == "antigravity" and not collegato("antigravity", self.cat):
                raise ValueError("Antigravity non è collegato: accedi dalla sezione Account oppure scegli "
                                 "Claude o Codex come controllore.")
            imp.salva(self.percorso, valori)
        except (ValueError, imp.ImpostazioniError) as exc:
            messagebox.showerror("Impostazioni non salvate", str(exc), parent=self.win)
            return
        avviso = ""
        if valori["avvio_automatico"]["attivo"] != self.valori["avvio_automatico"]["attivo"]:
            try:
                from orch.avvio_windows import imposta_avvio_automatico
                imposta_avvio_automatico(valori["avvio_automatico"]["attivo"])
            except Exception as exc:  # l'impostazione resta salvata; si segnala il problema
                avviso = f"\n\nAttenzione: attività di Windows non aggiornata: {exc}"
        if self.al_salvataggio:
            self.al_salvataggio()
        messagebox.showinfo("Impostazioni", "Impostazioni salvate. Valgono dal prossimo passaggio." + avviso,
                            parent=self.win)
        self.win.destroy()
