"""Ciclo persistente con attori, git, clock e sleep iniettabili."""

from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from . import claude, codex, controllori, esecutori, model, review, verification
from .budget import aggiorna_budget
from .fasce import in_fascia, prossimo_inizio
from .claude_limits import is_authentication_error, is_weekly_limit as claude_weekly
from .codex_limits import UsageLimit, reset_from_rollout, reset_from_text, is_weekly_limit as codex_weekly
from .config import Config
from .final_report import period_data, render_report
from .git_ops import GitError, GitOperations
from .impostazioni import carica as carica_impostazioni
from .persistence import StateError, StateStore, atomic_write
from .queries import extract_query_notes
from .reporting import ReportStore, TaskLogger
from .tasks import Task, load_task, scan_queue


PHASES = frozenset({"idle", "preparazione", "controlli_iniziali", "invio_codex", "verifica",
                    "modello", "claude", "commit", "push", "scarto", "chiusura", "attesa_limite"})


class StopRequested(Exception):
    """STOP osservato tra due azioni esterne, senza perdere la fase corrente."""


def local_now():
    return datetime.now().astimezone()


def codex_reset(config, thread_id, text, now):
    return (reset_from_rollout(config.cartella_sessioni_codex, thread_id, now=now)
            or reset_from_text(text, now=now, recent_hours=config.soglia_limite_settimanale_ore))


def shutdown_pc(config):
    subprocess.run(config.comando_spegnimento, shell=False, check=True)


@dataclass
class Services:
    run_codex: Callable = codex.run_codex
    run_claude: Callable = claude.run_claude
    decide_next_action: Callable = model.decide_next_action
    run_verifications: Callable = verification.run_verifications
    list_changed_files: Callable = verification.list_changed_files
    collect_review_diff: Callable = review.collect_review_diff
    build_claude_prompt: Callable = review.build_claude_prompt
    codex_reset: Callable = codex_reset
    clock: Callable = local_now
    sleep: Callable = time.sleep
    generate_final_report: Callable = model.generate_final_report
    shutdown: Callable = shutdown_pc
    load_settings: Callable = carica_impostazioni
    send_executor: Callable | None = None
    review_controller: Callable | None = None


def empty_state():
    return {"task_corrente": None, "fase": "idle", "thread_id": None, "pezzo_corrente": 1,
            "esecutore_corrente": None, "conversazioni": {}, "esecutori_usati": [],
            "esecutori_abbandonati": [], "controllori_usati": [],
            "rimandi_pezzo": 0, "rimandi_totali": 0, "bocciature": 0,
            "errori_claude_consecutivi": 0,
            "ultimo_messaggio": "", "attesa_fino_a": None, "eventi": []}


def task_snapshot(task: Task):
    return {"file": str(task.file.resolve()), "id": task.id, "titolo": task.titolo,
            "progetto": str(task.progetto.resolve()), "branch": task.branch, "push": task.push,
            "fascicolo": task.fascicolo, "pezzi": [asdict(piece) for piece in task.pezzi],
            "comandi_verifica": list(task.comandi_verifica)}


