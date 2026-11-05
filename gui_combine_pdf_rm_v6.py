#!/usr/bin/env python3
"""============================================================
Gui Combine PDF untuk RM V6
Combine PDF per pasien. Nama output = Nomor SEP (dibaca dari PDF pertama).
Hanya file TTE yang masuk Attachments (file asli, byte-identik).
File sumber TIDAK diubah / TIDAK dipindah / tidak dibuatkan folder baru.
RSU Puri Asih
============================================================
Folder pasien yang didukung:
    3805, 3806, 3807 ...                       (angka saja)
    0131R0291026V003805, 0131R...V001153 ...    (4 digit + R + angka + V + angka)
Folder yang ada spasi / format lain (mis. "3805 USG") -> dilewati.

Hasil:
    <Folder dipilih>\\Hasil Gabungan\\<NomorSEP>.pdf

TOMBOL TAMBAHAN (plugin):
    Taruh file  tombol_xxx_xxx.pyw / .py / .exe  di folder yang sama
    dengan aplikasi ini. Setiap file otomatis jadi satu tombol,
    labelnya dari nama file:
        tombol_cek_tte_pdf.pyw  ->  [CEK TTE PDF]
        tombol_cek_tte_pdf.exe  ->  [CEK TTE PDF]
    Saat diklik, file dijalankan dan menerima folder sumber yang
    sedang dipilih sebagai argumen pertama (sys.argv[1]).

Install:  pip install wxPython pikepdf pillow pymupdf
"""

import io
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

import wx
from wx.lib.buttons import GenButton
from PIL import Image, ImageOps, ImageSequence
import pikepdf

try:
    import pymupdf  # PyMuPDF versi baru
except ImportError:  # pragma: no cover
    import fitz as pymupdf  # PyMuPDF versi lama

# ============================================================
# CONFIG
# ============================================================
APP_TITLE = "Gui Combine PDF untuk RM V6"
DEFAULT_SOURCE_FOLDER = r"F:\TEs pdf rm\OKTOBER\28"

OUTPUT_SUBFOLDER_NAME = "Hasil Gabungan"
RAR_OUTPUT_SUBFOLDER_NAME = "Hasil RAR"

# Format nama folder pasien yang diterima:
#   3805                      -> angka saja
#   0131R0291026V003805       -> 4 digit + R + angka + V + angka
# Nama dengan spasi (mis. "3805 USG") otomatis TIDAK cocok -> dilewati.
FOLDER_PATTERN = re.compile(r"^(?:\d+|\d{4}R\d+V\d+)$")

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif"}

# "Nomor SEP: 0131R029026V003805"
SEP_LABEL_RE = re.compile(r"Nomor\s*SEP\s*[:：]?\s*(\d{4}R\d+V\d+)", re.IGNORECASE)
# cadangan: pola nomor SEP di mana saja pada teks
SEP_ANY_RE = re.compile(r"\b(\d{4}R\d+V\d+)\b", re.IGNORECASE)

RAR_EXE_PATH = r"C:\Program Files\WinRAR\Rar.exe"

# Tombol tambahan (plugin): file  tombol_xxx_xxx.pyw / .py / .exe  di folder aplikasi
PLUGIN_PREFIX = "tombol_"
PLUGIN_EXTENSIONS = (".pyw", ".py", ".exe")


