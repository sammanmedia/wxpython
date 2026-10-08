"""
MikroTik Dashboard v2 (wxPython + librouteros)

Install:
    pip install wxPython librouteros keyring

Alur:
    1. Dialog Login : host, username, password, [x] Ingat saya
    2. Dialog Port  : port API (default 8728) -> baru konek
    3. Dashboard    : info device + tombol KELUAR, Queue (atas 70%),
                      Interface (bawah 30%), pembatas bisa digeser.
                      Double-click queue -> grafik bandwidth 30 detik.
"""
import json
import math
import os
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
HISTORY_SECONDS = 30


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


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


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


# ---------------------------------------------------------------- grafik bandwidth
class BwGraph(wx.Panel):
    """Grafik batang vertikal tipis ala Winbox, 1 garis = 1 detik."""

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
        if pw <= 10 or ph <= 10:
            return

        peak = max(self.data, default=0)
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

        # garis vertikal tipis, data terbaru di kanan
        slot = pw / HISTORY_SECONDS
        n = len(self.data)
        dc.SetPen(wx.Pen(self.color, 2))
        base = top + ph
        for idx, v in enumerate(self.data):
            si = HISTORY_SECONDS - n + idx
            x = int(left + slot * (si + 0.5))
            bar = int(ph * v / vmax)
            dc.DrawLine(x, base, x, base - max(bar, 1))

        # judul + nilai terakhir
        cur = self.data[-1] if self.data else 0
        bold = self.GetFont()
        bold.SetWeight(wx.FONTWEIGHT_BOLD)
        dc.SetFont(bold)
        dc.SetTextForeground(self.color)
        dc.DrawText(f"{self.title}: {fmt_bps(cur)}", left, 4)