class Orchestrator:
    def __init__(self, config: Config, *, services: Services | None = None, git=None,
                 non_spegnere: bool = False, automatico: bool = False):
        self.config = config
        self.non_spegnere = non_spegnere
        # Solo l'avvio automatico rispetta le fasce orarie; lo Start manuale le ignora.
        self.automatico = automatico
        self.services = Services() if services is None else services
        # Ogni Start rivaluta le priorità al primo invio, anche riprendendo una task.
        self._executor_priority_on_start = True
        self._reload_settings()
        self.store = StateStore(config.cartella_base)
        saved = self.store.load()
        self.state = {**empty_state(), **saved}
        if self.state["task_corrente"] and "esecutore_corrente" not in saved:
            self.state["esecutore_corrente"] = "codex"
            self.state["conversazioni"] = {"codex": self.state["thread_id"]} if self.state["thread_id"] else {}
            self.state["esecutori_usati"] = ["codex"]
        if self.state["fase"] not in PHASES:
            raise StateError(f"Fase non riconosciuta: {self.state['fase']}")
        if self.state["task_corrente"] and "fascicolo" not in self.state["task_corrente"]:
            self.state["task_corrente"] = task_snapshot(load_task(self.state["task_corrente"]["file"]))
        self.reports = ReportStore(config.cartella_base)
        self.logs = TaskLogger(config.cartella_base)
        self.git = GitOperations(config, logger=self._log) if git is None else git
        self.exit_code = None
        if not self.state.get("periodo"):
            self.state["periodo"] = {"inizio": self._now().isoformat(), "task_ids": [],
                                     "task": {}, "eventi": []}
            # Migrazione dei soli stati precedenti privi di periodo.
            for event in self.state["eventi"]:
                self._period_event(event)
        self._save()

    def _reload_settings(self):
        self.impostazioni = self.services.load_settings(
            self.config.cartella_base / "impostazioni.json", config=self.config)

    @property
    def shutdown_enabled(self):
        return self.impostazioni["spegnimento_automatico"] and not self.non_spegnere

    def _now(self):
        value = self.services.clock()
        return value.astimezone() if value.tzinfo is None else value

    def _save(self):
        self.state["aggiornato_a"] = self._now().isoformat()
        self.store.save(self.state)

    def _log(self, message, data=None):
        if data is not None:
            message += "\n" + json.dumps(asdict(data) if is_dataclass(data) else data,
                                         ensure_ascii=False, default=str)
        task = self.state.get("task_corrente")
        self.logs.write(self._now().isoformat(), message,
                        task_id=task["id"] if task else None, day=self.state.get("giorno_log"))

    def _report_task(self):
        task = self.state.get("task_corrente")
        if not task:
            return None
        return {"id": task["id"], "titolo": task["titolo"],
                "progetto": task["progetto"],
                "query_da_eseguire": self.state.get("query_da_eseguire", []),
                "note_query_codex": self.state.get("note_query_codex", ""),
                "esecutori": list(self.state.get("esecutori_usati", [])),
                "controllore": self.state.get("controllore_corrente"),
                "controllori": list(self.state.get("controllori_usati", [])),
                "esito": "in attesa limite" if self.state["fase"] == "attesa_limite" else self.state.get("esito", "in corso"),
                "pezzi_completati": self.state.get("pezzi_completati", 0), "pezzi_totali": len(task["pezzi"]),
                "rimandi_totali": self.state["rimandi_totali"], "bocciature": self.state["bocciature"],
                "errori_claude_consecutivi": self.state["errori_claude_consecutivi"],
                "inizio": self.state.get("inizio"), "fine": self.state.get("fine"),
                "motivo": self.state.get("motivo", ""), "commit": self.state.get("commit_hash"),
                "push": self.state.get("push_esito"), "stash": self.state.get("stash_nome"),
                "modifiche_preesistenti": self.state.get("modifiche_preesistenti", {}),
                "preesistenti_toccati": self.state.get("preesistenti_toccati", []),
                "attese": self.state.get("attese", [])}

    def _event(self, kind, message):
        event = {"event_id": uuid4().hex, "at": self._now().isoformat(), "tipo": kind,
                 "messaggio": message, "run_id": self.state.get("run_id"), "task": self._report_task()}
        # Copia la fotografia: le attese successive non mutano eventi già salvati.
        event = json.loads(json.dumps(event, ensure_ascii=False))
        self.state["eventi"].append(event)
        self._period_event(event)
        self._save()  # Write-ahead; il report può essere ricostruito dopo un arresto.
        self.reports.record(event)
        self._log(f"{kind}: {message}")

    def _period_event(self, event):
        period = self.state["periodo"]
        if any(item["event_id"] == event["event_id"] for item in period["eventi"]):
            return
        period["eventi"].append(event)
        task = event.get("task")
        if task:
            if task["id"] not in period["task_ids"]:
                period["task_ids"].append(task["id"])
            period["task"][task["id"]] = task

    def _clear_task(self):
        preserved = {key: self.state[key] for key in (
            "periodo", "ultimo_periodo", "attese_esecutori", "utilizzo_esecutori") if key in self.state}
        self.state = empty_state()
        self.state.update(preserved)

    def _has_completed_tasks(self):
        return any(event["tipo"] in {"task_fatta", "task_scartata"}
                   for event in self.state["periodo"]["eventi"])

    def _has_activity(self):
        return self.state["periodo"].get("attivita", False) or any(
            event["tipo"] in {"task_iniziata", "task_fatta", "task_scartata"}
            for event in self.state["periodo"]["eventi"])

    def _end_period(self):
        closing = self.state.pop("finalizzazione")
        self.state["ultimo_periodo"] = {**self.state["periodo"],
                                        "fine": self._now().isoformat(),
                                        "report": closing["report"], "motivo": closing["motivo"]}
        self.state["periodo"] = {"inizio": self._now().isoformat(), "task_ids": [],
                                 "task": {}, "eventi": []}
        self.state["azione_esterna"] = None
        self._save()

    def _cancel_shutdown(self):
        self._disable_shutdown_report()
        self._event("spegnimento_annullato", "STOP presente: spegnimento annullato")
        self._end_period()
        self.exit_code = 0

    def _disable_shutdown_report(self):
        path = Path(self.state["finalizzazione"]["report"])
        content = path.read_text(encoding="utf-8")
        content = content.rsplit("\nSpegnimento:", 1)[0] + "\nSpegnimento: disattivato\n"
        atomic_write(path, content.encode("utf-8"))

    def _finalize_period(self, reason=None):
        self._reload_settings()
        if "finalizzazione" not in self.state:
            now = self._now()
            path = self.config.cartella_base / "report" / f"finale-{now:%Y-%m-%d-%H%M}.md"
            number = 2
            original = path
            while path.exists():
                path = original.with_name(f"{original.stem}-{number}.md")
                number += 1
            self.state["finalizzazione"] = {"motivo": reason, "report": str(path),
                "dati": period_data(self.state["periodo"], reason, now), "scritto": False}
            self._save()
        closing = self.state["finalizzazione"]
        if not closing["scritto"]:
            try:
                content = self._external("report_finale", self.services.generate_final_report,
                                         self.config, closing["dati"])
            except StopRequested:
                raise
            except Exception as exc:
                self._log("Modello non disponibile per il report finale", str(exc))
                content = ""
            when = (self._now() + timedelta(seconds=self.config.attesa_prima_spegnimento_s)
                    if self.shutdown_enabled else None)
            atomic_write(Path(closing["report"]), render_report(closing["dati"], content, when).encode("utf-8"))
            closing.update(scritto=True, spegnimento_alle=when.isoformat() if when else None)
            self._event("report_finale", f"Report finale: {closing['report']}; {closing['motivo']}")
        if not self.shutdown_enabled or not closing.get("spegnimento_alle"):
            if closing.get("spegnimento_alle"):
                self._disable_shutdown_report()
            self._end_period()
            self.exit_code = 0
            return
        when = datetime.fromisoformat(closing["spegnimento_alle"])
        if not closing.get("programmato"):
            closing["programmato"] = True
            self._event("spegnimento_programmato", f"Spegnimento programmato alle {when:%H:%M}")
        while self._now() < when:
            if self._stopped():
                self._cancel_shutdown()
                return
            remaining = (when - self._now()).total_seconds()
            if remaining <= 0:
                break
            self.services.sleep(min(1, remaining))
        if self._stopped():
            self._cancel_shutdown()
            return
        self._reload_settings()
        if not self.shutdown_enabled:
            self._disable_shutdown_report()
            self._end_period()
            self.exit_code = 0
            return
        # Stato chiuso prima del comando: al riavvio non si ripete lo spegnimento.
        self._end_period()
        self.services.shutdown(self.config)
        self.exit_code = 0

    def _transition(self, phase, message=None, **updates):
        self.state.update(updates)
        self.state.update(fase=phase, azione_esterna=None)
        self._event("fase", message or phase)

    def _external(self, label, function, *args, **kwargs):
        if self._stopped():
            self._save()
            raise StopRequested()
        self.state["azione_esterna"] = label
        self._save()
        return function(*args, **kwargs)

    def _stopped(self):
        return (self.config.cartella_base / "STOP").exists()

    def _destination(self, folder, name):
        target = self.config.cartella_base / folder / name
        if target.exists():
            target = target.with_name(f"{target.stem}-{self.state['run_id']}{target.suffix}")
        return target

    def _start(self, task, *, invalid_reason=None):
        self._clear_task()
        now = self._now()
        self.state.update(task_corrente=task, run_id=uuid4().hex, inizio=now.isoformat(),
                          giorno_log=now.date().isoformat(), esito="in corso", attese=[],
                          pezzi_completati=0, task_ha_lavorato=False, azione_iniziata=False)
        if invalid_reason:
            self.state.update(fase="chiusura", esito="scartata", motivo=invalid_reason)
        else:
            self.state["fase"] = "preparazione"
            self.state["file_in_corso"] = str(self._destination("in-corso", Path(task["file"]).name))
        self._event("task_iniziata", invalid_reason or f"Avvio task {task['id']}")

    def _choose(self):
        tasks, errors = scan_queue(self.config.cartella_base / "coda")
        if errors:
            path, reason = errors[0]
            self._start({"file": str(path.resolve()), "id": path.stem.split("-")[0], "titolo": path.stem,
                         "progetto": None, "branch": None, "push": False, "fascicolo": "",
                         "pezzi": [], "comandi_verifica": []}, invalid_reason=reason)
            return True
        if tasks:
            self._start(task_snapshot(tasks[0]))
            return True
        return False

    def _prepare(self):
        source = Path(self.state["task_corrente"]["file"])
        target = Path(self.state["file_in_corso"])
        target.parent.mkdir(parents=True, exist_ok=True)
        self._save()
        if source != target and source.exists():
            if target.exists():
                raise StateError(f"Destinazione in-corso già occupata: {target}")
            source.rename(target)
        elif not target.exists():
            raise StateError("Fascicolo corrente non trovato né in coda né in-corso")
        self.state["task_corrente"]["file"] = str(target)
        self._transition("controlli_iniziali")

    def _discard(self, reason):
        self._transition("scarto", reason, esito="scartata", motivo=reason)

    def _branch_ok(self):
        task = self.state["task_corrente"]
        branch = self._external("git_branch", self.git.branch, task["progetto"])
        if branch != task["branch"]:
            self._discard(f"Branch corrente {branch!r}, atteso {task['branch']!r}; nessun cambio branch eseguito")
            return False
        return True

    def _initial_checks(self):
        project = self.state["task_corrente"]["progetto"]
        try:
            if not Path(project).is_dir():
                self._discard(f"Cartella progetto non esistente: {project}")
            elif not self._external("git_repo", self.git.is_repo, project):
                self._discard("La cartella progetto non è un repository git")
            elif not self._branch_ok():
                return
            elif (not self.config.consenti_modifiche_preesistenti
                  and self._external("git_status_iniziale", self.git.dirty, project)):
                self._discard("Working tree iniziale non pulito; nessun file del progetto è stato modificato")
            else:
                if "modifiche_preesistenti" not in self.state:
                    self.state["modifiche_preesistenti"] = self._external(
                        "impronte_iniziali", self.git.preexisting_changes, project)
                    self._event("modifiche_preesistenti", "Modifiche preesistenti lasciate intatte: "
                                + (", ".join(self.state["modifiche_preesistenti"]) or "nessuna"))
                self._transition("invio_codex", controlli_superati=True, azione_iniziata=False)
        except GitError as exc:
            self._discard(f"Controlli git iniziali falliti: {exc}")

    def _piece(self):
        return self.state["task_corrente"]["pezzi"][self.state["pezzo_corrente"] - 1]["testo"]

    def _first_prompt(self):
        instructions = (self.config.cartella_base / "regole" / "istruzioni-codex.md").read_text(encoding="utf-8-sig")
        instructions += ("\n\nNel blocco NOTE della risposta indica sempre QUERY DA ESEGUIRE SU SUPABASE: "
                         "seguito dai percorsi dei nuovi file SQL da eseguire manualmente e dalle note "
                         "di controllo, oppure nessuna. Non eseguire query su Supabase.")
        if self.state.get("modifiche_preesistenti"):
            instructions += ("\n\nFile con modifiche preesistenti dell'utente: NON leggerli per modificarli, "
                             "NON toccarli: " + ", ".join(self.state["modifiche_preesistenti"]))
        completed = self.state.get("pezzi_completati", 0)
        if self.state.get("passaggio_esecutore") or completed:
            instructions += (f"\n\nI pezzi 1..{completed} sono già stati completati e approvati; "
                             "il loro lavoro è già nel working tree (non committato). "
                             if completed else "\n\nNessun pezzo è ancora stato approvato. ")
            instructions += ("L'esecutore precedente può avere lasciato lavoro parziale nel working tree: "
                             "controllalo e completa soltanto il pezzo corrente. Non ripetere i pezzi approvati.")
        return codex.build_first_prompt(instructions, self.state["task_corrente"]["fascicolo"],
                                        self.state["pezzo_corrente"], self._piece())

    def _review_exclusions(self):
        original = self.state.get("modifiche_preesistenti", {})
        project = self.state["task_corrente"]["progetto"]
        touched = [name for name, digest in original.items()
                   if self._external("impronta_corrente", self.git.fingerprint, project, name) != digest]
        reported = self.state.setdefault("preesistenti_toccati", [])
        new = [name for name in touched if name not in reported]
        if new:
            reported.extend(new)
            self._event("preesistenti_toccati", "Modifiche preesistenti toccate durante la task, "
                        "incluse nella revisione: " + ", ".join(new))
        return [name for name in original if name not in touched]

    @staticmethod
    def _exclusion_options(paths):
        return {"exclude_paths": paths} if paths else {}

    def _schedule_codex(self, message, event_message=None):
        current = self.state.get("esecutore_corrente") or "codex"
        if not self.state["conversazioni"].get(current):
            message = self._first_prompt() + "\n\n" + message
        self._transition("invio_codex", event_message, ultimo_messaggio=message,
                         azione_iniziata=False, ripeti_esatto=False, prompt_claude=None)

    def _login(self, tool, message):
        self.state.update(serve_login=tool, motivo=f"serve login: {tool} — {message}", azione_iniziata=False)
        if tool == "claude":
            self.state["ripeti_esatto"] = True
        self._event("serve login", self.state["motivo"])
        self.exit_code = 2

    def _wait_limit(self, tool, retry_at, repeat_phase, *, weekly=False, text=""):
        start = self._now()
        classifier = codex_weekly if tool == "codex" else claude_weekly
        weekly = weekly or classifier(text, retry_at, now=start,
                                     threshold_hours=self.config.soglia_limite_settimanale_ore)
        until = retry_at or (start + timedelta(minutes=self.config.attesa_limite_minuti))
        if until.tzinfo is None:
            until = until.astimezone()
        if retry_at is not None:
            until = max(until, start) + timedelta(seconds=60)
        self.state.setdefault("attese", []).append({"strumento": tool, "inizio": start.isoformat(),
                                                    "fino_a": until.isoformat(), "fine": None,
                                                    "settimanale": weekly,
                                                    "sblocco_previsto": retry_at.isoformat() if retry_at else None})
        self.state.update(fase="attesa_limite", attesa_fino_a=until.isoformat(), strumento_attesa=tool,
                          attesa_riserve=False,
                          limite_settimanale=weekly,
                          fase_da_ripetere=repeat_phase, ripeti_esatto=True, azione_iniziata=False)
        self._event("attesa_inizio", f"Limite {tool}; attesa fino a {until.isoformat()}")

    def _launch_failed(self, tool, error):
        if tool == "antigravity":
            from .antigravity import AGY
            eseguibile = str(AGY)
        else:
            eseguibile = (self.config.comando_codex if tool == "codex" else self.config.comando_claude)[0]
        message = f"Impossibile avviare {tool.title()}: {eseguibile} — controllare l'installazione ({error})"
        self.state.update(azione_iniziata=False, ripeti_esatto=True, ultimo_errore=message)
        self._event("errore_operativo", message)
        self.exit_code = 1

    def _wait(self, seconds):
        until = self._now() + timedelta(seconds=seconds)
        while self._now() < until:
            if self._stopped():
                return False
            remaining = (until - self._now()).total_seconds()
            if remaining <= 0:
                break
            self.services.sleep(min(60, remaining))
        return not self._stopped()

    def _resume_wait(self):
        self._reload_settings()
        previous = self.state.get("strumento_attesa")
        controller = self.impostazioni["controllore"]["strumento"]
        if self.state.get("fase_da_ripetere") == "claude" and previous and previous != controller:
            # La fase di revisione mantiene il nome storico anche per Codex/Agy.
            # Il limite salvato appartiene al vecchio strumento, non alla fase.
            waits = self.state.get("attese", [])
            if waits and waits[-1].get("strumento") == previous and not waits[-1].get("fine"):
                waits[-1]["fine"] = self._now().isoformat()
            self.state.update(fase="claude", attesa_fino_a=None, strumento_attesa=None,
                              fase_da_ripetere=None, attesa_riserve=False, limite_settimanale=False,
                              ripeti_esatto=False, prompt_claude=None, azione_esterna=None,
                              azione_iniziata=False)
            self._event("attesa_annullata", f"Controllore cambiato da {previous} a {controller}: "
                        "annullata l'attesa precedente, riprendo la revisione")
            return
        if self.state.get("attesa_riserve"):
            # Riconsidera subito anche modifiche alle impostazioni durante l'attesa.
            if self._select_executor():
                self.state.update(fase="invio_codex", attesa_fino_a=None, attesa_riserve=False)
                if self.state.get("attese"):
                    self.state["attese"][-1]["fine"] = self._now().isoformat()
                self._event("attesa_fine", "Esecutore disponibile")
                return
            if self.exit_code is not None:
                return
            # _select_executor aggiorna l'attesa più vicina senza chiamare strumenti.
            if not self.state.get("limite_settimanale"):
                seconds = max(0, (datetime.fromisoformat(self.state["attesa_fino_a"]) - self._now()).total_seconds())
                if not self._wait(min(60, seconds)):
                    self._event("arresto", "STOP durante attesa limite; stato conservato")
                    self.exit_code = 0
                return
        if self.state.get("limite_settimanale") and self._has_activity():
            reset = self.state["attese"][-1].get("sblocco_previsto") or self.state["attesa_fino_a"]
            self._finalize_period(f"limite settimanale {self.state['strumento_attesa']}, sblocco previsto {reset}")
            return
        until = datetime.fromisoformat(self.state["attesa_fino_a"])
        if until.tzinfo is None:
            until = until.astimezone()
        seconds = max(0, (until - self._now()).total_seconds())
        if seconds > 0:
            # Torna al ciclo per rileggere le impostazioni salvate ogni minuto.
            if not self._wait(min(60, seconds)):
                self._event("arresto", "STOP durante attesa limite; stato conservato")
                self.exit_code = 0
            return
        self.state["attese"][-1]["fine"] = self._now().isoformat()
        self.state.update(fase=self.state["fase_da_ripetere"], attesa_fino_a=None)
        self._event("attesa_fine", f"Fine attesa {self.state['strumento_attesa']}")

    def _record_usage(self, tool, observation, *, controller=False):
        # Le date diventano stringhe prima della persistenza JSON.
        serial = {key: value.isoformat() if isinstance(value, datetime) else value
                  for key, value in observation.items()}
        self.state.setdefault("utilizzo_esecutori", {})[tool] = serial
        budget = self.state["periodo"].setdefault("budget", {})
        if controller and tool not in budget:
            return
        entry = budget.setdefault(tool, {"consumato": 0.0, "ultima": None})
        entry["limite"] = self.impostazioni["esecutori"][tool]["limite_5h_percento"]
        if aggiorna_budget(entry, observation, controllore=controller):
            self._event("utilizzo_non_leggibile", f"{tool}: utilizzo non leggibile")
        self._save()

    def _budget_exhausted(self, name):
        limit = self.impostazioni["esecutori"][name]["limite_5h_percento"]
        used = self.state["periodo"].get("budget", {}).get(name, {}).get("consumato", 0)
        return limit is not None and used >= limit

    def _select_executor(self):
        settings = self.impostazioni["esecutori"]
        current = self.state.get("esecutore_corrente")
        retired = self.state["esecutori_abbandonati"]
        reconsider = getattr(self, "_executor_priority_on_start", False)
        candidates = sorted((name for name, item in settings.items()
                             if item["attivo"] and (reconsider or name not in retired)),
                            key=lambda name: settings[name]["priorita"])
        if not candidates:
            raise StateError("Nessun esecutore attivo configurato disponibile per questa task")
        remaining = [name for name in candidates if not self._budget_exhausted(name)]
        if not remaining:
            reason = "limiti di utilizzo impostati raggiunti: " + ", ".join(candidates)
            self.state.update(azione_iniziata=False, fase="invio_codex", attesa_riserve=False)
            self._event("budget_esauriti", reason)
            self._finalize_period(reason)
            return None
        waits = self.state.setdefault("attese_esecutori", {})

        def free(names):
            return [name for name in names if name not in waits
                    or datetime.fromisoformat(waits[name]["fino_a"]) <= self._now()]

        available = free(remaining)
        if not available:
            # Nessuna riserva libera: un esecutore già lasciato in questa task, se è tornato
            # disponibile e ha ancora budget, è meglio di un'attesa.
            back = free([name for name in sorted(retired, key=lambda n: settings[n]["priorita"])
                         if name in settings and settings[name]["attivo"]
                         and not self._budget_exhausted(name)])
            if back:
                retired.remove(back[0])
                available = back
        if not available:
            nearest = min(remaining, key=lambda name: datetime.fromisoformat(waits[name]["fino_a"]))
            record = waits[nearest]
            # Una sola attesa persistente; non accumulare eventi a ogni controllo.
            if not self.state.get("attesa_riserve"):
                self._wait_limit(nearest,
                    datetime.fromisoformat(record["reset"]) if record.get("reset") else None,
                    "invio_codex", weekly=record["settimanale"], text=record["motivo"])
            self.state.update(attesa_riserve=True, attesa_fino_a=record["fino_a"],
                              strumento_attesa=nearest, limite_settimanale=record["settimanale"])
            self._save()
            return None
        chosen = available[0] if reconsider else (current if current in available else available[0])
        self._executor_priority_on_start = False
        if chosen in retired:
            retired.remove(chosen)
        if chosen != current:
            if current:
                reason = ("priorità rivalutate all'avvio" if reconsider else
                          "budget esaurito" if self._budget_exhausted(current) else
                          waits.get(current, {}).get("motivo", "esecutore disattivato"))
                if current not in retired:
                    retired.append(current)
                self.state["conversazioni"].pop(chosen, None)
                self.state["passaggio_esecutore"] = True
                previous = self.state.get("ultimo_messaggio", "")
                # Se l'ultimo invio era già un primo prompt completo, non ripetere il fascicolo.
                tail = "" if "# FASCICOLO DELLA TASK" in previous else "\n\n" + previous
                self.state["ultimo_messaggio"] = self._first_prompt() + tail
                message = f"Cambio esecutore: {current} → {chosen} ({reason})"
            else:
                message = f"Esecutore scelto: {chosen}"
            self.state["esecutore_corrente"] = chosen
            self.state["attesa_riserve"] = False
            self._event("cambio_esecutore" if current else "esecutore_scelto", message)
        return chosen

    def _executor_limit(self, name, limit):
        self._reload_settings()
        start = self._now()
        classifier = codex_weekly if name == "codex" else claude_weekly
        weekly = limit.weekly or classifier(limit.message, limit.retry_at, now=start,
                                            threshold_hours=self.config.soglia_limite_settimanale_ore)
        retry = limit.retry_at
        if retry is not None and retry.tzinfo is None:
            retry = retry.astimezone()
        until = (max(retry, start) + timedelta(seconds=60) if retry is not None else
                 start + timedelta(minutes=self.config.attesa_limite_minuti))
        self.state.setdefault("attese_esecutori", {})[name] = {
            "fino_a": until.isoformat(), "reset": retry.isoformat() if retry else None,
            "settimanale": weekly, "motivo": limit.message}
        self.state.update(fase="invio_codex", azione_iniziata=False)
        self._event("limite_esecutore", f"Limite {name}: {limit.message}")
        self._select_executor()

    def _send_codex(self):
        self._reload_settings()
        name = self._select_executor()
        if name is None:
            return
        if not self._branch_ok():
            return
        task = self.state["task_corrente"]
        prompt = self.state["ultimo_messaggio"] or self._first_prompt()
        self.state.update(ultimo_messaggio=prompt, azione_iniziata=True,
                          ripeti_esatto=False, serve_login=None, motivo="")
        self._log(f"PROMPT {name.upper()}", prompt)

        def save_thread(thread_id):
            self.state["conversazioni"][name] = thread_id
            if name == "codex":
                self.state["thread_id"] = thread_id
            self.state["task_ha_lavorato"] = True
            self._event(f"conversazione_{name}", f"Conversazione {name} salvata: {thread_id}")

        if name not in self.state["esecutori_usati"]:
            self.state["esecutori_usati"].append(name)
        dispatch = self.services.send_executor or esecutori.invia
        injection = {} if self.services.send_executor else {
            "run_codex": self.services.run_codex, "run_claude": self.services.run_claude}
        result = self._external(
            name, dispatch, name, self.config, self.impostazioni["esecutori"][name], task["progetto"], prompt,
            conversazione=self.state["conversazioni"].get(name), on_conversazione=save_thread,
            log_label=task["id"], now=self._now(), **injection)
        self._log(f"RISPOSTA {name.upper()}", result)
        self.state["azione_iniziata"] = False
        if result.non_configurato:
            raise StateError(f"Esecutore {name} non configurato")
        if result.errore_avvio:
            self._launch_failed(name, result.errore_avvio)
            return
        self.state["periodo"]["attivita"] = True
        self.state["task_ha_lavorato"] = True
        if result.conversazione:
            self.state["conversazioni"][name] = result.conversazione
            if name == "codex":
                self.state["thread_id"] = result.conversazione
        self.state.update(ultima_risposta_codex=result.ultimo_messaggio,
                          ultima_risposta_esecutore=result.ultimo_messaggio,
                          note_query_codex=extract_query_notes(result.ultimo_messaggio),
                          errori_codex=[{"message": error} for error in result.errori], stderr_codex=result.stderr)
        self._record_usage(name, result.osservazione_utilizzo)
        diagnostic = "\n".join([result.stderr, *result.errori])
        if result.errore_autenticazione or is_authentication_error(diagnostic):
            self._login(name, diagnostic)
        elif result.limite is not None:
            self._executor_limit(name, result.limite)
        else:
            self._transition("verifica")

    def _verify(self):
        task = self.state["task_corrente"]
        results = self._external("verifiche", self.services.run_verifications,
                                 self.config, task["progetto"], task["comandi_verifica"])
        self.state["verifiche"] = [asdict(result) for result in results]
        self._log("OUTPUT COMPLETI VERIFICHE", self.state["verifiche"])
        self.state["file_cambiati"] = list(self._external(
            "git_status", self.services.list_changed_files, self.config, task["progetto"],
            **self._exclusion_options(self._review_exclusions())))
        self._transition("modello")

    def _verification_results(self):
        return [verification.VerificationResult(**{**item, "error_lines": tuple(item["error_lines"])})
                for item in self.state.get("verifiche", [])]

    def _model(self):
        task = self.state["task_corrente"]
        try:
            decision = self._external(
                "modello", self.services.decide_next_action, self.config, task["fascicolo"],
                self.state["pezzo_corrente"], self.state["rimandi_pezzo"],
                self.state.get("ultima_risposta_codex", ""), self._verification_results(),
                self.state.get("file_cambiati", []))
        except StopRequested:
            raise
        except Exception as exc:
            decision = model.ModelDecision("chiama_claude", "", f"Modello non disponibile: {exc}", forced=True)
        self._log("DECISIONE MODELLO", decision)
        outcome = decision.esito
        if outcome == "rimanda_a_codex" and self.state["rimandi_pezzo"] >= self.config.rimandi_max_per_pezzo:
            outcome = "chiama_claude"
        if outcome == "rimanda_a_codex":
            self.state["rimandi_pezzo"] += 1
            self.state["rimandi_totali"] += 1
            self._schedule_codex(decision.messaggio, f"Rimando: {decision.motivo}")
        elif outcome == "limite_raggiunto":
            name = self.state.get("esecutore_corrente") or "codex"
            text = self.state.get("ultima_risposta_codex", "")
            if name == "codex":
                reset = self._external("sblocco_codex", self.services.codex_reset, self.config,
                                       self.state["thread_id"], text, self._now())
            else:
                from .claude_limits import claude_reset_time
                reset = claude_reset_time(text, now=self._now())
            self._executor_limit(name, UsageLimit(text, reset, "testo"))
        else:
            self._transition("claude", f"Modello: {decision.motivo}", prompt_claude=None, ripeti_esatto=False)

    def _review(self):
        self._reload_settings()
        settings = self.impostazioni["controllore"]
        name = settings["strumento"]
        if self.state.get("controllore_corrente") != name:
            self.state.update(controllore_corrente=name, errori_claude_consecutivi=0,
                              claude_non_riconosciuto=0)
        if name not in self.state["controllori_usati"]:
            self.state["controllori_usati"].append(name)
        task = self.state["task_corrente"]
        prompt = self.state.get("prompt_claude") if self.state.get("ripeti_esatto") else None
        if prompt is None:
            excluded = self._review_exclusions()
            diff = self._external("diff", self.services.collect_review_diff, self.config, task["progetto"],
                                  **self._exclusion_options(excluded))
            context = {}
            if self.state.get("modifiche_preesistenti"):
                context = {"preexisting_paths": excluded,
                           "touched_preexisting_paths": [name for name in self.state["modifiche_preesistenti"]
                                                         if name not in excluded]}
            prompt = self.services.build_claude_prompt(
                self.config, task["fascicolo"], self.state["pezzo_corrente"], self._piece(),
                self.state.get("ultima_risposta_codex", ""), self._verification_results(), diff, **context)
        self.state.update(prompt_claude=prompt, ripeti_esatto=False, serve_login=None, motivo="")
        self._log(f"PROMPT {name.upper()}", prompt)
        dispatch = self.services.review_controller or controllori.revisiona
        injection = {} if self.services.review_controller else {
            "run_codex": self.services.run_codex, "run_claude": self.services.run_claude}
        result = self._external(name, dispatch, name, self.config, settings,
                                task["progetto"], prompt, log_label=task["id"], now=self._now(), **injection)
        self._log(f"RISPOSTA E DECISIONE {name.upper()}", result)
        if result.errore_avvio:
            self._launch_failed(name, result.errore_avvio)
            return
        self.state["periodo"]["attivita"] = True
        self._record_usage(name, result.osservazione_utilizzo, controller=True)
        verdict = result.review
        if verdict.kind == "errore_autenticazione":
            self._login(name, verdict.error or verdict.text)
            return
        if verdict.kind == "limite":
            self._wait_limit(name, verdict.retry_at, "claude", weekly=bool(result.limite and result.limite.weekly),
                             text=verdict.text + "\n" + result.stderr)
            return
        if verdict.kind == "errore":
            count = self.state.get("errori_claude_consecutivi", 0) + 1
            message = (f"Errore temporaneo {name.title()} ({count}/3) sul pezzo "
                       f"{self.state['pezzo_corrente']}: "
                       f"{verdict.error or verdict.text or 'Errore controllore'}")
            self.state.update(errori_claude_consecutivi=count, ripeti_esatto=True,
                              ultimo_errore=message)
            if count >= 3:
                self._event("errori_claude_esauriti", message + "; orchestratore fermato, stato conservato per la ripresa")
                self.exit_code = 1
            else:
                self._event("errore_temporaneo_claude", message)
                self._wait_limit(name, None, "claude")
            return
        if verdict.kind in {"ok", "correzioni"}:
            self.state["errori_claude_consecutivi"] = 0
        self.state["rimandi_pezzo"] = 0
        if verdict.kind == "non_riconosciuto" and self.state.get("claude_non_riconosciuto", 0) == 0:
            self.state.update(claude_non_riconosciuto=1, ripeti_esatto=True)
            self._event("claude_non_riconosciuto", "Ripetizione unica della revisione")
            return
        self.state["claude_non_riconosciuto"] = 0
        # Contatori e fase successiva vengono salvati insieme: nessun doppio
        # incremento se il PC si arresta durante l'applicazione del verdetto.
        if verdict.kind == "ok":
            self.state["pezzi_completati"] = self.state["pezzo_corrente"]
            message = f"Pezzo {self.state['pezzo_corrente']} approvato: {verdict.details}"
            if self.state["pezzo_corrente"] == len(task["pezzi"]):
                self._transition("commit", message, fase_commit="prepara")
            else:
                self.state["pezzo_corrente"] += 1
                self._schedule_codex(self._piece(), message)
        else:
            self.state["bocciature"] += 1
            correction = verdict.details if verdict.kind == "correzioni" else verdict.text
            if self.state["bocciature"] >= self.config.bocciature_claude_max:
                self._discard(f"Raggiunte {self.state['bocciature']} bocciature {name.title()}: {correction}")
            else:
                self._schedule_codex(f"Correzioni richieste dal revisore: {correction}",
                                     f"Correzioni {name.title()}: {correction}")

    def _commit(self):
        if not self._branch_ok():
            return
        task = self.state["task_corrente"]
        project = task["progetto"]
        message = f"{task['id']}: {task['titolo']}"
        preexisting = self.state.get("modifiche_preesistenti", {})
        if preexisting:
            message += "\n\nInclude modifiche preesistenti: " + ", ".join(preexisting)
        try:
            stage = self.state.get("fase_commit", "prepara")
            if stage == "prepara":
                self.state["head_prima_commit"] = self._external("head_prima_commit", self.git.head, project)
                self.state["fase_commit"] = stage = "add"
                self._save()
            if stage == "add":
                self._external("git_add", self.git.add, project)
                self.state["fase_commit"] = "commit"
                self._save()
            current = self._external("head_commit", self.git.head, project)
            before = self.state.get("head_prima_commit")
            if current != before:
                info = self._external("recupero_commit", self.git.commit_info, project)
                if info["subject"] != message or info["parents"] != ([before] if before else []):
                    raise StateError("HEAD è cambiato durante il commit; ripresa fermata per evitare duplicati")
            else:
                self._external("git_commit", self.git.commit, project, message)
                current = self._external("hash_commit", self.git.head, project)
                if not current or current == before:
                    raise GitError("Commit senza un nuovo hash HEAD")
            self.state["commit_hash"] = current
            self.state["query_da_eseguire"] = self._external(
                "query_del_commit", self.git.added_queries, project, before, current)
            self._event("commit", f"Commit registrato: {current}")
            if preexisting:
                self._event("commit_preesistenti", "Include modifiche preesistenti: " + ", ".join(preexisting))
            self._transition("push")
        except GitError as exc:
            if self.state.get("commit_hash"):
                raise  # Commit riuscito: conserva la fase per ritentare l'inventario.
            self._discard(f"Commit fallito: {exc}")

    def _push(self):
        task = self.state["task_corrente"]
        project = task["progetto"]
        if not task["push"] or not self.config.push_abilitato:
            outcome = "push saltato: disabilitato"
        else:
            try:
                if not self._external("git_origin", self.git.has_origin, project):
                    outcome = "push saltato: nessun remoto"
                elif self._external("branch_push", self.git.branch, project) != task["branch"]:
                    outcome = "push fallito: branch corrente diverso da quello della task"
                else:
                    self._external("git_push", self.git.push, project, task["branch"])
                    outcome = "push riuscito"
            except GitError as exc:
                outcome = f"push fallito: {exc}"
        self.state["push_esito"] = outcome
        self._transition("chiusura", outcome, esito="fatta")

    def _reject(self):
        task = self.state["task_corrente"]
        project = task["progetto"]
        exclusions = self._exclusion_options(list(self.state.get("modifiche_preesistenti", {})))
        if self.state.get("task_ha_lavorato") and not self.state.get("stash_fatto"):
            message = f"orchestratore: task {task['id']} scartata"
            latest = self._external("stash_corrente", self.git.latest_stash, project)
            if not self.state.get("stash_prima_salvato"):
                self.state.update(stash_prima_salvato=True, stash_prima=latest["hash"] if latest else None)
                self._save()
            recovered = (latest and latest["hash"] != self.state.get("stash_prima")
                         and message in latest["subject"])
            if not recovered and self._external("git_status_scarto", self.git.dirty, project, **exclusions):
                self._external("git_stash", self.git.stash, project, message, **exclusions)
                latest = self._external("stash_creato", self.git.latest_stash, project)
                if not latest or message not in latest["subject"]:
                    raise GitError("Stash non identificabile dopo lo scarto; ciclo fermato senza perdere il lavoro")
                recovered = True
            if recovered:
                if self._external("git_status_dopo_stash", self.git.dirty, project, **exclusions):
                    raise GitError("Working tree ancora sporco dopo lo stash; task conservata in-corso per la ripresa")
                self.state["stash_nome"] = f"{latest['name']} ({latest['hash']})"
            self.state["stash_fatto"] = True
            self._event("stash", self.state.get("stash_nome") or "Working tree pulito: stash non necessario")
        self._transition("chiusura")

    def _close(self):
        task = self.state["task_corrente"]
        source = Path(task["file"])
        if "file_destinazione" not in self.state:
            folder = "fatte" if self.state["esito"] == "fatta" else "scartate"
            self.state["file_destinazione"] = str(self._destination(folder, source.name))
            self._save()
        target = Path(self.state["file_destinazione"])
        marker = f"<!-- esito-orchestratore: {self.state['run_id']} -->"
        section = (f"\n\n## Esito orchestratore\n\n{marker}\nEsito: {self.state['esito']}\n"
                   f"Motivo: {self.state.get('motivo') or 'Tutti i pezzi approvati'}\n"
                   f"Commit: {self.state.get('commit_hash') or 'nessuno'}\n"
                   f"Push: {self.state.get('push_esito') or 'non eseguito'}\n"
                   f"Stash: {self.state.get('stash_nome') or 'non necessario'}\n").encode("utf-8")
        if source.exists() and source != target:
            data = source.read_bytes()
            if marker.encode("utf-8") not in data:
                self._save()
                atomic_write(source, data + section)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise StateError(f"Destinazione finale già occupata: {target}")
            self._save()
            source.rename(target)
        elif not target.exists():
            raise StateError("Fascicolo da chiudere non trovato")
        task["file"] = str(target)
        if not self.state.get("chiusura_conclusa"):
            self.state.update(fine=self._now().isoformat(), chiusura_conclusa=True)
            self._event("task_" + self.state["esito"], self.state.get("motivo") or "Task completata")
        self._clear_task()
        self._save()

    def _recover(self):
        for event in self.state["eventi"]:
            self.reports.record(event)
        if (self.state["task_corrente"] and self.state["fase"] == "invio_codex"
                and self.state.get("azione_iniziata", True)):
            number = self.state["pezzo_corrente"]
            previous = self.state.get("esecutore_corrente") or "codex"
            self._reload_settings()
            self.state.update(azione_iniziata=False, azione_esterna=None)
            name = self._select_executor()
            if name is None or name != previous:
                # Il selettore ha già preparato attesa o passaggio completo al nuovo esecutore.
                return
            if self.state["conversazioni"].get(name):
                message = (f"L'orchestratore si è riavviato durante il pezzo {number}. "
                           f"Controlla lo stato dei file e completa il pezzo {number}, "
                           "poi rispondi col blocco STATO.")
            else:
                message = self._first_prompt()
            self.state.update(ultimo_messaggio=message, azione_iniziata=False)
            self._event(f"ripresa_{name}", f"Ripresa dell'invio interrotto a {name}")

    def _fuori_fascia(self):
        """Con --automatico e modalità "fasce": True dopo aver atteso, False se si può lavorare.

        Si controlla solo tra un passaggio e l'altro, quindi il passaggio in corso finisce sempre.
        """
        if not self.automatico:
            return False
        self._reload_settings()
        avvio = self.impostazioni["avvio_automatico"]
        if avvio["modalita"] != "fasce" or in_fascia(self._now(), avvio["fasce"]):
            if self.state.pop("fuori_fascia_dal", None):
                self._event("inizio_fascia", "Inizio fascia oraria: riprendo le task")
            return False
        inizio = prossimo_inizio(self._now(), avvio["fasce"])
        if not self.state.get("fuori_fascia_dal"):
            self.state["fuori_fascia_dal"] = self._now().isoformat()
            self._event("fuori_fascia", "Fuori dalle fasce orarie: in pausa fino alle "
                        + (inizio.strftime("%H:%M") if inizio else "?"))
        attesa = (inizio - self._now()).total_seconds() if inizio else 60
        if not self._wait(max(1, min(attesa, 300))):
            raise StopRequested()
        return True

    def run(self, *, una_volta=False) -> int:
        if self._stopped():
            if self.state.get("finalizzazione", {}).get("scritto"):
                self._cancel_shutdown()
            self._log("STOP presente: orchestratore fermo")
            return 0
        try:
            self._recover()
            if "finalizzazione" in self.state:
                self._finalize_period()
                return self.exit_code
            while self.exit_code is None:
                if self._stopped():
                    self._event("arresto", "STOP presente; stato conservato")
                    return 0
                if self._fuori_fascia():
                    continue
                if not self.state["task_corrente"]:
                    if self.state["fase"] != "idle":
                        raise StateError("Stato incoerente: fase attiva senza task")
                    if not self._choose():
                        if self._has_completed_tasks():
                            self._finalize_period("coda terminata")
                            return self.exit_code
                        if una_volta:
                            return 0
                        if not self._wait(self.config.intervallo_coda_vuota_s):
                            self._event("arresto", "STOP durante attesa coda vuota")
                            return 0
                        continue
                handlers = {"preparazione": self._prepare, "controlli_iniziali": self._initial_checks,
                            "invio_codex": self._send_codex, "verifica": self._verify, "modello": self._model,
                            "claude": self._review, "commit": self._commit, "push": self._push,
                            "scarto": self._reject, "chiusura": self._close, "attesa_limite": self._resume_wait}
                phase = self.state["fase"]
                if phase not in handlers:
                    raise StateError(f"Fase attiva non gestibile: {phase}")
                handlers[phase]()
            return self.exit_code
        except StopRequested:
            self._event("arresto", "STOP al punto sicuro; stato conservato per la ripresa")
            return 0
        except KeyboardInterrupt:
            self._event("arresto", "Interruzione richiesta; stato conservato per la ripresa")
            return 0
        except Exception as exc:
            self.state["ultimo_errore"] = str(exc)
            self._event("errore_operativo", str(exc))
            return 1
