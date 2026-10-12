# -*- coding: utf-8 -*-
"""
Auto Klik Deteksi Layar - Versi 9
Windows 10 + wxPython + MSS + Tesseract OCR + OpenCV
"""

import io
import os
import re
import sys
import json
import time
import base64
import ctypes
import threading
import traceback

# ==================================================
# DPI AWARE (HARUS SEBELUM IMPORT wx / pyautogui)
# ==================================================
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import wx
import mss
import pytesseract
import pyautogui

from PIL import Image, ImageOps, ImageChops, ImageStat, ImageEnhance
from pytesseract import Output

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except Exception:
    cv2 = None
    np = None
    HAS_CV2 = False


# ==================================================
# KONFIGURASI
# ==================================================

APP_NAME = "Auto Klik Deteksi Layar V9 by Samman Media"

TESSERACT_PATH = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

# Jeda pemeriksaan layar dalam detik
SCAN_INTERVAL = 0.5

# Batas minimum keyakinan OCR
MIN_CONFIDENCE = 20

# Pembesaran gambar sebelum OCR (teks kecil jadi lebih terbaca)
OCR_SCALE = 2

# ---------- KECEPATAN AKSI (kecilkan = makin cepat) ----------

# Lama gerak mouse menuju target (detik)
MOVE_DURATION = 0.15

# Jeda setelah target ditemukan sebelum mouse bergerak
FOUND_DELAY = 0.1

# Jeda setelah mouse sampai sebelum klik
BEFORE_CLICK_DELAY = 0.05

# Jeda setelah klik sebelum mengetik
BEFORE_TYPE_DELAY = 0.1

# Jeda sebelum LANGSUNG KETIK
DIRECT_TYPE_DELAY = 0.3

# Jeda sebelum menekan Enter
BEFORE_ENTER_DELAY = 0.1

# Jeda antar tahap (menunggu layar berubah)
STEP_DELAY = 0.6

# Jeda tunggu jendela selesai ter-minimize
MINIMIZE_DELAY = 0.8

# Kecepatan mengetik (detik per huruf)
TYPE_INTERVAL = 0.01

# ---------- SCROLL ----------

# Besar satu langkah scroll (120 = satu notch roda mouse)
SCROLL_CLICKS = 120

# Jeda setelah scroll sebelum layar diperiksa (detik)
SCROLL_DELAY = 0.5

# Batas jumlah langkah scroll per arah (pengaman halaman tanpa ujung)
SCROLL_MAX_STEPS = 150

# Layar dianggap tidak berubah jika selisih rata-rata di bawah angka ini
SCROLL_SAME_THRESHOLD = 0.8

# ---------- PUTAR ULANG ----------

# Jeda antar putaran (detik)
REPEAT_DELAY = 1.0

# ---------- SCREENSHOT + KLIK ----------

# Tingkat kemiripan minimal (0.0 - 1.0). Turunkan jika sulit ketemu.
SHOT_THRESHOLD = 0.80

# Ukuran gambar yang dicoba saat mencocokkan (1.0 = ukuran asli)
SHOT_SCALES = (1.0, 0.9, 1.1, 0.8, 1.25, 0.75, 1.5)

# Jeda sebelum layar screenshot muncul (agar jendela ini sudah hilang)
SHOT_PREPARE_DELAY = 400

# File gambar untuk memeriksa apa yang dilihat program
DEBUG_FILE = "debug_layar.png"

# File konfigurasi (di folder yang sama dengan script)
CONFIG_NAME = "AutoKlik.cfg"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, CONFIG_NAME)

SHOT_METHOD = "SCREENSHOT + KLIK"

METHODS = [
    "KLIK",
    "KLIK GANDA",
    "KETIK",
    "LANGSUNG KETIK",
    "SCROLL",
    SHOT_METHOD,
]

# Metode yang membutuhkan isian "Teks yang diketik"
TYPING_METHODS = ("KETIK", "LANGSUNG KETIK")

# Metode yang tidak memakai kata deteksi
NO_KEYWORD_METHODS = ("LANGSUNG KETIK", SHOT_METHOD)

# Jumlah tahap bawaan yang tidak bisa dihapus
DEFAULT_STEPS = 3

# Teks tombol screenshot
LABEL_READY = "READY SCREENSHOOT"
LABEL_CANCEL = "BATAL SCREENSHOOT"
LABEL_OK = "OK"
LABEL_RETRY = "ULANGI SCREENSHOOT"

pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True


# ==================================================
# BANTUAN GAMBAR
# ==================================================

def pil_to_bitmap(image):
    """PIL Image -> wx.Bitmap."""
    image = image.convert("RGB")

    return wx.Bitmap.FromBuffer(
        image.width,
        image.height,
        image.tobytes()
    )


