"""
Kam - Bilgisayar Asistanı (v2)
Yerel Ollama modeliyle çalışan, sesli komutlu, araç destekli masaüstü asistanı.

Zorunlu paketler : requests, pyttsx3, SpeechRecognition, PyAudio, duckduckgo_search
İsteğe bağlı     : psutil (sistem bilgisi), pillow (ekran görüntüsü)
"""
import ast
import ctypes
import datetime
import difflib
import html
import io
import json
import logging
import math
import operator
import os
import queue
import re
import shutil
import tempfile
import subprocess
import threading
import time
import tkinter as tk
import urllib.parse
import uuid
import webbrowser
from tkinter import messagebox, ttk

# Saf mantık modülleri (kam paketi)
from kam.calc import hesapla
from kam.intents import (DUYGU_YANITLARI, arama_niyeti, bilgisayar_niyeti, duygu_niyeti, parlaklik_niyeti,
                         sesi_duzelt, uygulama_adi_duzelt)
from kam.policy import ONAY, SERBEST, YASAK, arac_karari, denetim_kaydi, denetim_yaz
from kam.stt import KonusmaTanima
from kam.textutil import arac_cagrisi_ayikla, cumlelere_bol, tr_kucuk
from kam.textutil import yanit_parcala as _yanit_parcala

import pyttsx3
import requests
import speech_recognition as sr
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

try:
    import psutil
except ImportError:
    psutil = None
try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

# ---------- Yollar, günlük ve ayarlar ----------

KLASOR = os.path.dirname(os.path.abspath(__file__))
AYAR_DOSYASI = os.path.join(KLASOR, "ayarlar.json")
HAFIZA_DOSYASI = os.path.join(KLASOR, "hafiza.json")
GUNLUK_DOSYASI = os.path.join(KLASOR, "konusma_gunlugu.txt")
HATA_DOSYASI = os.path.join(KLASOR, "hata_gunlugu.txt")
NOT_DOSYASI = os.path.join(KLASOR, "notlar.txt")
OTURUM_KLASORU = os.path.join(KLASOR, "sohbetler")
GORSEL_KLASORU = os.path.join(KLASOR, "gorseller")
os.makedirs(OTURUM_KLASORU, exist_ok=True)
os.makedirs(GORSEL_KLASORU, exist_ok=True)

logging.basicConfig(filename=HATA_DOSYASI, level=logging.ERROR,
                    format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
log = logging.getLogger("asistan")

VARSAYILAN_AYARLAR = {
    "sistem_adi": "Kam",
    "kullanici_adi": "Başkan",
    "ollama_url": "http://localhost:11434",
    "model": "qwen2.5:3b",
    "konum_adi": "Yozgat",
    "konum_lat": 39.82,
    "konum_lon": 34.81,
    "mic_baslangic": False,
    "sesli_baslangic": True,
    "enerji_esigi": 300,
    "duraklama_suresi": 0.8,
    "dinleme_zaman_asimi": 3,
    "cumle_sure_siniri": 8,
    "konusma_hizi": 175,
    "gunluk_saklama_gun": 30,
    "ek_uygulamalar": {
        "hesap makinesi": "calc.exe",
        "not defteri": "notepad.exe",
        "paint": "mspaint.exe",
    },
}


def ayarlari_yukle():
    ayar = dict(VARSAYILAN_AYARLAR)
    if os.path.exists(AYAR_DOSYASI):
        try:
            with open(AYAR_DOSYASI, "r", encoding="utf-8") as f:
                ayar.update(json.load(f))
        except (OSError, json.JSONDecodeError) as e:
            log.error("ayarlar.json okunamadı: %s", e)
    else:
        ayar_kaydet(ayar)
    return ayar


def ayar_kaydet(ayar):
    try:
        with open(AYAR_DOSYASI, "w", encoding="utf-8") as f:
            json.dump(ayar, f, ensure_ascii=False, indent=2)
    except OSError as e:
        log.error("ayarlar.json yazılamadı: %s", e)


AYAR = ayarlari_yukle()

# ---------- Ortak durum ve kuyruklar ----------

durum = {"mic": False, "sesli": True, "cik": False, "konusuyor": False,
         "ollama": False, "dinliyor": False, "uretiyor": False, "durdur": False}
girdi_kuyrugu = queue.Queue()      # (kaynak, veri)
ui_kuyrugu = queue.Queue()         # (etiket, veri)
konusma_kuyrugu = queue.Queue()
bekleyen = {"islem": None}
UYGULAMALAR = {}

EVET = ("evet", "tamam", "onaylıyorum", "eminim")
CIKIS = ("çık", "cik", "exit", "asistanı kapat")
ZAMAN_RE = re.compile(r"(\d+)\s*(saniye|dakika)\s*sonra", re.IGNORECASE)

RENK = {
    "arka": "#0f1115", "yan": "#15181e", "panel": "#1a1d24", "sohbet": "#12141a",
    "giris": "#1f232c", "metin": "#e6e8ee", "soluk": "#8b93a5",
    "kullanici": "#5ab0ff", "asistan": "#6ee7a8", "sistem": "#8b93a5",
    "hata": "#ff6b6b", "hatirlatma": "#ffb454", "vurgu": "#4c6fff",
    "vurgu2": "#6a86ff", "ok": "#4ade80", "kod_arka": "#0a0c10",
    "kullanici_arka": "#1c2740", "cizgi": "#262b36",
    "kart": "#161a22", "chip": "#1c2130",
}


def arayuze(etiket, veri=None):
    ui_kuyrugu.put((etiket, veri))


# ---------- Medya tuşları ----------

user32 = ctypes.windll.user32
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
VK_MEDIA_NEXT, VK_MEDIA_PREV, VK_MEDIA_STOP, VK_MEDIA_PLAY_PAUSE = 0xB0, 0xB1, 0xB2, 0xB3
KEYEVENTF_KEYUP = 0x0002


def tus_bas(vk, adet=1):
    for _ in range(adet):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)


def medya(vk, mesaj, adet=1):
    tus_bas(vk, adet)
    arayuze("sistem", mesaj)


# ---------- Uygulama indeksi ----------

BASLAT_KLASORLERI = [
    os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                 r"Microsoft\Windows\Start Menu\Programs"),
]
ATLANACAK_KELIMELER = ("uninstall", "kaldır", "readme", "help", "yardım", "belgel", "update")
WEB_KISAYOLLARI = {
    "tarayıcı": "https://www.google.com", "varsayılan tarayıcı": "https://www.google.com",
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com",
    "whatsapp web": "https://web.whatsapp.com", "whatsapp": "https://web.whatsapp.com",
}


def store_uygulamalari():
    """Microsoft Store uygulamaları (Outlook, WhatsApp vb.) için [(ad, AppID)] listesi."""
    import csv
    import io
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
                            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                            "Get-StartApps | ConvertTo-Csv -NoTypeInformation"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
        satirlar = csv.DictReader(io.StringIO(r.stdout))
        return [(s["Name"], s["AppID"]) for s in satirlar if s.get("Name") and s.get("AppID")]
    except (OSError, subprocess.SubprocessError, KeyError) as e:
        log.error("Store uygulamaları okunamadı: %s", e)
        return []


def uygulamalari_tara():
    indeks = {}
    for kok in BASLAT_KLASORLERI:
        for dp, _, dosyalar in os.walk(kok):
            for dosya in dosyalar:
                if not dosya.lower().endswith(".lnk"):
                    continue
                ad = tr_kucuk(os.path.splitext(dosya)[0])
                if any(k in ad for k in ATLANACAK_KELIMELER):
                    continue
                indeks.setdefault(ad, os.path.join(dp, dosya))
    for ad, kimlik in store_uygulamalari():
        indeks.setdefault(tr_kucuk(ad), "shell:AppsFolder\\" + kimlik)
    for ad, yol in AYAR["ek_uygulamalar"].items():
        indeks.setdefault(tr_kucuk(ad), yol)
    return indeks


def uygulama_bul(ad):
    ad = tr_kucuk(ad).strip()
    if not ad:
        return None
    if ad in UYGULAMALAR:
        return ad
    adaylar = [k for k in UYGULAMALAR if ad in k or k in ad]
    if adaylar:
        return min(adaylar, key=len)
    yakin = difflib.get_close_matches(ad, list(UYGULAMALAR), n=1, cutoff=0.72)
    return yakin[0] if yakin else None


def uygulama_ac(ad):
    ad = tr_kucuk(ad).strip()
    if not ad:
        return "Hangi uygulamayı açacağımı anlayamadım."
    if ad in WEB_KISAYOLLARI and not (ad == "whatsapp" and uygulama_bul(ad)):
        webbrowser.open(WEB_KISAYOLLARI[ad])
        return f"{ad} açıldı."
    eslesen = uygulama_bul(ad)
    if not eslesen:
        return f"'{ad}' bu bilgisayarda bulunamadı."
    try:
        os.startfile(UYGULAMALAR[eslesen])
        return f"{eslesen} açıldı."
    except OSError as e:
        log.error("Uygulama açılamadı (%s): %s", eslesen, e)
        return f"{eslesen} açılamadı: {e}"


# ---------- Hafıza ve günlük ----------

def hafiza_oku():
    if os.path.exists(HAFIZA_DOSYASI):
        try:
            with open(HAFIZA_DOSYASI, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.error("hafiza.json okunamadı: %s", e)
    return []


def hafiza_kaydet(liste):
    with open(HAFIZA_DOSYASI, "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=2)


def hafiza_yaz(bilgi):
    notlar = hafiza_oku()
    if bilgi and bilgi not in notlar:
        notlar.append(bilgi)
        hafiza_kaydet(notlar)
    return "Kaydedildi."


def unut(metin):
    if not metin:
        arayuze("sistem", "Neyi unutmamı istersiniz? Örnek: 'şunu unut Yozgat'")
        return
    notlar = hafiza_oku()
    kalan = [n for n in notlar if tr_kucuk(metin) not in tr_kucuk(n)]
    if len(kalan) == len(notlar):
        arayuze("sistem", "Bu konuda kayıt bulamadım.")
        return
    hafiza_kaydet(kalan)
    arayuze("sistem", f"{len(notlar) - len(kalan)} kayıt silindi.")


def gunluga_yaz(metin):
    try:
        with open(GUNLUK_DOSYASI, "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {metin}\n")
    except OSError as e:
        log.error("Günlük yazılamadı: %s", e)


def gunluk_eski_kayitlari_sil():
    if not os.path.exists(GUNLUK_DOSYASI):
        return
    sinir = datetime.datetime.now() - datetime.timedelta(days=AYAR["gunluk_saklama_gun"])
    try:
        with open(GUNLUK_DOSYASI, "r", encoding="utf-8") as f:
            satirlar = f.readlines()
        tutulan = []
        for satir in satirlar:
            try:
                zaman = datetime.datetime.strptime(satir[:19], "%Y-%m-%d %H:%M:%S")
                if zaman >= sinir:
                    tutulan.append(satir)
            except ValueError:
                tutulan.append(satir)
        with open(GUNLUK_DOSYASI, "w", encoding="utf-8") as f:
            f.writelines(tutulan)
    except OSError as e:
        log.error("Günlük temizlenemedi: %s", e)


# ---------- Sohbet oturumları ----------

def oturum_yeni():
    return {"id": uuid.uuid4().hex[:12], "baslik": "Yeni sohbet",
            "zaman": datetime.datetime.now().isoformat(timespec="seconds"),
            "messages": [{"role": "system", "content": ""}]}


def oturum_yolu(oid):
    return os.path.join(OTURUM_KLASORU, f"{oid}.json")


def oturum_kaydet(oturum):
    if len(oturum["messages"]) <= 1:
        return
    try:
        with open(oturum_yolu(oturum["id"]), "w", encoding="utf-8") as f:
            json.dump({"id": oturum["id"], "baslik": oturum["baslik"], "zaman": oturum["zaman"],
                       "messages": oturum["messages"][1:]}, f, ensure_ascii=False)
    except (OSError, TypeError) as e:
        log.error("Oturum kaydedilemedi: %s", e)


def oturum_yukle(oid):
    try:
        with open(oturum_yolu(oid), "r", encoding="utf-8") as f:
            v = json.load(f)
        v["messages"] = [{"role": "system", "content": ""}] + v.get("messages", [])
        return v
    except (OSError, json.JSONDecodeError) as e:
        log.error("Oturum yüklenemedi: %s", e)
        return None


def oturum_listesi():
    liste = []
    for ad in os.listdir(OTURUM_KLASORU):
        if not ad.endswith(".json"):
            continue
        try:
            with open(os.path.join(OTURUM_KLASORU, ad), "r", encoding="utf-8") as f:
                v = json.load(f)
            liste.append((v.get("zaman", ""), v["id"], v.get("baslik", "Sohbet")))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    liste.sort(reverse=True)
    return [(i, b) for _, i, b in liste]


def oturum_sil(oid):
    try:
        os.remove(oturum_yolu(oid))
    except OSError:
        pass


# ---------- Araçlar ----------

def hava_durumu(sehir=""):
    if sehir:
        g = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                         params={"name": sehir, "count": 1, "language": "tr"}, timeout=10).json()
        if not g.get("results"):
            return f"'{sehir}' bulunamadı."
        r = g["results"][0]
        lat, lon, ad = r["latitude"], r["longitude"], r["name"]
    else:
        lat, lon, ad = AYAR["konum_lat"], AYAR["konum_lon"], AYAR["konum_adi"]
    v = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 3,
        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,apparent_temperature",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    }, timeout=10).json()
    d, g = v["current"], v["daily"]
    satir = (f"{ad}: {d['temperature_2m']} °C (hissedilen {d['apparent_temperature']} °C), "
             f"nem %{d['relative_humidity_2m']}, rüzgar {d['wind_speed_10m']} km/sa.")
    gunler = ["bugün", "yarın", "ertesi gün"]
    for i in range(min(3, len(g["time"]))):
        satir += (f" {gunler[i].capitalize()}: {g['temperature_2m_min'][i]}–"
                  f"{g['temperature_2m_max'][i]} °C, yağış ihtimali "
                  f"%{g['precipitation_probability_max'][i]}.")
    return satir


def web_ara(sorgu):
    with DDGS() as ddgs:
        sonuc = list(ddgs.text(sorgu, region="tr-tr", max_results=5))
    if not sonuc:
        return "Sonuç bulunamadı."
    return "\n".join(f"- {s['title']} ({s['href']}): {s['body']}" for s in sonuc)


def sayfa_oku(url):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    t = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", r.text)
    t = html.unescape(re.sub(r"(?s)<[^>]+>", " ", t))
    t = " ".join(t.split())
    return t[:3500] or "Sayfada okunabilir metin bulunamadı."


def tarih_saat():
    s = datetime.datetime.now()
    gunler = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
    aylar = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos",
             "Eylül", "Ekim", "Kasım", "Aralık"]
    return f"{s.day} {aylar[s.month - 1]} {s.year} {gunler[s.weekday()]}, saat {s:%H:%M}"