def get_app_dir() -> Path:
    """Folder tempat aplikasi berada (juga benar saat dijadikan .exe)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = get_app_dir()
LOG_FILE = APP_DIR / "combine_pdf_rm_v6.log"

# Warna log
COLOR_BG = wx.Colour(12, 12, 12)
COLOR_INFO = wx.Colour(0, 230, 0)       # hijau
COLOR_WARN = wx.Colour(255, 200, 0)     # kuning
COLOR_ERROR = wx.Colour(255, 80, 80)    # merah
COLOR_WHITE = wx.Colour(255, 255, 255)  # putih -> PDF ada tanda tangan elektronik

COLOR_BY_NAME = {
    "white": COLOR_WHITE,
    "green": COLOR_INFO,
    "red": COLOR_ERROR,
    "yellow": COLOR_WARN,
}

# ============================================================
# LOGGING
# ============================================================
log = logging.getLogger("combine_pdf_v6")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
try:
    _fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
except OSError:
    pass  # folder tidak bisa ditulis -> log hanya tampil di GUI


class WxLogHandler(logging.Handler):
    """Kirim log ke panel terminal di GUI (thread-safe).
    Warna bisa dipaksa lewat log.info(..., extra={"color": "white|green|red|yellow"})."""

    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        self.setFormatter(_fmt)

    def emit(self, record):
        try:
            color = getattr(record, "color", None)
            wx.CallAfter(self.frame.append_log, self.format(record), record.levelno, color)
        except Exception:
            pass


# ============================================================
# TOMBOL TAMBAHAN (PLUGIN .pyw / .py / .exe)
# ============================================================
def plugin_label(script: Path) -> str:
    """tombol_cek_tte_pdf.pyw / .py / .exe -> 'CEK TTE PDF'"""
    name = script.stem
    if name.lower().startswith(PLUGIN_PREFIX):
        name = name[len(PLUGIN_PREFIX):]
    label = name.replace("_", " ").strip().upper()
    return label or script.stem.upper()


def find_plugin_scripts():
    """Cari semua file tombol_*.pyw / .py / .exe di folder aplikasi (urut nama)."""
    try:
        return sorted(
            [
                p for p in APP_DIR.iterdir()
                if p.is_file()
                and p.name.lower().startswith(PLUGIN_PREFIX)
                and p.suffix.lower() in PLUGIN_EXTENSIONS
            ],
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return []


def launch_plugin(script: Path, source_folder: str):
    """Jalankan file .pyw / .py / .exe di proses terpisah.
    Folder sumber dikirim sebagai argumen pertama (sys.argv[1])."""
    cwd = str(script.parent)

    # --- file .exe: langsung dijalankan ---
    if script.suffix.lower() == ".exe":
        subprocess.Popen([str(script), source_folder], cwd=cwd)
        return

    # --- file .pyw / .py ---
    if os.name == "nt":
        if not getattr(sys, "frozen", False):
            exe = Path(sys.executable)
            pythonw = exe.with_name("pythonw.exe")   # tanpa jendela console
            interpreter = pythonw if pythonw.exists() else exe
            subprocess.Popen(
                [str(interpreter), str(script), source_folder],
                cwd=cwd,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            # Saat aplikasi berupa .exe, serahkan ke asosiasi file Windows
            try:
                os.startfile(str(script), arguments=f'"{source_folder}"', cwd=cwd)  # type: ignore[attr-defined]
            except TypeError:
                os.startfile(str(script))  # type: ignore[attr-defined]
    else:
        subprocess.Popen([sys.executable, str(script), source_folder], cwd=cwd)


# ============================================================
# LOGIKA INTI
# ============================================================
def natural_sort_key(f: Path):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", f.stem)
    ]


def natural_sort_name(name: str):
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", name)
    ]


def image_to_pdf_bytes(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes))

    def prep(frame):
        frame = ImageOps.exif_transpose(frame)
        if frame.mode in ("RGBA", "P", "1", "L", "CMYK"):
            frame = frame.convert("RGB")
        return frame

    frames = [prep(frame.copy()) for frame in ImageSequence.Iterator(img)]
    buf = io.BytesIO()
    if len(frames) > 1:
        frames[0].save(buf, format="PDF", save_all=True, append_images=frames[1:])
    else:
        frames[0].save(buf, format="PDF")
    return buf.getvalue()


def has_digital_signature(pdf_doc: pikepdf.Pdf) -> bool:
    try:
        if "/AcroForm" in pdf_doc.Root and "/Fields" in pdf_doc.Root.AcroForm:
            for field in pdf_doc.Root.AcroForm.Fields:
                if "/FT" in field and field.FT == "/Sig":
                    return True

        for page in pdf_doc.pages:
            if "/Annots" in page:
                for annot in page.Annots:
                    if "/Subtype" in annot and annot.Subtype == "/Widget":
                        if "/FT" in annot and annot.FT == "/Sig":
                            return True

        for obj in pdf_doc.objects:
            if isinstance(obj, pikepdf.Dictionary):
                if "/Type" in obj and obj.Type in ("/Sig", "/DocTimeStamp"):
                    return True
                if "/FT" in obj and obj.FT == "/Sig":
                    return True
    except Exception:
        pass
    return False


def extract_sep_from_pdf(pdf_path: Path):
    """Baca teks PDF (read-only, file asli tidak diubah) dan cari Nomor SEP."""
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            texts = []
            for page in doc:
                text = page.get_text()
                m = SEP_LABEL_RE.search(text)
                if m:
                    return m.group(1).upper()
                texts.append(text)
            m = SEP_ANY_RE.search("\n".join(texts))
            if m:
                return m.group(1).upper()
    except Exception as e:
        log.warning("  Tidak bisa membaca teks '%s': %s", pdf_path.name, e)
    return None


def find_sep_number(files):
    """PDF pertama (urut nama) diprioritaskan; jika tak ketemu, coba PDF berikutnya."""
    pdf_files = [f for f in files if f.suffix.lower() == ".pdf"]
    for idx, f in enumerate(pdf_files):
        sep = extract_sep_from_pdf(f)
        if sep:
            if idx > 0:
                log.warning("  Nomor SEP tidak ada di PDF pertama, ditemukan di '%s'", f.name)
            return sep, f
    return None, None


def compress_to_rar(pdf_path: Path, rar_output_folder: Path, keep_pdf: bool):
    if not Path(RAR_EXE_PATH).exists():
        log.error("RAR_EXE_PATH tidak ditemukan (%s). Kompresi RAR dilewati.", RAR_EXE_PATH)
        return

    rar_output_folder.mkdir(parents=True, exist_ok=True)
    rar_path = rar_output_folder / (pdf_path.stem + ".rar")
    cmd = [RAR_EXE_PATH, "a", "-ep1", "-o+", str(rar_path), str(pdf_path)]

    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=flags)
        log.info("Berhasil dikompres ke RAR: %s", rar_path)
    except subprocess.CalledProcessError as e:
        log.error("Gagal kompres ke RAR untuk %s: %s", pdf_path.name, e.stderr or e)
        return

    if not keep_pdf:
        pdf_path.unlink()


def safe_replace(src: Path, dst: Path):
    if dst.exists():
        try:
            dst.unlink()
        except PermissionError:
            time.sleep(0.5)
            if dst.exists():
                dst.unlink()
    src.replace(dst)


def list_patient_files(patient_folder: Path):
    return sorted(
        [
            f
            for f in patient_folder.iterdir()
            if f.is_file()
            and f.suffix.lower() in SUPPORTED_EXTENSIONS
            and not f.name.startswith(("~", "."))
        ],
        key=natural_sort_key,
    )


def open_folder(path: Path):
    """Buka folder di file explorer."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        log.error("Gagal membuka folder %s: %s", path, e)


