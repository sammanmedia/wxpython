# -*- coding: utf-8 -*-
"""
PY to EXE Converter (Portable) - wxPython + PyInstaller

Fitur:
- Pilih 1 atau banyak file .py (digabung jadi satu aplikasi .exe)
- Input nama aplikasi
- Input icon aplikasi (.ico, atau .png/.jpg jika Pillow terpasang)
- Output otomatis ke folder "convert output exe" (di samping aplikasi ini)
"""

import os
import re
import sys
import shutil
import tempfile
import threading
import subprocess

import wx

APP_TITLE = "PY to EXE Converter"
OUTPUT_FOLDER_NAME = "convert output exe"


# --------------------------------------------------------------------------
# Helper
# --------------------------------------------------------------------------
def app_base_dir():
    """Folder tempat aplikasi ini berada (portable-friendly)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_python():
    """Cari interpreter Python untuk menjalankan PyInstaller.

    Jika aplikasi ini sendiri sudah di-compile jadi .exe, sys.executable
    menunjuk ke .exe itu, jadi harus cari python sistem.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    for name in ("python", "py"):
        path = shutil.which(name)
        if path:
            return path
    return None


def sanitize_name(name):
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    return name


def hidden_flags():
    if os.name == "nt":
        return subprocess.CREATE_NO_WINDOW
    return 0


# --------------------------------------------------------------------------
# Drag & drop
# --------------------------------------------------------------------------
class FileDrop(wx.FileDropTarget):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def OnDropFiles(self, x, y, filenames):
        self.callback([f for f in filenames if f.lower().endswith((".py", ".pyw"))])
        return True