def not_al(metin):
    with open(NOT_DOSYASI, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M}] {metin}\n")
    return "Not alındı."


def notlari_oku():
    if not os.path.exists(NOT_DOSYASI):
        return "Henüz not yok."
    with open(NOT_DOSYASI, "r", encoding="utf-8") as f:
        return f.read()[-2500:] or "Henüz not yok."


def dosya_ara(ad):
    ad = tr_kucuk(ad).strip()
    if not ad:
        return "Aranacak dosya adı verilmedi."
    ev = os.path.expanduser("~")
    bulunan = []
    for klasor in ("Desktop", "Documents", "Downloads", "Pictures", "Masaüstü", "Belgeler", "İndirilenler"):
        kok = os.path.join(ev, klasor)
        if not os.path.isdir(kok):
            continue
        for dp, _, dosyalar in os.walk(kok):
            for d in dosyalar:
                if ad in tr_kucuk(d):
                    bulunan.append(os.path.join(dp, d))
                    if len(bulunan) >= 10:
                        return "\n".join(bulunan)
    return "\n".join(bulunan) if bulunan else "Dosya bulunamadı."


def sistem_bilgisi():
    bilgi = []
    if psutil:
        bilgi.append(f"CPU: %{psutil.cpu_percent(interval=0.5)}")
        m = psutil.virtual_memory()
        bilgi.append(f"RAM: %{m.percent} ({m.used / 2**30:.1f}/{m.total / 2**30:.1f} GB)")
        b = psutil.sensors_battery()
        if b:
            bilgi.append(f"Pil: %{int(b.percent)}{' (şarjda)' if b.power_plugged else ''}")
    else:
        bilgi.append("(Daha fazla bilgi için: pip install psutil)")
    d = shutil.disk_usage(os.path.abspath(os.sep))
    bilgi.append(f"Disk: {d.used / 2**30:.0f}/{d.total / 2**30:.0f} GB")
    return " | ".join(bilgi)


def ekran_goruntusu():
    try:
        from PIL import ImageGrab
    except ImportError:
        return "Ekran görüntüsü için 'pip install pillow' gerekli."
    klasor = os.path.join(os.path.expanduser("~"), "Pictures")
    os.makedirs(klasor, exist_ok=True)
    yol = os.path.join(klasor, f"ekran_{datetime.datetime.now():%Y%m%d_%H%M%S}.png")
    ImageGrab.grab().save(yol)
    return f"Ekran görüntüsü kaydedildi: {yol}"


def hatirlatici_kur(sure, birim, konu):
    saniye = int(sure) * (60 if "dak" in birim else 1)
    konu = konu or "süre doldu"
    threading.Timer(saniye, hatirlatma_bildir, args=(konu,)).start()
    return f"{sure} {birim} sonra hatırlatılacak: {konu}"


GORSEL_RE = re.compile(r"\[Görsel: (.*?) \| (.*?)\]")
GECMIS = {"o": None}  # çalışan oturum (hızlı yol geçmişe not düşebilsin diye)


def gorsel_indir(url, sorgu):
    ana = re.match(r"(https?://[^/]+)", url)
    basliklar = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                 "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.5"}
    if ana:
        basliklar["Referer"] = ana.group(1) + "/"
    r = requests.get(url, timeout=15, stream=True, headers=basliklar)
    r.raise_for_status()
    veri = b""
    for parca in r.iter_content(65536):
        veri += parca
        if len(veri) > 8 * 2 ** 20:
            raise ValueError("Dosya çok büyük")
    tip = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    esle = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
    if PILImage:
        im = PILImage.open(io.BytesIO(veri))
        ext = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}.get(im.format)
        im.verify()
        if not ext:
            raise ValueError("Görsel biçimi desteklenmiyor: " + str(im.format))
    else:
        ext = esle.get(tip)
        if ext not in (".png", ".gif"):
            raise ValueError("Pillow gerekli (tür: " + tip + ")")
    ad = re.sub(r"\W+", "_", sorgu)[:30].strip("_") or "gorsel"
    yol = os.path.join(GORSEL_KLASORU, f"{ad}_{datetime.datetime.now():%Y%m%d_%H%M%S_%f}{ext}")
    with open(yol, "wb") as f:
        f.write(veri)
    return yol


UA = {"User-Agent": "KamAsistan/2.0 (kisisel masaustu asistani)"}
CEVIRI_ONBELLEK = {}


def ingilizceye_cevir(metin):
    """Arama ifadesini İngilizceye çevirir (yerel model, olmazsa MyMemory). Hata olursa aynısını döner."""
    if metin in CEVIRI_ONBELLEK:
        return CEVIRI_ONBELLEK[metin]
    sonuc = ""
    try:
        r = requests.post(AYAR["ollama_url"] + "/api/chat", json={
            "model": AYAR["model"], "stream": False, "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": "Sen bir çevirmensin. Kullanıcının verdiği Türkçe görsel "
                 "arama ifadesini İngilizceye çevir. Sadece çeviriyi yaz, başka hiçbir şey yazma."},
                {"role": "user", "content": metin}]}, timeout=30)
        r.raise_for_status()
        sonuc = r.json()["message"]["content"].strip().split("\n")[0].strip(" .\"'")
    except Exception as e:
        log.error("Ollama çevirisi başarısız: %s", e)
    if not sonuc or len(sonuc) > 60:
        try:
            r = requests.get("https://api.mymemory.translated.net/get",
                             params={"q": metin, "langpair": "tr|en"}, timeout=10)
            sonuc = r.json()["responseData"]["translatedText"].strip()
        except Exception as e:
            log.error("MyMemory çevirisi başarısız: %s", e)
            sonuc = ""
    sonuc = sonuc if sonuc and len(sonuc) <= 60 else metin
    CEVIRI_ONBELLEK[metin] = sonuc
    return sonuc


def gorsel_adaylari(sorgu):
    """Önce DuckDuckGo, olmazsa Openverse, o da olmazsa Wikimedia Commons. [(url, küçük, başlık)]"""
    adaylar = []
    try:
        with DDGS() as ddgs:
            for s in ddgs.images(sorgu, region="tr-tr", safesearch="moderate", max_results=40):
                adaylar.append((s.get("image"), s.get("thumbnail"), s.get("title") or sorgu))
    except Exception as e:
        log.error("DuckDuckGo görsel araması başarısız: %s", e)
    en = sorgu
    if len(adaylar) < 3:
        en = ingilizceye_cevir(sorgu)
        log.error("Yedek kaynaklar için arama: %r -> %r", sorgu, en)
    if len(adaylar) < 3:
        try:
            r = requests.get("https://api.openverse.org/v1/images/",
                             params={"q": en, "page_size": 15}, timeout=12, headers=UA)
            r.raise_for_status()
            for s in r.json().get("results", []):
                adaylar.append((s.get("url"), None, s.get("title") or sorgu))
        except Exception as e:
            log.error("Openverse görsel araması başarısız: %s", e)
    if len(adaylar) < 3:
        try:
            r = requests.get("https://commons.wikimedia.org/w/api.php", params={
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": en, "gsrnamespace": 6, "gsrlimit": 12,
                "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": 800}, timeout=12, headers=UA)
            r.raise_for_status()
            for p in (r.json().get("query", {}).get("pages", {})).values():
                bilgi = (p.get("imageinfo") or [{}])[0]
                if bilgi.get("mime", "").startswith("image/") and "svg" not in bilgi.get("mime", ""):
                    adaylar.append((bilgi.get("thumburl") or bilgi.get("url"), None,
                                    p.get("title", sorgu).replace("File:", "")))
        except Exception as e:
            log.error("Wikimedia görsel araması başarısız: %s", e)
    gorulen, sonuc = set(), []
    for url, kucuk_url, baslik in adaylar:
        if url and url not in gorulen:
            gorulen.add(url)
            sonuc.append((url, kucuk_url, baslik))
    return sonuc


GORSEL_GORULEN = {"sorgu": None, "url": set()}


def gorsel_bul(sorgu, adet=5):
    """İnternette sorguya uygun görselleri arar, indirir ve tek bir grup olarak gösterir.

    Aynı sorgu için daha önce gösterilen görseller tekrar gösterilmez ('daha fazla' için).
    """
    sorgu = (sorgu or "").strip()
    if not sorgu:
        return "Hangi görseli arayacağımı anlayamadım."
    try:
        adet = max(1, min(int(adet), 8))
    except (TypeError, ValueError):
        adet = 5
    if GORSEL_GORULEN["sorgu"] != sorgu:
        GORSEL_GORULEN.update(sorgu=sorgu, url=set())
    adaylar = gorsel_adaylari(sorgu)
    if not adaylar:
        return (f"'{sorgu}' için hiçbir kaynaktan görsel alınamadı. İnternet bağlantısı çalışıyorsa "
                "arama servisleri geçici olarak engellemiş olabilir; ayrıntı hata_gunlugu.txt içinde.")
    secilen = []
    for url, kucuk_url, baslik in adaylar:
        if not url or url in GORSEL_GORULEN["url"]:
            continue
        for adres in (url, kucuk_url):
            if not adres:
                continue
            try:
                yol = gorsel_indir(adres, sorgu)
            except Exception as e:
                log.error("Görsel indirilemedi (%s): %s", adres, e)
                continue
            GORSEL_GORULEN["url"].add(url)
            secilen.append((yol, (baslik or sorgu)[:70].replace("|", "-").replace("]", ")")))
            break
        if len(secilen) >= adet:
            break
    if not secilen:
        return f"'{sorgu}' için gösterebileceğim başka görsel kalmadı."
    arayuze("gorsel_grup", {"sorgu": sorgu, "items": secilen})
    return f"'{sorgu}' için {len(secilen)} görsel gösterildi. Kullanıcı birini seçebilir ya da daha fazlasını isteyebilir."


GORSEL_KELIME = re.compile(
    r"^(resim|resmi|resmini|resimleri|resimlerini|resimler|görsel|görseli|görselini|görselleri|"
    r"görseller|fotoğraf|fotoğrafı|fotoğrafını|fotoğrafları|fotoğraflar|foto|fotosu)$")
GORSEL_COGUL = {"resimleri", "resimlerini", "resimler", "görselleri", "görseller",
                "fotoğrafları", "fotoğraflar"}
GORSEL_DOLGU = {"bul", "bulsana", "bulur", "bulurmusun", "bulabilir", "bulabilirmisin", "bulabilir",
                "göster", "göstersene", "gösterir", "getir", "getirsene", "indir", "indirsene",
                "ara", "arasana", "at", "atsana", "ver", "versene", "çıkar", "mısın", "misin",
                "musun", "müsün", "bana", "lütfen", "bir", "şu", "şunun", "bunun", "şunu", "bunu",
                "internetten", "google'dan", "googledan", "için", "ilgili", "hakkında", "ve", "de",
                "da", "bakar", "bakarmısın", "bakabilir", "görebilir", "görelim", "bakalım"}
GORSEL_FIIL = re.compile(
    r"^(bul|göster|getir|indir|çıkar|ara|at|ver|aç|koy)"
    r"(s[ae]n[ae]|s[ae]ne|[uüıi]r\w*|abil\w*|[ae]r\w*)?$")
GORSEL_ATLA = ("resmi tatil", "resmi gazete", "resmi kurum", "resmi iş", "resmi evrak",
               "resmi işlem", "ekran görüntüsü", "resim çiz", "görsel oluştur", "resim oluştur",
               "resim yap", "görsel yap")


GORSEL_ANAHTAR = ["görsel", "görseli", "görselini", "görselleri", "resim", "resmi", "resmini",
                  "resimleri", "fotoğraf", "fotoğrafı", "fotoğrafını"]
GORSEL_FIILLER = ["getir", "bul", "göster", "indir", "getirsene", "bulsana", "göstersene"]


def _gorsel_duzelt(k):
    """Yazım hatalarını düzeltir: 'görsweli' -> 'görseli', 'getşr' -> 'getir'."""
    if len(k) < 4 or GORSEL_KELIME.match(k) or GORSEL_FIIL.match(k):
        return k
    y = difflib.get_close_matches(k, GORSEL_ANAHTAR, n=1, cutoff=0.78)
    if y:
        return y[0]
    y = difflib.get_close_matches(k, GORSEL_FIILLER, n=1, cutoff=0.75)
    return y[0] if y else k


def gorsel_istegi(kucuk):
    """'top resmi', 'kedi görseli bul' gibi istekleri ayıklar. (sorgu, adet) ya da None."""
    if any(a in kucuk for a in GORSEL_ATLA):
        return None
    kelimeler = [_gorsel_duzelt(k) for k in re.findall(r"[\w'’]+", kucuk)]
    if not any(GORSEL_KELIME.match(k) for k in kelimeler):
        return None
    fiil_var = any(GORSEL_FIIL.match(k) for k in kelimeler)
    sorgu = [k for k in kelimeler if not GORSEL_KELIME.match(k) and k not in GORSEL_DOLGU
             and not GORSEL_FIIL.match(k)]
    if not sorgu or (not fiil_var and (len(kelimeler) > 4 or not GORSEL_KELIME.match(kelimeler[-1]))):
        return None
    adet = 5
    return " ".join(sorgu), adet


def _g(a, k, varsayilan=""):
    return a.get(k, varsayilan) if isinstance(a, dict) else varsayilan


ARAC_FONKSIYONLARI = {
    "hava_durumu": lambda a: hava_durumu(_g(a, "sehir")),
    "web_ara": lambda a: web_ara(_g(a, "sorgu")),
    "sayfa_oku": lambda a: sayfa_oku(_g(a, "url")),
    "uygulama_ac": lambda a: uygulama_ac(_g(a, "ad")),
    "hafiza_yaz": lambda a: hafiza_yaz(_g(a, "bilgi")),
    "tarih_saat": lambda a: tarih_saat(),
    "hesapla": lambda a: hesapla(str(_g(a, "ifade"))),
    "not_al": lambda a: not_al(_g(a, "metin")),
    "notlari_oku": lambda a: notlari_oku(),
    "dosya_ara": lambda a: dosya_ara(_g(a, "ad")),
    "sistem_bilgisi": lambda a: sistem_bilgisi(),
    "ekran_goruntusu": lambda a: ekran_goruntusu(),
    "hatirlatici_kur": lambda a: hatirlatici_kur(_g(a, "sure", 1), str(_g(a, "birim", "dakika")),
                                                 _g(a, "konu")),
    "gorsel_bul": lambda a: gorsel_bul(_g(a, "sorgu"), _g(a, "adet", 5)),
    "parlaklik": lambda a: (parlaklik_degistir(20 if _g(a, "yon") == "artir" else -20)
                            if _g(a, "yon") else parlaklik_ayarla(_g(a, "yuzde", 50))),
    "bilgisayar_islem": lambda a: bilgisayar_islem(_g(a, "islem")),
    "klasor_ac": lambda a: klasor_ac(_g(a, "ad")),
    "tarayicida_ara": lambda a: tarayicida_ara(_g(a, "sorgu"), _g(a, "site", "google")),
    "virus_tara": lambda a: virus_tara(_g(a, "tur", "hizli")),
    "mail_kontrol": lambda a: mail_kontrol(),
    "panoya_kopyala": lambda a: (arayuze("pano", _g(a, "metin")), "Panoya kopyalandı.")[1],
}


