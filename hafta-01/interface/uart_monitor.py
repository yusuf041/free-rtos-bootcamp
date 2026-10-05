# uart_monitor.py  —  Sürüm 3 (baud seçimi, senaryo ve zaman aralığı açıklamaları)
# Ödev 01 PC arayüzü: telemetri izleme, buton yanıtı, deney yönetimi, CSV ve grafik.
#
# Gerekli paketler:  pip install pyserial matplotlib
# Çalıştırma:        python uart_monitor.py
#
# Yapı:
#   SerialReader (ayrı thread)  --kuyruk-->  App (pencere, ana thread)
#   Seri portu okur, '\n' ile çerçeveler      50 ms'de bir kuyruğu boşaltır, mesajları işler
#
#   Ayrıştırma, CSV ve özet hesapları uart_protocol.py'dedir (pencereden bağımsız).
#
# Arayüz ölçüme KARIŞMAZ: tüm zamanlar kartta ölçülür, PC sadece deney sonunda
# REC satırlarını alıp kaydeder. PC saatiyle hiçbir süre hesaplanmaz.

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

import serial
from serial.tools import list_ports

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure                                  # noqa: E402
from matplotlib.backends.backend_tkagg import (FigureCanvasTkAgg,     # noqa: E402
                                               NavigationToolbar2Tk)

import uart_protocol as up                                            # noqa: E402

MAX_LOG_LINES = 800
POLL_MS = 50
MAX_PER_POLL = 300      # pencere kilitlenmesin: her turda en fazla bu kadar mesaj işle
DUMP_TIMEOUT_S = 5.0    # 'D'den sonra bu sürede END gelmezse uyar
TICK_MS = 250
WARMUP_S = 5.0          # prosedür: 5 s ısınma
TARGET_PRESSES = 30     # prosedür: en az 30 kabul edilen basış
KEEP_LOGS = 3           # her ölçüm için en fazla 3 eski log / CSV yedeği tutulur

# Varsayılan kayıt klasörü: hafta-01/interface/ -> hafta-01/measurements/
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "measurements"

# Grafik renkleri (doğrulanmış kategorik paletin ilk dört sırası, sabit sırada)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e6e5e1"
STAGE_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
CRITICAL = "#e34948"      # yalnız deadline / hata durumu için
HIGHLIGHT = "#cde2fb"     # BTN göstergesinin kısa vurgusu


# ---------------------------------------------------------------------------
# Seri port okuyucu (ayrı thread)
# ---------------------------------------------------------------------------
class SerialReader(threading.Thread):
    def __init__(self, ser, out_q):
        super().__init__(daemon=True)
        self.ser = ser
        self.out_q = out_q
        self.stop_event = threading.Event()

    def run(self):
        buf = bytearray()
        while not self.stop_event.is_set():
            try:
                chunk = self.ser.read(self.ser.in_waiting or 1)
            except serial.SerialException as e:
                self.out_q.put(("error", str(e)))
                return
            if not chunk:
                continue
            buf += chunk
            # ÇERÇEVELEME: '\n' oldukça tam satırları kes
            while True:
                i = buf.find(b"\n")
                if i < 0:
                    break
                frame = bytes(buf[: i + 1])
                del buf[: i + 1]
                self.out_q.put(("line", frame))

    def stop(self):
        self.stop_event.set()


