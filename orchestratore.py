"""CLI dell'orchestratore persistente."""

import argparse
import json
from pathlib import Path
import sys

from orch.config import ConfigError, load_config
from orch.ciclo import Orchestrator
from orch.persistence import LockError, ProcessLock, StateError, StateStore
from orch.reporting import ReportStore


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        # StringIO e altri stream iniettati nei test non hanno reconfigure.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Esegue le task della coda con Codex e revisione Claude."
    )
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parent / "config.json",
        help="File JSON di configurazione alternativo",
    )
    parser.add_argument("--una-volta", action="store_true", help="Esce quando la coda è vuota")
    parser.add_argument("--non-spegnere", action="store_true", help="Disattiva lo spegnimento per questa esecuzione")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--stato", action="store_true", help="Mostra lo stato senza avviare attori")
    group.add_argument("--report", action="store_true", help="Rigenera il report di oggi")
    parser.add_argument("--ignora-stop", action="store_true", help="Cancella STOP e avvia il ciclo")
    parser.add_argument("--automatico", action="store_true",
                        help="Avvio automatico: parte solo se attivo nelle impostazioni e rispetta le fasce orarie")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.stato:
            print(json.dumps(StateStore(config.cartella_base).load(), ensure_ascii=False, indent=2))
            return 0
        stop = config.cartella_base / "STOP"
        if stop.exists() and not args.ignora_stop and not args.report:
            print("STOP presente: orchestratore non avviato. Usare --ignora-stop per cancellarlo.")
            return 0
        with ProcessLock(config.cartella_base):
            if args.report:
                print(ReportStore(config.cartella_base).regenerate())
                return 0
            if args.ignora_stop and stop.exists():
                stop.unlink()
            engine = Orchestrator(config, non_spegnere=args.non_spegnere, automatico=args.automatico)
            if args.automatico and not engine.impostazioni["avvio_automatico"]["attivo"]:
                print("Avvio automatico disattivato nelle impostazioni: nessuna azione.")
                return 0
            result = engine.run(una_volta=args.una_volta)
            if result == 2:
                print(engine.state.get("motivo", "serve login"), file=sys.stderr)
            elif result:
                print(engine.state.get("ultimo_errore", "Errore operativo; vedere log e report"), file=sys.stderr)
            return result
    except (ConfigError, StateError, LockError, OSError) as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
