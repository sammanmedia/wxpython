# -*- coding: utf-8 -*-
"""
Auto Klik Deteksi Layar - Versi 6
Windows 10 + wxPython + MSS + Tesseract OCR
"""

import os
import re
import sys
import json
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

from PIL import Image, ImageOps
from pytesseract import Output


# ==================================================
# KONFIGURASI
# ==================================================

APP_NAME = "Auto Klik Deteksi Layar"

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

# File gambar untuk memeriksa apa yang dilihat program
DEBUG_FILE = "debug_layar.png"

# File konfigurasi (di folder yang sama dengan script)
CONFIG_NAME = "AutoKlik.cfg"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, CONFIG_NAME)

METHODS = ["KLIK", "KLIK GANDA", "KETIK", "LANGSUNG KETIK"]

# Metode yang membutuhkan isian "Teks yang diketik"
TYPING_METHODS = ("KETIK", "LANGSUNG KETIK")

# Jumlah tahap bawaan yang tidak bisa dihapus
DEFAULT_STEPS = 3

pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True


class AutoKlikDeteksiLayar(wx.Frame):

    HOTKEY_ID = 9001

    def __init__(self):
        super().__init__(
            None,
            title=APP_NAME,
            size=(780, 780)
        )

        self.running = False
        self.stop_event = threading.Event()
        self.worker = None
        self.rows = []

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
                "Tekan Enter = otomatis menekan Enter "
                "setelah aksi tahap itu."
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
            size=(160, 40)
        )

        self.btn_stop = wx.Button(
            panel,
            label="BERHENTI",
            size=(160, 40)
        )

        self.btn_stop.Disable()

        self.chk_save = wx.CheckBox(
            panel,
            label="Save Konfigurasi"
        )

        buttons.Add(self.btn_run, 0, wx.RIGHT, 10)
        buttons.Add(self.btn_stop, 0, wx.RIGHT, 16)
        buttons.Add(
            self.chk_save, 0,
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
        self.Bind(wx.EVT_CLOSE, self.on_close)

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

        grid.Add(
            wx.StaticText(parent, label="Teks yang diketik:"),
            0, wx.ALIGN_CENTER_VERTICAL
        )
        grid.Add(typing, 1, wx.EXPAND)

        options = wx.BoxSizer(wx.HORIZONTAL)
        options.Add(
            enabled, 0,
            wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 16
        )
        options.Add(
            enter, 0,
            wx.ALIGN_CENTER_VERTICAL
        )

        if delete_button is not None:
            options.AddStretchSpacer(1)
            options.Add(
                delete_button, 0,
                wx.ALIGN_CENTER_VERTICAL
            )

        grid.Add(
            wx.StaticText(parent, label="Pilihan tahap:"),
            0, wx.ALIGN_CENTER_VERTICAL
        )
        grid.Add(options, 1, wx.EXPAND)

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
            "keyword": keyword,
            "method": method,
            "typing": typing,
            "enabled": enabled,
            "enter": enter,
            "delete": delete_button,
        }

        self.rows.append(row)

        enabled.Bind(
            wx.EVT_CHECKBOX,
            lambda event, r=row: self.refresh_row(r)
        )

        method.Bind(
            wx.EVT_CHOICE,
            lambda event, r=row: self.refresh_row(r)
        )

        if delete_button is not None:
            delete_button.Bind(
                wx.EVT_BUTTON,
                lambda event, r=row: self.remove_step(r)
            )

        self.refresh_row(row)
        self.relayout()

        if scroll_to_end:
            wx.CallAfter(self.scroll_to_end)

        return row

    def remove_step(self, row):
        if self.running:
            return

        if row not in self.rows:
            return

        self.rows.remove(row)
        self.steps_sizer.Detach(row["panel"])
        row["panel"].Destroy()

        # Urutkan ulang nomor tahap.
        for index, item in enumerate(self.rows):
            item["box"].GetStaticBox().SetLabel(
                f"Deteksi klik ke-{index + 1}"
            )

        self.relayout()

    def on_add_step(self, event):
        if self.running:
            return

        self.add_step(scroll_to_end=True)

    def relayout(self):
        self.steps_sizer.Layout()
        self.scroll.FitInside()
        self.scroll.Layout()

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

        # LANGSUNG KETIK tidak perlu mendeteksi kata.
        row["keyword"].Enable(
            active and method != "LANGSUNG KETIK"
        )

        row["method"].Enable(active)

        row["typing"].Enable(
            active and method in TYPING_METHODS
        )

        row["enter"].Enable(active)

    def set_status(self, message):
        wx.CallAfter(
            self.status.SetLabel,
            "Status: " + str(message)
        )

    def on_hotkey(self, event):
        self.stop()

    # ==================================================
    # SIMPAN DAN MUAT KONFIGURASI (AutoKlik.cfg)
    # ==================================================

    def collect_config(self):
        steps = []

        for index, row in enumerate(self.rows):
            steps.append({
                "enabled": (
                    True if index == 0
                    else row["enabled"].GetValue()
                ),
                "keyword": row["keyword"].GetValue(),
                "method": row["method"].GetStringSelection(),
                "typing": row["typing"].GetValue(),
                "enter": row["enter"].GetValue(),
            })

        return {
            "save_config": self.chk_save.GetValue(),
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

    def save_config(self):
        """
        Dicentang   : simpan seluruh isian ke AutoKlik.cfg.
        Tidak dicentang: isi file tetap ada (bisa dipakai lewat
        PILIH CONFIG), hanya tidak dimuat otomatis saat dibuka.
        """
        try:
            if self.chk_save.GetValue():
                data = self.collect_config()
            else:
                if not os.path.isfile(CONFIG_FILE):
                    return

                data = self.read_config_file(CONFIG_FILE)

                if data is None:
                    return

                data["save_config"] = False

            with open(CONFIG_FILE, "w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)

        except Exception:
            traceback.print_exc()

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

            method_index = row["method"].FindString(
                str(saved.get("method", "KLIK"))
            )

            if method_index == wx.NOT_FOUND:
                method_index = 0

            row["method"].SetSelection(method_index)

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
        if self.running:
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
        if self.running:
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

            # LANGSUNG KETIK tidak butuh kata deteksi.
            if method != "LANGSUNG KETIK" and not keyword:
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

            steps.append({
                "keyword": keyword,
                "method": method,
                "typing": typing,
                "enter": press_enter,
                "number": index + 1,
            })

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
            args=(steps,),
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
    # PROSES UTAMA
    # ==================================================

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

    def process_screen(self, steps):
        try:
            with mss.mss() as screen:
                # monitors[0] adalah gabungan seluruh monitor.
                monitor = screen.monitors[0]

                # Tunggu jendela selesai ter-minimize.
                self.stop_event.wait(MINIMIZE_DELAY)

                for step in steps:
                    if self.stop_event.is_set():
                        break

                    # ---------- LANGSUNG KETIK ----------
                    if step["method"] == "LANGSUNG KETIK":
                        done = self.type_directly(step)

                        if self.stop_event.is_set():
                            break

                        if done:
                            # Beri waktu aplikasi memperbarui layar.
                            if self.stop_event.wait(STEP_DELAY):
                                break

                        continue

                    # ---------- DETEKSI LALU AKSI ----------
                    self.set_status(
                        f"Tahap {step['number']}: "
                        f"mencari {step['keyword']}"
                    )

                    found = False
                    first_scan = True

                    while not self.stop_event.is_set():
                        # Ambil screenshot seluruh layar.
                        screenshot = screen.grab(monitor)

                        # Konversi MSS ScreenShot menjadi PIL Image.
                        image = Image.frombytes(
                            "RGB",
                            screenshot.size,
                            screenshot.rgb
                        )

                        if first_scan:
                            try:
                                image.save(DEBUG_FILE)
                            except Exception:
                                pass
                            first_scan = False

                        position = self.find_text(
                            image,
                            step["keyword"]
                        )

                        if position is None:
                            self.stop_event.wait(SCAN_INTERVAL)
                            continue

                        if self.stop_event.is_set():
                            break

                        # Koordinat OCR relatif terhadap gambar.
                        # Tambahkan offset monitor untuk koordinat desktop.
                        x = position[0] + monitor["left"]
                        y = position[1] + monitor["top"]

                        self.set_status(
                            f"Tahap {step['number']}: "
                            f"{step['keyword']} ditemukan "
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

                    if self.stop_event.is_set():
                        break

                    if found:
                        # Beri waktu aplikasi target memperbarui layar.
                        if self.stop_event.wait(STEP_DELAY):
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