def image_to_b64(image):
    """PIL Image -> teks Base64 (PNG), untuk disimpan di .cfg."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", optimize=True)

    return base64.b64encode(buffer.getvalue()).decode("ascii")


def b64_to_image(text):
    """Teks Base64 -> PIL Image."""
    raw = base64.b64decode(text.encode("ascii"))

    return Image.open(io.BytesIO(raw)).convert("RGB")


# ==================================================
# BANTUAN DELAY
# ==================================================

def parse_delay(text):
    """
    Ubah teks delay menjadi detik.
      1:15:00 -> 1 jam 15 menit 0 detik
      15:00   -> 15 menit 0 detik
      15      -> 15 detik
    Mengembalikan None jika format tidak valid.
    """
    text = text.strip()

    if not text:
        return None

    parts = text.split(":")

    if len(parts) > 3:
        return None

    numbers = []

    for part in parts:
        part = part.strip()

        if not re.fullmatch(r"[0-9]+", part):
            return None

        numbers.append(int(part))

    # Menit dan detik (selain angka paling depan) harus 0-59.
    for number in numbers[1:]:
        if number >= 60:
            return None

    seconds = 0

    for number in numbers:
        seconds = seconds * 60 + number

    return seconds


def format_hms(total_seconds):
    """Detik -> teks jam:menit:detik."""
    total_seconds = int(total_seconds)

    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    return f"{hours}:{minutes:02d}:{seconds:02d}"


# ==================================================
# LAYAR SCREENSHOT (KURSOR +, KLIK, DRAG, LEPAS)
# ==================================================

class ScreenshotOverlay(wx.Frame):

    def __init__(self, image, monitor, callback):
        super().__init__(
            None,
            style=(
                wx.FRAME_NO_TASKBAR
                | wx.STAY_ON_TOP
                | wx.BORDER_NONE
            )
        )

        self.image = image
        self.callback = callback

        self.start = None
        self.current = None
        self.mouse = wx.Point(-1, -1)
        self.dragging = False
        self.finished = False

        self.cache_size = None
        self.orig_bitmap = None
        self.dark_bitmap = None

        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_CROSS))

        self.SetPosition(
            wx.Point(monitor["left"], monitor["top"])
        )
        self.SetSize(
            wx.Size(monitor["width"], monitor["height"])
        )

        self.Bind(wx.EVT_PAINT, self.on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self.on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self.on_left_up)
        self.Bind(wx.EVT_MOTION, self.on_motion)
        self.Bind(wx.EVT_RIGHT_DOWN, self.on_right_down)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_key)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    # ---------- gambar latar ----------

    def ensure_bitmaps(self):
        width, height = self.GetClientSize()

        if (width, height) == self.cache_size:
            return

        if width <= 0 or height <= 0:
            return

        display = self.image

        if display.size != (width, height):
            display = display.resize(
                (width, height),
                Image.BILINEAR
            )

        self.orig_bitmap = pil_to_bitmap(display)

        dark = ImageEnhance.Brightness(display).enhance(0.55)
        self.dark_bitmap = pil_to_bitmap(dark)

        self.cache_size = (width, height)

    def selection_rect(self):
        if self.start is None or self.current is None:
            return None

        x1 = min(self.start.x, self.current.x)
        y1 = min(self.start.y, self.current.y)
        x2 = max(self.start.x, self.current.x)
        y2 = max(self.start.y, self.current.y)

        return wx.Rect(x1, y1, x2 - x1, y2 - y1)

    # ---------- menggambar ----------

    def on_paint(self, event):
        dc = wx.AutoBufferedPaintDC(self)

        self.ensure_bitmaps()

        if self.dark_bitmap is None:
            return

        width, height = self.GetClientSize()

        dc.DrawBitmap(self.dark_bitmap, 0, 0)

        rect = self.selection_rect()

        if rect is not None and rect.width > 0 and rect.height > 0:
            # Batasi agar tidak keluar dari bitmap.
            x = max(0, min(rect.x, width - 1))
            y = max(0, min(rect.y, height - 1))
            w = max(1, min(rect.width, width - x))
            h = max(1, min(rect.height, height - y))

            safe = wx.Rect(x, y, w, h)

            try:
                sub = self.orig_bitmap.GetSubBitmap(safe)
                dc.DrawBitmap(sub, x, y)
            except Exception:
                pass

            dc.SetBrush(wx.TRANSPARENT_BRUSH)
            dc.SetPen(wx.Pen(wx.Colour(255, 60, 60), 2))
            dc.DrawRectangle(safe)

            # Ukuran area
            sx = self.image.width / float(width)
            sy = self.image.height / float(height)
            text = f"{int(w * sx)} x {int(h * sy)}"

            dc.SetTextForeground(wx.Colour(255, 255, 255))
            dc.SetTextBackground(wx.Colour(0, 0, 0))
            dc.SetBackgroundMode(wx.SOLID)
            dc.DrawText(text, x, max(0, y - 20))

        # Kursor + besar (garis silang memanjang satu layar)
        if self.mouse.x >= 0:
            dc.SetPen(wx.Pen(wx.Colour(255, 60, 60), 1))
            dc.DrawLine(0, self.mouse.y, width, self.mouse.y)
            dc.DrawLine(self.mouse.x, 0, self.mouse.x, height)

        # Petunjuk
        dc.SetBackgroundMode(wx.SOLID)
        dc.SetTextForeground(wx.Colour(255, 255, 255))
        dc.SetTextBackground(wx.Colour(0, 0, 0))
        dc.DrawText(
            " Klik, drag, lalu lepas untuk screenshot."
            "  ESC / klik kanan = batal ",
            10, 10
        )

    # ---------- mouse dan keyboard ----------

    def on_left_down(self, event):
        position = event.GetPosition()

        self.start = position
        self.current = position
        self.mouse = position
        self.dragging = True

        if not self.HasCapture():
            self.CaptureMouse()

        self.Refresh(False)

    def on_motion(self, event):
        position = event.GetPosition()
        self.mouse = position

        if self.dragging:
            self.current = position

        self.Refresh(False)

    def on_left_up(self, event):
        if not self.dragging:
            return

        self.dragging = False

        if self.HasCapture():
            self.ReleaseMouse()

        self.current = event.GetPosition()

        rect = self.selection_rect()

        # Area terlalu kecil dianggap salah klik.
        if rect is None or rect.width < 6 or rect.height < 6:
            self.start = None
            self.current = None
            self.Refresh(False)
            return

        width, height = self.GetClientSize()

        sx = self.image.width / float(width)
        sy = self.image.height / float(height)

        left = max(0, int(rect.x * sx))
        top = max(0, int(rect.y * sy))
        right = min(self.image.width, int((rect.x + rect.width) * sx))
        bottom = min(self.image.height, int((rect.y + rect.height) * sy))

        if right - left < 2 or bottom - top < 2:
            self.start = None
            self.current = None
            self.Refresh(False)
            return

        result = self.image.crop((left, top, right, bottom)).copy()

        self.finish(result)

    def on_right_down(self, event):
        self.finish(None)

    def on_key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.finish(None)
        else:
            event.Skip()

    def on_close(self, event):
        self.finish(None)

    def finish(self, result):
        if self.finished:
            return

        self.finished = True

        try:
            if self.HasCapture():
                self.ReleaseMouse()
        except Exception:
            pass

        callback = self.callback

        self.Hide()
        self.Destroy()

        wx.CallAfter(callback, result)


# ==================================================
# APLIKASI UTAMA
# ==================================================

class AutoKlikDeteksiLayar(wx.Frame):

    HOTKEY_ID = 9001

    def __init__(self):
        super().__init__(
            None,
            title=APP_NAME,
            size=(860, 840)
        )

        self.running = False
        self.stop_event = threading.Event()
        self.worker = None
        self.rows = []

        self.overlay = None
        self.capture_row = None

        self.build_ui()
        self.load_config_startup()
        self.Centre()
        self.Show()

        # F9 = berhenti, walau jendela sedang di-minimize
        try:
            self.RegisterHotKey(
                self.HOTKEY_ID, wx.MOD_NONE, wx.WXK_F9
            )
            self.Bind(
                wx.EVT_HOTKEY, self.on_hotkey,
                id=self.HOTKEY_ID
            )
        except Exception:
            pass

    # ==================================================
    # MEMBUAT TAMPILAN
    # ==================================================

    def build_ui(self):
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(
            panel,
            label=APP_NAME
        )

        font = title.GetFont()
        font.SetPointSize(16)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        title.SetFont(font)

        root.Add(title, 0, wx.ALL, 12)

        info = wx.StaticText(
            panel,
            label=(
                "Deteksi tulisan pada layar dan jalankan "
                "aksi secara berurutan.\n"
                "Saat jalan, jendela ini otomatis di-minimize. "
                "Tekan F9 untuk berhenti.\n"
                "LANGSUNG KETIK = langsung mengetik tanpa "
                "mendeteksi kata.  "
                "SCROLL = scroll pelan (bawah dulu, lalu atas) "
                "sampai kata ditemukan.\n"
                "SCREENSHOT + KLIK = cari gambar hasil screenshot "
                "di layar lalu klik.  "
                "Tekan Enter = otomatis menekan Enter "
                "setelah aksi tahap itu.\n"
                "Idupin Delay = tahap menunggu dulu sebelum "
                "dijalankan (contoh: 1:15:00, 15:00, atau 15).  "
                "Putar Ulang = mengulang dari tahap 1."
            )
        )
        root.Add(
            info, 0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM, 12
        )

        # Daerah daftar tahap (bisa di-scroll)
        self.scroll = wx.ScrolledWindow(
            panel,
            style=wx.VSCROLL
        )
        self.scroll.SetScrollRate(0, 20)
        self.scroll.SetBackgroundColour(wx.Colour(255, 255, 255))

        self.steps_sizer = wx.BoxSizer(wx.VERTICAL)
        self.scroll.SetSizer(self.steps_sizer)

        root.Add(
            self.scroll, 1,
            wx.EXPAND | wx.LEFT | wx.RIGHT, 4
        )

        for _ in range(DEFAULT_STEPS):
            self.add_step()

        # Tombol tambah tahap dan pilih config
        tools = wx.BoxSizer(wx.HORIZONTAL)

        self.btn_add = wx.Button(
            panel,
            label="+ TAMBAH TAHAP",
            size=(200, 32)
        )

        self.btn_config = wx.Button(
            panel,
            label="PILIH CONFIG",
            size=(200, 32)
        )

        tools.Add(self.btn_add, 0, wx.RIGHT, 10)
        tools.Add(self.btn_config, 0)

        root.Add(
            tools, 0,
            wx.ALIGN_CENTER | wx.TOP, 8
        )

        buttons = wx.BoxSizer(wx.HORIZONTAL)

        self.btn_run = wx.Button(
            panel,
            label="JALANKAN",
            size=(150, 40)
        )

        self.btn_stop = wx.Button(
            panel,
            label="BERHENTI",
            size=(150, 40)
        )

        self.btn_stop.Disable()

        self.btn_save = wx.Button(
            panel,
            label="SAVE CONFIG",
            size=(150, 40)
        )

        # Dua centang ditumpuk: Save Konfigurasi di atas,
        # Putar Ulang di bawahnya.
        checks = wx.BoxSizer(wx.VERTICAL)

        self.chk_save = wx.CheckBox(
            panel,
            label="Save Konfigurasi"
        )

        self.chk_repeat = wx.CheckBox(
            panel,
            label="Putar Ulang"
        )

        checks.Add(self.chk_save, 0, wx.BOTTOM, 6)
        checks.Add(self.chk_repeat, 0)

        buttons.Add(self.btn_run, 0, wx.RIGHT, 10)
        buttons.Add(self.btn_stop, 0, wx.RIGHT, 10)
        buttons.Add(self.btn_save, 0, wx.RIGHT, 16)
        buttons.Add(
            checks, 0,
            wx.ALIGN_CENTER_VERTICAL
        )

        root.Add(
            buttons, 0,
            wx.ALIGN_CENTER | wx.ALL, 12
        )

        self.status = wx.StaticText(
            panel,
            label="Status: Siap"
        )

        root.Add(
            self.status, 0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
            12
        )

        panel.SetSizer(root)

        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add_step)
        self.btn_config.Bind(wx.EVT_BUTTON, self.on_pick_config)
        self.btn_run.Bind(wx.EVT_BUTTON, self.start)
        self.btn_stop.Bind(wx.EVT_BUTTON, self.stop)
        self.btn_save.Bind(wx.EVT_BUTTON, self.on_save_config)
        self.Bind(wx.EVT_CLOSE, self.on_close)

    # ==================================================
    # WARNA BELANG (ABU-ABU TIPIS DAN PUTIH)
    # ==================================================

    @staticmethod
    def paint_widget(widget, colour):
        widget.SetBackgroundColour(colour)

        for child in widget.GetChildren():
            # Kolom isian dan tombol dibiarkan tampil bawaan.
            if isinstance(
                child, (wx.TextCtrl, wx.Choice, wx.Button)
            ):
                continue

            AutoKlikDeteksiLayar.paint_widget(child, colour)

    def apply_row_color(self, row, index):
        if index % 2 == 0:
            colour = wx.Colour(228, 228, 228)
        else:
            colour = wx.Colour(255, 255, 255)

        self.paint_widget(row["panel"], colour)
        row["panel"].Refresh()

    # ==================================================
    # TAMBAH / HAPUS TAHAP
    # ==================================================

    def add_step(self, scroll_to_end=False):
        number = len(self.rows) + 1

        step_panel = wx.Panel(self.scroll)

        box = wx.StaticBoxSizer(
            wx.VERTICAL,
            step_panel,
            f"Deteksi klik ke-{number}"
        )

        parent = box.GetStaticBox()

        grid = wx.FlexGridSizer(
            rows=0,
            cols=2,
            vgap=8,
            hgap=10
        )

        grid.AddGrowableCol(1, 1)

        keyword = wx.TextCtrl(parent)

        method = wx.Choice(
            parent,
            choices=METHODS
        )
        method.SetSelection(0)

        typing = wx.TextCtrl(parent)

        enabled = wx.CheckBox(
            parent,
            label="Aktifkan tahap ini"
        )

        enter = wx.CheckBox(
            parent,
            label="Tekan Enter"
        )

        # ----- Delay -----
        delay_check = wx.CheckBox(
            parent,
            label="Idupin Delay"
        )

        delay_text = wx.TextCtrl(
            parent,
            size=(110, -1)
        )

        try:
            delay_text.SetHint("1:15:00")
        except Exception:
            pass

        delay_text.SetToolTip(
            "Contoh: 1:15:00 = 1 jam 15 menit\n"
            "15:00 = 15 menit\n"
            "15 = 15 detik"
        )

        # ----- Bagian screenshot (di bawah dropdown metode) -----
        shot_label = wx.StaticText(parent, label="Screenshot:")

        shot_panel = wx.Panel(parent)
        shot_sizer = wx.BoxSizer(wx.VERTICAL)

        shot_buttons = wx.BoxSizer(wx.HORIZONTAL)

        btn1 = wx.Button(
            shot_panel,
            label=LABEL_READY,
            size=(190, 28)
        )

        btn2 = wx.Button(
            shot_panel,
            label=LABEL_CANCEL,
            size=(190, 28)
        )

        shot_buttons.Add(btn1, 0, wx.RIGHT, 8)
        shot_buttons.Add(btn2, 0)

        thumb = wx.StaticBitmap(shot_panel, bitmap=wx.Bitmap(1, 1))
        shot_text = wx.StaticText(shot_panel, label="")

        shot_info = wx.BoxSizer(wx.HORIZONTAL)
        shot_info.Add(
            thumb, 0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10
        )
        shot_info.Add(shot_text, 0, wx.ALIGN_CENTER_VERTICAL)

        shot_sizer.Add(shot_buttons, 0)
        shot_sizer.Add(shot_info, 0, wx.TOP, 6)

        shot_panel.SetSizer(shot_sizer)

        delete_button = None

        if number == 1:
            keyword.SetValue("KIRIM")
            enabled.SetValue(True)
            enabled.Disable()
        elif number <= DEFAULT_STEPS:
            enabled.SetValue(False)
        else:
            # Tahap tambahan langsung aktif.
            enabled.SetValue(True)
            delete_button = wx.Button(
                parent,
                label="HAPUS TAHAP INI"
            )

        grid.Add(
            wx.StaticText(parent, label="Kata deteksi:"),
            0, wx.ALIGN_CENTER_VERTICAL
        )
        grid.Add(keyword, 1, wx.EXPAND)

        grid.Add(
            wx.StaticText(parent, label="Metode:"),
            0, wx.ALIGN_CENTER_VERTICAL
        )
        grid.Add(method, 1, wx.EXPAND)

        grid.Add(shot_label, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(shot_panel, 1, wx.EXPAND)

        grid.Add(
            wx.StaticText(parent, label="Teks yang diketik:"),
            0, wx.ALIGN_CENTER_VERTICAL
        )
        grid.Add(typing, 1, wx.EXPAND)

        # Baris pilihan: aktifkan, enter, [kanan] delay, hapus.
        options = wx.BoxSizer(wx.HORIZONTAL)

        options.Add(
            enabled, 0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16
        )
        options.Add(
            enter, 0,
            wx.ALIGN_CENTER_VERTICAL
        )

        options.AddStretchSpacer(1)

        options.Add(
            delay_check, 0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6
        )
        options.Add(
            delay_text, 0,
            wx.ALIGN_CENTER_VERTICAL
        )

        if delete_button is not None:
            options.Add(
                delete_button, 0,
                wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 12
            )

        grid.Add(
            wx.StaticText(parent, label="Pilihan tahap:"),
            0, wx.ALIGN_CENTER_VERTICAL
        )
        grid.Add(options, 1, wx.EXPAND)

        # Bagian screenshot disembunyikan sampai metodenya dipilih.
        grid.Hide(shot_label)
        grid.Hide(shot_panel)

        box.Add(grid, 1, wx.EXPAND | wx.ALL, 8)

        step_panel.SetSizer(box)

        self.steps_sizer.Add(
            step_panel, 0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM,
            8
        )

        row = {
            "panel": step_panel,
            "box": box,
            "grid": grid,
            "keyword": keyword,
            "method": method,
            "typing": typing,
            "enabled": enabled,
            "enter": enter,
            "delay_check": delay_check,
            "delay_text": delay_text,
            "delete": delete_button,
            "shot_label": shot_label,
            "shot_panel": shot_panel,
            "shot_info": shot_info,
            "btn1": btn1,
            "btn2": btn2,
            "thumb": thumb,
            "shot_text": shot_text,
            "image": None,
            "shot_state": "idle",
            "shot_visible": False,
        }

        self.rows.append(row)

        enabled.Bind(
            wx.EVT_CHECKBOX,
            lambda event, r=row: self.refresh_row(r)
        )

        delay_check.Bind(
            wx.EVT_CHECKBOX,
            lambda event, r=row: self.refresh_row(r)
        )

        method.Bind(
            wx.EVT_CHOICE,
            lambda event, r=row: self.refresh_row(r)
        )

        btn1.Bind(
            wx.EVT_BUTTON,
            lambda event, r=row: self.on_shot_btn1(r)
        )

        btn2.Bind(
            wx.EVT_BUTTON,
            lambda event, r=row: self.on_shot_btn2(r)
        )

        if delete_button is not None:
            delete_button.Bind(
                wx.EVT_BUTTON,
                lambda event, r=row: self.remove_step(r)
            )

        self.refresh_row(row)
        self.apply_row_color(row, number - 1)
        self.relayout()

        if scroll_to_end:
            wx.CallAfter(self.scroll_to_end)

        return row

    def remove_step(self, row):
        if self.running or self.overlay is not None:
            return

        if row not in self.rows:
            return

        self.rows.remove(row)
        self.steps_sizer.Detach(row["panel"])
        row["panel"].Destroy()

        # Urutkan ulang nomor dan warna belang.
        for index, item in enumerate(self.rows):
            item["box"].GetStaticBox().SetLabel(
                f"Deteksi klik ke-{index + 1}"
            )
            self.apply_row_color(item, index)

        self.relayout()

    def on_add_step(self, event):
        if self.running:
            return

        self.add_step(scroll_to_end=True)

    def relayout(self):
        self.steps_sizer.Layout()
        self.scroll.FitInside()
        self.scroll.Layout()

    def relayout_row(self, row):
        row["shot_panel"].Layout()
        row["panel"].InvalidateBestSize()
        row["panel"].Layout()
        self.relayout()

    def scroll_to_end(self):
        try:
            self.scroll.Scroll(
                0,
                self.scroll.GetScrollRange(wx.VERTICAL)
            )
        except Exception:
            pass

    # ==================================================
    # KONTROL INPUT
    # ==================================================

    def refresh_row(self, row):
        """Atur kolom mana yang aktif sesuai tahap dan metode."""
        active = row["enabled"].GetValue()
        method = row["method"].GetStringSelection()

        # LANGSUNG KETIK dan SCREENSHOT + KLIK tidak memakai
        # kata deteksi. SCROLL tetap butuh kata deteksi.
        row["keyword"].Enable(
            active and method not in NO_KEYWORD_METHODS
        )

        row["method"].Enable(active)

        row["typing"].Enable(
            active and method in TYPING_METHODS
        )

        row["enter"].Enable(active)

        # Delay: kolom isian hanya hidup jika "Idupin Delay" dicentang.
        row["delay_check"].Enable(active)

        row["delay_text"].Enable(
            active and row["delay_check"].GetValue()
        )

        # Tampilkan bagian screenshot hanya untuk metodenya.
        show_shot = (method == SHOT_METHOD)

        if show_shot != row["shot_visible"]:
            row["shot_visible"] = show_shot
            row["grid"].Show(row["shot_label"], show_shot)
            row["grid"].Show(row["shot_panel"], show_shot)
            self.relayout_row(row)

        self.update_shot_ui(row)

    def set_status(self, message):
        wx.CallAfter(
            self.status.SetLabel,
            "Status: " + str(message)
        )

    def on_hotkey(self, event):
        self.stop()

    # ==================================================
    # SCREENSHOT: TOMBOL DAN STATUSNYA
    # ==================================================

    def update_shot_ui(self, row):
        """Atur tulisan/aktif tombol dan gambar kecil sesuai status."""
        state = row["shot_state"]
        image = row["image"]
        active = row["enabled"].GetValue()

        if state == "pending":
            # Baru di-screenshot, menunggu OK atau ULANGI.
            row["btn1"].SetLabel(LABEL_OK)
            row["btn2"].SetLabel(LABEL_RETRY)
            row["btn1"].Enable(active)
            row["btn2"].Enable(active)
        elif state == "confirmed":
            row["btn1"].SetLabel(LABEL_READY)
            row["btn2"].SetLabel(LABEL_CANCEL)
            row["btn1"].Enable(active)
            row["btn2"].Enable(active)
        else:
            row["btn1"].SetLabel(LABEL_READY)
            row["btn2"].SetLabel(LABEL_CANCEL)
            row["btn1"].Enable(active)
            row["btn2"].Enable(False)

        if image is not None:
            small = image.copy()
            small.thumbnail((200, 60))

            row["thumb"].SetBitmap(pil_to_bitmap(small))
            row["shot_info"].Show(row["thumb"], True)

            text = f"Gambar: {image.width} x {image.height} px"

            if state == "pending":
                text += "  (tekan OK untuk mengunci)"

            row["shot_text"].SetLabel(text)
        else:
            row["shot_info"].Show(row["thumb"], False)
            row["shot_text"].SetLabel("Belum ada screenshot.")

        if row["shot_visible"]:
            self.relayout_row(row)

    def on_shot_btn1(self, row):
        """READY SCREENSHOOT (atau OK jika baru di-screenshot)."""
        if self.running:
            return

        if row["shot_state"] == "pending":
            row["shot_state"] = "confirmed"
            self.update_shot_ui(row)
            self.set_status(
                "Screenshot dikunci. Lanjut atur tahap berikutnya."
            )
            return

        self.begin_capture(row)

    def on_shot_btn2(self, row):
        """BATAL SCREENSHOOT (atau ULANGI jika baru di-screenshot)."""
        if self.running:
            return

        if row["shot_state"] == "pending":
            self.begin_capture(row)
            return

        if row["shot_state"] == "confirmed":
            row["image"] = None
            row["shot_state"] = "idle"
            self.update_shot_ui(row)
            self.set_status("Screenshot dihapus.")

    def begin_capture(self, row):
        if self.running or self.overlay is not None:
            return

        self.capture_row = row

        self.set_status("Menyiapkan screenshot...")

        # Sembunyikan jendela ini agar tidak ikut terpotret.
        self.Iconize(True)

        wx.CallLater(SHOT_PREPARE_DELAY, self.open_overlay)

    def open_overlay(self):
        try:
            with mss.mss() as sct:
                monitor = dict(sct.monitors[0])
                shot = sct.grab(monitor)

                image = Image.frombytes(
                    "RGB",
                    shot.size,
                    shot.rgb
                )
        except Exception as error:
            self.on_capture_done(None)

            wx.MessageBox(
                f"Gagal mengambil layar:\n{error}",
                APP_NAME,
                wx.OK | wx.ICON_ERROR
            )
            return

        self.overlay = ScreenshotOverlay(
            image,
            monitor,
            self.on_capture_done
        )

        self.overlay.Show()
        self.overlay.Raise()
        self.overlay.SetFocus()

    def on_capture_done(self, result):
        self.overlay = None

        self.Iconize(False)
        self.Raise()

        row = self.capture_row
        self.capture_row = None

        if row is None or row not in self.rows:
            return

        if result is None:
            self.set_status("Screenshot dibatalkan.")
            return

        row["image"] = result
        row["shot_state"] = "pending"

        self.update_shot_ui(row)

        self.set_status(
            f"Screenshot diambil ({result.width} x {result.height}). "
            f"Tekan OK atau ULANGI SCREENSHOOT."
        )

    # ==================================================
    # SIMPAN DAN MUAT KONFIGURASI (AutoKlik.cfg)
    # ==================================================

    def collect_config(self):
        steps = []

        for index, row in enumerate(self.rows):
            if row["image"] is not None:
                image_text = image_to_b64(row["image"])
            else:
                image_text = ""

            steps.append({
                "enabled": (
                    True if index == 0
                    else row["enabled"].GetValue()
                ),
                "keyword": row["keyword"].GetValue(),
                "method": row["method"].GetStringSelection(),
                "typing": row["typing"].GetValue(),
                "enter": row["enter"].GetValue(),
                "delay_on": row["delay_check"].GetValue(),
                "delay": row["delay_text"].GetValue(),
                "image": image_text,
            })

        return {
            "save_config": self.chk_save.GetValue(),
            "repeat": self.chk_repeat.GetValue(),
            "steps": steps,
        }

    @staticmethod
    def read_config_file(path):
        """Baca file .cfg. Mengembalikan dict atau None."""
        try:
            with open(path, "r", encoding="utf-8") as file:
                data = json.load(file)
        except Exception:
            traceback.print_exc()
            return None

        if not isinstance(data, dict):
            return None

        return data

    @staticmethod
    def write_config_file(path, data):
        """Tulis file .cfg. Mengembalikan True jika berhasil."""
        try:
            with open(path, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
            return True
        except Exception:
            traceback.print_exc()
            return False

    def save_config(self):
        """
        Dicentang   : simpan seluruh isian ke AutoKlik.cfg.
        Tidak dicentang: isi file tetap ada (bisa dipakai lewat
        PILIH CONFIG), hanya tidak dimuat otomatis saat dibuka.
        """
        if self.chk_save.GetValue():
            data = self.collect_config()
        else:
            if not os.path.isfile(CONFIG_FILE):
                return

            data = self.read_config_file(CONFIG_FILE)

            if data is None:
                return

            data["save_config"] = False

        self.write_config_file(CONFIG_FILE, data)

    def on_save_config(self, event):
        """Tombol SAVE CONFIG: simpan isian sekarang ke file .cfg."""
        dialog = wx.FileDialog(
            self,
            message="Simpan config",
            defaultDir=BASE_DIR,
            defaultFile=CONFIG_NAME,
            wildcard="File config (*.cfg)|*.cfg",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT
        )

        try:
            if dialog.ShowModal() != wx.ID_OK:
                return

            path = dialog.GetPath()
        finally:
            dialog.Destroy()

        if not path.lower().endswith(".cfg"):
            path += ".cfg"

        if self.write_config_file(path, self.collect_config()):
            self.set_status(
                "Config disimpan: " + os.path.basename(path)
            )
        else:
            wx.MessageBox(
                "Config gagal disimpan:\n\n" + path,
                APP_NAME,
                wx.OK | wx.ICON_ERROR
            )

    def apply_config(self, data):
        """Terapkan isi konfigurasi ke tampilan. True jika berhasil."""
        saved_steps = data.get("steps")

        if not isinstance(saved_steps, list) or not saved_steps:
            return False

        target = max(DEFAULT_STEPS, len(saved_steps))

        while len(self.rows) < target:
            self.add_step()

        while len(self.rows) > target:
            self.remove_step(self.rows[-1])

        self.chk_repeat.SetValue(bool(data.get("repeat", False)))

        for index, row in enumerate(self.rows):
            if index < len(saved_steps) and isinstance(
                saved_steps[index], dict
            ):
                saved = saved_steps[index]
            else:
                saved = {}

            row["keyword"].SetValue(str(saved.get("keyword", "")))
            row["typing"].SetValue(str(saved.get("typing", "")))
            row["enter"].SetValue(bool(saved.get("enter", False)))

            row["delay_check"].SetValue(
                bool(saved.get("delay_on", False))
            )
            row["delay_text"].SetValue(str(saved.get("delay", "")))

            method_index = row["method"].FindString(
                str(saved.get("method", "KLIK"))
            )

            if method_index == wx.NOT_FOUND:
                method_index = 0

            row["method"].SetSelection(method_index)

            # Gambar screenshot (tersimpan sebagai teks Base64).
            image_text = saved.get("image", "")

            row["image"] = None
            row["shot_state"] = "idle"

            if isinstance(image_text, str) and image_text:
                try:
                    row["image"] = b64_to_image(image_text)
                    row["shot_state"] = "confirmed"
                except Exception:
                    traceback.print_exc()

            if index == 0:
                row["enabled"].SetValue(True)
            else:
                row["enabled"].SetValue(
                    bool(saved.get("enabled", False))
                )

            self.refresh_row(row)

        self.relayout()
        return True

    def load_config_startup(self):
        """Saat aplikasi dibuka: muat AutoKlik.cfg jika Save dicentang."""
        if not os.path.isfile(CONFIG_FILE):
            return

        data = self.read_config_file(CONFIG_FILE)

        if data is None or not data.get("save_config"):
            return

        if self.apply_config(data):
            self.chk_save.SetValue(True)

    def on_pick_config(self, event):
        if self.running or self.overlay is not None:
            return

        dialog = wx.FileDialog(
            self,
            message="Pilih file config",
            defaultDir=BASE_DIR,
            defaultFile=CONFIG_NAME,
            wildcard="File config (*.cfg)|*.cfg",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST
        )

        try:
            if dialog.ShowModal() != wx.ID_OK:
                return

            path = dialog.GetPath()
        finally:
            dialog.Destroy()

        data = self.read_config_file(path)

        if data is None or not self.apply_config(data):
            wx.MessageBox(
                "File config tidak valid atau kosong:\n\n" + path,
                APP_NAME,
                wx.OK | wx.ICON_WARNING
            )
            return

        self.set_status(
            "Config dimuat: " + os.path.basename(path)
        )

    # ==================================================
    # VALIDASI DAN MULAI
    # ==================================================

    def start(self, event):
        if self.running or self.overlay is not None:
            return

        if not os.path.isfile(TESSERACT_PATH):
            wx.MessageBox(
                "Tesseract tidak ditemukan:\n\n"
                + TESSERACT_PATH,
                APP_NAME,
                wx.OK | wx.ICON_ERROR
            )
            return

        try:
            pytesseract.get_tesseract_version()
        except Exception as error:
            wx.MessageBox(
                f"Tesseract tidak dapat dijalankan:\n{error}",
                APP_NAME,
                wx.OK | wx.ICON_ERROR
            )
            return

        steps = []

        for index, row in enumerate(self.rows):
            # Tahap pertama selalu aktif.
            # Tahap lainnya hanya jika dicentang.
            if index > 0 and not row["enabled"].GetValue():
                continue

            keyword = row["keyword"].GetValue().strip()
            method = row["method"].GetStringSelection()
            typing = row["typing"].GetValue()
            press_enter = row["enter"].GetValue()

            # Metode tanpa kata deteksi tidak memerlukannya.
            if method not in NO_KEYWORD_METHODS and not keyword:
                wx.MessageBox(
                    f"Kata deteksi tahap {index + 1} kosong.",
                    APP_NAME,
                    wx.OK | wx.ICON_WARNING
                )
                return

            if method in TYPING_METHODS and not typing:
                wx.MessageBox(
                    f"Teks ketik tahap {index + 1} kosong.",
                    APP_NAME,
                    wx.OK | wx.ICON_WARNING
                )
                return

            if method == SHOT_METHOD:
                if not HAS_CV2:
                    wx.MessageBox(
                        "Metode SCREENSHOT + KLIK membutuhkan OpenCV.\n\n"
                        "Jalankan di CMD:\n"
                        "pip install opencv-python numpy",
                        APP_NAME,
                        wx.OK | wx.ICON_ERROR
                    )
                    return

                if row["image"] is None:
                    wx.MessageBox(
                        f"Screenshot tahap {index + 1} belum diambil.",
                        APP_NAME,
                        wx.OK | wx.ICON_WARNING
                    )
                    return

                # Screenshot yang belum ditekan OK dianggap OK.
                if row["shot_state"] == "pending":
                    row["shot_state"] = "confirmed"
                    self.update_shot_ui(row)

            # Delay hanya dipakai jika "Idupin Delay" dicentang.
            delay_seconds = 0

            if row["delay_check"].GetValue():
                delay_seconds = parse_delay(
                    row["delay_text"].GetValue()
                )

                if delay_seconds is None:
                    wx.MessageBox(
                        f"Delay tahap {index + 1} tidak valid.\n\n"
                        "Contoh yang benar:\n"
                        "1:15:00  (1 jam 15 menit)\n"
                        "15:00  (15 menit)\n"
                        "15  (15 detik)",
                        APP_NAME,
                        wx.OK | wx.ICON_WARNING
                    )
                    return

            steps.append({
                "keyword": keyword,
                "method": method,
                "typing": typing,
                "enter": press_enter,
                "image": row["image"],
                "delay": delay_seconds,
                "number": index + 1,
            })

        repeat = self.chk_repeat.GetValue()

        # Simpan konfigurasi saat tombol JALANKAN ditekan.
        self.save_config()

        self.stop_event.clear()
        self.running = True

        self.btn_run.Disable()
        self.btn_add.Disable()
        self.btn_config.Disable()
        self.btn_stop.Enable()

        self.set_status(
            "Memulai... jendela di-minimize (F9 = berhenti)"
        )

        # Minimize supaya tulisan di jendela ini
        # tidak ikut terbaca OCR.
        self.Iconize(True)

        self.worker = threading.Thread(
            target=self.process_screen,
            args=(steps, repeat),
            daemon=True
        )
        self.worker.start()

    # ==================================================
    # OCR: MENCARI TULISAN
    # ==================================================

    @staticmethod
    def normalize_word(text):
        # Abaikan tanda baca di sekitar kata.
        return re.sub(
            r"^[^\w]+|[^\w]+$",
            "",
            text.casefold()
        )

    @staticmethod
    def prepare_variants(image):
        """Buat beberapa versi gambar agar OCR lebih mudah."""
        width, height = image.size

        gray = image.convert("L").resize(
            (width * OCR_SCALE, height * OCR_SCALE),
            Image.LANCZOS
        )

        # Versi normal dan versi negatif
        # (teks putih di latar gelap jadi lebih terbaca).
        return [gray, ImageOps.invert(gray)]

    def search_in_data(self, data, target_words):
        words = []

        for i, raw_word in enumerate(data["text"]):
            raw_word = raw_word.strip()

            if not raw_word:
                continue

            try:
                confidence = float(data["conf"][i])
            except (ValueError, TypeError):
                continue

            if confidence < MIN_CONFIDENCE:
                continue

            words.append({
                "text": self.normalize_word(raw_word),
                "x": int(data["left"][i]),
                "y": int(data["top"][i]),
                "w": int(data["width"][i]),
                "h": int(data["height"][i]),
                "block": data["block_num"][i],
                "paragraph": data["par_num"][i],
                "line": data["line_num"][i],
            })

        length = len(target_words)

        for i in range(len(words)):
            candidate = words[i:i + length]

            if len(candidate) != length:
                continue

            same_line = all(
                (
                    word["block"] == candidate[0]["block"]
                    and word["paragraph"] == candidate[0]["paragraph"]
                    and word["line"] == candidate[0]["line"]
                )
                for word in candidate
            )

            if not same_line:
                continue

            detected = [word["text"] for word in candidate]

            if length == 1:
                # Satu kata: boleh cocok sebagian
                # (misal "Brave" pada "Brave1" atau "Brave.exe").
                if target_words[0] not in detected[0]:
                    continue
            else:
                if detected != target_words:
                    continue

            left = min(word["x"] for word in candidate)
            top = min(word["y"] for word in candidate)
            right = max(
                word["x"] + word["w"] for word in candidate
            )
            bottom = max(
                word["y"] + word["h"] for word in candidate
            )

            return (
                left + (right - left) // 2,
                top + (bottom - top) // 2
            )

        return None

    def find_text(self, image, target):
        target_words = [
            self.normalize_word(word)
            for word in target.split()
        ]
        target_words = [word for word in target_words if word]

        if not target_words:
            return None

        for variant in self.prepare_variants(image):
            data = pytesseract.image_to_data(
                variant,
                output_type=Output.DICT,
                config="--psm 11"
            )

            position = self.search_in_data(data, target_words)

            if position is not None:
                # Kembalikan ke ukuran asli (bagi skala).
                return (
                    position[0] // OCR_SCALE,
                    position[1] // OCR_SCALE
                )

        return None

    # ==================================================
    # PENCOCOKAN GAMBAR (SCREENSHOT + KLIK)
    # ==================================================

    @staticmethod
    def find_image(screen_image, template_image):
        """
        Cari gambar template di layar dengan OpenCV.
        Mengembalikan titik tengah (x, y) atau None.
        """
        if not HAS_CV2:
            return None

        screen_gray = cv2.cvtColor(
            np.array(screen_image.convert("RGB")),
            cv2.COLOR_RGB2GRAY
        )

        template_gray = cv2.cvtColor(
            np.array(template_image.convert("RGB")),
            cv2.COLOR_RGB2GRAY
        )

        screen_h, screen_w = screen_gray.shape[:2]

        for scale in SHOT_SCALES:
            if scale == 1.0:
                scaled = template_gray
            else:
                interpolation = (
                    cv2.INTER_AREA if scale < 1.0
                    else cv2.INTER_LINEAR
                )

                scaled = cv2.resize(
                    template_gray,
                    None,
                    fx=scale,
                    fy=scale,
                    interpolation=interpolation
                )

            height, width = scaled.shape[:2]

            if height < 8 or width < 8:
                continue

            if height > screen_h or width > screen_w:
                continue

            result = cv2.matchTemplate(
                screen_gray,
                scaled,
                cv2.TM_CCOEFF_NORMED
            )

            _, max_value, _, max_location = cv2.minMaxLoc(result)

            if not np.isfinite(max_value):
                continue

            if max_value >= SHOT_THRESHOLD:
                return (
                    max_location[0] + width // 2,
                    max_location[1] + height // 2
                )

        return None

    def find_target(self, step, image):
        """Cari target tahap ini: gambar atau tulisan."""
        if step["method"] == SHOT_METHOD:
            return self.find_image(image, step["image"])

        return self.find_text(image, step["keyword"])

    @staticmethod
    def target_name(step):
        if step["method"] == SHOT_METHOD:
            return "gambar screenshot"

        return step["keyword"]

    # ==================================================
    # BANTUAN LAYAR
    # ==================================================

    @staticmethod
    def grab_image(screen, monitor):
        """Ambil screenshot seluruh layar sebagai PIL Image."""
        screenshot = screen.grab(monitor)

        return Image.frombytes(
            "RGB",
            screenshot.size,
            screenshot.rgb
        )

    @staticmethod
    def images_similar(first, second):
        """True jika dua screenshot hampir sama (layar tidak berubah)."""
        small_a = first.convert("L").resize((160, 90))
        small_b = second.convert("L").resize((160, 90))

        diff = ImageChops.difference(small_a, small_b)
        mean = ImageStat.Stat(diff).mean[0]

        return mean < SCROLL_SAME_THRESHOLD

    @staticmethod
    def save_debug(image):
        try:
            image.save(DEBUG_FILE)
        except Exception:
            pass

    # ==================================================
    # AKSI
    # ==================================================

    def wait_delay(self, step):
        """
        Tunggu sesuai delay tahap ini sebelum dijalankan.
        Mengembalikan False jika dihentikan saat menunggu.
        """
        total = step["delay"]

        if total <= 0:
            return True

        end_time = time.monotonic() + total

        while True:
            remaining = end_time - time.monotonic()

            if remaining <= 0:
                return True

            self.set_status(
                f"Tahap {step['number']}: delay, "
                f"sisa {format_hms(remaining + 0.999)}"
            )

            if self.stop_event.wait(min(1.0, remaining)):
                return False

    def press_enter_if_needed(self, step):
        """Tekan Enter jika tahap ini mencentang 'Tekan Enter'."""
        if not step["enter"]:
            return

        if self.stop_event.wait(BEFORE_ENTER_DELAY):
            return

        self.set_status(
            f"Tahap {step['number']}: menekan Enter"
        )

        pyautogui.press("enter")

    def do_action(self, step, x, y):
        # Gerakkan mouse: cepat di awal, melambat dekat target.
        pyautogui.moveTo(
            x, y,
            duration=MOVE_DURATION,
            tween=pyautogui.easeOutQuad
        )

        if self.stop_event.wait(BEFORE_CLICK_DELAY):
            return

        if step["method"] == "KLIK GANDA":
            pyautogui.doubleClick(x, y)
        else:
            pyautogui.click(x, y)

        if step["method"] == "KETIK":
            if self.stop_event.wait(BEFORE_TYPE_DELAY):
                return

            # pyautogui.write cocok untuk teks ASCII.
            pyautogui.write(
                step["typing"],
                interval=TYPE_INTERVAL
            )

        self.press_enter_if_needed(step)

    def type_directly(self, step):
        """LANGSUNG KETIK: tanpa deteksi dan tanpa klik."""
        self.set_status(
            f"Tahap {step['number']}: langsung mengetik"
        )

        # Jeda singkat agar aplikasi target siap menerima ketikan.
        if self.stop_event.wait(DIRECT_TYPE_DELAY):
            return False

        pyautogui.write(
            step["typing"],
            interval=TYPE_INTERVAL
        )

        self.press_enter_if_needed(step)

        self.set_status(
            f"Tahap {step['number']} selesai"
        )

        return True

    def scroll_until_found(self, step, screen, monitor):
        """
        SCROLL: jika kata tidak ada di layar, scroll pelan ke bawah
        sampai mentok. Jika belum ketemu, scroll ke atas sampai mentok.
        Diulang sampai kata ditemukan atau dihentikan.
        """
        keyword = step["keyword"]
        number = step["number"]

        screen_size = pyautogui.size()
        center_x = screen_size.width // 2
        center_y = screen_size.height // 2

        first_scan = True

        while not self.stop_event.is_set():
            self.set_status(
                f"Tahap {number}: mencari {keyword}"
            )

            image = self.grab_image(screen, monitor)

            if first_scan:
                self.save_debug(image)
                first_scan = False

            if self.find_text(image, keyword) is not None:
                self.set_status(
                    f"Tahap {number}: {keyword} ditemukan"
                )
                return True

            # Arahkan mouse ke tengah layar agar scroll
            # mengenai jendela yang sedang tampil.
            pyautogui.moveTo(
                center_x, center_y,
                duration=MOVE_DURATION
            )

            # Utamakan ke bawah, lalu ke atas.
            for direction, label in ((-1, "bawah"), (1, "atas")):
                previous = image
                same_count = 0

                for _ in range(SCROLL_MAX_STEPS):
                    if self.stop_event.is_set():
                        return False

                    self.set_status(
                        f"Tahap {number}: scroll ke {label}, "
                        f"mencari {keyword}"
                    )

                    pyautogui.scroll(direction * SCROLL_CLICKS)

                    if self.stop_event.wait(SCROLL_DELAY):
                        return False

                    current = self.grab_image(screen, monitor)

                    if self.find_text(current, keyword) is not None:
                        self.set_status(
                            f"Tahap {number}: "
                            f"{keyword} ditemukan"
                        )
                        return True

                    # Layar tidak berubah = sudah mentok.
                    if self.images_similar(previous, current):
                        same_count += 1
                    else:
                        same_count = 0

                    if same_count >= 2:
                        break

                    previous = current

                image = previous

            self.set_status(
                f"Tahap {number}: {keyword} belum ditemukan, "
                f"mengulang pencarian..."
            )

            if self.stop_event.wait(SCAN_INTERVAL):
                return False

        return False

    # ==================================================
    # PROSES UTAMA
    # ==================================================

    def run_step(self, step, screen, monitor):
        """Jalankan satu tahap."""

        # ---------- DELAY (menunggu sebelum tahap dijalankan) ----------
        if not self.wait_delay(step):
            return

        # ---------- LANGSUNG KETIK ----------
        if step["method"] == "LANGSUNG KETIK":
            done = self.type_directly(step)

            if done and not self.stop_event.is_set():
                self.stop_event.wait(STEP_DELAY)

            return

        # ---------- SCROLL ----------
        if step["method"] == "SCROLL":
            found = self.scroll_until_found(step, screen, monitor)

            if found and not self.stop_event.is_set():
                self.press_enter_if_needed(step)

                self.set_status(
                    f"Tahap {step['number']} selesai"
                )

                self.stop_event.wait(STEP_DELAY)

            return

        # ---------- DETEKSI (TULISAN / GAMBAR) LALU AKSI ----------
        name = self.target_name(step)

        self.set_status(
            f"Tahap {step['number']}: mencari {name}"
        )

        found = False
        first_scan = True

        while not self.stop_event.is_set():
            image = self.grab_image(screen, monitor)

            if first_scan:
                self.save_debug(image)
                first_scan = False

            position = self.find_target(step, image)

            if position is None:
                self.stop_event.wait(SCAN_INTERVAL)
                continue

            if self.stop_event.is_set():
                break

            # Koordinat hasil deteksi relatif terhadap gambar.
            # Tambahkan offset monitor untuk koordinat desktop.
            x = position[0] + monitor["left"]
            y = position[1] + monitor["top"]

            self.set_status(
                f"Tahap {step['number']}: "
                f"{name} ditemukan "
                f"di ({x}, {y})"
            )

            # Jeda singkat agar tampilan stabil.
            if self.stop_event.wait(FOUND_DELAY):
                break

            self.do_action(step, x, y)

            self.set_status(
                f"Tahap {step['number']} selesai"
            )

            found = True
            break

        if found and not self.stop_event.is_set():
            # Beri waktu aplikasi target memperbarui layar.
            self.stop_event.wait(STEP_DELAY)

    def process_screen(self, steps, repeat):
        try:
            with mss.mss() as screen:
                # monitors[0] adalah gabungan seluruh monitor.
                monitor = screen.monitors[0]

                # Tunggu jendela selesai ter-minimize.
                self.stop_event.wait(MINIMIZE_DELAY)

                round_number = 1

                while not self.stop_event.is_set():
                    if repeat:
                        self.set_status(
                            f"Putaran ke-{round_number} dimulai"
                        )

                    for step in steps:
                        if self.stop_event.is_set():
                            break

                        self.run_step(step, screen, monitor)

                    if self.stop_event.is_set():
                        break

                    if not repeat:
                        break

                    round_number += 1

                    self.set_status(
                        f"Putaran ke-{round_number - 1} selesai, "
                        f"mengulang dari tahap 1..."
                    )

                    if self.stop_event.wait(REPEAT_DELAY):
                        break

                if self.stop_event.is_set():
                    self.set_status("Proses dihentikan")
                else:
                    self.set_status("Semua tahap selesai")

        except Exception as error:
            details = traceback.format_exc()

            self.set_status(
                f"ERROR: {type(error).__name__}: {error}"
            )

            print(details)

            wx.CallAfter(
                wx.MessageBox,
                f"Terjadi kesalahan:\n\n{error}\n\n"
                "Detail lengkap tersedia di konsol jika dijalankan dari CMD.",
                APP_NAME,
                wx.OK | wx.ICON_ERROR
            )

        finally:
            self.running = False
            wx.CallAfter(self.btn_run.Enable)
            wx.CallAfter(self.btn_add.Enable)
            wx.CallAfter(self.btn_config.Enable)
            wx.CallAfter(self.btn_stop.Disable)
            wx.CallAfter(self.restore_window)

    def restore_window(self):
        if self:
            self.Iconize(False)
            self.Raise()

    # ==================================================
    # BERHENTI DAN KELUAR
    # ==================================================

    def stop(self, event=None):
        self.stop_event.set()
        self.set_status("Meminta proses berhenti...")

        self.btn_stop.Disable()

    def on_close(self, event):
        self.stop_event.set()

        # Tutup layar screenshot jika masih terbuka.
        if self.overlay is not None:
            try:
                self.overlay.finished = True
                self.overlay.Destroy()
            except Exception:
                pass

            self.overlay = None

        # Simpan konfigurasi sebelum aplikasi ditutup.
        self.save_config()

        try:
            self.UnregisterHotKey(self.HOTKEY_ID)
        except Exception:
            pass

        self.Destroy()


if __name__ == "__main__":
    app = wx.App(False)
    frame = AutoKlikDeteksiLayar()
    app.MainLoop()
