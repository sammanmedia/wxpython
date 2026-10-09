"""
MikroTik Dashboard v4 (wxPython + librouteros)

Install:
    pip install wxPython librouteros keyring

Alur:
    1. Dialog Login : host, username, password, [x] Ingat saya
    2. Dialog Port  : port API (default 8728) -> baru konek
    3. Dashboard    : info device + tombol [USERNAME KELUAR]
        Baris 1 : Sort by (+ input refresh queue)
        Baris 2 : List Queue     (atas 70%)  - sort, bar usage, double-click = grafik
        Baris 3 : List Interface (bawah 30%) - status JALAN (ijo) / MATI (merah),
                                               Total Download (biru), Total Upload (ijo)
        Baris 4 : Interface WAN (hanya grafik)
                  - ether [ N ]      : nomor ether yang dipantau (ether1 dst)
                  - tombol ON / OFF  : OFF = grafik hilang, tombol tetap ada
                  - refresh [ N ] detik, tinggi grafik [ N ] px
                  - garis vertikal abu-abu tipis tiap 30 detik
                  - internet mati / link putus -> RX & TX merah NAIK FULL

    Teks baris queue: >=100% merah bold, >=70% oranye bold, selain itu hitam biasa.
"""
import json
import math
import os
import re
import threading
import time
from collections import deque

import wx
from wx.lib.buttons import GenButton
from librouteros import connect

try:
    import keyring  # opsional, untuk simpan password dengan aman
except ImportError:
    keyring = None

APP_NAME = "MikroTikDash"
CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".mikrotik_dash.json")

# ---- pengaturan grafik
HISTORY_MAX = 900      # riwayat disimpan (titik)
BAR_PX = 2             # lebar 1 batang (px)
SLOT_PX = 3            # jarak antar batang (px); rapat & TETAP walau di-maximize

# ---- pengaturan bar usage queue
WARN_PCT = 50          # >= 50% oranye
CRIT_PCT = 90          # >= 90% merah

# ---- refresh queue & warna teks baris queue
DEFAULT_Q_INTERVAL = 5       # detik (bisa diubah lewat input di bagian Queue)
ROW_RED_PCT = 100            # >= 100% -> teks merah bold
ROW_ORANGE_PCT = 70          # >= 70%  -> teks oranye bold
ROW_TEXT_RED = wx.Colour(200, 0, 0)
ROW_TEXT_ORANGE = wx.Colour(230, 120, 0)

# ---- warna teks list interface
COLOR_UP = wx.Colour(0, 150, 50)       # JALAN  (ijo)
COLOR_DOWN = wx.Colour(210, 30, 30)    # MATI   (merah)
COLOR_DL = wx.Colour(30, 90, 220)      # Total Download (biru)
COLOR_UL = wx.Colour(20, 150, 60)      # Total Upload   (ijo)

# ---- Interface WAN
WAN_DEFAULT_ON = False       # True = langsung ON saat aplikasi dibuka
WAN_DEFAULT_ETHER = 1        # ether1
DEFAULT_WAN_INTERVAL = 5     # detik
DEFAULT_WAN_HEIGHT = 120     # px (tinggi grafik)
WAN_VGRID_SEC = 30           # garis vertikal tiap 30 detik
WAN_PING_ENABLE = True       # cek internet lewat ping dari router
WAN_PING_HOST = "8.8.8.8"    # tujuan ping untuk cek internet
WAN_DOWN_COLOR = wx.Colour(225, 40, 40)
VGRID_COLOR = wx.Colour(205, 205, 205)

# ---- warna list belang
ROW_WHITE = wx.Colour(255, 255, 255)
ROW_GREY = wx.Colour(240, 242, 245)
ROW_SELECTED = wx.Colour(190, 215, 245)

BAR_BG = wx.Colour(235, 238, 242)


# ---------------------------------------------------------------- helper
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def fmt_bps(bps):
    bps = float(bps)
    for unit in ("bps", "Kbps", "Mbps", "Gbps"):
        if bps < 1000:
            return f"{bps:.1f} {unit}"
        bps /= 1000
    return f"{bps:.1f} Tbps"


def fmt_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_limit(v):
    return "\u221e" if v <= 0 else fmt_bps(v)


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_rate(s):
    """'10M' / '512k' / '10000000' -> bps (int). 0 = unlimited."""
    m = re.match(r"^\s*([\d.]+)\s*([kmg]?)", str(s).strip().lower())
    if not m:
        return 0
    try:
        val = float(m.group(1))
    except ValueError:
        return 0
    return int(val * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2)])


def parse_limit(s):
    """max-limit 'up/down' -> (up_bps, down_bps)."""
    up, sep, down = str(s or "0").partition("/")
    if not sep:
        down = up
    return parse_rate(up), parse_rate(down)


def usage_pct(tx, rx, lim_up, lim_down):
    """Persentase pemakaian terhadap max-limit (ambil arah yang paling penuh)."""
    ratios = []
    if lim_down > 0:
        ratios.append(tx / lim_down)     # TX = download ke client
    if lim_up > 0:
        ratios.append(rx / lim_up)       # RX = upload dari client
    return max(ratios) * 100 if ratios else None


def bar_color(pct):
    if pct >= CRIT_PCT:
        return wx.Colour(225, 50, 50)     # merah
    if pct >= WARN_PCT:
        return wx.Colour(255, 160, 0)     # oranye
    return wx.Colour(60, 190, 90)         # hijau


def nice_max(v):
    """Batas atas skala grafik yang 'rapi' (1, 2, 5, 10 x 10^n)."""
    if v <= 0:
        return 1000
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


