"""
Aplikasi Info IP Saya + Network Tools (wxPython)
------------------------------------------------
KIRI  : IP publik beserta kota, negara, region, ISP, zona waktu,
        mata uang, koordinat, status proxy/hosting, dan bendera negara.
KANAN : Form input IP / alamat web + tombol
            [PING] [TRACERT] [PORT] [IP SCANNER] [SPEEDTEST] [HTTP CHECKER] [QR GENERATOR]
            [                           STOP                                ]
        + grafik batang ping (ms) ala Winbox + statistik (avg/max/min/loss/time).
        PORT         : scan port TCP 1-65535 pada IP / host yang diinput.
        IP SCANNER   : wajib rentang IP, mis. 192.168.1.1-192.168.1.254
        SPEEDTEST    : ping, download, upload dengan gauge berjarum ala speedtest.net
                       (satuan bisa dipilih: Mbps atau MB/s lewat radio button).
                       Server tes: speed.cloudflare.com (tanpa instalasi tambahan).
        QR GENERATOR : isi teks / URL di kolom input, klik tombol -> QR muncul,
                       klik SIMPAN untuk menyimpan sebagai gambar PNG.

V5 (baru):
        TOGGLE PANEL KIRI : tombol tipis di tengah (◀ / ▶) untuk menyembunyikan / menampilkan sisi kiri.
        SERVER SPEEDTEST LAN (kiri bawah) : [START SERVER] [STOP SERVER] [PORT]
                       Klien LAN buka http://IP-KOMPUTER:PORT di browser -> halaman Speedtest LAN.
                       Bandwidth bar UPLOAD SERVER / DOWNLOAD SERVER tampil live ala Winbox.
                       (UPLOAD = klien upload ke server, DOWNLOAD = klien download dari server)

Instalasi : pip install wxPython qrcode
Jalankan  : python ipsaya.pyw

Sumber data: ip-api.com (gratis untuk penggunaan non-komersial, HTTP saja)
Bendera    : flagcdn.com
"""

import http.client
import io
import ipaddress
import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
import webbrowser
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed

import wx
from wx.lib.buttons import GenButton

API_URL = (
    "http://ip-api.com/json/?fields=status,message,continent,continentCode,"
    "country,countryCode,region,regionName,city,lat,lon,timezone,currency,"
    "isp,org,as,mobile,proxy,hosting,query"
)
FLAG_URL = "https://flagcdn.com/w160/{code}.png"
TIMEOUT = 10

IS_WINDOWS = platform.system().lower() == "windows"
TIME_RE = re.compile(r"(?:time|waktu|zeit|temps|tiempo)\s*[=<]\s*([\d.,]+)", re.I)
HOP_RE = re.compile(r"^\s*(\d+)\s")
TIMEOUT_RE = re.compile(
    r"timed out|time out|timeout|habis|unreachable|tidak dapat dijangkau|no answer|general failure",
    re.I,
)

GREEN = wx.Colour(46, 160, 67)
RED = wx.Colour(200, 40, 40)
GREY = wx.Colour(160, 160, 160)
BLUE = wx.Colour(30, 110, 220)
PURPLE = wx.Colour(120, 70, 200)

# ---- pengaturan speedtest ----
SPEED_HOST = "speed.cloudflare.com"
SPEED_DURATION = 8          # detik per tahap (download / upload)
SPEED_THREADS = 4           # koneksi paralel supaya bandwidth terpakai penuh
SPEED_SCALE = {"Mbps": 100.0, "MB/s": 20.0}   # skala maksimal gauge per satuan
SPEED_UA = {"User-Agent": "IPInfoApp/1.0"}

HTTP_TIMEOUT = 10
HTTP_UA = "IPInfoApp/1.0 HTTP Checker"


def make_button(parent, label, color):
    """Tombol berwarna (GenButton supaya warna konsisten di semua OS)."""
    btn = GenButton(parent, label=label, size=(60, 34))
    btn.SetUseFocusIndicator(False)
    btn.SetForegroundColour(wx.WHITE)
    btn.SetBackgroundColour(color)
    f = btn.GetFont()
    f.SetWeight(wx.FONTWEIGHT_BOLD)
    btn.SetFont(f)
    return btn


def fmt_elapsed(secs):
    """Format lama waktu. Di bawah 1 jam: '5 menit : 9 detik'.
    Mulai 60 menit: '1 jam 0 menit 9 detik'."""
    secs = int(secs)
    if secs >= 3600:
        return f"{secs // 3600} jam {(secs % 3600) // 60} menit {secs % 60} detik"
    return f"{secs // 60} menit : {secs % 60} detik"


def build_command(mode, target):
    """Bikin perintah ping/tracert sesuai OS. Return None kalau tool tidak ada."""
    if mode == "ping":
        if IS_WINDOWS:
            return ["ping", "-t", target]
        if platform.system() == "Linux":
            return ["ping", "-O", target]  # -O: laporkan paket yang belum dibalas
        return ["ping", target]
    if IS_WINDOWS:
        return ["tracert", target]
    if shutil.which("traceroute"):
        return ["traceroute", target]
    if shutil.which("tracepath"):
        return ["tracepath", target]
    return None


SCAN_WORKERS_PORT = 400   # jumlah thread scan port
SCAN_WORKERS_IP = 128     # jumlah thread scan IP
PORT_TIMEOUT = 0.5        # detik per port
MAX_IP_RANGE = 65536      # batas jumlah IP sekali scan


def parse_ip_range(text):
    """Format wajib: 192.168.1.1-192.168.1.254 (boleh singkat: 192.168.1.1-254)."""
    text = text.replace(" ", "")
    if "-" not in text:
        raise ValueError("Format harus rentang IP, mis. 192.168.1.1-192.168.1.254")
    left, right = text.split("-", 1)
    try:
        start = ipaddress.IPv4Address(left)
        if right.isdigit():
            end = ipaddress.IPv4Address(".".join(left.split(".")[:3] + [right]))
        else:
            end = ipaddress.IPv4Address(right)
    except ValueError:
        raise ValueError("IP address tidak valid.")
    if int(end) < int(start):
        raise ValueError("IP akhir harus lebih besar dari IP awal.")
    if int(end) - int(start) + 1 > MAX_IP_RANGE:
        raise ValueError(f"Rentang terlalu besar (maksimal {MAX_IP_RANGE} IP).")
    return int(start), int(end)


def check_port(ip, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(PORT_TIMEOUT)
    try:
        return sock.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def service_name(port):
    try:
        return socket.getservbyport(port, "tcp")
    except OSError:
        return "-"


def ping_once(ip):
    """Ping 1x. Return (hidup, teks_ms)."""
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", "800", ip]
    elif platform.system() == "Darwin":
        cmd = ["ping", "-c", "1", "-W", "1000", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "1", ip]
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=4, **kwargs)
    except Exception:
        return False, ""
    out = r.stdout.decode(errors="replace")
    if "ttl=" not in out.lower():       # TTL= hanya ada kalau benar-benar dibalas
        return False, ""
    m = TIME_RE.search(out)
    return True, (m.group(1) + " ms" if m else "")


