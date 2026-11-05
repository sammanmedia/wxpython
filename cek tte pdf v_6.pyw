import os
import sys
import time
import shutil
import sqlite3
import threading
import subprocess
import webbrowser
from contextlib import closing
from datetime import datetime

import wx
import wx.grid

from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

# ================== PENGATURAN ==================
CEK_URL = "https://cekdokumen.id/"
LEBAR_PERSEN = 0.5     # lebar tiap jendela (0.5 = setengah lebar layar)
TINGGI_PERSEN = 0.5    # tinggi tiap jendela (0.5 = setengah tinggi layar) -> 1/4 layar

FONT_DEFAULT = 10      # ukuran font awal tabel (pt)
FONT_MIN = 7
FONT_MAX = 28

DB_NAMA = "db_tte_ceklis.db"
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, DB_NAMA)

IS_WIN = sys.platform.startswith("win")
KELAS_BROWSER = "Chrome_WidgetWin_1"   # kelas jendela Chrome & Edge
KELAS_EXPLORER = "CabinetWClass"       # kelas jendela File Explorer

# ================== STATUS & WARNA ==================
ST_VALID = "TTE VALID"
ST_MOD = "TTE SUDAH DI MODIFIKASI"
ST_NONE = "TANPA TTD"
ST_ERR = "GAGAL DIBACA"
FLT_GABUNG = f"{ST_VALID} + {ST_MOD}"

WARNA = {
    ST_VALID: wx.Colour(198, 239, 206),   # hijau
    ST_MOD: wx.Colour(255, 192, 128),     # orange
    ST_NONE: wx.Colour(230, 230, 230),    # abu-abu
    ST_ERR: wx.Colour(255, 199, 206),     # merah muda
}

# Ceklis
TXT_SUDAH = "SUDAH DI CEK"
TXT_BELUM = "BELUM"
WARNA_BARIS_SUDAH = wx.Colour(153, 204, 255)   # background satu baris jika sudah dicek (biru)
WARNA_LABEL_SUDAH = wx.Colour(0, 90, 200)      # label "SUDAH DI CEK" (biru)
WARNA_LABEL_BELUM = wx.Colour(240, 130, 0)     # label "BELUM" (orange)

KOLOM = [("No", 50), ("Nama File", 300), ("Status", 190), ("Detail", 330),
         ("Penandatangan", 220), ("Lokasi", 380), ("Aksi", 490), ("Ceklis", 210)]
COL_LOKASI = 5
COL_AKSI = 6
COL_CEKLIS = 7
JUMLAH_KOL = len(KOLOM)

COVER = {
    "ENTIRE_FILE": "Seluruh file (tidak ada tambahan setelah TTD)",
    "ENTIRE_REVISION": "Seluruh revisi (ada data ditambahkan setelah TTD)",
    "CONTIGUOUS_BLOCK_FROM_START": "Blok awal file saja",
    "UNCLEAR": "Tidak jelas",
}
MODLVL = {
    "NONE": "Tidak ada perubahan",
    "LTA_UPDATES": "Hanya pembaruan validasi jangka panjang (sah)",
    "FORM_FILLING": "Pengisian formulir (diizinkan)",
    "ANNOTATIONS": "Penambahan anotasi / komentar",
    "OTHER": "Perubahan lain di luar yang diizinkan",
}


# ================== DATABASE (db_tte_ceklis.db) ==================
def kunci_path(p):
    return os.path.normcase(os.path.normpath(p))


def _koneksi():
    return sqlite3.connect(DB_PATH, timeout=10)