def find_wan_iface(ifaces, num):
    """Cari interface 'ether<num>'. Kalau tidak ada yang persis sama,
    ambil yang diawali ether<num> (mis. 'ether1-WAN'), tanpa nyasar ke ether10."""
    exact = f"ether{num}"
    pat = re.compile(rf"^ether{num}(?!\d)", re.I)
    first = None
    for it in ifaces:
        name = it.get("name", "")
        if name == exact:
            return it
        if first is None and pat.match(name):
            first = it
    return first


# ---------------------------------------------------------------- dialogs
class LoginDialog(wx.Dialog):
    def __init__(self, parent, cfg):
        super().__init__(parent, title="Login MikroTik", size=(340, 260))
        p = wx.Panel(self)
        grid = wx.FlexGridSizer(4, 2, 8, 8)
        grid.AddGrowableCol(1)

        self.host = wx.TextCtrl(p, value=cfg.get("host", "192.168.88.1"))
        self.user = wx.TextCtrl(p, value=cfg.get("username", "admin"))
        self.pwd = wx.TextCtrl(p, style=wx.TE_PASSWORD, value=cfg.get("password", ""))
        self.remember = wx.CheckBox(p, label="Ingat saya")
        self.remember.SetValue(cfg.get("remember", False))

        for label, ctrl in (("IP / Host", self.host), ("Username", self.user), ("Password", self.pwd)):
            grid.Add(wx.StaticText(p, label=label), 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 1, wx.EXPAND)
        grid.Add((0, 0))
        grid.Add(self.remember)

        btns = wx.StdDialogButtonSizer()
        btns.AddButton(wx.Button(p, wx.ID_OK, "Lanjut"))
        btns.AddButton(wx.Button(p, wx.ID_CANCEL, "Batal"))
        btns.Realize()

        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(grid, 1, wx.EXPAND | wx.ALL, 15)
        box.Add(btns, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        p.SetSizer(box)

    def values(self):
        return (self.host.GetValue().strip(), self.user.GetValue().strip(),
                self.pwd.GetValue(), self.remember.GetValue())


class PortDialog(wx.Dialog):
    def __init__(self, parent, default_port=8728):
        super().__init__(parent, title="Port API", size=(300, 160))
        p = wx.Panel(self)
        self.port = wx.SpinCtrl(p, min=1, max=65535, initial=int(default_port))
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(wx.StaticText(p, label="Port API"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        row.Add(self.port, 1)

        btns = wx.StdDialogButtonSizer()
        btns.AddButton(wx.Button(p, wx.ID_OK, "Konek"))
        btns.AddButton(wx.Button(p, wx.ID_CANCEL, "Batal"))
        btns.Realize()

        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(row, 0, wx.EXPAND | wx.ALL, 15)
        box.Add(btns, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        p.SetSizer(box)


# ---------------------------------------------------------------- list belang (custom draw)
class _RowBox(wx.VListBox):
    """Daftar baris yang digambar manual: belang, bar usage, seleksi per-nama."""

    def __init__(self, parent, owner):
        super().__init__(parent, style=wx.BORDER_NONE)
        self.owner = owner
        try:
            self.SetDoubleBuffered(True)
        except Exception:
            pass

    def OnMeasureItem(self, n):
        return self.owner.ROW_H

    def OnDrawBackground(self, dc, rect, n):
        o = self.owner
        if o.selected is not None and n < len(o.rows) and o.rows[n][0] == o.selected:
            color = ROW_SELECTED
        else:
            color = ROW_WHITE if n % 2 == 0 else ROW_GREY
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(color))
        dc.DrawRectangle(rect)

    def OnDrawItem(self, dc, rect, n):
        o = self.owner
        if n >= len(o.rows):
            return
        row = o.rows[n]
        widths = o.col_widths(rect.width)

        # warna & bold teks satu baris (berdasarkan usage%)
        color, bold = o.row_style(row)
        font = self.GetFont()
        if bold:
            font.SetWeight(wx.FONTWEIGHT_BOLD)
        dc.SetFont(font)

        x = rect.x
        for ci, ((title, _w, _m, kind), cw, val) in enumerate(zip(o.columns, widths, row)):
            cell = wx.Rect(x + 6, rect.y, max(cw - 10, 1), rect.height)
            if kind == "bar":
                self._draw_bar(dc, cell, val)
            else:
                cc = color
                if o.cell_style:
                    custom = o.cell_style(row, ci)     # warna khusus per-sel (opsional)
                    if custom is not None:
                        cc = custom
                dc.SetTextForeground(cc)
                text = str(val)
                tw, th = dc.GetTextExtent(text)
                dc.SetClippingRegion(cell)
                dc.DrawText(text, cell.x, rect.y + (rect.height - th) // 2)
                dc.DestroyClippingRegion()
            x += cw

    def _draw_bar(self, dc, cell, pct):
        if pct is None:                     # unlimited -> tidak ada bar
            dc.SetTextForeground(wx.Colour(140, 140, 140))
            tw, th = dc.GetTextExtent("-")
            dc.DrawText("-", cell.x, cell.y + (cell.height - th) // 2)
            return
        y, h = cell.y + 4, cell.height - 8
        dc.SetPen(wx.Pen(wx.Colour(185, 185, 185), 1))
        dc.SetBrush(wx.Brush(wx.Colour(252, 252, 252)))
        dc.DrawRectangle(cell.x, y, cell.width, h)

        fill = int((cell.width - 2) * min(pct, 100) / 100)
        if fill > 0:
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush(bar_color(pct)))
            dc.DrawRectangle(cell.x + 1, y + 1, fill, h - 2)

        text = f"{pct:.0f}%"
        tw, th = dc.GetTextExtent(text)
        dc.SetTextForeground(wx.WHITE if pct >= CRIT_PCT else wx.BLACK)
        dc.DrawText(text, cell.x + (cell.width - tw) // 2, y + (h - th) // 2)


class StripedList(wx.Panel):
    """Header + daftar belang. columns = [(judul, bobot_lebar, lebar_min, 'text'|'bar')].
    pct_index  = posisi nilai usage% di dalam tuple baris (untuk warna teks baris).
    cell_style = fungsi (row, index_kolom) -> wx.Colour / None (warna teks per-sel)."""

    ROW_H = 24
    HEAD_H = 26

    def __init__(self, parent, columns, on_activate=None, pct_index=None, cell_style=None):
        super().__init__(parent)
        self.columns = columns
        self.on_activate = on_activate
        self.pct_index = pct_index
        self.cell_style = cell_style
        self.rows = []
        self.selected = None

        self.header = wx.Panel(self, size=(-1, self.HEAD_H))
        self.header.SetMinSize((-1, self.HEAD_H))
        self.header.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.header.Bind(wx.EVT_PAINT, self.on_paint_header)
        self.header.Bind(wx.EVT_SIZE, lambda e: (self.header.Refresh(), e.Skip()))

        self.body = _RowBox(self, self)
        self.body.Bind(wx.EVT_SIZE, lambda e: (self.header.Refresh(), e.Skip()))
        self.body.Bind(wx.EVT_LISTBOX, self.on_select)
        self.body.Bind(wx.EVT_LISTBOX_DCLICK, self.on_dclick)

        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(self.header, 0, wx.EXPAND)
        box.Add(self.body, 1, wx.EXPAND)
        self.SetSizer(box)

    def col_widths(self, total):
        mins = [c[2] for c in self.columns]
        wsum = sum(c[1] for c in self.columns)
        extra = max(total - sum(mins), 0)
        return [int(m + extra * c[1] / wsum) for m, c in zip(mins, self.columns)]

    def row_style(self, row):
        """-> (warna_teks, bold) untuk satu baris."""
        if self.pct_index is not None and self.pct_index < len(row):
            pct = row[self.pct_index]
            if pct is not None:
                if pct >= ROW_RED_PCT:
                    return ROW_TEXT_RED, True
                if pct >= ROW_ORANGE_PCT:
                    return ROW_TEXT_ORANGE, True
        return wx.BLACK, False

    def set_rows(self, rows):
        if rows == self.rows:
            return
        count_changed = len(rows) != len(self.rows)
        self.rows = rows
        if count_changed:
            self.body.SetItemCount(len(rows))
        self.body.RefreshAll()

    def on_select(self, evt):
        n = evt.GetSelection()
        if 0 <= n < len(self.rows):
            self.selected = self.rows[n][0]
        self.body.RefreshAll()

    def on_dclick(self, evt):
        n = evt.GetSelection()
        if 0 <= n < len(self.rows) and self.on_activate:
            self.on_activate(self.rows[n][0])

    def on_paint_header(self, _evt):
        dc = wx.AutoBufferedPaintDC(self.header)
        w, h = self.header.GetClientSize()
        dc.SetBackground(wx.Brush(wx.Colour(222, 227, 234)))
        dc.Clear()
        font = self.GetFont()
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        dc.SetFont(font)
        dc.SetTextForeground(wx.Colour(40, 40, 40))
        widths = self.col_widths(self.body.GetClientSize().width or w)
        x = 0
        for (title, _w, _m, _k), cw in zip(self.columns, widths):
            tw, th = dc.GetTextExtent(title)
            dc.SetClippingRegion(wx.Rect(x + 6, 0, max(cw - 10, 1), h))
            dc.DrawText(title, x + 6, (h - th) // 2)
            dc.DestroyClippingRegion()
            x += cw
            dc.SetPen(wx.Pen(wx.Colour(190, 195, 202), 1))
            dc.DrawLine(x - 1, 3, x - 1, h - 3)
        dc.SetPen(wx.Pen(wx.Colour(170, 175, 182), 1))
        dc.DrawLine(0, h - 1, w, h - 1)


# ---------------------------------------------------------------- grafik bandwidth
class BwGraph(wx.Panel):
    """Grafik batang vertikal tipis & rapat (ala Winbox). 1 batang = 1 sampel data.
    Jarak antar batang TETAP (SLOT_PX); jendela lebar = lebih banyak titik terlihat,
    bukan batang yang merenggang.

    - Nilai None dalam data = 'internet mati': batang merah setinggi penuh.
    - vgrid_sec (opsional): garis vertikal abu-abu tipis tiap N detik
      (butuh `times` = timestamp tiap sampel, lihat set_data)."""

    def __init__(self, parent, title, color, vgrid_sec=None):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.title = title
        self.color = color
        self.vgrid_sec = vgrid_sec
        self.data = []
        self.times = None
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, self.on_size)

    def on_size(self, evt):
        self.Refresh()
        evt.Skip()

    def set_data(self, data, times=None):
        self.data = list(data)
        self.times = list(times) if times is not None else None
        self.Refresh()

    def on_paint(self, _evt):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.WHITE))
        dc.Clear()
        w, h = self.GetClientSize()
        left, top, right, bottom = 78, 26, 10, 10
        pw, ph = w - left - right, h - top - bottom
        if pw <= SLOT_PX or ph <= 10:
            return

        max_n = max(pw // SLOT_PX, 1)
        vis = self.data[-max_n:]                 # hanya yang muat di lebar saat ini
        vts = self.times[-max_n:] if self.times is not None else None
        nums = [v for v in vis if v is not None]
        peak = max(nums, default=0)
        vmax = nice_max(peak)

        small = self.GetFont()
        small.SetPointSize(max(7, small.GetPointSize() - 1))
        dc.SetFont(small)

        # grid + label sumbu Y
        for i in range(5):
            y = top + int(ph * i / 4)
            dc.SetPen(wx.Pen(wx.Colour(215, 215, 215), 1))
            dc.DrawLine(left, y, left + pw, y)
            dc.SetTextForeground(wx.Colour(110, 110, 110))
            label = fmt_bps(vmax * (4 - i) / 4)
            tw, th = dc.GetTextExtent(label)
            dc.DrawText(label, left - tw - 6, y - th // 2)

        base = top + ph
        x0 = left + pw - len(vis) * SLOT_PX

        # garis vertikal tipis abu-abu tiap vgrid_sec detik (mengikuti jam, ikut bergeser)
        if self.vgrid_sec and vts and len(vts) == len(vis):
            dc.SetPen(wx.Pen(VGRID_COLOR, 1))
            for i in range(1, len(vis)):
                if int(vts[i] // self.vgrid_sec) != int(vts[i - 1] // self.vgrid_sec):
                    gx = x0 + i * SLOT_PX
                    dc.DrawLine(gx, top, gx, base)

        # batang rapat, data terbaru di tepi kanan
        dc.SetPen(wx.TRANSPARENT_PEN)
        normal_brush = wx.Brush(self.color)
        down_brush = wx.Brush(WAN_DOWN_COLOR)
        for i, v in enumerate(vis):
            if v is None:                         # internet mati -> merah FULL
                dc.SetBrush(down_brush)
                dc.DrawRectangle(x0 + i * SLOT_PX, top, BAR_PX, ph)
                continue
            bar = int(ph * v / vmax)
            if v > 0:
                bar = max(bar, 1)
            if bar:
                dc.SetBrush(normal_brush)
                dc.DrawRectangle(x0 + i * SLOT_PX, base - bar, BAR_PX, bar)

        # judul + nilai terakhir + info
        cur = vis[-1] if vis else 0
        dc.SetFont(small)
        info = f"maks {fmt_bps(peak)}  |  {len(vis)} titik"
        tw, th = dc.GetTextExtent(info)
        dc.SetTextForeground(wx.Colour(110, 110, 110))
        dc.DrawText(info, left + pw - tw, 6)

        bold = self.GetFont()
        bold.SetWeight(wx.FONTWEIGHT_BOLD)
        dc.SetFont(bold)
        if cur is None:
            dc.SetTextForeground(WAN_DOWN_COLOR)
            dc.DrawText(f"{self.title}: MATI", left, 4)
        else:
            dc.SetTextForeground(self.color)
            dc.DrawText(f"{self.title}: {fmt_bps(cur)}", left, 4)


class BandwidthGraphFrame(wx.Frame):
    def __init__(self, parent, title, tx_label, rx_label):
        super().__init__(parent, title=title, size=(600, 440))
        p = wx.Panel(self)
        self.tx = BwGraph(p, tx_label, wx.Colour(30, 90, 220))
        self.rx = BwGraph(p, rx_label, wx.Colour(20, 150, 60))
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(self.tx, 1, wx.EXPAND | wx.ALL, 6)
        box.Add(self.rx, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        p.SetSizer(box)

    def refresh_data(self, tx, rx):
        self.tx.set_data(tx)
        self.rx.set_data(rx)


# ---------------------------------------------------------------- dashboard
# Urutan kolom = urutan nilai di tuple baris (lihat render_queue)
QUEUE_COLS = [
    ("Queue", 2.0, 110, "text"),
    ("Target", 2.0, 100, "text"),
    ("Max Limit (Up / Down)", 2.6, 170, "text"),
    ("TX (Download)", 1.3, 90, "text"),
    ("RX (Upload)", 1.3, 90, "text"),
    ("Total", 1.3, 90, "text"),
    ("Usage", 2.4, 130, "bar"),
    ("Total Download", 1.5, 100, "text"),
    ("Total Upload", 1.5, 100, "text"),
]
QUEUE_PCT_INDEX = 6     # posisi kolom Usage di QUEUE_COLS

# Urutan kolom = urutan nilai di tuple baris (lihat update_ui)
IF_COLS = [
    ("Interface", 2.0, 120, "text"),
    ("Type", 1.2, 90, "text"),
    ("Status", 1.0, 80, "text"),
    ("RX", 1.5, 100, "text"),
    ("TX", 1.5, 100, "text"),
    ("Total Download", 1.6, 110, "text"),
    ("Total Upload", 1.6, 110, "text"),
]


def if_cell_color(row, ci):
    """Warna teks per-sel di list interface."""
    if ci == 2:                                   # Status
        return COLOR_UP if row[2] == "JALAN" else COLOR_DOWN
    if ci == 5:                                   # Total Download
        return COLOR_DL
    if ci == 6:                                   # Total Upload
        return COLOR_UL
    return None


class Dashboard(wx.Frame):
    SORT_CHOICES = ["NAMA", "TRAFIK TX", "TRAFIK RX", "TOTAL BANDWIDTH"]

    def __init__(self, api, host, user):
        super().__init__(None, title=f"MikroTik Dashboard - {host}", size=(1150, 780))
        self.api = api
        self.user = user
        self.running = True
        self.q_interval = DEFAULT_Q_INTERVAL   # detik, dibaca oleh thread polling
        self.prev = {}            # {interface: (waktu, rx_byte, tx_byte)}
        self.q_history = {}       # {queue: {"tx": deque, "rx": deque}}
        self.if_history = {}      # {interface: {"tx": deque, "rx": deque}}
        self.graphs = {}          # {(jenis, nama): BandwidthGraphFrame}
        self.queue_rows = []      # (name, target, limit_text, tx, rx, pct, total_down, total_up)

        # ---- state Interface WAN (dibaca juga oleh thread polling)
        self.wan_on = WAN_DEFAULT_ON
        self.wan_num = WAN_DEFAULT_ETHER
        self.wan_interval = DEFAULT_WAN_INTERVAL
        self.wan_gen = 0          # naik tiap ON/OFF atau ganti ether -> poll reset hitungan
        self.wan_rx = deque(maxlen=HISTORY_MAX)   # download (RX di router); None = mati
        self.wan_tx = deque(maxlen=HISTORY_MAX)   # upload   (TX di router); None = mati
        self.wan_ts = deque(maxlen=HISTORY_MAX)   # timestamp tiap sampel (untuk garis 30 dtk)

        self.root = root = wx.Panel(self)

        # ---------- bar atas: info device + tombol [USERNAME KELUAR]
        top = wx.Panel(root)
        top.SetBackgroundColour(BAR_BG)
        bold = top.GetFont()
        bold.SetWeight(wx.FONTWEIGHT_BOLD)
        self.lbl_dev = wx.StaticText(top, label="Device : -")
        self.lbl_ram = wx.StaticText(top, label="Ram : -")
        self.lbl_use = wx.StaticText(top, label="Usage : -")
        self.lbl_cpu = wx.StaticText(top, label="CPU : -")
        for lb in (self.lbl_dev, self.lbl_ram, self.lbl_use, self.lbl_cpu):
            lb.SetFont(bold)

        self.btn_exit = GenButton(top, label=f"{(user or 'USER').upper()} KELUAR", size=(-1, 34))
        self.btn_exit.SetMinSize((130, 34))
        self.btn_exit.SetBackgroundColour(wx.Colour(200, 30, 30))
        self.btn_exit.SetForegroundColour(wx.WHITE)
        self.btn_exit.SetFont(bold)
        self.btn_exit.Bind(wx.EVT_BUTTON, lambda e: self.Close())

        trow = wx.BoxSizer(wx.HORIZONTAL)
        for lb in (self.lbl_dev, self.lbl_ram, self.lbl_use, self.lbl_cpu):
            trow.Add(lb, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 28)
        trow.AddStretchSpacer(1)
        trow.Add(self.btn_exit, 0, wx.ALIGN_CENTER_VERTICAL)
        tbox = wx.BoxSizer(wx.VERTICAL)
        tbox.Add(trow, 1, wx.EXPAND | wx.ALL, 10)
        top.SetSizer(tbox)

        # ---------- splitter: Queue (atas 70%) | Interface (bawah 30%)
        self.splitter = wx.SplitterWindow(root, style=wx.SP_LIVE_UPDATE | wx.SP_3D)
        self.splitter.SetMinimumPaneSize(80)
        self.splitter.SetSashGravity(0.7)

        q_panel = wx.Panel(self.splitter)
        self.sort_box = wx.RadioBox(q_panel, label="Sort by", choices=self.SORT_CHOICES,
                                    majorDimension=1, style=wx.RA_SPECIFY_ROWS)
        self.sort_box.Bind(wx.EVT_RADIOBOX, lambda e: self.render_queue())

        # input refresh rate queue (detik)
        self.spin_q = wx.SpinCtrl(q_panel, min=1, max=3600, initial=DEFAULT_Q_INTERVAL,
                                  size=(70, -1))
        self.spin_q.Bind(wx.EVT_SPINCTRL, self.on_q_interval)
        self.spin_q.Bind(wx.EVT_TEXT, self.on_q_interval)

        srow = wx.BoxSizer(wx.HORIZONTAL)
        srow.Add(self.sort_box, 0, wx.RIGHT, 20)
        srow.Add(wx.StaticText(q_panel, label="Refresh queue tiap"), 0,
                 wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        srow.Add(self.spin_q, 0, wx.ALIGN_CENTER_VERTICAL)
        srow.Add(wx.StaticText(q_panel, label="detik"), 0,
                 wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)

        self.lst_q = StripedList(q_panel, QUEUE_COLS,
                                 on_activate=lambda n: self.open_graph("queue", n),
                                 pct_index=QUEUE_PCT_INDEX)
        qbox = wx.BoxSizer(wx.VERTICAL)
        qbox.Add(srow, 0, wx.EXPAND | wx.ALL, 4)
        qbox.Add(self.lst_q, 1, wx.EXPAND)
        q_panel.SetSizer(qbox)

        if_panel = wx.Panel(self.splitter)
        self.lst_if = StripedList(if_panel, IF_COLS,
                                  on_activate=lambda n: self.open_graph("interface", n),
                                  cell_style=if_cell_color)
        ibox = wx.BoxSizer(wx.VERTICAL)
        ibox.Add(wx.StaticText(if_panel, label=" Interface"), 0, wx.TOP | wx.BOTTOM, 4)
        ibox.Add(self.lst_if, 1, wx.EXPAND)
        if_panel.SetSizer(ibox)

        self.splitter.SplitHorizontally(q_panel, if_panel, 400)

        # ---------- baris 4: Interface WAN (hanya grafik)
        self.wan_panel = wx.Panel(root)
        self.wan_panel.SetBackgroundColour(BAR_BG)

        wctrl = wx.Panel(self.wan_panel)
        wctrl.SetBackgroundColour(BAR_BG)

        wtitle = wx.StaticText(wctrl, label="Interface WAN")
        wtitle.SetFont(bold)

        self.spin_wan_n = wx.SpinCtrl(wctrl, min=1, max=99, initial=WAN_DEFAULT_ETHER,
                                      size=(60, -1))
        self.spin_wan_n.Bind(wx.EVT_SPINCTRL, self.on_wan_ether)
        self.spin_wan_n.Bind(wx.EVT_TEXT, self.on_wan_ether)

        self.btn_wan = GenButton(wctrl, label="OFF", size=(70, 28))
        self.btn_wan.SetFont(bold)
        self.btn_wan.Bind(wx.EVT_BUTTON, self.on_wan_toggle)

        self.spin_wan_i = wx.SpinCtrl(wctrl, min=1, max=3600, initial=DEFAULT_WAN_INTERVAL,
                                      size=(70, -1))
        self.spin_wan_i.Bind(wx.EVT_SPINCTRL, self.on_wan_interval)
        self.spin_wan_i.Bind(wx.EVT_TEXT, self.on_wan_interval)

        self.spin_wan_h = wx.SpinCtrl(wctrl, min=60, max=600, initial=DEFAULT_WAN_HEIGHT,
                                      size=(70, -1))
        self.spin_wan_h.Bind(wx.EVT_SPINCTRL, self.on_wan_height)
        self.spin_wan_h.Bind(wx.EVT_TEXT, self.on_wan_height)

        self.lbl_wan = wx.StaticText(wctrl, label="", size=(290, -1))

        wrow = wx.BoxSizer(wx.HORIZONTAL)
        wrow.Add(wtitle, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 18)
        wrow.Add(wx.StaticText(wctrl, label="ether ["), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        wrow.Add(self.spin_wan_n, 0, wx.ALIGN_CENTER_VERTICAL)
        wrow.Add(wx.StaticText(wctrl, label="] wan"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 4)
        wrow.Add(self.btn_wan, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 14)
        wrow.Add(wx.StaticText(wctrl, label="Refresh tiap"), 0,
                 wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 18)
        wrow.Add(self.spin_wan_i, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        wrow.Add(wx.StaticText(wctrl, label="detik"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        wrow.Add(wx.StaticText(wctrl, label="Tinggi grafik"), 0,
                 wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 18)
        wrow.Add(self.spin_wan_h, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        wrow.Add(wx.StaticText(wctrl, label="px"), 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 6)
        wrow.Add(self.lbl_wan, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 18)
        wcbox = wx.BoxSizer(wx.VERTICAL)
        wcbox.Add(wrow, 0, wx.EXPAND | wx.ALL, 6)
        wctrl.SetSizer(wcbox)

        # panel grafik (disembunyikan saat OFF)
        self.wan_gpanel = wx.Panel(self.wan_panel)
        self.wan_gpanel.SetBackgroundColour(BAR_BG)
        self.wan_dl = BwGraph(self.wan_gpanel, "RX (Download)", wx.Colour(30, 90, 220),
                              vgrid_sec=WAN_VGRID_SEC)
        self.wan_ul = BwGraph(self.wan_gpanel, "TX (Upload)", wx.Colour(20, 150, 60),
                              vgrid_sec=WAN_VGRID_SEC)
        gbox = wx.BoxSizer(wx.HORIZONTAL)
        gbox.Add(self.wan_dl, 1, wx.EXPAND | wx.ALL, 6)
        gbox.Add(self.wan_ul, 1, wx.EXPAND | wx.TOP | wx.RIGHT | wx.BOTTOM, 6)
        self.wan_gpanel.SetSizer(gbox)

        wbox = wx.BoxSizer(wx.VERTICAL)
        wbox.Add(wctrl, 0, wx.EXPAND)
        wbox.Add(self.wan_gpanel, 0, wx.EXPAND)
        self.wan_panel.SetSizer(wbox)

        rbox = wx.BoxSizer(wx.VERTICAL)
        rbox.Add(top, 0, wx.EXPAND)
        rbox.Add(self.splitter, 1, wx.EXPAND)
        rbox.Add(self.wan_panel, 0, wx.EXPAND)
        root.SetSizer(rbox)

        self.style_wan_button()
        self.update_wan_view()

        self.CreateStatusBar()
        self.Bind(wx.EVT_CLOSE, self.on_close)

        wx.CallAfter(self.set_split)
        threading.Thread(target=self.poll, daemon=True).start()

    def set_split(self):
        h = self.splitter.GetClientSize().height
        self.splitter.SetSashPosition(int(h * 0.7))   # queue 70% / interface 30%

    def on_q_interval(self, _evt):
        self.q_interval = max(1, self.spin_q.GetValue())

    # ------------------------------------------------ Interface WAN: kontrol
    def style_wan_button(self):
        if self.wan_on:
            self.btn_wan.SetLabel("ON")
            self.btn_wan.SetBackgroundColour(wx.Colour(40, 160, 70))
        else:
            self.btn_wan.SetLabel("OFF")
            self.btn_wan.SetBackgroundColour(wx.Colour(150, 150, 150))
        self.btn_wan.SetForegroundColour(wx.WHITE)
        self.btn_wan.Refresh()

    def set_wan_label(self, text, down=False):
        self.lbl_wan.SetForegroundColour(WAN_DOWN_COLOR if down else wx.BLACK)
        self.lbl_wan.SetLabel(text)
        self.lbl_wan.Refresh()

    def update_wan_view(self):
        """Tampilkan/sembunyikan grafik WAN & atur tingginya."""
        h = self.spin_wan_h.GetValue()
        self.wan_gpanel.SetMinSize((-1, h + 12))
        self.wan_gpanel.Show(self.wan_on)
        self.wan_gpanel.InvalidateBestSize()
        self.wan_panel.InvalidateBestSize()
        self.root.Layout()
        self.wan_panel.Layout()

    def wan_clear(self):
        self.wan_rx.clear()
        self.wan_tx.clear()
        self.wan_ts.clear()
        self.wan_dl.set_data([], [])
        self.wan_ul.set_data([], [])

    def on_wan_toggle(self, _evt):
        self.wan_on = not self.wan_on
        self.wan_clear()
        self.wan_gen += 1
        self.style_wan_button()
        self.set_wan_label(f"Menunggu data ether{self.wan_num}..." if self.wan_on else "")
        self.update_wan_view()

    def on_wan_ether(self, _evt):
        n = self.spin_wan_n.GetValue()
        if n == self.wan_num:
            return
        self.wan_num = n
        self.wan_clear()
        self.wan_gen += 1
        if self.wan_on:
            self.set_wan_label(f"Menunggu data ether{n}...")

    def on_wan_interval(self, _evt):
        self.wan_interval = max(1, self.spin_wan_i.GetValue())

    def on_wan_height(self, _evt):
        self.update_wan_view()

    # ------------------------------------------------ cek internet (dipanggil dari thread polling)
    def check_internet(self):
        """True = internet nyambung, False = tidak konek, None = tidak bisa dicek.
        Ping dilakukan oleh router (/ping) ke WAN_PING_HOST sebanyak 1x."""
        if not WAN_PING_ENABLE:
            return None
        try:
            rows = list(self.api("/ping", address=WAN_PING_HOST, count="1"))
        except Exception:
            return None
        received = max((to_int(r.get("received")) for r in rows), default=0)
        return received > 0

    # ------------------------------------------------ thread polling
    def poll(self):
        last_q = 0.0
        last_wan = 0.0
        wan_prev = None            # (waktu, rx_byte, tx_byte)
        wan_gen_seen = self.wan_gen
        while self.running:
            try:
                res = list(self.api.path("system", "resource"))
                ifaces = list(self.api.path("interface"))
                now = time.time()

                sysinfo = {}
                if res:
                    r = res[0]
                    total = to_int(r.get("total-memory"))
                    free = to_int(r.get("free-memory"))
                    used = max(total - free, 0)
                    sysinfo = {
                        "board": r.get("board-name", "-"),
                        "used": used / 1048576,
                        "total": total / 1048576,
                        "pct": (used / total * 100) if total else 0,
                        "cpu": to_int(r.get("cpu-load")),
                    }

                # (nama, type, status, rx_bps, tx_bps, rx_byte_total, tx_byte_total)
                if_rows = []
                for it in ifaces:
                    name = it.get("name", "")
                    rx, tx = to_int(it.get("rx-byte")), to_int(it.get("tx-byte"))
                    rx_bps = tx_bps = 0
                    if name in self.prev:
                        t0, rx0, tx0 = self.prev[name]
                        dt = max(now - t0, 0.001)
                        rx_bps = max(rx - rx0, 0) * 8 / dt
                        tx_bps = max(tx - tx0, 0) * 8 / dt
                    self.prev[name] = (now, rx, tx)
                    if_rows.append((name, it.get("type", ""),
                                    "running" if it.get("running") else "down",
                                    rx_bps, tx_bps, rx, tx))

                # queue: hanya diambil kalau sudah waktunya (None = tidak di-update)
                q_rows = None
                if now - last_q >= self.q_interval:
                    queues = list(self.api.path("queue", "simple"))
                    last_q = now
                    q_rows = []
                    for q in queues:
                        # rate = "upload/download" dari sudut pandang client.
                        # Sudut pandang router: TX = download ke client, RX = upload dari client.
                        up, _, down = str(q.get("rate", "0/0")).partition("/")
                        rx_bps, tx_bps = to_int(up), to_int(down)
                        lim_up, lim_down = parse_limit(q.get("max-limit"))
                        # bytes = "upload/download" (kumulatif, sudut pandang client)
                        b_up, _, b_down = str(q.get("bytes", "0/0")).partition("/")
                        q_rows.append((q.get("name", ""), q.get("target", ""),
                                       f"{fmt_limit(lim_up)} / {fmt_limit(lim_down)}",
                                       tx_bps, rx_bps,
                                       usage_pct(tx_bps, rx_bps, lim_up, lim_down),
                                       to_int(b_down), to_int(b_up)))

                # Interface WAN: pakai data interface yang sudah diambil (tanpa request tambahan)
                # wan_sample: None
                #           | ("ok", nama, rx_bps, tx_bps)
                #           | ("down", nama, alasan)      -> internet / link mati
                #           | ("miss", nama_dicari)       -> interface tidak ketemu
                wan_sample = None
                if self.wan_on:
                    if self.wan_gen != wan_gen_seen:
                        wan_gen_seen = self.wan_gen
                        wan_prev = None
                    if wan_prev is None or now - last_wan >= self.wan_interval:
                        it = find_wan_iface(ifaces, self.wan_num)
                        if it is None:
                            wan_sample = ("miss", f"ether{self.wan_num}")
                            wan_prev = None
                        else:
                            name = it.get("name", "")
                            rx, tx = to_int(it.get("rx-byte")), to_int(it.get("tx-byte"))

                            reason = None
                            if not it.get("running"):
                                reason = "link putus"
                            elif self.check_internet() is False:
                                reason = f"ping {WAN_PING_HOST} gagal"

                            if reason:
                                wan_sample = ("down", name, reason)
                            elif wan_prev is not None:
                                t0, rx0, tx0 = wan_prev
                                dt = max(now - t0, 0.001)
                                wan_sample = ("ok", name,
                                              max(rx - rx0, 0) * 8 / dt,
                                              max(tx - tx0, 0) * 8 / dt)
                            wan_prev = (now, rx, tx)
                            last_wan = now
                else:
                    wan_gen_seen = self.wan_gen
                    wan_prev = None

                wx.CallAfter(self.update_ui, sysinfo, if_rows, q_rows, wan_sample)
            except Exception as e:
                wx.CallAfter(self.SetStatusText, f"Error: {e}")
            time.sleep(1)

    # ------------------------------------------------ update UI (thread utama)
    @staticmethod
    def _push(store, name, tx, rx):
        h = store.setdefault(name, {"tx": deque(maxlen=HISTORY_MAX),
                                    "rx": deque(maxlen=HISTORY_MAX)})
        h["tx"].append(tx)
        h["rx"].append(rx)

    def update_ui(self, sysinfo, if_rows, q_rows, wan_sample=None):
        if not self.running:
            return

        if sysinfo:
            self.lbl_dev.SetLabel(f"Device : {sysinfo['board']}")
            self.lbl_ram.SetLabel(f"Ram : {sysinfo['used']:.1f} / {sysinfo['total']:.1f} MB")
            self.lbl_use.SetLabel(f"Usage : {sysinfo['pct']:.1f}%")
            self.lbl_cpu.SetLabel(f"CPU : {sysinfo['cpu']}%")
            self.lbl_dev.GetParent().Layout()

        # ---- interface
        for name, _t, _s, rx, tx, _rb, _tb in if_rows:
            self._push(self.if_history, name, tx, rx)
        if_names = {r[0] for r in if_rows}
        for gone in [n for n in self.if_history if n not in if_names]:
            del self.if_history[gone]
        self.lst_if.set_rows([
            (n, t, "JALAN" if s == "running" else "MATI",
             fmt_bps(rx), fmt_bps(tx), fmt_bytes(rb), fmt_bytes(tb))
            for n, t, s, rx, tx, rb, tb in if_rows
        ])

        # ---- queue (hanya kalau ada data baru sesuai interval)
        if q_rows is not None:
            for r in q_rows:
                self._push(self.q_history, r[0], r[3], r[4])
            q_names = {r[0] for r in q_rows}
            for gone in [n for n in self.q_history if n not in q_names]:
                del self.q_history[gone]
            self.queue_rows = q_rows
            self.render_queue()

        # ---- Interface WAN
        if wan_sample is not None and self.wan_on:
            kind = wan_sample[0]
            if kind == "miss":
                self.set_wan_label(f"{wan_sample[1]} tidak ditemukan", down=True)
            else:
                if kind == "ok":
                    rx_v, tx_v = wan_sample[2], wan_sample[3]
                    self.set_wan_label(f"Menampilkan: {wan_sample[1]}")
                else:                                  # "down" -> merah FULL
                    rx_v = tx_v = None
                    self.set_wan_label(f"{wan_sample[1]} MATI - {wan_sample[2]}", down=True)
                self.wan_rx.append(rx_v)
                self.wan_tx.append(tx_v)
                self.wan_ts.append(time.time())
                self.wan_dl.set_data(self.wan_rx, self.wan_ts)
                self.wan_ul.set_data(self.wan_tx, self.wan_ts)

        # ---- grafik yang sedang terbuka
        for (kind, name), win in self.graphs.items():
            h = self._history(kind).get(name)
            if h:
                win.refresh_data(h["tx"], h["rx"])

        self.SetStatusText(f"Update: {time.strftime('%H:%M:%S')}  |  "
                           f"Queue refresh tiap {self.q_interval} dtk  |  "
                           f"Double-click queue / interface untuk grafik")

    def render_queue(self):
        mode = self.sort_box.GetSelection()
        rows = list(self.queue_rows)
        if mode == 0:      # NAMA
            rows.sort(key=lambda r: r[0].lower())
        elif mode == 1:    # TRAFIK TX
            rows.sort(key=lambda r: r[3], reverse=True)
        elif mode == 2:    # TRAFIK RX
            rows.sort(key=lambda r: r[4], reverse=True)
        else:              # TOTAL BANDWIDTH
            rows.sort(key=lambda r: r[3] + r[4], reverse=True)

        # urutan harus sama dengan QUEUE_COLS
        self.lst_q.set_rows([
            (n, t, lim, fmt_bps(tx), fmt_bps(rx), fmt_bps(tx + rx), pct,
             fmt_bytes(tdown), fmt_bytes(tup))
            for n, t, lim, tx, rx, pct, tdown, tup in rows
        ])

    # ------------------------------------------------ double-click -> grafik
    def _history(self, kind):
        return self.q_history if kind == "queue" else self.if_history

    def open_graph(self, kind, name):
        key = (kind, name)
        if key in self.graphs:
            self.graphs[key].Raise()
            return
        if kind == "queue":
            title, txl, rxl = f"Queue <{name}>", "TX (Download)", "RX (Upload)"
        else:
            title, txl, rxl = f"Interface <{name}>", "TX", "RX"
        win = BandwidthGraphFrame(self, title, txl, rxl)
        win.Bind(wx.EVT_CLOSE, lambda e, k=key, w=win: self.on_graph_close(k, w))
        h = self._history(kind).get(name)
        if h:
            win.refresh_data(h["tx"], h["rx"])
        self.graphs[key] = win
        win.Show()

    def on_graph_close(self, key, win):
        self.graphs.pop(key, None)
        win.Destroy()

    def on_close(self, _evt):
        self.running = False
        for w in list(self.graphs.values()):
            w.Destroy()
        self.graphs.clear()
        try:
            self.api.close()
        except Exception:
            pass
        self.Destroy()


# ---------------------------------------------------------------- main
def main():
    app = wx.App()
    cfg = load_config()
    if cfg.get("remember") and keyring and not cfg.get("password"):
        try:
            cfg["password"] = keyring.get_password(APP_NAME, cfg.get("username", "")) or ""
        except Exception:
            pass

    while True:
        # 1) Login
        dlg = LoginDialog(None, cfg)
        if dlg.ShowModal() != wx.ID_OK:
            return
        host, user, pwd, remember = dlg.values()
        dlg.Destroy()

        # 2) Port API
        pdlg = PortDialog(None, cfg.get("port", 8728))
        if pdlg.ShowModal() != wx.ID_OK:
            return
        port = pdlg.port.GetValue()
        pdlg.Destroy()

        # 3) Konek
        try:
            api = connect(host=host, username=user, password=pwd, port=port, timeout=8)
        except Exception as e:
            wx.MessageBox(f"Gagal konek:\n{e}", "Error", wx.ICON_ERROR)
            cfg.update(host=host, username=user, password=pwd, port=port)
            continue

        # simpan "ingat saya"
        data = {"host": host, "username": user, "port": port, "remember": remember}
        if remember:
            if keyring:
                try:
                    keyring.set_password(APP_NAME, user, pwd)
                except Exception:
                    pass
            # tanpa keyring, password TIDAK disimpan (lebih aman)
        else:
            if keyring:
                try:
                    keyring.delete_password(APP_NAME, user)
                except Exception:
                    pass
        save_config(data)

        Dashboard(api, host, user).Show()
        break

    app.MainLoop()


if __name__ == "__main__":
    main()