# ============================================================
# WORKER THREAD  (mode "run" = combine, mode "check" = ceking)
# ============================================================
class Worker(threading.Thread):
    def __init__(self, frame, source_folder: Path, mode: str = "run",
                 use_rar: bool = False, keep_pdf: bool = True, skip_existing: bool = True):
        super().__init__(daemon=True)
        self.frame = frame
        self.source = source_folder
        self.mode = mode
        self.use_rar = use_rar
        self.keep_pdf = keep_pdf
        self.skip_existing = skip_existing

        self.run_event = threading.Event()   # set = jalan, clear = pause
        self.run_event.set()
        self.stop_event = threading.Event()

        self.done = 0
        self.total = 0
        self.combined = 0       # jumlah file yang sudah ter-combine
        self.failed_count = 0   # jumlah file/folder gagal

    # --- kontrol ---
    def pause(self):
        self.run_event.clear()

    def resume(self):
        self.run_event.set()

    def stop(self):
        self.stop_event.set()
        self.run_event.set()

    def should_stop(self) -> bool:
        self.run_event.wait()
        return self.stop_event.is_set()

    def tick(self, n=1):
        self.done += n
        wx.CallAfter(self.frame.update_progress, self.done, self.total)

    # --- util ---
    def collect_folders(self):
        """Ambil subfolder pasien yang formatnya valid. Folder lain dilewati."""
        folders = []
        for item in sorted(self.source.iterdir(), key=lambda p: natural_sort_name(p.name)):
            if not item.is_dir():
                continue
            if item.name in (OUTPUT_SUBFOLDER_NAME, RAR_OUTPUT_SUBFOLDER_NAME):
                continue
            if item.name.startswith("."):
                continue
            if not FOLDER_PATTERN.match(item.name):
                log.warning("Dilewati, nama folder tidak sesuai format: %s", item.name)
                continue
            if not list_patient_files(item):
                log.info("Dilewati, tidak ada file PDF/gambar di folder %s", item.name)
                continue
            folders.append(item)
        return folders

    # --- jalur utama ---
    def run(self):
        try:
            if self.mode == "check":
                self._check()
            else:
                self._run()
        except Exception as e:
            log.error("Kesalahan tak terduga: %s", e)
        finally:
            wx.CallAfter(self.frame.on_finished, self.stop_event.is_set(), self.mode)

    # ---------- COMBINE ----------
    def _run(self):
        if not self.source.exists():
            log.error("Folder sumber tidak ditemukan: %s", self.source)
            return

        output_folder = self.source / OUTPUT_SUBFOLDER_NAME
        output_folder.mkdir(exist_ok=True)
        rar_output_folder = self.source / RAR_OUTPUT_SUBFOLDER_NAME
        if self.use_rar:
            rar_output_folder.mkdir(exist_ok=True)

        folders = self.collect_folders()

        self.total = sum(len(list_patient_files(f)) for f in folders)
        wx.CallAfter(self.frame.update_progress, 0, self.total)
        log.info("Ditemukan %d folder pasien, total %d file.", len(folders), self.total)

        if self.total == 0:
            log.info("Tidak ada file untuk diproses. Pastikan folder yang dipilih berisi subfolder pasien.")
            return

        all_failed = []
        for item in folders:
            if self.should_stop():
                break
            try:
                all_failed.extend(
                    self.combine_patient_folder(item, output_folder, rar_output_folder)
                )
            except Exception as e:
                log.error("GAGAL proses folder %s: %s", item.name, e)
                all_failed.append((item.name, "(seluruh folder)", str(e)))

        self.failed_count = len(all_failed)
        if all_failed:
            log.warning("=" * 60)
            log.warning("RINGKASAN YANG GAGAL DIPROSES:")
            for folder, nama_file, pesan in all_failed:
                log.warning("  - Folder %s | %s | sebab: %s", folder, nama_file, pesan)
            log.warning("=" * 60)

    def combine_patient_folder(self, patient_folder, output_folder, rar_output_folder):
        folder_name = patient_folder.name
        failed = []

        files = list_patient_files(patient_folder)
        if not files:
            return failed

        # 1. Baca Nomor SEP dari PDF pertama (urut nama)
        sep, sep_src = find_sep_number(files)
        if not sep:
            msg = "Nomor SEP tidak ditemukan di PDF manapun (PDF hasil scan/gambar?)"
            log.error("Folder %s: %s. Folder dilewati.", folder_name, msg)
            failed.append((folder_name, "(Nomor SEP)", msg))
            self.tick(len(files))
            return failed

        log.info("Folder %s -> Nomor SEP %s (dari '%s')", folder_name, sep, sep_src.name)
        output_path = output_folder / f"{sep}.pdf"

        # 2. Opsional: lewati bila hasil sudah ada
        if self.skip_existing and output_path.exists():
            log.info("  Dilewati, hasil sudah ada: %s", output_path.name)
            self.combined += len(files)
            self.tick(len(files))
            return failed

        # 3. Buat PDF gabungan BARU (file sumber tidak disentuh sama sekali)
        pdf_out = pikepdf.Pdf.new()
        ok_count = 0
        stopped = False

        for src in files:
            if self.should_stop():
                stopped = True
                break
            try:
                ext = src.suffix.lower()
                if ext in IMAGE_EXTENSIONS:
                    pdf_bytes = image_to_pdf_bytes(src.read_bytes())
                    with pikepdf.Pdf.open(io.BytesIO(pdf_bytes)) as img_pdf:
                        pdf_out.add_pages_from(img_pdf)
                    log.info("  [Gambar] '%s' ditempel visualnya.", src.name)
                else:
                    with pikepdf.Pdf.open(src) as src_pdf:
                        pdf_out.add_pages_from(src_pdf)
                        if has_digital_signature(src_pdf):
                            # file asli dilampirkan apa adanya (byte-identik) -> TTE utuh
                            pdf_out.attachments[src.name] = src.read_bytes()
                            log.info(
                                "  [TTE Detected] '%s' ada tanda tangan elektronik -> masuk lampiran.",
                                src.name,
                                extra={"color": "white"},
                            )
                        else:
                            log.info("  [No TTE] '%s' hanya ditempel visualnya.", src.name)
                ok_count += 1
            except Exception as e:
                log.error("GAGAL memproses '%s' di folder %s: %s", src.name, folder_name, e)
                failed.append((folder_name, src.name, str(e)))
            self.tick()

        # Dihentikan di tengah folder -> hasil parsial dibuang
        if stopped:
            pdf_out.close()
            log.warning("  Folder %s dihentikan di tengah proses, hasil parsial tidak disimpan.", folder_name)
            return failed

        if ok_count == 0:
            pdf_out.close()
            return failed

        # 4. Simpan hasil (tulis ke file sementara dulu, lalu ganti)
        tmp_path = output_path.with_suffix(".tmp.pdf")
        pdf_out.save(tmp_path)
        pdf_out.close()
        safe_replace(tmp_path, output_path)
        self.combined += ok_count

        log.info("Selesai folder %s (%d file) -> %s", folder_name, ok_count, output_path.name)

        if self.use_rar:
            compress_to_rar(output_path, rar_output_folder, self.keep_pdf)

        return failed

    # ---------- CEKING PDF SETELAH COMBINE ----------
    def _check(self):
        if not self.source.exists():
            log.error("Folder sumber tidak ditemukan: %s", self.source)
            return

        output_folder = self.source / OUTPUT_SUBFOLDER_NAME
        if not output_folder.exists():
            log.warning("Folder '%s' belum ada, semua hasil akan dianggap tidak ada.", OUTPUT_SUBFOLDER_NAME)

        folders = self.collect_folders()
        self.total = len(folders)
        wx.CallAfter(self.frame.update_progress, 0, self.total)
        log.info("=== CEKING === %d folder pasien dicocokkan dengan folder '%s'",
                 len(folders), OUTPUT_SUBFOLDER_NAME)

        ada = 0
        tidak_ada = 0
        for item in folders:
            if self.should_stop():
                break

            files = list_patient_files(item)
            sep, _src = find_sep_number(files)

            if not sep:
                tidak_ada += 1
                log.error(
                    "Tidak ada file > (Nomor SEP tidak terbaca) | folder %s", item.name,
                    extra={"color": "red"},
                )
            else:
                expected = f"{sep}.pdf"
                if (output_folder / expected).exists():
                    ada += 1
                    log.info("Ada File > %s | folder %s", expected, item.name,
                             extra={"color": "green"})
                else:
                    tidak_ada += 1
                    log.error("Tidak ada file > %s | folder %s", expected, item.name,
                              extra={"color": "red"})
            self.tick()

        self.combined = ada
        self.failed_count = tidak_ada
        summary_color = "green" if tidak_ada == 0 else "red"
        log.info("=" * 60, extra={"color": summary_color})
        log.info("HASIL CEK: Ada %d | Tidak ada %d | Total folder %d", ada, tidak_ada, len(folders),
                 extra={"color": summary_color})
        log.info("=" * 60, extra={"color": summary_color})