class QueueGraphFrame(wx.Frame):
    def __init__(self, parent, name):
        super().__init__(parent, title=f"Queue <{name}>", size=(580, 440))
        p = wx.Panel(self)
        self.tx = BwGraph(p, "TX (Download)", wx.Colour(30, 90, 220))
        self.rx = BwGraph(p, "RX (Upload)", wx.Colour(20, 150, 60))
        box = wx.BoxSizer(wx.VERTICAL)
        box.Add(self.tx, 1, wx.EXPAND | wx.ALL, 6)
        box.Add(self.rx, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        p.SetSizer(box)

    def refresh_data(self, tx, rx):
        self.tx.set_data(tx)
        self.rx.set_data(rx)


# ---------------------------------------------------------------- dashboard
class Dashboard(wx.Frame):
    SORT_CHOICES = ["NAMA", "TRAFIK TX", "TRAFIK RX", "TOTAL BANDWIDTH"]

    def __init__(self, api, host):
        super().__init__(None, title=f"MikroTik Dashboard - {host}", size=(980, 680))
        self.api = api
        self.running = True
        self.prev = {}            # {interface: (waktu, rx_byte, tx_byte)}
        self.history = {}         # {queue: {"tx": deque, "rx": deque}}
        self.graphs = {}          # {queue: QueueGraphFrame}

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
        self.lc_q = wx.ListCtrl(q_panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, (t, w) in enumerate([("Queue", 170), ("Target", 150), ("Max Limit", 130),
                                    ("TX (Download)", 130), ("RX (Upload)", 130),
                                    ("Total", 130)]):
            self.lc_q.InsertColumn(i, t, width=w)
        self.lc_q.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_queue_dclick)
        qbox = wx.BoxSizer(wx.VERTICAL)
        qbox.Add(self.sort_box, 0, wx.EXPAND | wx.ALL, 4)
        qbox.Add(self.lc_q, 1, wx.EXPAND)
        q_panel.SetSizer(qbox)

        if_panel = wx.Panel(self.splitter)
        self.lc_if = wx.ListCtrl(if_panel, style=wx.LC_REPORT)
        for i, (t, w) in enumerate([("Interface", 170), ("Type", 110), ("Status", 90),
                                    ("RX", 150), ("TX", 150)]):
            self.lc_if.InsertColumn(i, t, width=w)
        ibox = wx.BoxSizer(wx.VERTICAL)
        ibox.Add(wx.StaticText(if_panel, label=" Interface"), 0, wx.TOP | wx.BOTTOM, 4)
        ibox.Add(self.lc_if, 1, wx.EXPAND)
        if_panel.SetSizer(ibox)

        self.splitter.SplitHorizontally(q_panel, if_panel, 400)

        rbox = wx.BoxSizer(wx.VERTICAL)
        rbox.Add(top, 0, wx.EXPAND)
        rbox.Add(self.splitter, 1, wx.EXPAND)
        root.SetSizer(rbox)

        self.queue_rows = []      # data terakhir: (name, target, limit, tx, rx)
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
                                    fmt_bps(rx_bps), fmt_bps(tx_bps)))

                q_rows = []
                for q in queues:
                    # rate = "upload/download" dari sudut pandang client.
                    # Sudut pandang router: TX = download ke client, RX = upload dari client.
                    up, _, down = str(q.get("rate", "0/0")).partition("/")
                    q_rows.append((q.get("name", ""), q.get("target", ""),
                                   q.get("max-limit", ""),
                                   to_int(down), to_int(up)))   # (.., tx, rx)

                wx.CallAfter(self.update_ui, sysinfo, if_rows, q_rows)
            except Exception as e:
                wx.CallAfter(self.SetStatusText, f"Error: {e}")
            time.sleep(1)

    # ------------------------------------------------ update UI (thread utama)
    def update_ui(self, sysinfo, if_rows, q_rows):
        if not self.running:
            return

        if sysinfo:
            self.lbl_dev.SetLabel(f"Device : {sysinfo['board']}")
            self.lbl_ram.SetLabel(f"Ram : {sysinfo['used']:.1f} / {sysinfo['total']:.1f} MB")
            self.lbl_use.SetLabel(f"Usage : {sysinfo['pct']:.1f}%")
            self.lbl_cpu.SetLabel(f"CPU : {sysinfo['cpu']}%")
            self.lbl_dev.GetParent().Layout()

        self.fill(self.lc_if, if_rows)

        # simpan history 30 detik per queue
        names = set()
        for name, _t, _l, tx, rx in q_rows:
            names.add(name)
            h = self.history.setdefault(name, {"tx": deque(maxlen=HISTORY_SECONDS),
                                               "rx": deque(maxlen=HISTORY_SECONDS)})
            h["tx"].append(tx)
            h["rx"].append(rx)
        for gone in [n for n in self.history if n not in names]:
            del self.history[gone]

        self.queue_rows = q_rows
        self.render_queue()

        for name, win in self.graphs.items():
            h = self.history.get(name)
            if h:
                win.refresh_data(h["tx"], h["rx"])

        self.SetStatusText(f"Update: {time.strftime('%H:%M:%S')}  |  Double-click queue untuk grafik")

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

        view = [(n, t, lim, fmt_bps(tx), fmt_bps(rx), fmt_bps(tx + rx))
                for n, t, lim, tx, rx in rows]
        self.fill(self.lc_q, view)

    def fill(self, lc, rows):
        lc.Freeze()
        while lc.GetItemCount() > len(rows):
            lc.DeleteItem(lc.GetItemCount() - 1)
        for r, row in enumerate(rows):
            if r >= lc.GetItemCount():
                lc.InsertItem(r, row[0])
            elif lc.GetItemText(r, 0) != row[0]:
                lc.SetItem(r, 0, row[0])
            for c in range(1, len(row)):
                if lc.GetItemText(r, c) != row[c]:
                    lc.SetItem(r, c, row[c])
        lc.Thaw()

    # ------------------------------------------------ double-click -> grafik
    def on_queue_dclick(self, evt):
        name = self.lc_q.GetItemText(evt.GetIndex(), 0)
        if not name:
            return
        if name in self.graphs:
            self.graphs[name].Raise()
            return
        win = QueueGraphFrame(self, name)
        win.Bind(wx.EVT_CLOSE, lambda e, n=name, w=win: self.on_graph_close(n, w))
        h = self.history.get(name)
        if h:
            win.refresh_data(h["tx"], h["rx"])
        self.graphs[name] = win
        win.Show()

    def on_graph_close(self, name, win):
        self.graphs.pop(name, None)
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