def _arac(ad, aciklama, ozellikler=None, zorunlu=None):
    p = {"type": "object", "properties": ozellikler or {}}
    if zorunlu:
        p["required"] = zorunlu
    return {"type": "function", "function": {"name": ad, "description": aciklama, "parameters": p}}


S = {"type": "string"}
ARACLAR = [
    _arac("hava_durumu", "Bir şehrin güncel hava durumunu ve 3 günlük tahminini getirir. "
          "Şehir verilmezse varsayılan konum kullanılır.", {"sehir": S}),
    _arac("web_ara", "İnternette güncel bilgi arar.", {"sorgu": S}, ["sorgu"]),
    _arac("sayfa_oku", "Bir web sayfasının metnini okur.", {"url": S}, ["url"]),
    _arac("uygulama_ac", "Bilgisayarda yüklü bir uygulamayı adıyla açar.", {"ad": S}, ["ad"]),
    _arac("hafiza_yaz", "Kullanıcı hakkında kalıcı hatırlanacak bilgiyi kaydeder.", {"bilgi": S}, ["bilgi"]),
    _arac("tarih_saat", "Güncel tarih ve saati verir."),
    _arac("hesapla", "Matematiksel ifadeyi hesaplar (örn. 12*(3+4), sqrt(16)).", {"ifade": S}, ["ifade"]),
    _arac("not_al", "Kullanıcının notunu not defterine ekler.", {"metin": S}, ["metin"]),
    _arac("notlari_oku", "Kaydedilmiş notları okur."),
    _arac("dosya_ara", "Masaüstü, Belgeler, İndirilenler ve Resimler içinde dosya adı arar.", {"ad": S}, ["ad"]),
    _arac("sistem_bilgisi", "CPU, RAM, disk ve pil durumunu verir."),
    _arac("ekran_goruntusu", "Ekran görüntüsü alıp Resimler klasörüne kaydeder."),
    _arac("hatirlatici_kur", "Belirli süre sonra hatırlatma kurar.",
          {"sure": {"type": "integer"}, "birim": {"type": "string", "enum": ["saniye", "dakika"]},
           "konu": S}, ["sure", "birim", "konu"]),
    _arac("gorsel_bul", "İnternetten bir konunun görselini bulup indirir ve kullanıcıya sohbet ekranında gösterir.", {"sorgu": S, "adet": {"type": "integer"}}, ["sorgu"]),
    _arac("parlaklik", "Sadece ekran parlaklığını ayarlar (ses için kullanılmaz). yuzde: 0-100 arası sayı. Ya da yon: artir veya azalt.",
          {"yuzde": {"type": "integer"}, "yon": {"type": "string", "enum": ["artir", "azalt"]}}),
    _arac("bilgisayar_islem", "Bilgisayarı kapatır, yeniden başlatır, uyutur veya kilitler. "
          "Kapatma, yeniden başlatma ve uyku için kullanıcıdan onay istenir.",
          {"islem": {"type": "string", "enum": ["kapat", "yeniden_baslat", "uyku", "kilitle"]}}, ["islem"]),
    _arac("klasor_ac", "Masaüstü, belgeler, indirilenler gibi bir klasörü ya da adı verilen dosyanın klasörünü açar.",
          {"ad": S}, ["ad"]),
    _arac("tarayicida_ara", "Tarayıcıda arama yapar. site: google, youtube, wikipedia veya harita.",
          {"sorgu": S, "site": {"type": "string", "enum": ["google", "youtube", "wikipedia", "harita"]}},
          ["sorgu"]),
    _arac("virus_tara", "Windows Defender ile bilgisayarda virüs taraması başlatır. tur: hizli veya tam.",
          {"tur": {"type": "string", "enum": ["hizli", "tam"]}}),
    _arac("mail_kontrol", "Outlook'a bakıp bugün mail gelip gelmediğini ve gönderenleri söyler."),
    _arac("panoya_kopyala", "Verilen metni panoya kopyalar.", {"metin": S}, ["metin"]),
]


def sistem_metni():
    metin = (f"Senin adın {AYAR['sistem_adi']}. Kullanıcının adı {AYAR['kullanici_adi']}. "
             f"Bugün: {tarih_saat()}. Kullanıcının konumu: {AYAR['konum_adi']}. "
             "Sen kullanıcının bilgisayarında çalışan zeki, samimi ve yetkin bir asistansın. "
             "Her zaman Türkçe yanıt ver. Basit sorulara kısa, karmaşık konulara ayrıntılı ve "
             "düzenli yanıt ver; gerekirse madde işareti ve kod blokları kullan. "
             "Emin değilsen bunu söyle, uydurma. Güncel bilgi gerektiğinde web_ara, hava için "
             "hava_durumu, uygulama için uygulama_ac, hesap için hesapla araçlarını kullan. "
             "Kullanıcı bir şeyi hatırlamanı isterse hafiza_yaz kullan. "
             "Kullanıcı bir şeyin resmini/görselini isterse gorsel_bul kullan; görsel ekranda "
             "otomatik gösterilir, sen sadece kısa bir not yaz. Dosya yolu ya da markdown görsel "
             "bağlantısı yazma; yeni görsel istenirse her seferinde gorsel_bul aracını çağır. "
             "Araç adlarını ya da JSON'u kullanıcıya yazma; araçları doğrudan çağır. "
             "Ses seviyesi için parlaklik aracını kullanma; ses için yüzde ayarı yoktur. "
             "Uydurma isim, ürün veya kişi yazma. Emin değilsen bunu söyle. "
             "Yalnızca Türkçe yaz; Çince veya başka dil kullanma. "
             "Yalnızca kullanıcının EN SON mesajına cevap ver; önceki cevaplarını tekrar etme, "
             "konuşulmamış konular hakkında yorum yapma.")
    notlar = hafiza_oku()
    if notlar:
        metin += "\nKullanıcı hakkında bilinenler: " + "; ".join(notlar)
    return metin


# ---------- Seslendirme ----------

def seslik_metin(metin):
    metin = re.sub(r"```.*?```", " kod bloğu. ", metin, flags=re.S)
    metin = re.sub(r"[*`#_>]", "", metin)
    return " ".join(metin.split())[:1200]


def edge_wav_uret(metin, cikis):
    """Microsoft Edge TTS ile Türkçe erkek ses üretir (internet ve ffmpeg gerekir)."""
    import asyncio
    import subprocess
    import edge_tts
    mp3 = cikis + ".mp3"

    async def _uret():
        com = edge_tts.Communicate(metin, AYAR.get("edge_ses", "tr-TR-AhmetNeural"),
                                   rate=AYAR.get("edge_hiz", "-5%"),
                                   pitch=AYAR.get("edge_perde", "-6Hz"))
        await com.save(mp3)

    asyncio.run(_uret())
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-ac", "1", "-ar", "22050", cikis],
                   check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        os.remove(mp3)
    except OSError:
        pass


