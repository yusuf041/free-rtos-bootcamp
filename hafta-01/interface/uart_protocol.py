"""
uart_protocol.py — protocol.md'deki mesajları ayrıştırır, deney verisini tutar,
CSV dosyalarını yazar ve özet istatistikleri hesaplar.

Pencere (Tkinter) kodundan BAĞIMSIZDIR:
  - uart_monitor.py canlı deneyde kullanır,
  - analiz betiği aynı fonksiyonlarla CSV'leri okur,
  - pencere açmadan test edilebilir.
"""
from __future__ import annotations

import csv
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

MSG_LEN = 64          # her mesaj tam 64 bayt (63 karakter + '\n')
U32 = 1 << 32         # TIM2 32 bit: farklar mod 2^32
DEADLINE_MS = 20.0    # ödevin deney deadline'ı

DEFAULT_BAUD = 115200 # ödev standardı
BAUDS = [9600, 57600, 115200, 230400, 460800, 921600]
BAUD_CMD = dict(zip(BAUDS, "abcdef"))   # PC -> kart baud komutu

# ---------------------------------------------------------------------------
# Zaman aralıklarının anlamı (arayüzde ve raporda aynı metin kullanılır)
# ---------------------------------------------------------------------------
STAGE_KEYS = ["wait_ms", "prep_ms", "txwait_ms", "uart_ms"]
STAGE_SHORT = ["ISR → ButtonTask", "Mesaj hazırlama", "TX kuyruğu bekleme", "UART iletimi + TC"]
STAGE_LABELS = [f"{t} {n}" for t, n in zip(["t₁−t₀", "t₂−t₁", "t₃−t₂", "t₄−t₃"], STAGE_SHORT)]

STAGE_HELP = [
    ("t₀", "Buton ISR'ına giriş",
     "EXTI0 kesmesine girilir girilmez TIM2 okunur. Filtre bu kenarı kabul ederse t₀ budur. "
     "Fiziksel basma anı değildir: kenar algılama + kesme giriş gecikmesi (~birkaç yüz ns) önceden olmuştur."),
    ("t₁−t₀", "ISR → ButtonTask (olay aktarımı + CPU bekleme)",
     "ISR'ın geri kalanı (filtre, kayıt kartı, xQueueSendFromISR ile olayı buton kuyruğuna kopyalama), "
     "portYIELD_FROM_ISR → PendSV ile bağlam değişimi ve ButtonTask'ın xQueueReceive'den dönmesi. "
     "CPU o an daha yüksek öncelikli TelemetryTask'taysa (S4/S5'teki hesaplama) onun bitmesi beklenir. "
     "Araya giren başka kesmeler (UART TXE/TC, SysTick) de buraya eklenir."),
    ("t₂−t₁", "BTN mesajını hazırlama",
     "ButtonTask'ın kayıt kartına t₁ yazması ve vsnprintf + memset/memcpy ile 64 baytlık "
     "\"BTN,<id>,<senaryo>,PRESSED\" metnini oluşturması. Saf CPU işi; duvar saati olduğu için "
     "araya kesme girerse o da dahildir."),
    ("t₃−t₂", "TX kuyruğunda bekleme (UART sırası)",
     "xQueueSend ile 64 baytın TX kuyruğuna kopyalanması, sonra sıra beklenmesi: UART o an bir TEL "
     "gönderiyorsa onun bitmesi (en fazla bir mesajın hat süresi) ve kuyrukta öndeki mesajlar. "
     "UartTxTask en düşük öncelikte olduğu için TC'den sonra CPU'yu alana kadar da bekler (S5'te baskın). "
     "Sonunda xQueueReceive ile mesaj tx_cur tamponuna kopyalanır."),
    ("t₄−t₃", "UART iletimi: DR → shift register → hat + TC",
     "HAL_UART_Transmit_IT kurulumu, sonra her bayt için TXE kesmesinde CPU baytı DR (data register)'a yazar; "
     "donanım baytı DR'den shift register'a alır ve bitleri TX hattına kaydırır (1 start + 8 veri + 1 stop = 10 bit). "
     "TXE = 'DR boş, sıradakini ver'; TC = 'shift register da boşaldı, son stop biti çıktı'. "
     "Hat süresi 64 × 10 / baud (115200'de ≈ 5,556 ms). t₄, TC kesmesinde HAL callback'ine girildiği an."),
    ("R = t₄−t₀", "Toplam yanıt süresi",
     "Kart saatinde, kabul edilen buton kenarından BTN yanıtının son bitinin hattan çıkışına kadar geçen süre. "
     "Deadline 20 ms."),
]


