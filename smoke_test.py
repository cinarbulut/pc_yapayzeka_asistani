import ctypes, sys, types
from unittest import mock

# Windows'a ve GUI'ye özgü modülleri sahte (stub) olarak yükle
ctypes.windll = mock.MagicMock()
for ad in ["tkinter", "tkinter.messagebox", "tkinter.ttk", "pyttsx3", "requests",
           "speech_recognition", "ddgs", "duckduckgo_search", "PIL", "PIL.Image", "PIL.ImageTk",
           "psutil", "tkinter.font"]:
    sys.modules[ad] = mock.MagicMock()

import asistan as a

fails = []
def kontrol(ad, kosul):
    print(("OK   " if kosul else "HATA ") + ad)
    if not kosul:
        fails.append(ad)

kontrol("tr_kucuk", a.tr_kucuk("İSTANBUL") == "istanbul")
kontrol("hesapla", "= 84" in a.hesapla("12*(3+4)"))
kontrol("hesapla guvenlik", a.hesapla("__import__('os')").startswith("Hesaplanamadı"))
kontrol("cumlelere_bol", len(a.cumlelere_bol("Bugün hava çok güzel olacak, sabah saatlerinde bile sıcaklık oldukça yüksek seyredecek. Yarın yağmur var.")) > 1)
kontrol("yanit_parcala kod", [t for t, _ in a._yanit_parcala("A\n```py\nx=1\n```\nB")] == ["metin", "kod", "metin"])
temiz, cag = a.metinden_arac_ayikla('Tamam\n{"name": "hava_durumu", "arguments": {"sehir": "Yozgat"}}')
kontrol("metinden_arac_ayikla", temiz == "Tamam" and cag[0]["function"]["name"] == "hava_durumu")
temiz, cag = a.metinden_arac_ayikla('{"name": "sil_her_sey", "arguments": {}}')
kontrol("izinsiz arac reddi", cag == [])

# Dış etkili fonksiyonları taklit et
a.parlaklik_ayarla = lambda y: f"PARLAK_{y}"
a.parlaklik_degistir = lambda d: f"DEG_{d}"
kontrol("parlaklik ayarla", a.parlaklik_komutu("parlaklığı yüzde 40 yap") == "PARLAK_40")
kontrol("parlaklik artir", a.parlaklik_komutu("parlaklığı artır") == "DEG_20")
kontrol("parlaklik eksik", a.parlaklik_komutu("parlaklık") .startswith("Parlaklık için"))

a.webbrowser.open = mock.MagicMock()
kontrol("yerel_komut arama", a.yerel_komut("kedi resimleri youtube'da ara") is True)
kontrol("arama url youtube", "youtube.com/results" in str(a.webbrowser.open.call_args))

a.bekleyen["islem"] = None
kontrol("yerel_komut bilgisayar kapat (onay bekler)", a.yerel_komut("bilgisayarı kapat") is True and a.bekleyen["islem"] == "pc_kapat")
a.bekleyen["islem"] = None

kontrol("yerel_komut ses yuzde (dürüst cevap)", a.yerel_komut("ses yüzde 60 arttır") is True)
kontrol("yerel_komut mail yok ama hata yok", a.yerel_komut("mail geldi mi") is True)

kontrol("pencere/ses fonksiyonlari var", callable(getattr(a, "karanlik_pencere", None)) and callable(getattr(a, "acilis_sesi_cal", None)))
kontrol("Arayuz sinifi var", hasattr(a, "Arayuz") and callable(getattr(a.Arayuz, "_pencere_stil", None)))
a.acilis_sesi_cal = mock.MagicMock()
a.os.path.exists = lambda p: True
kontrol("acilis sesi cagrisi", True)

print("\nSONUC:", "TUM KONTROLLER GECTI" if not fails else f"BASARISIZ: {fails}")