# ---------------------------------------------------------------------------
# Pencere
# ---------------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("UART Monitor — Ödev 01")
        self.geometry("1280x820")
        self.minsize(1000, 650)

        self.ser = None
        self.reader = None
        self.rx_q = queue.Queue()

        self.out_dir = DEFAULT_OUT
        self.exp = None             # o anki deney (up.Experiment)
        self.phase = "idle"         # idle | running | dumping | done
        self.plot_exp = None        # grafikte gösterilen son tamamlanmış deney
        self.cur_baud = up.DEFAULT_BAUD   # kart ve PC'nin şu an kullandığı hız
        self.pending_baud = None          # istenen ama henüz onaylanmamış hız
        self._baud_job = None
        self.bad_frames = 0
        self.ignored_tel = 0
        self.summaries = up.read_summary_csv(self.out_dir / "summary.csv")
        self._flash_job = None

        self._build_ui()
        self.refresh_ports()
        self.refresh_summary_table()
        self.refresh_scenario_info()
        self.draw_plots()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(POLL_MS, self.poll_queue)
        self.after(TICK_MS, self.tick)

    # =======================================================================
    # Arayüz
    # =======================================================================
    def _build_ui(self):
        # --- Port satırı ---------------------------------------------------
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Port:").pack(side="left")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, width=42, state="readonly")
        self.port_box.pack(side="left", padx=4)
        ttk.Button(top, text="Yenile", command=self.refresh_ports).pack(side="left")
        ttk.Label(top, text="Baud:").pack(side="left", padx=(12, 2))
        self.baud_var = tk.StringVar(value=str(up.DEFAULT_BAUD))
        self.baud_box = ttk.Combobox(top, textvariable=self.baud_var, width=8, state="readonly",
                                     values=[str(b) for b in up.BAUDS])
        self.baud_box.pack(side="left")
        self.baud_box.bind("<<ComboboxSelected>>", self.on_baud_selected)
        self.connect_btn = ttk.Button(top, text="Bağlan", command=self.toggle_connection)
        self.connect_btn.pack(side="left", padx=(12, 0))

        self.out_var = tk.StringVar(value=f"Kayıt klasörü: {self.out_dir}")
        ttk.Button(top, text="Klasör seç…", command=self.choose_out_dir).pack(side="right")
        ttk.Label(top, textvariable=self.out_var, foreground=INK2).pack(side="right", padx=8)

        # --- Komut satırı --------------------------------------------------
        cmd = ttk.Frame(self, padding=(8, 0, 8, 8))
        cmd.pack(fill="x")
        ttk.Label(cmd, text="Senaryo başlat:").pack(side="left")
        for i in range(6):
            scn = f"S{i}"
            info = up.SCENARIOS[scn]
            freq = "kapalı" if info["period_ms"] == 0 else f"{1000 // info['period_ms']} Hz"
            work = f" + {info['work_us'] / 1000:g} ms iş" if info["work_us"] else ""
            ttk.Button(cmd, text=f"{scn}\n{freq}{work}", width=14,
                       command=lambda c=str(i): self.send_cmd(c)).pack(side="left", padx=2)
        ttk.Button(cmd, text="Deneyi bitir ve kaydet (D)",
                   command=lambda: self.send_cmd("D")).pack(side="left", padx=(14, 0))

        # --- Ana alan: sol bilgi paneli | sağ sekmeler ---------------------
        main = ttk.Panedwindow(self, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8)

        left = ttk.Frame(main, padding=(0, 0, 8, 0))
        main.add(left, weight=0)

        exp_box = ttk.LabelFrame(left, text="Deney", padding=8)
        exp_box.pack(fill="x")
        self.scn_var = tk.StringVar(value="Senaryo: —")
        self.tel_var = tk.StringVar(value="Telemetri: —")
        self.warm_var = tk.StringVar(value="")
        self.press_var = tk.StringVar(value="")
        ttk.Label(exp_box, textvariable=self.scn_var, font=("Segoe UI", 11, "bold"),
                  wraplength=290).pack(anchor="w")
        self.goal_var = tk.StringVar(value="")
        ttk.Label(exp_box, textvariable=self.goal_var, foreground=INK2,
                  wraplength=290).pack(anchor="w")
        ttk.Label(exp_box, textvariable=self.tel_var, wraplength=290).pack(anchor="w", pady=(4, 0))
        ttk.Label(exp_box, textvariable=self.warm_var).pack(anchor="w", pady=(4, 0))
        ttk.Label(exp_box, textvariable=self.press_var).pack(anchor="w", pady=(4, 0))

        # BTN göstergesi
        self.btn_label = tk.Label(left, text="Buton bekleniyor", font=("Segoe UI", 15, "bold"),
                                  relief="groove", bd=2, pady=18, width=24)
        self.btn_label.pack(fill="x", pady=10)
        self._btn_bg = self.btn_label.cget("background")

        res_box = ttk.LabelFrame(left, text="Son tamamlanan deney", padding=8)
        res_box.pack(fill="both", expand=True)
        self.result_var = tk.StringVar(value="Henüz yok.\nBir senaryo başlat, butona bas,\n"
                                             "sonra 'Deneyi bitir' de.")
        ttk.Label(res_box, textvariable=self.result_var, justify="left",
                  font=("Consolas", 10), wraplength=300).pack(anchor="nw")

        tabs = ttk.Notebook(main)
        main.add(tabs, weight=1)

        # Sekme 1: grafikler
        plot_tab = ttk.Frame(tabs)
        tabs.add(plot_tab, text="Grafikler")
        ctl = ttk.Frame(plot_tab, padding=(0, 4))
        ctl.pack(fill="x")
        self.show_deadline = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Zaman çizelgesinde 20 ms deadline'ı ölçeğe dahil et",
                        variable=self.show_deadline, command=self.draw_plots).pack(side="left")
        ttk.Label(ctl, foreground=INK2,
                  text="   Grafikler son tamamlanan deneyi gösterir. Yakınlaştırmak için alttaki "
                       "büyüteci seç ve alanı sürükle; ev simgesi geri alır.").pack(side="left")

        self.fig = Figure(figsize=(8, 7.5), dpi=100, facecolor=SURFACE)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_tab)
        tb_frame = ttk.Frame(plot_tab)
        tb_frame.pack(side="bottom", fill="x")
        self.toolbar = NavigationToolbar2Tk(self.canvas, tb_frame, pack_toolbar=False)
        self.toolbar.pack(side="left", fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)

        # Sekme: olay kayıtları (CSV'deki gibi, her buton olayı bir satır)
        ev_tab = ttk.Frame(tabs, padding=4)
        tabs.add(ev_tab, text="Olay kayıtları")
        self.ev_info = tk.StringVar(value="Deney başlayınca her basış buraya eklenir; "
                                          "zamanlar 'Deneyi bitir' ile karttan gelince dolar.")
        ttk.Label(ev_tab, textvariable=self.ev_info, foreground=INK2).pack(anchor="w", pady=(0, 4))
        ecols = [("id", "Olay", 50), ("t0", "t₀ (µs)", 100),
                 ("d1", "t₁−t₀ ISR→Task", 110), ("d2", "t₂−t₁ Hazırlama", 110),
                 ("d3", "t₃−t₂ TX kuyruğu", 120), ("d4", "t₄−t₃ UART+TC", 110),
                 ("r", "R = t₄−t₀", 100), ("st", "Durum", 120)]
        ev_frame = ttk.Frame(ev_tab)
        ev_frame.pack(fill="both", expand=True)
        self.ev_tree = ttk.Treeview(ev_frame, columns=[c[0] for c in ecols], show="headings")
        for key, title, width in ecols:
            self.ev_tree.heading(key, text=title)
            self.ev_tree.column(key, width=width, anchor="e" if key not in ("st",) else "center")
        ev_scroll = ttk.Scrollbar(ev_frame, command=self.ev_tree.yview)
        self.ev_tree.configure(yscrollcommand=ev_scroll.set)
        self.ev_tree.pack(side="left", fill="both", expand=True)
        ev_scroll.pack(side="right", fill="y")
        self.ev_tree.tag_configure("late", foreground=CRITICAL)
        self.ev_tree.tag_configure("bad", foreground=CRITICAL, background="#fbe9e9")
        self.ev_tree.tag_configure("wait", foreground=INK2)
        ttk.Label(ev_tab, foreground=INK2, padding=(0, 4),
                  text="Süreler ms. Kırmızı: R > 20 ms. Kırmızı zemin: kayıp / hata "
                       "(ölçülemeyen zaman boş bırakılır).").pack(anchor="w")

        # Sekme 2: mesajlar
        log_tab = ttk.Frame(tabs)
        tabs.add(log_tab, text="Mesajlar")
        opts = ttk.Frame(log_tab, padding=(0, 4))
        opts.pack(fill="x")
        self.show_tel = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="TEL satırlarını da göster (100 Hz'de çok hızlı akar)",
                        variable=self.show_tel).pack(side="left")
        ttk.Button(opts, text="Temizle", command=self.clear_log).pack(side="right")
        log_frame = ttk.Frame(log_tab)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, font=("Consolas", 10), state="disabled", wrap="none")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log.tag_configure("info", foreground="#2a78d6")
        self.log.tag_configure("bad", foreground=CRITICAL)
        self.log.tag_configure("btn", font=("Consolas", 10, "bold"))
        self.log.tag_configure("dim", foreground="#8a8984")

        # Sekme 3: özet tablo
        tab_sum = ttk.Frame(tabs)
        tabs.add(tab_sum, text="Özet tablo")
        cols = [("scenario", "Senaryo", 70), ("tel", "Telemetri hedef / ölçülen", 170),
                ("work", "Ek iş (ölçülen ort.)", 140), ("ok", "ok / kayıt", 80),
                ("rmin", "R min", 70), ("ravg", "R ort", 70), ("rmax", "R max", 70),
                ("over", "> 20 ms", 70), ("loss", "Kayıp / hata", 100),
                ("txq", "TXQ max", 70), ("stages", "Ort. aşamalar (ms)", 230)]
        self.tree = ttk.Treeview(tab_sum, columns=[c[0] for c in cols], show="headings")
        for key, title, width in cols:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)
        ttk.Label(tab_sum, foreground=INK2, padding=4,
                  text="R = t₄ − t₀ (ms), yalnız 'ok' olaylar. Aşamalar: görev bekleme / "
                       "hazırlama / TX öncesi / UART+TC.").pack(anchor="w")

        # Sekme 4: senaryolar
        scn_tab = ttk.Frame(tabs, padding=8)
        tabs.add(scn_tab, text="Senaryolar")
        scols = [("scn", "Senaryo", 70), ("tel", "Telemetri", 130), ("work", "Ek CPU işi", 110),
                 ("cpu", "Ek CPU talebi", 110), ("uart", "UART hat kullanımı", 150),
                 ("goal", "Amaç", 380)]
        self.scn_tree = ttk.Treeview(scn_tab, columns=[c[0] for c in scols], show="headings",
                                     height=6)
        for key, title, width in scols:
            self.scn_tree.heading(key, text=title)
            self.scn_tree.column(key, width=width, anchor="w" if key == "goal" else "center")
        self.scn_tree.pack(fill="x")
        self.scn_note = tk.Text(scn_tab, height=12, wrap="word", font=("Segoe UI", 10),
                                relief="flat", background=SURFACE)
        self.scn_note.pack(fill="both", expand=True, pady=(8, 0))

        # Sekme 5: zaman aralıkları
        st_tab = ttk.Frame(tabs, padding=8)
        tabs.add(st_tab, text="Zaman aralıkları")
        st = tk.Text(st_tab, wrap="word", font=("Segoe UI", 10), relief="flat", background=SURFACE)
        st.pack(fill="both", expand=True)
        st.tag_configure("h", font=("Segoe UI", 11, "bold"))
        st.tag_configure("code", font=("Consolas", 10))
        st.insert("end", "Bir buton olayının yolu (hepsi kartın TIM2 sayacıyla, 1 µs çözünürlük)\n", "h")
        st.insert("end",
                  "Buton ─EXTI0─▶ [ISR] ─buton kuyruğu─▶ [ButtonTask] ─TX kuyruğu─▶ "
                  "[UartTxTask] ─IT─▶ USART2 ─▶ PC\n"
                  "         t₀               t₁        t₂                    t₃          t₄ (TC)\n\n",
                  "code")
        for name, title, body in up.STAGE_HELP:
            st.insert("end", f"{name}  ·  {title}\n", "h")
            st.insert("end", body + "\n\n")
        st.insert("end", "Notlar\n", "h")
        st.insert("end",
                  "• Tüm aralıklar 'duvar saati'dir: araya giren kesmeler ve daha yüksek öncelikli "
                  "görevler dahildir; saf CPU süresi değildir.\n"
                  "• PC saati hiçbir hesapta kullanılmaz; PC sadece kayıtları alır.\n"
                  "• Grafiklerdeki renkler bu dört aralığa karşılık gelir.")
        st.configure(state="disabled")

        # --- Durum çubuğu ---------------------------------------------------
        self.status_var = tk.StringVar(value="Bağlı değil")
        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(8, 4)).pack(fill="x")

    # =======================================================================
    # Port
    # =======================================================================
    def refresh_ports(self):
        ports = list_ports.comports()
        self.port_map = {f"{p.device} — {p.description}": p.device for p in ports}
        self.port_box["values"] = list(self.port_map.keys())
        if self.port_map and not self.port_var.get():
            self.port_box.current(0)

    def toggle_connection(self):
        self.disconnect() if self.ser else self.connect()

    def connect(self):
        label = self.port_var.get()
        if not label:
            messagebox.showwarning("Port yok", "Önce bir port seçin.")
            return
        device = self.port_map[label]
        try:
            baud = int(self.baud_var.get())
            self.ser = serial.Serial(device, baud, timeout=0.1)
            self.cur_baud = baud
        except serial.SerialException as e:
            messagebox.showerror("Bağlanılamadı", str(e))
            self.ser = None
            return
        self.reader = SerialReader(self.ser, self.rx_q)
        self.reader.start()
        self.connect_btn.configure(text="Bağlantıyı Kes")
        self.port_box.configure(state="disabled")
        self.add_line(f"--- {device} açıldı ({self.cur_baud} baud) ---", "info")
        if self.cur_baud != up.DEFAULT_BAUD:
            self.add_line("Not: Kart her açılışta 115200 ile başlar. Kart resetlendiyse 115200 "
                          "ile bağlanın.", "bad")
        self.refresh_scenario_info()
        self.update_status()

    def disconnect(self):
        if self.reader:
            self.reader.stop()
            self.reader.join(timeout=1)
            self.reader = None
        if self.ser:
            self.ser.close()
            self.ser = None
        self.connect_btn.configure(text="Bağlan")
        self.port_box.configure(state="readonly")
        self.add_line("--- bağlantı kapatıldı ---", "info")
        self.update_status()

    def choose_out_dir(self):
        d = filedialog.askdirectory(initialdir=str(self.out_dir), title="Ölçüm klasörü")
        if d:
            self.out_dir = Path(d)
            self.out_var.set(f"Kayıt klasörü: {self.out_dir}")
            self.summaries = up.read_summary_csv(self.out_dir / "summary.csv")
            self.refresh_summary_table()
            self.draw_plots()

    # --- Baud değişimi ---------------------------------------------------
    def on_baud_selected(self, _evt=None):
        baud = int(self.baud_var.get())
        if not self.ser:
            self.refresh_scenario_info()       # bağlı değil: sadece bağlanma hızı
            if baud != up.DEFAULT_BAUD:
                messagebox.showinfo("Bağlanma hızı",
                                    "Kart her açılışta 115200 ile başlar. Kart resetlendiyse "
                                    "115200 ile bağlanın; hızı bağlandıktan sonra bu kutudan "
                                    "değiştirin, kart da birlikte değişir.")
            return
        if baud == self.cur_baud:
            return
        if self.phase in ("running", "dumping"):
            messagebox.showwarning("Deney sürüyor",
                                   "Baud, deney sırasında değiştirilemez. Önce 'Deneyi bitir'.")
            self.baud_var.set(str(self.cur_baud))
            return
        if baud != up.DEFAULT_BAUD and not messagebox.askyesno(
                "Standart dışı baud",
                f"Ödev ölçümleri 115200 baud ile yapılır. {baud} ile yapılan ölçümler "
                f"ayrı etiketle (ör. S3_b{baud}.csv) kaydedilir. Devam edilsin mi?"):
            self.baud_var.set(str(self.cur_baud))
            return
        self.pending_baud = baud
        self.ser.write(up.BAUD_CMD[baud].encode("ascii"))
        self.add_line(f">>> baud değişimi istendi: {self.cur_baud} → {baud}", "info")
        self._baud_job = self.after(2000, self.baud_timeout)

    def baud_timeout(self):
        self._baud_job = None
        if self.pending_baud is not None:
            self.add_line(f"Kart baud değişimini onaylamadı; {self.cur_baud} ile devam.", "bad")
            self.pending_baud = None
            self.baud_var.set(str(self.cur_baud))

    def handle_bau(self, f: list):
        baud, state = int(f[0]), f[1].strip()
        if state == "SWITCH":
            if self._baud_job:
                self.after_cancel(self._baud_job)
                self._baud_job = None
            self.ser.baudrate = baud           # PC portunu da yeni hıza al
            self.cur_baud = baud
            self.pending_baud = None
            self.add_line(f"Kart {baud} baud'a geçiyor, PC portu da değiştirildi…", "info")
        elif state == "OK":
            self.cur_baud = baud
            self.baud_var.set(str(baud))
            self.add_line(f"✓ Kart ve PC artık {baud} baud. Senaryo bu hızla yeniden başlıyor.",
                          "info")
        self.phase = "idle"
        self.refresh_scenario_info()
        self.update_status()

    def send_cmd(self, c: str):
        if not self.ser:
            messagebox.showwarning("Bağlı değil", "Önce karta bağlanın.")
            return
        if c == "D":
            if self.phase != "running":
                if not messagebox.askyesno("Deney yok",
                                           "Bu oturumda başlatılmış bir senaryo görünmüyor "
                                           "(INF alınmadı). Yine de kayıtlar istensin mi?"):
                    return
            self.phase = "dumping"
            self.after(int(DUMP_TIMEOUT_S * 1000), self.check_dump)
        self.ser.write(c.encode("ascii"))
        self.add_line(f">>> komut gönderildi: {c}", "info")

    def check_dump(self):
        """D'den sonra kayıtlar gelmediyse kullanıcıyı uyar, tekrar göndermeyi teklif et."""
        if self.phase != "dumping" or not self.ser:
            return
        if self.exp and self.exp.records:
            self.after(int(DUMP_TIMEOUT_S * 1000), self.check_dump)   # geliyor, bekle
            return
        if messagebox.askyesno("Kayıtlar gelmedi",
                               f"'D' komutundan sonra {DUMP_TIMEOUT_S:.0f} saniyedir kayıt gelmedi.\n"
                               "Kart meşgul olabilir ya da komut kaybolmuş olabilir. "
                               "'D' tekrar gönderilsin mi?"):
            self.send_cmd("D")

    # =======================================================================
    # Gelen mesajlar
    # =======================================================================
    def poll_queue(self):
        """50 ms'de bir kuyruğu boşaltır. Yeniden zamanlama 'finally' içinde: içeride
        beklenmeyen bir hata olsa bile döngü ASLA durmaz (eski sürümde durup arayüzü
        sağırlaştırıyordu)."""
        try:
            for _ in range(MAX_PER_POLL):
                kind, data = self.rx_q.get_nowait()
                if kind == "line":
                    self.handle_frame(data)
                elif kind == "error":
                    self.add_line(f"--- HATA: {data} ---", "bad")
                    self.disconnect()
        except queue.Empty:
            pass
        except Exception as e:                       # son savunma hattı
            self.add_line(f"ARAYÜZ HATASI (okuma döngüsü): {e}", "bad")
        finally:
            self.after(POLL_MS, self.poll_queue)

    def handle_frame(self, frame: bytes):
        ok_len, text = up.decode_frame(frame)
        if not ok_len:
            self.bad_frames += 1
            if self.exp:
                self.exp.bad_frames += 1
            self.add_line(f"[{len(frame)} B] {text}   ← 64 bayt değil, yok sayıldı", "bad")
            self.update_status()
            return

        typ, f = up.split_fields(text)
        try:
            self.dispatch(typ, f, text)
        except (ValueError, IndexError) as e:
            self.add_line(f"Ayrıştırılamadı: {text}  ({e})", "bad")
        except Exception as e:                       # beklenmeyen hata: sessizce yutma
            import traceback
            tb = traceback.format_exc()
            self.add_line(f"ARAYÜZ HATASI ({text}): {e}", "bad")
            for ln in tb.strip().splitlines()[-6:]:
                self.add_line("    " + ln, "bad")
            messagebox.showerror("Arayüz hatası",
                                 f"'{typ}' mesajı işlenirken hata oluştu:\n{e}\n\n"
                                 "Ayrıntı Mesajlar sekmesinde. Lütfen bu metni paylaşın.")
        self.update_status()

    def _ensure_exp(self, scenario: str):
        """INF görülmeden kayıt gelirse (ör. arayüz sonradan bağlandı) veriyi kaybetme."""
        if self.exp is None or self.exp.scenario != scenario:
            self.exp = up.Experiment(scenario)
            self.add_line(f"Uyarı: {scenario} için INF görülmedi; senaryo bilgileri eksik "
                          f"kalacak.", "bad")

    def dispatch(self, typ: str, f: list, text: str):
        if typ == "INF":
            if self.exp and self.phase == "running":
                self.add_line(f"Uyarı: {self.exp.scenario} bitirilmeden yeni senaryo başladı; "
                              f"önceki deney kaydedilmedi.", "bad")
            self.exp = up.Experiment.from_inf(f)
            self.exp.raw.append(text)
            if self.exp.baud != up.DEFAULT_BAUD:
                self.add_line(f"Not: {self.exp.baud} baud standart dışı; bu deney "
                              f"'{self.exp.label}' olarak ayrı kaydedilecek.", "bad")
            self.phase = "running"
            self.events_clear(self.exp)
            self.reset_btn_label()
            self.update_exp_labels()
            self.add_line(text, "info")
            return

        if typ == "TEL":
            if self.phase != "running" or self.exp is None:
                self.ignored_tel += 1          # deney dışı TEL (ör. END'den sonra gelen)
                return
            self.exp.raw.append(text)
            self.exp.add_tel(f)
            if self.show_tel.get():
                self.add_line(text, "dim")
            return

        if typ == "BTN":
            if self.exp:
                self.exp.raw.append(text)
                self.exp.btn_ids.append(int(f[0]))
                self.events_add_pending(int(f[0]))
            self.flash_btn(int(f[0]), f[1])
            self.add_line(f"Butona basıldı · Olay {f[0]}   ({text})", "btn")
            return

        if typ == "REC":
            self._ensure_exp(f[0])
            self.exp.raw.append(text)
            self.exp.records.append(up.Record.from_fields(f))
            self.add_line(text)
            return

        if typ == "WRK":
            self._ensure_exp(f[0])
            self.exp.raw.append(text)
            self.exp.set_wrk(f)
            self.add_line(text)
            return

        if typ == "SUM":
            self._ensure_exp(f[0])
            self.exp.raw.append(text)
            self.exp.set_sum(f)
            self.add_line(text)
            return

        if typ == "BAU":
            self.handle_bau(f)
            return

        if typ == "END":
            if self.exp is None:
                self.add_line("END geldi ama kayıt yok.", "bad")
                return
            self.exp.raw.append(text)
            self.exp.end_count = int(f[0])
            self.add_line(text)
            self.finalize_experiment()
            return

        self.add_line(f"Bilinmeyen mesaj: {text}", "bad")

    # =======================================================================
    # Deney sonu: doğrula, kaydet, göster
    # =======================================================================
    def finalize_experiment(self):
        exp = self.exp
        self.phase = "done"

        if exp.end_count != len(exp.records):
            self.add_line(f"Uyarı: END {exp.end_count} kayıt dedi, {len(exp.records)} REC alındı.",
                          "bad")
        if exp.sums is None:
            self.add_line("Uyarı: SUM satırı alınmadı.", "bad")

        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            (self.out_dir / "raw").mkdir(exist_ok=True)

            ev_path = self.out_dir / f"{exp.label}.csv"
            old = up.backup_if_exists(ev_path)
            up.write_events_csv(exp, ev_path)

            stamp = time.strftime("%Y%m%d_%H%M%S")
            raw_path = self.out_dir / "raw" / f"{exp.label}_{stamp}.log"
            up.write_raw_log(exp, raw_path)

            self.summaries[exp.label] = exp.summary_row()
            up.write_summary_csv(self.summaries, self.out_dir / "summary.csv")

            removed = up.prune_old_files(self.out_dir / "raw", exp.label, ".log", KEEP_LOGS)
            removed += up.prune_old_files(self.out_dir, exp.label, ".csv", KEEP_LOGS)
        except OSError as e:
            messagebox.showerror("Kaydedilemedi", str(e))
            return

        msg = f"Kaydedildi: {ev_path.name}, raw/{raw_path.name}, summary.csv"
        if old:
            msg += f"  (önceki dosya {old.name} olarak saklandı)"
        self.add_line(msg, "info")

        if removed:
            self.add_line(f"Eski kayıtlar silindi (her ölçüm için en yeni {KEEP_LOGS} tutulur): "
                          + ", ".join(p.name for p in removed), "dim")

        self.plot_exp = exp
        self.show_result(exp)
        self.events_fill(exp)
        self.refresh_summary_table()
        self.draw_plots()
        self.update_exp_labels()

    def show_result(self, exp):
        s = exp.summary_row()

        def val(x, fmt="{:.2f}"):
            return "—" if x == "" else fmt.format(x)

        lines = [
            f"{exp.label}   ok {s['ok']} / {s['recorded']} kayıt",
            f"R min / ort / max : {val(s['R_min_ms'])} / {val(s['R_avg_ms'])} / "
            f"{val(s['R_max_ms'])} ms",
            f"20 ms'yi aşan     : {s['over_deadline']}",
            "Ortalama aşamalar (ms):",
            f"  t₁−t₀ ISR→ButtonTask : {val(s['wait_ms'], '{:.3f}')}",
            f"  t₂−t₁ mesaj hazırl.  : {val(s['prep_ms'], '{:.3f}')}",
            f"  t₃−t₂ TX kuyruğu     : {val(s['txwait_ms'], '{:.3f}')}",
            f"  t₄−t₃ UART + TC      : {val(s['uart_ms'], '{:.3f}')}",
            f"        (teorik hat    : {s['line_ms_theory']:.3f})",
        ]
        if exp.sums:
            m = exp.sums
            lines += [
                f"Kayıplar: btn_drop {m['btn_drop']}, tx_drop {m['tx_drop']},",
                f"  tx_err {m['tx_err']}, timeout {m['timeout']}, taşma {m['rec_ovf']}",
                f"TEL: {m['tel_sent']} gönderildi, {m['tel_drop']} düştü, TXQ max {m['txq_max']}",
                f"Filtre: titreşim {m['bounce']}, kilit {m['lockout']}",
            ]
        if exp.wrk and exp.wrk.get("n"):
            w = exp.wrk
            lines.append(f"Ek iş: {w['min_us']} / {w['avg_us']} / {w['max_us']} µs (n={w['n']})")
        if s["measured_tel_hz"] != "":
            lines.append(f"Ölçülen telemetri: {s['measured_tel_hz']} Hz")
        self.result_var.set("\n".join(lines))

    # =======================================================================
    # Grafikler
    # =======================================================================
    def _style(self, ax):
        ax.set_facecolor(SURFACE)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)
        ax.tick_params(colors=INK2, labelsize=9)
        ax.yaxis.grid(True, color=GRID, linewidth=1)
        ax.set_axisbelow(True)

    # --- Olay kayıtları tablosu -----------------------------------------
    def events_clear(self, exp=None):
        self.ev_tree.delete(*self.ev_tree.get_children())
        if exp is not None:
            self.ev_info.set(f"{exp.label} · deney sürüyor. Zamanlar 'Deneyi bitir' ile gelir.")

    def events_add_pending(self, event_id: int):
        iid = f"ev{event_id}"
        if not self.ev_tree.exists(iid):
            self.ev_tree.insert("", "end", iid=iid, tags=("wait",),
                                values=(event_id, "", "", "", "", "", "",
                                        time.strftime("BTN alındı %H:%M:%S")))
            self.ev_tree.see(iid)

    def events_fill(self, exp):
        self.ev_tree.delete(*self.ev_tree.get_children())

        def ms(v):
            return "" if v is None else f"{v / 1000:.3f}"

        for r in exp.records:
            d = [r.d[0]] + [None if (r.d[k] is None or r.d[k - 1] is None)
                             else r.d[k] - r.d[k - 1] for k in (1, 2, 3)]
            tag = "bad" if not r.ok else ("late" if r.r_ms > up.DEADLINE_MS else "")
            self.ev_tree.insert("", "end", iid=f"ev{r.event_id}", tags=(tag,), values=(
                r.event_id, r.t0, *(ms(x) for x in d),
                "" if r.r_ms is None else f"{r.r_ms:.3f}", r.status))
        s = exp.summary_row()
        self.ev_info.set(f"{exp.label} · {s['recorded']} olay, ok {s['ok']}, "
                         f"20 ms'yi aşan {s['over_deadline']} · dosya: {exp.label}.csv")

    # --- Çizim (yalnız son tamamlanan deney) -----------------------------
    @staticmethod
    def _fmt_ms(v: float) -> str:
        return f"{v * 1000:.0f} µs" if v < 1 else f"{v:.2f} ms"

    def draw_plots(self):
        self.fig.clear()
        gs = self.fig.add_gridspec(2, 1, height_ratios=[1, 1], hspace=0.55)
        self.ax_r = self.fig.add_subplot(gs[0])
        ax_t = self.fig.add_subplot(gs[1])
        self._draw_r_plot(self.ax_r)

        exp = self.plot_exp
        row = exp.summary_row() if exp else None
        if row and row.get("wait_ms", "") != "":
            self._draw_timeline(ax_t, exp.label, row)
        else:
            ax_t.axis("off")
            ax_t.text(0.5, 0.5, "Ortalama olayın zaman çizelgesi deney bitince çizilir.",
                      ha="center", va="center", color=INK2, fontsize=10)
        self.fig.subplots_adjust(left=0.17, right=0.94, top=0.95, bottom=0.08)
        self.canvas.draw_idle()

    def _draw_r_plot(self, ax):
        self._style(ax)
        exp = self.plot_exp
        if exp and exp.records:
            ok = [r for r in exp.records if r.ok]
            ids = [r.event_id for r in ok]
            rs = [r.r_ms for r in ok]
            ax.plot(ids, rs, color=STAGE_COLORS[0], linewidth=2, marker="o", markersize=6,
                    markeredgecolor=SURFACE, markeredgewidth=1.5, label="R (ok)")
            over = [(i, r) for i, r in zip(ids, rs) if r > up.DEADLINE_MS]
            if over:
                ax.scatter(*zip(*over), color=CRITICAL, marker="^", s=70, zorder=3,
                           edgecolors=SURFACE, linewidths=1.5, label="20 ms aşan")
            bad = [r.event_id for r in exp.records if not r.ok]
            if bad:
                ax.scatter(bad, [0] * len(bad), color=CRITICAL, marker="x", s=60, zorder=3,
                           label="kayıp / hata")
            ax.axhline(up.DEADLINE_MS, color=CRITICAL, linestyle=(0, (6, 4)), linewidth=1.5)
            ax.text(1.0, up.DEADLINE_MS, " 20 ms", transform=ax.get_yaxis_transform(),
                    color=CRITICAL, fontsize=9, va="center", ha="left", clip_on=False)
            top = max(max(rs, default=0), up.DEADLINE_MS) * 1.15
            ax.set_ylim(bottom=-top * 0.04 if bad else 0, top=top)
            ax.set_xlabel("Olay no", color=INK2, fontsize=9)
            ax.legend(frameon=False, fontsize=8, loc="upper left")
            ax.set_title(f"Son deney {exp.label} · her basışın yanıt süresi R = t₄ − t₀   "
                         f"(ok {len(ok)} / {len(exp.records)})", color=INK, fontsize=10, loc="left")
        else:
            ax.set_title("Son deney · her basışın yanıt süresi (deney bitince çizilir)",
                         color=INK2, fontsize=10, loc="left")
        ax.set_ylabel("R (ms)", color=INK2, fontsize=9)

    def _draw_timeline(self, ax, label: str, s: dict):
        """Son deneyin ortalama olayı: t₀'dan t₄'e aşamalar kendi satırında, art arda."""
        vals = [float(s[k]) for k in up.STAGE_KEYS]
        total = sum(vals)
        xmax = max(total, up.DEADLINE_MS) if self.show_deadline.get() else total
        self._style(ax)
        ax.yaxis.grid(False)
        ax.xaxis.grid(True, color=GRID, linewidth=1)

        names = up.STAGE_LABELS
        start = 0.0
        right = xmax * 1.32                         # değer yazıları için sağda yer
        for i, v in enumerate(vals):
            ax.barh(i, v, left=start, height=0.62, color=STAGE_COLORS[i],
                    edgecolor=SURFACE, linewidth=1)
            # Çok kısa aşama da görünsün: başladığı yere ince işaret
            ax.plot([start, start], [i - 0.36, i + 0.36], color=STAGE_COLORS[i], linewidth=2)
            ax.text(min(start + v, xmax) + xmax * 0.02, i, self._fmt_ms(v),
                    va="center", ha="left", fontsize=8, color=INK)
            start += v
        # t₀ ve t₄ sınırları
        ax.axvline(0, color=INK2, linewidth=1)
        if total <= xmax:
            ax.axvline(total, color=INK2, linewidth=1, linestyle=(0, (2, 2)))
        if up.DEADLINE_MS <= xmax:
            ax.axvline(up.DEADLINE_MS, color=CRITICAL, linestyle=(0, (6, 4)), linewidth=1.5)
            ax.text(up.DEADLINE_MS, -0.75, "20 ms", ha="center", va="bottom", fontsize=7.5,
                    color=CRITICAL)
        elif total > up.DEADLINE_MS:
            pass                                    # ölçek dışında: başlıkta belirtiliyor
        ax.set_yticks(range(4))
        ax.set_yticklabels(names, fontsize=8)
        ax.set_ylim(3.6, -0.9)
        ax.set_xlim(0, right)
        ax.tick_params(axis="x", labelsize=8)
        ax.set_xlabel("ms  (t₀ = 0) · tüm 'ok' olayların ortalaması", color=INK2, fontsize=8)

        scn = label.split("_b")[0]
        baud = f" @{label.split('_b')[1]}" if "_b" in label else ""
        flag = "  ⚠ deadline aşıldı" if total > up.DEADLINE_MS else ""
        ax.set_title(f"Ortalama olayın zaman çizelgesi · {up.scenario_title(scn)}{baud} · "
                     f"R ort = {total:.2f} ms (n={s.get('ok', '?')}){flag}",
                     color=CRITICAL if flag else INK, fontsize=10, loc="left")

    def refresh_scenario_info(self):
        baud = self.cur_baud if self.ser else int(self.baud_var.get())
        self.scn_tree.delete(*self.scn_tree.get_children())
        for scn, info in up.SCENARIOS.items():
            p, w = info["period_ms"], info["work_us"]
            tel = "kapalı" if p == 0 else f"{1000 // p} Hz ({p} ms)"
            work = "yok" if w == 0 else f"~{w / 1000:g} ms / periyot"
            self.scn_tree.insert("", "end", values=(
                scn, tel, work, f"%{up.cpu_load_pct(w, p):.0f}",
                f"%{up.uart_load_pct(p, baud):.1f}", info["goal"]))
        lt = up.line_time_ms(baud)
        note = (
            f"Hesaplar şu anki hız için: {baud} baud  →  64 baytlık bir mesajın hat süresi "
            f"{lt:.3f} ms.\n\n"
            "• Telemetri sıklığı (S1→S3) UART hattını doldurur: BTN yanıtı, o an gönderilmekte olan "
            "TEL'in bitmesini bekler (t₃−t₂ büyür).\n"
            "• Ek CPU işi (S4, S5) en yüksek öncelikli TelemetryTask içinde yapılır: ButtonTask "
            "(t₁−t₀) ve en düşük öncelikli UartTxTask (t₃−t₂) CPU'yu bekler.\n"
            "• Ek CPU talebi ≈ iş süresi × frekans (100 Hz × 2 ms ≈ %20). UART hat kullanımı = "
            "64 × 10 bit × frekans / baud (yalnız telemetri; BTN mesajları ek yük getirir). "
            "İkisi farklı kaynaklardır, toplanmaz.\n"
            f"• Kararlılık için kaba koşul: periyot − iş süresi ≥ hat süresi. 100 Hz'de "
            f"10 ms − iş ≥ {lt:.2f} ms → iş ≤ {10 - lt:.2f} ms. S5 (5 ms) bu sınırı "
            f"{'aşar: kuyruk birikir' if 10 - lt < 5 else 'aşmaz'}.\n"
            "• Ödev ölçümleri 115200 baud ile yapılır; başka hızlar ayrı etiketle kaydedilir.")
        self.scn_note.configure(state="normal")
        self.scn_note.delete("1.0", "end")
        self.scn_note.insert("end", note)
        self.scn_note.configure(state="disabled")

    def refresh_summary_table(self):
        self.tree.delete(*self.tree.get_children())

        def f2(x):
            try:
                return f"{float(x):.2f}"
            except (TypeError, ValueError):
                return "—"

        for scn in sorted(self.summaries):
            s = self.summaries[scn]
            losses = sum(int(s.get(k) or 0) for k in ("btn_drop", "tx_drop", "tx_err", "timeout",
                                                      "rec_ovf"))
            stages = " / ".join(f2(s.get(k)) for k in up.STAGE_KEYS)
            work = f"{s.get('work_us', '')} µs ({s.get('wrk_avg_us') or '—'})" \
                if str(s.get("work_us", "0")) not in ("0", "") else "—"
            tel = f"{s.get('target_tel_hz', '')} / {s.get('measured_tel_hz') or '—'} Hz"
            self.tree.insert("", "end", values=(
                scn, tel, work, f"{s.get('ok', '')} / {s.get('recorded', '')}",
                f2(s.get("R_min_ms")), f2(s.get("R_avg_ms")), f2(s.get("R_max_ms")),
                s.get("over_deadline", ""), losses, s.get("txq_max", ""), stages))

    # =======================================================================
    # Canlı bilgiler
    # =======================================================================
    def flash_btn(self, event_id: int, scenario: str):
        self.btn_label.configure(text=f"Butona basıldı · Olay {event_id}", background=HIGHLIGHT)
        if self._flash_job:
            self.after_cancel(self._flash_job)
        self._flash_job = self.after(700, lambda: self.btn_label.configure(background=self._btn_bg))

    def reset_btn_label(self):
        self.btn_label.configure(text="Buton bekleniyor", background=self._btn_bg)

    def update_exp_labels(self):
        exp = self.exp
        if exp is None:
            return
        phase_txt = {"running": "çalışıyor", "dumping": "kayıtlar alınıyor",
                     "done": "bitti, kaydedildi", "idle": ""}[self.phase]
        self.scn_var.set(f"{up.scenario_title(exp.scenario)}  ·  {phase_txt}")
        info = up.SCENARIOS.get(exp.scenario)
        self.goal_var.set((info["goal"] if info else "") +
                          (f"   [{exp.baud} baud]" if exp.baud != up.DEFAULT_BAUD else ""))

        if exp.period_ms == 0:
            tel = "Telemetri: kapalı"
        elif exp.period_ms > 0:
            rate = exp.tel_rate_hz()
            rate_txt = f"{rate:.1f} Hz" if rate else "—"
            tel = (f"Telemetri: hedef {1000 / exp.period_ms:.0f} Hz · ölçülen {rate_txt} · "
                   f"{exp.tel_count} TEL")
        else:
            tel = "Telemetri: (INF yok)"
        if exp.work_us:
            tel += f"\nEk CPU işi: {exp.work_us} µs ({exp.work_iters} iterasyon)"
        self.tel_var.set(tel)

        if self.phase == "running":
            el = time.time() - exp.started
            self.warm_var.set(f"Isınma: {el:.1f} / {WARMUP_S:.0f} s" if el < WARMUP_S
                              else "Isınma tamam ✓  basışlara başlayabilirsin")
            n = len(exp.btn_ids)
            mark = "  ✓" if n >= TARGET_PRESSES else ""
            self.press_var.set(f"Basış: {n} / {TARGET_PRESSES}{mark}   (aralarında ≥ 0,5 s)")
        else:
            self.warm_var.set("")
            self.press_var.set(f"Basış: {len(exp.btn_ids)}")

    def tick(self):
        if self.exp is not None and self.phase == "running":
            self.update_exp_labels()
        self.after(TICK_MS, self.tick)

    # =======================================================================
    # Yardımcılar
    # =======================================================================
    def add_line(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        n = int(self.log.index("end-1c").split(".")[0])
        if n > MAX_LOG_LINES:
            self.log.delete("1.0", f"{n - MAX_LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def update_status(self):
        conn = f"{self.ser.port} bağlı · {self.cur_baud} baud" if self.ser else "Bağlı değil"
        self.status_var.set(f"{conn}  ·  64 bayt olmayan çerçeve: {self.bad_frames}  ·  "
                            f"deney dışı TEL: {self.ignored_tel}")

    def on_close(self):
        if self.ser:
            self.disconnect()
        self.destroy()


def check_versions():
    """uart_monitor.py ile uart_protocol.py farklı sürümlerdeyse açılışta söyle."""
    needed = ["prune_old_files", "STAGE_HELP", "SCENARIOS", "BAUD_CMD", "line_time_ms"]
    missing = [n for n in needed if not hasattr(up, n)]
    if "rx_rearm" not in getattr(up, "SUM_KEYS", []):
        missing.append("SUM_KEYS.rx_rearm")
    return missing


if __name__ == "__main__":
    miss = check_versions()
    if miss:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Dosya sürümleri uyuşmuyor",
                             "uart_protocol.py eski bir sürüm. uart_monitor.py ile birlikte "
                             "gönderilen uart_protocol.py dosyasını aynı klasöre koyun.\n\n"
                             f"Eksik: {', '.join(miss)}")
        root.destroy()
    else:
        App().mainloop()
