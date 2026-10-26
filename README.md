# Orchestratore
**Un pannello desktop per coordinare agenti AI e trasformare una coda di task in lavoro verificato.**
Progetto di **Filippo Bello**. Sviluppato in Python per Windows, con interfaccia Tkinter, persistenza dello stato e integrazioni con CLI di sviluppo assistito da AI e un modello locale tramite Ollama.
## Il problema
Quando più strumenti AI partecipano a un progetto, occorre tenere insieme istruzioni, avanzamento, verifiche, revisioni e limiti d'uso. Orchestratore gestisce questo flusso attraverso task Markdown: suddivide il lavoro in passaggi, coordina gli esecutori, esegue i comandi di verifica e richiede una revisione prima del commit.
## Funzionalità
- Coda di task con priorità, obiettivi, passaggi e verifiche esplicite.
- Esecutori configurabili: Codex, Claude Code e Antigravity; controllore selezionabile.
- Modello locale tramite Ollama per le decisioni di avanzamento e il report finale.
- Pannello Start/Stop, monitor dell'attività e cronologia delle sessioni.
- Salvataggio dello stato e ripresa dopo un'interruzione.
- Gestione dei limiti d'uso e passaggio fra esecutori secondo priorità e budget configurati.
- Verifiche eseguite dal programma e revisione del codice prima del commit.
- Report giornalieri, report di fine sessione e indicazione degli script SQL da verificare manualmente.
- Avvio programmato e spegnimento opzionale su Windows.
```text
Task Markdown -> Esecutore AI -> Verifiche -> Decisione locale
                                  ^               |
                                  |          correzione o revisione
                                  +---------------+
                                                  |
                                     Controllore -> Commit -> Report
```
## Struttura
| Percorso | Responsabilità |
| --- | --- |
| `orchestratore.py` | Comandi CLI e avvio del ciclo |
| `pannello.py`, `monitor.py`, `impostazioni_gui.py` | Interfaccia desktop |
| `orch/ciclo.py` | Coordinamento e transizioni delle task |
| `orch/esecutori.py`, `orch/controllori.py` | Selezione degli strumenti |
| `orch/codex.py`, `orch/claude.py`, `orch/antigravity.py` | Adattatori delle CLI |
| `orch/persistence.py`, `orch/reporting.py` | Stato e report |
| `regole/` | Istruzioni degli agenti e del modello locale |
| `tests/` | Test automatici, inclusi scenari con strumenti simulati |
| `examples/001-esempio.md` | Esempio di task da adattare |
## Requisiti
Il progetto è pensato per **Windows e Python 3.12 con Tkinter**, Git, Ollama e le CLI degli esecutori che si vogliono utilizzare. Il codice Python usa la libreria standard. I programmi esterni richiedono installazione, autenticazione e accesso ai rispettivi modelli.
La configurazione include identificatori di modelli usati durante lo sviluppo. Verificare nel proprio account che siano disponibili e modificarli dalle impostazioni quando necessario. L'integrazione dipende dai comandi e dai formati di risposta delle CLI; aggiornamenti esterni possono richiedere adeguamenti.
## Avvio
1. Scaricare o clonare questo repository.
2. Installare Python 3.12 con Tkinter e Git. Preparare le CLI selezionate e completare i loro normali login.
3. Preparare Ollama e il modello configurato, predefinito `qwen3:14b`. Dimensionare il modello in base alla propria macchina.
4. Controllare `config.json` e `impostazioni.json`. Il programma cerca `codex.cmd` e `claude.cmd` tramite PATH; il pannello prevede anche i percorsi di installazione per utente di Windows. Se l'installazione è diversa, adattare la configurazione e i riferimenti nel pannello.
5. Aprire il pannello:
```powershell
py -3.12 pannello.py
```
Per controllare lo stato senza avviare gli agenti:
```powershell
py -3.12 orchestratore.py --stato
```
Per elaborare la coda una sola volta:
```powershell
py -3.12 orchestratore.py --una-volta --non-spegnere
```
Nella configurazione pubblicata, **push, avvio automatico e spegnimento sono disattivati**. Sono inoltre rifiutate le modifiche preesistenti nel repository di destinazione. I percorsi della configurazione sono relativi alla cartella del progetto o ricavati dall'ambiente Python e dal profilo Windows dell'utente.
## Prima task
Adattare `examples/001-esempio.md` al proprio repository e copiarlo in `coda/`. Indicare un percorso reale, il branch atteso, l'obiettivo, i passaggi e comandi di verifica appropriati. Avviare poi il ciclo dal pannello o dalla CLI.
Il repository di destinazione deve trovarsi sul branch indicato. La configurazione pubblica richiede una working tree pulita. Dopo una revisione positiva, Orchestratore può creare commit; il push richiede sia l'abilitazione in configurazione sia una task che lo richieda. Le task scartate dopo modifiche possono conservare il lavoro in uno stash.
I comandi nella sezione `Verifica` vengono eseguiti sulla macchina: usare soltanto task di cui si conoscono origine e contenuto. Le istruzioni agli agenti limitano operazioni come modifiche ai file `.env`, installazioni e query verso database, ma non costituiscono una garanzia contro ogni comportamento degli strumenti esterni.
## Test
```powershell
py -3.12 -m unittest discover -s tests -v
```
La suite copre configurazione, task, persistenza, limiti d'uso, budget, monitoraggio, revisione, operazioni Git e scenari di esecuzione con strumenti finti. I test end-to-end richiedono Git e usano repository temporanei: non chiamano servizi AI reali. Questi test non certificano il comportamento di tutte le versioni delle CLI o dei modelli esterni.
## Dati locali
La pubblicazione contiene codice, test, istruzioni ed esempi. Non contiene cronologia Git preesistente, task personali, log, conversazioni, report operativi, stato delle sessioni, file `.env` o credenziali.
Durante l'uso, log e report possono includere contenuti delle task e degli strumenti: conservarli localmente e controllarli prima di condividerli. Le variabili `OPENAI_API_KEY` e `ANTHROPIC_API_KEY` vengono rimosse dall'ambiente dei processi figli gestiti dagli adattatori; nel codice compaiono i nomi delle variabili, senza valori reali. I processi CLI usano le proprie modalità di autenticazione.
## Autore
**Filippo Bello** · [LinkedIn](https://www.linkedin.com/in/filippo-bello-799315222/)
Portfolio: orchestrazione di agenti AI, automazione dei flussi di sviluppo, Python e progettazione di interfacce desktop.