# ============================================================
# DIALOG SELESAI
# ============================================================
class DoneDialog(wx.Dialog):
    def __init__(self, parent, message: str, folder: Path):
        super().__init__(parent, title=APP_TITLE, style=wx.DEFAULT_DIALOG_STYLE)
        self.folder = folder

        sizer = wx.BoxSizer(wx.VERTICAL)
        lbl = wx.StaticText(self, label=message, style=wx.ALIGN_CENTER_HORIZONTAL)
        lbl.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(lbl, 0, wx.ALL | wx.ALIGN_CENTER_HORIZONTAL, 22)

        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        btn_ok = wx.Button(self, wx.ID_OK, "OK")
        btn_open = wx.Button(self, wx.ID_ANY, "BUKA FOLDER")
        btn_row.Add(btn_ok, 0, wx.RIGHT, 10)
        btn_row.Add(btn_open, 0)
        sizer.Add(btn_row, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.BOTTOM, 18)

        btn_ok.SetDefault()
        btn_open.Bind(wx.EVT_BUTTON, self.on_open)

        self.SetSizerAndFit(sizer)
        self.SetMinSize((320, -1))
        self.CentreOnParent()

    def on_open(self, _evt):
        open_folder(self.folder)
        self.EndModal(wx.ID_OPEN)


