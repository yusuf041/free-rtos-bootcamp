# UART Mesaj Sözleşmesi (Ödev 01)

Kart (STM32F407 Discovery) ile PC arayüzü arasındaki ortak dil.

## Hat ayarları

- USART2, PA2 = TX, PA3 = RX, GND ortak (USB-TTL dönüştürücü)
- 115200 baud, 8N1

## Genel kurallar (Kart → PC)

- Her mesaj **tam 64 bayt**: 63 bayt ASCII metin + 1 bayt `\n` (LF).
- Metin 63 bayttan kısaysa sonu **boşlukla** doldurulur.
- Metin 63 bayttan uzunsa **gönderilmez**, hata sayacı artırılır (sessizce kesilmez).
- Alanlar virgülle ayrılır. İlk alan mesaj tipidir.
- PC tarafı `\n` gelene kadar biriktirir, satırın 64 bayt olduğunu doğrular, sondaki boşlukları atar.
- Tüm zamanlar kartın **TIM2** sayacından gelir: 32 bit, 1 MHz (1 tick = 1 µs).

## Mesaj tipleri (Kart → PC)

### `INF` – Senaryo bilgisi
Senaryo seçildiğinde / sıfırlandığında bir kez gönderilir.

```
INF,<senaryo>,<telemetri_periyot_ms>,<ek_is_us>,<iterasyon>,<kalibrasyon_us>,<baud>
INF,S4,10,2000,9575,10442,115200
INF,S0,0,0,0,10442,115200          (0 = telemetri kapalı)
```
- `baud`: kartın o anki UART hızı. Ödev ölçümleri 115200 ile yapılır.
- `iterasyon`: her periyotta yapılan xorshift tekrar sayısı (deney boyunca sabit)
- `kalibrasyon_us`: açılışta 100000 iterasyonun en kısa süresi (5 ölçümün minimumu)

### `TEL` – Telemetri (ölçüm sırasında periyodik)
```
TEL,<sira_no>,<senaryo>,<zaman_ms>
TEL,1042,S3,726
```
`zaman_ms`: kart açıldığından beri geçen süre (FreeRTOS tick, ms).

### `BTN` – Buton yanıtı (ölçülen mesaj budur)
```
BTN,<olay_id>,<senaryo>,PRESSED
BTN,17,S3,PRESSED
```
Arayüz bunu görünce "Butona basıldı · Olay 17" gösterir.

### `REC` – Ölçüm kaydı (deney bittikten sonra, olay başına bir satır)
```
REC,<senaryo>,<olay_id>,<t0_us>,<d1>,<d2>,<d3>,<d4>,<durum>
REC,S3,17,1000000,2000,2300,4000,9606,ok
REC,S3,18,1500000,2100,2400,,,tx_drop
```
- `t0_us`: ISR girişindeki mutlak zaman (µs, 32 bit)
- `d1..d4`: `t1-t0`, `t2-t0`, `t3-t0`, `t4-t0` (µs, uint32 mod 2^32 çıkarma)
- Ölçülemeyen zaman **boş** bırakılır, asla 0 yazılmaz.
- `durum`: `ok` | `btn_drop` (buton kuyruğu dolu) | `tx_drop` (TX kuyruğu dolu) | `tx_err` (UART başlatılamadı) | `timeout` (1 s içinde tamamlanmadı)

PC tarafı mutlak zamanları geri hesaplar: `t1 = t0 + d1` vb.

### `WRK` – Ek CPU işinin gerçek süresi (kayıtlardan sonra, SUM'dan önce)
```
WRK,<senaryo>,<iterasyon>,<olcum_sayisi>,<min_us>,<ort_us>,<max_us>
WRK,S5,142857,1520,4998,5004,5130
```
Duvar saati ölçümüdür: araya giren kesmeler dahildir. S0–S3'te iş yoktur, değerler 0 gelir.

