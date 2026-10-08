"""
Aplikasi Info IP Saya (wxPython)
--------------------------------
Menampilkan IP publik beserta kota, negara, region, ISP, zona waktu,
mata uang, koordinat, status proxy/hosting, dan bendera negara.

Instalasi : pip install wxPython
Jalankan  : python ip_info_app.py

Sumber data: ip-api.com (gratis untuk penggunaan non-komersial, HTTP saja)
Bendera    : flagcdn.com
"""

import io
import json
import platform
import socket
import threading
import urllib.request
import webbrowser

import wx

API_URL = (
    "http://ip-api.com/json/?fields=status,message,continent,continentCode,"
    "country,countryCode,region,regionName,city,lat,lon,timezone,currency,"
    "isp,org,as,mobile,proxy,hosting,query"
)
FLAG_URL = "https://flagcdn.com/w160/{code}.png"
TIMEOUT = 10


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


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Info IP Saya", size=(560, 680))
        self.SetMinSize((480, 600))
        self.ip_text = ""
        self.lat = None
        self.lon = None
        self.rows = {}
        self._build_ui()
        self.Centre()
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(245, 247, 250))
        root = wx.BoxSizer(wx.VERTICAL)

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
        root.Add(header, 0, wx.EXPAND | wx.ALL, 16)

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
        root.Add(grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 16)

        # Tombol
        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_refresh = wx.Button(panel, label="Refresh")
        self.btn_copy = wx.Button(panel, label="Salin IP")
        self.btn_map = wx.Button(panel, label="Buka di Peta")
        btns.Add(self.btn_refresh, 0, wx.RIGHT, 8)
        btns.Add(self.btn_copy, 0, wx.RIGHT, 8)
        btns.Add(self.btn_map, 0)
        root.Add(btns, 0, wx.ALL, 16)

        panel.SetSizer(root)

        self.CreateStatusBar()
        self.SetStatusText("Siap")

        self.btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh())
        self.btn_copy.Bind(wx.EVT_BUTTON, self.on_copy)
        self.btn_map.Bind(wx.EVT_BUTTON, self.on_map)
        self.btn_copy.Disable()
        self.btn_map.Disable()

    # ------------------------------------------------------------- Aksi
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


if __name__ == "__main__":
    app = wx.App(False)
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
