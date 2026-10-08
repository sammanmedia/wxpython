"""
MikroTik Dashboard v3 (wxPython + librouteros)

Install:
    pip install wxPython librouteros keyring

Alur:
    1. Dialog Login : host, username, password, [x] Ingat saya
    2. Dialog Port  : port API (default 8728) -> baru konek
    3. Dashboard    : info device + tombol KELUAR
                      Queue (atas 70%) | Interface (bawah 30%), sash bisa digeser
                      - Queue    : sort, bar usage (hijau/oranye/merah), double-click = grafik
                      - Interface: double-click = grafik
                      - List belang abu-abu tipis & putih
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
HISTORY_MAX = 900      # riwayat disimpan (detik) -> 15 menit
BAR_PX = 2             # lebar 1 batang (px)
SLOT_PX = 3            # jarak antar batang (px); rapat & TETAP walau di-maximize

# ---- pengaturan bar usage queue
WARN_PCT = 50          # >= 50% oranye
CRIT_PCT = 90          # >= 90% merah

# ---- warna list belang
ROW_WHITE = wx.Colour(255, 255, 255)
ROW_GREY = wx.Colour(240, 242, 245)
ROW_SELECTED = wx.Colour(190, 215, 245)


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
        dc.SetFont(self.GetFont())
        x = rect.x
        for (title, _w, _m, kind), cw, val in zip(o.columns, widths, row):
            cell = wx.Rect(x + 6, rect.y, max(cw - 10, 1), rect.height)
            if kind == "bar":
                self._draw_bar(dc, cell, val)
            else:
                dc.SetTextForeground(wx.BLACK)
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
    """Header + daftar belang. columns = [(judul, bobot_lebar, lebar_min, 'text'|'bar')]."""

    ROW_H = 24
    HEAD_H = 26

    def __init__(self, parent, columns, on_activate=None):
        super().__init__(parent)
        self.columns = columns
        self.on_activate = on_activate
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
    """Grafik batang vertikal tipis & rapat (ala Winbox). 1 batang = 1 detik.
    Jarak antar batang TETAP (SLOT_PX); jendela lebar = lebih banyak detik terlihat,
    bukan batang yang merenggang."""

    def __init__(self, parent, title, color):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.title = title
        self.color = color
        self.data = []
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, self.on_size)

    def on_size(self, evt):
        self.Refresh()
        evt.Skip()

    def set_data(self, data):
        self.data = list(data)
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
        peak = max(vis, default=0)
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

        # batang rapat, data terbaru di tepi kanan
        base = top + ph
        x0 = left + pw - len(vis) * SLOT_PX
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(self.color))
        for i, v in enumerate(vis):
            bar = int(ph * v / vmax)
            if v > 0:
                bar = max(bar, 1)
            if bar:
                dc.DrawRectangle(x0 + i * SLOT_PX, base - bar, BAR_PX, bar)

        # judul + nilai terakhir + info
        cur = vis[-1] if vis else 0
        dc.SetFont(small)
        info = f"maks {fmt_bps(peak)}  |  {len(vis)} dtk"
        tw, th = dc.GetTextExtent(info)
        dc.SetTextForeground(wx.Colour(110, 110, 110))
        dc.DrawText(info, left + pw - tw, 6)

        bold = self.GetFont()
        bold.SetWeight(wx.FONTWEIGHT_BOLD)
        dc.SetFont(bold)
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
QUEUE_COLS = [
    ("Queue", 2.0, 110, "text"),
    ("Target", 2.0, 100, "text"),
    ("Max Limit (Up / Down)", 2.6, 190, "text"),
    ("TX (Download)", 1.3, 100, "text"),
    ("RX (Upload)", 1.3, 100, "text"),
    ("Total", 1.3, 100, "text"),
    ("Usage", 2.4, 140, "bar"),
]
IF_COLS = [
    ("Interface", 2.0, 120, "text"),
    ("Type", 1.2, 90, "text"),
    ("Status", 1.0, 80, "text"),
    ("RX", 1.5, 110, "text"),
    ("TX", 1.5, 110, "text"),
]


class Dashboard(wx.Frame):
    SORT_CHOICES = ["NAMA", "TRAFIK TX", "TRAFIK RX", "TOTAL BANDWIDTH"]

    def __init__(self, api, host):
        super().__init__(None, title=f"MikroTik Dashboard - {host}", size=(1020, 700))
        self.api = api
        self.running = True
        self.prev = {}            # {interface: (waktu, rx_byte, tx_byte)}
        self.q_history = {}       # {queue: {"tx": deque, "rx": deque}}
        self.if_history = {}      # {interface: {"tx": deque, "rx": deque}}
        self.graphs = {}          # {(jenis, nama): BandwidthGraphFrame}
        self.queue_rows = []      # (name, target, limit_text, tx, rx, pct)

        root = wx.Panel(self)

        # ---------- bar atas: info device + tombol KELUAR
        top = wx.Panel(root)
        top.SetBackgroundColour(wx.Colour(235, 238, 242))
        bold = top.GetFont()
        bold.SetWeight(wx.FONTWEIGHT_BOLD)
        self.lbl_dev = wx.StaticText(top, label="Device : -")
        self.lbl_ram = wx.StaticText(top, label="Ram : -")
        self.lbl_use = wx.StaticText(top, label="Usage : -")
        self.lbl_cpu = wx.StaticText(top, label="CPU : -")
        for lb in (self.lbl_dev, self.lbl_ram, self.lbl_use, self.lbl_cpu):
            lb.SetFont(bold)

        self.btn_exit = GenButton(top, label="KELUAR", size=(100, 34))
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
        self.lst_q = StripedList(q_panel, QUEUE_COLS,
                                 on_activate=lambda n: self.open_graph("queue", n))
        qbox = wx.BoxSizer(wx.VERTICAL)
        qbox.Add(self.sort_box, 0, wx.EXPAND | wx.ALL, 4)
        qbox.Add(self.lst_q, 1, wx.EXPAND)
        q_panel.SetSizer(qbox)

        if_panel = wx.Panel(self.splitter)
        self.lst_if = StripedList(if_panel, IF_COLS,
                                  on_activate=lambda n: self.open_graph("interface", n))
        ibox = wx.BoxSizer(wx.VERTICAL)
        ibox.Add(wx.StaticText(if_panel, label=" Interface"), 0, wx.TOP | wx.BOTTOM, 4)
        ibox.Add(self.lst_if, 1, wx.EXPAND)
        if_panel.SetSizer(ibox)

        self.splitter.SplitHorizontally(q_panel, if_panel, 400)

        rbox = wx.BoxSizer(wx.VERTICAL)
        rbox.Add(top, 0, wx.EXPAND)
        rbox.Add(self.splitter, 1, wx.EXPAND)
        root.SetSizer(rbox)

        self.CreateStatusBar()
        self.Bind(wx.EVT_CLOSE, self.on_close)

        wx.CallAfter(self.set_split)
        threading.Thread(target=self.poll, daemon=True).start()

    def set_split(self):
        h = self.splitter.GetClientSize().height
        self.splitter.SetSashPosition(int(h * 0.7))   # queue 70% / interface 30%

    # ------------------------------------------------ thread polling
    def poll(self):
        while self.running:
            try:
                res = list(self.api.path("system", "resource"))
                ifaces = list(self.api.path("interface"))
                queues = list(self.api.path("queue", "simple"))
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
                                    rx_bps, tx_bps))

                q_rows = []
                for q in queues:
                    # rate = "upload/download" dari sudut pandang client.
                    # Sudut pandang router: TX = download ke client, RX = upload dari client.
                    up, _, down = str(q.get("rate", "0/0")).partition("/")
                    rx_bps, tx_bps = to_int(up), to_int(down)
                    lim_up, lim_down = parse_limit(q.get("max-limit"))
                    q_rows.append((q.get("name", ""), q.get("target", ""),
                                   f"{fmt_limit(lim_up)} / {fmt_limit(lim_down)}",
                                   tx_bps, rx_bps,
                                   usage_pct(tx_bps, rx_bps, lim_up, lim_down)))

                wx.CallAfter(self.update_ui, sysinfo, if_rows, q_rows)
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

    def update_ui(self, sysinfo, if_rows, q_rows):
        if not self.running:
            return

        if sysinfo:
            self.lbl_dev.SetLabel(f"Device : {sysinfo['board']}")
            self.lbl_ram.SetLabel(f"Ram : {sysinfo['used']:.1f} / {sysinfo['total']:.1f} MB")
            self.lbl_use.SetLabel(f"Usage : {sysinfo['pct']:.1f}%")
            self.lbl_cpu.SetLabel(f"CPU : {sysinfo['cpu']}%")
            self.lbl_dev.GetParent().Layout()

        # ---- interface
        for name, _t, _s, rx, tx in if_rows:
            self._push(self.if_history, name, tx, rx)
        for gone in [n for n in self.if_history if n not in {r[0] for r in if_rows}]:
            del self.if_history[gone]
        self.lst_if.set_rows([(n, t, s, fmt_bps(rx), fmt_bps(tx))
                              for n, t, s, rx, tx in if_rows])

        # ---- queue
        for name, _t, _l, tx, rx, _p in q_rows:
            self._push(self.q_history, name, tx, rx)
        for gone in [n for n in self.q_history if n not in {r[0] for r in q_rows}]:
            del self.q_history[gone]
        self.queue_rows = q_rows
        self.render_queue()

        # ---- grafik yang sedang terbuka
        for (kind, name), win in self.graphs.items():
            h = self._history(kind).get(name)
            if h:
                win.refresh_data(h["tx"], h["rx"])

        self.SetStatusText(f"Update: {time.strftime('%H:%M:%S')}  |  Double-click queue / interface untuk grafik")

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

        self.lst_q.set_rows([(n, t, lim, fmt_bps(tx), fmt_bps(rx), fmt_bps(tx + rx), pct)
                             for n, t, lim, tx, rx, pct in rows])

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

        Dashboard(api, host).Show()
        break

    app.MainLoop()


if __name__ == "__main__":
    main()
