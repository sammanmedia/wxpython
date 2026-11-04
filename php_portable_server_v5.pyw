#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PHP Portable Server v5  -  mini "XAMPP portable" (wxPython)

Fitur:
  * Mode: PHP saja / Web server statis saja / Keduanya
  * Port bisa diubah, akses LAN (0.0.0.0) bisa dihidupkan/dimatikan
  * Import / Update PHP dari file ZIP (otomatis ekstrak ke folder ./php)
  * Folder WWW bebas di mana saja
  * Auto start aplikasi saat login/startup OS + auto start server
  * Blokir client berdasarkan IP (mendukung CIDR) dan MAC
  * Tray icon (jalan di background)
  * Grafik bandwidth realtime (bar batang) + popup device online
  * Tab "Konfigurasi": Mode, Port, Folder WWW, Import PHP
  * [v4] Import phpLiteAdmin (editor database SQLite lewat browser)
         - dibuka di  http://localhost:PORT/dbadmin
         - jalan di proses PHP terpisah (tidak mengganggu web utama)
         - default hanya bisa diakses dari komputer ini, login pakai password
         - folder database (.sqlite) bebas dipilih

  * [v5] Bandwidth bar + tombol Start Server / Buka di Browser / Buka DB Admin
         dipindah ke BAR BAWAH yang tetap terlihat di semua tab
         (Server, Konfigurasi, Blokir Client, Log).
         Grafik dibuat kecil & rapat: 120 batang tipis (2 menit terakhir).