# ---------------------------------------------------------------------------
# Senaryolar
# ---------------------------------------------------------------------------
SCENARIOS = {
    "S0": dict(period_ms=0,   work_us=0,    goal="Referans: telemetri kapalı, UART ve CPU boş"),
    "S1": dict(period_ms=100, work_us=0,    goal="Düşük telemetri sıklığı"),
    "S2": dict(period_ms=20,  work_us=0,    goal="Orta telemetri sıklığı"),
    "S3": dict(period_ms=10,  work_us=0,    goal="Yüksek telemetri sıklığı (UART paylaşımı)"),
    "S4": dict(period_ms=10,  work_us=2000, goal="Ek CPU yükü (TelemetryTask içinde ~2 ms hesap)"),
    "S5": dict(period_ms=10,  work_us=5000, goal="Daha yüksek CPU yükü (~5 ms hesap)"),
}


def line_time_ms(baud: int) -> float:
    """64 bayt × 10 bit (8N1) hat süresi."""
    return 64 * 10 / baud * 1000


def uart_load_pct(period_ms: int, baud: int) -> float:
    """Yalnız telemetrinin UART hat kullanımı (%)."""
    return 0.0 if period_ms <= 0 else line_time_ms(baud) / period_ms * 100


def cpu_load_pct(work_us: int, period_ms: int) -> float:
    """Ek hesaplamanın CPU talebi ≈ C × f (%)."""
    return 0.0 if period_ms <= 0 else work_us / (period_ms * 1000) * 100


def scenario_title(scn: str) -> str:
    """Kısa açıklama: 'S4 · 100 Hz telemetri + 2 ms iş'"""
    info = SCENARIOS.get(scn)
    if not info:
        return scn
    if info["period_ms"] == 0:
        txt = "telemetri kapalı"
    else:
        txt = f"{1000 // info['period_ms']} Hz telemetri"
    if info["work_us"]:
        txt += f" + {info['work_us'] / 1000:g} ms CPU işi"
    return f"{scn} · {txt}"


# ---------------------------------------------------------------------------
# Çerçeve ve alan ayrıştırma
# ---------------------------------------------------------------------------
def decode_frame(frame: bytes) -> tuple[bool, str]:
    """(64 bayt ve '\\n' ile bitiyor mu?, sondaki boşlukları atılmış metin)"""
    ok = len(frame) == MSG_LEN and frame.endswith(b"\n")
    return ok, frame.decode("ascii", errors="replace").rstrip()


def split_fields(text: str) -> tuple[str, list[str]]:
    parts = text.split(",")
    return parts[0], parts[1:]


def _int_or_none(s: str) -> Optional[int]:
    s = s.strip()
    return int(s) if s else None          # boş alan = ölçülemedi (asla 0 değil)


# ---------------------------------------------------------------------------
# Bir buton olayının kaydı (REC satırı)
# ---------------------------------------------------------------------------
@dataclass
class Record:
    scenario: str
    event_id: int
    t0: int
    d: list            # [d1, d2, d3, d4]  (tk - t0, µs) veya None
    status: str

    @classmethod
    def from_fields(cls, f: list[str]) -> "Record":
        # REC,<senaryo>,<id>,<t0>,<d1>,<d2>,<d3>,<d4>,<durum>
        if len(f) != 8:
            raise ValueError(f"REC 8 alan bekleniyordu, {len(f)} geldi")
        return cls(f[0], int(f[1]), int(f[2]),
                   [_int_or_none(x) for x in f[3:7]], f[7].strip())

    def t_abs(self, k: int) -> Optional[int]:
        """tk'nın mutlak değeri (µs, 32 bit sayaç)."""
        if k == 0:
            return self.t0
        dk = self.d[k - 1]
        return None if dk is None else (self.t0 + dk) % U32

    @property
    def ok(self) -> bool:
        return self.status == "ok" and all(x is not None for x in self.d)

    @property
    def r_ms(self) -> Optional[float]:
        return None if self.d[3] is None else self.d[3] / 1000.0

    def stages_ms(self) -> Optional[list[float]]:
        """[t1-t0, t2-t1, t3-t2, t4-t3] (ms). Eksik zaman varsa None."""
        if not self.ok:
            return None
        d1, d2, d3, d4 = self.d
        return [d1 / 1000, (d2 - d1) / 1000, (d3 - d2) / 1000, (d4 - d3) / 1000]


