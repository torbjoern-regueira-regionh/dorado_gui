"""
Dorado Basecaller GUI
=====================

A small Tkinter front-end for `dorado basecaller` on Windows (works on Linux/macOS too).

- pick dorado.exe, the pod5 folder and an output folder with file browsers
- choose DNA/RNA, fast/hac/sup, model version and modified-base models
  (or point to a locally downloaded model folder)
- barcoding kit, trimming, min-qscore, FASTQ output, alignment, poly(A), device
- live command preview, streamed log, progress line and Stop button

Run by double-clicking this file (.pyw = no console window) or `python dorado_gui.pyw`.
Only the Python standard library is used.
"""

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Dorado Basecaller GUI"
IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "dorado_gui")
else:
    SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".config", "dorado_gui")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")

# ---------------------------------------------------------------------------
# Model catalogue
# Default values reflect the Dorado model list (Dorado 2.1, Oct 2026).
# "Refresh from dorado" replaces this with whatever `dorado download --list` reports.
# key: (sample_type, speed) -> {"versions": [...], "mods": [...]}
# ---------------------------------------------------------------------------
DEFAULT_CATALOG = {
    ("dna", "fast"): {"versions": ["v5.2.0", "v5.0.0"], "mods": []},
    ("dna", "hac"): {"versions": ["v6.0.0", "v5.2.0", "v5.0.0"],
                     "mods": ["5mCG_5hmCG", "5mC_5hmC", "4mC_5mC", "6mA"]},
    ("dna", "sup"): {"versions": ["v5.2.0", "v5.0.0"],
                     "mods": ["5mCG_5hmCG", "5mC_5hmC", "4mC_5mC", "6mA"]},
    ("rna", "fast"): {"versions": ["v6.0.0", "v5.2.0"], "mods": []},
    ("rna", "hac"): {"versions": ["v6.0.0", "v5.2.0"],
                     "mods": ["m5C", "m6A_DRACH", "inosine_m6A", "pseU"]},
    ("rna", "sup"): {"versions": ["v6.0.0", "v5.2.0"],
                     "mods": ["m5C_2OmeC", "m6A_DRACH", "inosine_m6A_2OmeA", "pseU_2OmeU", "2OmeG"]},
}

# Dorado allows only one modification model per canonical base.
MOD_BASE_HINTS = [
    ("pseU", "T"), ("2OmeU", "T"), ("2OmeG", "G"), ("6mA", "A"), ("m6A", "A"),
    ("inosine", "A"), ("2OmeA", "A"), ("4mC", "C"), ("5mC", "C"), ("m5C", "C"),
    ("5hmC", "C"), ("2OmeC", "C"),
]

MOD_DESCRIPTIONS = {
    "5mCG_5hmCG": "5mC + 5hmC in CpG context",
    "5mC_5hmC": "5mC + 5hmC, all contexts",
    "4mC_5mC": "4mC + 5mC, all contexts (bacterial)",
    "6mA": "6mA, all contexts",
    "m5C": "m5C",
    "m5C_2OmeC": "m5C + 2'-O-methyl C",
    "m6A_DRACH": "m6A in DRACH motifs",
    "inosine_m6A": "Inosine + m6A, all contexts",
    "inosine_m6A_2OmeA": "Inosine + m6A + 2'-O-methyl A",
    "pseU": "Pseudouridine",
    "pseU_2OmeU": "Pseudouridine + 2'-O-methyl U",
    "2OmeG": "2'-O-methyl G",
}

KITS = [
    "", "SQK-NBD114-24", "SQK-NBD114-96", "SQK-RBK114-24", "SQK-RBK114-96",
    "SQK-PCB114-24", "SQK-RPB114-24", "SQK-16S114-24", "SQK-MAB114-24",
]

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def mod_base(mod):
    """Canonical base a modification model acts on (used to prevent clashes)."""
    for hint, base in MOD_BASE_HINTS:
        if hint in mod:
            return base
    return None


