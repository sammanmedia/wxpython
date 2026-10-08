"""
Aplikasi Info IP Saya + Network Tools (wxPython)
------------------------------------------------
KIRI  : IP publik beserta kota, negara, region, ISP, zona waktu,
        mata uang, koordinat, status proxy/hosting, dan bendera negara.
KANAN : Form input IP / alamat web + tombol
            [PING] [TRACERT] [PORT] [IP SCANNER] [SPEEDTEST]
            [                  STOP                        ]
        + grafik batang ping (ms) ala Winbox + statistik (avg/max/min/loss/time).
        PORT       : scan port TCP 1-65535 pada IP / host yang diinput.
        IP SCANNER : wajib rentang IP, mis. 192.168.1.1-192.168.1.254
        SPEEDTEST  : ping, download, upload dengan gauge berjarum ala speedtest.net
                     (satuan bisa dipilih: Mbps atau MB/s lewat radio button).
                     Server tes: speed.cloudflare.com (tanpa instalasi tambahan).

Instalasi : pip install wxPython
Jalankan  : python ip_info_app.py

Sumber data: ip-api.com (gratis untuk penggunaan non-komersial, HTTP saja)
Bendera    : flagcdn.com
"""

import http.client
import io
import ipaddress
import json
import math
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
import webbrowser
from collections import deque
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

# ---- pengaturan speedtest ----
SPEED_HOST = "speed.cloudflare.com"
SPEED_DURATION = 8          # detik per tahap (download / upload)
SPEED_THREADS = 4           # koneksi paralel supaya bandwidth terpakai penuh
SPEED_SCALE = {"Mbps": 100.0, "MB/s": 20.0}   # skala maksimal gauge per satuan
SPEED_UA = {"User-Agent": "IPInfoApp/1.0"}


def make_button(parent, label, color):
    """Tombol berwarna (GenButton supaya warna konsisten di semua OS)."""
    btn = GenButton(parent, label=label, size=(90, 34))
    btn.SetUseFocusIndicator(False)
    btn.SetForegroundColour(wx.WHITE)
    btn.SetBackgroundColour(color)
    f = btn.GetFont()
    f.SetWeight(wx.FONTWEIGHT_BOLD)
    btn.SetFont(f)
    return btn


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
        secs = int((self.t_end or time.monotonic()) - self.t0)
        return f"{secs // 60} menit : {secs % 60} detik"


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
        secs = int((self.t_end or time.monotonic()) - self.t0)
        return f"{secs // 60} menit : {secs % 60} detik"


