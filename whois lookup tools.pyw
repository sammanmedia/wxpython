import wx
import threading
import traceback
import socket
import subprocess
import platform
from concurrent.futures import ThreadPoolExecutor


class MainFrame(wx.Frame):
    def __init__(self):
        super().__init__(None, title="Whois Tools", size=(700, 500))
        panel = wx.Panel(self)

        self.domain = wx.TextCtrl(panel, style=wx.TE_PROCESS_ENTER)
        self.btn = wx.Button(panel, label="Go")
        self.btn.Bind(wx.EVT_BUTTON, self.on_go)
        self.domain.Bind(wx.EVT_TEXT_ENTER, self.on_go)

        labels = ["domain whois record", "DNS records", "traceroute",
                  "network whois record", "service scan"]
        self.checks = {}
        grid = wx.GridSizer(rows=2, cols=3, vgap=8, hgap=15)
        for text in labels:
            cb = wx.CheckBox(panel, label=text)
            cb.SetValue(True)
            self.checks[text] = cb
            grid.Add(cb)

        font = wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        self.output = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.output.SetFont(font)

        top = wx.BoxSizer(wx.HORIZONTAL)
        top.Add(self.domain, 1, wx.RIGHT, 8)
        top.Add(self.btn)

        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(top, 0, wx.EXPAND | wx.ALL, 10)
        main.Add(grid, 0, wx.LEFT | wx.RIGHT, 10)
        main.Add(self.output, 1, wx.EXPAND | wx.ALL, 10)
        panel.SetSizer(main)

    # ---------- util ----------
    def log(self, text):
        wx.CallAfter(self.output.AppendText, str(text) + "\n")

    def on_go(self, event):
        target = self.domain.GetValue().strip()
        target = target.replace("https://", "").replace("http://", "").split("/")[0]
        if not target:
            self.output.SetValue("Isi domain dulu bre.\n")
            return
        dipilih = [t for t, cb in self.checks.items() if cb.GetValue()]
        if not dipilih:
            self.output.SetValue("Centang minimal satu opsi.\n")
            return
        self.output.SetValue(f"Mulai untuk {target}...\n\n")
        self.btn.Disable()
        threading.Thread(target=self.worker, args=(target, dipilih), daemon=True).start()

    def worker(self, target, dipilih):
        handlers = {
            "domain whois record": self.do_domain_whois,
            "DNS records": self.do_dns,
            "traceroute": self.do_traceroute,
            "network whois record": self.do_network_whois,
            "service scan": self.do_service_scan,
        }
        try:
            for item in dipilih:
                self.log(f"== {item} ==")
                try:
                    handlers[item](target)
                except Exception:
                    self.log("ERROR:\n" + traceback.format_exc())
                self.log("")
        finally:
            wx.CallAfter(self.btn.Enable)
            self.log("Selesai.")

    # ---------- fitur ----------
    def do_domain_whois(self, target):
        import whois
        w = whois.whois(target)
        for k, v in w.items():
            self.log(f"{k}: {v}")

    def do_dns(self, target):
        import dns.resolver
        found = False
        for rtype in ["A", "AAAA", "MX", "NS", "TXT", "SOA", "CNAME"]:
            try:
                answers = dns.resolver.resolve(target, rtype)
                for r in answers:
                    self.log(f"{rtype}\t{r.to_text()}")
                    found = True
            except Exception:
                pass
        if not found:
            self.log("Tidak ada record yang ditemukan.")

    def do_network_whois(self, target):
        from ipwhois import IPWhois
        ip = socket.gethostbyname(target)
        self.log(f"IP: {ip}")
        res = IPWhois(ip).lookup_rdap(depth=1)
        net = res.get("network", {})
        self.log(f"ASN: {res.get('asn')} ({res.get('asn_description')})")
        self.log(f"Network name: {net.get('name')}")
        self.log(f"CIDR: {net.get('cidr')}")
        self.log(f"Country: {net.get('country')}")

    def do_traceroute(self, target):
        if platform.system() == "Windows":
            cmd = ["tracert", "-d", "-h", "20", target]
            flags = subprocess.CREATE_NO_WINDOW
        else:
            cmd = ["traceroute", "-n", "-m", "20", target]
            flags = 0
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, creationflags=flags)
        for line in p.stdout:
            self.log(line.rstrip())

    def do_service_scan(self, target):
        ports = {21: "FTP", 22: "SSH", 25: "SMTP", 53: "DNS", 80: "HTTP",
                 110: "POP3", 143: "IMAP", 443: "HTTPS", 465: "SMTPS",
                 587: "SMTP-Sub", 993: "IMAPS", 995: "POP3S",
                 3306: "MySQL", 8080: "HTTP-Alt"}

        ip = socket.gethostbyname(target)
        self.log(f"Scanning {target} ({ip}) ...")

        def check(item):
            port, name = item
            s = socket.socket()
            s.settimeout(2)
            try:
                ok = s.connect_ex((ip, port)) == 0
            except Exception:
                ok = False
            finally:
                s.close()
            return port, name, ok

        with ThreadPoolExecutor(max_workers=14) as ex:
            for port, name, ok in ex.map(check, ports.items()):
                self.log(f"{port:<6}{name:<10}{'OPEN' if ok else 'closed/filtered'}")


if __name__ == "__main__":
    app = wx.App()
    frame = MainFrame()
    frame.Show()
    app.MainLoop()