### `SUM` – Sayaç özeti (kayıtlardan sonra bir kez)
```
SUM,<senaryo>,<kabul>,<titresim>,<btn_drop>,<tx_drop>,<tx_err>,<timeout>,<kayit_tasma>,<tel_gonderilen>,<tel_drop>,<txq_max>,<fmt_err>,<kilit>,<rx_yeniden>
SUM,S3,32,1250,0,0,0,0,0,2871,0,3,0,1,0
```
- `kilit`: önceki kabul edilen basıştan 250 ms içinde gelip olay üretmeyen basış sayısı
- `rx_yeniden`: kapanmış UART alımının UartTxTask tarafından yeniden kurulma sayısı (HAL kilit yarışı)
- `titresim`: filtrenin attığı kenar sayısı
- `tx_err`, `timeout`: tüm mesajlar (TEL + BTN) için
- `txq_max`: TX kuyruğunun deney boyunca gördüğü en yüksek doluluk (16 üzerinden)

### `BAU` – Baud değişimi
```
BAU,<yeni_baud>,SWITCH     (ESKİ hızla gönderilir: "şimdi değiştiriyorum")
BAU,<yeni_baud>,OK         (~300 ms sonra YENİ hızla gönderilir: "değiştirdim")
```
PC, `SWITCH` satırını alınca kendi portunu yeni hıza alır; `OK` gelirse değişim doğrulanmıştır.
Kart her açılışta 115200 ile başlar.

### `END` – Aktarım bitti
```
END,<gonderilen_kayit_sayisi>
END,32
```

## Komutlar (PC → Kart)

Tek karakter, sonunda satır sonu gerekmez. Kart bunları UART RX kesmesiyle alır; ayrı bir görev yoktur.
Komut gelince telemetri ve basış kaydı hemen durur; komut TX kuyruğuna KONMAZ (dolu kuyrukta kaybolabilirdi),
UartTxTask kuyruktaki mesajlar bitince komutu çalıştırır.

| Komut | Anlamı |
|---|---|
| `0` … `5` | Senaryo S0…S5 seç; kayıtları ve sayaçları sıfırla; `INF` gönder; telemetriyi başlat |
| `D` | Deneyi bitir: telemetriyi durdur, TX kuyruğunun boşalmasını bekle, `REC`… `WRK` `SUM` `END` gönder |
| `a` … `f` | Baud değiştir: a=9600, b=57600, c=115200, d=230400, e=460800, f=921600. Ardından aynı senaryo yeni hızla baştan başlar (`INF` gelir) |

## Bir deneyin akışı

```
PC  → '3'                    (S3 seç, sıfırla)
Kart→ INF,S3,10,0
Kart→ TEL,... TEL,... (5 s ısınma)
      ... en az 30 basış, her birinde BTN,<id>,S3,PRESSED ...
PC  → 'D'
Kart→ REC,... (her olay için)
Kart→ WRK,...
Kart→ SUM,...
Kart→ END,32
PC  : measurements/S3.csv dosyasına yazar
```

## Senaryolar

| ID | Telemetri | Ek CPU işi | Ek CPU talebi | UART hat kullanımı (115200) | Amaç |
|---|---|---|---|---|---|
| S0 | Kapalı | Yok | %0 | %0 | Referans yanıt süresi |
| S1 | 10 Hz (100 ms) | Yok | %0 | %5,6 | Düşük telemetri sıklığı |
| S2 | 50 Hz (20 ms) | Yok | %0 | %27,8 | Orta telemetri sıklığı |
| S3 | 100 Hz (10 ms) | Yok | %0 | %55,6 | Yüksek telemetri sıklığı |
| S4 | 100 Hz (10 ms) | ~2 ms | ≈%20 | %55,6 | Ek CPU yükü |
| S5 | 100 Hz (10 ms) | ~5 ms | ≈%50 | %55,6 | Daha yüksek CPU yükü |

- Ek CPU talebi ≈ iş süresi × frekans. Hat kullanımı = 64 bayt × 10 bit × frekans / baud (yalnız telemetri).
