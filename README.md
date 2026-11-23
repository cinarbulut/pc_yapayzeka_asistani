# Kam - Aşama 1: Temel yapı

Bu aşama, uygulamanın saf mantığını (yan etkisiz kısımlarını) ayrı modüllere taşır ve test eder.
Mevcut `asistan.py` bu aşamada **değiştirilmedi**; çalışan sürüm olduğu gibi duruyor.

## Yapı

```
kam/
  config.py      ayarlar.json okuma, doğrulama, sınırlar
  textutil.py    Türkçe küçük harf, cümle bölme, markdown temizliği, araç çağrısı ayıklama
  calc.py        güvenli hesap makinesi (eval kullanmaz)
  intents.py     hızlı yol niyet tanıma (parlaklık, bilgisayar, arama, zamanlayıcı)
tests/           birim testleri (unittest, ek paket gerektirmez)
```

## Testleri çalıştırma

Proje klasöründe:

```
py -3.12 -m unittest discover -s tests -t . -v
```

Şu anda 34 test var ve hepsi geçiyor.

## Bu aşamada düzeltilen hatalar

- Bozuk JSON argümanlı araç çağrısı artık çalıştırılmıyor (eskiden boş argümanla çalışıyordu).
- Tanımlı olmayan araç adları hem çalıştırılmıyor hem metinden siliniyor.

## Sonraki adım (Aşama 1b)

`asistan.py` içindeki bu fonksiyonlar yeni modüllere bağlanacak ve eski kopyaları silinecek:
`tr_kucuk`, `cumlelere_bol`, `hesapla`, `yanit_parcala`, `ARAC_JSON` ayıklama, `hizli_yol` niyet kontrolleri.
Bu adım, testler geçtikten ve sizin bilgisayarınızda uygulama başarıyla açıldıktan sonra yapılacak.