def resolve_name(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except Exception:
        return ""


def fmt_ms(value):
    return f"{round(value, 1):g} ms"


def yes_no(value):
    return "Ya" if value else "Tidak"


def get_os_name():
    system = platform.system()
    if system == "Windows":
        return f"Windows {platform.release()}"
    if system == "Darwin":
        return f"macOS {platform.mac_ver()[0]}"
    return f"{system} {platform.release()}"


def get_private_ip():
    """Mengambil IP lokal (private) komputer di jaringan, mis. 192.168.x.x."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Tidak ada data yang dikirim; hanya untuk menentukan adapter yang dipakai.
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "Tidak terdeteksi"
    finally:
        sock.close()


def get_default_browser():
    """Mencoba mengetahui browser default. Hasilnya bisa 'Tidak diketahui'."""
    try:
        browser = webbrowser.get()
        name = getattr(browser, "name", "") or ""
        if name:
            return name.replace("-", " ").title()
    except Exception:
        pass
    return "Tidak diketahui"


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "IPInfoApp/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "IPInfoApp/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def normalize_http_url(value):
    value = value.strip()
    if not value:
        raise ValueError("Alamat web tidak boleh kosong.")
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("URL tidak valid. Contoh: https://example.com")
    return value


def build_qr_matrix(text):
    """Bikin matriks QR (list of list bool) memakai library 'qrcode'.
    Raise ImportError kalau library belum terpasang."""
    import qrcode
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=0,
    )
    qr.add_data(text)
    qr.make(fit=True)
    return qr.get_matrix()


class QRPanel(wx.Panel):
    """Menampilkan gambar QR code (digambar sendiri dari matriks, tanpa PIL)."""

    QUIET = 4   # zona tenang (jumlah modul putih di sekeliling QR)

    def __init__(self, parent, height=260):
        super().__init__(parent, size=(-1, height))
        self.SetMinSize((100, height))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.matrix = None
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def set_matrix(self, matrix):
        self.matrix = matrix
        self.Refresh()

    def _draw_modules(self, dc, ox, oy, mod):
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(wx.BLACK))
        q = self.QUIET
        for r, row in enumerate(self.matrix):
            for c, val in enumerate(row):
                if val:
                    dc.DrawRectangle(ox + (c + q) * mod, oy + (r + q) * mod, mod, mod)

    def on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        w, h = self.GetClientSize()
        if not self.matrix:
            return
        n = len(self.matrix) + 2 * self.QUIET
        mod = max(1, min(w, h) // n)
        side = mod * n
        ox, oy = (w - side) // 2, (h - side) // 2
        dc.SetPen(wx.Pen(wx.Colour(210, 214, 222)))
        dc.SetBrush(wx.Brush(wx.WHITE))
        dc.DrawRectangle(ox, oy, side, side)
        self._draw_modules(dc, ox, oy, mod)

    def to_bitmap(self, module_px=12):
        """Render QR ke wx.Bitmap beresolusi tinggi (untuk disimpan)."""
        n = len(self.matrix) + 2 * self.QUIET
        side = n * module_px
        bmp = wx.Bitmap(side, side)
        mdc = wx.MemoryDC(bmp)
        mdc.SetBackground(wx.Brush(wx.WHITE))
        mdc.Clear()
        self._draw_modules(mdc, 0, 0, module_px)
        mdc.SelectObject(wx.NullBitmap)
        return bmp


class PingGraph(wx.Panel):
    """Grafik batang ping ala Winbox. Batang baru muncul di kanan.
    Batang makin pendek = ping makin bagus. Timeout = batang merah penuh.
    Garis vertikal tipis muncul tiap GRID_SECONDS detik (30 atau 60)."""

    BAR_W = 4
    GAP = 1
    STEPS = (100, 200, 500, 1000, 2000, 5000)  # skala minimal 100 ms
    GRID_SECONDS = 30                           # ganti 60 kalau mau tiap 1 menit

    def __init__(self, parent, height=72):
        super().__init__(parent, size=(-1, height))
        self.SetMinSize((100, height))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.samples = deque(maxlen=1200)   # isi: (waktu, ms atau None)
        self.t0 = time.monotonic()
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, self.on_size)

    def on_size(self, evt):
        self.Refresh()
        evt.Skip()

    def add(self, value):
        """value = ms (float) atau None untuk timeout."""
        self.samples.append((time.monotonic(), value))
        self.Refresh()

    def clear(self):
        self.samples.clear()
        self.t0 = time.monotonic()
        self.Refresh()

    def _scale(self, visible):
        peak = max((v for _, v in visible if v is not None), default=0)
        for step in self.STEPS:
            if peak <= step:
                return step
        return (int(peak) // 1000 + 1) * 1000

    @staticmethod
    def _color(ms):
        if ms <= 50:
            return wx.Colour(46, 160, 67)     # bagus
        if ms <= 150:
            return wx.Colour(240, 160, 30)    # sedang
        return wx.Colour(200, 40, 40)         # jelek

    def on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(wx.WHITE))
        dc.Clear()

        # bingkai
        dc.SetPen(wx.Pen(wx.Colour(200, 205, 215)))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRectangle(0, 0, w, h)

        # garis bantu horizontal 25% / 50% / 75%
        dc.SetPen(wx.Pen(wx.Colour(228, 232, 238)))
        for frac in (0.25, 0.5, 0.75):
            y = int(h - 1 - frac * (h - 2))
            dc.DrawLine(1, y, w - 1, y)

        step = self.BAR_W + self.GAP
        n = max(1, (w - 2) // step)
        vis = list(self.samples)[-n:]          # lama -> baru
        count = len(vis)
        scale = self._scale(vis)

        def bar_x(i):                          # posisi batang ke-i (rata kanan)
            return w - 1 - self.BAR_W - (count - 1 - i) * step

        small = wx.Font(7, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)

        # garis vertikal tipis tiap GRID_SECONDS detik + label waktu
        interval = self.GRID_SECONDS
        if interval and count > 1:
            dc.SetFont(small)
            prev = int((vis[0][0] - self.t0) // interval)
            for i in range(1, count):
                bucket = int((vis[i][0] - self.t0) // interval)
                if bucket != prev:
                    gx = bar_x(i) - 1
                    dc.SetPen(wx.Pen(wx.Colour(185, 194, 210)))
                    dc.DrawLine(gx, 1, gx, h - 1)
                    secs = bucket * interval
                    label = f"{secs // 60}:{secs % 60:02d}"
                    tw = dc.GetTextExtent(label)[0]
                    if gx > 50 and gx + 3 + tw < w:
                        dc.SetTextForeground(wx.Colour(150, 158, 172))
                        dc.DrawText(label, gx + 3, h - 12)
                prev = bucket

        # batang
        dc.SetPen(wx.TRANSPARENT_PEN)
        for i, (_, v) in enumerate(vis):
            x = bar_x(i)
            if v is None:                       # packet loss -> merah penuh
                bh = h - 2
                color = wx.Colour(200, 40, 40)
            else:
                bh = max(1, int(min(v, scale) / scale * (h - 2)))
                color = self._color(v)
            dc.SetBrush(wx.Brush(color))
            dc.DrawRectangle(x, h - 1 - bh, self.BAR_W, bh)

        # label skala
        dc.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        dc.SetTextForeground(wx.Colour(130, 138, 150))
        dc.DrawText(f"{scale} ms", 4, 2)


class SpeedGauge(wx.Panel):
    """Gauge speedtest berjarum ala speedtest.net (busur 270 derajat, skala linear).

    Nilai internal selalu dalam Mbps. Satuan tampilan (Mbps / MB/s) diatur lewat
    atribut `unit`; skala maksimalnya diambil dari SPEED_SCALE.
    """

    A0 = 0.75 * math.pi      # sudut awal (kiri bawah)
    SWEEP = 1.5 * math.pi    # lebar busur 270 derajat
    BG = wx.Colour(20, 24, 45)
    TRACK = wx.Colour(48, 55, 88)
    TEXT = wx.Colour(235, 238, 248)
    DIM = wx.Colour(140, 150, 180)
    C_START = (0, 190, 255)
    C_END = (150, 80, 235)

    def __init__(self, parent, height=240):
        super().__init__(parent, size=(-1, height))
        self.SetMinSize((100, height))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.unit = "Mbps"
        self.target = 0.0        # nilai tujuan (Mbps)
        self.shown = 0.0         # nilai yang sedang digambar (Mbps), meluncur ke target
        self.phase = "SIAP"
        self.center = None       # (teks_besar, teks_kecil) untuk menimpa angka tengah
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_tick, self.timer)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))
        self.timer.Start(30)

    # --- konversi satuan ---
    def to_unit(self, mbps):
        return mbps / 8.0 if self.unit == "MB/s" else mbps

    def max_value(self):
        return SPEED_SCALE[self.unit]

    def reset(self):
        self.target = 0.0
        self.shown = 0.0
        self.center = None
        self.Refresh()

    def on_tick(self, _evt):
        diff = self.target - self.shown
        if abs(diff) < 0.02:
            if self.shown != self.target:
                self.shown = self.target
                self.Refresh()
            return
        self.shown += diff * 0.18          # efek smoothing jarum
        self.Refresh()

    @classmethod
    def _lerp(cls, f):
        a, b = cls.C_START, cls.C_END
        return wx.Colour(*(int(a[i] + (b[i] - a[i]) * f) for i in range(3)))

    def on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        w, h = self.GetClientSize()

        # latar gelap membulat
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(self.BG))
        gc.DrawRoundedRectangle(0, 0, w, h, 10)

        r = min((w - 60) / 2.0, (h - 52) / 1.707)
        r = max(r, 40)
        cx = w / 2.0
        cy = 36 + r
        vmax = self.max_value()
        value = self.to_unit(self.shown)
        frac = max(0.0, min(value / vmax, 1.0))

        # label fase di atas
        gc.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD), self.DIM)
        tw, th = gc.GetTextExtent(self.phase)[:2]
        gc.DrawText(self.phase, cx - tw / 2, 8)

        # busur latar (track)
        pen = wx.Pen(self.TRACK, 10)
        pen.SetCap(wx.CAP_BUTT)
        gc.SetPen(pen)
        path = gc.CreatePath()
        path.AddArc(cx, cy, r, self.A0, self.A0 + self.SWEEP, True)
        gc.StrokePath(path)

        # busur berwarna (gradasi biru -> ungu) sampai posisi jarum
        segs = 60
        for i in range(segs):
            s = i / segs
            if s >= frac:
                break
            e = min((i + 1) / segs, frac)
            a_s = self.A0 + s * self.SWEEP
            a_e = self.A0 + e * self.SWEEP + (0.012 if e < frac else 0)
            pen = wx.Pen(self._lerp(s), 10)
            pen.SetCap(wx.CAP_BUTT)
            gc.SetPen(pen)
            seg = gc.CreatePath()
            seg.AddArc(cx, cy, r, a_s, a_e, True)
            gc.StrokePath(seg)

        # garis kecil + angka skala 0..max (11 label)
        gc.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL), self.DIM)
        tick_pen = wx.Pen(self.DIM, 1)
        for i in range(11):
            a = self.A0 + (i / 10.0) * self.SWEEP
            ca, sa = math.cos(a), math.sin(a)
            gc.SetPen(tick_pen)
            tick = gc.CreatePath()
            tick.MoveToPoint(cx + (r - 10) * ca, cy + (r - 10) * sa)
            tick.AddLineToPoint(cx + (r - 16) * ca, cy + (r - 16) * sa)
            gc.StrokePath(tick)
            label = f"{vmax * i / 10:g}"
            lw, lh = gc.GetTextExtent(label)[:2]
            gc.DrawText(label, cx + (r - 30) * ca - lw / 2, cy + (r - 30) * sa - lh / 2)

        # jarum
        a = self.A0 + frac * self.SWEEP
        ca, sa = math.cos(a), math.sin(a)
        px, py = -sa, ca                     # vektor tegak lurus
        tip = (cx + (r - 46) * ca, cy + (r - 46) * sa)
        b1 = (cx + 5 * px, cy + 5 * py)
        b2 = (cx - 5 * px, cy - 5 * py)
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(wx.Colour(255, 255, 255)))
        needle = gc.CreatePath()
        needle.MoveToPoint(*tip)
        needle.AddLineToPoint(*b1)
        needle.AddLineToPoint(*b2)
        needle.CloseSubpath()
        gc.FillPath(needle)
        gc.SetBrush(wx.Brush(self._lerp(frac)))
        gc.DrawEllipse(cx - 9, cy - 9, 18, 18)

        # angka besar + satuan di bawah poros
        if self.center is not None:
            big, small = self.center
        elif self.phase in ("DOWNLOAD", "UPLOAD"):
            big, small = f"{value:.1f}", self.unit
        else:
            big, small = f"{value:.1f}", self.unit
        gc.SetFont(wx.Font(24, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD), self.TEXT)
        bw, bh = gc.GetTextExtent(big)[:2]
        by = cy + 0.22 * r
        gc.DrawText(big, cx - bw / 2, by)
        gc.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL), self.DIM)
        sw = gc.GetTextExtent(small)[0]
        gc.DrawText(small, cx - sw / 2, by + bh + 1)


class PingStats:
    """Menghitung ping terakhir, avg, max, min, loss, dan lama waktu."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.sent = 0
        self.lost = 0
        self.total = 0.0
        self.min = None
        self.max = None
        self.last = None
        self.last_lost = False
        self.t0 = None
        self.t_end = None

    def start(self):
        self.reset()
        self.t0 = time.monotonic()

    def stop(self):
        if self.t0 is not None and self.t_end is None:
            self.t_end = time.monotonic()

    def add(self, ms):
        self.sent += 1
        if ms is None:
            self.lost += 1
            self.last_lost = True
            return
        self.last_lost = False
        self.last = ms
        self.total += ms
        self.min = ms if self.min is None else min(self.min, ms)
        self.max = ms if self.max is None else max(self.max, ms)

    def avg(self):
        got = self.sent - self.lost
        return self.total / got if got else None

    def loss_pct(self):
        return (self.lost / self.sent * 100) if self.sent else None

    def elapsed_text(self):
        if self.t0 is None:
            return "--"
        return fmt_elapsed((self.t_end or time.monotonic()) - self.t0)


class ScanStats:
    """Progres scan port / IP."""

    def __init__(self, total):
        self.total = total
        self.done = 0
        self.found = 0
        self.t0 = time.monotonic()
        self.t_end = None

    def percent(self):
        return self.done / self.total * 100 if self.total else 0

    def elapsed_text(self):
        return fmt_elapsed((self.t_end or time.monotonic()) - self.t0)


class StatsBar(wx.Panel):
    """Teks statistik tebal (1-2 baris), rata tengah. Mendukung warna per bagian.
    Ukuran huruf mengecil otomatis kalau teks tidak muat dalam satu baris."""

    DARK = wx.Colour(30, 35, 45)
    SEP = wx.Colour(150, 158, 172)
    SIZES = (10, 9, 8, 7)

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.lines = []
        self.SetMinSize((200, 48))
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def set_lines(self, lines):
        self.lines = lines
        self.Refresh()

    def on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        w, h = self.GetClientSize()

        # pilih ukuran huruf terbesar yang masih muat
        for size in self.SIZES:
            dc.SetFont(wx.Font(size, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
            widest = max((sum(dc.GetTextExtent(t)[0] for t, _ in line) for line in self.lines), default=0)
            if widest <= w - 4:
                break

        lh = dc.GetTextExtent("Ag")[1]
        gap = 4
        total_h = len(self.lines) * lh + max(0, len(self.lines) - 1) * gap
        y = max(0, (h - total_h) // 2)
        for line in self.lines:
            line_w = sum(dc.GetTextExtent(t)[0] for t, _ in line)
            x = max(0, (w - line_w) // 2)
            for text, colour in line:
                dc.SetTextForeground(colour)
                dc.DrawText(text, x, y)
                x += dc.GetTextExtent(text)[0]
            y += lh + gap


# =====================================================================
#  SERVER SPEEDTEST LAN (v5)
#  Server HTTP kecil (stdlib saja). Client membuka http://IP:PORT di browser
#  -> tampil halaman Speedtest LAN. Endpoint: / , /ping , /download , /upload
# =====================================================================
SERVER_DEFAULT_PORT = 6000
SERVER_BLOCK = os.urandom(1 << 18)      # 256 KB data acak untuk tes download

SPEEDTEST_HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Speedtest LAN</title>
<style>
:root{--ink:#eef1fb;--dim:#8f9bc4;--card:#171f45;--line:#2a3566}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0}
body{min-height:100vh;background:radial-gradient(ellipse at 50% -10%,#24338a 0,#0f1530 62%) fixed;color:var(--ink);font-family:"Segoe UI",system-ui,Roboto,Arial,sans-serif;display:flex;justify-content:center}
.wrap{width:100%;max-width:560px;padding:20px 16px 32px;text-align:center}
h1{margin:6px 0 0;font-size:26px;letter-spacing:3px;font-weight:800}
h1 span{background:linear-gradient(90deg,#00bfff,#9650eb);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{color:var(--dim);font-size:13px;margin:4px 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:10px 8px 6px;box-shadow:0 12px 40px rgba(0,0,0,.35)}
.phase{font-size:13px;letter-spacing:3px;color:var(--dim);font-weight:700;padding-top:4px;min-height:22px}
svg{width:100%;height:auto;max-width:420px;display:block;margin:0 auto}
.res{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:14px 0}
.res div{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 4px}
.res small{display:block;color:var(--dim);font-size:11px;letter-spacing:1px}
.res b{display:block;font-size:20px;margin-top:4px}
.res i{font-style:normal;color:var(--dim);font-size:11px}
#go{width:100%;border:0;border-radius:999px;padding:15px;font-size:17px;font-weight:800;letter-spacing:2px;color:#fff;background:linear-gradient(90deg,#00bfff,#9650eb);cursor:pointer}
#go:disabled{opacity:.55;cursor:default}
.units{margin-top:14px;color:var(--dim);font-size:13px}
.units button{background:none;border:1px solid var(--line);color:var(--dim);border-radius:8px;padding:5px 12px;margin-left:6px;cursor:pointer}
.units button.on{background:#2a3566;color:#fff}
.msg{margin-top:12px;color:#ff8a8a;font-size:13px;min-height:18px}
</style>
</head>
<body>
<div class="wrap">
  <h1>SPEEDTEST <span>LAN</span></h1>
  <div class="sub">Server: <b id="host"></b></div>

  <div class="card">
    <div class="phase" id="phase">SIAP</div>
    <svg viewBox="0 0 300 250">
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="#00bfff"/><stop offset="1" stop-color="#9650eb"/>
        </linearGradient>
      </defs>
      <path id="track" fill="none" stroke="#303758" stroke-width="12"/>
      <path id="prog" fill="none" stroke="url(#g)" stroke-width="12"/>
      <g id="ticks" fill="#8f9bc4" font-size="10" text-anchor="middle"></g>
      <g id="needle"><polygon points="230,150 150,145 150,155" fill="#ffffff"/></g>
      <circle cx="150" cy="150" r="9" fill="#9650eb"/>
      <text id="big" x="150" y="200" text-anchor="middle" font-size="38" font-weight="800" fill="#eef1fb">0.0</text>
      <text id="unit" x="150" y="222" text-anchor="middle" font-size="13" fill="#8f9bc4">Mbps</text>
    </svg>
  </div>

  <div class="res">
    <div><small>PING</small><b id="r-ping">--</b><i>ms</i></div>
    <div><small>JITTER</small><b id="r-jit">--</b><i>ms</i></div>
    <div><small>DOWNLOAD</small><b id="r-down">--</b><i id="u-down">Mbps</i></div>
    <div><small>UPLOAD</small><b id="r-up">--</b><i id="u-up">Mbps</i></div>
  </div>

  <button id="go">MULAI TES</button>
  <div class="units">Satuan:
    <button data-u="Mbps" class="on">Mbps</button><button data-u="MB/s">MB/s</button>
  </div>
  <div class="msg" id="msg"></div>
</div>

<script>
(function () {
  'use strict';
  var TIERS = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000];
  var CX = 150, CY = 150, R = 118, A0 = 135, SW = 270;
  var STREAMS = 6, DUR = 10000, WARM = 1000;
  var NS = 'http://www.w3.org/2000/svg';
  var unit = 'Mbps', maxv = 100, target = 0, shown = 0, running = false;
  var bigText = null, blobCache = null;
  var res = { ping: null, jitter: null, down: null, up: null };

  function $(i) { return document.getElementById(i); }
  function rad(d) { return d * Math.PI / 180; }
  function pt(a, r) { return [CX + r * Math.cos(rad(a)), CY + r * Math.sin(rad(a))]; }
  function conv(m) { return unit === 'MB/s' ? m / 8 : m; }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  // ---- gauge ----
  var p0 = pt(A0, R), p1 = pt(A0 + SW, R);
  var arc = 'M ' + p0[0] + ' ' + p0[1] + ' A ' + R + ' ' + R + ' 0 1 1 ' + p1[0] + ' ' + p1[1];
  $('track').setAttribute('d', arc);
  $('prog').setAttribute('d', arc);
  var L = R * rad(SW);

  function buildTicks() {
    var g = $('ticks');
    while (g.firstChild) g.removeChild(g.firstChild);
    for (var i = 0; i <= 10; i++) {
      var a = A0 + SW * i / 10, v = maxv * i / 10;
      var a1 = pt(a, R - 10), a2 = pt(a, R - 16), tp = pt(a, R - 29);
      var ln = document.createElementNS(NS, 'line');
      ln.setAttribute('x1', a1[0]); ln.setAttribute('y1', a1[1]);
      ln.setAttribute('x2', a2[0]); ln.setAttribute('y2', a2[1]);
      ln.setAttribute('stroke', '#8f9bc4');
      g.appendChild(ln);
      var t = document.createElementNS(NS, 'text');
      t.setAttribute('x', tp[0]); t.setAttribute('y', tp[1] + 3.5);
      t.textContent = String(parseFloat(v.toFixed(1)));
      g.appendChild(t);
    }
  }

  function resetScale() {
    maxv = unit === 'MB/s' ? 20 : 100;
    $('unit').textContent = unit;
    buildTicks();
  }

  function frame() {
    shown += (target - shown) * 0.15;
    if (Math.abs(target - shown) < 0.005) shown = target;
    var v = conv(shown);
    while (v > maxv * 0.98 && TIERS.indexOf(maxv) < TIERS.length - 1) {
      maxv = TIERS[TIERS.indexOf(maxv) + 1];
      buildTicks();
    }
    var f = Math.max(0, Math.min(v / maxv, 1));
    $('prog').setAttribute('stroke-dasharray', (f * L) + ' ' + (L + 20));
    $('needle').setAttribute('transform', 'rotate(' + (A0 + SW * f) + ' ' + CX + ' ' + CY + ')');
    $('big').textContent = bigText === null ? v.toFixed(v < 10 ? 2 : 1) : bigText;
    requestAnimationFrame(frame);
  }

  function setPhase(name) {
    $('phase').textContent = name;
    target = 0;
  }

  // ---- hasil ----
  function fmtV(m) {
    if (m === null) return '--';
    var v = conv(m);
    return v.toFixed(v < 10 ? 2 : 1);
  }
  function render() {
    $('r-ping').textContent = res.ping === null ? '--' : res.ping.toFixed(1);
    $('r-jit').textContent = res.jitter === null ? '--' : res.jitter.toFixed(1);
    $('r-down').textContent = fmtV(res.down);
    $('r-up').textContent = fmtV(res.up);
    $('u-down').textContent = unit;
    $('u-up').textContent = unit;
  }

  // ---- ping ----
  async function measurePing() {
    var t = [];
    for (var i = 0; i < 12; i++) {
      var s = performance.now();
      var r = await fetch('/ping?r=' + Math.random(), { cache: 'no-store' });
      await r.text();
      t.push(performance.now() - s);
      await sleep(60);
    }
    t.shift();                                   // buang sampel pertama (handshake)
    var avg = t.reduce(function (a, b) { return a + b; }, 0) / t.length, j = 0;
    for (var k = 1; k < t.length; k++) j += Math.abs(t[k] - t[k - 1]);
    return [avg, t.length > 1 ? j / (t.length - 1) : 0];
  }

  // ---- download / upload ----
  async function downWorker(st, ctrl) {
    var errs = 0;
    while (!st.stop) {
      try {
        var r = await fetch('/download?size=200000000&r=' + Math.random(), { cache: 'no-store', signal: ctrl.signal });
        var rd = r.body.getReader();
        while (!st.stop) {
          var x = await rd.read();
          if (x.done) break;
          st.bytes += x.value.length;
        }
        try { await rd.cancel(); } catch (e) {}
      } catch (e) {
        if (st.stop || ++errs > 5) return;
        await sleep(200);
      }
    }
  }

  function getBlob() {
    if (!blobCache) {
      var buf = new Uint8Array(8 * 1024 * 1024);
      for (var i = 0; i < buf.length; i += 65536) crypto.getRandomValues(buf.subarray(i, i + 65536));
      blobCache = new Blob([buf]);
    }
    return blobCache;
  }

  function upWorker(st, blob, xhrs) {
    return new Promise(function (resolve) {
      var errs = 0;
      function next() {
        if (st.stop) return resolve();
        var x = new XMLHttpRequest(), last = 0;
        xhrs.add(x);
        x.open('POST', '/upload?r=' + Math.random());
        x.upload.onprogress = function (e) { st.bytes += e.loaded - last; last = e.loaded; };
        x.onload = function () { xhrs.delete(x); next(); };
        x.onerror = function () {
          xhrs.delete(x);
          if (st.stop || ++errs > 5) return resolve();
          setTimeout(next, 200);
        };
        x.onabort = function () { xhrs.delete(x); resolve(); };
        x.send(blob);
      }
      next();
    });
  }

  async function phaseRun(kind) {
    var st = { bytes: 0, stop: false };
    var ctrl = new AbortController();
    var xhrs = new Set();
    var workers = [];
    var blob = kind === 'up' ? getBlob() : null;
    for (var i = 0; i < STREAMS; i++) {
      workers.push(kind === 'down' ? downWorker(st, ctrl) : upWorker(st, blob, xhrs));
    }
    var t0 = performance.now(), hist = [[t0, 0]], warm = null, end = null;
    await new Promise(function (resolve) {
      var iv = setInterval(function () {
        var now = performance.now();
        hist.push([now, st.bytes]);
        while (hist.length > 2 && now - hist[0][0] > 1000) hist.shift();
        var o = hist[0];
        if (now > o[0]) target = (st.bytes - o[1]) * 8 / 1e6 / ((now - o[0]) / 1000);
        if (!warm && now - t0 >= WARM) warm = [now, st.bytes];
        if (now - t0 >= DUR) {
          clearInterval(iv);
          end = [now, st.bytes];
          resolve();
        }
      }, 100);
    });
    st.stop = true;
    ctrl.abort();
    xhrs.forEach(function (x) { try { x.abort(); } catch (e) {} });
    await Promise.all(workers);
    if (end[1] === 0) throw new Error('tidak ada data yang diterima');
    var w = warm || [t0, 0];
    var span = (end[0] - w[0]) / 1000;
    return span > 0 ? (end[1] - w[1]) * 8 / 1e6 / span : 0;
  }

  // ---- alur utama ----
  async function start() {
    if (running) return;
    running = true;
    var btn = $('go');
    btn.disabled = true;
    btn.textContent = 'MENGUKUR...';
    $('msg').textContent = '';
    res = { ping: null, jitter: null, down: null, up: null };
    render();
    try {
      setPhase('PING'); bigText = '...'; resetScale();
      var p = await measurePing();
      res.ping = p[0]; res.jitter = p[1]; render();

      bigText = null; setPhase('DOWNLOAD'); resetScale();
      res.down = await phaseRun('down'); render();

      target = 0; await sleep(800);
      setPhase('UPLOAD'); resetScale();
      res.up = await phaseRun('up'); render();

      setPhase('SELESAI');
      target = res.down;
    } catch (e) {
      bigText = null;
      setPhase('GAGAL');
      $('msg').textContent = 'Tes gagal: ' + (e && e.message ? e.message : e);
    }
    running = false;
    btn.disabled = false;
    btn.textContent = 'ULANGI TES';
  }

  $('host').textContent = location.host;
  $('go').addEventListener('click', start);
  var ub = document.querySelectorAll('.units button');
  for (var i = 0; i < ub.length; i++) {
    ub[i].addEventListener('click', function () {
      unit = this.getAttribute('data-u');
      for (var j = 0; j < ub.length; j++) ub[j].className = ub[j] === this ? 'on' : '';
      if (!running) shown = target;
      resetScale();
      render();
    });
  }
  resetScale();
  render();
  requestAnimationFrame(frame);
})();
</script>
</body>
</html>
"""


class _SpeedHttpd(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = not IS_WINDOWS     # di Windows: biar bentrok port terdeteksi
    request_queue_size = 128


def make_speed_handler(srv):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "IPSayaSpeedLAN/5.0"
        timeout = 30

        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            try:
                u = urllib.parse.urlparse(self.path)
                if u.path in ("/", "/index.html"):
                    srv.log(f"[SERVER] {self.client_address[0]} membuka halaman Speedtest LAN")
                    self._send(200, SPEEDTEST_HTML.encode("utf-8"), "text/html; charset=utf-8")
                elif u.path == "/ping":
                    self._send(200, b"pong", "text/plain")
                elif u.path == "/download":
                    self._download(urllib.parse.parse_qs(u.query))
                else:
                    self._send(404, b"Not found", "text/plain")
            except OSError:
                self.close_connection = True

        def _download(self, q):
            try:
                size = int(q.get("size", ["25000000"])[0])
            except ValueError:
                size = 25000000
            size = max(0, min(size, 2000000000))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            view = memoryview(SERVER_BLOCK)
            sent = 0
            while sent < size and not srv.stopping.is_set():
                n = min(len(SERVER_BLOCK), size - sent)
                self.wfile.write(view[:n])
                sent += n
                srv.add_down(n)
            if sent < size:
                self.close_connection = True

        def do_POST(self):
            try:
                u = urllib.parse.urlparse(self.path)
                if u.path != "/upload":
                    self.close_connection = True
                    self._send(404, b"Not found", "text/plain")
                    return
                remaining = int(self.headers.get("Content-Length", "0") or 0)
                while remaining > 0 and not srv.stopping.is_set():
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    srv.add_up(len(chunk))
                if remaining > 0:
                    self.close_connection = True
                    return
                self._send(200, b"ok", "text/plain")
            except (OSError, ValueError):
                self.close_connection = True

    return Handler


class LanSpeedServer:
    """Server speedtest LAN. Menghitung byte masuk (upload klien) dan keluar (download klien)."""

    def __init__(self, port, on_log=None):
        self.port = port
        self.on_log = on_log
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._up = 0       # byte yang DITERIMA server (klien upload)
        self._down = 0     # byte yang DIKIRIM server (klien download)
        self._httpd = None
        self._thread = None

    def log(self, msg):
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass

    def add_up(self, n):
        with self._lock:
            self._up += n

    def add_down(self, n):
        with self._lock:
            self._down += n

    def counters(self):
        with self._lock:
            return self._up, self._down

    def start(self):
        """Raise OSError kalau port sudah dipakai."""
        self._httpd = _SpeedHttpd(("0.0.0.0", self.port), make_speed_handler(self))
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True)
        self._thread.start()

    def stop(self):
        self.stopping.set()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)


class BandwidthGraph(wx.Panel):
    """Bandwidth bar ala Winbox jadul (batang bergeser ke kiri, batang baru di kanan).
    Nilai dalam Mbps. Skala otomatis mengikuti puncak yang terlihat."""

    BAR_W = 3
    GAP = 1
    STEPS = (1, 5, 10, 50, 100, 500, 1000, 5000, 10000)

    def __init__(self, parent, title, color, height=84):
        super().__init__(parent, size=(-1, height))
        self.SetMinSize((100, height))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.title = title
        self.color = color
        self.samples = deque(maxlen=1200)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, lambda e: (self.Refresh(), e.Skip()))

    def add(self, mbps):
        self.samples.append(mbps)
        self.Refresh()

    def clear(self):
        self.samples.clear()
        self.Refresh()

    def _scale(self, visible):
        peak = max(visible, default=0)
        for step in self.STEPS:
            if peak <= step:
                return step
        return (int(peak) // 10000 + 1) * 10000

    def on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(wx.WHITE))
        dc.Clear()

        dc.SetPen(wx.Pen(wx.Colour(200, 205, 215)))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRectangle(0, 0, w, h)

        dc.SetPen(wx.Pen(wx.Colour(228, 232, 238)))
        for frac in (0.25, 0.5, 0.75):
            y = int(h - 1 - frac * (h - 2))
            dc.DrawLine(1, y, w - 1, y)

        step = self.BAR_W + self.GAP
        n = max(1, (w - 2) // step)
        vis = list(self.samples)[-n:]
        count = len(vis)
        scale = self._scale(vis)

        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(self.color))
        for i, v in enumerate(vis):
            bh = max(1, int(min(v, scale) / scale * (h - 2))) if v > 0 else 1
            x = w - 1 - self.BAR_W - (count - 1 - i) * step
            dc.DrawRectangle(x, h - 1 - bh, self.BAR_W, bh)

        # judul + nilai terakhir (kiri atas), skala (kanan atas)
        last = vis[-1] if vis else 0.0
        dc.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        dc.SetTextForeground(wx.Colour(60, 68, 85))
        dc.DrawText(self.title, 4, 2)
        dc.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        dc.SetTextForeground(self.color)
        dc.DrawText(f"{last:.1f} Mbps", 4, 16)
        dc.SetFont(wx.Font(7, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        dc.SetTextForeground(wx.Colour(130, 138, 150))
        label = f"{scale} Mbps"
        dc.DrawText(label, w - dc.GetTextExtent(label)[0] - 4, 2)


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Info IP Saya + Network Tools v5", size=(1300, 820))
        self.SetMinSize((1200, 760))
        self.ip_text = ""
        self.lat = None
        self.lon = None
        self.rows = {}
        self.proc = None
        self.running = False
        self.stats = PingStats()
        self.mode = None
        self.scan = None
        self.qr_text = ""
        self.left_visible = True
        self.srv = None
        self.srv_url = ""
        self.srv_last = (0, 0, 0.0)
        self.sp = {"ping": None, "download": None, "upload": None}   # hasil speedtest (Mbps / ms)
        self.sp_phase = ""
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self.srv_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_srv_timer, self.srv_timer)
        self._build_ui()
        self.Centre()
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        panel = wx.Panel(self)
        self.panel = panel
        panel.SetBackgroundColour(wx.Colour(245, 247, 250))
        root = wx.BoxSizer(wx.HORIZONTAL)
        self.root = root

        left = self._build_left(panel)
        self.left_sizer = left
        right = self._build_right(panel)

        # tombol toggle tipis (pengganti garis pemisah): tampil / sembunyikan panel kiri
        self.toggle_btn = GenButton(panel, label="\u25c0", size=(22, -1))
        self.toggle_btn.SetUseFocusIndicator(False)
        self.toggle_btn.SetBackgroundColour(wx.Colour(222, 227, 238))
        self.toggle_btn.SetForegroundColour(wx.Colour(60, 70, 95))
        self.toggle_btn.SetToolTip("Tampilkan / sembunyikan panel kiri")
        self.toggle_btn.Bind(wx.EVT_BUTTON, self.on_toggle_left)

        root.Add(left, 3, wx.EXPAND)
        root.Add(self.toggle_btn, 0, wx.EXPAND | wx.TOP | wx.BOTTOM | wx.LEFT | wx.RIGHT, 6)
        root.Add(right, 5, wx.EXPAND)
        panel.SetSizer(root)

        self.CreateStatusBar()
        self.SetStatusText("Siap")

    def _build_left(self, panel):
        """Sisi kiri: informasi IP (kode asli)."""
        left = wx.BoxSizer(wx.VERTICAL)

        # Header: IP + bendera
        header = wx.BoxSizer(wx.HORIZONTAL)
        ip_box = wx.BoxSizer(wx.VERTICAL)

        caption = wx.StaticText(panel, label="Alamat IP Publik Anda")
        caption.SetForegroundColour(wx.Colour(100, 110, 125))

        self.ip_label = wx.StaticText(panel, label="Memuat...")
        font = self.ip_label.GetFont()
        font.SetPointSize(font.GetPointSize() + 10)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        self.ip_label.SetFont(font)

        ip_box.Add(caption, 0, wx.BOTTOM, 4)
        ip_box.Add(self.ip_label, 0)

        self.flag_bmp = wx.StaticBitmap(panel, bitmap=wx.Bitmap(80, 54))

        header.Add(ip_box, 1, wx.ALIGN_CENTER_VERTICAL)
        header.Add(self.flag_bmp, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12)
        left.Add(header, 0, wx.EXPAND | wx.ALL, 16)

        # Tabel info
        labels = [
            "IP Private", "City", "Country", "Region", "Browser", "ISP",
            "Operating System", "Timezone", "Device", "Currency",
            "Proxy", "Latitude & Longitude", "Hosting",
        ]
        grid = wx.FlexGridSizer(cols=2, vgap=0, hgap=0)
        grid.AddGrowableCol(1, 1)

        for i, name in enumerate(labels):
            bg = wx.Colour(255, 255, 255) if i % 2 == 0 else wx.Colour(238, 241, 246)

            key_panel = wx.Panel(panel)
            key_panel.SetBackgroundColour(bg)
            key_sizer = wx.BoxSizer(wx.VERTICAL)
            key = wx.StaticText(key_panel, label=name + ":")
            kf = key.GetFont()
            kf.SetWeight(wx.FONTWEIGHT_BOLD)
            key.SetFont(kf)
            key_sizer.Add(key, 1, wx.EXPAND | wx.ALL, 6)
            key_panel.SetSizer(key_sizer)

            val_panel = wx.Panel(panel)
            val_panel.SetBackgroundColour(bg)
            val_sizer = wx.BoxSizer(wx.VERTICAL)
            val = wx.StaticText(val_panel, label="-")
            val_sizer.Add(val, 1, wx.EXPAND | wx.ALL, 6)
            val_panel.SetSizer(val_sizer)

            grid.Add(key_panel, 0, wx.EXPAND)
            grid.Add(val_panel, 1, wx.EXPAND)
            self.rows[name] = val

        grid.AddGrowableCol(0, 0)
        left.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)

        # Tombol Refresh / Salin / Peta
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh = wx.Button(panel, label="Refresh")
        self.btn_copy = wx.Button(panel, label="Salin IP")
        self.btn_map = wx.Button(panel, label="Buka di Peta")
        btns.Add(self.btn_refresh, 0, wx.RIGHT, 8)
        btns.Add(self.btn_copy, 0, wx.RIGHT, 8)
        btns.Add(self.btn_map, 0)
        left.Add(btns, 0, wx.ALL, 16)

        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh())
        self.btn_copy.Bind(wx.EVT_BUTTON, self.on_copy)
        self.btn_map.Bind(wx.EVT_BUTTON, self.on_map)
        self.btn_copy.Disable()
        self.btn_map.Disable()
        self._build_server_box(panel, left)
        return left

    def _build_server_box(self, panel, left):
        """Kotak 'Server Speedtest LAN' di paling bawah sisi kiri."""
        box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Server Speedtest LAN")
        sb = box.GetStaticBox()

        # bandwidth bar (di atas tombol)
        bars = wx.BoxSizer(wx.HORIZONTAL)
        self.bw_up = BandwidthGraph(sb, "UPLOAD SERVER", BLUE, height=84)
        self.bw_down = BandwidthGraph(sb, "DOWNLOAD SERVER", GREEN, height=84)
        bars.Add(self.bw_up, 1, wx.EXPAND | wx.RIGHT, 6)
        bars.Add(self.bw_down, 1, wx.EXPAND)
        box.Add(bars, 0, wx.EXPAND | wx.ALL, 8)

        # tombol START SERVER / STOP SERVER / PORT
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.srv_start_btn = make_button(sb, "START SERVER", GREEN)
        self.srv_stop_btn = make_button(sb, "STOP SERVER", GREY)
        self.srv_stop_btn.Disable()
        port_lbl = wx.StaticText(sb, label="PORT:")
        self.srv_port = wx.TextCtrl(sb, value=str(SERVER_DEFAULT_PORT), size=(70, -1),
                                    style=wx.TE_CENTER)
        row.Add(self.srv_start_btn, 1, wx.EXPAND | wx.RIGHT, 6)
        row.Add(self.srv_stop_btn, 1, wx.EXPAND | wx.RIGHT, 10)
        row.Add(port_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        row.Add(self.srv_port, 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        # status: http://IP:PORT  Sedang Berjalan
        self.srv_status = wx.StaticText(sb, label="Server mati")
        sf = self.srv_status.GetFont()
        sf.SetWeight(wx.FONTWEIGHT_BOLD)
        self.srv_status.SetFont(sf)
        self.srv_status.SetForegroundColour(wx.Colour(120, 128, 140))
        box.Add(self.srv_status, 0, wx.ALL, 8)

        left.AddStretchSpacer(1)
        left.Add(box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 16)

        self.srv_start_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_server())
        self.srv_stop_btn.Bind(wx.EVT_BUTTON, lambda e: self.stop_server())

    def _build_right(self, panel):
        """Sisi kanan: input + tombol (2 baris) + grafik ping / gauge / QR + statistik + log."""
        right = wx.BoxSizer(wx.VERTICAL)
        self.right = right

        caption = wx.StaticText(
            panel,
            label="Network Tools - IP / Alamat Web / Teks QR      (IP SCANNER: 192.168.1.1-192.168.1.254)")
        caption.SetForegroundColour(wx.Colour(100, 110, 125))
        right.Add(caption, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)

        self.host = wx.TextCtrl(panel, value="8.8.8.8", style=wx.TE_PROCESS_ENTER)
        right.Add(self.host, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

        # baris 1: tujuh tombol alat
        self.ping_btn = make_button(panel, "PING", GREEN)
        self.trace_btn = make_button(panel, "TRACERT", GREEN)
        self.port_btn = make_button(panel, "PORT", GREEN)
        self.ipscan_btn = make_button(panel, "IP SCANNER", GREEN)
        self.speed_btn = make_button(panel, "SPEEDTEST", GREEN)
        self.http_btn = make_button(panel, "HTTP CHECKER", GREEN)
        self.qr_btn = make_button(panel, "QR GENERATOR", GREEN)
        self.stop_btn = make_button(panel, "STOP", GREY)
        self.stop_btn.Disable()

        brow1 = wx.BoxSizer(wx.HORIZONTAL)
        brow1.Add(self.ping_btn, 1, wx.EXPAND | wx.RIGHT, 8)
        brow1.Add(self.trace_btn, 1, wx.EXPAND | wx.RIGHT, 8)
        brow1.Add(self.port_btn, 1, wx.EXPAND | wx.RIGHT, 8)
        brow1.Add(self.ipscan_btn, 1, wx.EXPAND | wx.RIGHT, 8)
        brow1.Add(self.speed_btn, 1, wx.EXPAND | wx.RIGHT, 8)
        brow1.Add(self.http_btn, 1, wx.EXPAND | wx.RIGHT, 8)
        brow1.Add(self.qr_btn, 1, wx.EXPAND)
        right.Add(brow1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

        # baris 2: STOP selebar penuh
        right.Add(self.stop_btn, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

        # grafik batang ping (lebar penuh sisi kanan, tinggi kecil)
        self.graph = PingGraph(panel, height=72)
        right.Add(self.graph, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

        # gauge speedtest (menggantikan grafik ping saat SPEEDTEST dipakai)
        self.gauge = SpeedGauge(panel, height=240)
        right.Add(self.gauge, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)
        right.Show(self.gauge, False)

        # QR code + tombol simpan (menggantikan grafik saat QR GENERATOR dipakai)
        self.qr_box = wx.BoxSizer(wx.VERTICAL)
        self.qr_panel = QRPanel(panel, height=260)
        self.save_btn = make_button(panel, "SIMPAN QR (PNG)", BLUE)
        self.qr_box.Add(self.qr_panel, 1, wx.EXPAND)
        self.qr_box.Add(self.save_btn, 0, wx.EXPAND | wx.TOP, 8)
        right.Add(self.qr_box, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)
        right.Show(self.qr_box, False)

        # angka besar (ms / Hop / kecepatan), lalu statistik satu baris di bawahnya
        self.big = wx.StaticText(
            panel, label="--", style=wx.ALIGN_CENTER_HORIZONTAL | wx.ST_NO_AUTORESIZE
        )
        bf = self.big.GetFont()
        bf.SetPointSize(26)
        bf.SetWeight(wx.FONTWEIGHT_BOLD)
        self.big.SetFont(bf)
        self.big.SetMinSize((200, 48))
        self.stats_bar = StatsBar(panel)
        right.Add(self.big, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)
        right.Add(self.stats_bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 4)

        # radio button satuan speedtest (tepat di atas kotak output)
        unit_row = wx.BoxSizer(wx.HORIZONTAL)
        unit_lbl = wx.StaticText(panel, label="Satuan speedtest:")
        unit_lbl.SetForegroundColour(wx.Colour(100, 110, 125))
        self.rb_mbps = wx.RadioButton(panel, label="Mbps", style=wx.RB_GROUP)
        self.rb_mbs = wx.RadioButton(panel, label="MB/s")
        self.rb_mbps.SetValue(True)
        unit_row.Add(unit_lbl, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        unit_row.Add(self.rb_mbps, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)
        unit_row.Add(self.rb_mbs, 0, wx.ALIGN_CENTER_VERTICAL)
        right.Add(unit_row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 12)

        self.log = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.log.SetFont(wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        right.Add(self.log, 1, wx.EXPAND | wx.ALL, 16)

        self.ping_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("ping"))
        self.trace_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("tracert"))
        self.port_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("port"))
        self.ipscan_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("ipscan"))
        self.speed_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("speedtest"))
        self.http_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("http"))
        self.qr_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("qr"))
        self.save_btn.Bind(wx.EVT_BUTTON, self.on_save_qr)
        self.host.Bind(wx.EVT_TEXT_ENTER, lambda e: self.start_tool("ping"))
        self.stop_btn.Bind(wx.EVT_BUTTON, lambda e: self.stop_proc())
        self.rb_mbps.Bind(wx.EVT_RADIOBUTTON, self.on_unit)
        self.rb_mbs.Bind(wx.EVT_RADIOBUTTON, self.on_unit)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.update_stats()
        return right

    # ------------------------------------------------------------- Info IP
    def refresh(self):
        self.rows["IP Private"].SetLabel(get_private_ip())
        self.btn_refresh.Disable()
        self.btn_copy.Disable()
        self.btn_map.Disable()
        self.ip_label.SetLabel("Memuat...")
        self.SetStatusText("Mengambil data dari internet...")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            data = fetch_json(API_URL)
            if data.get("status") != "success":
                raise RuntimeError(data.get("message", "Permintaan gagal"))
        except Exception as exc:
            wx.CallAfter(self._on_error, exc)
            return

        flag_bytes = None
        code = (data.get("countryCode") or "").lower()
        if code:
            try:
                flag_bytes = fetch_bytes(FLAG_URL.format(code=code))
            except Exception:
                flag_bytes = None

        wx.CallAfter(self._on_data, data, flag_bytes)

    def _on_error(self, exc):
        self.ip_label.SetLabel("Gagal memuat")
        self.SetStatusText(f"Error: {exc}")
        self.btn_refresh.Enable()
        wx.MessageBox(
            f"Tidak dapat mengambil data IP.\n\nDetail: {exc}\n\n"
            "Periksa koneksi internet Anda lalu klik Refresh.",
            "Kesalahan",
            wx.OK | wx.ICON_ERROR,
        )

    def _on_data(self, data, flag_bytes):
        self.ip_text = data.get("query", "")
        self.lat = data.get("lat")
        self.lon = data.get("lon")
        self.ip_label.SetLabel(self.ip_text)

        country = data.get("country", "-")
        code = data.get("countryCode", "")
        continent = data.get("continent", "")
        country_line = f"{country} {code} {continent}".strip()

        region_line = f"{data.get('region', '')} {data.get('regionName', '')}".strip()

        values = {
            "City": data.get("city", "-"),
            "Country": country_line,
            "Region": region_line or "-",
            "Browser": get_default_browser() + " (browser default)",
            "ISP": data.get("isp", "-"),
            "Operating System": get_os_name(),
            "Timezone": data.get("timezone", "-"),
            "Device": "Mobile / Seluler" if data.get("mobile") else "No",
            "Currency": data.get("currency", "-"),
            "Proxy": "Yes" if data.get("proxy") else "No",
            "Latitude & Longitude": f"{self.lat} {self.lon}",
            "Hosting": "Yes" if data.get("hosting") else "No",
        }
        for key, val in values.items():
            self.rows[key].SetLabel(str(val))

        if flag_bytes:
            try:
                img = wx.Image(io.BytesIO(flag_bytes))
                if img.IsOk():
                    w = 80
                    h = int(img.GetHeight() * w / img.GetWidth())
                    img = img.Scale(w, h, wx.IMAGE_QUALITY_HIGH)
                    self.flag_bmp.SetBitmap(wx.Bitmap(img))
            except Exception:
                pass

        self.Layout()
        self.btn_refresh.Enable()
        self.btn_copy.Enable()
        self.btn_map.Enable()
        self.SetStatusText("Data berhasil dimuat")

    def on_copy(self, _evt):
        if not self.ip_text:
            return
        if wx.TheClipboard.Open():
            wx.TheClipboard.SetData(wx.TextDataObject(self.ip_text))
            wx.TheClipboard.Close()
            self.SetStatusText(f"IP {self.ip_text} disalin ke clipboard")

    def on_map(self, _evt):
        if self.lat is not None and self.lon is not None:
            webbrowser.open(
                f"https://www.openstreetmap.org/?mlat={self.lat}&mlon={self.lon}"
                f"#map=11/{self.lat}/{self.lon}"
            )

    # ------------------------------------------------- Network Tools (kanan)
    def set_busy(self, busy):
        tools = (self.ping_btn, self.trace_btn, self.port_btn, self.ipscan_btn,
                 self.speed_btn, self.http_btn, self.qr_btn)
        for b in tools:
            b.Enable(not busy)
            b.SetBackgroundColour(GREY if busy else GREEN)
        self.host.Enable(not busy)
        self.stop_btn.Enable(busy)
        # hijau saat aktif, abu-abu saat dikunci; STOP merah saat bisa dipakai
        self.stop_btn.SetBackgroundColour(RED if busy else GREY)
        for b in tools + (self.stop_btn,):
            b.Refresh()

    def show_view(self, view):
        """view: 'graph' (grafik ping), 'gauge' (speedtest), atau 'qr' (QR code)."""
        self.right.Show(self.graph, view == "graph")
        self.right.Show(self.gauge, view == "gauge")
        self.right.Show(self.qr_box, view == "qr")
        self.right.Layout()

    def current_unit(self):
        return "MB/s" if self.rb_mbs.GetValue() else "Mbps"

    def to_unit(self, mbps):
        return mbps / 8.0 if self.current_unit() == "MB/s" else mbps

    def fmt_speed(self, mbps):
        return f"{self.to_unit(mbps):.2f} {self.current_unit()}"

    def update_stats(self):
        st = self.stats
        D, S = StatsBar.DARK, StatsBar.SEP

        # QR dan HTTP mengisi statistiknya sendiri
        if self.mode in ("qr", "http"):
            return

        if self.mode == "speedtest":
            sep = ("  |  ", S)
            sp = self.sp
            ping_txt = fmt_ms(sp["ping"]) if sp["ping"] is not None else "--"
            dl_txt = self.fmt_speed(sp["download"]) if sp["download"] is not None else "--"
            ul_txt = self.fmt_speed(sp["upload"]) if sp["upload"] is not None else "--"
            self.stats_bar.set_lines([
                [("fase : ", D), (self.sp_phase or "--", D), sep, ("ping : ", D), (ping_txt, D)],
                [("download : ", D), (dl_txt, GREEN if sp["download"] is not None else D), sep,
                 ("upload : ", D), (ul_txt, GREEN if sp["upload"] is not None else D)],
            ])
            return

        if self.mode in ("port", "ipscan") and self.scan is not None:
            sc = self.scan
            found_label = "port terbuka" if self.mode == "port" else "host aktif"
            sep = ("  |  ", S)
            self.stats_bar.set_lines([
                [("scan : ", D), (f"{sc.percent():.1f}%", D), sep,
                 ("dicek : ", D), (f"{sc.done}/{sc.total}", D)],
                [(found_label + " : ", D), (str(sc.found), GREEN if sc.found else D), sep,
                 ("time : ", D), (sc.elapsed_text(), D)],
            ])
            return

        ping_txt, ping_col = "--", D
        if st.last_lost:
            ping_txt, ping_col = "timeout", RED
        elif st.last is not None:
            ping_txt, ping_col = fmt_ms(st.last), PingGraph._color(st.last)

        avg = st.avg()
        loss = st.loss_pct()
        avg_txt = fmt_ms(avg) if avg is not None else "--"
        max_txt = fmt_ms(st.max) if st.max is not None else "--"
        min_txt = fmt_ms(st.min) if st.min is not None else "--"
        loss_txt = f"{round(loss, 1):g}%" if loss is not None else "--"
        loss_col = RED if st.lost > 0 else D
        sep = (" | ", S)

        # semua dalam SATU baris
        line = [("ping : ", D), (ping_txt, ping_col), sep,
                ("avg : ", D), (avg_txt, D), sep,
                ("max : ", D), (max_txt, D), sep,
                ("min : ", D), (min_txt, D), sep,
                ("loss : ", D), (loss_txt, loss_col), sep,
                ("time : ", D), (st.elapsed_text(), D)]
        self.stats_bar.set_lines([line])

    def on_timer(self, _evt):
        if self.running:
            self.update_stats()

    # ------------------------------------------------------- QR Generator
    def make_qr(self):
        text = self.host.GetValue().strip()
        if not text:
            wx.MessageBox("Isi teks atau URL di kolom input dulu, lalu klik QR GENERATOR.",
                          "QR Generator", wx.OK | wx.ICON_WARNING)
            return
        try:
            matrix = build_qr_matrix(text)
        except ImportError:
            wx.MessageBox("Library 'qrcode' belum terpasang.\n\nJalankan di terminal / CMD:\n"
                          "    pip install qrcode",
                          "QR Generator", wx.OK | wx.ICON_ERROR)
            return
        except Exception as exc:
            wx.MessageBox(f"Gagal membuat QR code.\n\nDetail: {exc}\n"
                          "(Teks mungkin terlalu panjang.)",
                          "QR Generator", wx.OK | wx.ICON_ERROR)
            return

        self.mode = "qr"
        self.qr_text = text
        self.qr_panel.set_matrix(matrix)
        self.show_view("qr")
        n = len(matrix)
        self.big.SetLabel("QR")
        self.big.SetForegroundColour(wx.Colour(30, 35, 45))
        D, S = StatsBar.DARK, StatsBar.SEP
        self.stats_bar.set_lines([
            [("panjang data : ", D), (f"{len(text)} karakter", D), (" | ", S),
             ("ukuran : ", D), (f"{n} x {n} modul", D)],
        ])
        self.log.Clear()
        self.append_log(f"QR code dibuat dari:\n{text}\n\nKlik 'SIMPAN QR (PNG)' untuk menyimpan gambar.")
        self.SetStatusText("QR code berhasil dibuat")

    def on_save_qr(self, _evt):
        if not self.qr_panel.matrix:
            return
        with wx.FileDialog(self, "Simpan QR Code", wildcard="Gambar PNG (*.png)|*.png",
                           defaultFile="qrcode.png",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            path = dlg.GetPath()
        if not path.lower().endswith(".png"):
            path += ".png"
        bmp = self.qr_panel.to_bitmap()
        if bmp.SaveFile(path, wx.BITMAP_TYPE_PNG):
            self.SetStatusText(f"QR code disimpan: {path}")
            wx.MessageBox(f"QR code berhasil disimpan:\n{path}", "QR Generator",
                          wx.OK | wx.ICON_INFORMATION)
        else:
            wx.MessageBox("Gagal menyimpan file.", "QR Generator", wx.OK | wx.ICON_ERROR)

    # ------------------------------------------------ Toggle panel kiri (v5)
    def on_toggle_left(self, _evt=None):
        self.left_visible = not self.left_visible
        self.root.Show(self.left_sizer, self.left_visible)
        self.toggle_btn.SetLabel("\u25c0" if self.left_visible else "\u25b6")
        self.panel.Layout()
        self.panel.Refresh()

    # ------------------------------------------- Server Speedtest LAN (v5)
    def set_server_ui(self, running):
        self.srv_start_btn.Enable(not running)
        self.srv_stop_btn.Enable(running)
        self.srv_start_btn.SetBackgroundColour(GREY if running else GREEN)
        self.srv_stop_btn.SetBackgroundColour(RED if running else GREY)
        self.srv_port.Enable(not running)
        if running:
            self.srv_status.SetLabel(f"{self.srv_url}  Sedang Berjalan")
            self.srv_status.SetForegroundColour(GREEN)
        else:
            self.srv_status.SetLabel("Server mati")
            self.srv_status.SetForegroundColour(wx.Colour(120, 128, 140))
        for b in (self.srv_start_btn, self.srv_stop_btn):
            b.Refresh()
        self.panel.Layout()

    def start_server(self):
        if self.srv is not None:
            return
        try:
            port = int(self.srv_port.GetValue().strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            wx.MessageBox("Port harus berupa angka 1 - 65535.", "Server Speedtest LAN",
                          wx.OK | wx.ICON_WARNING)
            return

        srv = LanSpeedServer(port, on_log=lambda m: wx.CallAfter(self.append_log, m))
        try:
            srv.start()
        except OSError as exc:
            wx.MessageBox(
                f"Server gagal dijalankan di port {port}.\n\nDetail: {exc}\n\n"
                "Kemungkinan port sedang dipakai program lain. Coba port lain.",
                "Server Speedtest LAN", wx.OK | wx.ICON_ERROR)
            return

        self.srv = srv
        self.srv_url = f"http://{get_private_ip()}:{port}"
        self.srv_last = (0, 0, time.monotonic())
        self.bw_up.clear()
        self.bw_down.clear()
        self.set_server_ui(True)
        self.srv_timer.Start(500)
        self.append_log(f"[SERVER] Speedtest LAN berjalan: {self.srv_url}")
        self.SetStatusText(f"Server Speedtest LAN berjalan di {self.srv_url}")

    def stop_server(self):
        if self.srv is None:
            return
        self.srv_timer.Stop()
        srv, self.srv = self.srv, None
        srv.stop()
        self.bw_up.add(0.0)
        self.bw_down.add(0.0)
        self.set_server_ui(False)
        self.append_log("[SERVER] Speedtest LAN dihentikan.")
        self.SetStatusText("Server Speedtest LAN dihentikan")

    def on_srv_timer(self, _evt):
        if self.srv is None:
            return
        up, down = self.srv.counters()
        now = time.monotonic()
        pu, pd, pt = self.srv_last
        dt = now - pt
        if dt > 0:
            self.bw_up.add((up - pu) * 8 / 1e6 / dt)       # klien upload -> server terima
            self.bw_down.add((down - pd) * 8 / 1e6 / dt)   # klien download <- server kirim
        self.srv_last = (up, down, now)

    def start_tool(self, mode):
        if mode == "qr":
            if not self.running:
                self.make_qr()
            return

        target = self.host.GetValue().strip()
        if self.running:
            return
        if not target and mode != "speedtest":
            return

        ip_range = None
        if mode == "ipscan":
            try:
                ip_range = parse_ip_range(target)
            except ValueError as exc:
                priv = get_private_ip()
                base = priv.rsplit(".", 1)[0] if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", priv) else "192.168.1"
                wx.MessageBox(
                    f"{exc}\n\nIP SCANNER wajib memakai rentang IP address.\n"
                    f"Contoh: {base}.1-{base}.254   atau   {base}.1-254",
                    "IP Scanner", wx.OK | wx.ICON_WARNING)
                return
        elif mode == "http":
            try:
                target = normalize_http_url(target)
            except ValueError as exc:
                wx.MessageBox(str(exc), "HTTP Checker", wx.OK | wx.ICON_WARNING)
                return
        elif mode != "speedtest" and not re.fullmatch(r"[A-Za-z0-9.\-:_]+", target):
            wx.MessageBox("Alamat tidak valid.", "Network Tools", wx.OK | wx.ICON_WARNING)
            return

        cmd = None
        if mode in ("ping", "tracert"):
            cmd = build_command(mode, target)
            if cmd is None:
                wx.MessageBox("traceroute/tracepath tidak terpasang di sistem ini.",
                              "Network Tools", wx.OK | wx.ICON_ERROR)
                return

        self.mode = mode
        self.log.Clear()
        self.graph.clear()
        self.scan = None
        if mode == "ping":
            self.stats.start()
        else:
            self.stats.reset()
        if mode == "port":
            self.scan = ScanStats(65535)
        elif mode == "ipscan":
            self.scan = ScanStats(ip_range[1] - ip_range[0] + 1)
        elif mode == "speedtest":
            self.sp = {"ping": None, "download": None, "upload": None}
            self.sp_phase = ""
            self.gauge.reset()
            self.gauge.phase = "PING"
            self.gauge.unit = self.current_unit()
        if mode in ("ping", "port", "ipscan"):
            self.timer.Start(1000)

        self.show_view("gauge" if mode == "speedtest" else "graph")
        self.update_stats()
        self.big.SetLabel("...")
        self.big.SetForegroundColour(wx.Colour(30, 35, 45))
        self.running = True
        self.set_busy(True)
        names = {"ping": "PING", "tracert": "TRACERT", "port": "SCAN PORT",
                 "ipscan": "IP SCANNER", "speedtest": "SPEEDTEST", "http": "HTTP CHECKER"}
        shown = target if mode != "speedtest" else SPEED_HOST
        self.SetStatusText(f"{names[mode]} {shown} berjalan...")

        if mode == "port":
            threading.Thread(target=self.port_worker, args=(target,), daemon=True).start()
        elif mode == "ipscan":
            threading.Thread(target=self.ip_worker, args=ip_range, daemon=True).start()
        elif mode == "speedtest":
            threading.Thread(target=self.speed_worker, daemon=True).start()
        elif mode == "http":
            threading.Thread(target=self.http_worker, args=(target,), daemon=True).start()
        else:
            threading.Thread(target=self.tool_worker, args=(mode, cmd), daemon=True).start()

    def stop_proc(self):
        self.running = False
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def on_close(self, evt):
        self.timer.Stop()
        self.srv_timer.Stop()
        if self.srv is not None:
            self.srv.stop()
            self.srv = None
        self.gauge.timer.Stop()
        self.stop_proc()
        evt.Skip()

    def tool_worker(self, mode, cmd):
        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1, **kwargs
            )
            for raw in iter(self.proc.stdout.readline, b""):
                if not self.running:
                    break
                line = raw.decode(errors="replace").rstrip()
                if line:
                    wx.CallAfter(self.show_line, mode, line)
        except FileNotFoundError:
            wx.CallAfter(self.show_line, mode, f"Perintah '{cmd[0]}' tidak ditemukan.")
        except Exception as e:
            wx.CallAfter(self.show_line, mode, f"Error: {e}")
        finally:
            wx.CallAfter(self.tool_finished)

    def append_log(self, text):
        self.log.AppendText(text + "\n")

    def scan_progress(self, done, found):
        if self.scan is None:
            return
        self.scan.done = done
        self.scan.found = found
        self.big.SetLabel(f"{self.scan.percent():.0f}%")
        self.update_stats()

    def port_worker(self, target):
        """Scan port TCP 1-65535 (TCP connect scan)."""
        try:
            try:
                ip = socket.gethostbyname(target)
            except OSError:
                wx.CallAfter(self.append_log, f"Host tidak ditemukan: {target}")
                return
            wx.CallAfter(self.append_log,
                         f"Scan port TCP 1-65535 pada {target} ({ip})\n"
                         "(port 0 dilewati: port cadangan, tidak bisa dikoneksikan)\n")

            def probe(port):
                return self.running and check_port(ip, port)

            open_ports, done, last_ui = [], 0, 0.0
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS_PORT) as ex:
                for first in range(1, 65536, 4000):
                    if not self.running:
                        break
                    futures = {ex.submit(probe, p): p for p in range(first, min(first + 4000, 65536))}
                    for fut in as_completed(futures):
                        done += 1
                        port = futures[fut]
                        if fut.result():
                            open_ports.append(port)
                            wx.CallAfter(self.append_log, f"[+] PORT {port}/tcp  open  {service_name(port)}")
                        now = time.monotonic()
                        if now - last_ui > 0.2:
                            last_ui = now
                            wx.CallAfter(self.scan_progress, done, len(open_ports))
                        if not self.running:
                            for f in futures:
                                f.cancel()
                            break

            wx.CallAfter(self.scan_progress, done, len(open_ports))
            state = "selesai" if self.running else "dihentikan"
            lines = ["", f"--- {state}: {len(open_ports)} port terbuka dari {done} port dicek ---"]
            lines += [f"{p:>5}/tcp  open  {service_name(p)}" for p in sorted(open_ports)]
            wx.CallAfter(self.append_log, "\n".join(lines))
        except Exception as e:
            wx.CallAfter(self.append_log, f"Error: {e}")
        finally:
            wx.CallAfter(self.tool_finished)

    def ip_worker(self, start, end):
        """Scan rentang IP dengan ping."""
        try:
            ips = [str(ipaddress.IPv4Address(i)) for i in range(start, end + 1)]
            wx.CallAfter(self.append_log, f"Scan {ips[0]} - {ips[-1]} ({len(ips)} IP)\n")

            def probe(ip):
                if not self.running:
                    return None
                ok, ms = ping_once(ip)
                return (ip, ms, resolve_name(ip)) if ok else None

            alive, done, last_ui = [], 0, 0.0
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS_IP) as ex:
                for first in range(0, len(ips), 2000):
                    if not self.running:
                        break
                    futures = [ex.submit(probe, ip) for ip in ips[first:first + 2000]]
                    for fut in as_completed(futures):
                        done += 1
                        r = fut.result()
                        if r:
                            alive.append(r)
                            wx.CallAfter(self.append_log, f"[+] {r[0]:<15}  {r[1]:<8}  {r[2]}")
                        now = time.monotonic()
                        if now - last_ui > 0.2:
                            last_ui = now
                            wx.CallAfter(self.scan_progress, done, len(alive))
                        if not self.running:
                            for f in futures:
                                f.cancel()
                            break

            wx.CallAfter(self.scan_progress, done, len(alive))
            state = "selesai" if self.running else "dihentikan"
            alive.sort(key=lambda r: int(ipaddress.IPv4Address(r[0])))
            lines = ["", f"--- {state}: {len(alive)} host aktif dari {done} IP dicek ---"]
            lines += [f"{r[0]:<15}  {r[1]:<8}  {r[2]}" for r in alive]
            lines.append("(host yang memblokir ping tidak akan muncul)")
            wx.CallAfter(self.append_log, "\n".join(lines))
        except Exception as e:
            wx.CallAfter(self.append_log, f"Error: {e}")
        finally:
            wx.CallAfter(self.tool_finished)

    # ---------------------------------------------------------- HTTP Checker
    def http_worker(self, target):
        try:
            wx.CallAfter(self.append_log, f"HTTP Checker: {target}\n")
            req = urllib.request.Request(
                target,
                headers={"User-Agent": HTTP_UA},
                method="GET",
            )
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                    elapsed = (time.perf_counter() - t0) * 1000
                    status = resp.status
                    reason = getattr(resp, "reason", "") or ""
                    final_url = resp.geturl()
                    server = resp.headers.get("Server", "-")
                    content_type = resp.headers.get("Content-Type", "-")
                    content_length = resp.headers.get("Content-Length")
                    body = resp.read(1024 * 1024)
                    size = int(content_length) if content_length and content_length.isdigit() else len(body)
                    wx.CallAfter(self.http_result, status, reason, elapsed, size,
                                 server, content_type, final_url)
            except urllib.error.HTTPError as e:
                elapsed = (time.perf_counter() - t0) * 1000
                wx.CallAfter(self.http_result, e.code, str(e.reason), elapsed, 0,
                             e.headers.get("Server", "-") if e.headers else "-",
                             e.headers.get("Content-Type", "-") if e.headers else "-",
                             e.geturl())
        except Exception as e:
            wx.CallAfter(self.append_log, f"HTTP ERROR: {e}")
        finally:
            wx.CallAfter(self.tool_finished)

    def http_result(self, status, reason, elapsed, size, server, content_type, final_url):
        ok = 200 <= status < 400
        status_text = f"{status} {reason}".strip()
        color = GREEN if ok else RED
        self.big.SetLabel(str(status))
        self.big.SetForegroundColour(color)
        self.stats_bar.set_lines([
            [("status : ", StatsBar.DARK), (status_text, color),
             ("  |  ", StatsBar.SEP), ("time : ", StatsBar.DARK),
             (f"{elapsed:.0f} ms", StatsBar.DARK)],
            [("size : ", StatsBar.DARK), (f"{size / 1024:.1f} KB", StatsBar.DARK),
             ("  |  ", StatsBar.SEP), ("server : ", StatsBar.DARK), (server, StatsBar.DARK)],
        ])
        self.append_log(f"Status       : {status_text}")
        self.append_log(f"Response time: {elapsed:.1f} ms")
        self.append_log(f"Content-Type : {content_type}")
        self.append_log(f"Size         : {size / 1024:.1f} KB")
        self.append_log(f"Server       : {server}")
        self.append_log(f"Final URL    : {final_url}")
        self.append_log(f"Kesimpulan   : {'ONLINE / OK' if ok else 'HTTP ERROR'}")

    # ------------------------------------------------------------ Speedtest
    def on_unit(self, _evt):
        self.refresh_speed_view()

    def refresh_speed_view(self):
        """Dipanggil saat radio Mbps / MB/s diganti (atau saat tes selesai)."""
        unit = self.current_unit()
        self.gauge.unit = unit
        if self.sp_phase in ("SELESAI", "BERHENTI"):
            dl = self.sp["download"]
            if dl is not None:
                self.gauge.center = (f"{self.to_unit(dl):.1f}", f"{unit}  (download)")
            else:
                self.gauge.center = ("--", unit)
        if self.mode == "speedtest":
            self.update_stats()
            dl = self.sp["download"]
            if self.sp_phase in ("SELESAI", "BERHENTI") and dl is not None:
                self.big.SetLabel(f"{self.to_unit(dl):.1f} {unit}")
        self.gauge.Refresh()

    def speed_phase(self, phase):
        self.sp_phase = phase
        self.gauge.phase = phase
        self.gauge.target = 0.0
        if phase == "PING":
            self.gauge.center = ("...", "mengukur ping")
            self.big.SetLabel("...")
        elif phase in ("DOWNLOAD", "UPLOAD"):
            self.gauge.center = None
            self.append_log(f"{phase.capitalize()} dimulai ({SPEED_DURATION} detik, {SPEED_THREADS} koneksi)...")
        self.refresh_speed_view()

    def speed_tick(self, mbps):
        """Update live saat download / upload berjalan."""
        self.gauge.target = mbps
        self.big.SetLabel(f"{self.to_unit(mbps):.1f} {self.current_unit()}")
        self.update_stats()

    def speed_result(self, kind, value):
        self.sp[kind] = value
        if kind == "ping":
            self.append_log(f"Ping     : {value:.1f} ms")
        else:
            self.append_log(
                f"{kind.capitalize():<9}: {value:.2f} Mbps  ({value / 8:.2f} MB/s)")
        self.update_stats()

    def _speed_conn(self):
        return http.client.HTTPSConnection(SPEED_HOST, timeout=10)

    def measure_ping(self):
        """Ping HTTP ke server speedtest (rata-rata beberapa kali, tanpa handshake pertama)."""
        conn = self._speed_conn()
        try:
            conn.request("GET", "/__down?bytes=0", headers=SPEED_UA)
            conn.getresponse().read()             # pemanasan: koneksi + TLS
            times = []
            for _ in range(6):
                if not self.running:
                    break
                t = time.perf_counter()
                conn.request("GET", "/__down?bytes=0", headers=SPEED_UA)
                conn.getresponse().read()
                times.append((time.perf_counter() - t) * 1000)
                time.sleep(0.1)
            return sum(times) / len(times) if times else None
        finally:
            conn.close()

    def _dl_thread(self, counter, lock, deadline):
        try:
            while self.running and time.monotonic() < deadline:
                conn = self._speed_conn()
                try:
                    conn.request("GET", "/__down?bytes=25000000", headers=SPEED_UA)
                    resp = conn.getresponse()
                    while self.running and time.monotonic() < deadline:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        with lock:
                            counter[0] += len(chunk)
                finally:
                    conn.close()
        except Exception as e:
            counter[1] = str(e)

    def _ul_thread(self, counter, lock, deadline):
        block = b"0" * 65536
        size = 10 * 1024 * 1024
        try:
            while self.running and time.monotonic() < deadline:
                conn = self._speed_conn()
                try:
                    conn.putrequest("POST", "/__up")
                    conn.putheader("User-Agent", SPEED_UA["User-Agent"])
                    conn.putheader("Content-Type", "application/octet-stream")
                    conn.putheader("Content-Length", str(size))
                    conn.endheaders()
                    sent = 0
                    while sent < size and self.running and time.monotonic() < deadline:
                        conn.send(block)
                        sent += len(block)
                        with lock:
                            counter[0] += len(block)
                    if sent >= size:
                        conn.getresponse().read()
                finally:
                    conn.close()
        except Exception as e:
            counter[1] = str(e)

    def run_phase(self, kind):
        """Jalankan tahap download / upload. Return kecepatan rata-rata (Mbps) atau None."""
        counter = [0, None]            # [total byte, pesan error terakhir]
        lock = threading.Lock()
        deadline = time.monotonic() + SPEED_DURATION
        target = self._dl_thread if kind == "download" else self._ul_thread
        threads = [threading.Thread(target=target, args=(counter, lock, deadline), daemon=True)
                   for _ in range(SPEED_THREADS)]
        for th in threads:
            th.start()

        t0 = time.monotonic()
        hist = deque()                 # jendela geser 1 detik untuk kecepatan live
        warm = None                    # titik awal hitung rata-rata (lewati 1 detik pertama)
        now, b = t0, 0
        while self.running:
            time.sleep(0.15)
            now = time.monotonic()
            with lock:
                b = counter[0]
            hist.append((now, b))
            while len(hist) > 2 and now - hist[0][0] > 1.0:
                hist.popleft()
            t_old, b_old = hist[0]
            cur = (b - b_old) * 8 / 1e6 / (now - t_old) if now > t_old else 0.0
            if warm is None and now - t0 >= 1.0:
                warm = (now, b)
            wx.CallAfter(self.speed_tick, cur)
            if now >= deadline or not any(t.is_alive() for t in threads):
                break

        for th in threads:
            th.join(timeout=1)
        if not self.running:
            return None
        if b == 0:
            raise RuntimeError(counter[1] or "tidak ada data yang diterima")
        start_t, start_b = warm if warm else (t0, 0)
        span = now - start_t
        if span <= 0:
            return 0.0
        return (b - start_b) * 8 / 1e6 / span

    def speed_worker(self):
        try:
            wx.CallAfter(self.speed_phase, "PING")
            wx.CallAfter(self.append_log, f"Server: {SPEED_HOST}\n")
            ping = self.measure_ping()
            if not self.running:
                return
            if ping is None:
                raise RuntimeError("pengukuran ping gagal")
            wx.CallAfter(self.speed_result, "ping", ping)

            for kind in ("download", "upload"):
                wx.CallAfter(self.speed_phase, kind.upper())
                value = self.run_phase(kind)
                if value is None:          # dihentikan lewat tombol STOP
                    return
                wx.CallAfter(self.speed_result, kind, value)

            wx.CallAfter(self.speed_phase, "SELESAI")
            wx.CallAfter(self.append_log, "\n--- speedtest selesai ---")
        except Exception as e:
            wx.CallAfter(
                self.append_log,
                f"Error: {e}\nPeriksa koneksi internet / firewall (butuh akses ke {SPEED_HOST}).")
        finally:
            wx.CallAfter(self.tool_finished)

    def show_line(self, mode, line):
        self.log.AppendText(line + "\n")
        if mode == "ping":
            m = TIME_RE.search(line)
            if m:
                try:
                    ms = float(m.group(1).replace(",", ".").rstrip("."))
                except ValueError:
                    ms = None
                if ms is not None:
                    self.graph.add(ms)
                    self.stats.add(ms)
                    self.update_stats()
                    self.big.SetLabel(f"{m.group(1)} ms")
            elif TIMEOUT_RE.search(line):
                self.graph.add(None)
                self.stats.add(None)
                self.update_stats()
                self.big.SetLabel("Timeout")
        else:
            m = HOP_RE.match(line)
            if m:
                self.big.SetLabel(f"Hop {m.group(1)}")
        self.big.GetParent().Layout()

    def tool_finished(self):
        self.running = False
        self.timer.Stop()
        self.stats.stop()
        if self.scan is not None and self.scan.t_end is None:
            self.scan.t_end = time.monotonic()
        if self.mode == "speedtest" and self.sp_phase != "SELESAI":
            self.speed_phase("BERHENTI")
        self.update_stats()
        self.set_busy(False)
        if self.mode in ("ping", "tracert"):
            self.log.AppendText("--- selesai ---\n")
        self.SetStatusText("Siap")


if __name__ == "__main__":
    app = wx.App(False)
    frame = MainFrame()
    frame.Show()
    frame.Maximize(True)
    app.MainLoop()