def db_init():
    with closing(_koneksi()) as con:
        with con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS ceklis (
                    kunci       TEXT PRIMARY KEY,
                    path        TEXT NOT NULL,
                    nama_file   TEXT,
                    sudah_dicek INTEGER NOT NULL DEFAULT 0,
                    waktu_cek   TEXT
                )""")
            con.execute("""
                CREATE TABLE IF NOT EXISTS pengaturan (
                    kunci TEXT PRIMARY KEY,
                    nilai TEXT
                )""")


def siapkan_db():
    """Buat database di folder aplikasi; jika folder tidak bisa ditulis, pakai folder user."""
    global DB_PATH
    try:
        db_init()
    except Exception:
        DB_PATH = os.path.join(os.path.expanduser("~"), DB_NAMA)
        db_init()


def db_muat_ceklis():
    with closing(_koneksi()) as con:
        cur = con.execute("SELECT kunci FROM ceklis WHERE sudah_dicek = 1")
        return {r[0]: True for r in cur.fetchall()}


def db_simpan_ceklis(path, nama, sudah):
    with closing(_koneksi()) as con:
        with con:
            con.execute(
                "INSERT OR REPLACE INTO ceklis (kunci, path, nama_file, sudah_dicek, waktu_cek) "
                "VALUES (?, ?, ?, ?, ?)",
                (kunci_path(path), path, nama, 1 if sudah else 0,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def db_baca_pengaturan(kunci, default=None):
    try:
        with closing(_koneksi()) as con:
            cur = con.execute("SELECT nilai FROM pengaturan WHERE kunci = ?", (kunci,))
            r = cur.fetchone()
            return r[0] if r else default
    except Exception:
        return default


def db_simpan_pengaturan(kunci, nilai):
    try:
        with closing(_koneksi()) as con:
            with con:
                con.execute("INSERT OR REPLACE INTO pengaturan (kunci, nilai) VALUES (?, ?)",
                            (kunci, str(nilai)))
    except Exception:
        pass


# ================== HELPER UMUM ==================
def aman(fn, default="-"):
    try:
        v = fn()
        if v is None or v == "":
            return default
        return v
    except Exception:
        return default


def baris(label, nilai):
    return f"{label:<32}: {nilai}"


def ya_tidak(v):
    if v is None:
        return "-"
    return "Ya" if v else "Tidak"


# ================== PEMERIKSAAN TTD ==================
def evaluasi_sig(sig):
    """Return (status, keterangan, objek_status_pyhanko)"""
    try:
        status = validate_pdf_signature(sig, ValidationContext(allow_fetching=False))
    except Exception as e:
        return ST_MOD, f"gagal validasi ({e})", None

    if not (status.intact and status.valid):
        return ST_MOD, "isi dokumen tidak cocok dengan tanda tangan", status

    catatan = []
    try:
        if status.coverage.name != "ENTIRE_FILE":
            catatan.append("ada revisi/data tambahan setelah TTD")
    except Exception:
        pass
    try:
        lvl = status.modification_level
        if lvl is not None and lvl.name != "NONE":
            catatan.append(f"perubahan setelah TTD: {lvl.name}")
    except Exception:
        pass

    if catatan:
        return ST_VALID, "; ".join(catatan), status
    return ST_VALID, "utuh, tidak ada perubahan", status


def cek_pdf(path):
    """Return (status, detail, penandatangan)"""
    try:
        with open(path, "rb") as f:
            reader = PdfFileReader(f, strict=False)
            sigs = list(reader.embedded_signatures)
            if not sigs:
                return ST_NONE, "Tidak ada tanda tangan elektronik", ""

            hasil, penanda = [], []
            for sig in sigs:
                nama = aman(lambda: sig.signer_cert.subject.native.get("common_name"), None)
                if not nama:
                    nama = aman(lambda: sig.signer_cert.subject.human_friendly, sig.field_name or "-")
                penanda.append(str(nama))
                kode, alasan, _ = evaluasi_sig(sig)
                hasil.append((kode, f"{sig.field_name}: {alasan}"))

            if any(h[0] == ST_MOD for h in hasil):
                final = ST_MOD
            else:
                final = ST_VALID
            return final, " | ".join(h[1] for h in hasil), "; ".join(penanda)
    except Exception as e:
        return ST_ERR, f"File tidak bisa dibaca / rusak: {e}", ""


def ambil_detail(path):
    """Teks detail seluruh tanda tangan elektronik di sebuah PDF."""
    L = []
    L.append(baris("File", os.path.basename(path)))
    L.append(baris("Lokasi", os.path.dirname(path)))
    L.append(baris("Ukuran file", aman(lambda: f"{os.path.getsize(path):,} byte")))

    try:
        with open(path, "rb") as f:
            reader = PdfFileReader(f, strict=False)
            sigs = list(reader.embedded_signatures)
            total_rev = aman(lambda: reader.xrefs.total_revisions)
            L.append(baris("Total revisi dokumen", total_rev))
            L.append(baris("Jumlah tanda tangan", len(sigs)))

            if not sigs:
                L.append("")
                L.append("PDF ini tidak memiliki tanda tangan elektronik.")

            for i, sig in enumerate(sigs, 1):
                L.append("")
                L.append("=" * 76)
                L.append(f"TANDA TANGAN {i} DARI {len(sigs)}")
                L.append("=" * 76)

                kode, alasan, st = evaluasi_sig(sig)
                L.append(baris("HASIL", kode))
                L.append(baris("Keterangan", alasan))

                # ---- Penandatangan ----
                cert = sig.signer_cert
                sn = aman(lambda: dict(cert.subject.native), {})
                L.append("")
                L.append("[ PENANDATANGAN ]")
                L.append(baris("Nama (CN)", sn.get("common_name", "-")))
                L.append(baris("Organisasi", sn.get("organization_name", "-")))
                L.append(baris("Unit organisasi", sn.get("organizational_unit_name", "-")))
                L.append(baris("Negara", sn.get("country_name", "-")))
                L.append(baris("Email", sn.get("email_address", "-")))
                L.append(baris("Subjek lengkap", aman(lambda: cert.subject.human_friendly)))

                # ---- Sertifikat ----
                L.append("")
                L.append("[ SERTIFIKAT ]")
                L.append(baris("Penerbit (Issuer)", aman(lambda: cert.issuer.human_friendly)))
                L.append(baris("Nomor seri (hex)", aman(lambda: format(cert.serial_number, "X"))))
                L.append(baris("Berlaku dari", aman(lambda: cert["tbs_certificate"]["validity"]["not_before"].native)))
                L.append(baris("Berlaku sampai", aman(lambda: cert["tbs_certificate"]["validity"]["not_after"].native)))
                L.append(baris("Self-signed", aman(lambda: cert.self_signed)))
                L.append(baris("Algoritma kunci publik",
                               aman(lambda: f"{cert.public_key.algorithm.upper()} {cert.public_key.bit_size} bit")))
                L.append(baris("Algoritma tanda tangan sert.", aman(lambda: cert.signature_algo)))
                L.append(baris("Sidik jari SHA-256", aman(lambda: cert.sha256_fingerprint)))
                L.append(baris("Sidik jari SHA-1", aman(lambda: cert.sha1_fingerprint)))
                L.append(baris("Sertifikat tertanam lain", aman(lambda: len(sig.other_embedded_certs))))

                # ---- Data tanda tangan ----
                so = sig.sig_object
                L.append("")
                L.append("[ DATA TANDA TANGAN ]")
                L.append(baris("Nama field", sig.field_name))
                L.append(baris("Format (SubFilter)", aman(lambda: str(so["/SubFilter"]))))
                L.append(baris("Nama (di dokumen)", aman(lambda: str(so["/Name"]))))
                L.append(baris("Alasan", aman(lambda: str(so["/Reason"]))))
                L.append(baris("Lokasi penandatanganan", aman(lambda: str(so["/Location"]))))
                L.append(baris("Kontak", aman(lambda: str(so["/ContactInfo"]))))
                L.append(baris("Waktu TTD (klaim penanda)", aman(lambda: sig.self_reported_timestamp)))
                L.append(baris("Waktu TTD (field /M)", aman(lambda: str(so["/M"]))))
                L.append(baris("Revisi yang memuat TTD", aman(lambda: sig.signed_revision)))
                L.append(baris("Total revisi dokumen", total_rev))

                # ---- Hasil pemeriksaan ----
                if st is not None:
                    L.append("")
                    L.append("[ HASIL PEMERIKSAAN ]")
                    L.append(baris("Isi tidak berubah (intact)", ya_tidak(st.intact)))
                    L.append(baris("Tanda tangan kriptografi valid", ya_tidak(st.valid)))

                    cov = aman(lambda: st.coverage.name, "-")
                    L.append(baris("Cakupan tanda tangan", f"{cov} - {COVER.get(cov, '')}"))

                    lvl = aman(lambda: st.modification_level.name, "-")
                    L.append(baris("Level perubahan setelah TTD", f"{lvl} - {MODLVL.get(lvl, '')}"))

                    L.append(baris("Sesuai kebijakan DocMDP", ya_tidak(aman(lambda: st.docmdp_ok, None))))
                    L.append(baris("Algoritma digest", aman(lambda: st.md_algorithm)))
                    L.append(baris("Mekanisme tanda tangan", aman(lambda: st.pkcs7_signature_mechanism)))
                    L.append(baris("Waktu timestamp (TSA)", aman(lambda: st.timestamp_validity.timestamp)))

                    if st.trusted:
                        L.append(baris("Sertifikat terpercaya (CA root)", "Ya"))
                    else:
                        L.append(baris("Sertifikat terpercaya (CA root)",
                                       "Belum diverifikasi (root CA tidak dimuat di aplikasi)"))

                    L.append("")
                    L.append("[ RINGKASAN pyHanko ]")
                    L.append(aman(lambda: st.summary()))
                    ringkas = aman(lambda: st.pretty_print_details(), "")
                    if ringkas:
                        L.append("")
                        L.append(str(ringkas))
    except Exception as e:
        L.append("")
        L.append(f"GAGAL membaca file: {e}")
    return "\n".join(L)


# ================== PENATAAN JENDELA (WINDOWS / Win32) ==================
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


def set_dpi_thread():
    """Pakai koordinat piksel fisik di thread ini (supaya akurat di layar dengan scaling 125%/150%)."""
    if not IS_WIN:
        return
    try:
        user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
    except Exception:
        pass


def kelas_jendela(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def area_kerja_monitor(hwnd_app):
    """(x, y, lebar, tinggi) area kerja (tanpa taskbar) monitor tempat aplikasi berada."""
    mon = user32.MonitorFromWindow(hwnd_app, 2)      # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def daftar_jendela(nama_kelas, min_w=0, min_h=0):
    """Return {hwnd: judul} jendela top-level yang terlihat, tanpa owner, dengan kelas tertentu."""
    hasil = {}
    if not IS_WIN:
        return hasil

    def cb(hwnd, lparam):
        try:
            if kelas_jendela(hwnd) != nama_kelas or not user32.IsWindowVisible(hwnd):
                return True
            if user32.GetWindow(hwnd, 4):            # GW_OWNER -> lewati jendela turunan
                return True
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            if (r.right - r.left) < min_w or (r.bottom - r.top) < min_h:
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            t = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, t, n + 1)
            hasil[hwnd] = t.value
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return hasil


def tunggu_jendela_baru(nama_kelas, sebelum, timeout, min_w=0, min_h=0):
    akhir = time.time() + timeout
    while time.time() < akhir:
        for hwnd in daftar_jendela(nama_kelas, min_w, min_h):
            if hwnd not in sebelum:
                return hwnd
        time.sleep(0.1)
    return None


def pasang_jendela(hwnd, x, y, w, h):
    """Taruh jendela tepat di (x, y, w, h), dengan koreksi bingkai bayangan Windows 10/11."""
    SW_RESTORE = 9
    SWP_SHOWWINDOW = 0x0040
    user32.ShowWindow(hwnd, SW_RESTORE)                 # keluar dari maximize / minimize
    user32.SetWindowPos(hwnd, None, x, y, w, h, SWP_SHOWWINDOW)
    try:
        r = wintypes.RECT()
        f = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)) and \
                dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(f), ctypes.sizeof(f)) == 0:
            kiri = f.left - r.left
            atas = f.top - r.top
            kanan = r.right - f.right
            bawah = r.bottom - f.bottom
            user32.SetWindowPos(hwnd, None, x - kiri, y - atas,
                                w + kiri + kanan, h + atas + bawah, SWP_SHOWWINDOW)
    except Exception:
        pass


def cari_browser():
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LOCALAPPDATA", "")
    kandidat = [
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(lad, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for k in kandidat:
        if k and os.path.isfile(k):
            return k
    return shutil.which("chrome") or shutil.which("msedge")


def jalankan_browser(x, y, w, h):
    """Buka browser BARU (profil terpisah supaya ukuran/posisi dituruti). Return True jika Chrome/Edge ketemu."""
    exe = cari_browser()
    if not exe:
        webbrowser.open_new(CEK_URL)
        return False
    dasar = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    profil = os.path.join(dasar, "CekTTD_Browser_" + os.path.splitext(os.path.basename(exe))[0])
    subprocess.Popen([
        exe,
        f"--user-data-dir={profil}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        f"--window-position={x},{y}",
        f"--window-size={w},{h}",
        CEK_URL,
    ])
    return True


def jalankan_explorer(path):
    """Buka File Explorer pada folder file dan langsung menyorot file-nya."""
    subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')


def atur_jendela_browser(sebelum, pos):
    hwnd = tunggu_jendela_baru(KELAS_BROWSER, sebelum, timeout=12, min_w=300, min_h=200)
    if not hwnd:
        return False
    pasang_jendela(hwnd, *pos)
    time.sleep(0.7)                  # Chrome kadang mengubah ukuran sendiri setelah tampil -> pasang ulang
    pasang_jendela(hwnd, *pos)
    return True


def atur_jendela_explorer(path, sebelum, pos):
    hwnd = tunggu_jendela_baru(KELAS_EXPLORER, sebelum, timeout=5)
    if not hwnd:
        # Explorer mungkin memakai ulang jendela yang sudah ada -> cari berdasarkan nama folder
        nama = os.path.basename(os.path.dirname(os.path.normpath(path))).lower()
        for hw, judul in daftar_jendela(KELAS_EXPLORER).items():
            if judul.lower() == nama:
                hwnd = hw
                break
    if not hwnd:
        fg = user32.GetForegroundWindow()
        if fg and kelas_jendela(fg) == KELAS_EXPLORER:
            hwnd = fg
    if not hwnd:
        return False
    pasang_jendela(hwnd, *pos)
    time.sleep(0.5)
    pasang_jendela(hwnd, *pos)
    return True


# ================== RENDERER KOLOM AKSI (3 TOMBOL) ==================
LABEL_TOMBOL = ("Folder Lokasi", "Detail Tanda Tangan", "cekdokumen.id")


def tombol_rects(rect, n=len(LABEL_TOMBOL)):
    pad = 4
    w = (rect.width - pad * (n + 1)) // n
    h = rect.height - pad
    y = rect.y + pad // 2
    return [wx.Rect(rect.x + pad + i * (w + pad), y, w, h) for i in range(n)]


class TombolRenderer(wx.grid.GridCellRenderer):
    def Draw(self, grid, attr, dc, rect, row, col, isSelected):
        dc.SetClippingRegion(rect)
        dc.SetBrush(wx.Brush(attr.GetBackgroundColour()))
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.DrawRectangle(rect)

        dc.SetFont(grid.GetDefaultCellFont())
        dc.SetTextForeground(wx.BLACK)
        for r, label in zip(tombol_rects(rect), LABEL_TOMBOL):
            wx.RendererNative.Get().DrawPushButton(grid, dc, r)
            tw, th = dc.GetTextExtent(label)
            dc.DrawText(label, r.x + (r.width - tw) // 2, r.y + (r.height - th) // 2)
        dc.DestroyClippingRegion()

    def GetBestSize(self, grid, attr, dc, row, col):
        return wx.Size(480, 28)

    def Clone(self):
        return TombolRenderer()


# ================== RENDERER KOLOM CEKLIS (CHECKBOX + LABEL) ==================
class CeklisRenderer(wx.grid.GridCellRenderer):
    def Draw(self, grid, attr, dc, rect, row, col, isSelected):
        sudah = (grid.GetCellValue(row, col) == TXT_SUDAH)

        dc.SetClippingRegion(rect)
        dc.SetBrush(wx.Brush(attr.GetBackgroundColour()))
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.DrawRectangle(rect)

        # kotak centang
        ukuran = max(14, min(rect.height - 8, 24))
        cb = wx.Rect(rect.x + 8, rect.y + (rect.height - ukuran) // 2, ukuran, ukuran)
        wx.RendererNative.Get().DrawCheckBox(grid, dc, cb, wx.CONTROL_CHECKED if sudah else 0)

        # label berwarna: SUDAH DI CEK (biru) / BELUM (orange)
        teks = TXT_SUDAH if sudah else TXT_BELUM
        warna = WARNA_LABEL_SUDAH if sudah else WARNA_LABEL_BELUM
        f = wx.Font(grid.GetDefaultCellFont())
        f.MakeBold()
        dc.SetFont(f)
        tw, th = dc.GetTextExtent(teks)
        pill = wx.Rect(cb.x + cb.width + 10, rect.y + 4, tw + 22, max(rect.height - 8, th + 4))
        dc.SetBrush(wx.Brush(warna))
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.DrawRoundedRectangle(pill, 6)
        dc.SetTextForeground(wx.WHITE)
        dc.DrawText(teks, pill.x + (pill.width - tw) // 2, pill.y + (pill.height - th) // 2)
        dc.DestroyClippingRegion()

    def GetBestSize(self, grid, attr, dc, row, col):
        return wx.Size(200, 28)

    def Clone(self):
        return CeklisRenderer()


# ================== DIALOG DETAIL ==================
class DetailDialog(wx.Dialog):
    def __init__(self, parent, judul, teks):
        super().__init__(parent, title=judul, size=(860, 680),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.teks = teks
        v = wx.BoxSizer(wx.VERTICAL)
        self.tc = wx.TextCtrl(self, value=teks,
                              style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.tc.SetFont(wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        v.Add(self.tc, 1, wx.ALL | wx.EXPAND, 8)

        h = wx.BoxSizer(wx.HORIZONTAL)
        btn_salin = wx.Button(self, label="Salin")
        btn_tutup = wx.Button(self, wx.ID_CANCEL, label="Tutup")
        h.AddStretchSpacer(1)
        h.Add(btn_salin, 0, wx.ALL, 5)
        h.Add(btn_tutup, 0, wx.ALL, 5)
        v.Add(h, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.SetSizer(v)

        btn_salin.Bind(wx.EVT_BUTTON, self.on_salin)
        self.CentreOnParent()

    def on_salin(self, e):
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self.teks))
            wx.TheClipboard.Close()
            wx.MessageBox("Detail berhasil disalin ke clipboard.", "Info", wx.ICON_INFORMATION)


# ================== FRAME UTAMA ==================
class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Cek Tanda Tangan Elektronik PDF v6", size=(1300, 700))
        self.rows = []      # (no, nama, status, detail, penandatangan, path)
        self.view = []      # baris yang sedang tampil di grid
        self.sort_col = -1
        self.sort_asc = True
        self.stop_flag = False

        # --- Database ceklis ---
        self.db_ok = True
        self.cek = {}
        try:
            siapkan_db()
            self.cek = db_muat_ceklis()
        except Exception as ex:
            self.db_ok = False
            wx.MessageBox(f"Database {DB_NAMA} tidak bisa dibuat:\n{ex}\n\n"
                          "Ceklis tetap bisa dipakai, tapi tidak akan tersimpan.",
                          "Peringatan", wx.ICON_WARNING)

        # --- Ukuran font tersimpan ---
        self.font_size = FONT_DEFAULT
        if self.db_ok:
            try:
                self.font_size = int(db_baca_pengaturan("font_size", FONT_DEFAULT))
            except Exception:
                self.font_size = FONT_DEFAULT
        self.font_size = max(FONT_MIN, min(FONT_MAX, self.font_size))

        panel = wx.Panel(self)
        vbox = wx.BoxSizer(wx.VERTICAL)

        # --- Baris atas ---
        top = wx.BoxSizer(wx.HORIZONTAL)
        self.txt_folder = wx.TextCtrl(panel)
        btn_browse = wx.Button(panel, label="Pilih Folder")
        self.chk_sub = wx.CheckBox(panel, label="Termasuk subfolder")
        self.chk_sub.SetValue(True)
        self.btn_scan = wx.Button(panel, label="SCAN")
        self.btn_stop = wx.Button(panel, label="Stop")
        self.btn_stop.Disable()
        top.Add(self.txt_folder, 1, wx.ALL | wx.EXPAND, 5)
        top.Add(btn_browse, 0, wx.ALL, 5)
        top.Add(self.chk_sub, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        top.Add(self.btn_scan, 0, wx.ALL, 5)
        top.Add(self.btn_stop, 0, wx.ALL, 5)
        vbox.Add(top, 0, wx.EXPAND)

        # --- Baris filter + ukuran font ---
        va = wx.ALL | wx.ALIGN_CENTER_VERTICAL
        flt = wx.BoxSizer(wx.HORIZONTAL)
        flt.Add(wx.StaticText(panel, label="Filter status:"), 0, va, 5)
        self.cmb = wx.ComboBox(panel,
                               choices=["SEMUA", ST_VALID, ST_MOD, FLT_GABUNG, ST_NONE, ST_ERR],
                               style=wx.CB_READONLY, size=(350, -1))
        self.cmb.SetSelection(0)
        flt.Add(self.cmb, 0, va, 5)

        flt.AddSpacer(8)
        flt.Add(wx.StaticText(panel, label="Ukuran font:"), 0, va, 5)
        self.btn_kecil = wx.Button(panel, label="-", size=(36, -1))
        self.btn_kecil.SetToolTip("Perkecil font tabel")
        self.lbl_font = wx.StaticText(panel, label=str(self.font_size), size=(32, -1),
                                      style=wx.ALIGN_CENTRE_HORIZONTAL)
        self.btn_besar = wx.Button(panel, label="+", size=(36, -1))
        self.btn_besar.SetToolTip("Perbesar font tabel")
        flt.Add(self.btn_kecil, 0, va, 2)
        flt.Add(self.lbl_font, 0, va, 2)
        flt.Add(self.btn_besar, 0, va, 2)

        flt.AddSpacer(8)
        flt.Add(wx.StaticText(panel, label="Cari nama:"), 0, va, 5)
        self.txt_cari = wx.TextCtrl(panel)
        flt.Add(self.txt_cari, 1, va, 5)
        vbox.Add(flt, 0, wx.EXPAND)

        # --- Tabel (Grid) ---
        self.grid = wx.grid.Grid(panel)
        self.grid.CreateGrid(0, JUMLAH_KOL)
        self.grid.EnableEditing(False)
        self.grid.HideRowLabels()
        self.grid.DisableDragRowSize()
        self.grid.SetCellHighlightPenWidth(0)
        self.grid.SetColLabelAlignment(wx.ALIGN_LEFT, wx.ALIGN_CENTER)
        # sorotan baris terpilih dibuat kuning muda supaya tidak tertukar dengan baris biru "sudah dicek"
        self.grid.SetSelectionBackground(wx.Colour(255, 242, 153))
        self.grid.SetSelectionForeground(wx.BLACK)
        try:
            self.grid.SetSelectionMode(wx.grid.Grid.SelectRows)
        except Exception:
            pass
        rasio = self.font_size / float(FONT_DEFAULT)
        for i, (judul, lebar) in enumerate(KOLOM):
            self.grid.SetColLabelValue(i, judul)
            self.grid.SetColSize(i, max(30, int(lebar * rasio)))

        attr_aksi = wx.grid.GridCellAttr()
        attr_aksi.SetRenderer(TombolRenderer())
        attr_aksi.SetReadOnly(True)
        self.grid.SetColAttr(COL_AKSI, attr_aksi)

        attr_cek = wx.grid.GridCellAttr()
        attr_cek.SetRenderer(CeklisRenderer())
        attr_cek.SetReadOnly(True)
        self.grid.SetColAttr(COL_CEKLIS, attr_cek)

        self.terapkan_font()

        vbox.Add(self.grid, 1, wx.ALL | wx.EXPAND, 5)

        # --- Ringkasan + progress ---
        self.lbl = wx.StaticText(panel, label="Siap.")
        self.gauge = wx.Gauge(panel, range=100)
        vbox.Add(self.lbl, 0, wx.LEFT | wx.RIGHT, 8)
        vbox.Add(self.gauge, 0, wx.ALL | wx.EXPAND, 5)

        panel.SetSizer(vbox)

        # --- Event ---
        btn_browse.Bind(wx.EVT_BUTTON, self.on_browse)
        self.btn_scan.Bind(wx.EVT_BUTTON, self.on_scan)
        self.btn_stop.Bind(wx.EVT_BUTTON, lambda e: setattr(self, "stop_flag", True))
        self.btn_kecil.Bind(wx.EVT_BUTTON, lambda e: self.ubah_font(-1))
        self.btn_besar.Bind(wx.EVT_BUTTON, lambda e: self.ubah_font(+1))
        self.cmb.Bind(wx.EVT_COMBOBOX, lambda e: self.refresh())
        self.txt_cari.Bind(wx.EVT_TEXT, lambda e: self.refresh())
        self.grid.Bind(wx.grid.EVT_GRID_LABEL_LEFT_CLICK, self.on_label)
        self.grid.Bind(wx.grid.EVT_GRID_CELL_LEFT_CLICK, self.on_cell_click)
        self.grid.Bind(wx.grid.EVT_GRID_CELL_LEFT_DCLICK, self.on_cell_dclick)

        self.update_tombol_font()
        self.update_ringkasan()
        self.Maximize(True)
        self.Centre()

    # ---------- font ----------
    def terapkan_font(self):
        g = self.grid
        f = wx.Font(self.font_size, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        fb = wx.Font(self.font_size, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        g.SetDefaultCellFont(f)
        g.SetLabelFont(fb)

        # tinggi baris mengikuti tinggi teks (aman untuk layar dengan scaling)
        dc = wx.ClientDC(g)
        dc.SetFont(f)
        tinggi_teks = dc.GetTextExtent("Ag")[1]
        g.SetDefaultRowSize(tinggi_teks + 14, True)
        g.SetColLabelSize(tinggi_teks + 16)

    def ubah_font(self, delta):
        baru = max(FONT_MIN, min(FONT_MAX, self.font_size + delta))
        if baru == self.font_size:
            return
        rasio = baru / float(self.font_size)
        self.font_size = baru

        # lebar kolom ikut menyesuaikan (lebar hasil geser manual user tetap proporsional)
        for c in range(JUMLAH_KOL):
            self.grid.SetColSize(c, max(30, int(self.grid.GetColSize(c) * rasio)))

        self.terapkan_font()
        self.lbl_font.SetLabel(str(baru))
        self.update_tombol_font()
        if self.db_ok:
            db_simpan_pengaturan("font_size", baru)
        self.refresh()

    def update_tombol_font(self):
        self.btn_kecil.Enable(self.font_size > FONT_MIN)
        self.btn_besar.Enable(self.font_size < FONT_MAX)

    # ---------- ceklis ----------
    def sudah_dicek(self, path):
        return self.cek.get(kunci_path(path), False)

    def warna_baris(self, r):
        if self.sudah_dicek(r[5]):
            return WARNA_BARIS_SUDAH
        return WARNA[r[2]]

    def toggle_ceklis(self, row):
        data = self.view[row]
        path, nama = data[5], data[1]
        baru = not self.sudah_dicek(path)

        if self.db_ok:
            try:
                db_simpan_ceklis(path, nama, baru)
            except Exception as ex:
                self.db_ok = False
                wx.MessageBox(f"Gagal menyimpan ke {DB_NAMA}:\n{ex}\n\n"
                              "Ceklis tetap berlaku selama aplikasi terbuka, tapi tidak tersimpan.",
                              "Peringatan", wx.ICON_WARNING)

        if baru:
            self.cek[kunci_path(path)] = True
        else:
            self.cek.pop(kunci_path(path), None)

        # perbarui tampilan baris ini saja
        g = self.grid
        warna = self.warna_baris(data)
        for c in range(JUMLAH_KOL):
            g.SetCellBackgroundColour(row, c, warna)
        g.SetCellValue(row, COL_CEKLIS, TXT_SUDAH if baru else TXT_BELUM)
        g.ForceRefresh()
        self.update_ringkasan()

    # ---------- scan ----------
    def on_browse(self, e):
        dlg = wx.DirDialog(self, "Pilih folder berisi PDF")
        if dlg.ShowModal() == wx.ID_OK:
            self.txt_folder.SetValue(dlg.GetPath())
        dlg.Destroy()

    def on_scan(self, e):
        folder = self.txt_folder.GetValue().strip()
        if not os.path.isdir(folder):
            wx.MessageBox("Folder tidak ditemukan.", "Info", wx.ICON_WARNING)
            return
        self.rows = []
        self.refresh()
        self.stop_flag = False
        self.btn_scan.Disable()
        self.btn_stop.Enable()
        threading.Thread(target=self.worker, args=(folder, self.chk_sub.GetValue()),
                         daemon=True).start()

    def worker(self, folder, sub):
        files = []
        if sub:
            for r, _, fs in os.walk(folder):
                for f in fs:
                    if f.lower().endswith(".pdf"):
                        files.append(os.path.join(r, f))
        else:
            for f in os.listdir(folder):
                if f.lower().endswith(".pdf"):
                    files.append(os.path.join(folder, f))
        files.sort()
        total = len(files)
        wx.CallAfter(self.gauge.SetRange, max(total, 1))

        for i, p in enumerate(files, 1):
            if self.stop_flag:
                break
            st, det, pen = cek_pdf(p)
            self.rows.append((i, os.path.basename(p), st, det, pen, os.path.normpath(p)))
            wx.CallAfter(self.update_progress, i, total)
        wx.CallAfter(self.selesai)

    def update_progress(self, i, total):
        self.gauge.SetValue(i)
        self.lbl.SetLabel(f"Memeriksa {i} / {total} ...")
        if i % 10 == 0:
            self.refresh()

    def selesai(self):
        self.btn_scan.Enable()
        self.btn_stop.Disable()
        self.refresh()

    # ---------- tampilan ----------
    def refresh(self):
        flt = self.cmb.GetValue()
        cari = self.txt_cari.GetValue().lower()
        rows = list(self.rows)

        def lolos(r):
            if flt == FLT_GABUNG:
                ok = r[2] in (ST_VALID, ST_MOD)
            elif flt in ("", "SEMUA"):
                ok = True
            else:
                ok = (r[2] == flt)
            return ok and cari in r[1].lower()

        data = [r for r in rows if lolos(r)]

        if self.sort_col >= 0:
            if self.sort_col == 0:
                data.sort(key=lambda r: r[0], reverse=not self.sort_asc)
            elif self.sort_col == COL_CEKLIS:
                data.sort(key=lambda r: self.sudah_dicek(r[5]), reverse=not self.sort_asc)
            else:
                data.sort(key=lambda r: str(r[self.sort_col]).lower(), reverse=not self.sort_asc)
        self.view = data

        g = self.grid
        g.BeginBatch()
        n = g.GetNumberRows()
        if n:
            g.DeleteRows(0, n)
        if data:
            g.AppendRows(len(data))
        for i, r in enumerate(data):
            warna = self.warna_baris(r)
            for c in range(COL_AKSI):
                g.SetCellValue(i, c, str(r[c]))
            g.SetCellValue(i, COL_CEKLIS, TXT_SUDAH if self.sudah_dicek(r[5]) else TXT_BELUM)
            for c in range(JUMLAH_KOL):
                g.SetCellBackgroundColour(i, c, warna)
        g.EndBatch()

        # tanda panah sort di header
        for i, (judul, _) in enumerate(KOLOM):
            panah = ""
            if i == self.sort_col:
                panah = "  ▲" if self.sort_asc else "  ▼"
            g.SetColLabelValue(i, judul + panah)

        self.update_ringkasan()

    def update_ringkasan(self):
        rows = list(self.rows)
        n = {k: 0 for k in WARNA}
        sudah = 0
        for r in rows:
            n[r[2]] += 1
            if self.sudah_dicek(r[5]):
                sudah += 1
        self.lbl.SetLabel(
            f"Total: {len(rows)}   |   TTE Valid: {n[ST_VALID]}   |   "
            f"TTE Sudah Di Modifikasi: {n[ST_MOD]}   |   Tanpa TTD: {n[ST_NONE]}   |   "
            f"Gagal Dibaca: {n[ST_ERR]}   |   Sudah Dicek: {sudah}   |   "
            f"Belum Dicek: {len(rows) - sudah}   |   Tampil: {len(self.view)}")

    def on_label(self, e):
        c = e.GetCol()
        if c < 0 or c == COL_AKSI:
            e.Skip()
            return
        if self.sort_col == c:
            self.sort_asc = not self.sort_asc
        else:
            self.sort_col, self.sort_asc = c, True
        self.refresh()

    # ---------- klik di tabel ----------
    def on_cell_click(self, e):
        row, col = e.GetRow(), e.GetCol()
        if row < 0 or row >= len(self.view):
            e.Skip()
            return

        # kolom Ceklis: klik di mana saja pada sel = ganti status
        if col == COL_CEKLIS:
            self.toggle_ceklis(row)
            return

        if col != COL_AKSI:
            e.Skip()
            return

        pos = self.grid.GetGridWindow().ScreenToClient(wx.GetMousePosition())
        pt = self.grid.CalcUnscrolledPosition(pos)
        rect = self.grid.CellToRect(row, col)
        data = self.view[row]

        for idx, r in enumerate(tombol_rects(rect)):
            if r.Contains(pt):
                if idx == 0:
                    self.buka_folder(data[5])
                elif idx == 1:
                    self.tampil_detail(data)
                elif idx == 2:
                    self.buka_cekdokumen(data[5])
                break

    def on_cell_dclick(self, e):
        row, col = e.GetRow(), e.GetCol()
        if row < 0 or row >= len(self.view):
            e.Skip()
            return
        if col == COL_CEKLIS:           # klik cepat kedua dihitung sebagai klik biasa
            self.toggle_ceklis(row)
            return
        if col == COL_AKSI:
            e.Skip()
            return
        path = self.view[row][5]
        if not os.path.isfile(path):
            wx.MessageBox("File tidak ditemukan.", "Info", wx.ICON_WARNING)
            return
        try:
            if IS_WIN:
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as ex:
            wx.MessageBox(f"Gagal membuka file:\n{ex}", "Error", wx.ICON_ERROR)

    def buka_folder(self, path):
        path = os.path.normpath(path)
        if not os.path.exists(path):
            wx.MessageBox("File tidak ditemukan (mungkin sudah dipindah / dihapus).",
                          "Info", wx.ICON_WARNING)
            return
        try:
            if IS_WIN:
                jalankan_explorer(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as ex:
            wx.MessageBox(f"Gagal membuka folder:\n{ex}", "Error", wx.ICON_ERROR)

    def tampil_detail(self, data):
        with wx.BusyCursor():
            teks = ambil_detail(data[5])
        dlg = DetailDialog(self, f"Detail Tanda Tangan - {data[1]}", teks)
        dlg.ShowModal()
        dlg.Destroy()

    # ---------- cekdokumen.id + folder berdampingan ----------
    def buka_cekdokumen(self, path):
        path = os.path.normpath(path)
        if not os.path.isfile(path):
            wx.MessageBox("File tidak ditemukan (mungkin sudah dipindah / dihapus).",
                          "Info", wx.ICON_WARNING)
            return

        if not IS_WIN:
            webbrowser.open_new(CEK_URL)
            self.buka_folder(path)
            self.lbl.SetLabel("cekdokumen.id dan folder dibuka (penataan jendela otomatis hanya untuk Windows).")
            return

        self.lbl.SetLabel("Membuka cekdokumen.id dan folder file ...")
        threading.Thread(target=self._susun_worker, args=(path, self.GetHandle()),
                         daemon=True).start()

    def _susun_worker(self, path, hwnd_app):
        try:
            set_dpi_thread()
            x0, y0, W, H = area_kerja_monitor(hwnd_app)
            w = int(W * LEBAR_PERSEN)
            h = int(H * TINGGI_PERSEN)
            pos_browser = (x0, y0, w, h)           # paling kiri
            pos_explorer = (x0 + w, y0, w, h)      # tepat di kanan browser

            # catat jendela yang sudah ada, supaya jendela BARU mudah dikenali
            sebelum_b = daftar_jendela(KELAS_BROWSER, 300, 200)
            sebelum_e = daftar_jendela(KELAS_EXPLORER)

            ada_browser = jalankan_browser(*pos_browser)
            jalankan_explorer(path)

            hasil = {}

            def thread_explorer():
                set_dpi_thread()
                hasil["e"] = atur_jendela_explorer(path, sebelum_e, pos_explorer)

            te = threading.Thread(target=thread_explorer, daemon=True)
            te.start()
            hasil["b"] = atur_jendela_browser(sebelum_b, pos_browser) if ada_browser else False
            te.join(timeout=15)

            if not ada_browser:
                pesan = "Chrome/Edge tidak ditemukan, situs dibuka di browser default. Folder sudah dibuka."
            elif hasil.get("b") and hasil.get("e"):
                pesan = "Browser (kiri) dan folder (kanan) sudah berdampingan. Tinggal seret file ke cekdokumen.id."
            else:
                pesan = ("Browser & folder sudah dibuka, tapi penataan posisi belum sempurna "
                         "(atur manual jika perlu).")
            wx.CallAfter(self.lbl.SetLabel, pesan)
        except Exception as e:
            wx.CallAfter(self.lbl.SetLabel, f"Gagal menyusun jendela: {e}")


if __name__ == "__main__":
    app = wx.App(False)
    MainFrame().Show()
    app.MainLoop()