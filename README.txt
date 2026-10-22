DejedeMon - aplikasi monitoring hardware dan cuaca untuk OLED/LCD TFT 240x240
(OpenWrt, Orange Pi Zero 3 + ST7789 via SPI)

Isi paket
  tft_monitor.py     skrip layar (nama berkas tetap agar layanan lama tetap jalan)
  logos/             LOGOKU_COBA2.png (sticker) dan LOGOKUMARK1.png (goresan tangan)
  htdocs/, root/     halaman LuCI: Services > DejedeMon

Pasang di router dari folder ini:
  sh install.sh
  (atau: sh install.sh /lokasi/tft_monitor.py bila skrip lama tidak ditemukan otomatis)

Fitur
  10 tema warna : Cyber Cyan, Emerald, Black & Gold, Sunset, Crimson, Chrome Silver,
                  Cyberpunk Neon Ungu, Cyberpunk Neon Hijau, Electric Blue, Mocha Mousse
  4 logo opening: Cinematic 3D, Sticker Peel, Hand-drawn Mark, Neon Sign
                  LuCI > Logo Opening: bisa dimatikan, dipilih satu, atau diputar bergantian
                  (logo gambar dicari di /usr/share/dejedemon/logos; bila hilang otomatis pakai Cinematic)

Pratinjau tanpa layar (di PC/router, butuh Pillow):
  python3 tft_monitor.py --preview-logo neon violet   -> logo_preview_neon.png
  python3 tft_monitor.py --preview-theme              -> themes_preview.png