def parse_model_list(text):
    """Build a catalogue from `dorado download --list` output."""
    catalog = {}
    simplex = re.compile(r"\b(dna|rna)[\w.]*?_(fast|hac|sup)@(v[\d.]+)(?:_([A-Za-z0-9_]+?)@v[\d.]+)?\b")
    for m in simplex.finditer(text):
        stype, speed, ver, mod = m.group(1), m.group(2), m.group(3), m.group(4)
        entry = catalog.setdefault((stype, speed), {"versions": [], "mods": []})
        if ver not in entry["versions"]:
            entry["versions"].append(ver)
        if mod and mod not in entry["mods"]:
            entry["mods"].append(mod)

    def vkey(v):
        return tuple(int(x) for x in v.lstrip("v").split(".") if x.isdigit())

    for entry in catalog.values():
        entry["versions"].sort(key=vkey, reverse=True)
    return catalog


def find_dorado():
    exe = shutil.which("dorado")
    if exe:
        return exe
    if IS_WINDOWS:
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"), "C:\\",
                 os.path.expanduser("~"), os.path.join(os.path.expanduser("~"), "Downloads")]
        for root in roots:
            try:
                for name in sorted(os.listdir(root), reverse=True):
                    if name.lower().startswith("dorado"):
                        cand = os.path.join(root, name, "bin", "dorado.exe")
                        if os.path.isfile(cand):
                            return cand
            except OSError:
                pass
    return ""


def count_pod5(folder, recursive):
    n = 0
    if recursive:
        for _, _, files in os.walk(folder):
            n += sum(f.lower().endswith(".pod5") for f in files)
    else:
        try:
            n = sum(f.lower().endswith(".pod5") for f in os.listdir(folder))
        except OSError:
            return 0
    return n


def popen_kwargs():
    kw = {}
    if IS_WINDOWS:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kw


class DoradoGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("980x900")
        self.minsize(820, 700)

        self.catalog = {k: dict(v) for k, v in DEFAULT_CATALOG.items()}
        self.proc = None
        self.msg_queue = queue.Queue()
        self.log_file = None
        self.start_time = None
        self.mod_vars = {}

        self._make_vars()
        self._load_settings()
        self._build_ui()
        self._refresh_model_widgets()
        self._update_pod5_count()
        self._update_command()
        self._check_dorado_version()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queue)

    # ------------------------------------------------------------------ state
    def _make_vars(self):
        self.v_dorado = tk.StringVar(value=find_dorado())
        self.v_models_dir = tk.StringVar()
        self.v_input = tk.StringVar()
        self.v_recursive = tk.BooleanVar(value=True)
        self.v_output = tk.StringVar()
        self.v_mode = tk.StringVar(value="standard")  # standard | custom
        self.v_type = tk.StringVar(value="dna")
        self.v_speed = tk.StringVar(value="sup")
        self.v_version = tk.StringVar(value="latest")
        self.v_custom_model = tk.StringVar()
        self.v_custom_mods = tk.StringVar()
        self.v_kit = tk.StringVar()
        self.v_no_trim = tk.BooleanVar(value=False)
        self.v_min_q = tk.StringVar(value="")
        self.v_fastq = tk.BooleanVar(value=False)
        self.v_reference = tk.StringVar()
        self.v_polya = tk.BooleanVar(value=False)
        self.v_device = tk.StringVar(value="cuda:all")
        self.v_extra = tk.StringVar()
        self.v_status = tk.StringVar(value="Idle")
        self.v_progress = tk.StringVar(value="")
        self.v_pod5_info = tk.StringVar(value="")
        self.v_dorado_info = tk.StringVar(value="")
        self.saved_mods = []

    SAVED_KEYS = ["dorado", "models_dir", "input", "recursive", "output", "mode", "type",
                  "speed", "version", "custom_model", "custom_mods", "kit", "no_trim",
                  "min_q", "fastq", "reference", "polya", "device", "extra"]

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for key in self.SAVED_KEYS:
            if key in data and data[key] is not None:
                if key == "dorado" and not data[key]:
                    continue
                getattr(self, "v_" + key).set(data[key])
        self.saved_mods = data.get("mods", [])
        cat = data.get("catalog")
        if cat:
            try:
                self.catalog = {tuple(k.split("|")): v for k, v in cat.items()}
            except (AttributeError, ValueError):
                pass

    def _save_settings(self):
        data = {key: getattr(self, "v_" + key).get() for key in self.SAVED_KEYS}
        data["mods"] = self._selected_mods()
        data["catalog"] = {"|".join(k): v for k, v in self.catalog.items()}
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            pass

    # --------------------------------------------------------------------- UI
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        outer = ttk.Frame(self, padding=8)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        # --- Dorado executable
        f = ttk.LabelFrame(outer, text="Dorado", padding=6)
        f.grid(row=0, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="dorado executable:").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v_dorado).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(f, text="Browse…", command=self._browse_dorado).grid(row=0, column=2)
        ttk.Label(f, textvariable=self.v_dorado_info, foreground="gray").grid(
            row=1, column=1, sticky="w", padx=4)
        ttk.Label(f, text="Models directory (optional):").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v_models_dir).grid(row=2, column=1, sticky="ew", padx=4)
        ttk.Button(f, text="Browse…", command=lambda: self._browse_dir(self.v_models_dir)).grid(row=2, column=2)
        ttk.Label(f, text="Where models are downloaded/cached. Leave empty to let dorado download "
                          "into a temporary folder each run.", foreground="gray").grid(
            row=3, column=1, columnspan=2, sticky="w", padx=4)

        # --- Input / output
        f = ttk.LabelFrame(outer, text="Input / output", padding=6)
        f.grid(row=1, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="pod5 folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v_input).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(f, text="Browse…", command=self._browse_input).grid(row=0, column=2)
        sub = ttk.Frame(f)
        sub.grid(row=1, column=1, sticky="w", padx=4)
        ttk.Checkbutton(sub, text="Include subfolders (--recursive)", variable=self.v_recursive,
                        command=self._update_pod5_count).pack(side="left")
        ttk.Label(sub, textvariable=self.v_pod5_info, foreground="gray").pack(side="left", padx=12)
        ttk.Label(f, text="Output folder:").grid(row=2, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v_output).grid(row=2, column=1, sticky="ew", padx=4)
        ttk.Button(f, text="Browse…", command=lambda: self._browse_dir(self.v_output)).grid(row=2, column=2)

        # --- Model
        f = ttk.LabelFrame(outer, text="Model", padding=6)
        f.grid(row=2, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)

        row = ttk.Frame(f)
        row.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(row, text="Standard model (auto-selected for the pod5 chemistry)",
                        value="standard", variable=self.v_mode,
                        command=self._refresh_model_widgets).pack(side="left")
        ttk.Radiobutton(row, text="Local model folder", value="custom", variable=self.v_mode,
                        command=self._refresh_model_widgets).pack(side="left", padx=16)
        ttk.Button(row, text="Refresh model list from dorado",
                   command=self._refresh_catalog).pack(side="left", padx=16)

        # standard
        self.std_frame = ttk.Frame(f)
        self.std_frame.grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)
        r = ttk.Frame(self.std_frame)
        r.pack(fill="x")
        ttk.Label(r, text="Sample:").pack(side="left")
        for val, txt in (("dna", "DNA"), ("rna", "RNA (RNA004)")):
            ttk.Radiobutton(r, text=txt, value=val, variable=self.v_type,
                            command=self._refresh_model_widgets).pack(side="left", padx=4)
        ttk.Label(r, text="     Accuracy:").pack(side="left")
        for val in ("fast", "hac", "sup"):
            ttk.Radiobutton(r, text=val, value=val, variable=self.v_speed,
                            command=self._refresh_model_widgets).pack(side="left", padx=4)
        ttk.Label(r, text="     Version:").pack(side="left")
        self.cb_version = ttk.Combobox(r, textvariable=self.v_version, width=10)
        self.cb_version.pack(side="left", padx=4)
        self.cb_version.bind("<<ComboboxSelected>>", lambda e: self._update_command())

        self.mods_frame = ttk.LabelFrame(self.std_frame, text="Modified bases", padding=4)
        self.mods_frame.pack(fill="x", pady=(6, 0))

        # custom
        self.custom_frame = ttk.Frame(f)
        self.custom_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=4)
        self.custom_frame.columnconfigure(1, weight=1)
        ttk.Label(self.custom_frame, text="Model folder:").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.custom_frame, textvariable=self.v_custom_model).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(self.custom_frame, text="Browse…",
                   command=lambda: self._browse_dir(self.v_custom_model)).grid(row=0, column=2)
        ttk.Label(self.custom_frame, text="Modbase model folders:").grid(row=1, column=0, sticky="w")
        ttk.Entry(self.custom_frame, textvariable=self.v_custom_mods).grid(row=1, column=1, sticky="ew", padx=4)
        bb = ttk.Frame(self.custom_frame)
        bb.grid(row=1, column=2)
        ttk.Button(bb, text="Add…", command=self._add_custom_mod).pack(side="left")
        ttk.Button(bb, text="Clear", command=lambda: self.v_custom_mods.set("")).pack(side="left")
        ttk.Label(self.custom_frame, text="Separate multiple modbase model folders with ';'",
                  foreground="gray").grid(row=2, column=1, sticky="w", padx=4)

        # --- Options
        f = ttk.LabelFrame(outer, text="Options", padding=6)
        f.grid(row=3, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        f.columnconfigure(4, weight=1)
        ttk.Label(f, text="Barcode kit:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(f, textvariable=self.v_kit, values=KITS, width=20).grid(row=0, column=1, sticky="w", padx=4)
        ttk.Checkbutton(f, text="Don't trim adapters/primers/barcodes (--no-trim)",
                        variable=self.v_no_trim).grid(row=0, column=3, columnspan=2, sticky="w")
        ttk.Label(f, text="Min. Q-score:").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(f, textvariable=self.v_min_q, from_=0, to=50, width=6).grid(row=1, column=1, sticky="w", padx=4)
        self.chk_fastq = ttk.Checkbutton(f, text="Write FASTQ instead of BAM (--emit-fastq)",
                                         variable=self.v_fastq)
        self.chk_fastq.grid(row=1, column=3, columnspan=2, sticky="w")
        ttk.Label(f, text="Device:").grid(row=2, column=0, sticky="w")
        ttk.Combobox(f, textvariable=self.v_device, width=20,
                     values=["cuda:all", "cuda:0", "cuda:1", "cuda:0,1", "cpu", "auto"]).grid(
            row=2, column=1, sticky="w", padx=4)
        self.chk_polya = ttk.Checkbutton(f, text="Estimate poly(A) tail length (--estimate-poly-a)",
                                         variable=self.v_polya)
        self.chk_polya.grid(row=2, column=3, columnspan=2, sticky="w")
        ttk.Label(f, text="Align to reference:").grid(row=3, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v_reference).grid(row=3, column=1, columnspan=3, sticky="ew", padx=4)
        ttk.Button(f, text="Browse…", command=self._browse_reference).grid(row=3, column=4, sticky="w")
        ttk.Label(f, text="Extra arguments:").grid(row=4, column=0, sticky="w")
        ttk.Entry(f, textvariable=self.v_extra).grid(row=4, column=1, columnspan=3, sticky="ew", padx=4)
        ttk.Label(f, text="e.g. --batchsize 256", foreground="gray").grid(row=4, column=4, sticky="w")

        # --- Command preview
        f = ttk.LabelFrame(outer, text="Command", padding=6)
        f.grid(row=4, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.txt_cmd = tk.Text(f, height=3, wrap="word", font=("Consolas", 9), relief="flat",
                               background=self.cget("background"))
        self.txt_cmd.grid(row=0, column=0, sticky="ew")
        ttk.Button(f, text="Copy", command=self._copy_command).grid(row=0, column=1, sticky="n", padx=4)

        # --- Run controls
        f = ttk.Frame(outer)
        f.grid(row=5, column=0, sticky="ew", **pad)
        f.columnconfigure(3, weight=1)
        self.btn_start = ttk.Button(f, text="▶  Start basecalling", command=self._start)
        self.btn_start.grid(row=0, column=0)
        self.btn_stop = ttk.Button(f, text="■  Stop", command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=6)
        ttk.Button(f, text="Open output folder", command=self._open_output).grid(row=0, column=2)
        ttk.Label(f, textvariable=self.v_status, font=("Segoe UI", 9, "bold")).grid(
            row=0, column=3, sticky="e", padx=6)
        ttk.Label(f, textvariable=self.v_progress, font=("Consolas", 9)).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # --- Log
        f = ttk.LabelFrame(outer, text="Log", padding=4)
        f.grid(row=6, column=0, sticky="nsew", **pad)
        outer.rowconfigure(6, weight=1)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self.txt_log = tk.Text(f, height=10, wrap="none", font=("Consolas", 9), state="disabled")
        self.txt_log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.txt_log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=sb.set)

        # live command updates
        for var in (self.v_dorado, self.v_models_dir, self.v_input, self.v_recursive, self.v_output,
                    self.v_version, self.v_custom_model, self.v_custom_mods, self.v_kit,
                    self.v_no_trim, self.v_min_q, self.v_fastq, self.v_reference, self.v_polya,
                    self.v_device, self.v_extra):
            var.trace_add("write", lambda *a: self._update_command())
        self.v_input.trace_add("write", lambda *a: self._update_pod5_count())

    def _refresh_model_widgets(self):
        custom = self.v_mode.get() == "custom"
        if custom:
            self.std_frame.grid_remove()
            self.custom_frame.grid()
        else:
            self.custom_frame.grid_remove()
            self.std_frame.grid()

        key = (self.v_type.get(), self.v_speed.get())
        entry = self.catalog.get(key, {"versions": [], "mods": []})
        self.cb_version["values"] = ["latest"] + entry["versions"]
        if self.v_version.get() not in self.cb_version["values"]:
            self.v_version.set("latest")

        # rebuild mod checkboxes, keeping selections that still apply
        previous = set(self._selected_mods()) or set(self.saved_mods)
        self.saved_mods = []
        for w in self.mods_frame.winfo_children():
            w.destroy()
        self.mod_vars = {}
        if not entry["mods"]:
            ttk.Label(self.mods_frame, text="No modified-base models available for this model "
                                            "(use hac or sup).", foreground="gray").grid(row=0, column=0)
        for i, mod in enumerate(entry["mods"]):
            var = tk.BooleanVar(value=mod in previous)
            self.mod_vars[mod] = var
            desc = MOD_DESCRIPTIONS.get(mod, "")
            text = f"{mod}" + (f"  –  {desc}" if desc else "")
            ttk.Checkbutton(self.mods_frame, text=text, variable=var,
                            command=lambda m=mod: self._mod_toggled(m)).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 30))
        self._dedupe_mods()

        polya_ok = custom or self.v_type.get() == "rna"
        self.chk_polya.configure(state="normal" if polya_ok else "disabled")
        if not polya_ok:
            self.v_polya.set(False)
        self._update_command()

    def _mod_toggled(self, mod):
        """Only one modification model per canonical base is allowed."""
        if self.mod_vars[mod].get():
            base = mod_base(mod)
            for other, var in self.mod_vars.items():
                if other != mod and base and mod_base(other) == base and var.get():
                    var.set(False)
        self._update_command()

    def _dedupe_mods(self):
        seen = set()
        for mod, var in self.mod_vars.items():
            if var.get():
                b = mod_base(mod)
                if b in seen:
                    var.set(False)
                elif b:
                    seen.add(b)

    def _selected_mods(self):
        return [m for m, v in self.mod_vars.items() if v.get()]

    # --------------------------------------------------------------- browsing
    def _browse_dorado(self):
        types = [("dorado", "dorado.exe"), ("All files", "*.*")] if IS_WINDOWS else [("All files", "*")]
        path = filedialog.askopenfilename(title="Select dorado executable", filetypes=types)
        if path:
            self.v_dorado.set(os.path.normpath(path))
            self._check_dorado_version()

    def _browse_dir(self, var):
        path = filedialog.askdirectory(title="Select folder", initialdir=var.get() or None)
        if path:
            var.set(os.path.normpath(path))

    def _browse_input(self):
        path = filedialog.askdirectory(title="Select folder containing pod5 files",
                                       initialdir=self.v_input.get() or None)
        if path:
            path = os.path.normpath(path)
            self.v_input.set(path)
            if not self.v_output.get():
                self.v_output.set(os.path.join(os.path.dirname(path), "basecalled"))

    def _browse_reference(self):
        path = filedialog.askopenfilename(
            title="Select reference (FASTA or minimap2 index)",
            filetypes=[("Reference", "*.fa *.fasta *.fna *.fa.gz *.fasta.gz *.fna.gz *.mmi"),
                       ("All files", "*.*")])
        if path:
            self.v_reference.set(os.path.normpath(path))

    def _add_custom_mod(self):
        path = filedialog.askdirectory(title="Select modified-base model folder")
        if path:
            cur = [p for p in self.v_custom_mods.get().split(";") if p.strip()]
            cur.append(os.path.normpath(path))
            self.v_custom_mods.set(";".join(cur))

    def _update_pod5_count(self):
        folder = self.v_input.get()
        if folder and os.path.isdir(folder):
            n = count_pod5(folder, self.v_recursive.get())
            self.v_pod5_info.set(f"{n} pod5 file(s) found")
        else:
            self.v_pod5_info.set("")

    def _open_output(self):
        out = self.v_output.get()
        if not out or not os.path.isdir(out):
            messagebox.showinfo(APP_NAME, "Output folder does not exist yet.")
            return
        if IS_WINDOWS:
            os.startfile(out)
        else:
            subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", out])

    # ---------------------------------------------------------------- dorado
    def _check_dorado_version(self):
        exe = self.v_dorado.get()
        if not exe:
            self.v_dorado_info.set("dorado not found – click Browse and select dorado.exe (in the 'bin' folder)")
            return

        def worker():
            try:
                r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                   timeout=30, **popen_kwargs())
                ver = (r.stdout + r.stderr).strip().splitlines()
                self.msg_queue.put(("dorado_info", f"version {ver[-1]}" if ver else "found"))
            except (OSError, subprocess.SubprocessError) as e:
                self.msg_queue.put(("dorado_info", f"could not run dorado: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def _refresh_catalog(self):
        exe = self.v_dorado.get()
        if not exe:
            messagebox.showerror(APP_NAME, "Select the dorado executable first.")
            return
        self.v_status.set("Querying dorado for available models…")

        def worker():
            try:
                r = subprocess.run([exe, "download", "--list"], capture_output=True, text=True,
                                   timeout=120, **popen_kwargs())
                cat = parse_model_list(r.stdout + "\n" + r.stderr)
                self.msg_queue.put(("catalog", cat))
            except (OSError, subprocess.SubprocessError) as e:
                self.msg_queue.put(("catalog_err", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _build_command(self):
        exe = self.v_dorado.get() or "dorado"
        cmd = [exe, "basecaller"]

        if self.v_mode.get() == "standard":
            model = self.v_speed.get()
            ver = self.v_version.get().strip()
            if ver and ver != "latest":
                model += "@" + (ver if ver.startswith("v") else "v" + ver)
            mods = self._selected_mods()
            if mods:
                model += "," + ",".join(mods)
            cmd.append(model)
        else:
            cmd.append(self.v_custom_model.get() or "<model folder>")
        cmd.append(self.v_input.get() or "<pod5 folder>")

        if self.v_mode.get() == "custom":
            mods = [p.strip() for p in self.v_custom_mods.get().split(";") if p.strip()]
            if mods:
                cmd += ["--modified-bases-models", ",".join(mods)]
        if self.v_recursive.get():
            cmd.append("--recursive")
        if self.v_models_dir.get():
            cmd += ["--models-directory", self.v_models_dir.get()]
        if self.v_device.get().strip():
            cmd += ["--device", self.v_device.get().strip()]
        if self.v_kit.get().strip():
            cmd += ["--kit-name", self.v_kit.get().strip()]
        if self.v_no_trim.get():
            cmd.append("--no-trim")
        if self.v_min_q.get().strip() not in ("", "0"):
            cmd += ["--min-qscore", self.v_min_q.get().strip()]
        if self.v_fastq.get():
            cmd.append("--emit-fastq")
        if self.v_reference.get().strip():
            cmd += ["--reference", self.v_reference.get().strip()]
        if self.v_polya.get():
            cmd.append("--estimate-poly-a")
        cmd += ["--output-dir", self.v_output.get() or "<output folder>"]
        if self.v_extra.get().strip():
            cmd += [t.strip('"') for t in shlex.split(self.v_extra.get(), posix=False)]
        return cmd

    @staticmethod
    def _cmd_to_str(cmd):
        return subprocess.list2cmdline(cmd) if IS_WINDOWS else shlex.join(cmd)

    def _update_command(self):
        try:
            text = self._cmd_to_str(self._build_command())
        except ValueError as e:  # bad quoting in extra args
            text = f"(invalid extra arguments: {e})"
        self.txt_cmd.configure(state="normal")
        self.txt_cmd.delete("1.0", "end")
        self.txt_cmd.insert("1.0", text)
        self.txt_cmd.configure(state="disabled")

    def _copy_command(self):
        self.clipboard_clear()
        self.clipboard_append(self.txt_cmd.get("1.0", "end").strip())
        self.v_status.set("Command copied to clipboard")

    def _validate(self):
        errs = []
        exe = self.v_dorado.get()
        if not exe or not (os.path.isfile(exe) or shutil.which(exe)):
            errs.append("Select a valid dorado executable.")
        if not self.v_input.get() or not os.path.isdir(self.v_input.get()):
            errs.append("Select the folder containing pod5 files.")
        elif count_pod5(self.v_input.get(), self.v_recursive.get()) == 0:
            errs.append("No .pod5 files found in the selected folder"
                        + ("" if self.v_recursive.get() else " (try 'Include subfolders')") + ".")
        if not self.v_output.get():
            errs.append("Select an output folder.")
        if self.v_mode.get() == "custom" and not os.path.isdir(self.v_custom_model.get()):
            errs.append("Select a valid local model folder.")
        has_mods = bool(self._selected_mods()) if self.v_mode.get() == "standard" \
            else bool(self.v_custom_mods.get().strip())
        if self.v_fastq.get() and self.v_reference.get().strip():
            errs.append("FASTQ output cannot be combined with alignment – untick FASTQ or clear the reference.")
        if self.v_fastq.get() and has_mods:
            if not messagebox.askyesno(APP_NAME, "FASTQ output with modified bases: the modification "
                                       "calls are only kept as MM/ML tags in the FASTQ header and many "
                                       "tools won't read them. BAM is recommended.\n\nContinue anyway?"):
                return False
        if self.v_min_q.get().strip():
            try:
                float(self.v_min_q.get())
            except ValueError:
                errs.append("Min. Q-score must be a number.")
        if errs:
            messagebox.showerror(APP_NAME, "\n".join(errs))
            return False
        out = self.v_output.get()
        if os.path.isdir(out) and os.listdir(out):
            if not messagebox.askyesno(APP_NAME, f"The output folder is not empty:\n{out}\n\n"
                                       "Dorado may add to or overwrite files there. Continue?"):
                return False
        return True

    # ------------------------------------------------------------------- run
    def _start(self):
        if self.proc is not None:
            return
        if not self._validate():
            return
        cmd = self._build_command()
        self._save_settings()
        out = self.v_output.get()
        os.makedirs(out, exist_ok=True)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        try:
            self.log_file = open(os.path.join(out, f"dorado_gui_{stamp}.log"), "w", encoding="utf-8")
        except OSError:
            self.log_file = None

        self._log_clear()
        self._log(f"# {time.strftime('%Y-%m-%d %H:%M:%S')}\n# {self._cmd_to_str(cmd)}\n\n")

        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, **popen_kwargs())
        except OSError as e:
            self._log(f"Failed to start dorado: {e}\n")
            messagebox.showerror(APP_NAME, f"Failed to start dorado:\n{e}")
            self._close_log()
            return

        self.start_time = time.time()
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.v_status.set("Running…")
        self.v_progress.set("")
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self._tick()

    def _reader(self, proc):
        """Read dorado's output; '\\r'-terminated chunks are progress-bar updates."""
        buf = b""
        while True:
            chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
            if not chunk:
                break
            buf += chunk
            while True:
                idx_n, idx_r = buf.find(b"\n"), buf.find(b"\r")
                cands = [i for i in (idx_n, idx_r) if i >= 0]
                if not cands:
                    break
                i = min(cands)
                line = ANSI_RE.sub("", buf[:i].decode("utf-8", errors="replace"))
                is_progress = buf[i:i + 1] == b"\r" and buf[i + 1:i + 2] != b"\n"
                buf = buf[i + 1:]
                if not line.strip():
                    continue
                if is_progress:
                    self.msg_queue.put(("progress", line.strip()))
                else:
                    self.msg_queue.put(("log", line + "\n"))
        if buf.strip():
            self.msg_queue.put(("log", ANSI_RE.sub("", buf.decode("utf-8", errors="replace")) + "\n"))
        rc = proc.wait()
        self.msg_queue.put(("done", rc))

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            if messagebox.askyesno(APP_NAME, "Stop the running basecalling job?"):
                self._log("\n# Stopped by user\n")
                self.proc.terminate()
                self.v_status.set("Stopping…")

    def _tick(self):
        if self.proc is not None:
            el = int(time.time() - self.start_time)
            self.v_status.set(f"Running…  {el // 3600:d}:{el % 3600 // 60:02d}:{el % 60:02d}")
            self.after(1000, self._tick)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    self.v_progress.set(payload[-160:])
                elif kind == "done":
                    self._finished(payload)
                elif kind == "dorado_info":
                    self.v_dorado_info.set(payload)
                elif kind == "catalog":
                    if payload:
                        self.catalog = payload
                        self._refresh_model_widgets()
                        self._save_settings()
                        n = sum(len(v["versions"]) for v in payload.values())
                        self.v_status.set(f"Model list updated ({n} basecalling models)")
                    else:
                        self.v_status.set("Could not parse model list – keeping built-in list")
                elif kind == "catalog_err":
                    self.v_status.set("Idle")
                    messagebox.showerror(APP_NAME, f"Could not query dorado:\n{payload}")
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _finished(self, rc):
        el = int(time.time() - (self.start_time or time.time()))
        dur = f"{el // 3600:d}:{el % 3600 // 60:02d}:{el % 60:02d}"
        self.proc = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if rc == 0:
            self.v_status.set(f"Finished ✔  ({dur})")
            self._log(f"\n# Finished successfully in {dur}\n")
        else:
            self.v_status.set(f"Failed / stopped (exit code {rc})")
            self._log(f"\n# dorado exited with code {rc} after {dur}\n")
        self._close_log()
        self.bell()

    def _log(self, text):
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")
        if self.log_file:
            self.log_file.write(text)
            self.log_file.flush()

    def _log_clear(self):
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def _close_log(self):
        if self.log_file:
            self.log_file.close()
            self.log_file = None

    def _on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno(APP_NAME, "Basecalling is still running. Stop it and quit?"):
                return
            self.proc.terminate()
        self._save_settings()
        self.destroy()


def main():
    if IS_WINDOWS:
        try:  # crisp fonts on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    app = DoradoGUI()
    try:
        ttk.Style(app).theme_use("vista" if IS_WINDOWS else "clam")
    except tk.TclError:
        pass
    app.mainloop()


if __name__ == "__main__":
    main()