def robot_efekt_uygula(yol):
    """WAV dosyasını yerinde robotik efektle işler (perde, metalik titreşim, yankı)."""
    try:
        import wave
        import numpy as np
    except ImportError as e:
        log.error("Robot efekti için numpy gerekli, efektsiz çalınacak: %s", e)
        return
    with wave.open(yol, "rb") as w:
        kanal, genislik, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        veri = w.readframes(w.getnframes())
    if genislik != 2:
        return
    x = np.frombuffer(veri, dtype=np.int16).astype(np.float32) / 32768.0
    if kanal > 1:
        x = x.reshape(-1, kanal).mean(axis=1)
    perde = min(1.0, max(0.5, float(AYAR.get("robot_perde", 0.72))))
    halka_hz = float(AYAR.get("robot_halka_hz", 35))
    yanki = min(0.6, max(0.0, float(AYAR.get("robot_yanki", 0.35))))
    idx = np.arange(0, len(x) - 1, perde)
    y = np.interp(idx, np.arange(len(x)), x).astype(np.float32)
    t = np.arange(len(y)) / sr
    y = y * (0.35 + 0.65 * np.abs(np.sin(np.pi * halka_hz * t)))
    gecikme = int(0.12 * sr)
    if 0 < gecikme < len(y):
        fark = np.zeros_like(y)
        fark[gecikme:] = y[:-gecikme] * yanki
        y = y + fark
    tepe = float(np.max(np.abs(y))) or 1.0
    y = (y / tepe * 0.9 * 32767).astype(np.int16)
    with wave.open(yol, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(y.tobytes())


oynat_kuyrugu = queue.Queue()   # hazır ses dosyaları (yol)
def robot_hazirla(motor, metin, cikis):
    """Metni bir WAV dosyasına çevirir (Edge TTS ya da pyttsx3) ve robot efektini uygular."""
    kaynak = False
    if AYAR.get("ses_motoru", "edge") == "edge":
        try:
            edge_wav_uret(metin, cikis)
            kaynak = True
        except Exception as e:
            log.error("Edge TTS başarısız, yedek motor kullanılıyor: %s", e)
    if not kaynak:
        motor.save_to_file(metin, cikis)
        motor.runAndWait()
    if AYAR.get("robot_ses", True):
        robot_efekt_uygula(cikis)
    return cikis


def sese_hazirla_ve_yolla(motor, metin):
    yol = os.path.join(KLASOR, f"_kam_{uuid.uuid4().hex[:8]}.wav")
    try:
        robot_hazirla(motor, metin, yol)
        oynat_kuyrugu.put(yol)
    except Exception as e:
        log.error("Ses hazırlanamadı, yedek ses kullanılıyor: %s", e)
        try:
            os.remove(yol)
        except OSError:
            pass
        motor.say(metin)
        motor.runAndWait()


def oynatici_iscisi():
    """Hazır WAV dosyalarını sırayla çalar; hazırlayıcı bir sonrakini bu sırada hazırlar."""
    import winsound
    while True:
        yol = oynat_kuyrugu.get()
        if yol is None:
            break
        try:
            winsound.PlaySound(yol, winsound.SND_FILENAME)
        except Exception as e:
            log.error("Ses çalınamadı: %s", e)
        finally:
            try:
                os.remove(yol)
            except OSError:
                pass
            if konusma_kuyrugu.empty() and oynat_kuyrugu.empty():
                durum["konusuyor"] = False


def konusma_iscisi():
    try:
        motor = pyttsx3.init()
        motor.setProperty("rate", AYAR["konusma_hizi"])
        tum = motor.getProperty("voices")
        tr_sesler = [s for s in tum if "turkish" in s.name.lower()
                     or "tr-tr" in s.id.lower() or "tr_tr" in s.id.lower()]
        istenen = str(AYAR.get("ses_adi", "")).lower().strip()
        secilen = None
        if istenen:
            secilen = next((s for s in tum if istenen in s.name.lower()), None)
        if secilen is None and tr_sesler:
            secilen = tr_sesler[0]
        if secilen is not None:
            motor.setProperty("voice", secilen.id)
        log.error("Ses bilgisi | seçilen: %s | Türkçe sesler: %s | tümü: %s",
                  secilen.name if secilen else "yok",
                  [s.name for s in tr_sesler], [s.name for s in tum])
    except Exception as e:
        log.error("Seslendirme başlatılamadı: %s", e)
        arayuze("hata", "Seslendirme başlatılamadı.")
        return
    while True:
        metin = konusma_kuyrugu.get()
        if metin is None:
            oynat_kuyrugu.put(None)
            break
        try:
            sese_hazirla_ve_yolla(motor, metin)
        except Exception as e:
            log.error("Seslendirme hatası: %s", e)
        finally:
            time.sleep(0.4)
            if konusma_kuyrugu.empty():
                durum["konusuyor"] = False


def seslendir(metin):
    if not durum["sesli"]:
        return
    metin = seslik_metin(metin)
    if not metin:
        return
    durum["konusuyor"] = True
    for parca in cumlelere_bol(metin):
        konusma_kuyrugu.put(parca)


def sesi_sustur():
    for kuyruk in (konusma_kuyrugu, oynat_kuyrugu):
        try:
            while True:
                eleman = kuyruk.get_nowait()
                if isinstance(eleman, str) and eleman.endswith(".wav"):
                    try:
                        os.remove(eleman)
                    except OSError:
                        pass
        except queue.Empty:
            pass
    try:
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception:
        pass


# ---------- Ses girişi ----------

_STT = None


def _yerel_metin(ses):
    """Ses verisini geçici WAV dosyasına yazar ve faster-whisper ile metne çevirir."""
    global _STT
    if _STT is None:
        _STT = KonusmaTanima(model_adi=AYAR.get("stt_model", "small"))
    fd, yol = tempfile.mkstemp(suffix=".wav", dir=KLASOR)
    os.close(fd)
    try:
        with open(yol, "wb") as f:
            f.write(ses.get_wav_data())
        return _STT.metne_cevir(yol, dil="tr")
    finally:
        try:
            os.remove(yol)
        except OSError:
            pass


_ORTAM = {"esik": None}  # ortam gürültü eşiği: bir kez ölçülür, sonra saklanır

SESLI_SOZLUK = ["aç", "kapat", "yeniden", "başlat", "parlaklık", "artır", "azalt", "hava", "durumu",
                "mail", "tara", "virüs", "saat", "hesapla", "sesi", "sesli", "kilitle", "klasör", "götür",
                "ara", "hatırlat", "fotoğraf", "yükselt", "sustur", "mikrofon", "yardım", "bilgisayarı",
                "uyut", "tarayıcı", "tam", "cevap", "spotify", "whatsapp", "outlook", "youtube"]


def _dinle_ham():
    r = sr.Recognizer()
    r.energy_threshold = AYAR["enerji_esigi"]
    r.dynamic_energy_threshold = True
    r.pause_threshold = AYAR["duraklama_suresi"]
    try:
        with sr.Microphone() as kaynak:
            if _ORTAM["esik"] is None:
                r.adjust_for_ambient_noise(kaynak, duration=0.5)
                _ORTAM["esik"] = r.energy_threshold
            else:
                r.energy_threshold = _ORTAM["esik"]
            ses = r.listen(kaynak, timeout=max(AYAR["dinleme_zaman_asimi"], 8),
                           phrase_time_limit=max(AYAR["cumle_sure_siniri"], 12))
    except sr.WaitTimeoutError:
        return ""
    except (OSError, AttributeError) as e:
        log.error("Mikrofon hatası: %s", e)
        arayuze("hata", "Mikrofon açılamadı. Windows mikrofon ayarlarını kontrol edin.")
        mic_ayarla(False)
        return ""
    if AYAR.get("stt_motoru", "whisper") == "whisper":
        try:
            return _yerel_metin(ses)
        except ImportError as e:
            log.error("faster-whisper kurulu değil, Google kullanılıyor: %s", e)
    try:
        return r.recognize_google(ses, language="tr-TR").strip()
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        log.error("Ses servisi hatası: %s", e)
        arayuze("hata", "Ses servisine ulaşılamadı, internet bağlantısını kontrol edin.")
        return ""


def dinle():
    """Mikrofondan metin alır ve bilinen komut kelimelerine göre düzeltir."""
    metin = _dinle_ham()
    return sesi_duzelt(metin, SESLI_SOZLUK + list(UYGULAMALAR) + list(WEB_KISAYOLLARI))


def mikrofon_iscisi():
    while not durum["cik"]:
        if not durum["mic"] or durum["konusuyor"] or durum["uretiyor"]:
            time.sleep(0.2)
            continue
        arayuze("durum", ("dinliyor", True))
        metin = dinle()
        arayuze("durum", ("dinliyor", False))
        if metin:
            arayuze("kullanici", metin)
            girdi_kuyrugu.put(("mic", metin))


# ---------- Model bağlantısı ----------

def ollama_mesajlari(messages):
    """Modele giden geçmişten görsel dosya yollarını çıkarır; model yol uydurup yazmasın."""
    cikti = []
    for m in messages:
        c = m.get("content")
        if c and "[Görsel:" in c:
            m = dict(m, content=GORSEL_RE.sub(lambda g: f"[Görsel gösterildi: {g.group(1)}]", c))
        cikti.append(m)
    son_gorsel = max((i for i, m in enumerate(cikti) if m.get("images")), default=-1)
    return [m if (i == son_gorsel or not m.get("images")) else {k: v for k, v in m.items() if k != "images"}
            for i, m in enumerate(cikti)]


def metinden_arac_ayikla(icerik):
    """kam.textutil.arac_cagrisi_ayikla sarmalayıcısı; akış kodunun beklediği biçimde döner."""
    temiz, cagrilar = arac_cagrisi_ayikla(icerik, set(ARAC_FONKSIYONLARI))
    return temiz, [{"function": {"name": ad, "arguments": args}} for ad, args in cagrilar]


def gorsel_model_sec(messages):
    """Fotoğraflı turda görüntü modelini, değilse seçili normal modeli kullanır."""
    return AYAR.get("gorsel_model", "llava:7b") if _gorsel_turu_mu(messages) else AYAR["model"]


def arac_listesi_sec(messages):
    """Görüntü modelleri araç çağırmaz; o turda araç listesi boş gönderilir."""
    return ARACLAR if gorsel_model_sec(messages) == AYAR["model"] else []


def _gorsel_turu_mu(messages):
    """Son kullanıcı mesajında fotoğraf var mı? (Model adına bakmaz; seçili model llava olsa da çalışır.)"""
    for m in reversed(messages):
        if m.get("role") == "user":
            return bool(m.get("images"))
    return False


def _gorsel_secenek(messages):
    """Fotoğraflı turda tekrar döngüsünü ve uzun cevapları sınırlayan ayarlar."""
    if not _gorsel_turu_mu(messages):
        return {}
    return {"temperature": 0.2, "repeat_penalty": 1.3, "num_predict": 400}


def _arac_alani(messages):
    """Fotoğraflı turda araç alanı hiç gönderilmez (görüntü modelleri araç desteklemez)."""
    return {} if _gorsel_turu_mu(messages) else {"tools": ARACLAR}


def akisla(messages):
    """Ollama'dan akışlı yanıt alır. Metin parçalarını arayüze gönderir."""
    yanit = requests.post(AYAR["ollama_url"] + "/api/chat", json={
        "model": gorsel_model_sec(messages), "messages": ollama_mesajlari(messages),
        **_arac_alani(messages),
        "stream": True, "options": {
            "num_ctx": AYAR.get("baglam_uzunlugu", 8192),
            "temperature": AYAR.get("model_sicaklik", 0.4),
            "top_p": 0.9,
            "repeat_penalty": 1.15,
            **_gorsel_secenek(messages),
        },
    }, stream=True, timeout=(10, 300))
    yanit.raise_for_status()
    icerik, araclar, basladi = "", [], False
    try:
        for satir in yanit.iter_lines():
            if durum["durdur"]:
                break
            if not satir:
                continue
            veri = json.loads(satir)
            m = veri.get("message", {})
            parca = m.get("content", "")
            if parca:
                if not basladi:
                    arayuze("akis_basla")
                    basladi = True
                icerik += parca
                arayuze("akis_parca", parca)
            araclar.extend(m.get("tool_calls") or [])
            if veri.get("done"):
                break
    finally:
        yanit.close()
    temiz, sizan = metinden_arac_ayikla(icerik)
    if sizan:
        araclar.extend(sizan)
        icerik = temiz
    if basladi:
        arayuze("akis_bitti", icerik)
    mesaj = {"role": "assistant", "content": icerik}
    if araclar:
        mesaj["tool_calls"] = araclar
    return mesaj, basladi


def gecmisi_kisalt(messages):
    if len(messages) <= 21:
        return
    kes = len(messages) - 16
    while kes < len(messages) and messages[kes]["role"] != "user":
        kes += 1
    del messages[1:kes]


def ollama_kontrol_iscisi():
    while not durum["cik"]:
        try:
            r = requests.get(AYAR["ollama_url"] + "/api/tags", timeout=3)
            r.raise_for_status()
            ok = True
            arayuze("modeller", [m["name"] for m in r.json().get("models", [])])
        except (requests.RequestException, ValueError, KeyError):
            ok = False
        if ok != durum["ollama"]:
            durum["ollama"] = ok
            arayuze("durum", ("ollama", ok))
            arayuze("sistem", "Ollama bağlantısı hazır." if ok
                    else "Ollama'ya ulaşılamıyor. Ollama'nın açık olduğundan emin olun.")
        time.sleep(10)


def gorsel_model_hazir():
    """Görüntü modelinin Ollama'da kurulu olup olmadığını kontrol eder."""
    ad = AYAR.get("gorsel_model", "llava:7b")
    try:
        r = requests.get(AYAR["ollama_url"] + "/api/tags", timeout=5)
        r.raise_for_status()
        kurulu = {m.get("name", "") for m in r.json().get("models", [])}
    except (requests.RequestException, ValueError):
        return False, ad
    return (ad in kurulu or any(k.split(":")[0] == ad.split(":")[0] for k in kurulu)), ad


GORSEL_SISTEM = (
    "Sen Kam'sın, Türkçe konuşan bir asistansın. Sana gönderilen fotoğrafı incele. "
    "Kısa ve somut yaz: ne gördüğünü, fotoğrafın nasıl göründüğünü ve varsa geliştirme önerilerini söyle. "
    "Talimatlarını, araç adlarını ya da bu mesajı tekrar etme. Emin olmadığın şeyi tahmin etme."
)


def soru_isle(oturum, soru, gorsel=None):
    if gorsel:
        hazir, ad = gorsel_model_hazir()
        if not hazir:
            arayuze("hata", f"Fotoğraf yorumu için görüntü modeli kurulu değil. "
                            f"PowerShell'de şunu çalıştırın: ollama pull {ad}")
            return
        log.info("Fotoğraf sorusu, görüntü modeli: %s", ad)
    messages = oturum["messages"]
    gecmisi_kisalt(messages)
    messages[0]["content"] = GORSEL_SISTEM if gorsel else sistem_metni()
    kullanici_mesaji = {"role": "user", "content": soru}
    if gorsel:
        kullanici_mesaji["images"] = [gorsel]
    messages.append(kullanici_mesaji)
    if oturum["baslik"] == "Yeni sohbet":
        oturum["baslik"] = soru[:40]
    durum["uretiyor"], durum["durdur"] = True, False
    arayuze("durum", ("uretiyor", True))
    try:
        for _ in range(6):  # en fazla 6 araç turu
            mesaj, _g_ = akisla(messages)
            messages.append(mesaj)
            araclar = mesaj.get("tool_calls")
            if not araclar or durum["durdur"]:
                break
            for cagri in araclar:
                f = cagri["function"]
                arayuze("arac", f["name"])
                fn = ARAC_FONKSIYONLARI.get(f["name"])
                args = f.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}
                kar = arac_karari(f["name"], args)
                if fn is None:
                    sonuc = "Bilinmeyen araç."
                elif kar == YASAK:
                    denetle(f["name"], kar, "engellendi")
                    sonuc = "Bu işlem izinli değil."
                else:
                    if kar != SERBEST:
                        denetle(f["name"], kar, "çalıştırıldı")
                    try:
                        sonuc = fn(args)
                    except Exception as e:
                        log.exception("Araç hatası: %s", f["name"])
                        sonuc = f"Araç hatası: {e}"
                messages.append({"role": "tool", "content": str(sonuc)})
        cevap = (messages[-1].get("content") or "").strip()
        if durum["durdur"]:
            arayuze("sistem", "Yanıt durduruldu.")
        elif not cevap:
            arayuze("asistan", "(Yanıt boş geldi)")
        else:
            seslendir(cevap)
    except requests.RequestException as e:
        log.error("Ollama isteği başarısız: %s", e)
        arayuze("akis_iptal")
        cevap_obj = getattr(e, "response", None)
        if cevap_obj is not None:
            govde = (cevap_obj.text or "").strip()[:200]
            mesaj = f"Ollama hata verdi ({cevap_obj.status_code}): {govde or 'ayrıntı yok'}"
        elif isinstance(e, requests.Timeout):
            mesaj = "Ollama zaman aşımına uğradı. Model yükleniyor olabilir, birkaç saniye sonra tekrar deneyin."
        else:
            mesaj = "Ollama'ya bağlanılamadı. Ollama'nın açık olduğundan emin olun."
        arayuze("hata", mesaj)
    finally:
        durum["uretiyor"] = False
        arayuze("durum", ("uretiyor", False))
        oturum_kaydet(oturum)
        arayuze("oturumlar", oturum_listesi())


# ---------- Komutlar ----------

def mic_ayarla(acik):
    durum["mic"] = acik
    arayuze("durum", ("mic", acik))
    arayuze("sistem", "Mikrofon açıldı." if acik else "Mikrofon kapatıldı.")


def sesli_ayarla(acik):
    durum["sesli"] = acik
    if not acik:
        sesi_sustur()
    arayuze("durum", ("sesli", acik))
    arayuze("sistem", "Sesli cevap açıldı." if acik else "Sesli cevap kapatıldı.")


KOMUTLAR = [
    (("mic aç", "mik aç", "mikrofonu aç", "mikrofon aç"), lambda: mic_ayarla(True)),
    (("mic kapat", "mik kapat", "mikrofonu kapat", "mikrofon kapat", "dinlemeyi durdur"),
     lambda: mic_ayarla(False)),
    (("sesli cevap aç", "sesli yanıt aç", "sesli cevabı aç", "sesli yanıtı aç"),
     lambda: sesli_ayarla(True)),
    (("sesli cevap kapat", "sesli yanıt kapat", "sesli cevabı kapat", "sesli yanıtı kapat"),
     lambda: sesli_ayarla(False)),
    (("müziği durdur", "müziği kapat"), lambda: medya(VK_MEDIA_STOP, "Müzik durduruldu")),
    (("müziği duraklat", "duraklat", "müziği çal", "müziği başlat", "müziği aç",
      "müziği oynat", "müziği devam ettir", "şarkıyı aç"),
     lambda: medya(VK_MEDIA_PLAY_PAUSE, "Müzik oynatma/duraklatma")),
    (("sonraki şarkı", "sonraki parça", "sonrakine geç", "sonraki müziğe geçiş yap",
      "sonraki müzik", "sonraki müziğ"), lambda: medya(VK_MEDIA_NEXT, "Sonraki parça")),
    (("önceki şarkı", "önceki parça", "öncekine dön", "önceki müzik"),
     lambda: medya(VK_MEDIA_PREV, "Önceki parça")),
    (("sesi artır", "sesi yükselt", "ses seviyesini yükselt", "ses aç"),
     lambda: medya(VK_VOLUME_UP, "Ses artırıldı", 5)),
    (("sesi azalt", "sesi kıs", "ses seviyesini düşür", "ses kıs"),
     lambda: medya(VK_VOLUME_DOWN, "Ses azaltıldı", 5)),
    (("sessize al", "sesi kapat"), lambda: medya(VK_VOLUME_MUTE, "Ses kapatıldı/açıldı")),
]


def komut_isle(metin):
    """Komut ifadelerini uygular, kalan metni döndürür."""
    kalan = " " + metin + " "
    for ifadeler, islem in KOMUTLAR:
        bulundu = False
        for ifade in sorted(ifadeler, key=len, reverse=True):
            desen = re.compile(re.escape(ifade), re.IGNORECASE)
            if desen.search(kalan):
                bulundu = True
                kalan = desen.sub(" ", kalan)
        if bulundu:
            islem()
    return " ".join(kalan.split())


def denetle(islem, karar_, sonuc):
    """Denetim günlüğüne (denetim.jsonl) bir kayıt ekler. Hata uygulamayı durdurmaz."""
    denetim_yaz(os.path.join(KLASOR, "denetim.jsonl"),
                denetim_kaydi(islem, karar_, sonuc, kaynak="kullanici"))


def bekle_onay(islem, mesaj):
    denetle(islem, ONAY, "istendi")
    bekleyen["islem"] = islem
    arayuze("sistem", f"{mesaj} Onaylamak için 'evet', iptal için 'hayır' yazın.")


def onayla_uygula(islem):
    if islem == "hafiza_sil":
        hafiza_kaydet([])
        arayuze("sistem", "Hafıza temizlendi.")
    elif islem == "pc_kapat":
        subprocess.Popen(["shutdown", "/s", "/t", "0"], creationflags=subprocess.CREATE_NO_WINDOW)
    elif islem == "pc_yeniden":
        subprocess.Popen(["shutdown", "/r", "/t", "0"], creationflags=subprocess.CREATE_NO_WINDOW)
    elif islem == "pc_uyku":
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    elif islem == "gunluk_sil":
        try:
            open(GUNLUK_DOSYASI, "w", encoding="utf-8").close()
            arayuze("sistem", "Konuşma günlüğü temizlendi.")
        except OSError as e:
            log.error("Günlük silinemedi: %s", e)
            arayuze("hata", "Günlük silinemedi.")