# ---------------------------------------------------------------------------
# Bir senaryonun deneyi (INF ... END arası)
# ---------------------------------------------------------------------------
SUM_KEYS = ["accepted", "bounce", "btn_drop", "tx_drop", "tx_err", "timeout",
            "rec_ovf", "tel_sent", "tel_drop", "txq_max", "fmt_err", "lockout", "rx_rearm"]
WRK_KEYS = ["iters", "n", "min_us", "avg_us", "max_us"]


@dataclass
class Experiment:
    scenario: str
    period_ms: int = -1          # -1: INF görülmedi
    work_us: int = 0
    work_iters: int = 0
    cal_us: int = 0
    baud: int = DEFAULT_BAUD
    started: float = field(default_factory=time.time)
    tel_count: int = 0
    tel_first_ms: Optional[int] = None
    tel_last_ms: Optional[int] = None
    btn_ids: list = field(default_factory=list)
    records: list = field(default_factory=list)
    wrk: Optional[dict] = None
    sums: Optional[dict] = None
    end_count: Optional[int] = None
    bad_frames: int = 0
    raw: list = field(default_factory=list)

    @classmethod
    def from_inf(cls, f: list[str]) -> "Experiment":
        # INF,<senaryo>,<periyot_ms>,<ek_is_us>,<iterasyon>,<kalibrasyon_us>,<baud>
        vals = [int(x) for x in f[1:]] + [0, 0, 0, 0, 0]
        return cls(f[0], vals[0], vals[1], vals[2], vals[3], vals[4] or DEFAULT_BAUD)

    @property
    def label(self) -> str:
        """Dosya/özet etiketi. Standart dışı baud ayrı kaydedilir: S3_b230400"""
        return self.scenario if self.baud == DEFAULT_BAUD else f"{self.scenario}_b{self.baud}"

    def add_tel(self, f: list[str]) -> None:
        # TEL,<sira>,<senaryo>,<zaman_ms>
        ms = int(f[2])
        self.tel_count += 1
        if self.tel_first_ms is None:
            self.tel_first_ms = ms
        self.tel_last_ms = ms

    def tel_rate_hz(self) -> Optional[float]:
        """Kartın kendi zaman damgalarından ölçülen gerçek telemetri hızı."""
        if self.tel_count < 2 or self.tel_last_ms == self.tel_first_ms:
            return None
        return (self.tel_count - 1) * 1000.0 / (self.tel_last_ms - self.tel_first_ms)

    def set_wrk(self, f: list[str]) -> None:
        self.wrk = dict(zip(WRK_KEYS, (int(x) for x in f[1:])))

    def set_sum(self, f: list[str]) -> None:
        self.sums = dict(zip(SUM_KEYS, (int(x) for x in f[1:])))

    # --- Özet -------------------------------------------------------------
    def summary_row(self) -> dict:
        ok = [r for r in self.records if r.ok]
        rs = [r.r_ms for r in ok]
        status_counts = {}
        for r in self.records:
            status_counts[r.status] = status_counts.get(r.status, 0) + 1

        row = {
            "label": self.label,
            "scenario": self.scenario,
            "baud": self.baud,
            "line_ms_theory": round(line_time_ms(self.baud), 3),
            "period_ms": self.period_ms,
            "target_tel_hz": round(1000 / self.period_ms, 1) if self.period_ms > 0 else 0,
            "measured_tel_hz": round(self.tel_rate_hz(), 2) if self.tel_rate_hz() else "",
            "work_us": self.work_us,
            "work_iters": self.work_iters,
            "cal_us": self.cal_us,
            "recorded": len(self.records),
            "ok": len(ok),
            "not_ok": len(self.records) - len(ok),
            "status_counts": ";".join(f"{k}={v}" for k, v in sorted(status_counts.items())),
            "R_min_ms": round(min(rs), 3) if rs else "",
            "R_avg_ms": round(statistics.fmean(rs), 3) if rs else "",
            "R_max_ms": round(max(rs), 3) if rs else "",
            "over_deadline": sum(1 for x in rs if x > DEADLINE_MS),
        }
        for i, key in enumerate(STAGE_KEYS):
            vals = [r.stages_ms()[i] for r in ok]
            row[key] = round(statistics.fmean(vals), 3) if vals else ""
            row[key.replace("_ms", "_max_ms")] = round(max(vals), 3) if vals else ""
        for k in SUM_KEYS:
            row[k] = self.sums.get(k, "") if self.sums else ""
        for k in WRK_KEYS:
            row["wrk_" + k] = self.wrk.get(k, "") if self.wrk else ""
        row["bad_frames"] = self.bad_frames
        row["end_count"] = self.end_count if self.end_count is not None else ""
        row["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return row


SUMMARY_COLS = [
    "label", "scenario", "baud", "line_ms_theory", "period_ms", "target_tel_hz", "measured_tel_hz", "work_us", "work_iters", "cal_us",
    "recorded", "ok", "not_ok", "status_counts",
    "R_min_ms", "R_avg_ms", "R_max_ms", "over_deadline",
    "wait_ms", "prep_ms", "txwait_ms", "uart_ms",
    "wait_max_ms", "prep_max_ms", "txwait_max_ms", "uart_max_ms",
    *SUM_KEYS, *("wrk_" + k for k in WRK_KEYS), "bad_frames", "end_count", "saved_at",
]
EVENT_COLS = ["scenario", "event_id", "t0_us", "t1_us", "t2_us", "t3_us", "t4_us", "status"]


# ---------------------------------------------------------------------------
# Dosyalar
# ---------------------------------------------------------------------------
def backup_if_exists(path: Path) -> Optional[Path]:
    """Var olan dosyanın üzerine yazmadan önce zaman damgalı bir kopyaya taşı."""
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(path.stat().st_mtime))
    target = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.stem}_{stamp}_{n}{path.suffix}")
        n += 1
    path.rename(target)
    return target


