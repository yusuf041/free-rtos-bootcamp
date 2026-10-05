# list_ports.py
# Bilgisayardaki seri portları (COM portları) listeler.
# Çalıştırma: python list_ports.py

# pyserial kütüphanesinin port listeleme aracını içeri alıyoruz
from serial.tools import list_ports

# comports() bilgisayardaki tüm seri portları bir liste olarak döndürür
ports = list_ports.comports()

if not ports:
    print("Hiç seri port bulunamadı.")
    print("USB-TTL dönüştürücü takılı mı? Sürücüsü (CH340 / CP2102 / FTDI) kurulu mu?")
else:
    print(f"{len(ports)} port bulundu:\n")
    for p in ports:
        # p.device      -> port adı, ör. COM5
        # p.description -> Windows'un gösterdiği açıklama, ör. "USB-SERIAL CH340"
        # p.hwid        -> donanım kimliği (VID:PID), hangi çip olduğunu anlamaya yarar
        print(f"  {p.device:<8} {p.description}")
        print(f"           {p.hwid}\n")