def zamanlayici_kur(m, kucuk):
    sayi = int(m.group(1))
    birim = m.group(2)
    konu = ZAMAN_RE.sub(" ", kucuk)
    konu = re.sub(r"(hatırlat\w*|haber ver\w*)", " ", konu)
    konu = " ".join(konu.split()) or "süre doldu"
    arayuze("sistem", hatirlatici_kur(sayi, birim, konu))


def hatirlatma_bildir(konu):
    arayuze("hatirlatma", konu)
    seslendir(f"Hatırlatma: {konu}")


MPCMD_YOLU = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                          "Windows Defender", "MpCmdRun.exe")
MAIL_GORULEN = set()


def bugunku_mailler():
    """Outlook gelen kutusundaki bugünkü mailler: [(id, gönderen, konu, okunmamış)]."""
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        ogeler = outlook.GetNamespace("MAPI").GetDefaultFolder(6).Items
        ogeler.Sort("[ReceivedTime]", True)
        baslangic = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
        sonuc = []
        o = ogeler.GetFirst()
        sayac = 0
        while o is not None and sayac < 150:
            sayac += 1
            try:
                zaman = o.ReceivedTime.replace(tzinfo=None)
                if zaman < baslangic:
                    break
                sonuc.append((str(o.EntryID), str(o.SenderName), str(o.Subject), bool(o.UnRead)))
            except Exception:
                pass
            o = ogeler.GetNext()
        return sonuc
    finally:
        pythoncom.CoUninitialize()


def mail_kontrol():
    try:
        mailler = bugunku_mailler()
    except ImportError:
        return "Mail kontrolü için 'pip install pywin32' gerekli."
    except Exception as e:
        log.error("Mail kontrolü başarısız: %s", e)
        return ("Outlook'a bağlanılamadı. Mail kontrolü klasik Outlook masaüstü uygulamasında çalışır; "
                "yeni Outlook uygulamasında desteklenmez.")
    if not mailler:
        return "Bugün henüz mail gelmemiş."
    okunmamis = sum(1 for m in mailler if m[3])
    satirlar = [f"- {g}: {k}" for _, g, k, _ in mailler[:5]]
    return (f"Bugün {len(mailler)} mail geldi, {okunmamis} tanesi okunmamış. "
            "Son gelenler:\n" + "\n".join(satirlar))


def mail_izleyici():
    """Arka planda yeni mail gelince bildirir. Bugünkü mailler açılışta 'görülmüş' sayılır."""
    try:
        MAIL_GORULEN.update(m[0] for m in bugunku_mailler())
    except Exception as e:
        log.error("Mail izleyici başlatılamadı: %s", e)
        return
    hata = 0
    while not durum["cik"]:
        time.sleep(120)
        if not AYAR.get("mail_bildirim", True):
            continue
        try:
            yeniler = [m for m in bugunku_mailler() if m[0] not in MAIL_GORULEN]
            hata = 0
        except Exception as e:
            hata += 1
            log.error("Mail izleyici hatası: %s", e)
            if hata >= 3:
                return
            continue
        if yeniler:
            MAIL_GORULEN.update(m[0] for m in yeniler)
            for _, g, k, _ in yeniler[:3]:
                arayuze("sistem", f"📧 Yeni mail: {g} - {k}")
            ozet = f"{len(yeniler)} yeni mail geldi. Gönderen: {yeniler[0][1]}."
            seslendir(ozet)


def _tarama_calistir(ad, tip):
    try:
        kod = subprocess.run([MPCMD_YOLU, "-Scan", "-ScanType", tip], capture_output=True,
                             creationflags=subprocess.CREATE_NO_WINDOW).returncode
    except OSError as e:
        log.error("Tarama başlatılamadı: %s", e)
        arayuze("hata", "Tarama başlatılamadı.")
        return
    if kod == 0:
        msg = f"{ad} bitti. Tehdit bulunmadı."
    elif kod == 2:
        msg = f"{ad} bitti. Tehdit bulundu! Windows Güvenliği'nden kontrol et."
    else:
        msg = f"{ad} bitti (kod {kod})."
    arayuze("sistem", msg)
    seslendir(msg)


def virus_tara(tur="hizli"):
    if not os.path.exists(MPCMD_YOLU):
        return "Windows Defender bulunamadı. Windows Güvenliği'nin açık olduğundan emin ol."
    tip = "2" if tur == "tam" else "1"
    ad = "Tam tarama" if tip == "2" else "Hızlı tarama"
    threading.Thread(target=_tarama_calistir, args=(ad, tip), daemon=True).start()
    return f"{ad} başlatıldı. Bitince haber vereceğim."


def _ps(komut):
    return subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", komut],
                          capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)


def parlaklik_oku():
    try:
        return int(_ps("(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness")
                   .stdout.strip())
    except (ValueError, OSError):
        return None


def parlaklik_ayarla(yuzde):
    y = max(0, min(100, int(yuzde)))
    r = _ps(f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{y})")
    if r.returncode == 0 and not r.stderr.strip():
        return f"Parlaklık %{y} yapıldı."
    return "Parlaklık ayarlanamadı. Bu ekran parlaklığı değiştirmeyi desteklemiyor olabilir."


def parlaklik_degistir(delta):
    mevcut = parlaklik_oku()
    if mevcut is None:
        return "Mevcut parlaklık okunamadı, yüzde söyler misin?"
    return parlaklik_ayarla(mevcut + delta)


def parlaklik_komutu(kucuk):
    n = parlaklik_niyeti(kucuk)
    if n is None or n[0] == "eksik":
        return "Parlaklık için bir yüzde ya da artır/azalt deyin."
    if n[0] == "ayarla":
        return parlaklik_ayarla(n[1])
    return parlaklik_degistir(n[1])


PC_ISLEMLERI = {"kapat": "pc_kapat", "yeniden_baslat": "pc_yeniden", "uyku": "pc_uyku"}
PC_ETIKET = {"kapat": "Bilgisayar kapatılacak.", "yeniden_baslat": "Bilgisayar yeniden başlatılacak.",
             "uyku": "Bilgisayar uyku moduna geçecek."}


def bilgisayar_islem(islem):
    if islem == "kilitle":
        ctypes.windll.user32.LockWorkStation()
        return "Ekran kilitlendi."
    if islem not in PC_ISLEMLERI:
        return "Bilinmeyen bilgisayar işlemi."
    bekle_onay(PC_ISLEMLERI[islem], PC_ETIKET[islem])
    return "Onay bekleniyor. Kullanıcıya onaylayıp onaylamadığını sor."


KLASOR_KISAYOLLARI = {
    "masaüstü": ["Desktop", "Masaüstü"], "belgeler": ["Documents", "Belgeler"],
    "indirilenler": ["Downloads", "İndirilenler"], "resimler": ["Pictures", "Resimler"],
    "videolar": ["Videos", "Videolar"], "müzik": ["Music", "Müzik"],
}


def klasor_ac(ad):
    ad = tr_kucuk(ad or "").strip()
    if not ad:
        return "Hangi klasörü açayım?"
    if os.path.isdir(ad):
        os.startfile(ad)
        return f"{ad} açıldı."
    ev = os.path.expanduser("~")
    for anahtar, adlar in KLASOR_KISAYOLLARI.items():
        if anahtar in ad or difflib.get_close_matches(ad, [anahtar], n=1, cutoff=0.6):
            for k in adlar:
                yol = os.path.join(ev, k)
                if os.path.isdir(yol):
                    os.startfile(yol)
                    return f"{k} klasörü açıldı."
    bulunan = dosya_ara(ad)
    ilk = bulunan.split("\n")[0] if bulunan else ""
    if ilk and os.path.exists(ilk):
        klasor = ilk if os.path.isdir(ilk) else os.path.dirname(ilk)
        os.startfile(klasor)
        return f"{klasor} açıldı."
    return f"'{ad}' bulunamadı."


ARAMA_SITELERI = {
    "google": "https://www.google.com/search?q=",
    "youtube": "https://www.youtube.com/results?search_query=",
    "wikipedia": "https://tr.wikipedia.org/w/index.php?search=",
    "harita": "https://www.google.com/maps/search/?api=1&query=",
}


def tarayicida_ara(sorgu, site="google"):
    sorgu = (sorgu or "").strip()
    if not sorgu:
        return "Ne arayayım?"
    taban = ARAMA_SITELERI.get(site, ARAMA_SITELERI["google"])
    webbrowser.open(taban + urllib.parse.quote_plus(sorgu))
    return f"{site} içinde '{sorgu}' aranıyor."


UYGULAMA_RE = re.compile(
    r"^(?:lütfen\s+|bana\s+)*(.+?)\s+(?:uygulamasını\s+|uygulamayı\s+|programını\s+)?"
    r"(?:aç|açar mısın|açsana|açabilir misin|açarmısın|başlat|çalıştır|gir|girsene|giriş yap|eriş|erişsene|git)$")
HAVA_RE = re.compile(r"(?:^|\s)(\S+)?\s*(?:hava|havası)\b")


def hizli_yol(kucuk):
    """Modele gitmeden, kesin ve hızlı çözülebilen istekler. İşlendiyse True."""
    g = gorsel_istegi(kucuk)
    if not g and GECMIS.get("son_gorsel") and re.search(
            r"\b(tekrar|yeniden|bir daha)\b.*\b(den\w*|dene\w*|ara\w*|bul\w*)|\b(başka|farklı) (bir )?(tane|tane daha|görsel|resim)\b", kucuk):
        g = GECMIS["son_gorsel"]
    if g:
        sorgu, adet = g
        GECMIS["son_gorsel"] = g
        arayuze("sistem", f"🔎 '{sorgu}' için görsel aranıyor…")
        sonuc = gorsel_bul(sorgu, adet)
        isaretler = GORSEL_RE.findall(sonuc)
        if isaretler:
            if GECMIS["o"]:
                GECMIS["o"]["messages"].append(
                    {"role": "assistant", "content": " ".join(f"[Görsel: {b} | {y}]" for b, y in isaretler)})
        else:
            arayuze("hata", sonuc)
        return True
    if re.search(r"\bsaat kaç\b|\bsaati söyle\b|\bbugün günlerden\b|\bbugün ayın kaçı\b|\bhangi gündeyiz\b",
                 kucuk):
        c = tarih_saat()
        arayuze("asistan", c)
        seslendir(c)
        return True
    m = UYGULAMA_RE.match(kucuk.strip())
    if m:
        hedef = re.sub(r"['’](a|e|ı|i|u|ü|ya|ye|na|ne|da|de|ta|te)$", "", m.group(1).strip())
        hedef = uygulama_adi_duzelt(hedef, list(UYGULAMALAR) + list(WEB_KISAYOLLARI))
        if hedef in WEB_KISAYOLLARI or uygulama_bul(hedef):
            c = uygulama_ac(hedef)
            arayuze("sistem", c)
            seslendir(c)
            return True
    if "hava" in kucuk and any(k in kucuk for k in ("durum", "nasıl", "kaç derece", "sıcaklık")):
        sehir = ""
        onceki = kucuk.split("hava")[0].split()
        for kelime in reversed(onceki):
            if kelime in ("bugün", "şu", "an", "şuan", "yarın", "bana", "lütfen", "ve"):
                continue
            if kelime.endswith(("den", "dan", "ten", "tan")):
                break
            sehir = re.sub(r"['’]?(da|de|ta|te)$", "", kelime) if "'" in kelime or "’" in kelime \
                else kelime
            sehir = sehir.split("'")[0].split("’")[0]
            break
        try:
            c = hava_durumu(sehir)
        except Exception as e:
            log.error("Hava hatası: %s", e)
            c = "Hava durumu alınamadı. İnternet bağlantısını kontrol edin."
        arayuze("asistan", c)
        seslendir(c)
        return True
    return False


def yerel_komut(metin):
    kucuk = tr_kucuk(metin)
    if "hafızayı göster" in kucuk or "hafızayı listele" in kucuk:
        notlar = hafiza_oku()
        arayuze("sistem", ("Kayıtlı bilgiler: " + "; ".join(notlar)) if notlar
                else "Kayıtlı bilgi yok.")
        return True
    if "hafızayı sil" in kucuk or "tüm hafızayı" in kucuk:
        bekle_onay("hafiza_sil", "Tüm hafıza silinecek.")
        return True
    if "günlüğü temizle" in kucuk:
        bekle_onay("gunluk_sil", "Konuşma günlüğü silinecek.")
        return True
    m = re.search(r"(şunu|bunu)\s+unut\s*[:,]?\s*(.*)", kucuk)
    if m:
        unut(m.group(2).strip())
        return True
    m = ZAMAN_RE.search(kucuk)
    if m and ("hatırlat" in kucuk or "haber ver" in kucuk):
        zamanlayici_kur(m, kucuk)
        return True
    if "tara" in kucuk and any(k in kucuk for k in ("virüs", "virus", "bilgisayar", "güvenlik")):
        c = virus_tara("tam" if "tam" in kucuk else "hizli")
        arayuze("sistem", c)
        seslendir(c)
        return True
    duygu = duygu_niyeti(kucuk)
    if duygu:
        c = DUYGU_YANITLARI[duygu]
        arayuze("asistan", c)
        seslendir(c)
        if duygu == "kriz":
            denetle("duygu_kriz", SERBEST, "kriz kaynağı gösterildi")
        return True
    if re.search(r"\bses(i|in|ini)?\b", kucuk) and "parlakl" not in kucuk and "sesli" not in kucuk:
        if re.search(r"\d", kucuk):
            c = "Ses seviyesini yüzde olarak ayarlayamıyorum. 'Sesi artır' ya da 'sesi azalt' diyebilirsiniz."
            arayuze("sistem", c)
            seslendir(c)
            return True
        if re.search(r"artır|arttır|yükselt|çıkar|fazlalaştır|fazla", kucuk):
            medya(VK_VOLUME_UP, "Ses artırıldı", 5)
            return True
        if re.search(r"azalt|kıs|düşür|eksilt", kucuk):
            medya(VK_VOLUME_DOWN, "Ses azaltıldı", 5)
            return True
    if re.search(r"\bses\w*", kucuk) and re.search(r"\d", kucuk) and "parlakl" not in kucuk:
        c = "Ses seviyesini yüzde olarak ayarlayamıyorum. 'Sesi artır' ya da 'sesi azalt' diyebilirsiniz."
        arayuze("sistem", c)
        seslendir(c)
        return True
    if "parlakl" in kucuk:
        c = parlaklik_komutu(kucuk)
        arayuze("sistem", c)
        seslendir(c)
        return True
    islem = bilgisayar_niyeti(kucuk)
    if islem:
        c = bilgisayar_islem(islem)
        arayuze("sistem", c)
        seslendir(c)
        return True
    if re.search(r"götür|klasör\w*\s+aç|klasörü\s+aç", kucuk):
        ad = re.sub(r"\b(götür\w*|git|gir|aç\w*|klasör\w*|dosya\w*|dizin\w*|bana|lütfen|bir|şu|o|da|de)\b",
                    " ", kucuk)
        c = klasor_ac(" ".join(ad.split()))
        arayuze("sistem", c)
        seslendir(c)
        return True
    a = arama_niyeti(kucuk)
    if a:
        sorgu, site = a
        c = tarayicida_ara(sorgu, site)
        arayuze("sistem", c)
        seslendir(c)
        return True
    if any(k in kucuk for k in ("mail", "e-posta", "eposta")) and any(
            k in kucuk for k in ("geldi", "gelmiş", "var mı", "var mi", "kontrol", "bak", "gelen", "oku")):
        c = mail_kontrol()
        arayuze("sistem", c)
        seslendir(c.split("\n")[0])
        return True
    return hizli_yol(kucuk)


