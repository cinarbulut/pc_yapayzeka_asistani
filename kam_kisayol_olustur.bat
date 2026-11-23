@echo off
chcp 65001 >nul
title Kam kisayol kurulumu
set "KLASOR=C:\asistan"
set "KAM_DIR=%KLASOR%"
set "KAM_IKON=%KLASOR%\kam.ico"

cd /d "%KLASOR%" 2>nul || (echo Klasor bulunamadi: %KLASOR% & pause & exit /b 1)
if not exist "kam_qt.py" (echo kam_qt.py bulunamadi: %KLASOR% & pause & exit /b 1)
if not exist "kam.ico" (echo UYARI: kam.ico bulunamadi, varsayilan ikon kullanilacak. & set "KAM_IKON=%SystemRoot%\System32\shell32.dll")

rem Python 3.12 yolunu dosyaya yaz (Turkce karakterli kullanici yolu cmd'de bozulmasin)
if exist "python_yolu.txt" del /q "python_yolu.txt"
py -3.12 -c "import sys;open(r'C:\asistan\python_yolu.txt','w',encoding='utf-8').write(sys.executable)" 2>nul
if not exist "python_yolu.txt" (echo Python 3.12 bulunamadi. & pause & exit /b 1)

echo Eski kisayollar siliniyor ve yenileri olusturuluyor...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$py=(Get-Content -LiteralPath (Join-Path $env:KAM_DIR 'python_yolu.txt') -Encoding UTF8 -Raw).Trim(); $pyw=Join-Path (Split-Path $py) 'pythonw.exe'; if(-not (Test-Path -LiteralPath $pyw)){ Write-Host ('pythonw bulunamadi: '+$pyw); exit 2 }; $prog=[Environment]::GetFolderPath('Programs'); $masa=[Environment]::GetFolderPath('Desktop'); $bas=[Environment]::GetFolderPath('Startup'); foreach($d in @($prog,$masa,$bas)){ Remove-Item -LiteralPath (Join-Path $d 'Kam.lnk') -Force -ErrorAction SilentlyContinue }; $s=New-Object -ComObject WScript.Shell; foreach($d in @($prog,$masa)){ $l=$s.CreateShortcut((Join-Path $d 'Kam.lnk')); $l.TargetPath=$pyw; $l.Arguments='kam_qt.py'; $l.WorkingDirectory=$env:KAM_DIR; $l.IconLocation=$env:KAM_IKON+',0'; $l.Description='Kam Asistan'; $l.Save() }; Write-Host 'Kisayollar olusturuldu.'"
if errorlevel 1 (echo Kisayol olusturulamadi. & pause & exit /b 1)

choice /c EH /m "Bilgisayar acilinca Kam otomatik baslasin mi"
if errorlevel 2 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item -LiteralPath (Join-Path ([Environment]::GetFolderPath('Startup')) 'Kam.lnk') -Force -ErrorAction SilentlyContinue"
    echo Otomatik baslatma kaldirildi.
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$py=(Get-Content -LiteralPath (Join-Path $env:KAM_DIR 'python_yolu.txt') -Encoding UTF8 -Raw).Trim(); $pyw=Join-Path (Split-Path $py) 'pythonw.exe'; $s=New-Object -ComObject WScript.Shell; $l=$s.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'Kam.lnk')); $l.TargetPath=$pyw; $l.Arguments='kam_qt.py'; $l.WorkingDirectory=$env:KAM_DIR; $l.IconLocation=$env:KAM_IKON+',0'; $l.Save()"
    echo Otomatik baslatma eklendi.
)

echo Ikon onbellegi temizleniyor ve Gezgin yeniden baslatiliyor...
ie4uinit.exe -ClearIconCache
taskkill /f /im explorer.exe >nul 2>&1
start explorer.exe

echo.
echo Tamam. Eski kisayollar yenilendi. Masaustu ve Baslat ikonlari Kam (Qt) arayuzunu acar.
pause
