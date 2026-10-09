
import wx
from PIL import Image
import os


class IcoConverter(wx.Frame):
    def __init__(self):
        super().__init__(
            parent=None,
            title="Image to ICO Converter by Samman - V1",
            size=(600, 570)
        )

        self.file_gambar = ""

        panel = wx.Panel(self)
        panel.SetBackgroundColour("#F5F5F5")

        # ==============================
        # JUDUL APLIKASI
        # ==============================
        judul = wx.StaticText(
            panel,
            label="IMAGE TO ICO CONVERTER"
        )

        judul.SetFont(wx.Font(
            17,
            wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL,
            wx.FONTWEIGHT_BOLD
        ))

        # ==============================
        # PREVIEW GAMBAR
        # ==============================
        self.preview = wx.StaticBitmap(
            panel,
            bitmap=wx.Bitmap(250, 200)
        )

        self.label_file = wx.StaticText(
            panel,
            label="Belum ada gambar dipilih"
        )

        # ==============================
        # TOMBOL PILIH FOTO
        # ==============================
        tombol_pilih = wx.Button(
            panel,
            label="Pilih Foto"
        )

        tombol_pilih.Bind(
            wx.EVT_BUTTON,
            self.pilih_foto
        )

        # ==============================
        # PENGATURAN UKURAN
        # ==============================
        label_ukuran = wx.StaticText(
            panel,
            label="Ukuran ICO:"
        )

        self.lebar = wx.SpinCtrl(
            panel,
            min=1,
            max=256,
            initial=256
        )

        self.tinggi = wx.SpinCtrl(
            panel,
            min=1,
            max=256,
            initial=256
        )

        label_x = wx.StaticText(
            panel,
            label="X"
        )

        baris_ukuran = wx.BoxSizer(wx.HORIZONTAL)
        baris_ukuran.Add(label_ukuran, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        baris_ukuran.Add(self.lebar, 1, wx.RIGHT, 8)
        baris_ukuran.Add(label_x, 0,
                         wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        baris_ukuran.Add(self.tinggi, 1)

        # ==============================
        # TOMBOL CONVERT
        # ==============================
        tombol_convert = wx.Button(
            panel,
            label="Convert to ICO",
            size=(-1, 42)
        )

        tombol_convert.SetBackgroundColour("#1976D2")
        tombol_convert.SetForegroundColour("white")

        tombol_convert.Bind(
            wx.EVT_BUTTON,
            self.convert_ico
        )

        # ==============================
        # FOOTER
        # ==============================
        footer = wx.StaticText(
            panel,
            label="ICO Converter | wxPython + Pillow"
        )

        footer.SetForegroundColour("#666666")

        # ==============================
        # TATA LETAK APLIKASI
        # ==============================
        layout = wx.BoxSizer(wx.VERTICAL)

        layout.Add(judul, 0,
                   wx.ALIGN_CENTER | wx.TOP | wx.BOTTOM, 15)

        layout.Add(self.preview, 0,
                   wx.ALIGN_CENTER | wx.ALL, 8)

        layout.Add(self.label_file, 0,
                   wx.ALIGN_CENTER | wx.BOTTOM, 8)

        layout.Add(tombol_pilih, 0,
                   wx.ALIGN_CENTER | wx.BOTTOM, 18)

        layout.Add(baris_ukuran, 0,
                   wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 35)

        layout.Add(tombol_convert, 0,
                   wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 35)

        layout.AddStretchSpacer()

        layout.Add(footer, 0,
                   wx.ALIGN_CENTER | wx.BOTTOM, 12)

        panel.SetSizer(layout)

        self.Centre()
        self.Show()

    # ==============================
    # PILIH GAMBAR
    # ==============================
    def pilih_foto(self, event):

        dialog = wx.FileDialog(
            self,
            "Pilih gambar",
            wildcard=(
                "File gambar (*.png;*.jpg;*.jpeg;*.bmp;*.webp)|"
                "*.png;*.jpg;*.jpeg;*.bmp;*.webp"
            ),
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST
        )

        if dialog.ShowModal() == wx.ID_OK:
            self.file_gambar = dialog.GetPath()

            nama = os.path.basename(self.file_gambar)
            self.label_file.SetLabel(nama)

            # Tampilkan preview gambar
            try:
                gambar = wx.Image(self.file_gambar)

                gambar = gambar.Scale(
                    250,
                    200,
                    wx.IMAGE_QUALITY_HIGH
                )

                self.preview.SetBitmap(wx.Bitmap(gambar))
                self.Layout()

            except Exception as e:
                wx.MessageBox(
                    f"Gagal menampilkan gambar:\n{e}",
                    "Error",
                    wx.OK | wx.ICON_ERROR
                )

        dialog.Destroy()

    # ==============================
    # CONVERT KE ICO
    # ==============================
    def convert_ico(self, event):

        if not self.file_gambar:
            wx.MessageBox(
                "Silakan pilih foto terlebih dahulu!",
                "Peringatan",
                wx.OK | wx.ICON_WARNING
            )
            return

        lebar = self.lebar.GetValue()
        tinggi = self.tinggi.GetValue()

        try:
            # Folder output di samping script
            folder_aplikasi = os.path.dirname(
                os.path.abspath(__file__)
            )

            folder_output = os.path.join(
                folder_aplikasi,
                "output ico"
            )

            os.makedirs(folder_output, exist_ok=True)

            # Nama output mengikuti gambar sumber
            nama_file = os.path.splitext(
                os.path.basename(self.file_gambar)
            )[0]

            lokasi_output = os.path.join(
                folder_output,
                nama_file + ".ico"
            )

            # Buka gambar dan pertahankan transparansi
            with Image.open(self.file_gambar) as gambar:
                gambar = gambar.convert("RGBA")

                gambar = gambar.resize(
                    (lebar, tinggi),
                    Image.Resampling.LANCZOS
                )

                gambar.save(
                    lokasi_output,
                    format="ICO",
                    sizes=[(lebar, tinggi)]
                )

            wx.MessageBox(
                "Convert berhasil!\n\n"
                f"Ukuran: {lebar} x {tinggi}\n\n"
                f"Lokasi:\n{lokasi_output}",
                "Berhasil",
                wx.OK | wx.ICON_INFORMATION
            )

        except Exception as e:
            wx.MessageBox(
                f"Gagal mengubah gambar:\n{e}",
                "Error",
                wx.OK | wx.ICON_ERROR
            )


if __name__ == "__main__":
    app = wx.App()
    frame = IcoConverter()
    app.MainLoop()