def write_events_csv(exp: Experiment, path: Path) -> None:
    """Şartnamedeki biçim: her satır bir buton olayı, eksik zaman BOŞ."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(EVENT_COLS)
        for r in exp.records:
            ts = [r.t_abs(k) for k in range(5)]
            w.writerow([r.scenario, r.event_id, *("" if t is None else t for t in ts), r.status])


def read_events_csv(path: Path) -> list[Record]:
    """write_events_csv'nin tersi (analiz betiği için)."""
    out = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            t0 = int(row["t0_us"])
            d = []
            for k in range(1, 5):
                v = row[f"t{k}_us"].strip()
                d.append(None if not v else (int(v) - t0) % U32)
            out.append(Record(row["scenario"], int(row["event_id"]), t0, d, row["status"]))
    return out


def write_summary_csv(rows: dict, path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_COLS, extrasaction="ignore")
        w.writeheader()
        for scn in sorted(rows):
            w.writerow(rows[scn])


def read_summary_csv(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {(row.get("label") or row["scenario"]): row for row in csv.DictReader(fh)}


def write_raw_log(exp: Experiment, path: Path) -> None:
    path.write_text("\n".join(exp.raw) + "\n", encoding="utf-8")


def prune_old_files(folder: Path, label: str, suffix: str, keep: int) -> list:
    """<label>_YYYYmmdd_HHMMSS*.<suffix> biçimindeki ESKİ dosyalardan en yeni `keep` tanesini
    bırakır, gerisini siler. Güncel dosya (ör. S3.csv) bu kalıba uymadığı için asla silinmez.
    'S3' ile 'S3_b230400' karışmasın diye ad tam kalıpla eşleştirilir."""
    import re
    pat = re.compile(rf"^{re.escape(label)}_\d{{8}}_\d{{6}}(_\d+)?{re.escape(suffix)}$")
    if not folder.exists():
        return []
    files = sorted((p for p in folder.iterdir() if pat.match(p.name)),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for p in files[keep:]:
        try:
            p.unlink()
            removed.append(p)
        except OSError:
            pass
    return removed
