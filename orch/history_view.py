"""Vista dello storico della sessione per l'orchestratore.

Fornisce un widget di testo condiviso con scrollbar verticale, a capo automatico,
sola lettura e aggiornamento incrementale senza sfarfallio né perdita dello scroll.
"""

import tkinter as tk
from tkinter import ttk


def create_history(parent, background=None):
    """Crea un widget Text a sola lettura all'interno di un frame espandibile con scrollbar verticale."""
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)

    scrollbar = ttk.Scrollbar(frame, orient="vertical")
    scrollbar.pack(side="right", fill="y")

    kwargs = {
        "wrap": "word",
        "state": "disabled",
        "yscrollcommand": scrollbar.set,
        "font": ("Consolas", 9),
        "relief": "flat",
    }
    if background is not None:
        kwargs["background"] = background
    else:
        try:
            kwargs["background"] = parent.cget("background")
        except Exception:
            pass

    text = tk.Text(frame, **kwargs)
    text.pack(side="left", fill="both", expand=True)
    scrollbar.config(command=text.yview)

    # Scorrimento con tastiera e rotella del mouse su widget a sola lettura
    def _on_mousewheel(event):
        delta = getattr(event, "delta", 0)
        if delta:
            units = -1 * int(delta / 120) if abs(delta) >= 120 else (-1 if delta > 0 else 1)
            text.yview_scroll(units, "units")
        return "break"

    def _on_up(_):
        text.yview_scroll(-1, "units")
        return "break"

    def _on_down(_):
        text.yview_scroll(1, "units")
        return "break"

    def _on_prior(_):
        text.yview_scroll(-1, "pages")
        return "break"

    def _on_next(_):
        text.yview_scroll(1, "pages")
        return "break"

    def _on_home(_):
        text.yview_moveto(0.0)
        return "break"

    def _on_end(_):
        text.yview_moveto(1.0)
        return "break"

    # Permette il fuoco per la tastiera senza bloccare la selezione del testo
    text.bind("<Button-1>", lambda _: text.focus_set(), add="+")
    text.bind("<MouseWheel>", _on_mousewheel)
    text.bind("<Button-4>", lambda _: (text.yview_scroll(-1, "units"), "break")[1])
    text.bind("<Button-5>", lambda _: (text.yview_scroll(1, "units"), "break")[1])
    text.bind("<Up>", _on_up)
    text.bind("<Down>", _on_down)
    text.bind("<Prior>", _on_prior)
    text.bind("<Next>", _on_next)
    text.bind("<Home>", _on_home)
    text.bind("<End>", _on_end)

    return text


def update_history(widget, lines):
    """Aggiorna le righe dello storico nel widget di testo.

    - Se le righe non sono cambiate, non fa nulla.
    - Se le vecchie righe sono un prefisso delle nuove, appende solo le righe aggiunte.
    - Altrimenti sostituisce l'intero contenuto.
    - Ricorda se l'utente era in fondo alla vista e segue le nuove voci solo in quel caso;
      se era scrollato verso l'alto, conserva il viewport e la selezione (nessun furto di scroll).
    - Memorizza le righe correnti sull'attributo _history_lines del widget.
    """
    if lines is None:
        lines = []
    elif isinstance(lines, str):
        lines = lines.splitlines()
    else:
        lines = list(lines)
    # Una chiamata CLI contiene più righe nello stesso gruppo: confronta le righe
    # visibili, così i nuovi messaggi non obbligano a ridisegnare tutto lo storico.
    lines = [row for line in lines for row in line.split("\n")]

    prev_lines = getattr(widget, "_history_lines", None)
    if prev_lines is not None and prev_lines == lines:
        return

    # Controlla se la vista era in fondo e registra il viewport corrente
    was_at_bottom = True
    old_top = 0.0
    old_top_index = None
    try:
        yv = widget.yview()
        if yv and len(yv) >= 2:
            was_at_bottom = float(yv[1]) >= 0.999
            old_top = float(yv[0])
            if not was_at_bottom:
                try:
                    old_top_index = widget.index("@0,0")
                except (AttributeError, tk.TclError):
                    pass
    except Exception:
        pass

    is_prefix = (
        prev_lines is not None
        and len(lines) >= len(prev_lines)
        and lines[:len(prev_lines)] == prev_lines
    )
    insertion_at = None
    insertion_count = 0
    if prev_lines is not None and not is_prefix and len(lines) > len(prev_lines):
        prefix = 0
        while prefix < len(prev_lines) and prev_lines[prefix] == lines[prefix]:
            prefix += 1
        added = len(lines) - len(prev_lines)
        if lines[prefix + added:] == prev_lines[prefix:]:
            insertion_at, insertion_count = prefix, added

    # Conserva l'eventuale selezione attiva su append
    sel_ranges = None
    if is_prefix:
        try:
            sel_ranges = widget.tag_ranges("sel")
        except Exception:
            pass

    # Abilita temporaneamente la modifica del widget di sola lettura
    was_disabled = False
    try:
        if str(widget.cget("state")) == "disabled":
            was_disabled = True
            widget.config(state="normal")
    except Exception:
        pass

    try:
        if is_prefix:
            new_lines = lines[len(prev_lines):]
            if new_lines:
                testo = ("\n" + "\n".join(new_lines)) if prev_lines else "\n".join(new_lines)
                widget.insert("end", testo)
            if sel_ranges:
                try:
                    widget.tag_remove("sel", "1.0", "end")
                    for i in range(0, len(sel_ranges), 2):
                        widget.tag_add("sel", sel_ranges[i], sel_ranges[i + 1])
                except Exception:
                    pass
        elif insertion_at is not None:
            # Inserimento fra un gruppo CLI e gli eventi successivi. Tk conserva
            # selezione e posizione delle righe già presenti, anche se scorri indietro.
            widget.insert(f"{insertion_at + 1}.0", "\n".join(
                lines[insertion_at:insertion_at + insertion_count]) + "\n")
        else:
            widget.delete("1.0", "end")
            if lines:
                widget.insert("1.0", "\n".join(lines))
            if not was_at_bottom:
                try:
                    if old_top_index is not None:
                        widget.yview(old_top_index)
                    else:
                        widget.yview_moveto(old_top)
                except Exception:
                    pass
    finally:
        if was_disabled:
            try:
                widget.config(state="disabled")
            except Exception:
                pass

    if was_at_bottom and lines:
        try:
            widget.see("end")
            if prev_lines is None and hasattr(widget, "after_idle"):
                widget.after_idle(lambda: widget.see("end"))
        except Exception:
            pass

    widget._history_lines = list(lines)