# --------------------------------------------------------------------------
# Main Frame
# --------------------------------------------------------------------------
class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title=APP_TITLE, size=(720, 720))
        self.files = []          # daftar path .py, index 0 = file utama (entry point)
        self.building = False

        self._build_ui()
        self.Centre()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.VERTICAL)

        # --- 1. Pilih file .py
        box_files = wx.StaticBoxSizer(wx.VERTICAL, panel, "1. Pilih File Python (.py / .pyw)")
        sb = box_files.GetStaticBox()

        self.lst_files = wx.ListBox(sb, style=wx.LB_EXTENDED, size=(-1, 140))
        self.lst_files.SetDropTarget(FileDrop(self.add_files))
        box_files.Add(self.lst_files, 1, wx.EXPAND | wx.ALL, 5)

        row = wx.BoxSizer(wx.HORIZONTAL)
        btn_add = wx.Button(sb, label="Tambah File...")
        btn_remove = wx.Button(sb, label="Hapus")
        btn_main = wx.Button(sb, label="Jadikan File Utama")
        btn_clear = wx.Button(sb, label="Kosongkan")
        for b in (btn_add, btn_remove, btn_main, btn_clear):
            row.Add(b, 0, wx.RIGHT, 6)
        box_files.Add(row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 5)

        hint = wx.StaticText(
            sb,
            label="Tip: bisa drag & drop file .py / .pyw ke daftar. File teratas ([UTAMA]) "
                  "adalah file yang pertama dijalankan; file lain disertakan sebagai modul. "
                  "Jika file utama berupa .pyw, console otomatis disembunyikan.",
        )
        hint.Wrap(650)
        box_files.Add(hint, 0, wx.ALL, 5)
        main.Add(box_files, 0, wx.EXPAND | wx.ALL, 8)

        # --- 2. Nama aplikasi
        box_name = wx.StaticBoxSizer(wx.VERTICAL, panel, "2. Nama Aplikasi")
        sb2 = box_name.GetStaticBox()
        self.txt_name = wx.TextCtrl(sb2)
        box_name.Add(self.txt_name, 0, wx.EXPAND | wx.ALL, 5)
        main.Add(box_name, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- 3. Icon
        box_icon = wx.StaticBoxSizer(wx.VERTICAL, panel, "3. Icon Aplikasi (opsional)")
        sb3 = box_icon.GetStaticBox()
        row_icon = wx.BoxSizer(wx.HORIZONTAL)
        self.txt_icon = wx.TextCtrl(sb3, style=wx.TE_READONLY)
        btn_icon = wx.Button(sb3, label="Pilih Icon...")
        btn_icon_clear = wx.Button(sb3, label="Hapus")
        self.bmp_icon = wx.StaticBitmap(sb3, size=(40, 40))
        row_icon.Add(self.txt_icon, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        row_icon.Add(btn_icon, 0, wx.RIGHT, 6)
        row_icon.Add(btn_icon_clear, 0, wx.RIGHT, 6)
        row_icon.Add(self.bmp_icon, 0)
        box_icon.Add(row_icon, 0, wx.EXPAND | wx.ALL, 5)
        main.Add(box_icon, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- 4. Opsi
        box_opt = wx.StaticBoxSizer(wx.HORIZONTAL, panel, "4. Opsi")
        sb4 = box_opt.GetStaticBox()
        self.chk_onefile = wx.CheckBox(sb4, label="Satu file .exe (portable)")
        self.chk_onefile.SetValue(True)
        self.chk_noconsole = wx.CheckBox(sb4, label="Tanpa jendela console (GUI app)")
        box_opt.Add(self.chk_onefile, 0, wx.ALL, 8)
        box_opt.Add(self.chk_noconsole, 0, wx.ALL, 8)
        main.Add(box_opt, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        # --- Output info
        self.out_dir = os.path.join(app_base_dir(), OUTPUT_FOLDER_NAME)
        lbl_out = wx.StaticText(panel, label="Output otomatis ke:  " + self.out_dir)
        lbl_out.Wrap(680)
        main.Add(lbl_out, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- Tombol aksi
        row_act = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_convert = wx.Button(panel, label="CONVERT KE .EXE", size=(-1, 38))
        self.btn_convert.SetFont(self.btn_convert.GetFont().Bold())
        self.btn_open = wx.Button(panel, label="Buka Folder Output", size=(-1, 38))
        row_act.Add(self.btn_convert, 1, wx.RIGHT, 8)
        row_act.Add(self.btn_open, 0)
        main.Add(row_act, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        self.gauge = wx.Gauge(panel, range=100, size=(-1, 14))
        main.Add(self.gauge, 0, wx.EXPAND | wx.ALL, 8)

        # --- Log
        self.log = wx.TextCtrl(
            panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL, size=(-1, 160)
        )
        self.log.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        main.Add(self.log, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        panel.SetSizer(main)

        # --- Events
        btn_add.Bind(wx.EVT_BUTTON, self.on_add)
        btn_remove.Bind(wx.EVT_BUTTON, self.on_remove)
        btn_main.Bind(wx.EVT_BUTTON, self.on_make_main)
        btn_clear.Bind(wx.EVT_BUTTON, self.on_clear)
        btn_icon.Bind(wx.EVT_BUTTON, self.on_pick_icon)
        btn_icon_clear.Bind(wx.EVT_BUTTON, self.on_clear_icon)
        self.btn_convert.Bind(wx.EVT_BUTTON, self.on_convert)
        self.btn_open.Bind(wx.EVT_BUTTON, self.on_open_output)

        self.icon_path = ""

    # -------------------------------------------------------------- Logging
    def write_log(self, text):
        wx.CallAfter(self._append_log, text)

    def _append_log(self, text):
        self.log.AppendText(text if text.endswith("\n") else text + "\n")

    # ------------------------------------------------------------ File list
    def refresh_list(self):
        self.lst_files.Clear()
        for i, f in enumerate(self.files):
            prefix = "[UTAMA]  " if i == 0 else "             "
            self.lst_files.Append(prefix + f)
        if self.files and self.files[0].lower().endswith(".pyw"):
            self.chk_noconsole.SetValue(True)
        if self.files and not self.txt_name.GetValue().strip():
            self.txt_name.SetValue(os.path.splitext(os.path.basename(self.files[0]))[0])

    def add_files(self, paths):
        added = 0
        for p in paths:
            p = os.path.abspath(p)
            if p.lower().endswith((".py", ".pyw")) and p not in self.files:
                self.files.append(p)
                added += 1
        if added:
            self.refresh_list()

    def on_add(self, _):
        with wx.FileDialog(
            self, "Pilih file Python",
            wildcard="Python files (*.py;*.pyw)|*.py;*.pyw",
            style=wx.FD_OPEN | wx.FD_MULTIPLE | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.add_files(dlg.GetPaths())

    def on_remove(self, _):
        sel = sorted(self.lst_files.GetSelections(), reverse=True)
        for i in sel:
            del self.files[i]
        self.refresh_list()

    def on_make_main(self, _):
        sel = self.lst_files.GetSelections()
        if len(sel) != 1:
            wx.MessageBox("Pilih satu file untuk dijadikan file utama.", APP_TITLE, wx.ICON_INFORMATION)
            return
        f = self.files.pop(sel[0])
        self.files.insert(0, f)
        self.refresh_list()

    def on_clear(self, _):
        self.files = []
        self.refresh_list()

    # ----------------------------------------------------------------- Icon
    def on_pick_icon(self, _):
        with wx.FileDialog(
            self, "Pilih icon aplikasi",
            wildcard="Icon / Gambar (*.ico;*.png;*.jpg;*.jpeg;*.bmp)|*.ico;*.png;*.jpg;*.jpeg;*.bmp",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self.set_icon(dlg.GetPath())

    def set_icon(self, path):
        self.icon_path = path
        self.txt_icon.SetValue(path)
        try:
            img = wx.Image(path)
            if img.IsOk():
                img = img.Scale(40, 40, wx.IMAGE_QUALITY_HIGH)
                self.bmp_icon.SetBitmap(wx.Bitmap(img))
            else:
                self.bmp_icon.SetBitmap(wx.NullBitmap)
        except Exception:
            self.bmp_icon.SetBitmap(wx.NullBitmap)

    def on_clear_icon(self, _):
        self.icon_path = ""
        self.txt_icon.SetValue("")
        self.bmp_icon.SetBitmap(wx.NullBitmap)

    # -------------------------------------------------------------- Actions
    def on_open_output(self, _):
        os.makedirs(self.out_dir, exist_ok=True)
        if os.name == "nt":
            os.startfile(self.out_dir)
        else:
            subprocess.Popen(["xdg-open", self.out_dir])

    def set_busy(self, busy):
        self.building = busy
        self.btn_convert.Enable(not busy)
        if busy:
            self.gauge.Pulse()
            self._timer = wx.Timer(self)
            self.Bind(wx.EVT_TIMER, lambda e: self.gauge.Pulse(), self._timer)
            self._timer.Start(120)
        else:
            if hasattr(self, "_timer"):
                self._timer.Stop()
            self.gauge.SetValue(0)

    def on_convert(self, _):
        if self.building:
            return
        if not self.files:
            wx.MessageBox("Pilih minimal satu file .py dulu.", APP_TITLE, wx.ICON_WARNING)
            return

        name = sanitize_name(self.txt_name.GetValue())
        if not name:
            wx.MessageBox("Nama aplikasi belum diisi.", APP_TITLE, wx.ICON_WARNING)
            return

        # nama file bentrok?
        base_names = [os.path.splitext(os.path.basename(f))[0].lower() for f in self.files]
        if len(base_names) != len(set(base_names)):
            wx.MessageBox(
                "Ada file dengan nama yang sama. Ubah nama salah satunya.",
                APP_TITLE, wx.ICON_WARNING,
            )
            return

        py = find_python()
        if not py:
            wx.MessageBox(
                "Python tidak ditemukan di sistem.\n"
                "Install Python dan pastikan masuk PATH.",
                APP_TITLE, wx.ICON_ERROR,
            )
            return

        self.log.Clear()
        self.set_busy(True)
        threading.Thread(
            target=self.worker,
            args=(py, list(self.files), name, self.icon_path,
                  self.chk_onefile.GetValue(), self.chk_noconsole.GetValue()),
            daemon=True,
        ).start()

    # --------------------------------------------------------------- Worker
    def run_cmd(self, cmd):
        """Jalankan perintah, stream output ke log. Return exit code."""
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=hidden_flags(),
        )
        for line in iter(proc.stdout.readline, b""):
            self.write_log(line.decode("utf-8", errors="replace").rstrip())
        proc.wait()
        return proc.returncode

    def ensure_pyinstaller(self, py):
        code = subprocess.call(
            [py, "-m", "PyInstaller", "--version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=hidden_flags(),
        )
        if code == 0:
            return True

        self.write_log("PyInstaller belum terpasang. Mencoba install via pip...")
        code = self.run_cmd([py, "-m", "pip", "install", "pyinstaller"])
        return code == 0

    def prepare_icon(self, icon_path, tmp_dir):
        """Pastikan icon berformat .ico. Konversi dengan Pillow bila perlu."""
        if not icon_path:
            return None
        if icon_path.lower().endswith(".ico"):
            return icon_path
        try:
            from PIL import Image  # noqa
            img = Image.open(icon_path).convert("RGBA")
            ico = os.path.join(tmp_dir, "app_icon.ico")
            img.save(ico, format="ICO",
                     sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
            self.write_log("Icon dikonversi ke .ico")
            return ico
        except ImportError:
            self.write_log("PERINGATAN: Pillow tidak terpasang, icon non-.ico diabaikan. "
                           "Gunakan file .ico atau jalankan: pip install pillow")
        except Exception as e:
            self.write_log("PERINGATAN: gagal mengonversi icon: %s" % e)
        return None

    def worker(self, py, files, name, icon_path, onefile, noconsole):
        tmp = tempfile.mkdtemp(prefix="py2exe_")
        success = False
        try:
            if not self.ensure_pyinstaller(py):
                self.write_log("GAGAL: PyInstaller tidak bisa dipasang.")
                return

            # Salin semua file ke satu folder sementara agar import antar-file jalan
            src_dir = os.path.join(tmp, "src")
            work_dir = os.path.join(tmp, "work")
            os.makedirs(src_dir)
            os.makedirs(work_dir)
            # .pyw disalin sebagai .py supaya bisa di-import antar-file
            for f in files:
                stem = os.path.splitext(os.path.basename(f))[0]
                shutil.copy2(f, os.path.join(src_dir, stem + ".py"))

            entry = os.path.join(
                src_dir, os.path.splitext(os.path.basename(files[0]))[0] + ".py"
            )
            if files[0].lower().endswith(".pyw"):
                noconsole = True
            os.makedirs(self.out_dir, exist_ok=True)

            cmd = [
                py, "-m", "PyInstaller", "--noconfirm", "--clean",
                "--name", name,
                "--distpath", self.out_dir,
                "--workpath", work_dir,
                "--specpath", work_dir,
                "--paths", src_dir,
            ]
            if onefile:
                cmd.append("--onefile")
            if noconsole:
                cmd.append("--noconsole")

            ico = self.prepare_icon(icon_path, tmp)
            if ico:
                cmd += ["--icon", ico]

            # file selain utama disertakan sebagai modul
            for f in files[1:]:
                mod = os.path.splitext(os.path.basename(f))[0]
                cmd += ["--hidden-import", mod]

            cmd.append(entry)

            self.write_log("Mulai convert: %s" % name)
            self.write_log("File utama : %s" % files[0])
            if len(files) > 1:
                self.write_log("Modul lain : %s" % ", ".join(os.path.basename(f) for f in files[1:]))
            self.write_log("-" * 60)

            code = self.run_cmd(cmd)
            self.write_log("-" * 60)

            result = os.path.join(self.out_dir, name + ".exe") if onefile \
                else os.path.join(self.out_dir, name)

            if code == 0 and os.path.exists(result):
                success = True
                self.write_log("SELESAI! Hasil: %s" % result)
            else:
                self.write_log("GAGAL (exit code %s). Cek log di atas." % code)
        except Exception as e:
            self.write_log("ERROR: %s" % e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            wx.CallAfter(self.on_done, success)

    def on_done(self, success):
        self.set_busy(False)
        if success:
            if wx.MessageBox(
                "Convert berhasil!\n\nBuka folder output sekarang?",
                APP_TITLE, wx.YES_NO | wx.ICON_INFORMATION,
            ) == wx.YES:
                self.on_open_output(None)
        else:
            wx.MessageBox("Convert gagal. Lihat log untuk detail.", APP_TITLE, wx.ICON_ERROR)


# --------------------------------------------------------------------------
if __name__ == "__main__":
    app = wx.App(False)
    MainFrame().Show()
    app.MainLoop()
