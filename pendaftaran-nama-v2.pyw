
import wx
import sqlite3
import calendar
from datetime import date, datetime
from pathlib import Path

# ==================================================
# KONFIGURASI
# ==================================================

DB_PATH = Path(__file__).resolve().parent / "pendaftaran.db"

WARNA_PUTIH = "#FFFFFF"
WARNA_ABU = "#F1F3F5"
WARNA_BIRU = "#1976D2"


# ==================================================
# DATABASE
# ==================================================

def koneksi_database():
    return sqlite3.connect(DB_PATH)


def ambil_kolom():
    with koneksi_database() as conn:
        hasil = conn.execute(
            'PRAGMA table_info("tabel_orang")'
        ).fetchall()

    return [baris[1] for baris in hasil]


def buat_database():
    with koneksi_database() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tabel_orang (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nama TEXT NOT NULL,
                usia TEXT NOT NULL,
                jenis_kelamin TEXT NOT NULL,
                tanggal_lahir TEXT NOT NULL,
                alamat TEXT NOT NULL
            )
        """)


def kutip(nama):
    # Mengamankan nama kolom SQLite
    return '"' + nama.replace('"', '""') + '"'


# ==================================================
# PERHITUNGAN USIA
# ==================================================

def hitung_usia(lahir, hari_ini):
    if lahir > hari_ini:
        raise ValueError(
            "Tanggal lahir tidak boleh di masa depan."
        )

    tahun = hari_ini.year - lahir.year
    bulan = hari_ini.month - lahir.month
    hari = hari_ini.day - lahir.day

    if hari < 0:
        bulan -= 1

        bulan_sebelumnya = hari_ini.month - 1
        tahun_sebelumnya = hari_ini.year

        if bulan_sebelumnya == 0:
            bulan_sebelumnya = 12
            tahun_sebelumnya -= 1

        hari += calendar.monthrange(
            tahun_sebelumnya,
            bulan_sebelumnya
        )[1]

    if bulan < 0:
        tahun -= 1
        bulan += 12

    return f"{tahun} tahun, {bulan} bulan, {hari} hari"


def parse_tanggal(teks):
    teks = teks.strip()

    for format_tanggal in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(
                teks, format_tanggal
            ).date()
        except ValueError:
            pass

    raise ValueError(
        "Tanggal tidak valid. Gunakan DD-MM-YYYY.\n"
        "Contoh: 15-06-2010"
    )


def format_tanggal_form(teks):
    try:
        return date.fromisoformat(
            teks[:10]
        ).strftime("%d-%m-%Y")
    except (ValueError, TypeError):
        return teks or ""


# ==================================================
# DIALOG EDIT DATA
# ==================================================

class DialogEdit(wx.Dialog):

    def __init__(self, parent, kolom, data):
        super().__init__(
            parent,
            title="Edit Data Pendaftar",
            size=(480, 600)
        )

        self.kolom = kolom
        self.data_awal = data
        self.inputan = {}

        panel = wx.Panel(self)
        utama = wx.BoxSizer(wx.VERTICAL)

        judul = wx.StaticText(
            panel, label="EDIT DATA PENDAFTAR"
        )
        judul.SetFont(wx.Font(
            13,
            wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_BOLD
        ))

        utama.Add(
            judul, 0,
            wx.ALL | wx.ALIGN_CENTER,
            15
        )

        scroll = wx.ScrolledWindow(
            panel,
            style=wx.VSCROLL
        )
        scroll.SetScrollRate(0, 15)

        form = wx.FlexGridSizer(0, 2, 10, 10)
        form.AddGrowableCol(1, 1)

        for nama_kolom in kolom:

            nilai = data.get(nama_kolom, "")

            if nama_kolom == "id":
                continue

            if nama_kolom == "tanggal_lahir":
                nilai = format_tanggal_form(str(nilai))

            form.Add(
                wx.StaticText(
                    scroll,
                    label=nama_kolom.replace("_", " ").title() + ":"
                ),
                0,
                wx.ALIGN_CENTER_VERTICAL
            )

            if nama_kolom == "alamat":
                kontrol = wx.TextCtrl(
                    scroll,
                    value=str(nilai or ""),
                    style=wx.TE_MULTILINE,
                    size=(-1, 65)
                )
            else:
                kontrol = wx.TextCtrl(
                    scroll,
                    value=str(nilai or "")
                )

            self.inputan[nama_kolom] = kontrol

            form.Add(kontrol, 1, wx.EXPAND)

        scroll.SetSizer(form)

        utama.Add(
            scroll, 1,
            wx.EXPAND | wx.LEFT | wx.RIGHT,
            15
        )

        tombol = wx.BoxSizer(wx.HORIZONTAL)

        btn_simpan = wx.Button(
            panel, label="SIMPAN PERUBAHAN"
        )
        btn_batal = wx.Button(
            panel, label="BATAL"
        )

        btn_simpan.Bind(
            wx.EVT_BUTTON, self.simpan
        )
        btn_batal.Bind(
            wx.EVT_BUTTON,
            lambda event: self.EndModal(wx.ID_CANCEL)
        )

        tombol.Add(btn_simpan, 1, wx.RIGHT, 8)
        tombol.Add(btn_batal, 0)

        utama.Add(
            tombol, 0,
            wx.EXPAND | wx.ALL,
            15
        )

        panel.SetSizer(utama)

        self.SetMinSize((420, 450))
        self.Centre()

    def simpan(self, event):
        self.data_baru = {}

        try:
            for nama_kolom, kontrol in self.inputan.items():
                nilai = kontrol.GetValue().strip()

                if nama_kolom == "tanggal_lahir":
                    lahir = parse_tanggal(nilai)

                    nilai = lahir.isoformat()

                    if lahir > date.today():
                        raise ValueError(
                            "Tanggal lahir tidak boleh di masa depan."
                        )

                self.data_baru[nama_kolom] = nilai

            # Usia tidak diketik manual.
            if (
                "tanggal_lahir" in self.data_baru
                and "usia" in self.data_baru
            ):
                lahir = date.fromisoformat(
                    self.data_baru["tanggal_lahir"]
                )

                self.data_baru["usia"] = hitung_usia(
                    lahir, date.today()
                )

        except ValueError as error:
            wx.MessageBox(
                str(error),
                "Data Tidak Valid",
                wx.OK | wx.ICON_WARNING,
                self
            )
            return

        self.EndModal(wx.ID_OK)


# ==================================================
# APLIKASI UTAMA VERSI 2
# ==================================================

class FormPendaftaran(wx.Frame):

    def __init__(self):
        super().__init__(
            parent=None,
            title="Aplikasi Pendaftaran - Versi 2",
            size=(1400, 760)
        )

        buat_database()

        self.kolom = ambil_kolom()
        self.inputan = {}

        panel = wx.Panel(self)
        panel.SetBackgroundColour("#E9EDF2")

        utama = wx.BoxSizer(wx.VERTICAL)

        # JUDUL UTAMA
        judul = wx.StaticText(
            panel,
            label="APLIKASI PENDAFTARAN - VERSI 2"
        )

        judul.SetFont(wx.Font(
            17,
            wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_BOLD
        ))

        judul.SetForegroundColour("#174A7E")

        utama.Add(
            judul, 0,
            wx.ALIGN_CENTER | wx.ALL,
            15
        )

        # PANEL KIRI + KANAN
        isi = wx.BoxSizer(wx.HORIZONTAL)

        # ==========================================
        # PANEL KIRI: FORMULIR
        # ==========================================

        kiri = wx.Panel(panel)
        kiri.SetBackgroundColour(WARNA_PUTIH)

        kiri_sizer = wx.BoxSizer(wx.VERTICAL)

        judul_form = wx.StaticText(
            kiri, label="FORMULIR PENDAFTARAN"
        )
        judul_form.SetFont(wx.Font(
            12,
            wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_BOLD
        ))

        kiri_sizer.Add(
            judul_form, 0,
            wx.ALL | wx.ALIGN_CENTER,
            15
        )

        scroll_form = wx.ScrolledWindow(
            kiri, style=wx.VSCROLL
        )
        scroll_form.SetScrollRate(0, 15)

        form = wx.FlexGridSizer(0, 2, 10, 8)
        form.AddGrowableCol(1, 1)

        # Field standar ditampilkan dengan urutan ini.
        field_standar = [
            "nama",
            "tanggal_lahir",
            "jenis_kelamin",
            "alamat",
        ]

        # Field tambahan otomatis mengikuti kolom database.
        field_tambahan = [
            nama for nama in self.kolom
            if nama not in (
                "id",
                "nama",
                "usia",
                "jenis_kelamin",
                "tanggal_lahir",
                "alamat",
            )
        ]

        urutan_form = (
            field_standar
            + field_tambahan
        )

        for nama_kolom in urutan_form:

            if nama_kolom not in self.kolom:
                continue

            label = nama_kolom.replace(
                "_", " "
            ).title() + ":"

            form.Add(
                wx.StaticText(
                    scroll_form, label=label
                ),
                0,
                wx.ALIGN_CENTER_VERTICAL
            )

            if nama_kolom == "jenis_kelamin":
                kontrol = wx.Choice(
                    scroll_form,
                    choices=["Laki-laki", "Perempuan"]
                )
                kontrol.SetSelection(0)

            elif nama_kolom == "alamat":
                kontrol = wx.TextCtrl(
                    scroll_form,
                    style=wx.TE_MULTILINE,
                    size=(-1, 65)
                )

            else:
                kontrol = wx.TextCtrl(scroll_form)

                if nama_kolom == "tanggal_lahir":
                    kontrol.SetValue(
                        "01-01-2000"
                    )

            self.inputan[nama_kolom] = kontrol

            form.Add(kontrol, 1, wx.EXPAND)

        # USIA OTOMATIS
        if "usia" in self.kolom:
            form.Add(
                wx.StaticText(
                    scroll_form, label="Usia otomatis:"
                ),
                0,
                wx.ALIGN_CENTER_VERTICAL
            )

            self.kontrol_usia = wx.TextCtrl(
                scroll_form,
                style=wx.TE_READONLY
            )

            form.Add(
                self.kontrol_usia, 1, wx.EXPAND
            )
        else:
            self.kontrol_usia = None

        scroll_form.SetSizer(form)

        kiri_sizer.Add(
            scroll_form, 1,
            wx.EXPAND | wx.LEFT | wx.RIGHT,
            12
        )

        tombol_simpan = wx.Button(
            kiri, label="SIMPAN DATA"
        )
        tombol_simpan.SetMinSize((-1, 40))
        tombol_simpan.SetBackgroundColour(WARNA_BIRU)
        tombol_simpan.SetForegroundColour(WARNA_PUTIH)

        tombol_simpan.Bind(
            wx.EVT_BUTTON,
            self.simpan_data
        )

        kiri_sizer.Add(
            tombol_simpan, 0,
            wx.EXPAND | wx.ALL,
            15
        )

        kiri.SetSizer(kiri_sizer)

        # EVENT HITUNG USIA
        if "tanggal_lahir" in self.inputan:
            self.inputan["tanggal_lahir"].Bind(
                wx.EVT_TEXT, self.perbarui_usia
            )

        self.perbarui_usia(None)

        # ==========================================
        # PANEL KANAN: DAFTAR PENDAFTAR
        # ==========================================

        kanan = wx.Panel(panel)
        kanan.SetBackgroundColour(WARNA_PUTIH)

        kanan_sizer = wx.BoxSizer(wx.VERTICAL)

        judul_list = wx.StaticText(
            kanan,
            label="DAFTAR PENDAFTAR"
        )
        judul_list.SetFont(wx.Font(
            12,
            wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_BOLD
        ))

        kanan_sizer.Add(
            judul_list, 0,
            wx.ALL,
            12
        )

        # TOMBOL EDIT DAN HAPUS
        baris_tombol = wx.BoxSizer(wx.HORIZONTAL)

        self.btn_edit = wx.Button(
            kanan, label="EDIT"
        )
        self.btn_hapus = wx.Button(
            kanan, label="HAPUS"
        )
        btn_refresh = wx.Button(
            kanan, label="REFRESH"
        )

        self.btn_edit.Bind(
            wx.EVT_BUTTON, self.edit_data
        )
        self.btn_hapus.Bind(
            wx.EVT_BUTTON, self.hapus_data
        )
        btn_refresh.Bind(
            wx.EVT_BUTTON,
            lambda event: self.muat_data()
        )

        baris_tombol.Add(
            self.btn_edit, 0, wx.RIGHT, 8
        )
        baris_tombol.Add(
            self.btn_hapus, 0, wx.RIGHT, 8
        )
        baris_tombol.Add(btn_refresh, 0)

        kanan_sizer.Add(
            baris_tombol, 0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            10
        )

        # TABEL DINAMIS
        self.tabel = wx.ListCtrl(
            kanan,
            style=(
                wx.LC_REPORT
                | wx.LC_SINGLE_SEL
                | wx.LC_HRULES
                | wx.LC_VRULES
            )
        )

        for indeks, nama_kolom in enumerate(self.kolom):
            self.tabel.InsertColumn(
                indeks,
                nama_kolom.replace("_", " ").upper()
            )

        self.tabel.Bind(
            wx.EVT_LIST_ITEM_SELECTED,
            self.pilih_baris
        )
        self.tabel.Bind(
            wx.EVT_LIST_ITEM_DESELECTED,
            self.pilih_baris
        )

        kanan_sizer.Add(
            self.tabel, 1,
            wx.EXPAND | wx.ALL,
            10
        )

        self.label_jumlah = wx.StaticText(
            kanan, label="Jumlah pendaftar: 0"
        )

        kanan_sizer.Add(
            self.label_jumlah, 0,
            wx.ALL,
            10
        )

        kanan.SetSizer(kanan_sizer)

        # PROPORSI KIRI DAN KANAN
        isi.Add(
            kiri, 0,
            wx.EXPAND | wx.ALL,
            8
        )

        kiri.SetMinSize((350, -1))

        isi.Add(
            kanan, 1,
            wx.EXPAND | wx.ALL,
            8
        )

        utama.Add(
            isi, 1,
            wx.EXPAND | wx.LEFT | wx.RIGHT,
            8
        )

        panel.SetSizer(utama)

        self.pilih_id = None

        self.muat_data()
        self.perbarui_tombol()

        self.Centre()
        self.Show()

    # ==========================================
    # USIA OTOMATIS
    # ==========================================

    def perbarui_usia(self, event):
        if self.kontrol_usia is not None:
            try:
                teks = self.inputan[
                    "tanggal_lahir"
                ].GetValue()

                lahir = parse_tanggal(teks)

                usia = hitung_usia(
                    lahir, date.today()
                )

                self.kontrol_usia.SetValue(usia)

            except ValueError:
                self.kontrol_usia.SetValue(
                    "Tanggal belum valid"
                )

        if event:
            event.Skip()

    # ==========================================
    # SIMPAN PENDAFTAR BARU
    # ==========================================

    def simpan_data(self, event):
        nilai_data = {}

        try:
            for nama_kolom, kontrol in self.inputan.items():

                if nama_kolom == "jenis_kelamin":
                    nilai = kontrol.GetStringSelection()
                else:
                    nilai = kontrol.GetValue().strip()

                if not nilai:
                    raise ValueError(
                        f"Kolom {nama_kolom} wajib diisi."
                    )

                if nama_kolom == "tanggal_lahir":
                    lahir = parse_tanggal(nilai)

                    if lahir > date.today():
                        raise ValueError(
                            "Tanggal lahir tidak boleh di masa depan."
                        )

                    nilai = lahir.isoformat()

                nilai_data[nama_kolom] = nilai

            if "usia" in self.kolom:
                lahir = date.fromisoformat(
                    nilai_data["tanggal_lahir"]
                )

                nilai_data["usia"] = hitung_usia(
                    lahir, date.today()
                )

            kolom_simpan = [
                nama for nama in self.kolom
                if nama != "id"
            ]

            # Isi kolom tambahan jika tersedia.
            for nama in kolom_simpan:
                if nama not in nilai_data:
                    nilai_data[nama] = ""

            nama_sql = ", ".join(
                kutip(nama) for nama in kolom_simpan
            )
            tanda_tanya = ", ".join(
                "?" for nama in kolom_simpan
            )

            with koneksi_database() as conn:
                conn.execute(
                    f"""
                    INSERT INTO tabel_orang ({nama_sql})
                    VALUES ({tanda_tanya})
                    """,
                    tuple(
                        nilai_data[nama]
                        for nama in kolom_simpan
                    )
                )

            wx.MessageBox(
                "Data berhasil disimpan!",
                "Berhasil",
                wx.OK | wx.ICON_INFORMATION,
                self
            )

            self.kosongkan_form()
            self.muat_data()

        except (ValueError, sqlite3.Error) as error:
            wx.MessageBox(
                str(error),
                "Gagal Menyimpan",
                wx.OK | wx.ICON_WARNING,
                self
            )

    def kosongkan_form(self):
        for nama_kolom, kontrol in self.inputan.items():
            if nama_kolom == "jenis_kelamin":
                kontrol.SetSelection(0)
            elif nama_kolom == "tanggal_lahir":
                kontrol.SetValue("01-01-2000")
            else:
                kontrol.SetValue("")

        self.perbarui_usia(None)

    # ==========================================
    # MEMUAT DATA DARI SQLITE
    # ==========================================

    def muat_data(self):
        # Baca ulang struktur tabel setiap refresh.
        kolom_baru = ambil_kolom()

        if kolom_baru != self.kolom:
            self.kolom = kolom_baru

            # Kolom tabel dibuat ulang otomatis.
            self.tabel.ClearAll()

            for indeks, nama_kolom in enumerate(self.kolom):
                self.tabel.InsertColumn(
                    indeks,
                    nama_kolom.replace("_", " ").upper()
                )

        self.tabel.DeleteAllItems()
        self.pilih_id = None

        try:
            with koneksi_database() as conn:
                hasil = conn.execute(
                    "SELECT * FROM tabel_orang ORDER BY id DESC"
                ).fetchall()

            for nomor, baris in enumerate(hasil):
                indeks = self.tabel.InsertItem(
                    nomor, str(baris[0])
                )

                for kolom_index in range(1, len(baris)):
                    nilai = baris[kolom_index]

                    if (
                        self.kolom[kolom_index]
                        == "tanggal_lahir"
                    ):
                        nilai = format_tanggal_form(
                            str(nilai)
                        )

                    self.tabel.SetItem(
                        indeks,
                        kolom_index,
                        str(nilai if nilai is not None else "")
                    )

                # WARNA BELANG PUTIH DAN ABU-ABU
                if nomor % 2 == 0:
                    self.tabel.SetItemBackgroundColour(
                        indeks, WARNA_PUTIH
                    )
                else:
                    self.tabel.SetItemBackgroundColour(
                        indeks, WARNA_ABU
                    )

            # Lebar kolom menyesuaikan isi.
            for indeks in range(len(self.kolom)):
                self.tabel.SetColumnWidth(
                    indeks, wx.LIST_AUTOSIZE_USEHEADER
                )

            self.label_jumlah.SetLabel(
                f"Jumlah pendaftar: {len(hasil)}"
            )

        except sqlite3.Error as error:
            wx.MessageBox(
                str(error),
                "Kesalahan Database",
                wx.OK | wx.ICON_ERROR,
                self
            )

        self.perbarui_tombol()

    # ==========================================
    # PILIH BARIS
    # ==========================================

    def pilih_baris(self, event):
        indeks = self.tabel.GetFirstSelected()

        if indeks != -1:
            self.pilih_id = self.tabel.GetItemText(
                indeks, 0
            )
        else:
            self.pilih_id = None

        self.perbarui_tombol()

        if event:
            event.Skip()

    def perbarui_tombol(self):
        ada_pilihan = self.pilih_id is not None

        self.btn_edit.Enable(ada_pilihan)
        self.btn_hapus.Enable(ada_pilihan)

    # ==========================================
    # EDIT DATA
    # ==========================================

    def edit_data(self, event):
        if self.pilih_id is None:
            return

        with koneksi_database() as conn:
            conn.row_factory = sqlite3.Row

            hasil = conn.execute(
                "SELECT * FROM tabel_orang WHERE id = ?",
                (self.pilih_id,)
            ).fetchone()

        if hasil is None:
            wx.MessageBox(
                "Data tidak ditemukan.",
                "Peringatan",
                wx.OK | wx.ICON_WARNING,
                self
            )
            self.muat_data()
            return

        data_lama = dict(hasil)

        dialog = DialogEdit(
            self, self.kolom, data_lama
        )

        if dialog.ShowModal() == wx.ID_OK:
            try:
                data_baru = dialog.data_baru

                kolom_update = [
                    nama for nama in self.kolom
                    if nama != "id"
                ]

                # Pastikan kolom usia dihitung ulang.
                if (
                    "usia" in kolom_update
                    and "tanggal_lahir" in data_baru
                ):
                    lahir = date.fromisoformat(
                        data_baru["tanggal_lahir"]
                    )

                    data_baru["usia"] = hitung_usia(
                        lahir, date.today()
                    )

                set_sql = ", ".join(
                    f"{kutip(nama)} = ?"
                    for nama in kolom_update
                )

                with koneksi_database() as conn:
                    conn.execute(
                        f"""
                        UPDATE tabel_orang
                        SET {set_sql}
                        WHERE id = ?
                        """,
                        tuple(
                            data_baru.get(nama, "")
                            for nama in kolom_update
                        ) + (self.pilih_id,)
                    )

                wx.MessageBox(
                    "Data berhasil diperbarui!",
                    "Berhasil",
                    wx.OK | wx.ICON_INFORMATION,
                    self
                )

                self.muat_data()

            except (ValueError, sqlite3.Error) as error:
                wx.MessageBox(
                    str(error),
                    "Gagal Mengedit",
                    wx.OK | wx.ICON_ERROR,
                    self
                )

        dialog.Destroy()

    # ==========================================
    # HAPUS DATA
    # ==========================================

    def hapus_data(self, event):
        if self.pilih_id is None:
            return

        indeks = self.tabel.GetFirstSelected()

        if indeks == -1:
            return

        nama = (
            self.tabel.GetItemText(indeks, 1)
            if len(self.kolom) > 1
            else self.pilih_id
        )

        konfirmasi = wx.MessageBox(
            f"Yakin ingin menghapus data:\n\n{nama}?",
            "Konfirmasi Hapus",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
            self
        )

        if konfirmasi != wx.YES:
            return

        try:
            with koneksi_database() as conn:
                conn.execute(
                    "DELETE FROM tabel_orang WHERE id = ?",
                    (self.pilih_id,)
                )

            wx.MessageBox(
                "Data berhasil dihapus.",
                "Berhasil",
                wx.OK | wx.ICON_INFORMATION,
                self
            )

            self.muat_data()

        except sqlite3.Error as error:
            wx.MessageBox(
                str(error),
                "Gagal Menghapus",
                wx.OK | wx.ICON_ERROR,
                self
            )


# ==================================================
# JALANKAN APLIKASI
# ==================================================

if __name__ == "__main__":
    buat_database()

    app = wx.App()
    frame = FormPendaftaran()

    app.MainLoop()