Install : pip install wxPython
Jalankan: python php_portable_server_v5.py
Jadi EXE: pyinstaller --noconsole --onefile php_portable_server_v5.py
"""
import os
import re
import sys
import json
import math
import time
import shutil
import socket
import string
import secrets
import zipfile
import threading
import subprocess
import ipaddress
import collections
import http.client
import webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import wx
import wx.adv

APP_NAME = "PHPPortableServer"
IS_WIN = os.name == "nt"
BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CFG_FILE = BASE_DIR / "config.json"
PHP_DIR = BASE_DIR / "php"
DBADMIN_DIR = BASE_DIR / "dbadmin"            # tempat phpLiteAdmin
PREPEND_FILE = BASE_DIR / "_prepend.php"
PREPEND_PLA = BASE_DIR / "_prepend_pla.php"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PLA_PREFIX = "/dbadmin"                       # alamat phpLiteAdmin di browser
PLA_PAGE = "phpliteadmin.php"

# Device dianggap "online" kalau ada request dalam N detik terakhir
ONLINE_WINDOW = 60

# ekstensi yang otomatis diaktifkan di php.ini saat import
PHP_EXTS = ["curl", "fileinfo", "gd", "mbstring", "mysqli", "openssl",
            "pdo_mysql", "pdo_sqlite", "sqlite3", "zip"]

DEFAULT_CFG = {
    "mode": 0,               # 0 = PHP saja, 1 = statis saja, 2 = keduanya
    "port": 8080,
    "lan": True,             # True = 0.0.0.0 (bisa diakses komputer lain)
    "www": str(BASE_DIR / "www"),
    "unit": 0,               # satuan grafik: 0 = bit/detik (Mbps), 1 = byte/detik (MB/s)
    "autostart_os": False,   # jalankan aplikasi saat startup OS
    "autostart_server": False,  # langsung start server saat aplikasi dibuka
    "blocked_ips": [],
    "blocked_macs": [],
    # --- phpLiteAdmin (v4)
    "pla_enabled": True,     # aktifkan /dbadmin (kalau sudah diimport)
    "pla_lan": False,        # False = hanya bisa dibuka dari komputer ini
    "pla_password": "",      # kosong -> dibuat acak otomatis
    "pla_dir": str(BASE_DIR / "databases"),   # folder file .sqlite
    "pla_debug": False,      # True = tampilkan error PHP di phpLiteAdmin (untuk diagnosa)
}


# ----------------------------------------------------------------- helpers
def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        cfg.update(json.loads(CFG_FILE.read_text("utf-8")))
    except Exception:
        pass
    return cfg


def gen_password(n=8):
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))


def find_php():
    p = PHP_DIR / ("php.exe" if IS_WIN else "php")
    return p if p.is_file() else None


def php_version():
    exe = find_php()
    if not exe:
        return None
    try:
        r = subprocess.run([str(exe), "-v"], capture_output=True, text=True,
                           timeout=15, creationflags=NO_WINDOW)
        out = (r.stdout or r.stderr or "").strip().splitlines()
        return out[0] if out else "PHP (versi tidak terbaca)"
    except Exception as e:
        return f"Error: {e}"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            ips.add(ip)
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def norm_mac(s):
    s = s.strip()
    parts = re.split(r"[:\-]", s)
    if len(parts) == 6 and all(re.fullmatch(r"[0-9a-fA-F]{1,2}", p) for p in parts):
        return ":".join(p.zfill(2) for p in parts).lower()
    h = re.sub(r"[^0-9a-fA-F]", "", s)
    if len(h) == 12:
        return ":".join(h[i:i + 2] for i in range(0, 12, 2)).lower()
    return None


def norm_ip(s):
    s = s.strip()
    try:
        if "/" in s:
            return str(ipaddress.ip_network(s, strict=False))
        return str(ipaddress.ip_address(s))
    except ValueError:
        return None


def ip_sort_key(ip):
    try:
        return int(ipaddress.ip_address(ip))
    except ValueError:
        return 0


def read_arp():
    """Return {ip: mac} dari tabel ARP OS."""
    table = {}
    try:
        if os.path.exists("/proc/net/arp"):
            for line in Path("/proc/net/arp").read_text().splitlines()[1:]:
                p = line.split()
                if len(p) >= 4:
                    mac = norm_mac(p[3])
                    if mac:
                        table[p[0]] = mac
        else:
            out = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                                 timeout=10, creationflags=NO_WINDOW).stdout
            for line in out.splitlines():
                mi = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
                mm = re.search(r"((?:[0-9a-fA-F]{1,2}[:\-]){5}[0-9a-fA-F]{1,2})", line)
                if mi and mm:
                    mac = norm_mac(mm.group(1))
                    if mac:
                        table[mi.group(1)] = mac
    except Exception:
        pass
    for ip, mac in list(table.items()):
        if mac in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00"):
            del table[ip]
    return table


def fmt_rate(bytes_per_sec, unit):
    """Format kecepatan. unit 0 = bit/detik (gaya Winbox), 1 = byte/detik."""
    if unit == 0:
        v = bytes_per_sec * 8
        for div, suf in ((1e9, "Gbps"), (1e6, "Mbps"), (1e3, "kbps")):
            if v >= div:
                return f"{v / div:.2f} {suf}"
        return f"{v:.0f} bps"
    v = bytes_per_sec
    for div, suf in ((1e9, "GB/s"), (1e6, "MB/s"), (1e3, "KB/s")):
        if v >= div:
            return f"{v / div:.2f} {suf}"
    return f"{v:.0f} B/s"


def fmt_size(n):
    for div, suf in ((1e12, "TB"), (1e9, "GB"), (1e6, "MB"), (1e3, "KB")):
        if n >= div:
            return f"{n / div:.2f} {suf}"
    return f"{n:.0f} B"


def nice_ceil(x):
    """Bulatkan ke atas ke 1 / 2 / 5 x 10^n."""
    if x <= 0:
        return 1
    e = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 5, 10):
        if x <= m * e:
            return m * e
    return 10 * e


def import_php_zip(zip_path):
    """Ekstrak ZIP PHP ke ./php (php.ini lama dipertahankan)."""
    tmp = BASE_DIR / "_php_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)

    exe_name = "php.exe" if IS_WIN else "php"
    found = None
    for root, _dirs, files in os.walk(tmp):
        if exe_name in files:
            found = Path(root)
            break
    if not found:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"{exe_name} tidak ditemukan di dalam ZIP.")

    old_ini = None
    if PHP_DIR.exists():
        if (PHP_DIR / "php.ini").exists():
            old_ini = (PHP_DIR / "php.ini").read_bytes()
        try:
            shutil.rmtree(PHP_DIR)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"Folder php lama tidak bisa dihapus (masih dipakai?): {e}")

    shutil.move(str(found), str(PHP_DIR))
    shutil.rmtree(tmp, ignore_errors=True)
    if not IS_WIN:
        (PHP_DIR / "php").chmod(0o755)

    ini = PHP_DIR / "php.ini"
    dev = PHP_DIR / "php.ini-development"
    if old_ini is not None:
        ini.write_bytes(old_ini)
    elif dev.exists():
        text = dev.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r'^;\s*(extension_dir\s*=\s*"ext")', r"\1", text, flags=re.M)
        text = re.sub(r"^;\s*(extension\s*=\s*(?:%s))\s*$" % "|".join(PHP_EXTS),
                      r"\1", text, flags=re.M)
        ini.write_text(text, encoding="utf-8")


# ------------------------------------------------------------ phpLiteAdmin
def pla_installed():
    return (DBADMIN_DIR / PLA_PAGE).is_file()


def pla_version():
    f = DBADMIN_DIR / PLA_PAGE
    if not f.is_file():
        return None
    try:
        head = f.read_text("utf-8", errors="replace")[:100000]
        m = re.search(r"\$version\s*=\s*['\"]([^'\"]+)['\"]", head)
        return f"v{m.group(1)}" if m else "versi tidak terbaca"
    except OSError:
        return "terpasang"


def import_pla(path):
    """Import phpLiteAdmin dari ZIP (atau langsung file phpliteadmin.php) ke ./dbadmin."""
    path = Path(path)
    if path.suffix.lower() == ".php":
        DBADMIN_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(path), str(DBADMIN_DIR / PLA_PAGE))
        return

    tmp = BASE_DIR / "_pla_tmp"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(tmp)

    found, real_name = None, None
    for root, _dirs, files in os.walk(tmp):
        for f in files:
            if f.lower() == PLA_PAGE:
                found, real_name = Path(root), f
                break
        if found:
            break
    if not found:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"{PLA_PAGE} tidak ditemukan di dalam ZIP.\n"
                         "Download dari phpliteadmin.org (file ZIP-nya).")

    if DBADMIN_DIR.exists():
        try:
            shutil.rmtree(DBADMIN_DIR)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"Folder dbadmin lama tidak bisa dihapus (masih dipakai?): {e}")
    shutil.move(str(found), str(DBADMIN_DIR))
    shutil.rmtree(tmp, ignore_errors=True)
    if real_name != PLA_PAGE:   # pastikan nama file huruf kecil (Linux case-sensitive)
        os.replace(DBADMIN_DIR / real_name, DBADMIN_DIR / PLA_PAGE)


def _php_str(s):
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def write_pla_config(state):
    """Buat phpliteadmin.config.php dari sample (atau minimal) sesuai pengaturan."""
    cfg = state.cfg
    db_dir = Path(cfg["pla_dir"])
    db_dir.mkdir(parents=True, exist_ok=True)
    if not cfg["pla_password"]:
        cfg["pla_password"] = gen_password()
        state.save()

    values = {
        "password": _php_str(cfg["pla_password"]),
        "directory": _php_str(db_dir.as_posix()),   # slash biasa, aman di Windows
        "subdirectories": "false",
        "debug": "true" if cfg.get("pla_debug") else "false",
    }
    sample = DBADMIN_DIR / "phpliteadmin.config.sample.php"
    if sample.is_file():
        text = sample.read_text("utf-8", errors="replace")
        for key, val in values.items():
            pat = r"^\$%s\s*=\s*(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|[^;\n]*)\s*;" % key
            text, n = re.subn(pat, lambda m, k=key, v=val: f"${k} = {v};", text, count=1, flags=re.M)
            if n == 0:
                text = re.sub(r"\?>\s*$", "", text) + f"\n${key} = {val};\n"
    else:
        text = ("<?php\n"
                f"$password = {values['password']};\n"
                f"$directory = {values['directory']};\n"
                "$subdirectories = false;\n"
                "$databases = array();\n"
                "$theme = 'phpliteadmin.css';\n"
                "$language = 'en';\n"
                "$rowsNum = 30;\n"
                "$charsNum = 300;\n"
                "$maxSavedQueries = 10;\n"
                "$custom_functions = array('md5', 'sha1', 'strtotime', 'strtolower', 'strtoupper', 'str_rot13');\n"
                "$cookie_name = 'pla3412';\n"
                f"$debug = {values['debug']};\n"
                "$allowed_extensions = array('db','db3','sqlite','sqlite3');\n")
    (DBADMIN_DIR / "phpliteadmin.config.php").write_text(text, "utf-8")


# ---------------------------------------------------------------- autostart
def autostart_cmd():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    if IS_WIN and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return f'"{exe}" "{Path(__file__).resolve()}" --minimized'


def _desktop_file():
    return Path.home() / ".config" / "autostart" / f"{APP_NAME}.desktop"


def is_autostart():
    if IS_WIN:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, APP_NAME)
                return True
        except OSError:
            return False
    return _desktop_file().exists()


def set_autostart(enable):
    if IS_WIN:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_cmd())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
    else:
        f = _desktop_file()
        if enable:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("[Desktop Entry]\nType=Application\n"
                         f"Name={APP_NAME}\nExec={autostart_cmd()}\n"
                         "X-GNOME-Autostart-enabled=true\n")
        elif f.exists():
            f.unlink()


# -------------------------------------------------------------------- state
class State:
    def __init__(self):
        self.cfg = load_cfg()
        self.logcb = lambda m: None
        self.php_port = 0
        self.pla_port = 0    # port internal proses PHP untuk phpLiteAdmin (0 = tidak aktif)
        self._arp = {}
        self._arp_t = 0.0
        self._arp_busy = False
        self._lock = threading.Lock()
        # --- trafik & online device
        self._tlock = threading.Lock()
        self.tx = 0          # byte terkirim ke client  => "DOWNLOAD"
        self.rx = 0          # byte diterima dari client => "UPLOAD"
        self.seen = {}       # {key: (ip, waktu terakhir request)}
        self.own_ips = {"127.0.0.1"}
        self.names = {}      # cache nama host {ip: (nama, waktu)}
        self._resolving = set()

    def save(self):
        try:
            CFG_FILE.write_text(json.dumps(self.cfg, indent=2), "utf-8")
        except OSError:
            pass

    def log(self, msg):
        self.logcb(msg)

    def is_local(self, ip):
        return ip in self.own_ips or ip.startswith("127.") or ip == "::1"

    # ---- trafik
    def add_tx(self, n):
        with self._tlock:
            self.tx += n

    def add_rx(self, n):
        with self._tlock:
            self.rx += n

    def traffic(self):
        with self._tlock:
            return self.tx, self.rx

    def reset_traffic(self):
        with self._tlock:
            self.tx = self.rx = 0
            self.seen.clear()

    # ---- online device
    def touch(self, ip):
        key = "local" if self.is_local(ip) else ip
        with self._tlock:
            self.seen[key] = (ip, time.time())

    def _prune_seen(self):
        now = time.time()
        for k in [k for k, (_ip, t) in self.seen.items() if now - t > ONLINE_WINDOW]:
            del self.seen[k]

    def online_count(self):
        with self._tlock:
            self._prune_seen()
            return len(self.seen)

    def online_devices(self):
        """List [(is_local, ip, last_seen)] - komputer ini dulu, lalu urut IP."""
        with self._tlock:
            self._prune_seen()
            items = [(k == "local", ip, t) for k, (ip, t) in self.seen.items()]
        items.sort(key=lambda d: (not d[0], ip_sort_key(d[1])))
        return items

    def name_of(self, ip):
        """Nama host (reverse DNS) di-resolve di thread terpisah + di-cache."""
        now = time.time()
        cached = self.names.get(ip)
        if cached and (cached[0] != "-" or now - cached[1] < 300):
            return cached[0]
        with self._tlock:
            if ip in self._resolving:
                return cached[0] if cached else "..."
            self._resolving.add(ip)
        threading.Thread(target=self._resolve, args=(ip,), daemon=True).start()
        return cached[0] if cached else "..."

    def _resolve(self, ip):
        name = "-"
        try:
            host = socket.gethostbyaddr(ip)[0]
            if host and host != ip:
                name = host.split(".")[0] or host
        except Exception:
            pass
        self.names[ip] = (name, time.time())
        with self._tlock:
            self._resolving.discard(ip)

    # ---- ARP / MAC
    def refresh_arp(self):
        with self._lock:
            self._arp = read_arp()
            self._arp_t = time.time()

    def refresh_arp_async(self):
        if self._arp_busy:
            return
        self._arp_busy = True

        def job():
            try:
                self.refresh_arp()
            finally:
                self._arp_busy = False
        threading.Thread(target=job, daemon=True).start()

    def arp_age(self):
        return time.time() - self._arp_t

    def mac_cached(self, ip):
        return self._arp.get(ip)

    def mac_of(self, ip):
        with self._lock:
            now = time.time()
            if (ip not in self._arp and now - self._arp_t > 3) or now - self._arp_t > 30:
                self._arp = read_arp()
                self._arp_t = time.time()
            return self._arp.get(ip)

    # ---- blokir
    def is_blocked(self, ip):
        if ip in ("127.0.0.1", "::1"):
            return False  # localhost tidak pernah diblokir
        try:
            addr = ipaddress.ip_address(ip)
            for entry in self.cfg["blocked_ips"]:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
        except ValueError:
            pass
        macs = self.cfg["blocked_macs"]
        if macs:
            mac = self.mac_of(ip)
            if mac and mac in macs:
                return True
        return False


# ------------------------------------------------------------- HTTP server
HOP_REQ = {"connection", "keep-alive", "proxy-connection", "te", "trailer",
           "transfer-encoding", "upgrade", "x-forwarded-for"}
HOP_RES = {"connection", "keep-alive", "transfer-encoding", "content-length",
           "date", "server"}


class CountReader:
    """Bungkus rfile: hitung byte yang masuk dari client (UPLOAD)."""

    def __init__(self, f, cb):
        self._f, self._cb = f, cb

    def read(self, *a):
        d = self._f.read(*a)
        if d:
            self._cb(len(d))
        return d

    def readline(self, *a):
        d = self._f.readline(*a)
        if d:
            self._cb(len(d))
        return d

    def readinto(self, b):
        n = self._f.readinto(b)
        if n:
            self._cb(n)
        return n

    def __getattr__(self, name):
        return getattr(self._f, name)


class CountWriter:
    """Bungkus wfile: hitung byte yang keluar ke client (DOWNLOAD)."""

    def __init__(self, f, cb):
        self._f, self._cb = f, cb

    def write(self, b):
        n = self._f.write(b)
        self._cb(len(b) if n is None else n)
        return n

    def __getattr__(self, name):
        return getattr(self._f, name)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not IS_WIN   # di Windows SO_REUSEADDR bisa "mencuri" port
    request_queue_size = 64

    def __init__(self, addr, handler, state):
        self.state = state
        super().__init__(addr, handler)

    def finish_request(self, request, client_address):
        ip = client_address[0]
        if self.state.is_blocked(ip):
            self.state.log(f"[BLOKIR] koneksi dari {ip} ditolak")
            return  # koneksi ditutup tanpa respon
        super().finish_request(request, client_address)


def make_handler(state):
    class Handler(SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 30

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=state.cfg["www"], **kw)

        def setup(self):
            super().setup()
            self.rfile = CountReader(self.rfile, state.add_rx)
            self.wfile = CountWriter(self.wfile, state.add_tx)

        def log_message(self, fmt, *args):
            state.log(f"{self.client_address[0]}  {fmt % args}")

        def _wants_php(self):
            mode = state.cfg["mode"]
            if mode == 0:
                return True
            if mode == 1:
                return False
            fs = self.translate_path(self.path)
            if os.path.isdir(fs):
                return os.path.isfile(os.path.join(fs, "index.php"))
            if os.path.isfile(fs):
                return fs.lower().endswith(".php")
            return True  # file tidak ada -> biar PHP yang menangani (router/404)

        def _route(self):
            state.touch(self.client_address[0])
            path = self.path.split("?", 1)[0]
            # phpLiteAdmin di /dbadmin (proses PHP terpisah)
            if state.pla_port and (path == PLA_PREFIX or path.startswith(PLA_PREFIX + "/")):
                return self._dbadmin(path)
            if state.cfg["mode"] == 1 and path.lower().endswith(".php"):
                return self.send_error(403, "Mode statis: PHP tidak aktif")
            if self._wants_php():
                return self._proxy()
            if self.command == "GET":
                SimpleHTTPRequestHandler.do_GET(self)
            elif self.command == "HEAD":
                SimpleHTTPRequestHandler.do_HEAD(self)
            else:
                self.send_error(405, "Method tidak didukung di mode statis")

        do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _route

        def _dbadmin(self, path):
            if not state.cfg["pla_lan"] and not state.is_local(self.client_address[0]):
                state.log(f"[dbadmin] akses dari {self.client_address[0]} ditolak (hanya komputer ini)")
                return self.send_error(403, "phpLiteAdmin hanya bisa dibuka dari komputer ini")
            if path in (PLA_PREFIX, PLA_PREFIX + "/"):
                self.send_response(302)
                self.send_header("Location", f"{PLA_PREFIX}/{PLA_PAGE}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._proxy(state.pla_port, PLA_PREFIX)

        def _proxy(self, port=None, strip=""):
            conn = None
            port = port or state.php_port
            try:
                target = self.path
                if strip:
                    target = target[len(strip):]
                    if not target.startswith("/"):
                        target = "/" + target
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_REQ}
                headers["X-Forwarded-For"] = self.client_address[0]
                headers["Connection"] = "close"
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=300)
                conn.request(self.command, target, body, headers)
                resp = conn.getresponse()
                data = resp.read()
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    kl = k.lower()
                    if kl in HOP_RES:
                        continue
                    if strip and kl == "location":   # redirect dari sub-aplikasi -> beri prefix
                        m = re.match(r"https?://127\.0\.0\.1:%d(/.*)?$" % port, v)
                        if m:
                            v = strip + (m.group(1) or "/")
                        elif v.startswith("/"):
                            v = strip + v
                    self.send_header(k, v)
                if self.command == "HEAD":
                    self.send_header("Content-Length", resp.getheader("Content-Length", "0"))
                    self.end_headers()
                else:
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            except (OSError, http.client.HTTPException) as e:
                state.log(f"[proxy] error: {e}")
                try:
                    self.send_error(502, "PHP backend tidak merespon")
                except OSError:
                    pass
            finally:
                if conn:
                    conn.close()

    return Handler


class Runner:
    def __init__(self, state):
        self.state = state
        self.httpd = None
        self.php = None      # proses PHP web utama
        self.pla = None      # proses PHP untuk phpLiteAdmin

    def running(self):
        return self.httpd is not None

    def _pump(self, proc, tag):
        for line in proc.stdout:
            line = line.strip()
            if line and "Accepted" not in line and "Closing" not in line:
                self.state.log(f"[{tag}] {line}")

    def _kill_php(self):
        for attr in ("php", "pla"):
            proc = getattr(self, attr)
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                setattr(self, attr, None)

    def _spawn_php(self, port, docroot, prepend, tag, extra=()):
        """Jalankan `php -S 127.0.0.1:port -t docroot` dan tunggu sampai siap."""
        exe = find_php()
        cmd = [str(exe), "-c", str(PHP_DIR),
               "-d", f"auto_prepend_file={prepend}", *extra,
               "-S", f"127.0.0.1:{port}", "-t", str(docroot)]
        env = dict(os.environ, PHP_CLI_SERVER_WORKERS="8")  # multi-worker (Linux/macOS)
        proc = subprocess.Popen(cmd, cwd=str(docroot), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace", env=env, creationflags=NO_WINDOW)
        threading.Thread(target=self._pump, args=(proc, tag), daemon=True).start()
        for _ in range(50):
            if proc.poll() is not None:
                raise RuntimeError("PHP gagal berjalan.\nPastikan Visual C++ Redistributable "
                                   "terpasang dan ZIP PHP-nya benar (cek tab Log).")
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                return proc
            except OSError:
                time.sleep(0.1)
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("PHP tidak merespon (timeout).")

    def start(self):
        st, cfg = self.state, self.state.cfg
        st.reset_traffic()
        st.own_ips = set(local_ips()) | {"127.0.0.1"}
        st.pla_port = 0

        www = Path(cfg["www"])
        new = not www.exists()
        www.mkdir(parents=True, exist_ok=True)
        if new or not any(www.iterdir()):
            (www / "index.php").write_text(
                '<?php\necho "<h1>PHP " . PHP_VERSION . " jalan!</h1>";\n'
                'echo "<p>Client IP: " . $_SERVER["REMOTE_ADDR"] . "</p>";\n'
                'echo "<p><a href=\\"?info\\">phpinfo()</a></p>";\n'
                'if (isset($_GET["info"])) phpinfo();\n', "utf-8")

        if cfg["mode"] in (0, 2):
            if not find_php():
                raise RuntimeError("PHP belum ada.\nKlik tombol 'Import / Update PHP dari ZIP' "
                                   "di tab Konfigurasi dulu.")
            PREPEND_FILE.write_text(
                "<?php if(isset($_SERVER['HTTP_X_FORWARDED_FOR'])) "
                "$_SERVER['REMOTE_ADDR']=$_SERVER['HTTP_X_FORWARDED_FOR'];\n", "utf-8")
            st.php_port = free_port()
            self.php = self._spawn_php(st.php_port, www, PREPEND_FILE, "php")

            # phpLiteAdmin: proses PHP terpisah (gagal = tidak fatal, cukup dicatat di log)
            if cfg["pla_enabled"] and pla_installed():
                try:
                    write_pla_config(st)
                    PREPEND_PLA.write_text(
                        "<?php\n"
                        "(function () {\n"
                        "    if (isset($_SERVER['HTTP_X_FORWARDED_FOR'])) "
                        "$_SERVER['REMOTE_ADDR'] = $_SERVER['HTTP_X_FORWARDED_FOR'];\n"
                        "    foreach (array('PHP_SELF', 'SCRIPT_NAME', 'REQUEST_URI') as $k) {\n"
                        "        if (isset($_SERVER[$k])) $_SERVER[$k] = '" + PLA_PREFIX + "' . $_SERVER[$k];\n"
                        "    }\n"
                        "})();\n", "utf-8")
                    port = free_port()
                    extra = ["-d", "log_errors=1"]          # error PHP juga masuk tab Log
                    if cfg["pla_debug"]:                    # tampilkan error (tanpa notice/deprecated)
                        extra += ["-d", "display_errors=1", "-d", "error_reporting=6135"]
                    self.pla = self._spawn_php(port, DBADMIN_DIR, PREPEND_PLA, "dbadmin", extra)
                    st.pla_port = port
                except Exception as e:
                    st.log(f"[dbadmin] gagal start: {e}")
                    self.pla = None
                    st.pla_port = 0

        host = "0.0.0.0" if cfg["lan"] else "127.0.0.1"
        try:
            self.httpd = Server((host, cfg["port"]), make_handler(st), st)
        except OSError as e:
            self._kill_php()
            st.pla_port = 0
            raise RuntimeError(f"Port {cfg['port']} tidak bisa dipakai:\n{e}")
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        st.log(f"Server START  mode={cfg['mode']}  {host}:{cfg['port']}  www={www}"
               + (f"  dbadmin={PLA_PREFIX}" if st.pla_port else ""))

    def stop(self):
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        self._kill_php()
        self.state.pla_port = 0
        self.state.log("Server STOP")


# ---------------------------------------------------------------------- GUI
def make_icon():
    bmp = wx.Bitmap(32, 32)
    dc = wx.MemoryDC(bmp)
    dc.SetBackground(wx.Brush(wx.Colour(63, 81, 181)))
    dc.Clear()
    dc.SetTextForeground(wx.WHITE)
    dc.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
    dc.DrawText("P", 9, 5)
    dc.SelectObject(wx.NullBitmap)
    icon = wx.Icon()
    icon.CopyFromBitmap(bmp)
    return icon


class BandwidthGraph(wx.Panel):
    """Grafik BAR BATANG kecil gaya Winbox jadul (ringkas, untuk bar bawah):
    background putih, 1 batang tipis = 1 detik, 120 detik terakhir (terbaru di kanan),
    garis vertikal tipis tiap 30 detik."""
    SECONDS = 120     # jumlah batang (makin banyak = batang makin tipis)
    MARK_EVERY = 30   # garis vertikal tiap N detik
    HEIGHT = 80       # tinggi panel (kecil / ringkas)

    def __init__(self, parent, title, color, dark, get_unit):
        super().__init__(parent, style=wx.BORDER_SIMPLE)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetMinSize((150, self.HEIGHT))
        self.title, self.color, self.dark, self.get_unit = title, color, dark, get_unit
        self.data = collections.deque([0.0] * self.SECONDS, maxlen=self.SECONDS)
        self.count = 0          # jumlah sampel sejak server start (untuk posisi garis 30 detik)
        self.total = 0
        self.f_bold = wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        self.f_small = wx.Font(8, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_SIZE, self.on_size)

    def on_size(self, event):
        self.Refresh()
        event.Skip()

    def push(self, rate, total):
        if total == 0 and self.total > 0:   # server di-restart -> mulai lagi dari nol
            self.count = 0
        self.data.append(float(rate))
        self.count += 1
        self.total = total
        self.Refresh()

    def on_paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetClientSize()
        dc.SetBackground(wx.Brush(wx.Colour(255, 255, 255)))   # putih
        dc.Clear()
        top, bottom = 17, 15
        area_h = h - top - bottom
        if w < 30 or area_h < 10:
            return

        unit = self.get_unit()
        base_y = top + area_h
        peak = max(self.data)
        if unit == 0:   # skala "cantik" dalam bit/detik
            scale = nice_ceil(max(peak * 8, 8000)) / 8.0
        else:           # skala "cantik" dalam byte/detik
            scale = float(nice_ceil(max(peak, 1000)))

        # grid horizontal (abu-abu muda)
        dc.SetPen(wx.Pen(wx.Colour(228, 228, 228), 1))
        for i in range(4):
            y = int(top + area_h * i / 3.0)
            dc.DrawLine(0, y, w, y)

        # batang kecil-kecil
        n = len(self.data)
        bw = w / float(n)
        gap = 1 if bw >= 2.5 else 0
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(self.color))
        for i, v in enumerate(self.data):
            if v <= 0:
                continue
            bh = max(1, int(area_h * min(v / scale, 1.0)))
            x0 = int(i * bw)
            x1 = int((i + 1) * bw)
            dc.DrawRectangle(x0, base_y - bh, max(1, x1 - x0 - gap), bh)

        # garis vertikal tipis tiap 30 detik (ikut bergeser bersama data)
        dc.SetPen(wx.Pen(wx.Colour(150, 150, 165), 1))
        for i in range(n):
            gi = self.count - n + i          # indeks sampel global
            if gi > 0 and gi % self.MARK_EVERY == 0:
                x = int(i * bw)
                dc.DrawLine(x, top, x, base_y)

        # garis dasar
        dc.SetPen(wx.Pen(wx.Colour(120, 120, 120), 1))
        dc.DrawLine(0, base_y, w, base_y)

        # teks
        dc.SetFont(self.f_bold)
        dc.SetTextForeground(self.dark)
        dc.DrawText(self.title, 4, 1)
        cur = fmt_rate(self.data[-1], unit)
        tw = dc.GetTextExtent(cur)[0]
        dc.SetTextForeground(wx.Colour(40, 40, 40))
        dc.DrawText(cur, w - tw - 4, 1)
        dc.SetFont(self.f_small)
        dc.SetTextForeground(wx.Colour(110, 110, 110))
        dc.DrawText(f"skala {fmt_rate(scale, unit)}", 4, base_y + 1)
        tot = f"total {fmt_size(self.total)}"
        tw = dc.GetTextExtent(tot)[0]
        dc.DrawText(tot, w - tw - 4, base_y + 1)


class OnlineWindow(wx.Frame):
    """Jendela kecil: daftar device online (NO | NAME DEVICE | IP ADDRESS | MAC ADDRESS)."""

    def __init__(self, parent):
        style = wx.CAPTION | wx.CLOSE_BOX | wx.SYSTEM_MENU | wx.RESIZE_BORDER | wx.FRAME_FLOAT_ON_PARENT
        super().__init__(parent, title="Device Online", size=(560, 290), style=style)
        self.owner = parent
        p = wx.Panel(self)
        s = wx.BoxSizer(wx.VERTICAL)
        self.lc = wx.ListCtrl(p, style=wx.LC_REPORT | wx.LC_HRULES | wx.LC_VRULES | wx.BORDER_SUNKEN)
        for i, (t, wd) in enumerate((("NO", 45), ("NAME DEVICE", 190), ("IP ADDRESS", 125), ("MAC ADDRESS", 150))):
            self.lc.InsertColumn(i, t, width=wd)
        self.lbl = wx.StaticText(p, label="")
        s.Add(self.lc, 1, wx.EXPAND | wx.ALL, 6)
        s.Add(self.lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        p.SetSizer(s)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    def set_rows(self, rows, note):
        lc = self.lc
        while lc.GetItemCount() > len(rows):
            lc.DeleteItem(lc.GetItemCount() - 1)
        for i, row in enumerate(rows):
            if i >= lc.GetItemCount():
                lc.InsertItem(i, row[0])
            for col in range(len(row)):
                if lc.GetItemText(i, col) != row[col]:
                    lc.SetItem(i, col, row[col])
        self.lbl.SetLabel(note)

    def on_close(self, event):
        self.owner.online_win = None
        self.Destroy()


class Tray(wx.adv.TaskBarIcon):
    def __init__(self, frame):
        super().__init__()
        self.frame = frame
        self.SetIcon(frame.icon, "PHP Portable Server")
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, lambda e: frame.show_window())

    def CreatePopupMenu(self):
        m = wx.Menu()
        i1 = m.Append(wx.ID_ANY, "Buka jendela")
        i2 = m.Append(wx.ID_ANY, "Stop Server" if self.frame.runner.running() else "Start Server")
        m.AppendSeparator()
        i3 = m.Append(wx.ID_ANY, "Keluar")
        self.Bind(wx.EVT_MENU, lambda e: self.frame.show_window(), i1)
        self.Bind(wx.EVT_MENU, lambda e: self.frame.toggle(), i2)
        self.Bind(wx.EVT_MENU, lambda e: self.frame.quit(), i3)
        return m


class MainFrame(wx.Frame):
    def __init__(self, state, runner):
        super().__init__(None, title="PHP Portable Server v5", size=(720, 790))
        self.SetMinSize((660, 700))
        self.state, self.runner = state, runner
        self.online_win = None
        self.icon = make_icon()
        self.SetIcon(self.icon)
        self.tray = Tray(self)

        mb = wx.MenuBar()
        fm = wx.Menu()
        it = fm.Append(wx.ID_EXIT, "Keluar")
        mb.Append(fm, "&File")
        self.SetMenuBar(mb)
        self.Bind(wx.EVT_MENU, lambda e: self.quit(), it)

        # panel utama: [ Notebook (tab) ]  +  [ BAR BAWAH tetap: bandwidth + tombol ]
        self.main_panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)
        nb = wx.Notebook(self.main_panel)
        nb.AddPage(self._tab_server(nb), "Server")
        nb.AddPage(self._tab_config(nb), "Konfigurasi")
        nb.AddPage(self._tab_block(nb), "Blokir Client")
        nb.AddPage(self._tab_log(nb), "Log")
        root.Add(nb, 1, wx.EXPAND | wx.ALL, 4)
        root.Add(wx.StaticLine(self.main_panel, style=wx.LI_HORIZONTAL), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)
        root.Add(self._bottom_bar(self.main_panel), 0, wx.EXPAND | wx.ALL, 8)
        self.main_panel.SetSizer(root)

        sb = self.CreateStatusBar()
        sb.SetCursor(wx.Cursor(wx.CURSOR_HAND))   # kursor tangan = bisa diklik
        sb.Bind(wx.EVT_LEFT_UP, self.on_status_click)

        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.php_info = php_version()
        self.pla_info = pla_version()

        # timer 1 detik: hitung bandwidth + update status bar + popup online
        self._t0 = time.time()
        self._last_tx, self._last_rx = state.traffic()
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.on_tick, self.timer)
        self.timer.Start(1000)

        self.refresh_ui()

    # ---- tab server (alamat akses + opsi LAN / autostart)
    def _tab_server(self, nb):
        c = self.state.cfg
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)

        sb = wx.StaticBoxSizer(wx.VERTICAL, p, "Alamat Akses")
        self.lbl_url = wx.StaticText(sb.GetStaticBox(), label="Server mati.")
        sb.Add(self.lbl_url, 0, wx.ALL, 10)
        s.Add(sb, 0, wx.EXPAND | wx.ALL, 10)

        self.cb_lan = wx.CheckBox(p, label="Izinkan akses dari komputer lain (LAN, bind 0.0.0.0)")
        self.cb_lan.SetValue(bool(c["lan"]))
        self.cb_os = wx.CheckBox(p, label="Jalankan aplikasi ini saat startup komputer (di tray)")
        self.cb_os.SetValue(is_autostart())
        self.cb_srv = wx.CheckBox(p, label="Otomatis start server begitu aplikasi dibuka")
        self.cb_srv.SetValue(bool(c["autostart_server"]))
        s.Add(self.cb_lan, 0, wx.LEFT | wx.TOP, 12)
        s.Add(self.cb_os, 0, wx.LEFT | wx.TOP, 12)
        s.Add(self.cb_srv, 0, wx.LEFT | wx.TOP, 12)
        p.SetSizer(s)

        self.cb_os.Bind(wx.EVT_CHECKBOX, self.on_autostart_os)
        self.cb_srv.Bind(wx.EVT_CHECKBOX, self.on_autostart_srv)
        return p

    # ---- BAR BAWAH: bandwidth + tombol (terlihat di semua tab)
    def _bottom_bar(self, parent):
        c = self.state.cfg
        box = wx.BoxSizer(wx.VERTICAL)

        self.ch_unit = wx.Choice(parent, choices=["Mbps (bit/detik)", "MB/s (byte/detik)"])
        self.ch_unit.SetSelection(int(c.get("unit", 0)))
        get_unit = lambda: self.ch_unit.GetSelection()

        self.g_down = BandwidthGraph(parent, "\u25BC DOWNLOAD", wx.Colour(33, 150, 243),
                                     wx.Colour(21, 101, 192), get_unit)   # biru, kiri
        self.g_up = BandwidthGraph(parent, "\u25B2 UPLOAD", wx.Colour(76, 175, 80),
                                   wx.Colour(46, 125, 50), get_unit)      # hijau, kanan
        bw = wx.BoxSizer(wx.HORIZONTAL)
        bw.Add(self.g_down, 1, wx.EXPAND | wx.RIGHT, 6)
        bw.Add(self.g_up, 1, wx.EXPAND)
        box.Add(bw, 0, wx.EXPAND)

        row = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_start = wx.Button(parent, label="Start Server", size=(150, 32))
        self.btn_open = wx.Button(parent, label="Buka di Browser", size=(150, 32))
        self.btn_dbadmin = wx.Button(parent, label="Buka DB Admin", size=(150, 32))
        row.Add(self.btn_start, 0, wx.RIGHT, 8)
        row.Add(self.btn_open, 0, wx.RIGHT, 8)
        row.Add(self.btn_dbadmin, 0)
        row.AddStretchSpacer(1)
        row.Add(wx.StaticText(parent, label="Satuan:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        row.Add(self.ch_unit, 0, wx.ALIGN_CENTER_VERTICAL)
        box.Add(row, 0, wx.EXPAND | wx.TOP, 8)

        self.btn_start.Bind(wx.EVT_BUTTON, lambda e: self.toggle())
        self.btn_open.Bind(wx.EVT_BUTTON, lambda e: webbrowser.open(f"http://localhost:{self.state.cfg['port']}"))
        self.btn_dbadmin.Bind(wx.EVT_BUTTON,
                              lambda e: webbrowser.open(f"http://localhost:{self.state.cfg['port']}{PLA_PREFIX}"))
        self.ch_unit.Bind(wx.EVT_CHOICE, self.on_unit)
        return box

    # ---- tab konfigurasi (Mode, Port, Folder WWW, PHP, phpLiteAdmin)
    def _tab_config(self, nb):
        c = self.state.cfg
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)

        self.rb_mode = wx.RadioBox(p, label="Mode", majorDimension=1, style=wx.RA_SPECIFY_COLS,
                                   choices=["PHP saja  (php -S, semua request lewat PHP)",
                                            "Web server statis saja  (HTML/CSS/JS, tanpa PHP)",
                                            "Keduanya  (file statis langsung, .php dijalankan PHP)"])
        self.rb_mode.SetSelection(int(c["mode"]))
        s.Add(self.rb_mode, 0, wx.EXPAND | wx.ALL, 10)

        g = wx.FlexGridSizer(cols=3, vgap=10, hgap=8)
        g.AddGrowableCol(1)
        g.Add(wx.StaticText(p, label="Port:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.sp_port = wx.SpinCtrl(p, min=1, max=65535, initial=int(c["port"]))
        g.Add(self.sp_port, 0)
        g.Add((0, 0))

        g.Add(wx.StaticText(p, label="Folder WWW:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.tc_www = wx.TextCtrl(p, value=c["www"])
        g.Add(self.tc_www, 1, wx.EXPAND)
        self.btn_browse = wx.Button(p, label="Browse...")
        g.Add(self.btn_browse)

        g.Add(wx.StaticText(p, label="PHP:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.lbl_php = wx.StaticText(p, label="-")
        g.Add(self.lbl_php, 1, wx.EXPAND)
        self.btn_import = wx.Button(p, label="Import / Update PHP dari ZIP")
        g.Add(self.btn_import)
        s.Add(g, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # ---- phpLiteAdmin
        sb = wx.StaticBoxSizer(wx.VERTICAL, p, "Editor Database SQLite  (phpLiteAdmin, lewat browser)")
        box = sb.GetStaticBox()

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(wx.StaticText(box, label="Status:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        self.lbl_pla = wx.StaticText(box, label="-")
        top.Add(self.lbl_pla, 1, wx.ALIGN_CENTER_VERTICAL)
        self.btn_import_pla = wx.Button(box, label="Import phpLiteAdmin dari ZIP")
        top.Add(self.btn_import_pla)
        sb.Add(top, 0, wx.EXPAND | wx.ALL, 8)

        self.cb_pla = wx.CheckBox(box, label=f"Aktifkan phpLiteAdmin  (dibuka di  {PLA_PREFIX})")
        self.cb_pla.SetValue(bool(c["pla_enabled"]))
        self.cb_pla_lan = wx.CheckBox(box, label="Izinkan dibuka dari komputer lain / LAN  (tidak disarankan)")
        self.cb_pla_lan.SetValue(bool(c["pla_lan"]))
        sb.Add(self.cb_pla, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        sb.Add(self.cb_pla_lan, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        self.cb_pla_debug = wx.CheckBox(box, label="Mode debug: tampilkan error PHP  (aktifkan kalau muncul HTTP ERROR 500)")
        self.cb_pla_debug.SetValue(bool(c["pla_debug"]))
        sb.Add(self.cb_pla_debug, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        g2 = wx.FlexGridSizer(cols=3, vgap=8, hgap=8)
        g2.AddGrowableCol(1)
        g2.Add(wx.StaticText(box, label="Password login:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.tc_pla_pw = wx.TextCtrl(box, value=c["pla_password"])
        g2.Add(self.tc_pla_pw, 1, wx.EXPAND)
        g2.Add((0, 0))
        g2.Add(wx.StaticText(box, label="Folder database:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.tc_pla_dir = wx.TextCtrl(box, value=c["pla_dir"])
        g2.Add(self.tc_pla_dir, 1, wx.EXPAND)
        self.btn_pla_dir = wx.Button(box, label="Browse...")
        g2.Add(self.btn_pla_dir)
        sb.Add(g2, 0, wx.EXPAND | wx.ALL, 8)
        sb.Add(wx.StaticText(box, label="phpLiteAdmin aktif di mode 'PHP saja' atau 'Keduanya'. "
                                        "File .sqlite di folder database otomatis terdaftar."),
               0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        s.Add(sb, 0, wx.EXPAND | wx.ALL, 10)

        self.lbl_cfg_note = wx.StaticText(p, label="")
        s.Add(self.lbl_cfg_note, 0, wx.ALL, 12)
        p.SetSizer(s)

        self.btn_browse.Bind(wx.EVT_BUTTON, lambda e: self.browse_dir(self.tc_www, "Pilih folder WWW"))
        self.btn_pla_dir.Bind(wx.EVT_BUTTON, lambda e: self.browse_dir(self.tc_pla_dir, "Pilih folder database SQLite"))
        self.btn_import.Bind(wx.EVT_BUTTON, self.on_import_php)
        self.btn_import_pla.Bind(wx.EVT_BUTTON, self.on_import_pla)
        return p

    # ---- tab blokir
    def _tab_block(self, nb):
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        note = wx.StaticText(p, label=(
            "Blokir langsung aktif tanpa restart server. IP boleh berupa CIDR (192.168.1.0/24).\n"
            "MAC hanya bisa dibaca untuk perangkat di jaringan/subnet yang sama (tabel ARP).\n"
            "127.0.0.1 (komputer ini) tidak pernah diblokir."))
        s.Add(note, 0, wx.ALL, 10)
        s.Add(self._block_box(p, "Blokir berdasarkan IP", "blocked_ips", norm_ip, "ip"),
              1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        s.Add(self._block_box(p, "Blokir berdasarkan MAC address", "blocked_macs", norm_mac, "mac"),
              1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        p.SetSizer(s)
        return p

    def _block_box(self, parent, title, key, validate, kind):
        cfg = self.state.cfg
        sb = wx.StaticBoxSizer(wx.VERTICAL, parent, title)
        box = sb.GetStaticBox()
        lb = wx.ListBox(box, choices=list(cfg[key]))
        tc = wx.TextCtrl(box, style=wx.TE_PROCESS_ENTER)
        b_add = wx.Button(box, label="Tambah")
        b_del = wx.Button(box, label="Hapus")
        b_arp = wx.Button(box, label="Pilih dari ARP")
        row = wx.BoxSizer(wx.HORIZONTAL)
        row.Add(tc, 1, wx.RIGHT, 6)
        for b in (b_add, b_del, b_arp):
            row.Add(b, 0, wx.RIGHT, 6)
        sb.Add(lb, 1, wx.EXPAND | wx.ALL, 6)
        sb.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        def add(value):
            v = validate(value)
            if not v:
                wx.MessageBox("Format tidak valid.", "Blokir", wx.OK | wx.ICON_WARNING, self)
                return
            if v not in cfg[key]:
                cfg[key].append(v)
                self.state.save()
                lb.Append(v)
            tc.Clear()

        def on_del(_):
            i = lb.GetSelection()
            if i != wx.NOT_FOUND:
                cfg[key].remove(lb.GetString(i))
                self.state.save()
                lb.Delete(i)

        def on_arp(_):
            with wx.BusyCursor():
                table = read_arp()
            if not table:
                wx.MessageBox("Tabel ARP kosong. Coba ping dulu perangkatnya.", "ARP", wx.OK, self)
                return
            items = sorted(table.items())
            dlg = wx.SingleChoiceDialog(self, "Pilih perangkat:", "Perangkat di LAN",
                                        [f"{ip}    {mac}" for ip, mac in items])
            if dlg.ShowModal() == wx.ID_OK:
                ip, mac = items[dlg.GetSelection()]
                add(ip if kind == "ip" else mac)
            dlg.Destroy()

        b_add.Bind(wx.EVT_BUTTON, lambda e: add(tc.GetValue()))
        tc.Bind(wx.EVT_TEXT_ENTER, lambda e: add(tc.GetValue()))
        b_del.Bind(wx.EVT_BUTTON, on_del)
        b_arp.Bind(wx.EVT_BUTTON, on_arp)
        return sb

    # ---- tab log
    def _tab_log(self, nb):
        p = wx.Panel(nb)
        s = wx.BoxSizer(wx.VERTICAL)
        self.log = wx.TextCtrl(p, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        btn = wx.Button(p, label="Bersihkan log")
        btn.Bind(wx.EVT_BUTTON, lambda e: self.log.Clear())
        s.Add(self.log, 1, wx.EXPAND | wx.ALL, 8)
        s.Add(btn, 0, wx.LEFT | wx.BOTTOM, 8)
        p.SetSizer(s)
        return p

    def append_log(self, msg):
        if self.log.GetNumberOfLines() > 3000:
            self.log.Remove(0, self.log.GetLastPosition() // 2)
        self.log.AppendText(f"[{time.strftime('%H:%M:%S')}] {msg}\n")

    # ---- timer: bandwidth + status + popup online
    def on_tick(self, _event):
        now = time.time()
        dt = max(now - self._t0, 0.001)
        self._t0 = now
        tx, rx = self.state.traffic()
        d_tx = max(0, tx - self._last_tx)
        d_rx = max(0, rx - self._last_rx)
        self._last_tx, self._last_rx = tx, rx
        self.g_down.push(d_tx / dt, tx)   # DOWNLOAD = server -> client
        self.g_up.push(d_rx / dt, rx)     # UPLOAD   = client -> server
        self.update_status()
        self.update_online_window()

    def update_status(self):
        if self.runner.running():
            n = self.state.online_count()
            self.SetStatusText(f"Server ON - Port {self.state.cfg['port']} - Online : {n} device")
        else:
            self.SetStatusText("Server OFF")

    def on_unit(self, _):
        self.state.cfg["unit"] = self.ch_unit.GetSelection()
        self.state.save()
        self.g_down.Refresh()
        self.g_up.Refresh()

    # ---- popup daftar device online
    def on_status_click(self, event):
        self.show_online_window()
        event.Skip()

    def show_online_window(self):
        if self.online_win:
            self.online_win.Raise()
            self.update_online_window()
            return
        win = OnlineWindow(self)
        mx, my = wx.GetMousePosition()
        w, h = win.GetSize()
        idx = wx.Display.GetFromPoint(wx.Point(mx, my))
        area = wx.Display(idx if idx != wx.NOT_FOUND else 0).GetClientArea()
        x = max(area.x, min(mx - 40, area.x + area.width - w))
        y = max(area.y, my - h - 25)          # muncul tepat di atas status bar
        win.SetPosition(wx.Point(x, y))
        self.online_win = win
        self.update_online_window()
        win.Show()

    def update_online_window(self):
        win = self.online_win
        if not win:
            return
        rows, missing = [], False
        for i, (is_local, ip, _t) in enumerate(self.state.online_devices(), 1):
            if is_local:
                name = f"{socket.gethostname()} (komputer ini)"
                mac = "-"
            else:
                name = self.state.name_of(ip)
                m = self.state.mac_cached(ip)
                mac = m.upper() if m else "-"
                missing = missing or not m
            rows.append((str(i), name, ip, mac))
        if (missing and self.state.arp_age() > 3) or self.state.arp_age() > 10:
            self.state.refresh_arp_async()
        if self.runner.running():
            note = f"Online : {len(rows)} device  (aktif dalam {ONLINE_WINDOW} detik terakhir)"
        else:
            note = "Server OFF"
        win.set_rows(rows, note)

    # ---- actions
    def commit(self):
        c = self.state.cfg
        c["mode"] = self.rb_mode.GetSelection()
        c["port"] = self.sp_port.GetValue()
        c["www"] = self.tc_www.GetValue().strip() or str(BASE_DIR / "www")
        c["lan"] = self.cb_lan.GetValue()
        c["pla_enabled"] = self.cb_pla.GetValue()
        c["pla_lan"] = self.cb_pla_lan.GetValue()
        c["pla_debug"] = self.cb_pla_debug.GetValue()
        c["pla_dir"] = self.tc_pla_dir.GetValue().strip() or str(BASE_DIR / "databases")
        pw = self.tc_pla_pw.GetValue().strip()
        if not pw and pla_installed():
            pw = gen_password()
            self.tc_pla_pw.SetValue(pw)
        c["pla_password"] = pw
        self.state.save()

    def toggle(self):
        if self.runner.running():
            self.runner.stop()
        else:
            self.commit()
            with wx.BusyCursor():
                try:
                    self.runner.start()
                except Exception as e:
                    wx.MessageBox(str(e), "Gagal start", wx.OK | wx.ICON_ERROR, self)
        self.refresh_ui()

    def autostart_server(self):
        try:
            self.runner.start()
        except Exception as e:
            self.append_log(f"Auto start gagal: {e}")
        self.refresh_ui()

    def browse_dir(self, tc, title):
        dlg = wx.DirDialog(self, title, tc.GetValue())
        if dlg.ShowModal() == wx.ID_OK:
            tc.SetValue(dlg.GetPath())
        dlg.Destroy()

    def on_import_php(self, _):
        if self.runner.running():
            wx.MessageBox("Hentikan server dulu sebelum update PHP.", "Info", wx.OK, self)
            return
        dlg = wx.FileDialog(self, "Pilih ZIP PHP (mis. dari windows.php.net)",
                            wildcard="ZIP (*.zip)|*.zip", style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            try:
                with wx.BusyCursor():
                    import_php_zip(path)
                    self.php_info = php_version()
                self.refresh_ui()
                wx.MessageBox(f"PHP berhasil diimport:\n{self.php_info}", "Sukses", wx.OK | wx.ICON_INFORMATION, self)
            except Exception as e:
                wx.MessageBox(str(e), "Gagal import", wx.OK | wx.ICON_ERROR, self)
        dlg.Destroy()

    def on_import_pla(self, _):
        if self.runner.running():
            wx.MessageBox("Hentikan server dulu sebelum import phpLiteAdmin.", "Info", wx.OK, self)
            return
        dlg = wx.FileDialog(self, "Pilih ZIP phpLiteAdmin (dari phpliteadmin.org)",
                            wildcard="phpLiteAdmin (*.zip;*.php)|*.zip;*.php",
                            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            try:
                with wx.BusyCursor():
                    import_pla(path)
                    self.pla_info = pla_version()
                if not self.tc_pla_pw.GetValue().strip():
                    self.tc_pla_pw.SetValue(gen_password())
                self.commit()
                self.refresh_ui()
                msg = (f"phpLiteAdmin berhasil diimport ({self.pla_info}).\n\n"
                       f"Alamat   : http://localhost:{self.state.cfg['port']}{PLA_PREFIX}\n"
                       f"Password : {self.state.cfg['pla_password']}\n"
                       f"Folder DB: {self.state.cfg['pla_dir']}\n\n"
                       "Klik 'Start Server' untuk mulai memakainya.")
                if not find_php():
                    msg += "\n\nCatatan: PHP belum diimport. phpLiteAdmin butuh PHP."
                wx.MessageBox(msg, "Sukses", wx.OK | wx.ICON_INFORMATION, self)
            except Exception as e:
                wx.MessageBox(str(e), "Gagal import", wx.OK | wx.ICON_ERROR, self)
        dlg.Destroy()

    def on_autostart_os(self, _):
        want = self.cb_os.GetValue()
        try:
            set_autostart(want)
        except Exception as e:
            self.cb_os.SetValue(not want)
            wx.MessageBox(f"Gagal mengubah autostart:\n{e}", "Error", wx.OK | wx.ICON_ERROR, self)
            return
        self.state.cfg["autostart_os"] = want
        self.state.save()

    def on_autostart_srv(self, _):
        self.state.cfg["autostart_server"] = self.cb_srv.GetValue()
        self.state.save()

    def refresh_ui(self):
        st = self.state
        run = self.runner.running()
        self.btn_start.SetLabel("Stop Server" if run else "Start Server")
        for w in (self.rb_mode, self.sp_port, self.tc_www, self.btn_browse, self.cb_lan,
                  self.btn_import, self.btn_import_pla, self.cb_pla, self.cb_pla_lan, self.cb_pla_debug,
                  self.tc_pla_pw, self.tc_pla_dir, self.btn_pla_dir):
            w.Enable(not run)
        self.btn_dbadmin.Enable(bool(run and st.pla_port))
        self.lbl_php.SetLabel(self.php_info or "Belum ada  (klik Import PHP dari ZIP)")
        self.lbl_pla.SetLabel(f"Terpasang ({self.pla_info})" if self.pla_info
                              else "Belum ada  (klik Import phpLiteAdmin dari ZIP)")
        self.lbl_cfg_note.SetLabel(
            "Catatan: server sedang berjalan. Klik 'Stop Server' dulu untuk mengubah konfigurasi."
            if run else
            "Perubahan konfigurasi diterapkan saat 'Start Server' diklik.")
        if run:
            port = st.cfg["port"]
            lines = [f"Berjalan:  http://localhost:{port}"]
            if st.cfg["lan"]:
                lines += [f"LAN     :  http://{ip}:{port}" for ip in local_ips()]
            if st.pla_port:
                lines.append(f"DB Admin:  http://localhost:{port}{PLA_PREFIX}")
            elif st.cfg["pla_enabled"] and pla_installed() and st.cfg["mode"] in (0, 2):
                lines.append("DB Admin:  tidak aktif (cek tab Log)")
            self.lbl_url.SetLabel("\n".join(lines))
        else:
            self.lbl_url.SetLabel("Server mati.")
        self.update_status()
        self.lbl_url.GetParent().GetParent().Layout()   # tab Server
        self.lbl_cfg_note.GetParent().Layout()          # tab Konfigurasi
        self.main_panel.Layout()

    # ---- window mgmt
    def show_window(self):
        self.Show()
        self.Iconize(False)
        self.Raise()

    def on_close(self, event):
        if event.CanVeto():
            if self.online_win:
                self.online_win.Close()
            self.Hide()          # tetap jalan di tray
            event.Veto()
        else:
            self.quit()

    def quit(self):
        self.timer.Stop()
        self.state.logcb = lambda m: None
        if self.online_win:
            self.online_win.Destroy()
            self.online_win = None
        self.runner.stop()
        self.tray.RemoveIcon()
        self.tray.Destroy()
        self.Destroy()


def main():
    app = wx.App(False)
    checker = wx.SingleInstanceChecker(f"{APP_NAME}-{wx.GetUserId()}")
    if checker.IsAnotherRunning():
        wx.MessageBox("Aplikasi sudah berjalan (cek tray icon).", "PHP Portable Server")
        return
    state = State()
    runner = Runner(state)
    frame = MainFrame(state, runner)
    state.logcb = lambda m: wx.CallAfter(frame.append_log, m)
    if "--minimized" not in sys.argv:
        frame.Show()
    if state.cfg["autostart_server"]:
        wx.CallAfter(frame.autostart_server)
    app.MainLoop()
    del checker


if __name__ == "__main__":
    main()