def isle(metin, oturum):
    kucuk = tr_kucuk(metin).strip()
    GECMIS["o"] = oturum
    if kucuk in CIKIS:
        arayuze("sistem", "Kapatılıyor...")
        return "cik"

    gunluga_yaz(metin)
    if bekleyen["islem"]:
        islem = bekleyen["islem"]
        bekleyen["islem"] = None
        if kucuk in EVET:
            denetle(islem, ONAY, "onaylandı")
            onayla_uygula(islem)
        else:
            denetle(islem, ONAY, "iptal edildi")
            arayuze("sistem", "İşlem iptal edildi.")
        return None

    kalan = komut_isle(metin)
    if not kalan:
        return None
    if yerel_komut(kalan):
        return None
    soru_isle(oturum, kalan)
    return None


def oturumu_arayuze_aktar(oturum):
    arayuze("temizle")
    for m in oturum["messages"][1:]:
        icerik = m.get("content") or ""
        if m["role"] in ("assistant", "tool"):
            for baslik, yol in GORSEL_RE.findall(icerik):
                if os.path.exists(yol):
                    arayuze("gorsel", (yol, baslik))
            icerik = GORSEL_RE.sub("", icerik).strip()
        if m["role"] == "user":
            arayuze("kullanici", icerik)
        elif m["role"] == "assistant" and icerik:
            arayuze("asistan", icerik)
    arayuze("baslik", oturum["baslik"])


def isci_dongusu():
    oturum = oturum_yeni()
    arayuze("oturumlar", oturum_listesi())
    while not durum["cik"]:
        try:
            kaynak, veri = girdi_kuyrugu.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            if kaynak == "yeni":
                oturum = oturum_yeni()
                oturumu_arayuze_aktar(oturum)
                arayuze("sistem", "Yeni sohbet başlatıldı.")
            elif kaynak == "oturum":
                yuklenen = oturum_yukle(veri)
                if yuklenen:
                    oturum = yuklenen
                    oturumu_arayuze_aktar(oturum)
            elif kaynak == "gorsel_sor":
                soru_isle(oturum, veri.get("metin") or "Bu fotoğrafı Türkçe olarak yorumla: ne gördüğünü, nasıl bulduğunu ve varsa geliştirme önerilerini söyle.", veri.get("b64"))
            elif kaynak == "gorsel_daha":
                arayuze("sistem", gorsel_bul(veri, 5))
            elif kaynak == "sil_oturum":
                oturum_sil(veri)
                if veri == oturum["id"]:
                    oturum = oturum_yeni()
                    oturumu_arayuze_aktar(oturum)
                arayuze("oturumlar", oturum_listesi())
            elif kaynak == "disa_aktar":
                yol = os.path.join(KLASOR, f"sohbet_{oturum['id']}.txt")
                with open(yol, "w", encoding="utf-8") as f:
                    for m in oturum["messages"][1:]:
                        if m["role"] in ("user", "assistant") and (m.get("content") or "").strip():
                            f.write(f"[{'Siz' if m['role'] == 'user' else AYAR['sistem_adi']}]\n"
                                    f"{m['content']}\n\n")
                arayuze("sistem", f"Sohbet dışa aktarıldı: {yol}")
            elif isle(veri, oturum) == "cik":
                break
        except Exception as e:
            log.exception("İşlem hatası")
            arayuze("hata", f"Beklenmeyen bir hata oluştu: {e}")
    durum["cik"] = True
    konusma_kuyrugu.put(None)
    arayuze("cik")


# ---------- Arayüz ----------

MD_TOKEN = re.compile(r"(\*\*.+?\*\*|`[^`\n]+`)")
KOD_BLOK = re.compile(r"```[^\n`]*\n?(.*?)```", re.S)


# ---------- Arayüz (v3): keskin metin, balonlar metne göre boyutlanır, animasyon, arka plan ----------
import tkinter.font as tkfont

try:
    from PIL import ImageTk as PILImageTk
except ImportError:
    PILImageTk = None
_RESAMPLE = getattr(getattr(PILImage, "Resampling", PILImage), "LANCZOS", None) if PILImage else None

TEMA = {
    "arka": "#0b0d12", "ust": "#10141b", "yan": "#0e1116", "giris": "#171c25",
    "metin": "#eceff5", "soluk": "#8a93a6", "cizgi": "#222a36",
    "kullanici": "#4c6fff", "asistan_cam": "#161b24",
    "kod_arka": "#080a0f", "kod_yazi": "#d7dce7",
    "chip": "#1a202b", "chip_ustu": "#243047", "sistem_hap": "#141922",
    "hata": "#ff6b6b", "uyari": "#ffb454", "ok": "#4ade80",
    "vurgu": "#4c6fff", "vurgu2": "#7b96ff",
}


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))


def _hex(c):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in c)


def _karis(a, b, t):
    return _hex(tuple(x + (y - x) * t for x, y in zip(_rgb(a), _rgb(b))))


def _yuv_pts(x1, y1, x2, y2, r):
    r = max(0, min(r, (y2 - y1) / 2, (x2 - x1) / 2))
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


def _olcu(tuval, oid, font):
    bb = tuval.bbox(oid)
    if not bb:
        return 0, font.metrics("linespace")
    return bb[2] - bb[0], max(bb[3] - bb[1], font.metrics("linespace"))


# ---------- Pencere ve açılış sesi (Windows) ----------

def karanlik_pencere(hwnd):
    """Windows başlık çubuğunu koyu temaya alır (Win10 ve Win11)."""
    dwm = ctypes.windll.dwmapi
    koyu = ctypes.c_int(1)
    dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(koyu), ctypes.sizeof(koyu))
    dwm.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(koyu), ctypes.sizeof(koyu))
    renk = ctypes.c_int(0x120D0B)   # BGR: #0b0d12 (başlık arka planı, Win11)
    dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(renk), ctypes.sizeof(renk))
    yazi = ctypes.c_int(0xF5EFEC)   # BGR: #eceff5 (başlık yazısı, Win11)
    dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(yazi), ctypes.sizeof(yazi))


def acilis_sesi_cal(dosya):
    """MCI ile mp3 çalar. Dosya yoksa ya da bir hata olursa nedenini hata_gunlugu.txt'ye yazar."""
    if not os.path.exists(dosya):
        log.error("Açılış sesi dosyası bulunamadı: %s", dosya)
        return
    try:
        mci = ctypes.windll.winmm.mciSendStringW

        def cagri(komut):
            kod = mci(komut, None, 0, None)
            if kod != 0:
                buf = ctypes.create_unicode_buffer(256)
                ctypes.windll.winmm.mciGetErrorStringW(kod, buf, 256)
                log.error("Açılış sesi MCI hatası (%s): %s", komut, buf.value)
            return kod

        mci("close kamses", None, 0, None)  # önceki örnek varsa kapat, hata yoksayılır
        if cagri(f'open "{dosya}" type mpegvideo alias kamses') == 0:
            cagri("play kamses from 0")
    except (AttributeError, OSError) as e:
        log.error("Açılış sesi çalınamadı: %s", e)


