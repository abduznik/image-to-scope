"""
gui.py

Tkinter front end for Image to Scope.

  * Browse for an image (PNG, JPEG, WebP, SVG, ...) - it is processed right
    away into an oscilloscope WAV and a scope preview PNG.
  * Pick a folder and custom names, then save both files.
  * The history sidebar lists everything processed so far with the source
    image, the scope preview and the WAV, which can be played back.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import backend
from backend import HistoryStore, ScopeError, ScopeSettings

SIDEBAR_WIDTH = 330
THUMB_SIZE = 72
POLL_MS = 100


def open_in_file_manager(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opening a folder the user chose
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ------------------------------------------------------------ widgets --
class ImagePanel(ttk.LabelFrame):
    """A titled canvas that shows a PIL image scaled to fit."""

    def __init__(self, master, title: str, background: str, placeholder: str):
        super().__init__(master, text=title, padding=4)
        self.placeholder = placeholder
        self.canvas = tk.Canvas(self, background=background,
                                highlightthickness=0, width=320, height=320)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())
        self._image: Image.Image | None = None
        self._photo = None
        self._redraw()

    def set_image(self, image: Image.Image | None, placeholder: str | None = None):
        self._image = image
        if placeholder is not None:
            self.placeholder = placeholder
        self._redraw()

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        w, h = max(c.winfo_width(), 2), max(c.winfo_height(), 2)
        if self._image is None:
            c.create_text(w / 2, h / 2, text=self.placeholder, fill="#8a8a8a",
                          font=("Segoe UI", 11), width=w - 20, justify="center")
            return
        img = self._image.copy()
        img.thumbnail((max(w - 8, 1), max(h - 8, 1)), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(img)
        c.create_image(w / 2, h / 2, image=self._photo, anchor="center")


class ScrollableFrame(ttk.Frame):
    """A vertically scrolling frame (the history list)."""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical",
                                       command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner,
                                                 anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.inner.bind("<Configure>", lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self._window, width=e.width))
        self.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self.bind("<Leave>", lambda _e: self._bind_wheel(False))

    def _bind_wheel(self, on: bool):
        if on:
            self.bind_all("<MouseWheel>", self._on_wheel)
            self.bind_all("<Button-4>", self._on_wheel)
            self.bind_all("<Button-5>", self._on_wheel)
        else:
            self.unbind_all("<MouseWheel>")
            self.unbind_all("<Button-4>")
            self.unbind_all("<Button-5>")

    def _on_wheel(self, event):
        if self.canvas.yview() == (0.0, 1.0):
            return
        if getattr(event, "num", None) == 4:
            step = -1
        elif getattr(event, "num", None) == 5:
            step = 1
        else:
            step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step, "units")


# --------------------------------------------------------------- app --
class ScopeApp:
    def __init__(self, root: tk.Tk, history: HistoryStore | None = None):
        self.root = root
        self.history = history or HistoryStore()
        self.player = backend.AudioPlayer()

        self.image_path: Path | None = None
        self.image: Image.Image | None = None
        self.entry: backend.HistoryEntry | None = None   # shown in main view
        self.playing_id: str | None = None
        self.busy = False
        self._results: queue.Queue = queue.Queue()
        self._card_widgets: dict[str, dict] = {}

        root.title(f"{backend.APP_NAME} v{backend.__version__}")
        self._set_icon()
        root.geometry("1280x800")
        root.minsize(980, 640)
        self._init_style()
        self._build()
        self._bind_keys()
        self.refresh_history()
        self._update_controls()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(POLL_MS, self._poll)

    # ---------------------------------------------------------- layout --
    def _set_icon(self):
        icon = backend.resource_path("assets/icon-256.png")
        try:
            self._icon = ImageTk.PhotoImage(Image.open(icon))
            self.root.iconphoto(True, self._icon)
        except (OSError, tk.TclError):
            pass  # running without the assets folder - keep the default icon

    def _init_style(self):
        style = ttk.Style(self.root)
        if sys.platform == "win32" and "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("CardName.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Muted.TLabel", foreground="#6b6b6b")
        style.configure("Card.TFrame", relief="groove", borderwidth=1)
        style.configure("Selected.TFrame", relief="solid", borderwidth=2)
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

    def _build(self):
        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True)

        # Sidebar ------------------------------------------------------
        sidebar = ttk.Frame(paned, padding=(8, 8, 4, 8), width=SIDEBAR_WIDTH)
        paned.add(sidebar, weight=0)
        head = ttk.Frame(sidebar)
        head.pack(fill="x")
        ttk.Label(head, text="History", style="Title.TLabel").pack(side="left")
        self.clear_btn = ttk.Button(head, text="Clear all", command=self.clear_history)
        self.clear_btn.pack(side="right")
        self.history_list = ScrollableFrame(sidebar, width=SIDEBAR_WIDTH)
        self.history_list.pack(fill="both", expand=True, pady=(6, 0))
        self.empty_label = ttk.Label(
            self.history_list.inner, style="Muted.TLabel", justify="center",
            text="Nothing here yet.\nBrowse for an image to get started.")

        # Main area ----------------------------------------------------
        main = ttk.Frame(paned, padding=(4, 8, 8, 8))
        paned.add(main, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky="ew")
        self.browse_btn = ttk.Button(top, text="Browse image…", style="Accent.TButton",
                                     command=self.browse_image)
        self.browse_btn.pack(side="left")
        self.path_var = tk.StringVar(value="No image selected")
        ttk.Label(top, textvariable=self.path_var, style="Muted.TLabel").pack(
            side="left", padx=10, fill="x", expand=True)

        panels = ttk.Frame(main)
        panels.grid(row=1, column=0, sticky="nsew", pady=8)
        panels.columnconfigure((0, 1), weight=1, uniform="panel")
        panels.rowconfigure(0, weight=1)
        self.source_panel = ImagePanel(
            panels, "Source image", "#f4f4f4",
            "Browse for an image (PNG, JPEG, WebP, SVG, …)")
        self.source_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self.preview_panel = ImagePanel(
            panels, "Scope preview", "#000000",
            "The oscilloscope preview appears here")
        self.preview_panel.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        self._build_settings(main).grid(row=2, column=0, sticky="ew")

        action = ttk.Frame(main)
        action.grid(row=3, column=0, sticky="ew", pady=8)
        self.process_btn = ttk.Button(action, text="Process", style="Accent.TButton",
                                      command=self.start_processing)
        self.process_btn.pack(side="left")
        self.progress = ttk.Progressbar(action, mode="indeterminate", length=160)
        self.progress.pack(side="left", padx=10)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(action, textvariable=self.status_var).pack(side="left", fill="x")

        self._build_export(main).grid(row=4, column=0, sticky="ew")

    def _build_settings(self, parent):
        box = ttk.LabelFrame(parent, text="Settings", padding=8)
        defaults = ScopeSettings()
        self.vars = {
            "sample_rate": tk.StringVar(value=str(defaults.sample_rate)),
            "points_per_frame": tk.StringVar(value=str(defaults.points_per_frame)),
            "loop_seconds": tk.StringVar(value=str(defaults.loop_seconds)),
            "canny_low": tk.StringVar(value=str(defaults.canny_low)),
            "canny_high": tk.StringVar(value=str(defaults.canny_high)),
            "min_contour_perimeter": tk.StringVar(
                value=str(int(defaults.min_contour_perimeter))),
        }
        specs = [
            ("Sample rate (Hz)", "sample_rate", None,
             ("44100", "48000", "96000", "192000")),
            ("Points per frame", "points_per_frame", (100, 50000, 100), None),
            ("Duration (s)", "loop_seconds", (0.5, 600, 0.5), None),
            ("Edge low", "canny_low", (0, 254, 5), None),
            ("Edge high", "canny_high", (1, 255, 5), None),
            ("Min stroke length", "min_contour_perimeter", (0, 5000, 5), None),
        ]
        for i, (label, key, spin, values) in enumerate(specs):
            row, col = divmod(i, 3)
            ttk.Label(box, text=label).grid(row=row, column=col * 2, sticky="w",
                                            padx=(0 if col == 0 else 16, 6), pady=2)
            if values:
                w = ttk.Combobox(box, textvariable=self.vars[key], values=values,
                                 width=10)
            else:
                lo, hi, inc = spin
                w = ttk.Spinbox(box, textvariable=self.vars[key], from_=lo, to=hi,
                                increment=inc, width=10)
            w.grid(row=row, column=col * 2 + 1, sticky="w", pady=2)
        self.refresh_var = tk.StringVar()
        ttk.Label(box, textvariable=self.refresh_var, style="Muted.TLabel").grid(
            row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Button(box, text="Reset defaults", command=self.reset_settings).grid(
            row=2, column=4, columnspan=2, sticky="e", pady=(4, 0))
        for var in self.vars.values():
            var.trace_add("write", lambda *_: self._update_refresh_label())
        self._update_refresh_label()
        return box

    def _build_export(self, parent):
        box = ttk.LabelFrame(parent, text="Save", padding=8)
        box.columnconfigure(1, weight=1)
        self.folder_var = tk.StringVar()
        self.wav_name_var = tk.StringVar()
        self.preview_name_var = tk.StringVar()

        ttk.Label(box, text="Folder").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(box, textvariable=self.folder_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(box, text="Choose…", command=self.choose_folder).grid(
            row=0, column=2, padx=(6, 0))
        ttk.Label(box, text="Sound file").grid(row=1, column=0, sticky="w", pady=4)
        self.wav_entry = ttk.Entry(box, textvariable=self.wav_name_var)
        self.wav_entry.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Label(box, text="Preview image").grid(row=2, column=0, sticky="w")
        self.preview_entry = ttk.Entry(box, textvariable=self.preview_name_var)
        self.preview_entry.grid(row=2, column=1, sticky="ew")

        buttons = ttk.Frame(box)
        buttons.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.play_btn = ttk.Button(buttons, text="▶ Play sound",
                                   command=self.toggle_play_current)
        self.play_btn.pack(side="left")
        self.save_btn = ttk.Button(buttons, text="Save files", style="Accent.TButton",
                                   command=self.save_files)
        self.save_btn.pack(side="right")
        self.open_btn = ttk.Button(buttons, text="Open folder",
                                   command=self.open_output_folder)
        self.open_btn.pack(side="right", padx=6)
        return box

    def _bind_keys(self):
        self.root.bind("<Control-o>", lambda _e: self.browse_image())
        self.root.bind("<Control-s>", lambda _e: self.save_files())
        self.root.bind("<F5>", lambda _e: self.start_processing())

    # -------------------------------------------------------- settings --
    def read_settings(self) -> ScopeSettings:
        def num(key, cast):
            raw = self.vars[key].get().strip()
            try:
                return cast(float(raw))
            except ValueError:
                raise ScopeError(f"'{raw}' is not a valid number.") from None

        settings = ScopeSettings(
            sample_rate=num("sample_rate", int),
            points_per_frame=num("points_per_frame", int),
            loop_seconds=num("loop_seconds", float),
            canny_low=num("canny_low", int),
            canny_high=num("canny_high", int),
            min_contour_perimeter=num("min_contour_perimeter", float),
        )
        settings.validate()
        return settings

    def apply_settings(self, settings: ScopeSettings):
        for key, var in self.vars.items():
            value = getattr(settings, key)
            if isinstance(value, float) and value.is_integer() and key != "loop_seconds":
                value = int(value)
            var.set(str(value))

    def reset_settings(self):
        self.apply_settings(ScopeSettings())

    def _update_refresh_label(self):
        try:
            s = self.read_settings()
            self.refresh_var.set(
                f"The picture redraws {s.refresh_rate:.1f} times per second "
                f"({s.points_per_frame} points per frame).")
        except ScopeError as exc:
            self.refresh_var.set(str(exc))

    # ---------------------------------------------------------- images --
    def browse_image(self):
        if self.busy:
            return
        initial = str(self.image_path.parent) if self.image_path else None
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose an image",
            filetypes=backend.file_dialog_types(), initialdir=initial)
        if path:
            self.load_image(path)

    def load_image(self, path, auto_process: bool = True) -> bool:
        path = Path(path)
        try:
            settings = self.read_settings()
            image = backend.load_image(path, settings.svg_render_size)
        except ScopeError as exc:
            messagebox.showerror("Can't open image", str(exc), parent=self.root)
            return False
        self.image_path, self.image = path, image
        self.entry = None
        self.path_var.set(str(path))
        self.source_panel.set_image(image)
        self.preview_panel.set_image(None, "Processing…" if auto_process
                                     else "Press Process to generate the preview")
        wav_name, preview_name = backend.default_output_names(path)
        self.wav_name_var.set(wav_name)
        self.preview_name_var.set(preview_name)
        if not self.folder_var.get().strip():
            self.folder_var.set(str(path.parent))
        self._highlight_card(None)
        self._update_controls()
        if auto_process:
            self.start_processing()
        return True

    # ------------------------------------------------------ processing --
    def start_processing(self):
        if self.busy:
            return
        if self.image_path is None:
            messagebox.showinfo("No image", "Browse for an image first.",
                                parent=self.root)
            return
        try:
            settings = self.read_settings()
        except ScopeError as exc:
            messagebox.showerror("Invalid settings", str(exc), parent=self.root)
            return
        if self.image is None:
            try:
                self.image = backend.load_image(self.image_path,
                                                settings.svg_render_size)
            except ScopeError as exc:
                messagebox.showerror("Can't open image", str(exc), parent=self.root)
                return

        self.busy = True
        self.status_var.set(f"Processing {self.image_path.name}…")
        self.progress.start(12)
        self._update_controls()
        path, image = self.image_path, self.image

        def work():
            try:
                self._results.put(("ok", self.history.process(path, settings, image)))
            except ScopeError as exc:
                self._results.put(("error", str(exc)))
            except Exception as exc:  # unexpected - still report it
                self._results.put(("error", f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self._results.get_nowait()
                self._finish_processing(kind, payload)
        except queue.Empty:
            pass
        if self.playing_id and not self.player.is_playing():
            self._set_playing(None)
        self.root.after(POLL_MS, self._poll)

    def _finish_processing(self, kind, payload):
        self.busy = False
        self.progress.stop()
        if kind == "error":
            self.status_var.set("Processing failed.")
            self.preview_panel.set_image(None, "No preview - see the error message")
            self._update_controls()
            messagebox.showerror("Processing failed", payload, parent=self.root)
            return
        entry = payload
        self.refresh_history()
        self.show_entry(entry, reload_source=False)
        self.status_var.set(
            f"Done: {entry.n_strokes} strokes, {entry.duration:.1f} s at "
            f"{entry.sample_rate} Hz. Choose a folder and names, then Save.")

    # --------------------------------------------------------- history --
    def show_entry(self, entry, reload_source: bool = True):
        """Show a history entry in the main view."""
        if self.busy:
            return
        self.entry = entry
        if reload_source:
            source = Path(entry.image_path)
            self.image_path = source if source.is_file() else None
            self.image = None
            img = None
            if self.image_path is not None:
                try:
                    img = backend.load_image(source)
                    self.image = img
                except ScopeError:
                    img = None
            if img is None:
                img = Image.open(self.history.thumbnail_path(entry))
            self.source_panel.set_image(img)
            self.path_var.set(entry.image_path if self.image_path
                              else f"{entry.image_name} (original file not found)")
            if entry.settings:
                self.apply_settings(ScopeSettings.from_dict(entry.settings))
            self.wav_name_var.set(entry.wav)
            self.preview_name_var.set(entry.preview)
            if not self.folder_var.get().strip() and self.image_path:
                self.folder_var.set(str(self.image_path.parent))
            self.status_var.set(f"Loaded {entry.image_name} from history.")
        self.preview_panel.set_image(Image.open(self.history.preview_path(entry)))
        self._highlight_card(entry.id)
        self._update_controls()

    def refresh_history(self):
        inner = self.history_list.inner
        for child in inner.winfo_children():
            if child is not self.empty_label:
                child.destroy()
        self._card_widgets.clear()
        if not self.history.entries:
            self.empty_label.pack(pady=30)
        else:
            self.empty_label.pack_forget()
            for entry in self.history.entries:
                self._build_card(inner, entry)
        if self.entry is not None and self.history.get(self.entry.id) is None:
            self.entry = None
        self._highlight_card(self.entry.id if self.entry else None)
        self._set_playing(self.playing_id if self.player.is_playing() else None)
        self._update_controls()

    def _thumb(self, path: Path, background: str):
        try:
            with Image.open(path) as img:
                img = img.convert("RGBA")
                img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        except OSError:
            img = Image.new("RGBA", (THUMB_SIZE, THUMB_SIZE), "#cccccc")
        tile = Image.new("RGBA", (THUMB_SIZE, THUMB_SIZE), background)
        tile.alpha_composite(img, ((THUMB_SIZE - img.width) // 2,
                                   (THUMB_SIZE - img.height) // 2))
        return ImageTk.PhotoImage(tile)

    def _build_card(self, parent, entry):
        card = ttk.Frame(parent, style="Card.TFrame", padding=6)
        card.pack(fill="x", pady=3, padx=(0, 4))
        card.columnconfigure(2, weight=1)

        src = self._thumb(self.history.thumbnail_path(entry), "#f4f4f4")
        prev = self._thumb(self.history.preview_path(entry), "#000000")
        src_lbl = ttk.Label(card, image=src, cursor="hand2")
        src_lbl.grid(row=0, column=0, rowspan=3, padx=(0, 4))
        prev_lbl = ttk.Label(card, image=prev, cursor="hand2")
        prev_lbl.grid(row=0, column=1, rowspan=3, padx=(0, 6))

        ttk.Label(card, text=entry.image_name, style="CardName.TLabel",
                  wraplength=SIDEBAR_WIDTH - 2 * THUMB_SIZE - 40).grid(
            row=0, column=2, sticky="w")
        when = entry.created
        try:
            when = datetime.fromisoformat(entry.created).strftime("%d %b %H:%M")
        except ValueError:
            pass
        ttk.Label(card, text=f"♪ {entry.wav}", style="Muted.TLabel",
                  wraplength=SIDEBAR_WIDTH - 2 * THUMB_SIZE - 40).grid(
            row=1, column=2, sticky="w")
        ttk.Label(card, text=f"{when} · {entry.duration:.1f} s",
                  style="Muted.TLabel").grid(row=2, column=2, sticky="w")

        btns = ttk.Frame(card)
        btns.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        play = ttk.Button(btns, text="▶ Play", width=8,
                          command=lambda e=entry: self.toggle_play(e.id))
        play.pack(side="left")
        ttk.Button(btns, text="Open", width=6,
                   command=lambda e=entry: self.show_entry(e)).pack(side="left", padx=4)
        ttk.Button(btns, text="Remove", width=8,
                   command=lambda e=entry: self.remove_entry(e.id)).pack(side="right")

        for w in (src_lbl, prev_lbl):
            w.bind("<Button-1>", lambda _ev, e=entry: self.show_entry(e))
        self._card_widgets[entry.id] = {"frame": card, "play": play,
                                        "photos": (src, prev)}

    def _highlight_card(self, entry_id):
        for eid, w in self._card_widgets.items():
            w["frame"].configure(style="Selected.TFrame" if eid == entry_id
                                 else "Card.TFrame")

    def remove_entry(self, entry_id):
        entry = self.history.get(entry_id)
        if entry is None:
            return
        if not messagebox.askyesno(
                "Remove from history",
                f"Remove {entry.image_name} from the history?\n"
                "Files you already saved are not affected.", parent=self.root):
            return
        if self.playing_id == entry_id:
            self.stop_audio()
        self.history.remove(entry_id)
        if self.entry and self.entry.id == entry_id:
            self.entry = None
            self.preview_panel.set_image(None, "Press Process to generate the preview")
        self.refresh_history()

    def clear_history(self):
        if not self.history.entries:
            return
        if not messagebox.askyesno(
                "Clear history",
                "Remove every item from the history?\n"
                "Files you already saved are not affected.", parent=self.root):
            return
        self.stop_audio()
        self.history.clear()
        self.entry = None
        self.preview_panel.set_image(None, "Press Process to generate the preview")
        self.refresh_history()

    # ----------------------------------------------------------- audio --
    def toggle_play(self, entry_id):
        if self.playing_id == entry_id:
            self.stop_audio()
            return
        entry = self.history.get(entry_id)
        if entry is None:
            return
        try:
            self.player.play(self.history.wav_path(entry))
        except Exception as exc:
            messagebox.showerror("Playback failed", str(exc), parent=self.root)
            self._set_playing(None)
            return
        self._set_playing(entry_id)

    def toggle_play_current(self):
        if self.entry is not None:
            self.toggle_play(self.entry.id)

    def stop_audio(self):
        self.player.stop()
        self._set_playing(None)

    def _set_playing(self, entry_id):
        self.playing_id = entry_id
        for eid, w in self._card_widgets.items():
            w["play"].configure(text="■ Stop" if eid == entry_id else "▶ Play")
        current = self.entry is not None and self.entry.id == entry_id
        self.play_btn.configure(text="■ Stop sound" if current else "▶ Play sound")

    # ------------------------------------------------------------ save --
    def choose_folder(self):
        folder = filedialog.askdirectory(
            parent=self.root, title="Save files to…",
            initialdir=self.folder_var.get() or None, mustexist=False)
        if folder:
            self.folder_var.set(folder)

    def save_files(self):
        if self.entry is None or self.busy:
            return
        folder = self.folder_var.get().strip()
        if not folder:
            self.choose_folder()
            folder = self.folder_var.get().strip()
            if not folder:
                return
        wav_name = self.wav_name_var.get().strip()
        preview_name = self.preview_name_var.get().strip()
        if not wav_name and not preview_name:
            messagebox.showerror("Nothing to save",
                                 "Enter a name for the sound file and/or the preview.",
                                 parent=self.root)
            return
        for label, name in (("sound file", wav_name), ("preview image", preview_name)):
            bad = backend.invalid_filename_chars(name)
            if bad:
                messagebox.showerror(
                    "Invalid name", f"The {label} name can't contain: {bad}",
                    parent=self.root)
                return

        targets = []
        if wav_name:
            targets.append(Path(folder) / backend.ensure_extension(wav_name, ".wav"))
        if preview_name:
            targets.append(Path(folder) / backend.ensure_extension(preview_name, ".png"))
        existing = [t.name for t in targets if t.exists()]
        if existing and not messagebox.askyesno(
                "Replace files?",
                "These files already exist and will be replaced:\n\n"
                + "\n".join(existing), parent=self.root):
            return
        try:
            wav_dest, preview_dest = backend.export_files(
                self.history.wav_path(self.entry),
                self.history.preview_path(self.entry),
                folder, wav_name, preview_name)
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc), parent=self.root)
            return
        saved = [p.name for p in (wav_dest, preview_dest) if p]
        self.status_var.set(f"Saved {' and '.join(saved)} to {folder}")

    def open_output_folder(self):
        folder = Path(self.folder_var.get().strip() or ".")
        if folder.is_dir():
            open_in_file_manager(folder)
        else:
            messagebox.showinfo("Folder not found",
                                f"{folder} does not exist yet.", parent=self.root)

    # ----------------------------------------------------------- misc --
    def _update_controls(self):
        has_entry = self.entry is not None and not self.busy
        idle = "disabled" if self.busy else "normal"
        self.browse_btn.configure(state=idle)
        self.process_btn.configure(
            state="normal" if self.image_path and not self.busy else "disabled")
        self.save_btn.configure(state="normal" if has_entry else "disabled")
        self.play_btn.configure(
            state="normal" if has_entry and self.player.available else "disabled")
        self.clear_btn.configure(state=idle)

    def close(self):
        try:
            self.player.stop()
        finally:
            self.root.destroy()


def run(history: HistoryStore | None = None, image: str | None = None) -> None:
    """Create the main window and run the Tk event loop."""
    root = tk.Tk(className=backend.APP_SLUG)
    app = ScopeApp(root, history=history)
    if image:
        root.after(200, lambda: app.load_image(image))
    root.mainloop()
