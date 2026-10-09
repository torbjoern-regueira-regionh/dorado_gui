"""
Dorado Basecaller GUI
=====================

A small Tkinter front-end for `dorado basecaller` on Windows (works on Linux/macOS too).

- pick dorado.exe, the pod5 folder and an output folder with file browsers
- choose DNA/RNA, fast/hac/sup, model version and modified-base models
  (or point to a locally downloaded model folder)
- barcoding kit, trimming, min-qscore, FASTQ output, alignment, poly(A), device
- live command preview, streamed log, progress line and Stop button
- resume a crashed run from the BAM files it left behind (e.g. a bam_pass folder)

Run by double-clicking this file (.pyw = no console window) or `python dorado_gui.pyw`.
Only the Python standard library is used.
"""

import json
import os
import queue
import re
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
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

# dorado draws its progress bar only in a terminal, so started from here it stays silent
# until it is done. The GUI therefore reports how much output has been written.
OUTPUT_CHECK_SECS = 10    # how often the output size is measured for the progress line
HEARTBEAT_SECS = 600      # how often a line with time and output size is added to the log


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


# ---------------------------------------------------------------------------
# Resume support
# `dorado basecaller --resume-from` takes a single BAM, but a crashed run that
# wrote to an output folder leaves many (bam_pass/..., one per barcode/batch),
# the last of which are usually truncated. The functions below join them into
# one BAM using only the standard library. Records are copied byte-for-byte, so
# all tags (MM/ML modification calls, barcodes, poly(A), moves, ...) are kept.
# ---------------------------------------------------------------------------
RESUME_FILE = "_resume_input.bam"
RESUME_INFO = "_resume_input.json"  # what RESUME_FILE was joined from, so a retry can reuse it
# dorado refuses --resume-from together with --output-dir, so a resumed run writes
# this one file (.bam or .fastq) through stdout instead
RESUME_OUTPUT = "calls"
BGZF_EOF = bytes.fromhex("1f8b08040000000000ff0600424302001b0003000000000000000000")
COPY_CHUNK = 8 << 20   # bytes read/written at a time
INFLATE_BATCH = 64     # BGZF blocks handed to a worker thread at a time


def find_bams(folder):
    """All .bam files below folder (sorted), ignoring resume files written by this GUI."""
    found = []
    for root, _, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".bam") and f != RESUME_FILE:
                found.append(os.path.join(root, f))
    return sorted(found)


def total_size(paths):
    n = 0
    for p in paths:
        try:
            n += os.path.getsize(p)
        except OSError:
            pass
    return n