# ============================================================
# GUI
# ============================================================
class MainFrame(wx.Frame):
    GREEN = wx.Colour(40, 167, 69)
    YELLOW = wx.Colour(255, 193, 7)
    RED = wx.Colour(220, 53, 69)
    BLUE = wx.Colour(0, 123, 255)
    PURPLE = wx.Colour(111, 66, 193)

    def __init__(self):
        super().__init__(None, title=APP_TITLE, size=(1150, 780))
        self.worker = None
        self.last_output_folder = None
        self.plugin_scripts = find_plugin_scripts()

        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.HORIZONTAL)

        # ---------- KIRI ----------
        left = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(panel, label="Combine PDF per Nomor SEP")
        title.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        left.Add(title, 0, wx.ALL, 10)

        left.Add(wx.StaticText(panel, label="Folder sumber (berisi subfolder pasien):"),
                 0, wx.LEFT | wx.RIGHT, 10)
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.txt_folder = wx.TextCtrl(panel, value=DEFAULT_SOURCE_FOLDER)
        self.btn_pick = wx.Button(panel, label="Pilih Folder")
        row.Add(self.txt_folder, 1, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 6)
        row.Add(self.btn_pick, 0)
        left.Add(row, 0, wx.EXPAND | wx.ALL, 10)

        self.chk_skip = wx.CheckBox(panel, label="Lewati folder yang hasilnya sudah ada")
        self.chk_skip.SetValue(True)
        self.chk_rar = wx.CheckBox(panel, label="Kompres hasil ke RAR")
        self.chk_rar.SetValue(True)
        self.chk_keep = wx.CheckBox(panel, label="Simpan PDF setelah dikompres RAR")
        self.chk_keep.SetValue(True)
        left.Add(self.chk_skip, 0, wx.LEFT | wx.RIGHT, 10)
        left.Add(self.chk_rar, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        left.Add(self.chk_keep, 0, wx.ALL, 10)

        self.btn_run = self._make_btn(panel, "JALANKAN", self.GREEN, wx.WHITE)
        self.btn_pause = self._make_btn(panel, "PAUSE", self.YELLOW, wx.BLACK)
        self.btn_stop = self._make_btn(panel, "BERHENTI", self.RED, wx.WHITE)
        brow = wx.BoxSizer(wx.HORIZONTAL)
        for b in (self.btn_run, self.btn_pause, self.btn_stop):
            brow.Add(b, 1, wx.RIGHT, 6)
        left.Add(brow, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        self.btn_check = self._make_btn(panel, "CEKING PDF SETELAH COMBINE", self.BLUE, wx.WHITE)
        left.Add(self.btn_check, 0, wx.EXPAND | wx.ALL, 10)

        self.gauge = wx.Gauge(panel, range=100, size=(-1, 28))
        self.lbl_progress = wx.StaticText(panel, label="0/0 -- 0%", style=wx.ALIGN_CENTER_HORIZONTAL)
        self.lbl_progress.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        left.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
        left.Add(self.lbl_progress, 0, wx.EXPAND | wx.ALL, 6)

        self.lbl_status = wx.StaticText(panel, label="Status: Siap")
        left.Add(self.lbl_status, 0, wx.ALL, 10)

        legend = wx.StaticText(
            panel,
            label="Warna log:  putih = PDF ada TTE  |  hijau = info / ada file\n"
                  "kuning = peringatan  |  merah = error / tidak ada file",
        )
        left.Add(legend, 0, wx.ALL, 10)

        # ---------- TOMBOL TAMBAHAN (dari file tombol_*.pyw / .py / .exe) ----------
        self.plugin_buttons = []
        if self.plugin_scripts:
            left.Add(wx.StaticText(panel, label="Tombol tambahan:"), 0, wx.LEFT | wx.RIGHT, 10)

            plugin_panel = wx.ScrolledWindow(panel, style=wx.VSCROLL)
            plugin_panel.SetScrollRate(0, 10)
            plugin_sizer = wx.BoxSizer(wx.VERTICAL)
            for script in self.plugin_scripts:
                btn = self._make_btn(plugin_panel, plugin_label(script), self.PURPLE, wx.WHITE, height=38)
                btn.SetToolTip(script.name)
                btn.Bind(wx.EVT_BUTTON, lambda evt, s=script: self.on_plugin(s))
                plugin_sizer.Add(btn, 0, wx.EXPAND | wx.BOTTOM, 6)
                self.plugin_buttons.append(btn)
            plugin_panel.SetSizer(plugin_sizer)
            plugin_panel.SetMinSize((-1, min(len(self.plugin_scripts) * 44 + 4, 140)))
            left.Add(plugin_panel, 0, wx.EXPAND | wx.ALL, 10)

        # ---------- KANAN: LOG ----------
        right = wx.BoxSizer(wx.VERTICAL)
        right.Add(wx.StaticText(panel, label="LOG"), 0, wx.BOTTOM, 4)
        self.txt_log = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.HSCROLL
        )
        self.txt_log.SetBackgroundColour(COLOR_BG)
        self.txt_log.SetForegroundColour(COLOR_INFO)
        self.txt_log.SetFont(wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        right.Add(self.txt_log, 1, wx.EXPAND)

        root.Add(left, 0, wx.EXPAND | wx.ALL, 6)
        root.Add(right, 1, wx.EXPAND | wx.ALL, 10)
        left.SetMinSize((430, -1))
        panel.SetSizer(root)

        self.btn_pick.Bind(wx.EVT_BUTTON, self.on_pick)
        self.btn_run.Bind(wx.EVT_BUTTON, self.on_run)
        self.btn_pause.Bind(wx.EVT_BUTTON, self.on_pause)
        self.btn_stop.Bind(wx.EVT_BUTTON, self.on_stop)
        self.btn_check.Bind(wx.EVT_BUTTON, self.on_check)
        self.Bind(wx.EVT_CLOSE, self.on_close)

        self._set_running(False)
        self.handler = WxLogHandler(self)
        log.addHandler(self.handler)

        if self.plugin_scripts:
            log.info("Tombol tambahan dimuat: %s",
                     ", ".join(f"{plugin_label(s)} ({s.name})" for s in self.plugin_scripts))

        # Buka aplikasi langsung full screen (maximized)
        self.Maximize(True)

    # ---------- helper UI ----------
    def _make_btn(self, parent, label, bg, fg, height=44):
        b = GenButton(parent, label=label, size=(-1, height))
        b.SetBackgroundColour(bg)
        b.SetForegroundColour(fg)
        b.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        return b

    def _set_running(self, running: bool):
        self.btn_run.Enable(not running)
        self.btn_check.Enable(not running)
        self.btn_pause.Enable(running)
        self.btn_stop.Enable(running)
        self.btn_pick.Enable(not running)
        self.txt_folder.Enable(not running)
        self.chk_skip.Enable(not running)
        self.chk_rar.Enable(not running)
        self.chk_keep.Enable(not running)
        if not running:
            self.btn_pause.SetLabel("PAUSE")

    def append_log(self, text: str, level: int, color_name=None):
        if color_name in COLOR_BY_NAME:
            color = COLOR_BY_NAME[color_name]
        elif level >= logging.ERROR:
            color = COLOR_ERROR
        elif level >= logging.WARNING:
            color = COLOR_WARN
        else:
            color = COLOR_INFO
        self.txt_log.SetDefaultStyle(wx.TextAttr(color, COLOR_BG))
        self.txt_log.AppendText(text + "\n")

    def update_progress(self, done: int, total: int):
        pct = int(done * 100 / total) if total else 0
        self.gauge.SetRange(max(total, 1))
        self.gauge.SetValue(min(done, max(total, 1)))
        self.lbl_progress.SetLabel(f"{done}/{total} -- {pct}%")

    def _get_source(self):
        folder = Path(self.txt_folder.GetValue().strip())
        if not folder.exists():
            wx.MessageBox("Folder sumber tidak ditemukan.", APP_TITLE, wx.ICON_ERROR)
            return None
        return folder

    # ---------- event handler ----------
    def on_pick(self, _evt):
        with wx.DirDialog(self, "Pilih folder sumber", defaultPath=self.txt_folder.GetValue()) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.txt_folder.SetValue(dlg.GetPath())

    def on_plugin(self, script: Path):
        """Jalankan file tombol_*.pyw / .py / .exe; folder sumber dikirim sebagai argumen pertama."""
        if not script.exists():
            wx.MessageBox(f"File tidak ditemukan:\n{script}", APP_TITLE, wx.ICON_ERROR)
            return
        folder = self.txt_folder.GetValue().strip()
        try:
            launch_plugin(script, folder)
            log.info("Menjalankan tombol tambahan: %s", script.name)
        except Exception as e:
            log.error("Gagal menjalankan %s: %s", script.name, e)
            wx.MessageBox(f"Gagal menjalankan {script.name}:\n{e}", APP_TITLE, wx.ICON_ERROR)

    def on_run(self, _evt):
        folder = self._get_source()
        if not folder:
            return
        self.last_output_folder = folder / OUTPUT_SUBFOLDER_NAME
        self.txt_log.Clear()
        self.update_progress(0, 0)
        self.worker = Worker(
            self, folder, mode="run",
            use_rar=self.chk_rar.GetValue(),
            keep_pdf=self.chk_keep.GetValue(),
            skip_existing=self.chk_skip.GetValue(),
        )
        self._set_running(True)
        self.lbl_status.SetLabel("Status: Berjalan...")
        log.info("=== MULAI === Folder: %s", folder)
        self.worker.start()

    def on_check(self, _evt):
        folder = self._get_source()
        if not folder:
            return
        self.last_output_folder = folder / OUTPUT_SUBFOLDER_NAME
        self.txt_log.Clear()
        self.update_progress(0, 0)
        self.worker = Worker(self, folder, mode="check")
        self._set_running(True)
        self.lbl_status.SetLabel("Status: Mengecek hasil combine...")
        self.worker.start()

    def on_pause(self, _evt):
        if not self.worker:
            return
        if self.worker.run_event.is_set():
            self.worker.pause()
            self.btn_pause.SetLabel("LANJUTKAN")
            self.lbl_status.SetLabel("Status: Pause (menunggu proses saat ini selesai)")
            log.info("--- PAUSE ---")
        else:
            self.worker.resume()
            self.btn_pause.SetLabel("PAUSE")
            self.lbl_status.SetLabel("Status: Berjalan...")
            log.info("--- LANJUT ---")

    def on_stop(self, _evt):
        if not self.worker:
            return
        if wx.MessageBox(
            "Hentikan proses sekarang?\n(Folder yang sedang diproses tidak disimpan parsial)",
            APP_TITLE,
            wx.YES_NO | wx.ICON_QUESTION,
        ) != wx.YES:
            return
        self.lbl_status.SetLabel("Status: Menghentikan...")
        self.worker.stop()

    def on_finished(self, stopped: bool, mode: str):
        worker = self.worker
        self.worker = None
        self._set_running(False)

        if stopped:
            self.lbl_status.SetLabel("Status: Dihentikan")
            log.warning("=== DIHENTIKAN OLEH PENGGUNA ===")
            return

        if mode == "check":
            self.lbl_status.SetLabel("Status: Ceking selesai")
            log.info("=== CEKING SELESAI ===")
            return

        self.lbl_status.SetLabel("Status: Selesai")
        log.info("=== SELESAI ===")

        # Pop-up kecil setelah combine selesai
        if worker is not None and worker.total > 0:
            msg = f"{worker.combined}/{worker.total} File Sudah di Combine"
            if worker.failed_count:
                msg += f"\n({worker.failed_count} gagal, cek LOG)"
            folder = self.last_output_folder or (Path(self.txt_folder.GetValue()) / OUTPUT_SUBFOLDER_NAME)
            with DoneDialog(self, msg, folder) as dlg:
                dlg.ShowModal()

    def on_close(self, evt):
        if self.worker and self.worker.is_alive():
            if wx.MessageBox("Proses masih berjalan. Hentikan dan keluar?",
                             APP_TITLE, wx.YES_NO | wx.ICON_WARNING) != wx.YES:
                evt.Veto()
                return
            self.worker.stop()
            self.worker.join(timeout=10)
        log.removeHandler(self.handler)
        evt.Skip()


if __name__ == "__main__":
    app = wx.App(False)
    MainFrame().Show()
    app.MainLoop()