class Arayuz:
    ONERILER = [
        ("🌤  Yozgat hava durumu", "yozgat hava durumu"),
        ("🖼  Bir kedi resmi bul", "kedi resmi bul"),
        ("🖥  Sistem durumum", "sistem durumumu söyle"),
        ("🧮  125 çarpı 48 kaç eder?", "125*48 kaç eder?"),
    ]
    ARAC_ADLARI = {
        "web_ara": "internette aranıyor", "gorsel_bul": "görsel aranıyor",
        "hava_durumu": "hava durumu alınıyor", "sayfa_oku": "sayfa okunuyor",
        "uygulama_ac": "uygulama açılıyor", "hesapla": "hesaplanıyor",
        "dosya_ara": "dosyalar aranıyor", "sistem_bilgisi": "sistem bilgisi alınıyor",
        "ekran_goruntusu": "ekran görüntüsü alınıyor", "not_al": "not alınıyor",
    }
    KENAR = 28
    PAD = 16

    def __init__(self, kok):
        self.kok = kok
        self.son_yanit = ""
        self.oturum_idleri, self.tum_oturumlar = [], []
        self.mesajlar = []
        self.icerik_h = 0
        self.takip = True
        self.akis_aktif = False
        self.uretiyor = False
        self.nokta = 0
        self.yenile_job = None
        self.ciz_job = None
        self.anim_bekle = None
        self.karsilama_dugmeleri = []
        self.arka_ham = None
        self.arka_ref = None
        self.f_metin = tkfont.Font(family="Segoe UI", size=11)
        self.f_kucuk = tkfont.Font(family="Segoe UI", size=9)
        self.f_satir = tkfont.Font(family="Segoe UI", size=10)
        self.f_kod = tkfont.Font(family="Consolas", size=10)

        kok.title(f"{AYAR['sistem_adi']} - Yapay Zeka Asistanı")
        try:
            kok.iconbitmap(os.path.join(KLASOR, "kam.ico"))
        except tk.TclError:
            pass
        kok.geometry("1180x760")
        kok.minsize(900, 580)
        kok.configure(bg=TEMA["arka"])
        kok.protocol("WM_DELETE_WINDOW", self.kapat)
        self.arka_ham_yukle()
        self.stil()
        self.yan_panel_kur()
        self.ana_panel_kur()
        for a in ("ollama", "mic", "sesli", "dinliyor", "uretiyor"):
            self.durum_guncelle(a, durum.get(a, False))
        kok.bind("<Control-n>", lambda e: girdi_kuyrugu.put(("yeni", None)))
        kok.bind("<Control-l>", lambda e: self.giris.focus_set())
        kok.bind("<Escape>", lambda e: self.gonder_veya_durdur() if durum["uretiyor"] else None)
        self.giris.focus_set()
        kok.after(80, self._yenile)
        kok.after(150, self._pencere_stil)
        kok.after(60, self.poll)
        kok.after(260, self.animasyon)

    # --- Kurulum ---
    def _pencere_stil(self):
        try:
            karanlik_pencere(ctypes.windll.user32.GetParent(self.kok.winfo_id()))
        except (AttributeError, OSError) as e:
            log.error("Pencere stili uygulanamadı: %s", e)
        if AYAR.get("acilis_sesi", True):
            acilis_sesi_cal(os.path.join(KLASOR, "hosgeldin.mp3"))

    def _logo_yukle(self, boyut):
        yol = os.path.join(KLASOR, "logo.png")
        if not (PILImage and PILImageTk and os.path.exists(yol)):
            return None
        try:
            im = PILImage.open(yol).convert("RGBA")
            im.thumbnail((boyut, boyut))
            self.logo_ref = PILImageTk.PhotoImage(im)
            return self.logo_ref
        except Exception as e:
            log.error("Logo yüklenemedi: %s", e)
            return None

    def stil(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("TCombobox", fieldbackground=TEMA["giris"], background=TEMA["giris"],
                    foreground=TEMA["metin"], arrowcolor=TEMA["metin"], bordercolor=TEMA["cizgi"])
        s.map("TCombobox", fieldbackground=[("readonly", TEMA["giris"])],
              foreground=[("readonly", TEMA["metin"])])

    def hover(self, b, normal, ustu):
        b.bind("<Enter>", lambda e: b.config(bg=ustu))
        b.bind("<Leave>", lambda e: b.config(bg=normal))

    def dugme(self, ebeveyn, metin, komut, yan="left"):
        b = tk.Button(ebeveyn, text=metin, command=komut, bg=TEMA["chip"], fg=TEMA["metin"],
                      relief="flat", bd=0, padx=12, pady=6, cursor="hand2",
                      activebackground=TEMA["chip_ustu"], activeforeground="white",
                      font=("Segoe UI", 10))
        b.pack(side=yan, padx=4)
        self.hover(b, TEMA["chip"], TEMA["chip_ustu"])
        return b

    def arka_ham_yukle(self):
        if not PILImage:
            return
        for ad in ("arka_plan.jpg", "arka_plan.png", "wallpaperflare_com_wallpaper.jpg"):
            yol = os.path.join(KLASOR, ad)
            if os.path.exists(yol):
                try:
                    self.arka_ham = PILImage.open(yol).convert("RGB")
                    return
                except Exception as e:
                    log.error("Arka plan yüklenemedi (%s): %s", yol, e)

    def yan_panel_kur(self):
        yan = tk.Frame(self.kok, bg=TEMA["yan"], width=260)
        yan.pack(side="left", fill="y")
        yan.pack_propagate(False)
        satir = tk.Frame(yan, bg=TEMA["yan"])
        satir.pack(anchor="w", padx=14, pady=(14, 0))
        logo = self._logo_yukle(46)
        if logo is not None:
            tk.Label(satir, image=logo, bg=TEMA["yan"], bd=0).pack(side="left", padx=(4, 10))
        tk.Label(satir, text=AYAR["sistem_adi"], font=("Segoe UI", 18, "bold"),
                 bg=TEMA["yan"], fg=TEMA["metin"]).pack(side="left")
        tk.Label(yan, text="Kişisel yapay zeka asistanın", font=("Segoe UI", 9),
                 bg=TEMA["yan"], fg=TEMA["soluk"]).pack(anchor="w", padx=18, pady=(0, 16))
        yeni = tk.Button(yan, text="＋  Yeni sohbet  (Ctrl+N)",
                         command=lambda: girdi_kuyrugu.put(("yeni", None)),
                         bg=TEMA["vurgu"], fg="white", relief="flat", bd=0, pady=10,
                         cursor="hand2", font=("Segoe UI", 10, "bold"),
                         activebackground=TEMA["vurgu2"], activeforeground="white")
        yeni.pack(fill="x", padx=16, pady=(0, 14))
        self.hover(yeni, TEMA["vurgu"], TEMA["vurgu2"])
        self.ara_var = tk.StringVar()
        self.ara_var.trace_add("write", lambda *a: self.liste_filtrele())
        ara = tk.Frame(yan, bg=TEMA["giris"])
        ara.pack(fill="x", padx=16, pady=(0, 12))
        tk.Label(ara, text="🔍", bg=TEMA["giris"], fg=TEMA["soluk"]).pack(side="left", padx=(10, 0))
        tk.Entry(ara, textvariable=self.ara_var, bg=TEMA["giris"], fg=TEMA["metin"],
                 insertbackground=TEMA["metin"], relief="flat", font=("Segoe UI", 10),
                 bd=0).pack(side="left", fill="x", expand=True, ipady=7, padx=8)
        tk.Label(yan, text="SOHBETLER", font=("Segoe UI", 8, "bold"), bg=TEMA["yan"],
                 fg=TEMA["soluk"]).pack(anchor="w", padx=18)
        self.liste = tk.Listbox(yan, bg=TEMA["yan"], fg=TEMA["metin"], bd=0, highlightthickness=0,
                                selectbackground=TEMA["chip_ustu"], selectforeground="white",
                                activestyle="none", font=("Segoe UI", 10), relief="flat")
        self.liste.pack(fill="both", expand=True, padx=12, pady=8)
        self.liste.bind("<<ListboxSelect>>", self.oturum_sec)
        alt = tk.Frame(yan, bg=TEMA["yan"])
        alt.pack(fill="x", padx=12, pady=(0, 14))
        self.dugme(alt, "Sil", self.oturum_sil_tikla)
        self.dugme(alt, "Dışa aktar", lambda: girdi_kuyrugu.put(("disa_aktar", None)))
        self.dugme(alt, "⚙", self.ayarlar_penceresi)

    def ana_panel_kur(self):
        ana = tk.Frame(self.kok, bg=TEMA["arka"])
        ana.pack(side="left", fill="both", expand=True)

        ust = tk.Frame(ana, bg=TEMA["ust"])
        ust.pack(fill="x")
        self.lbl_baslik = tk.Label(ust, text="Yeni sohbet", font=("Segoe UI", 12, "bold"),
                                   bg=TEMA["ust"], fg=TEMA["metin"])
        self.lbl_baslik.pack(side="left", padx=18, pady=12)
        self.model_var = tk.StringVar(value=AYAR["model"])
        self.model_kutu = ttk.Combobox(ust, textvariable=self.model_var, width=20,
                                       values=[AYAR["model"]], state="readonly")
        self.model_kutu.pack(side="right", padx=14)
        self.model_kutu.bind("<<ComboboxSelected>>", self.model_degisti)
        tk.Label(ust, text="Model:", bg=TEMA["ust"], fg=TEMA["soluk"],
                 font=("Segoe UI", 9)).pack(side="right")
        self.lbl = {}
        for a in ("dinliyor", "uretiyor", "sesli", "mic", "ollama"):
            self.lbl[a] = tk.Label(ust, text="", font=("Segoe UI", 9), bg=TEMA["ust"], fg=TEMA["soluk"])
            self.lbl[a].pack(side="right", padx=8)
        tk.Frame(ana, bg=TEMA["cizgi"], height=1).pack(fill="x")

        giris_cerceve = tk.Frame(ana, bg=TEMA["arka"])
        giris_cerceve.pack(side="bottom", fill="x", padx=22, pady=(6, 14))
        kutu = tk.Frame(giris_cerceve, bg=TEMA["giris"], highlightthickness=1,
                        highlightbackground=TEMA["cizgi"], highlightcolor=TEMA["vurgu"])
        kutu.pack(fill="x")
        self.giris = tk.Text(kutu, height=2, font=("Segoe UI", 12), bg=TEMA["giris"],
                             fg=TEMA["metin"], insertbackground=TEMA["metin"], relief="flat",
                             wrap="word", padx=14, pady=10, bd=0)
        self.giris.pack(side="left", fill="x", expand=True)
        self.giris.bind("<Return>", self.gonder)
        self.btn_gonder = tk.Button(kutu, text="Gönder ➤", command=self.gonder_veya_durdur,
                                    bg=TEMA["vurgu"], fg="white", relief="flat", bd=0, padx=20,
                                    font=("Segoe UI", 10, "bold"), cursor="hand2",
                                    activebackground=TEMA["vurgu2"], activeforeground="white")
        self.btn_gonder.pack(side="right", fill="y", padx=6, pady=6)
        tk.Label(giris_cerceve,
                 text="Enter: gönder  •  Shift+Enter: yeni satır  •  Esc: durdur  •  Ctrl+L: yazı kutusu",
                 font=("Segoe UI", 8), bg=TEMA["arka"], fg=TEMA["soluk"]).pack(anchor="w", pady=(6, 0))

        arac = tk.Frame(ana, bg=TEMA["arka"])
        arac.pack(side="bottom", fill="x", padx=20, pady=(0, 4))
        self.btn_mic = self.dugme(arac, "🎤 Mikrofon", lambda: mic_ayarla(not durum["mic"]))
        self.btn_sesli = self.dugme(arac, "🔊 Sesli cevap", lambda: sesli_ayarla(not durum["sesli"]))
        self.dugme(arac, "🖼 Görsel bul", self.gorsel_istemi)
        self.dugme(arac, "📋 Kopyala", self.son_yaniti_kopyala)
        self.dugme(arac, "🧠 Hafıza", lambda: girdi_kuyrugu.put(("yazi", "hafızayı göster")))
        self.dugme(arac, "🖥 Sistem", lambda: self.hizli_sor("sistem durumumu söyle"))
        self.dugme(arac, "⏻ Çık", self.kapat, yan="right")

        self.tuval = tk.Canvas(ana, bg=TEMA["arka"], highlightthickness=0, bd=0)
        self.tuval.pack(fill="both", expand=True)
        self.tuval.bind("<Configure>", self._boyut_degisti)
        self.tuval.bind("<MouseWheel>", lambda e: self._kaydir(-e.delta / 120 * 70))

    # --- Arka plan ve yerleşim ---
    def _arka_hazirla(self, w, h):
        if not (self.arka_ham and PILImageTk and _RESAMPLE):
            return None
        im = self.arka_ham
        olcek = max(w / im.width, h / im.height)
        yeni = im.resize((int(im.width * olcek) + 1, int(im.height * olcek) + 1), _RESAMPLE)
        sol = (yeni.width - w) // 2
        ust = (yeni.height - h) // 2
        kes = yeni.crop((sol, ust, sol + w, ust + h))
        koyu = PILImage.new("RGB", (w, h), _rgb(TEMA["arka"]))
        return PILImageTk.PhotoImage(PILImage.blend(kes, koyu, 0.45))

    def _arka_konumla(self):
        if self.tuval.find_withtag("arkaplan"):
            self.tuval.coords("arkaplan", 0, self.tuval.canvasy(0))

    def _boyut_degisti(self, _=None):
        if self.yenile_job:
            self.kok.after_cancel(self.yenile_job)
        self.yenile_job = self.kok.after(120, self._yenile)

    def _yenile(self):
        self.yenile_job = None
        W, H = self.tuval.winfo_width(), self.tuval.winfo_height()
        if W < 50 or H < 50:
            return
        self.tuval.delete("arkaplan")
        foto = self._arka_hazirla(W, H)
        if foto is not None:
            self.arka_ref = foto
            self.tuval.create_image(0, 0, image=foto, anchor="nw", tags="arkaplan")
        self._tumunu_ciz()

    def _ciz_iste(self):
        if self.ciz_job is None:
            self.ciz_job = self.kok.after(10, self._ciz_zamanli)

    def _ciz_zamanli(self):
        self.ciz_job = None
        self._tumunu_ciz()

    def _tumunu_ciz(self):
        t = self.tuval
        t.delete("mesaj")
        t.delete("karsilama")
        self._karsilama_temizle()
        W, H = max(t.winfo_width(), 200), max(t.winfo_height(), 200)
        if not self.mesajlar:
            self._karsilama_ciz(W, H)
        y = 24
        for i, m in enumerate(self.mesajlar):
            y = self._mesaj_ciz(i, m, y, W)
        self.icerik_h = y
        t.configure(scrollregion=(0, 0, W, max(H, y)))
        if self.takip:
            t.yview_moveto(1.0)
        self._arka_konumla()
        if self.anim_bekle is not None and self.anim_bekle < len(self.mesajlar):
            self._canlandir(self.anim_bekle, 0)
        self.anim_bekle = None

    def _son_yenile(self):
        if not self.mesajlar:
            return
        i = len(self.mesajlar) - 1
        m = self.mesajlar[i]
        if "y" not in m:
            self._tumunu_ciz()
            return
        self.tuval.delete(f"m{i}")
        W = max(self.tuval.winfo_width(), 200)
        H = max(self.tuval.winfo_height(), 200)
        y = self._mesaj_ciz(i, m, m["y"], W)
        self.icerik_h = y
        self.tuval.configure(scrollregion=(0, 0, W, max(H, y)))
        if self.takip:
            self.tuval.yview_moveto(1.0)
        self._arka_konumla()

    def _kaydir(self, px):
        t = self.tuval
        H = t.winfo_height()
        toplam = max(H, self.icerik_h)
        if toplam <= H:
            return
        ust = max(0, min(t.canvasy(0) + px, toplam - H))
        t.yview_moveto(ust / toplam)
        self.takip = (toplam - H - ust) < 40
        self._arka_konumla()

    def _karsilama_ciz(self, W, H):
        t = self.tuval
        cx, cy = W / 2, H * 0.34
        t.create_text(cx, cy, text="✦", font=("Segoe UI", 34), fill=TEMA["vurgu2"], tags="karsilama")
        t.create_text(cx, cy + 56, text=f"Merhaba {AYAR['kullanici_adi']}",
                      font=("Segoe UI", 22, "bold"), fill=TEMA["metin"], tags="karsilama")
        t.create_text(cx, cy + 96, text="Bugün sana nasıl yardımcı olabilirim?",
                      font=("Segoe UI", 11), fill=TEMA["soluk"], tags="karsilama")
        for k, (etiket, metin) in enumerate(self.ONERILER):
            x = cx + (-175 if k % 2 == 0 else 175)
            y = cy + 160 + (k // 2) * 60
            b = tk.Button(t, text=etiket, command=lambda m=metin: self.hizli_sor(m),
                          bg=TEMA["chip"], fg=TEMA["metin"], relief="flat", bd=0, padx=16,
                          pady=12, cursor="hand2", font=("Segoe UI", 10), width=24, anchor="w",
                          activebackground=TEMA["chip_ustu"], activeforeground="white")
            t.create_window(x, y, window=b, tags="karsilama")
            self.hover(b, TEMA["chip"], TEMA["chip_ustu"])
            self.karsilama_dugmeleri.append(b)

    def _karsilama_temizle(self):
        for b in self.karsilama_dugmeleri:
            try:
                b.destroy()
            except tk.TclError:
                pass
        self.karsilama_dugmeleri = []

    # --- Mesaj çizimi ---
    def _mesaj_ciz(self, i, m, y, W):
        tur = m["tur"]
        if tur == "yaziyor":
            return self._yazi_ciz(i, m, y, W)
        if tur in ("sistem", "hata", "hatirlatma", "arac"):
            return self._satir_ciz(i, m, y, W)
        return self._balon_ciz(i, m, y, W)

    def _satir_ciz(self, i, m, y, W):
        t = self.tuval
        etiket = (f"m{i}", "mesaj")
        renk = {"hata": TEMA["hata"], "hatirlatma": TEMA["uyari"]}.get(m["tur"], TEMA["soluk"])
        onek = {"hata": "⚠  ", "hatirlatma": "⏰  "}.get(m["tur"], "")
        oid = t.create_text(W / 2, y + 9, text=onek + m["metin"], anchor="n", font=self.f_satir,
                            fill=renk, width=W - 4 * self.KENAR, justify="center", tags=etiket)
        w, h = _olcu(t, oid, self.f_satir)
        kutu = t.create_polygon(0, 0, 0, 0, 0, 0, smooth=True, outline="",
                                fill=TEMA["sistem_hap"], tags=etiket)
        t.coords(kutu, *_yuv_pts(W / 2 - w / 2 - 16, y, W / 2 + w / 2 + 16, y + h + 18, 12))
        t.tag_raise(oid, kutu)
        m["y"] = y
        return y + h + 18 + 14

    def _yazi_ciz(self, i, m, y, W):
        t = self.tuval
        etiket = (f"m{i}", "mesaj")
        x1, y1 = self.KENAR, y
        x2, y2 = x1 + 78, y1 + 36
        t.create_polygon(*_yuv_pts(x1, y1, x2, y2, 18), smooth=True,
                         fill=TEMA["asistan_cam"], outline="", tags=etiket)
        for k in range(3):
            cx, cy = x1 + 24 + k * 15, y1 + 18
            t.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=TEMA["soluk"], outline="",
                          tags=etiket + ("yazi_nokta",))
        m["y"] = y
        return y2 + 14

    def _balon_ciz(self, i, m, y, W):
        t = self.tuval
        etiket = (f"m{i}", "mesaj")
        kullanici = m["tur"] == "kullanici"
        ad = "Siz" if kullanici else AYAR["sistem_adi"]
        zaman = m.get("zaman", "")
        t.create_text(W - self.KENAR if kullanici else self.KENAR, y,
                      text=f"{ad}  ·  {zaman}" if zaman else ad,
                      anchor="ne" if kullanici else "nw", font=self.f_kucuk,
                      fill=TEMA["soluk"], tags=etiket)
        ust = y + 22
        renk = TEMA["kullanici"] if kullanici else TEMA["asistan_cam"]
        m["renk"] = renk
        kutu = t.create_polygon(0, 0, 0, 0, 0, 0, smooth=True, outline="", fill=renk,
                                tags=etiket + (f"kutu{i}",))

        maks = max(180, int(W * (0.62 if kullanici else 0.80)) - 2 * self.PAD)
        if m["tur"] == "gorsel":
            parcalar = [("gorsel", None)]
            if m.get("metin"):
                parcalar.append(("metin", m["metin"]))
        elif kullanici:
            parcalar = [("metin", m["metin"])]
        else:
            parcalar = _yanit_parcala(m["metin"]) or [("metin", "…")]

        ogeler = []  # (tur, id, genislik, yukseklik)
        for tur_p, s in parcalar:
            if tur_p == "gorsel":
                gid = t.create_image(0, 0, image=m["foto"], anchor="nw",
                                     tags=etiket + (f"img{i}",))
                ogeler.append(("gorsel", gid, m["foto"].width(), m["foto"].height()))
            elif tur_p == "kod":
                oid = t.create_text(0, 0, text=s, anchor="nw", font=self.f_kod,
                                    fill=TEMA["kod_yazi"], width=maks - 24, tags=etiket)
                w, h = _olcu(t, oid, self.f_kod)
                ogeler.append(("kod", oid, w + 24, h))
            else:
                oid = t.create_text(0, 0, text=s, anchor="nw", font=self.f_metin,
                                    fill="#ffffff" if kullanici else TEMA["metin"],
                                    width=maks, tags=etiket)
                w, h = _olcu(t, oid, self.f_metin)
                ogeler.append(("metin", oid, w, h))

        ic_w = max([o[2] for o in ogeler] + [60])
        bw = min(ic_w, maks) + 2 * self.PAD
        if kullanici:
            bx2 = W - self.KENAR
            bx1 = bx2 - bw
        else:
            bx1 = self.KENAR
            bx2 = bx1 + bw

        cy = ust + self.PAD
        for tur_o, oid, w, h in ogeler:
            if tur_o == "kod":
                kod_y2 = cy + h + 20
                r = t.create_polygon(*_yuv_pts(bx1 + self.PAD - 6, cy, bx2 - self.PAD + 6, kod_y2, 10),
                                     smooth=True, fill=TEMA["kod_arka"], outline="", tags=etiket)
                t.tag_raise(oid, r)
                t.coords(oid, bx1 + self.PAD + 6, cy + 10)
                cy = kod_y2 + 10
            else:
                t.coords(oid, bx1 + self.PAD, cy)
                cy += h + 10
        bottom = cy - 10 + self.PAD
        t.coords(kutu, *_yuv_pts(bx1, ust, bx2, bottom, 18))

        if m["tur"] == "gorsel":
            yol = m["yol"]
            t.tag_bind(f"img{i}", "<Button-1>", lambda e, y_=yol: os.startfile(y_))
            t.tag_bind(f"img{i}", "<Enter>", lambda e: t.config(cursor="hand2"))
            t.tag_bind(f"img{i}", "<Leave>", lambda e: t.config(cursor=""))
        m["y"] = y
        return bottom + 16

    # --- Mesaj yönetimi ---
    def _mesaj_ekle(self, m):
        if self.mesajlar and self.mesajlar[-1]["tur"] == "yaziyor":
            self.mesajlar.pop()
        m.setdefault("zaman", datetime.datetime.now().strftime("%H:%M"))
        self.mesajlar.append(m)
        self.anim_bekle = len(self.mesajlar) - 1
        self.takip = True
        if self.uretiyor and not self.akis_aktif:
            self.mesajlar.append({"tur": "yaziyor"})
        self._ciz_iste()

    def _yazi_goster(self, goster):
        var = bool(self.mesajlar) and self.mesajlar[-1]["tur"] == "yaziyor"
        if goster and not var and not self.akis_aktif:
            self.mesajlar.append({"tur": "yaziyor"})
            self.takip = True
            self._ciz_iste()
        elif not goster and var:
            self.mesajlar.pop()
            self._ciz_iste()

    def _canlandir(self, i, adim):
        if i >= len(self.mesajlar):
            return
        toplam = 12
        if adim == 0:
            self.tuval.move(f"m{i}", 0, 14)
        if adim >= toplam:
            return
        e = 1 - (1 - (adim + 1) / toplam) ** 3
        onceki = 1 - (1 - adim / toplam) ** 3
        self.tuval.move(f"m{i}", 0, (onceki - e) * 14)
        self.tuval.itemconfig(f"kutu{i}", fill=_karis(TEMA["arka"],
                                                      self.mesajlar[i].get("renk", TEMA["asistan_cam"]), e))
        self.kok.after(22, lambda: self._canlandir(i, adim + 1))

    def yaz(self, etiket, metin):
        if etiket == "kullanici":
            self._mesaj_ekle({"tur": "kullanici", "metin": metin})
        elif etiket == "asistan":
            self.son_yanit = metin
            self._mesaj_ekle({"tur": "asistan", "metin": metin})
        else:
            tur = etiket if etiket in ("hata", "hatirlatma") else "sistem"
            self._mesaj_ekle({"tur": tur, "metin": metin})

    def gorsel_goster(self, yol, baslik):
        if not PILImageTk:
            self.yaz("hata", f"Görseli göstermek için 'pip install pillow' gerekli. Dosya: {yol}")
            return
        try:
            im = PILImage.open(yol)
            im.load()
            im.thumbnail((420, 320))
            foto = PILImageTk.PhotoImage(im)
        except Exception as e:
            log.error("Görsel açılamadı (%s): %s", yol, e)
            self.yaz("hata", "Görsel açılamadı.")
            return
        self._mesaj_ekle({"tur": "gorsel", "metin": f"{baslik}  •  büyütmek için tıkla",
                          "foto": foto, "yol": yol})

    # --- Akış ---
    def akis_basla(self):
        self.akis_aktif = True
        self._yazi_goster(False)
        self._mesaj_ekle({"tur": "asistan", "metin": ""})

    def akis_parca(self, parca):
        if not self.akis_aktif or not self.mesajlar:
            return
        self.mesajlar[-1]["metin"] += parca
        self._son_yenile()

    def akis_bitti(self, tam):
        if not self.akis_aktif or not self.mesajlar:
            return
        self.mesajlar[-1]["metin"] = tam
        self.son_yanit = tam
        self.akis_aktif = False
        self._son_yenile()

    def akis_iptal(self):
        self.akis_aktif = False

    # --- Durum ---
    def durum_guncelle(self, anahtar, deger):
        if anahtar == "ollama":
            self.lbl["ollama"].config(text=f"● Ollama {'bağlı' if deger else 'yok'}",
                                      fg=TEMA["ok"] if deger else TEMA["hata"])
        elif anahtar == "mic":
            self.lbl["mic"].config(text=f"● Mik. {'açık' if deger else 'kapalı'}",
                                   fg=TEMA["ok"] if deger else TEMA["soluk"])
            self.btn_mic.config(text="🎤 Mikrofonu kapat" if deger else "🎤 Mikrofon")
        elif anahtar == "sesli":
            self.lbl["sesli"].config(text=f"● Ses {'açık' if deger else 'kapalı'}",
                                     fg=TEMA["ok"] if deger else TEMA["soluk"])
            self.btn_sesli.config(text="🔊 Sesli cevap: açık" if deger else "🔇 Sesli cevap: kapalı")
        elif anahtar == "dinliyor":
            self.lbl["dinliyor"].config(text="● DİNLİYOR" if deger else "", fg=TEMA["hata"])
        elif anahtar == "uretiyor":
            self.uretiyor = bool(deger)
            self.lbl["uretiyor"].config(text="● Yazıyor" if deger else "", fg=TEMA["uyari"])
            self.btn_gonder.config(text="■ Durdur" if deger else "Gönder ➤",
                                   bg=TEMA["hata"] if deger else TEMA["vurgu"])
            self._yazi_goster(bool(deger))

    def animasyon(self):
        self.nokta = (self.nokta + 1) % 4
        if durum["uretiyor"]:
            self.lbl["uretiyor"].config(text="● Yazıyor" + "." * self.nokta)
        try:
            for k, oid in enumerate(self.tuval.find_withtag("yazi_nokta")):
                self.tuval.itemconfig(oid, fill=TEMA["vurgu2"] if k == self.nokta % 3 else TEMA["soluk"])
        except tk.TclError:
            pass
        if not durum["cik"]:
            self.kok.after(260, self.animasyon)

    def poll(self):
        while True:
            try:
                etiket, veri = ui_kuyrugu.get_nowait()
            except queue.Empty:
                break
            if etiket == "cik":
                self.kok.destroy()
                return
            elif etiket == "durum":
                self.durum_guncelle(*veri)
            elif etiket == "akis_basla":
                self.akis_basla()
            elif etiket == "akis_parca":
                self.akis_parca(veri)
            elif etiket == "akis_bitti":
                self.akis_bitti(veri)
            elif etiket == "akis_iptal":
                self.akis_iptal()
            elif etiket == "gorsel":
                self.gorsel_goster(*veri)
            elif etiket == "temizle":
                self.mesajlar = []
                self.akis_aktif = False
                self.anim_bekle = None
                self.takip = True
                self._ciz_iste()
            elif etiket == "baslik":
                self.lbl_baslik.config(text=veri)
            elif etiket == "oturumlar":
                self.liste_guncelle(veri)
            elif etiket == "modeller":
                self.modelleri_guncelle(veri)
            elif etiket == "pano":
                self.kok.clipboard_clear()
                self.kok.clipboard_append(veri)
            elif etiket == "arac":
                self._mesaj_ekle({"tur": "arac",
                                  "metin": self.ARAC_ADLARI.get(veri, "araç kullanılıyor: " + str(veri)) + "…"})
            else:
                self.yaz(etiket, veri)
        self.kok.after(60, self.poll)

    # --- Etkileşim ---
    def gonder(self, olay=None):
        if olay is not None and (olay.state & 0x1):  # Shift+Enter: yeni satır
            return None
        metin = self.giris.get("1.0", "end").strip()
        if metin:
            self.giris.delete("1.0", "end")
            arayuze("kullanici", metin)
            girdi_kuyrugu.put(("yazi", metin))
        return "break"

    def gonder_veya_durdur(self):
        if durum["uretiyor"]:
            durum["durdur"] = True
            sesi_sustur()
        else:
            self.gonder()

    def hizli_sor(self, metin):
        arayuze("kullanici", metin)
        girdi_kuyrugu.put(("yazi", metin))

    def gorsel_istemi(self):
        self.giris.delete("1.0", "end")
        self.giris.insert("1.0", "görsel bul: ")
        self.giris.focus_set()

    def son_yaniti_kopyala(self):
        if self.son_yanit:
            self.kok.clipboard_clear()
            self.kok.clipboard_append(self.son_yanit)
            self.yaz("sistem", "Son yanıt panoya kopyalandı.")

    def liste_guncelle(self, oturumlar):
        self.tum_oturumlar = oturumlar
        self.liste_filtrele()

    def liste_filtrele(self):
        f = tr_kucuk(self.ara_var.get().strip())
        gorunen = [(i, b) for i, b in self.tum_oturumlar if f in tr_kucuk(b)]
        self.oturum_idleri = [i for i, _ in gorunen]
        self.liste.delete(0, "end")
        for _, baslik in gorunen:
            self.liste.insert("end", "  " + baslik)

    def oturum_sec(self, _=None):
        s = self.liste.curselection()
        if s and not durum["uretiyor"]:
            girdi_kuyrugu.put(("oturum", self.oturum_idleri[s[0]]))

    def oturum_sil_tikla(self):
        s = self.liste.curselection()
        if s and messagebox.askyesno("Sohbeti sil", "Seçili sohbet silinsin mi?"):
            girdi_kuyrugu.put(("sil_oturum", self.oturum_idleri[s[0]]))

    def modelleri_guncelle(self, modeller):
        if modeller:
            self.model_kutu.config(values=modeller)

    def model_degisti(self, _=None):
        AYAR["model"] = self.model_var.get()
        ayar_kaydet(AYAR)
        self.yaz("sistem", f"Model değiştirildi: {AYAR['model']}")

    def ayarlar_penceresi(self):
        w = tk.Toplevel(self.kok)
        w.title("Ayarlar")
        w.configure(bg=TEMA["ust"])
        w.geometry("400x380")
        w.transient(self.kok)
        alanlar = [("Asistan adı", "sistem_adi"), ("Senin adın", "kullanici_adi"),
                   ("Konum adı", "konum_adi"), ("Enlem", "konum_lat"), ("Boylam", "konum_lon"),
                   ("Konuşma hızı", "konusma_hizi"), ("Mikrofon eşiği", "enerji_esigi")]
        girisler = {}
        for i, (etiket, anahtar) in enumerate(alanlar):
            tk.Label(w, text=etiket, bg=TEMA["ust"], fg=TEMA["soluk"],
                     font=("Segoe UI", 9)).grid(row=i, column=0, sticky="w", padx=14, pady=6)
            e = tk.Entry(w, bg=TEMA["giris"], fg=TEMA["metin"], insertbackground=TEMA["metin"],
                         relief="flat", font=("Segoe UI", 10))
            e.insert(0, str(AYAR[anahtar]))
            e.grid(row=i, column=1, sticky="ew", padx=14, pady=6, ipady=4)
            girisler[anahtar] = e
        w.columnconfigure(1, weight=1)
        mic_v = tk.BooleanVar(value=AYAR["mic_baslangic"])
        ses_v = tk.BooleanVar(value=AYAR["sesli_baslangic"])
        for j, (metin, var) in enumerate(((("Açılışta mikrofon açık", mic_v),
                                           ("Açılışta sesli cevap açık", ses_v)))):
            tk.Checkbutton(w, text=metin, variable=var, bg=TEMA["ust"], fg=TEMA["metin"],
                           selectcolor=TEMA["giris"], activebackground=TEMA["ust"],
                           activeforeground=TEMA["metin"]).grid(row=len(alanlar) + j, column=0,
                                                                columnspan=2, sticky="w", padx=12)

        def kaydet():
            try:
                for anahtar, e in girisler.items():
                    eski = AYAR[anahtar]
                    v = e.get().strip()
                    AYAR[anahtar] = type(eski)(v) if not isinstance(eski, str) else v
                AYAR["mic_baslangic"], AYAR["sesli_baslangic"] = mic_v.get(), ses_v.get()
            except ValueError:
                messagebox.showerror("Hata", "Sayısal alanlara geçerli bir sayı girin.")
                return
            ayar_kaydet(AYAR)
            self.yaz("sistem", "Ayarlar kaydedildi (ad/hız değişiklikleri yeniden başlatınca tam uygulanır).")
            w.destroy()

        tk.Button(w, text="Kaydet", command=kaydet, bg=TEMA["vurgu"], fg="white", relief="flat",
                  padx=20, pady=6, cursor="hand2").grid(row=len(alanlar) + 2, column=0,
                                                       columnspan=2, pady=16)

    def kapat(self):
        durum["cik"] = True
        konusma_kuyrugu.put(None)
        self.kok.destroy()



# ---------- Başlatma ----------

def arka_plan_baslat():
    """Çekirdek iş parçacıklarını başlatır. Arayüzden bağımsızdır (Tk ya da Qt ile çalışır)."""
    UYGULAMALAR.update(uygulamalari_tara())
    gunluk_eski_kayitlari_sil()
    durum["mic"] = AYAR["mic_baslangic"]
    durum["sesli"] = AYAR["sesli_baslangic"]
    for hedef in (isci_dongusu, konusma_iscisi, oynatici_iscisi, mikrofon_iscisi,
                  ollama_kontrol_iscisi, mail_izleyici):
        threading.Thread(target=hedef, daemon=True).start()


def main():
    """Eski Tkinter arayüzü (yedek). Yeni arayüz için kam_qt.py kullanın."""
    arka_plan_baslat()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    kok = tk.Tk()
    Arayuz(kok)
    kok.mainloop()


if __name__ == "__main__":
    main()