class StatsBar(wx.Panel):
    """Teks statistik tebal (2 baris) di samping angka ms. Mendukung warna per bagian."""

    DARK = wx.Colour(30, 35, 45)
    SEP = wx.Colour(150, 158, 172)

    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.font = wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.lines = []
        self.SetMinSize((200, 48))
        self.Bind(wx.EVT_PAINT, self.on_paint)

    def set_lines(self, lines):
        self.lines = lines
        self.Refresh()

    def on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(self.GetParent().GetBackgroundColour()))
        dc.Clear()
        dc.SetFont(self.font)
        lh = dc.GetTextExtent("Ag")[1]
        gap = 4
        total_h = len(self.lines) * lh + max(0, len(self.lines) - 1) * gap
        y = max(0, (self.GetClientSize().height - total_h) // 2)
        for line in self.lines:
            x = 0
            for text, colour in line:
                dc.SetTextForeground(colour)
                dc.DrawText(text, x, y)
                x += dc.GetTextExtent(text)[0]
            y += lh + gap


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Info IP Saya + Network Tools", size=(1200, 780))
        self.SetMinSize((1080, 700))
        self.ip_text = ""
        self.lat = None
        self.lon = None
        self.rows = {}
        self.proc = None
        self.running = False
        self.stats = PingStats()
        self.mode = None
        self.scan = None
        self.sp = {"ping": None, "download": None, "upload": None}   # hasil speedtest (Mbps / ms)
        self.sp_phase = ""
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_timer, self.timer)
        self._build_ui()
        self.Centre()
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(245, 247, 250))
        root = wx.BoxSizer(wx.HORIZONTAL)

        left = self._build_left(panel)
        right = self._build_right(panel)

        root.Add(left, 4, wx.EXPAND)
        root.Add(wx.StaticLine(panel, style=wx.LI_VERTICAL), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 16)
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
            key_sizer.Add(key, 1, wx.EXPAND | wx.ALL, 8)
            key_panel.SetSizer(key_sizer)

            val_panel = wx.Panel(panel)
            val_panel.SetBackgroundColour(bg)
            val_sizer = wx.BoxSizer(wx.VERTICAL)
            val = wx.StaticText(val_panel, label="-")
            val_sizer.Add(val, 1, wx.EXPAND | wx.ALL, 8)
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
        return left

    def _build_right(self, panel):
        """Sisi kanan: input + tombol (2 baris) + grafik ping / gauge + statistik + log."""
        right = wx.BoxSizer(wx.VERTICAL)
        self.right = right

        caption = wx.StaticText(panel, label="Network Tools - IP / Alamat Web      (IP SCANNER: 192.168.1.1-192.168.1.254)")
        caption.SetForegroundColour(wx.Colour(100, 110, 125))
        right.Add(caption, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)

        self.host = wx.TextCtrl(panel, value="8.8.8.8", style=wx.TE_PROCESS_ENTER)
        right.Add(self.host, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

        # baris 1: lima tombol alat
        self.ping_btn = make_button(panel, "PING", GREEN)
        self.trace_btn = make_button(panel, "TRACERT", GREEN)
        self.port_btn = make_button(panel, "PORT", GREEN)
        self.ipscan_btn = make_button(panel, "IP SCANNER", GREEN)
        self.speed_btn = make_button(panel, "SPEEDTEST", GREEN)
        self.stop_btn = make_button(panel, "STOP", GREY)
        self.stop_btn.Disable()

        brow1 = wx.BoxSizer(wx.HORIZONTAL)
        brow1.Add(self.ping_btn, 1, wx.EXPAND | wx.RIGHT, 8)
        brow1.Add(self.trace_btn, 1, wx.EXPAND | wx.RIGHT, 8)
        brow1.Add(self.port_btn, 1, wx.EXPAND | wx.RIGHT, 8)
        brow1.Add(self.ipscan_btn, 1, wx.EXPAND | wx.RIGHT, 8)
        brow1.Add(self.speed_btn, 1, wx.EXPAND)
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

        # angka besar (ms / Hop / kecepatan) + statistik tebal di sebelahnya
        info_row = wx.BoxSizer(wx.HORIZONTAL)
        self.big = wx.StaticText(
            panel, label="--", style=wx.ALIGN_CENTER_HORIZONTAL | wx.ST_NO_AUTORESIZE
        )
        bf = self.big.GetFont()
        bf.SetPointSize(26)
        bf.SetWeight(wx.FONTWEIGHT_BOLD)
        self.big.SetFont(bf)
        self.big.SetMinSize((200, 48))
        self.stats_bar = StatsBar(panel)
        info_row.Add(self.big, 0, wx.ALIGN_CENTER_VERTICAL)
        info_row.Add(self.stats_bar, 1, wx.EXPAND | wx.LEFT, 12)
        right.Add(info_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 16)

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
        right.Add(unit_row, 0, wx.LEFT | wx.RIGHT | wx.TOP, 16)

        self.log = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.log.SetFont(wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        right.Add(self.log, 1, wx.EXPAND | wx.ALL, 16)

        self.ping_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("ping"))
        self.trace_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("tracert"))
        self.port_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("port"))
        self.ipscan_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("ipscan"))
        self.speed_btn.Bind(wx.EVT_BUTTON, lambda e: self.start_tool("speedtest"))
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
        tools = (self.ping_btn, self.trace_btn, self.port_btn, self.ipscan_btn, self.speed_btn)
        for b in tools:
            b.Enable(not busy)
            b.SetBackgroundColour(GREY if busy else GREEN)
        self.host.Enable(not busy)
        self.stop_btn.Enable(busy)
        # hijau saat aktif, abu-abu saat dikunci; STOP merah saat bisa dipakai
        self.stop_btn.SetBackgroundColour(RED if busy else GREY)
        for b in tools + (self.stop_btn,):
            b.Refresh()

    def show_gauge(self, gauge):
        """True = tampilkan gauge speedtest, False = tampilkan grafik ping."""
        self.right.Show(self.graph, not gauge)
        self.right.Show(self.gauge, gauge)
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
        sep = ("  |  ", S)

        line1 = [("ping : ", D), (ping_txt, ping_col), sep,
                 ("avg : ", D), (avg_txt, D), sep,
                 ("max : ", D), (max_txt, D)]
        line2 = [("min : ", D), (min_txt, D), sep,
                 ("loss : ", D), (loss_txt, loss_col), sep,
                 ("time : ", D), (st.elapsed_text(), D)]
        self.stats_bar.set_lines([line1, line2])

    def on_timer(self, _evt):
        if self.running:
            self.update_stats()

    def start_tool(self, mode):
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

        self.show_gauge(mode == "speedtest")
        self.update_stats()
        self.big.SetLabel("...")
        self.running = True
        self.set_busy(True)
        names = {"ping": "PING", "tracert": "TRACERT", "port": "SCAN PORT",
                 "ipscan": "IP SCANNER", "speedtest": "SPEEDTEST"}
        shown = target if mode != "speedtest" else SPEED_HOST
        self.SetStatusText(f"{names[mode]} {shown} berjalan...")

        if mode == "port":
            threading.Thread(target=self.port_worker, args=(target,), daemon=True).start()
        elif mode == "ipscan":
            threading.Thread(target=self.ip_worker, args=ip_range, daemon=True).start()
        elif mode == "speedtest":
            threading.Thread(target=self.speed_worker, daemon=True).start()
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
    app.MainLoop()