def tree_size(folder):
    """Size of all files below folder, without this GUI's own log and resume files."""
    n = 0
    for root, _, files in os.walk(folder):
        for f in files:
            if f in (RESUME_FILE, RESUME_FILE + ".part", RESUME_INFO) \
                    or (f.startswith("dorado_gui_") and f.endswith(".log")):
                continue
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def fmt_size(n):
    for unit in ("bytes", "kB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def bam_signature(paths):
    """Path, size and modification time of each file: changes when the files do."""
    sig = []
    for p in paths:
        st = os.stat(p)
        sig.append([os.path.abspath(p), st.st_size, st.st_mtime_ns])
    return sig


def load_resume_info(resume_path):
    """Info saved next to a completely written resume file; None if missing or outdated."""
    try:
        with open(os.path.join(os.path.dirname(resume_path), RESUME_INFO), encoding="utf-8") as fh:
            info = json.load(fh)
        if info["size"] == os.path.getsize(resume_path):
            return info
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def is_within(path, parent):
    """True if path is parent or lies inside it."""
    path = os.path.normcase(os.path.abspath(path))
    parent = os.path.normcase(os.path.abspath(parent))
    try:
        return os.path.commonpath([path, parent]) == parent
    except ValueError:  # different drives
        return False


def read_bgzf_raw(fh):
    """Next BGZF block still compressed, as (raw, payload offset); None at end of file.
    ValueError if truncated/damaged."""
    head = fh.read(12)
    if not head:
        return None
    if len(head) < 12 or head[:4] != b"\x1f\x8b\x08\x04":
        raise ValueError("truncated or damaged block")
    xlen = struct.unpack_from("<H", head, 10)[0]
    extra = fh.read(xlen)
    size, i = None, 0
    while i + 4 <= len(extra):
        slen = struct.unpack_from("<H", extra, i + 2)[0]
        if extra[i:i + 2] == b"BC" and slen == 2 and i + 6 <= len(extra):
            size = struct.unpack_from("<H", extra, i + 4)[0] + 1
        i += 4 + slen
    if len(extra) < xlen or size is None or size < 12 + xlen + 8:
        raise ValueError("truncated or damaged block")
    rest = fh.read(size - 12 - xlen)
    if len(rest) < size - 12 - xlen:
        raise ValueError("truncated block")
    return head + extra + rest, 12 + xlen


def inflate_bgzf(raw, offset):
    """Decompressed content of a block from read_bgzf_raw. ValueError if damaged."""
    crc, isize = struct.unpack_from("<II", raw, len(raw) - 8)
    try:
        data = zlib.decompress(memoryview(raw)[offset:-8], -15)
    except zlib.error:
        raise ValueError("damaged block") from None
    if len(data) != isize or zlib.crc32(data) != crc:
        raise ValueError("damaged block")
    return data


def inflate_many(blocks):
    """inflate_bgzf for a list of blocks (run in a worker thread; zlib releases the GIL).
    The list stops with None at the first damaged block."""
    out = []
    for raw, offset in blocks:
        try:
            out.append(inflate_bgzf(raw, offset))
        except ValueError:
            out.append(None)
            break
    return out


def read_bgzf_block(fh):
    """Next BGZF block as (raw, data); None at end of file. ValueError if truncated/damaged."""
    blk = read_bgzf_raw(fh)
    if blk is None:
        return None
    return blk[0], inflate_bgzf(*blk)


def write_bgzf(out, data):
    for i in range(0, len(data), 0xff00):
        chunk = data[i:i + 0xff00]
        comp = zlib.compressobj(1, zlib.DEFLATED, -15)
        cdata = comp.compress(chunk) + comp.flush()
        out.write(b"\x1f\x8b\x08\x04\x00\x00\x00\x00\x00\xff\x06\x00BC\x02\x00"
                  + struct.pack("<H", len(cdata) + 25) + cdata
                  + struct.pack("<II", zlib.crc32(chunk), len(chunk)))


def read_bam_header(fh):
    """Read a BAM header. Returns (text, refs, rest): the SAM header text, the binary
    reference list and any record bytes that followed the header in the last block read."""
    buf = bytearray()

    def fill(n):
        while len(buf) < n:
            blk = read_bgzf_block(fh)
            if blk is None:
                raise ValueError("file is empty or ends inside the header")
            buf.extend(blk[1])

    fill(8)
    if buf[:4] != b"BAM\x01":
        raise ValueError("not a BAM file")
    l_text = struct.unpack_from("<i", buf, 4)[0]
    if l_text < 0:
        raise ValueError("damaged header")
    refs_start = pos = 8 + l_text
    fill(pos + 4)
    n_ref = struct.unpack_from("<i", buf, pos)[0]
    pos += 4
    for _ in range(n_ref):
        fill(pos + 4)
        l_name = struct.unpack_from("<i", buf, pos)[0]
        if l_name < 0:
            raise ValueError("damaged header")
        pos += 4 + l_name + 4
        fill(pos)
    return bytes(buf[8:8 + l_text]), bytes(buf[refs_start:pos]), bytes(buf[pos:])


def scan_bam_headers(paths):
    """Returns (good, bad): good = [(path, text, refs)], bad = [(path, reason)]."""
    good, bad = [], []
    for p in paths:
        try:
            with open(p, "rb") as fh:
                text, refs, _ = read_bam_header(fh)
            good.append((p, text, refs))
        except (OSError, ValueError) as e:
            bad.append((p, str(e)))
    return good, bad


def merged_header(headers):
    """Header of the first file plus any @RG lines that only occur in the others."""
    text, refs = headers[0]
    lines = text.rstrip(b"\x00").splitlines()

    def rg_id(line):
        for field in line.split(b"\t")[1:]:
            if field.startswith(b"ID:"):
                return field
        return None

    seen = {rg_id(ln) for ln in lines if ln.startswith(b"@RG")}
    for other, _ in headers[1:]:
        for ln in other.rstrip(b"\x00").splitlines():
            if ln.startswith(b"@RG") and rg_id(ln) not in seen:
                seen.add(rg_id(ln))
                lines.append(ln)
    text = b"\n".join(lines) + b"\n"
    return b"BAM\x01" + struct.pack("<i", len(text)) + text + refs


def basecaller_cl(text):
    """Command line of the `dorado basecaller` run recorded in a BAM header ('' if none)."""
    for line in text.rstrip(b"\x00").decode("utf-8", errors="replace").splitlines():
        if line.startswith("@PG"):
            fields = dict(f.split(":", 1) for f in line.split("\t")[1:] if ":" in f)
            if fields.get("ID", "").startswith("basecaller"):
                return fields.get("CL", "")
    return ""


def model_name(arg):
    """Model argument reduced to what dorado compares on resume (folder name for paths)."""
    arg = arg.strip().strip('"\'').rstrip("\\/")
    return re.split(r"[\\/]", arg)[-1]


def cl_model(cl):
    """Model argument of a recorded `dorado basecaller` command line ('' if unclear)."""
    try:
        tokens = shlex.split(cl, posix=False)
    except ValueError:
        return ""
    if "basecaller" in tokens[:-1]:
        tok = tokens[tokens.index("basecaller") + 1]
        if not tok.startswith("-"):
            return model_name(tok)
    return ""


class _RecordCopier:
    """Copies whole BAM records to `out`, block by block. Untouched BGZF blocks are copied
    as they are (no recompression); a partial record at the end of a file is dropped."""

    def __init__(self, out):
        self.out = out
        self.records = 0
        self.start_file()

    def start_file(self):
        self.need = 0        # bytes left of the current record
        self.lenbuf = b""    # partially read record length
        self.held = []       # (raw, data, cut) blocks after the last record end written

    def _write(self, raw, data):
        if raw is not None:
            self.out.write(raw)
        else:
            write_bgzf(self.out, data)

    def feed(self, raw, data):
        """Add a block; raw=None means it has to be recompressed. ValueError if damaged."""
        pos, n, last, bad = 0, len(data), 0, False
        while pos < n:
            if self.need:
                step = min(self.need, n - pos)
                pos += step
                self.need -= step
                if not self.need:
                    last = pos
                    self.records += 1
            else:
                take = min(4 - len(self.lenbuf), n - pos)
                self.lenbuf += data[pos:pos + take]
                pos += take
                if len(self.lenbuf) == 4:
                    self.need = struct.unpack("<i", self.lenbuf)[0]
                    self.lenbuf = b""
                    if not 32 <= self.need < 1 << 30:
                        bad = True
                        break
        if last:
            for r, d, _ in self.held:
                self._write(r, d)
            self.held = []
        if last == n:
            self._write(raw, data)
        elif n:
            self.held.append((raw, data, last))
        if bad:
            raise ValueError("damaged record")

    def finish_file(self):
        """Write what is complete; returns True if the file ended in the middle of a record."""
        cut_short = bool(self.held)
        if cut_short and self.held[0][2]:
            write_bgzf(self.out, self.held[0][1][:self.held[0][2]])
        self.start_file()
        return cut_short


def merge_bams(paths, header, out_path, progress=None, cancel=None):
    """Join BAM files into one. Returns (n_records, truncated, whole): truncated lists the
    files that were cut short (their complete records are still used), whole is the size of
    the files that were copied as they are and whose records are not in n_records.
    progress(done_bytes) is called now and then; cancel is a threading.Event.

    A file that ends with the BGZF end-of-file marker was closed properly, so it ends on a
    record boundary and its blocks are copied without looking inside. Only files without
    the marker (cut off by the crash) are decompressed, on several threads, to find the
    last complete record."""
    truncated, done, whole, last_report = [], 0, 0, 0.0
    workers = min(8, os.cpu_count() or 1)

    def tick(pos):
        nonlocal last_report
        if cancel is not None and cancel.is_set():
            raise InterruptedError
        if progress and time.time() - last_report > 0.25:
            last_report = time.time()
            progress(done + pos)

    with open(out_path, "wb", buffering=COPY_CHUNK) as out, ThreadPoolExecutor(workers) as pool:
        write_bgzf(out, header)
        copier = _RecordCopier(out)
        for p in paths:
            damaged = False
            with open(p, "rb", buffering=COPY_CHUNK) as fh:
                size = os.fstat(fh.fileno()).st_size
                complete = False
                if size > len(BGZF_EOF):
                    fh.seek(size - len(BGZF_EOF))
                    complete = fh.read() == BGZF_EOF
                    fh.seek(0)
                try:
                    _, _, rest = read_bam_header(fh)
                    if complete:
                        left = size - len(BGZF_EOF) - fh.tell()
                        if rest or left > 0:
                            whole += size
                        write_bgzf(out, rest)
                        while left > 0:
                            chunk = fh.read(min(COPY_CHUNK, left))
                            if not chunk:
                                break
                            out.write(chunk)
                            left -= len(chunk)
                            tick(fh.tell())
                        continue
                    copier.feed(None, rest)
                    at_end = False
                    while not at_end:
                        # read a batch of blocks, inflate them in parallel, walk them in order
                        blocks, cut = [], False
                        try:
                            while len(blocks) < INFLATE_BATCH * workers:
                                blk = read_bgzf_raw(fh)
                                if blk is None:
                                    at_end = True
                                    break
                                blocks.append(blk)
                        except ValueError:
                            cut = at_end = True  # the blocks before the broken one still count
                        parts = [blocks[i:i + INFLATE_BATCH] for i in range(0, len(blocks), INFLATE_BATCH)]
                        for part, datas in zip(parts, pool.map(inflate_many, parts)):
                            for (raw, _), data in zip(part, datas):
                                if data is None:
                                    raise ValueError("damaged block")
                                copier.feed(raw, data)
                        if cut:
                            raise ValueError("truncated block")
                        tick(fh.tell())
                except ValueError:
                    damaged = True
                finally:
                    done += size
            if copier.finish_file() or damaged:
                truncated.append(p)
        out.write(BGZF_EOF)
    return copier.records, truncated, whole


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
        self.merge_cancel = None  # threading.Event while the resume file is being written
        self.merge_thread = None
        self.resume_path = None
        self.stdout_path = None  # file dorado's stdout goes to (resumed runs only)
        self.progress_at = 0.0   # when dorado last reported progress itself
        self.out_base = 0        # size of the output folder before dorado started
        self.out_history = []    # (time, bytes written) of the last minutes, for the rate
        self.next_output_check = self.next_heartbeat = 0.0

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
        self.v_resume = tk.BooleanVar(value=False)  # deliberately not remembered between sessions
        self.v_resume_dir = tk.StringVar()
        self.v_resume_info = tk.StringVar(value="")
        self.v_mode =tk.StringVar(value="standard")  # standard | custom
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

    SAVED_KEYS = ["dorado", "models_dir", "input", "recursive", "output", "resume_dir", "mode", "type",
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
        ttk.Checkbutton(f, text="Resume from BAM folder:", variable=self.v_resume,
                        command=self._refresh_resume_widgets).grid(row=3, column=0, sticky="w")
        self.ent_resume = ttk.Entry(f, textvariable=self.v_resume_dir)
        self.ent_resume.grid(row=3, column=1, sticky="ew", padx=4)
        self.btn_resume = ttk.Button(f, text="Browse…", command=self._browse_resume)
        self.btn_resume.grid(row=3, column=2)
        ttk.Label(f, textvariable=self.v_resume_info, foreground="gray").grid(
            row=4, column=1, columnspan=2, sticky="w", padx=4)

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
                    self.v_device, self.v_extra, self.v_resume, self.v_resume_dir):
            var.trace_add("write", lambda *a: self._update_command())
        self.v_input.trace_add("write", lambda *a: self._update_pod5_count())
        self.v_resume_dir.trace_add("write", lambda *a: self._refresh_resume_widgets())
        self._refresh_resume_widgets()

    def _refresh_resume_widgets(self):
        on = self.v_resume.get()
        self.ent_resume.configure(state="normal" if on else "disabled")
        self.btn_resume.configure(state="normal" if on else "disabled")
        folder = self.v_resume_dir.get()
        if not on:
            self.v_resume_info.set("Tick to continue a crashed run: reads already in its BAM files "
                                   "are kept and not basecalled again.")
        elif folder and os.path.isdir(folder):
            bams = find_bams(folder)
            self.v_resume_info.set(f"{len(bams)} BAM file(s) found, {fmt_size(total_size(bams))}. "
                                   "Use the same model and options as the crashed run, "
                                   "and a new output folder.")
        else:
            self.v_resume_info.set("Select the crashed run's output folder (the one containing "
                                   "bam_pass, so bam_fail is included too).")

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

    def _browse_resume(self):
        path = filedialog.askdirectory(title="Select the folder with the crashed run's BAM files",
                                       initialdir=self.v_resume_dir.get() or self.v_output.get() or None)
        if path:
            self.v_resume_dir.set(os.path.normpath(path))

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
        if self.v_resume.get():
            # joined copy of the old BAM files, written just before dorado starts
            cmd += ["--resume-from", os.path.join(self.v_output.get() or "<output folder>", RESUME_FILE)]
        else:
            cmd += ["--output-dir", self.v_output.get() or "<output folder>"]
        if self.v_extra.get().strip():
            cmd += [t.strip('"') for t in shlex.split(self.v_extra.get(), posix=False)]
        return cmd

    def _stdout_file(self):
        """File a resumed run is written to (dorado's stdout); None for a normal run."""
        if not self.v_resume.get():
            return None
        return os.path.join(self.v_output.get() or "<output folder>",
                            RESUME_OUTPUT + (".fastq" if self.v_fastq.get() else ".bam"))

    @staticmethod
    def _cmd_to_str(cmd, stdout_file=None):
        text = subprocess.list2cmdline(cmd) if IS_WINDOWS else shlex.join(cmd)
        if stdout_file:
            text += " > " + (subprocess.list2cmdline([stdout_file]) if IS_WINDOWS else shlex.quote(stdout_file))
        return text

    def _update_command(self):
        try:
            text = self._cmd_to_str(self._build_command(), self._stdout_file())
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
        if self.v_resume.get():
            old, out = self.v_resume_dir.get(), self.v_output.get()
            if not old or not os.path.isdir(old):
                errs.append("Select the folder with the BAM files of the crashed run.")
            elif not find_bams(old):
                errs.append("No .bam files found in the resume folder.")
            elif out and (is_within(out, old) or is_within(old, out)):
                errs.append("When resuming, the output folder must be a new folder outside the one "
                            "holding the old BAM files, so the old files cannot be overwritten.")
        if errs:
            messagebox.showerror(APP_NAME, "\n".join(errs))
            return False
        out = self.v_output.get()
        target = self._stdout_file()
        if target and os.path.exists(target):
            if not messagebox.askyesno(APP_NAME, f"The output file already exists:\n{target}\n\n"
                                       "It will be overwritten. Continue?"):
                return False
        elif os.path.isdir(out) and os.listdir(out):
            if not messagebox.askyesno(APP_NAME, f"The output folder is not empty:\n{out}\n\n"
                                       "Dorado may add to or overwrite files there. Continue?"):
                return False
        return True

    # ------------------------------------------------------------------- run
    def _prepare_resume(self, cmd):
        """Check the old BAM files. Returns (paths, header, notes, info) or None to abort.
        info is set when the resume file of an earlier attempt can be used again."""
        folder = self.v_resume_dir.get()
        self.v_status.set("Checking previous BAM files…")
        self.update_idletasks()
        good, bad = scan_bam_headers(find_bams(folder))
        self.v_status.set("Idle")
        if not good:
            messagebox.showerror(APP_NAME, "None of the BAM files in the resume folder could be read.")
            return None
        paths = [p for p, _, _ in good]
        notes = [f"# Resuming from {len(paths)} BAM file(s) in {folder}"]
        notes += [f"# Skipped unreadable file {p}: {why}" for p, why in bad]

        cls = [basecaller_cl(text) for _, text, _ in good]
        old = cls[0]
        if len(set(cls)) > 1:
            if not messagebox.askyesno(APP_NAME, "The BAM files in the resume folder were written by "
                                       f"{len(set(cls))} different dorado commands, so the folder seems "
                                       "to hold more than one run.\n\nContinue anyway?"):
                return None
        if not old:
            if not messagebox.askyesno(APP_NAME, "The BAM files do not record a 'dorado basecaller' "
                                       "command, so dorado will probably refuse to resume from them."
                                       "\n\nTry anyway?"):
                return None
        else:
            notes.append(f"# Previous command: {old}")
            old_model, new_model = cl_model(old), model_name(cmd[2])
            if old_model and old_model != new_model:
                if not messagebox.askyesno(APP_NAME, "The crashed run used a different model selection:\n\n"
                                           f"    previous run:  {old_model}\n    now selected:  {new_model}\n\n"
                                           "Dorado only resumes when the model is the same.\n\nContinue anyway?"):
                    return None

        try:
            info = load_resume_info(cmd[cmd.index("--resume-from") + 1])
            if info and info.get("sources") != bam_signature(paths):
                info = None
        except OSError:
            info = None

        # the old reads are copied twice: into the resume file and into the new output
        need = (1 if info else 2) * total_size(paths)
        probe = os.path.abspath(self.v_output.get())
        while not os.path.isdir(probe) and os.path.dirname(probe) != probe:
            probe = os.path.dirname(probe)
        try:
            free = shutil.disk_usage(probe).free
        except OSError:
            free = None
        if free is not None and free < need:
            if not messagebox.askyesno(APP_NAME, f"Resuming needs about {fmt_size(need)} for the reads that "
                                       f"are already basecalled, but only {fmt_size(free)} is free on the "
                                       "output drive.\n\nContinue anyway?"):
                return None
        return paths, merged_header([(text, refs) for _, text, refs in good]), notes, info

    def _start(self):
        if self.proc is not None or self.merge_cancel is not None:
            return
        if not self._validate():
            return
        cmd = self._build_command()
        resume = None
        if self.v_resume.get():
            resume = self._prepare_resume(cmd)
            if resume is None:
                return
        self._save_settings()
        out = self.v_output.get()
        os.makedirs(out, exist_ok=True)

        stamp = time.strftime("%Y%m%d_%H%M%S")
        try:
            self.log_file = open(os.path.join(out, f"dorado_gui_{stamp}.log"), "w", encoding="utf-8")
        except OSError:
            self.log_file = None

        self._log_clear()
        self.stdout_path = self._stdout_file()
        self._log(f"# {time.strftime('%Y-%m-%d %H:%M:%S')}\n# {self._cmd_to_str(cmd, self.stdout_path)}\n\n")

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.v_progress.set("")
        if resume:
            self._start_merge(cmd, *resume)
        else:
            self._launch(cmd)

    def _start_merge(self, cmd, paths, header, notes, info):
        """Join the old BAM files into the --resume-from file, then launch dorado."""
        self._log("\n".join(notes) + "\n")
        self.resume_path = dest = cmd[cmd.index("--resume-from") + 1]
        if info:
            self._log("# Using the resume file left by the previous attempt (old BAM files unchanged)\n")
            self.start_time = time.time()
            self._merged(cmd, info.get("records", 0), info.get("truncated", []), info.get("whole", 0))
            return
        self._remove_resume_file(forget=False)  # outdated leftovers
        try:
            sources = bam_signature(paths)
        except OSError:
            sources = None
        self.merge_cancel = cancel = threading.Event()
        self.start_time = time.time()
        self.v_status.set("Preparing resume file…")
        total = total_size(paths) or 1

        def progress(done):
            self.msg_queue.put(("progress", f"Joining previous BAM files: {100 * done / total:.0f}%  "
                                            f"({fmt_size(done)} of {fmt_size(total)})"))

        def worker():
            try:
                n, truncated, whole = merge_bams(paths, header, dest + ".part", progress, cancel)
                os.replace(dest + ".part", dest)
                if sources is not None and (n or whole):
                    try:  # lets a later attempt reuse the file instead of joining again
                        with open(os.path.join(os.path.dirname(dest), RESUME_INFO), "w", encoding="utf-8") as fh:
                            json.dump({"sources": sources, "records": n, "truncated": truncated,
                                       "whole": whole, "size": os.path.getsize(dest)}, fh)
                    except OSError:
                        pass
                self.msg_queue.put(("merged", (cmd, n, truncated, whole)))
            except InterruptedError:
                self.msg_queue.put(("merge_failed", None))
            except OSError as e:
                self.msg_queue.put(("merge_failed", str(e)))

        self.merge_thread = threading.Thread(target=worker, daemon=True)
        self.merge_thread.start()

    def _merged(self, cmd, n, truncated, whole):
        self.merge_cancel = None
        self.v_progress.set("")
        for p in truncated:
            self._log(f"# Incomplete file (cut off by the crash), complete reads kept: {p}\n")
        # complete files are copied without counting their reads
        kept = " and ".join(([f"{fmt_size(whole)} of complete BAM files"] if whole else [])
                            + ([f"{n} reads from incomplete files"] if truncated and whole else [])
                            + ([f"{n} reads"] if not whole else []))
        self._log(f"# {kept} from the previous run will be kept and not basecalled again\n")
        if n == 0 and not whole:
            self._abort_start("The old BAM files contain no complete reads, so there is nothing to "
                              "resume from. Untick 'Resume from BAM folder' to basecall from scratch.")
            return
        self._log(f"# All reads are written to one file: {self.stdout_path}\n")
        if "--kit-name" in cmd:
            self._log("# Barcodes are stored in that file (BC tag); split it afterwards with "
                      "'dorado demux --no-classify'\n")
        self._log("\n")
        self._launch(cmd)

    def _merge_failed(self, error):
        self.merge_cancel = None
        self.v_progress.set("")
        if error is None:
            self._log("# Stopped by user\n")
            self._abort_start(None)
        else:
            self._abort_start(f"Could not write the resume file:\n{error}")

    def _abort_start(self, error, keep_resume_file=False):
        if error:
            self._log(error + "\n")
            messagebox.showerror(APP_NAME, error)
        if keep_resume_file:
            self.resume_path = None
        else:
            self._remove_resume_file()
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.v_status.set("Failed" if error else "Stopped")
        self._close_log()

    def _remove_resume_file(self, forget=True):
        """The resume file is only a temporary copy; the original BAM files are never touched."""
        if self.resume_path:
            for path in (self.resume_path, self.resume_path + ".part",
                         os.path.join(os.path.dirname(self.resume_path), RESUME_INFO)):
                try:
                    os.remove(path)
                except OSError:
                    pass
            if forget:
                self.resume_path = None

    def _launch(self, cmd):
        out_fh = None
        self.out_base = 0 if self.stdout_path else tree_size(self.v_output.get())
        try:
            if self.stdout_path:
                # the reads go straight into the file, only messages and progress come to the GUI
                out_fh = open(self.stdout_path, "wb")
                self.proc = subprocess.Popen(cmd, stdout=out_fh, stderr=subprocess.PIPE,
                                             stdin=subprocess.DEVNULL, **popen_kwargs())
                stream = self.proc.stderr
            else:
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, **popen_kwargs())
                stream = self.proc.stdout
        except OSError as e:
            self._abort_start(f"Failed to start dorado:\n{e}", keep_resume_file=True)
            return
        finally:
            if out_fh:
                out_fh.close()

        self.start_time = time.time()
        self.out_history = []
        self.next_output_check = self.start_time + OUTPUT_CHECK_SECS
        self.next_heartbeat = self.start_time + HEARTBEAT_SECS
        self.v_status.set("Running…")
        threading.Thread(target=self._reader, args=(self.proc, stream), daemon=True).start()
        self._tick()

    def _reader(self, proc, stream):
        """Read dorado's messages; '\\r'-terminated chunks are progress-bar updates."""
        buf = b""
        while True:
            chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(1)
            if not chunk:
                break
            buf += chunk
            while True:
                idx_n, idx_r = buf.find(b"\n"), buf.find(b"\r")
                cands = [i for i in (idx_n, idx_r) if i >= 0]
                if not cands:
                    break
                i = min(cands)
                if i == len(buf) - 1 and buf[i:] == b"\r":
                    break  # could be the first half of a Windows line end: wait for the next byte
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
        if self.merge_cancel is not None:
            if messagebox.askyesno(APP_NAME, "Stop preparing the resume file?") and self.merge_cancel:
                self.merge_cancel.set()
                self.v_status.set("Stopping…")
        elif self.proc and self.proc.poll() is None:
            if messagebox.askyesno(APP_NAME, "Stop the running basecalling job?"):
                self._log("\n# Stopped by user\n")
                self.proc.terminate()
                self.v_status.set("Stopping…")

    def _tick(self):
        if self.proc is not None:
            now = time.time()
            el = int(now - self.start_time)
            dur = f"{el // 3600:d}:{el % 3600 // 60:02d}:{el % 60:02d}"
            self.v_status.set(f"Running…  {dur}")
            if now >= self.next_output_check:
                self.next_output_check = now + OUTPUT_CHECK_SECS
                self._report_output(now, dur)
            self.after(1000, self._tick)

    def _output_size(self):
        """Bytes dorado has written so far in this run."""
        if self.stdout_path:
            try:
                return os.path.getsize(self.stdout_path)
            except OSError:
                return 0
        return max(0, tree_size(self.v_output.get()) - self.out_base)

    def _report_output(self, now, dur):
        """Progress line and regular log lines from the size of the output, as dorado
        reports no progress itself when it is not run in a terminal."""
        size = self._output_size()
        self.out_history.append((now, size))
        while len(self.out_history) > 2 and now - self.out_history[1][0] >= 300:
            del self.out_history[0]
        text = f"Output written so far: {fmt_size(size)}"
        t0, s0 = self.out_history[0]
        if now - t0 >= 60:
            text += f"  ({fmt_size(max(0, size - s0) * 60 / (now - t0))} per minute)"
        if now - self.progress_at > 3 * OUTPUT_CHECK_SECS:
            self.v_progress.set(text)
        if now >= self.next_heartbeat:
            self.next_heartbeat = now + HEARTBEAT_SECS
            self._log(f"# {time.strftime('%H:%M:%S')}  running for {dur}, {text[0].lower()}{text[1:]}\n")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "progress":
                    self.progress_at = time.time()
                    self.v_progress.set(payload[-160:])
                elif kind == "done":
                    self._finished(payload)
                elif kind == "merged":
                    self._merged(*payload)
                elif kind == "merge_failed":
                    self._merge_failed(payload)
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
        written = f"{fmt_size(self._output_size())} of output written"
        if rc == 0:
            self.v_status.set(f"Finished ✔  ({dur})")
            self._log(f"\n# Finished successfully in {dur}, {written}\n")
        else:
            self.v_status.set(f"Failed / stopped (exit code {rc})")
            self._log(f"\n# dorado exited with code {rc} after {dur}, {written}\n")
        self.v_progress.set("")
        if rc == 0:
            self._remove_resume_file()
        elif self.resume_path:
            # joining the old BAM files takes long; Start with the same folders uses the file again
            self._log(f"# Resume file kept for another attempt: {self.resume_path}\n")
            self.resume_path = None
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
        if self.merge_cancel is not None:
            if not messagebox.askyesno(APP_NAME, "The resume file is still being prepared. Stop and quit?"):
                return
            self.merge_cancel.set()
            self.merge_thread.join(10)
            self._remove_resume_file()
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
