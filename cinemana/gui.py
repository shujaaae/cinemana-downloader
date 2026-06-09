"""Tkinter GUI for the Cinemana downloader.

Threading model (critical so the window never freezes):
* The Tk event loop runs on the main thread.
* Background worker threads (managed by the service) run the downloads.
* Workers never touch widgets; they push events onto a ``queue.Queue``.
* The UI drains that queue every 100 ms via ``root.after`` and updates widgets.
* The UI may call the service's thread-safe ``request_pause/resume/cancel``
  directly (they only flip events / nudge a queue).

Features: season-grouped checkbox tree (pick episodes), per-file pause/resume,
live speed + ETA, concurrency/segments settings (IDM-style), and a live
Arabic/English language switch (top-right dropdown, persisted across launches).
Every user-facing string flows through :mod:`cinemana.i18n`, so switching the
language re-renders the whole window in place via ``_retranslate``.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .i18n import get_language, set_language, t
from .manifest import Manifest
from .rate import format_eta, human_speed
from .service import (
    DEFAULT_CONCURRENCY, DEFAULT_SEGMENTS, MAX_CONNECTIONS, DownloadService,
    Events, SeriesPlan, height_label,
)
from .session import load_session, plan_from_dict, plan_to_dict, save_session
from .settings import load_settings, save_settings

POLL_MS = 100

CHECK_ON = "☑"
CHECK_OFF = "☐"
CHECK_PARTIAL = "◪"

# Treeview columns and their i18n heading keys (reused by build + retranslate).
COLS = ("title", "status", "progress", "speed", "eta", "size")
COL_KEYS = {
    "title": "col_title", "status": "col_status", "progress": "col_progress",
    "speed": "col_speed", "eta": "col_eta", "size": "col_size",
}


def _human_size(n) -> str:
    if not n:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.0f} {u}" if u == "B" else f"{f:.1f} {u}"
        f /= 1024
    return f"{n}"


class SegmentBar(tk.Canvas):
    """IDM-style segmented progress bar.

    Draws one block per download segment; each block fills left->right (red over
    grey) as that segment's bytes arrive. The hot path (:meth:`set_segment`) only
    resizes the one changed block's fill rect — a full redraw happens solely on
    resize / :meth:`set_segments`, which keeps 32 blocks flicker-free.
    """

    BG = "#e6e6e6"      # remaining (track)
    DONE = "#c62828"    # downloaded
    BORDER = "#bdbdbd"  # block separators
    GAP = 2             # px between blocks
    MIN_BLOCK = 3       # px, so 32 blocks stay visible

    def __init__(self, parent, height=16, **kw):
        super().__init__(parent, height=height, highlightthickness=1,
                         highlightbackground=self.BORDER, bg=self.BG, **kw)
        self.seg_totals: list[int] = []
        self.seg_done: list[int] = []
        self._blocks: list[dict] = []  # per block: {bg, fill, x0, w, total}
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_segments(self, seg_totals: list[int]) -> None:
        self.seg_totals = [max(0, int(t)) for t in seg_totals] or [0]
        self.seg_done = [0] * len(self.seg_totals)
        self._redraw()

    def set_segment(self, k: int, done: int) -> None:
        if 0 <= k < len(self.seg_done):
            self.seg_done[k] = max(0, int(done))
            self._redraw_block(k)

    def mark_full(self) -> None:
        self.seg_done = [t for t in self.seg_totals]
        self._redraw()

    # -- drawing --------------------------------------------------------------

    def _layout(self):
        """Return per-block (x0, width) given the current canvas width."""
        w = self.winfo_width()
        n = len(self.seg_totals)
        if w <= 1 or n == 0:
            return []
        usable = max(n, w - self.GAP * (n - 1))
        known = sum(self.seg_totals)
        widths: list[int] = []
        if known > 0:
            acc = 0
            for t in self.seg_totals[:-1]:
                bw = max(self.MIN_BLOCK, int(usable * t / known))
                widths.append(bw)
                acc += bw
            widths.append(max(self.MIN_BLOCK, usable - acc))  # last absorbs rounding
        else:
            base = usable // n
            widths = [base] * (n - 1) + [usable - base * (n - 1)]
        out = []
        x = 0
        for bw in widths:
            out.append((x, bw))
            x += bw + self.GAP
        return out

    def _redraw(self) -> None:
        self.delete("all")
        self._blocks = []
        layout = self._layout()
        if not layout:
            return
        h = self.winfo_height()
        for k, (x0, bw) in enumerate(layout):
            total = self.seg_totals[k]
            bg = self.create_rectangle(x0, 0, x0 + bw, h, fill=self.BG, width=0)
            fw = int(bw * self.seg_done[k] / total) if total else 0
            fill = self.create_rectangle(x0, 0, x0 + fw, h, fill=self.DONE, width=0)
            self._blocks.append({"bg": bg, "fill": fill, "x0": x0, "w": bw, "total": total})

    def _redraw_block(self, k: int) -> None:
        if k >= len(self._blocks):
            return
        b = self._blocks[k]
        h = self.winfo_height()
        total = b["total"]
        fw = int(b["w"] * self.seg_done[k] / total) if total else 0
        self.coords(b["fill"], b["x0"], 0, b["x0"] + fw, h)


class CinemanaGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(t("app_title"))
        self.root.geometry("1040x680")
        self.root.minsize(880, 540)

        self.q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.service: DownloadService | None = None
        self.plan: SeriesPlan | None = None

        # Tree bookkeeping.
        self.row_by_nb: dict[str, str] = {}     # episode nb -> tree item id
        self.nb_by_item: dict[str, str] = {}     # tree item id -> episode nb
        self.checked: dict[str, bool] = {}       # episode item id -> checked
        self.item_label: dict[str, str] = {}     # item id -> text without glyph
        self.season_of_item: dict[str, str] = {} # episode item id -> season item id
        self.season_children: dict[str, list[str]] = {}
        self.status_of_nb: dict[str, str] = {}   # episode nb -> status *key* (for retranslate)
        self.item_kind: dict[str, tuple] = {}    # item id -> ("season", n) | ("movie",)

        # Active-downloads panel: one IDM-style segmented bar per downloading nb.
        self.seg_bars: dict[str, dict] = {}      # nb -> {row, bar, label, info_var}

        # i18n: registry of static (widget, key) pairs + the once-built entry menus.
        self._i18n_widgets: list[tuple[tk.Misc, str]] = []
        self._entry_menus: list[tk.Menu] = []

        self.dest_var = tk.StringVar(value=str(Path.cwd()))
        self.url_var = tk.StringVar()
        self.quality_var = tk.StringVar()
        self.conc_var = tk.IntVar(value=DEFAULT_CONCURRENCY)
        self.seg_var = tk.IntVar(value=DEFAULT_SEGMENTS)
        self.conn_var = tk.StringVar()
        self.agg_var = tk.StringVar(value="")
        self.heights: list[int] = []

        self._build_ui()
        self._update_conn_label()
        self.root.after(POLL_MS, self._drain_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- i18n widget helpers --------------------------------------------------

    def _tr_label(self, parent, key, **kw) -> ttk.Label:
        w = ttk.Label(parent, text=t(key), **kw)
        self._i18n_widgets.append((w, key))
        return w

    def _tr_button(self, parent, key, **kw) -> ttk.Button:
        w = ttk.Button(parent, text=t(key), **kw)
        self._i18n_widgets.append((w, key))
        return w

    def _status_text(self, status: str) -> str:
        return t("status_" + status)

    # -- layout ---------------------------------------------------------------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # Language switcher (top-right). Display names stay each in their own
        # script and are intentionally NOT translated.
        langbar = ttk.Frame(self.root)
        langbar.pack(fill="x", padx=8, pady=(6, 0))
        self.lang_combo = ttk.Combobox(langbar, state="readonly", width=10,
                                       values=["العربية", "English"])
        self.lang_combo.set("English" if get_language() == "en" else "العربية")
        self.lang_combo.pack(side="right")
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language_change)

        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        self._tr_label(top, "lbl_url").grid(row=0, column=0, sticky="e")
        self.url_entry = ttk.Entry(top, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=1, columnspan=3, sticky="we", padx=4)
        self._tr_button(top, "btn_paste", width=6,
                        command=lambda: self._paste_into(self.url_entry)).grid(row=0, column=4, padx=2)
        self.fetch_btn = self._tr_button(top, "btn_fetch", command=self._on_fetch)
        self.fetch_btn.grid(row=0, column=5, padx=4)

        self._tr_label(top, "lbl_dest").grid(row=1, column=0, sticky="e")
        self.dest_entry = ttk.Entry(top, textvariable=self.dest_var)
        self.dest_entry.grid(row=1, column=1, columnspan=2, sticky="we", padx=4)
        self._tr_button(top, "btn_browse", command=self._on_browse).grid(row=1, column=3, padx=4)
        self._tr_label(top, "lbl_quality").grid(row=1, column=4, sticky="e")
        self.quality_combo = ttk.Combobox(top, textvariable=self.quality_var,
                                           state="readonly", width=9)
        self.quality_combo.grid(row=1, column=5, padx=4)

        # IDM-style settings: concurrency (N) and segments per file (M).
        self._tr_label(top, "lbl_concurrency").grid(row=2, column=0, sticky="e")
        sp1 = ttk.Spinbox(top, from_=1, to=6, width=4, textvariable=self.conc_var,
                          command=self._update_conn_label)
        sp1.grid(row=2, column=1, sticky="w", padx=4)
        self._tr_label(top, "lbl_segments").grid(row=2, column=2, sticky="e")
        sp2 = ttk.Spinbox(top, from_=1, to=32, width=4, textvariable=self.seg_var,
                          command=self._update_conn_label)
        sp2.grid(row=2, column=3, sticky="w", padx=4)
        ttk.Label(top, textvariable=self.conn_var).grid(row=2, column=4, columnspan=2, sticky="w")

        top.columnconfigure(1, weight=1)
        top.columnconfigure(2, weight=1)

        self.url_entry.bind("<Control-KeyPress>", lambda e: self._entry_ctrl(e, self.url_entry))
        self.dest_entry.bind("<Control-KeyPress>", lambda e: self._entry_ctrl(e, self.dest_entry))
        self._attach_entry_menu(self.url_entry)
        self._attach_entry_menu(self.dest_entry)

        # Series title + selection toolbar.
        self.title_var = tk.StringVar(value=t("no_series_yet"))
        ttk.Label(self.root, textvariable=self.title_var,
                  font=("Segoe UI", 11, "bold")).pack(fill="x", padx=8)

        seltools = ttk.Frame(self.root)
        seltools.pack(fill="x", padx=8)
        self._tr_label(seltools, "lbl_selection").pack(side="left")
        self._tr_button(seltools, "btn_select_all",
                        command=lambda: self._select_all(True)).pack(side="left", padx=2)
        self._tr_button(seltools, "btn_select_none",
                        command=lambda: self._select_all(False)).pack(side="left", padx=2)
        self._tr_button(seltools, "btn_invert",
                        command=self._invert_selection).pack(side="left", padx=2)
        self._tr_label(seltools, "hint_selection").pack(side="left")

        # Episodes tree (season-grouped, with checkboxes).
        table_frame = ttk.Frame(self.root)
        table_frame.pack(fill="both", expand=True, padx=8, pady=4)
        self.tree = ttk.Treeview(table_frame, columns=COLS, show="tree headings", height=14)
        self.tree.heading("#0", text=t("col_series_episode"))
        self.tree.column("#0", width=230, anchor="w")
        widths = {"title": 250, "status": 110, "progress": 80, "speed": 95, "eta": 80, "size": 130}
        for c in COLS:
            self.tree.heading(c, text=t(COL_KEYS[c]))
            self.tree.column(c, width=widths[c], anchor="center" if c != "title" else "w")
        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Button-3>", self._on_tree_rightclick)

        # Active-downloads panel: one segmented bar per concurrent download.
        # A scrollable inner frame so many concurrent bars never steal tree space.
        self.active_frame = ttk.LabelFrame(self.root, text=t("active_downloads"))
        self.active_frame.pack(fill="x", padx=8, pady=2)
        self.active_canvas = tk.Canvas(self.active_frame, height=96,
                                       highlightthickness=0)
        asb = ttk.Scrollbar(self.active_frame, orient="vertical",
                            command=self.active_canvas.yview)
        self.active_inner = ttk.Frame(self.active_canvas)
        self._active_win = self.active_canvas.create_window(
            (0, 0), window=self.active_inner, anchor="nw")
        self.active_canvas.configure(yscrollcommand=asb.set)
        self.active_canvas.pack(side="left", fill="x", expand=True)
        asb.pack(side="right", fill="y")
        self.active_inner.bind(
            "<Configure>",
            lambda _e: self.active_canvas.configure(
                scrollregion=self.active_canvas.bbox("all")))
        self.active_canvas.bind(
            "<Configure>",
            lambda e: self.active_canvas.itemconfigure(self._active_win, width=e.width))

        # Overall progress + speed + controls.
        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=8, pady=4)
        self.overall = ttk.Progressbar(bottom, mode="determinate")
        self.overall.pack(side="left", fill="x", expand=True, padx=4)
        self.start_btn = self._tr_button(bottom, "btn_start", command=self._on_start, state="disabled")
        self.start_btn.pack(side="left", padx=4)
        self.pause_btn = self._tr_button(bottom, "btn_pause", command=self._on_pause_all, state="disabled")
        self.pause_btn.pack(side="left", padx=4)
        self.resume_btn = self._tr_button(bottom, "btn_resume", command=self._on_resume_all, state="disabled")
        self.resume_btn.pack(side="left", padx=4)
        self.stop_btn = self._tr_button(bottom, "btn_stop", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)

        ttk.Label(self.root, textvariable=self.agg_var,
                  font=("Segoe UI", 10)).pack(fill="x", padx=12)

        # Log box.
        self.log_frame = ttk.LabelFrame(self.root, text=t("log_panel_title"))
        self.log_frame.pack(fill="both", expand=False, padx=8, pady=4)
        self.log = tk.Text(self.log_frame, height=6, wrap="word", state="disabled")
        log_vsb = ttk.Scrollbar(self.log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_vsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_vsb.pack(side="right", fill="y")

    # -- language switch ------------------------------------------------------

    def _on_language_change(self, _evt=None):
        lang = "en" if self.lang_combo.get() == "English" else "ar"
        if lang == get_language():
            return
        set_language(lang)
        save_settings({"language": lang})
        self._retranslate()

    def _retranslate(self):
        """Re-render every visible string in the active language, in place."""
        self.root.title(t("app_title"))
        for w, key in self._i18n_widgets:
            w.configure(text=t(key))

        # Tree headings.
        self.tree.heading("#0", text=t("col_series_episode"))
        for c in COLS:
            self.tree.heading(c, text=t(COL_KEYS[c]))
        self.log_frame.configure(text=t("log_panel_title"))
        self.active_frame.configure(text=t("active_downloads"))
        for nb, entry in self.seg_bars.items():
            entry["label"].configure(text=self._seg_label(nb))

        # Entry context menus (built once; rows 0,1,2 + 4, index 3 is a separator).
        for menu in self._entry_menus:
            menu.entryconfigure(0, label=t("menu_cut"))
            menu.entryconfigure(1, label=t("menu_copy"))
            menu.entryconfigure(2, label=t("menu_paste"))
            menu.entryconfigure(4, label=t("menu_select_all"))

        # Dynamic labels.
        if self.plan:
            kind = (t("kind_movie") if self.plan.is_movie
                    else t("kind_episodes", n=len(self.plan.episodes)))
            self.title_var.set(t("title_with_kind", title=self.plan.title, kind=kind))
        else:
            self.title_var.set(t("no_series_yet"))
        self._update_conn_label()
        # agg_var self-heals on the next rate tick; leave as-is.

        # Tree season/movie labels.
        for item, kind in self.item_kind.items():
            if kind[0] == "season":
                self.item_label[item] = t("season_label", n=kind[1])
                self._refresh_season(item)
            elif kind[0] == "movie":
                self.item_label[item] = t("movie_label")
                self._render_item(item, self._glyph(self.checked.get(item, False)))

        # Tree status cells.
        for nb, st in self.status_of_nb.items():
            item = self.row_by_nb.get(nb)
            if item:
                self.tree.set(item, "status", self._status_text(st))

    # -- clipboard / entry helpers (Arabic-keyboard-safe paste) ---------------

    def _attach_entry_menu(self, entry: tk.Entry):
        menu = tk.Menu(entry, tearoff=0)
        menu.add_command(label=t("menu_cut"), command=lambda: entry.event_generate("<<Cut>>"))
        menu.add_command(label=t("menu_copy"), command=lambda: entry.event_generate("<<Copy>>"))
        menu.add_command(label=t("menu_paste"), command=lambda: self._paste_into(entry))
        menu.add_separator()
        menu.add_command(label=t("menu_select_all"), command=lambda: self._select_all_entry(entry))
        self._entry_menus.append(menu)

        def popup(e):
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()
            return "break"

        entry.bind("<Button-3>", popup)

    def _paste_into(self, entry: tk.Entry):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return
        try:
            entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        entry.insert("insert", text.strip())

    def _select_all_entry(self, entry: tk.Entry):
        entry.select_range(0, "end")
        entry.icursor("end")

    def _entry_ctrl(self, e, entry: tk.Entry):
        # keycode-based so shortcuts work under a non-Latin keyboard layout
        # (the usual reason Ctrl+V "doesn't paste" on Arabic layouts).
        kc = getattr(e, "keycode", 0)
        if kc == 86:    # V
            self._paste_into(entry)
            return "break"
        if kc == 67:    # C
            entry.event_generate("<<Copy>>")
            return "break"
        if kc == 88:    # X
            entry.event_generate("<<Cut>>")
            return "break"
        if kc == 65:    # A
            self._select_all_entry(entry)
            return "break"
        return None

    # -- settings -------------------------------------------------------------

    def _update_conn_label(self):
        try:
            n = max(1, int(self.conc_var.get()))
            m = max(1, int(self.seg_var.get()))
        except (tk.TclError, ValueError):
            return
        total = n * m
        text = t("conn_label", n=n, m=m, total=total)
        if total > MAX_CONNECTIONS:
            text += t("conn_cap_warn", max=MAX_CONNECTIONS)
        self.conn_var.set(text)

    # -- fetch ----------------------------------------------------------------

    def _on_browse(self):
        folder = filedialog.askdirectory(initialdir=self.dest_var.get() or str(Path.cwd()))
        if folder:
            self.dest_var.set(folder)

    def _on_fetch(self):
        raw = self.url_var.get().strip()
        if not raw:
            messagebox.showwarning(t("app_title"), t("dlg_paste_url_first"))
            return
        self.fetch_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")
        self.resume_btn.configure(state="disabled")
        self._log(t("log_fetching_series"))
        threading.Thread(target=self._fetch_worker, args=(raw,), daemon=True).start()

    def _fetch_worker(self, raw: str):
        try:
            # Wire a log-only Events so prepare()'s progress messages are shown.
            events = Events(on_log=lambda msg: self.q.put(("log", msg)))
            service = DownloadService(Path(self.dest_var.get() or "."), events=events)
            plan = service.prepare(raw)
            self.q.put(("plan", plan))
        except Exception as exc:  # noqa: BLE001
            self.q.put(("fetch_error", str(exc)))

    # -- start / stop ---------------------------------------------------------

    def _on_start(self):
        if not self.plan:
            return
        dest = self.dest_var.get().strip()
        if not dest:
            messagebox.showwarning(t("app_title"), t("dlg_choose_dest"))
            return
        selected = self._selected_nbs()
        if not selected:
            messagebox.showwarning(t("app_title"), t("dlg_no_episode"))
            return
        height = self._selected_height()
        n = max(1, int(self.conc_var.get()))
        m = max(1, int(self.seg_var.get()))
        self._save_session()
        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.fetch_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.pause_btn.configure(state="normal")
        self.resume_btn.configure(state="normal")
        self.overall.configure(maximum=max(1, len(selected)), value=0)
        self._seg_clear()
        self._log(t("log_start_run", n=len(selected), q=height_label(height),
                    nn=n, m=m, dest=dest))

        events = Events(
            on_log=lambda msg: self.q.put(("log", msg)),
            on_status=lambda nb, st, extra: self.q.put(("status", (nb, st, extra))),
            on_progress=lambda nb, d, tot: self.q.put(("progress", (nb, d, tot))),
            on_segments=lambda nb, tots: self.q.put(("segments", (nb, tots))),
            on_segment_progress=lambda nb, k, d: self.q.put(("seg_progress", (nb, k, d))),
            on_rate=lambda nb, sp, eta: self.q.put(("rate", (nb, sp, eta))),
            on_series_done=lambda s: self.q.put(("done", s)),
        )
        self.service = DownloadService(Path(dest), events=events)
        self.worker = threading.Thread(
            target=self._download_worker,
            args=(self.plan, height, selected, n, m), daemon=True)
        self.worker.start()

    def _download_worker(self, plan, height, selected, n, m):
        try:
            self.service.run(plan, height, selected_nbs=selected,
                             should_stop=self.stop_event.is_set,
                             concurrency=n, segments=m)
        except Exception as exc:  # noqa: BLE001
            self.q.put(("log", t("log_unexpected_stop", err=exc)))
            self.q.put(("done", None))

    def _on_stop(self):
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")
        self._log(t("log_stopping"))

    def _running(self) -> bool:
        return bool(self.service and self.worker and self.worker.is_alive())

    def _on_pause_all(self):
        if self._running():
            self.service.request_pause_all()
            self._log(t("log_pause_all"))
            self._save_session()

    def _on_resume_all(self):
        if self._running():
            self.service.request_resume_all()
            self._log(t("log_resume_all"))
        elif self.plan:
            # Idle (e.g. just reopened): "Continue" == start, which resumes every
            # unfinished episode from its .part offset on disk.
            self._on_start()
        self._save_session()

    def _set_resume_enabled(self, on: bool):
        self.resume_btn.configure(state="normal" if on else "disabled")

    def _on_close(self):
        self._save_session()
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(t("app_title"), t("dlg_close_while_downloading")):
                return
            self.stop_event.set()
        self.root.destroy()

    # -- selection tree -------------------------------------------------------

    def _glyph(self, checked: bool) -> str:
        return CHECK_ON if checked else CHECK_OFF

    def _render_item(self, item: str, glyph: str):
        self.tree.item(item, text=f"{glyph} {self.item_label.get(item, '')}")

    def _refresh_season(self, season_item: str):
        kids = self.season_children.get(season_item, [])
        if not kids:
            return
        on = sum(1 for k in kids if self.checked.get(k))
        glyph = CHECK_ON if on == len(kids) else (CHECK_OFF if on == 0 else CHECK_PARTIAL)
        self._render_item(season_item, glyph)

    def _toggle_episode(self, item: str):
        self.checked[item] = not self.checked.get(item, False)
        self._render_item(item, self._glyph(self.checked[item]))
        season = self.season_of_item.get(item)
        if season:
            self._refresh_season(season)
        self._save_session()

    def _toggle_season(self, season_item: str):
        kids = self.season_children.get(season_item, [])
        target = not all(self.checked.get(k) for k in kids)
        for k in kids:
            self.checked[k] = target
            self._render_item(k, self._glyph(target))
        self._refresh_season(season_item)
        self._save_session()

    def _select_all(self, value: bool):
        for item in self.nb_by_item:
            self.checked[item] = value
            self._render_item(item, self._glyph(value))
        for s in self.season_children:
            self._refresh_season(s)
        self._save_session()

    def _invert_selection(self):
        for item in self.nb_by_item:
            self.checked[item] = not self.checked.get(item, False)
            self._render_item(item, self._glyph(self.checked[item]))
        for s in self.season_children:
            self._refresh_season(s)
        self._save_session()

    def _selected_nbs(self) -> set[str]:
        return {nb for item, nb in self.nb_by_item.items() if self.checked.get(item)}

    def _on_tree_click(self, event):
        if self.tree.identify_element(event.x, event.y) == "Treeitem.indicator":
            return  # clicking the expand arrow: let it expand/collapse
        item = self.tree.identify_row(event.y)
        if not item:
            return
        if item in self.nb_by_item:
            self._toggle_episode(item)
        elif item in self.season_children:
            self._toggle_season(item)

    def _on_tree_rightclick(self, event):
        item = self.tree.identify_row(event.y)
        if not item or item not in self.nb_by_item or self.service is None:
            return
        nb = self.nb_by_item[item]
        self.tree.selection_set(item)
        menu = tk.Menu(self.tree, tearoff=0)
        menu.add_command(label=t("menu_pause"), command=lambda: self.service.request_pause(nb))
        menu.add_command(label=t("menu_resume"), command=lambda: self.service.request_resume(nb))
        menu.add_command(label=t("menu_cancel"), command=lambda: self.service.request_cancel(nb))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # -- queue draining (runs on UI thread) -----------------------------------

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                self._handle_event(kind, payload)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._drain_queue)

    def _handle_event(self, kind: str, payload):
        if kind == "plan":
            self._apply_plan(payload)
        elif kind == "fetch_error":
            self.fetch_btn.configure(state="normal")
            self._log(t("log_fetch_failed", err=payload))
            messagebox.showerror(t("app_title"), t("dlg_fetch_failed", err=payload))
        elif kind == "log":
            self._log(payload)
        elif kind == "status":
            nb, st, extra = payload
            self._set_row_status(nb, st, extra)
        elif kind == "progress":
            nb, done, total = payload
            self._set_row_progress(nb, done, total)
        elif kind == "segments":
            nb, tots = payload
            self._seg_add_or_replace(nb, tots)
        elif kind == "seg_progress":
            nb, k, done = payload
            self._seg_update(nb, k, done)
        elif kind == "rate":
            nb, speed, eta = payload
            self._set_rate(nb, speed, eta)
        elif kind == "done":
            self._on_done(payload)

    def _apply_plan(self, plan: SeriesPlan, selected_nbs=None, restore_status=False):
        self.plan = plan
        self.heights = plan.available_heights or [1080, 720, 480, 360, 240]
        kind = t("kind_movie") if plan.is_movie else t("kind_episodes", n=len(plan.episodes))
        self.title_var.set(t("title_with_kind", title=plan.title, kind=kind))
        labels = [height_label(h) for h in self.heights]
        self.quality_combo.configure(values=labels)
        self.quality_var.set(height_label(plan.default_height))

        self.tree.delete(*self.tree.get_children())
        self.row_by_nb.clear()
        self.nb_by_item.clear()
        self.checked.clear()
        self.item_label.clear()
        self.season_of_item.clear()
        self.season_children.clear()
        self.status_of_nb.clear()
        self.item_kind.clear()

        season_items: dict[int, str] = {}
        for ep in plan.episodes:
            if ep.is_movie:
                label = t("movie_label")
                parent = ""
            else:
                if ep.season not in season_items:
                    s_item = self.tree.insert("", "end", open=True)
                    self.item_label[s_item] = t("season_label", n=ep.season)
                    self.item_kind[s_item] = ("season", ep.season)
                    self.season_children[s_item] = []
                    self._render_item(s_item, CHECK_ON)
                    season_items[ep.season] = s_item
                parent = season_items[ep.season]
                label = f"S{ep.season:02d}E{ep.episode:02d}"

            item = self.tree.insert(parent, "end",
                                    values=(ep.title, self._status_text("pending"), "0%", "", "", ""))
            if ep.is_movie:
                self.item_kind[item] = ("movie",)
            self.item_label[item] = label
            chk = selected_nbs is None or ep.nb in selected_nbs
            self._render_item(item, self._glyph(chk))
            self.checked[item] = chk
            self.row_by_nb[ep.nb] = item
            self.nb_by_item[item] = ep.nb
            self.status_of_nb[ep.nb] = "pending"
            if parent:
                self.season_of_item[item] = parent
                self.season_children[parent].append(item)

        for s_item in season_items.values():
            self._refresh_season(s_item)

        self.overall.configure(maximum=max(1, len(plan.episodes)), value=0)
        self.agg_var.set("")
        self.fetch_btn.configure(state="normal")
        self.start_btn.configure(state="normal")
        self._set_resume_enabled(True)
        if restore_status:
            self._restore_statuses(plan)
        else:
            self._log(t("log_fetched_ready", title=plan.title, kind=kind))
            self._save_session()

    def _restore_statuses(self, plan: SeriesPlan):
        """Overlay saved per-episode status + % from the manifest at dest (offline).

        Partially-downloaded episodes are shown as *paused* with their percentage
        ("as if I paused it"), so the user sees exactly where each file stopped.
        """
        try:
            m = Manifest.load(Path(self.dest_var.get()))
        except Exception:  # noqa: BLE001 - never block restore on manifest issues
            return
        for ep in plan.episodes:
            rec = m.episode(plan.series_id, ep.nb)
            if not rec:
                continue
            status = rec.get("status", "pending")
            done = rec.get("downloaded_bytes") or 0
            total = rec.get("total_bytes")
            if status == "done":
                self._set_row_status(ep.nb, "done", {"size": total})
            elif status == "error":
                self._set_row_status(ep.nb, "error", {})
            elif done > 0 or status in ("paused", "downloading"):
                self._set_row_status(ep.nb, "paused", {})
                if done > 0:
                    self._set_row_progress(ep.nb, done, total)
            # else: pending with 0 bytes -> leave the default pending / 0%

    def _save_session(self):
        """Persist the current form inputs + selection + plan (resume snapshot)."""
        try:
            conc = int(self.conc_var.get())
        except (tk.TclError, ValueError):
            conc = DEFAULT_CONCURRENCY
        try:
            seg = int(self.seg_var.get())
        except (tk.TclError, ValueError):
            seg = DEFAULT_SEGMENTS
        save_session({
            "url": self.url_var.get(),
            "dest": self.dest_var.get(),
            "quality_height": self._selected_height() if self.plan else None,
            "concurrency": conc,
            "segments": seg,
            "selected_nbs": sorted(self._selected_nbs()),
            "plan": plan_to_dict(self.plan) if self.plan else None,
        })

    def restore_session(self):
        """Restore the last session into the UI (offline). Safe to call once at start."""
        try:
            data = load_session()
            if not data:
                return
            if data.get("url"):
                self.url_var.set(data["url"])
            if data.get("dest"):
                self.dest_var.set(data["dest"])
            if data.get("concurrency"):
                self.conc_var.set(int(data["concurrency"]))
            if data.get("segments"):
                self.seg_var.set(int(data["segments"]))
            self._update_conn_label()
            plan = plan_from_dict(data.get("plan")) if data.get("plan") else None
            if plan:
                sel = set(data.get("selected_nbs") or [])
                self._apply_plan(plan, selected_nbs=sel or None, restore_status=True)
                qh = data.get("quality_height")
                if qh and qh in self.heights:
                    self.quality_var.set(height_label(qh))
                self._set_resume_enabled(True)
                self._log(t("log_session_restored"))
        except Exception:  # noqa: BLE001 - degrade to a blank UI, never crash on launch
            pass

    def _selected_height(self) -> int:
        label = self.quality_var.get()
        for h in self.heights:
            if height_label(h) == label:
                return h
        return self.plan.default_height if self.plan else 1080

    def _set_row_status(self, nb: str, status: str, extra: dict):
        self.status_of_nb[nb] = status
        item = self.row_by_nb.get(nb)
        if not item:
            return
        self.tree.set(item, "status", self._status_text(status))
        if status == "done":
            self.tree.set(item, "progress", "100%")
            self.tree.set(item, "speed", "")
            self.tree.set(item, "eta", "")
            if extra.get("size"):
                self.tree.set(item, "size", _human_size(extra["size"]))
            self._bump_overall()
            self._seg_remove(nb)
            self.tree.see(item)
        elif status == "downloading":
            self.tree.see(item)
        elif status == "paused":
            self.tree.set(item, "speed", "")
            self.tree.set(item, "eta", "")
            self._seg_remove(nb)  # resume re-creates the bar via a fresh on_segments
        elif status == "error":
            self.tree.set(item, "progress", "—")
            self.tree.set(item, "speed", "")
            self.tree.set(item, "eta", "")
            self._seg_remove(nb)

    def _set_row_progress(self, nb: str, done: int, total):
        item = self.row_by_nb.get(nb)
        if not item:
            return
        if total:
            pct = min(100, int(done * 100 / total))
            self.tree.set(item, "progress", f"{pct}%")
            self.tree.set(item, "size", f"{_human_size(done)} / {_human_size(total)}")
        else:
            self.tree.set(item, "size", _human_size(done))

    def _set_rate(self, nb, speed, eta):
        if nb is None:
            sp = human_speed(speed)
            et = format_eta(eta)
            parts = []
            if sp:
                parts.append(t("agg_speed", sp=sp))
            if et:
                parts.append(t("agg_eta", et=et))
            self.agg_var.set("   •   ".join(parts))
            return
        item = self.row_by_nb.get(nb)
        if not item:
            return
        self.tree.set(item, "speed", human_speed(speed))
        self.tree.set(item, "eta", format_eta(eta))
        self._seg_set_speed(nb, speed)

    # -- active-downloads segmented bars --------------------------------------

    def _seg_label(self, nb: str) -> str:
        item = self.row_by_nb.get(nb)
        return self.item_label.get(item, nb) if item else nb

    def _seg_add_or_replace(self, nb: str, seg_totals: list[int]) -> None:
        existing = self.seg_bars.get(nb)
        if existing:
            existing["label"].configure(text=self._seg_label(nb))
            existing["bar"].set_segments(seg_totals)
            return
        row = ttk.Frame(self.active_inner)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Label(row, text=self._seg_label(nb), width=10).pack(side="left")
        info_var = tk.StringVar(value="")
        ttk.Label(row, textvariable=info_var, width=12, anchor="e").pack(side="right")
        bar = SegmentBar(row)
        bar.pack(side="left", fill="x", expand=True, padx=4)
        bar.set_segments(seg_totals)
        self.seg_bars[nb] = {"row": row, "bar": bar,
                             "label": row.winfo_children()[0], "info_var": info_var}

    def _seg_update(self, nb: str, k: int, done: int) -> None:
        entry = self.seg_bars.get(nb)
        if entry is None:
            return
        entry["bar"].set_segment(k, done)

    def _seg_set_speed(self, nb: str, speed) -> None:
        entry = self.seg_bars.get(nb)
        if entry is None:
            return
        entry["info_var"].set(human_speed(speed))

    def _seg_remove(self, nb: str) -> None:
        entry = self.seg_bars.pop(nb, None)
        if entry is not None:
            entry["row"].destroy()

    def _seg_clear(self) -> None:
        for nb in list(self.seg_bars):
            self._seg_remove(nb)

    def _bump_overall(self):
        self.overall.configure(value=min(self.overall["maximum"], self.overall["value"] + 1))

    def _on_done(self, summary):
        self.start_btn.configure(state="normal")
        self.fetch_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.pause_btn.configure(state="disabled")
        self._set_resume_enabled(bool(self.plan))
        self.agg_var.set("")
        self._seg_clear()
        if summary:
            self._log(t(
                "log_run_summary",
                done=summary.get("done", 0), total=summary.get("total", 0),
                error=summary.get("error", 0), paused=summary.get("paused", 0),
                pending=summary.get("pending", 0),
            ))
            done = summary.get("done", 0)
            total = summary.get("total", 0)
            if done >= total and total:
                messagebox.showinfo(t("app_title"), t("dlg_all_done"))

    def _log(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.log.see("end")
        self.log.configure(state="disabled")


def run():
    set_language(load_settings().get("language", "ar"))
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    gui = CinemanaGUI(root)
    gui.restore_session()
    root.mainloop()
