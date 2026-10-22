# DejedeMon

Aplikasi monitoring hardware dan cuaca yang berjalan di OLED/LCD TFT **240x240** (ST7789 via SPI), dibuat untuk **OpenWrt** di **Orange Pi Zero 3**. Semua pengaturan dilakukan dari halaman web LuCI, tanpa perlu edit kode.

![Uploading preview_10_tema.png…]()


## Fitur

- **Dashboard hardware**: CPU, suhu, RAM, load, uptime, jaringan LAN/WAN, jumlah klien DHCP, dan sinyal modem seluler.
- **Scene cuaca pixel-art** dengan data resmi BMKG per desa, dilengkapi Open-Meteo (tekanan udara, terasa seperti, siang/malam).
- **10 tema warna**, bisa tetap atau ganti otomatis tiap beberapa menit.
- **4 logo opening DEJEDE**: bisa dimatikan, dipilih satu, atau diputar bergantian.
- **Transisi antar scene**: iris kamera, fade, atau dissolve blok-pixel.
- **Konfigurasi hidup**: perubahan di LuCI berlaku dalam beberapa detik, tanpa restart layanan.

## Isi repositori

```
tft_monitor.py          skrip utama yang menggambar ke layar
logos/                  LOGOKU_COBA2.png dan LOGOKUMARK1.png (dipakai logo opening)
htdocs/, root/          halaman LuCI: Services > DejedeMon
install.sh              installer untuk router
```

> Nama berkas `tft_monitor.py` dan config UCI `tftmonitor` sengaja dipertahankan dari versi lama supaya instalasi lama tetap kompatibel.

## 1. Persiapan perangkat

### Kebutuhan

- Orange Pi Zero 3 dengan OpenWrt
- Layar TFT 240x240 berbasis ST7789 (SPI)
- Akses SSH ke router

### Pin yang dipakai skrip

Nilai ini ada di bagian `KONFIGURASI` pada `tft_monitor.py`. Sesuaikan dengan kabel Anda.

| Fungsi | Nilai di skrip |
|---|---|
| Perangkat SPI | `/dev/spidev1.1` (mode 3, 20 MHz) |
| GPIO chip | `gpiochip0` |
| DC | pin 73 (PC9) |
| RST | pin 70 (PC6) |
| BLK (backlight) | pin 69 (PC5) |

Pastikan SPI sudah aktif di device tree. Bila `/dev/spidev1.1` tidak ada, skrip berhenti dengan pesan "SPI belum aktif di device tree".

### Paket yang dibutuhkan

Skrip memakai Python 3, Pillow, dan library gpiod. Contoh pemasangan (nama paket bisa berbeda antar versi OpenWrt, cek dengan `opkg list | grep -i pillow`):

```sh
opkg update
opkg install python3 python3-pillow python3-gpiod
```

Font: skrip mencari `DejaVuSans.ttf` / `DejaVuSans-Bold.ttf`, atau berkas `.ttf` di folder yang sama dengan `tft_monitor.py`. Taruh satu font regular dan satu font bold di sana bila tampilan teks aneh.

## 2. Pemasangan

1. Unduh repositori ini (atau rilisnya) lalu salin ke router, misalnya ke `/tmp`:

   ```sh
   scp -r DejedeMon root@192.168.1.1:/tmp/
   ```

2. Masuk SSH dan jalankan installer:

   ```sh
   cd /tmp/DejedeMon
   sh install.sh
   ```

   Bila skrip lama Anda bukan di `/root`, `/usr/bin`, `/usr/local/bin`, `/opt`, atau `/etc`, tentukan lokasinya:

   ```sh
   sh install.sh /lokasi/tft_monitor.py
   ```

Installer akan:

- memasang halaman LuCI **Services > DejedeMon**,
- menyalin logo ke `/usr/share/dejedemon/logos`,
- memperbarui `tft_monitor.py` (cadangan lama disimpan sebagai `.bak`),
- membuat `/etc/config/tftmonitor` bila belum ada. Bila sudah ada, config **tidak ditimpa**: hanya opsi logo dan tiga tema baru yang ditambahkan,
- menghapus kontroler lama "TFT Monitor" agar menu tidak dobel,
- me-restart layanan layar bila ada init script yang namanya mengandung `tft` atau `dejede`.

Buka LuCI > **Services > DejedeMon**. Bila menu belum muncul, logout lalu login lagi.

### Menjalankan sebagai layanan

Installer tidak membuat init script karena bentuknya berbeda di tiap setup. Bila belum punya, contoh sederhana (`/etc/init.d/dejedemon`, sesuaikan path skrip):

```sh
#!/bin/sh /etc/rc.common
START=99
USE_PROCD=1

start_service() {
	procd_open_instance
	procd_set_param command python3 /root/tft_monitor.py
	procd_set_param respawn
	procd_set_param stdout 1
	procd_set_param stderr 1
	procd_close_instance
}
```

```sh
chmod +x /etc/init.d/dejedemon
/etc/init.d/dejedemon enable
/etc/init.d/dejedemon start
```

## 3. Pengaturan di LuCI

### Scene & Transisi

- **Ulangi urutan scene tiap (detik)**: dashboard tampil terus, lalu tiap periode ini layar memutar logo opening dan scene cuaca. Contoh: `60` = setiap menit.
- **Lama scene cuaca** dan **gaya transisi** (iris, fade, pixel).

### Logo Opening

| Pengaturan | Fungsi |
|---|---|
| Aktifkan logo opening | Dimatikan = scene logo dilewati sepenuhnya |
| Cara memilih logo | **Tetap** (satu logo) atau **Bergantian** tiap kemunculan |
| Logo yang dipakai | Pilihan satu logo (mode Tetap) |
| Logo yang diputar | Pilih sebagian, kosong = semua (mode Bergantian) |
| Lama logo opening | 5 sampai 30 detik |
| Warna logo mengikuti tema | Mati = logo tetap hijau asli DEJEDE |

![4 logo opening](preview_4_logo_opening.png)

| Kunci | Nama | Deskripsi |
|---|---|---|
| `cinematic` | Cinematic 3D | Pelat armor dirakit lengan robot, jadi logo 3D, krom, lalu flare lensa. Animasi prosedural, tanpa berkas gambar |
| `sticker` | Sticker Peel | Stiker DEJEDE mendarat berputar, berpendar, kilau menyapu |
| `mark` | Hand-drawn Mark | Kamera goresan tangan terbuka dari lensa, flash, tulisan tersapu masuk |
| `neon` | Neon Sign | Logo jadi papan neon berkedip, warnanya mengikuti tema aktif |

Bila berkas gambar logo tidak ditemukan, gaya itu otomatis diganti `cinematic`.

### Cuaca & Lokasi

- **Sumber data**: BMKG (data resmi per desa) + Open-Meteo, atau Open-Meteo saja.
- **Desa / Kecamatan / Kabupaten**: kode wilayah BMKG dicari otomatis dari nama (sekali, lalu disimpan). Isi kecamatan bila ada beberapa desa bernama sama.
- **Kode wilayah BMKG (opsional)**: format `35.08.01.2001`. Bila diisi, kode ini yang dipakai.
- **Lintang/bujur**: dipakai Open-Meteo dan sebagai cadangan.
- **Uji tampilan cuaca**: paksa satu kondisi untuk melihat animasinya. Kembalikan ke "Data nyata" setelah selesai.

Lokasi juga bisa diatur lewat SSH:

```sh
python3 /root/tft_monitor.py --lokasi "Tempursari, Lumajang"
python3 /root/tft_monitor.py --lokasi "Desa, Kecamatan, Kabupaten"
```

### Tema Warna

Mode **Tetap** (satu tema) atau **Ganti otomatis** (interval 1 sampai 1440 menit, berurutan atau acak). Kosongkan "Tema yang diputar" untuk memakai semua 10 tema.

| Kunci | Nama |
|---|---|
| `cyber` | Cyber Cyan |
| `emerald` | Emerald |
| `gold` | Black & Gold |
| `sunset` | Sunset |
| `crimson` | Crimson |
| `ice` | Chrome Silver |
| `violet` | Cyberpunk Neon Ungu |
| `neongreen` | Cyberpunk Neon Hijau |
| `electric` | Electric Blue |
| `mocha` | Mocha Mousse |

## 4. Pratinjau tanpa layar

Bisa dijalankan di PC atau router (butuh Pillow). Hasilnya berupa gambar PNG.

```sh
python3 tft_monitor.py --preview                    # dashboard -> preview.png
python3 tft_monitor.py --preview-theme              # semua tema -> themes_preview.png
python3 tft_monitor.py --preview-logo neon violet   # logo neon, tema ungu -> logo_preview_neon.png
python3 tft_monitor.py --preview-logo sticker       # gaya: cinematic | sticker | mark | neon
python3 tft_monitor.py --preview-weather rain       # kondisi cuaca (atau tanpa argumen = semua)
python3 tft_monitor.py --cek-cuaca                  # tes pengambilan data cuaca
```

## 5. Kustomisasi lanjutan

Semua ada di bagian atas `tft_monitor.py`:

| Variabel | Fungsi |
|---|---|
| `THEMES` | Tambah/ubah tema: `bg`, `panel`, `edge` (latar & bingkai) dan `a`-`d` (empat warna aksen). Tambahkan juga kunci dan warnanya di `THEMES` pada `settings.js` agar muncul di LuCI |
| `LOGO_STACKED` | `True` = kamera di atas, tulisan di bawah. `False` = tata letak melebar asli |
| `LOGO_TAGLINE` | Teks kecil di bawah logo mark/neon. Kosongkan (`""`) untuk menghapus |
| `LOGO_FILES` | Nama berkas gambar tiap gaya logo |
| `LOGO_DIRS` | Folder pencarian logo (`/usr/share/dejedemon/logos`, lalu folder `logos` di samping skrip) |

Untuk mengganti gambar logo, timpa berkas di `/usr/share/dejedemon/logos` dengan nama yang sama. PNG transparan bekerja paling baik. Gaya `mark` dan `neon` mengharapkan kamera berwarna hijau dan tulisan berwarna putih.

## 6. Pemecahan masalah

| Gejala | Penyebab / solusi |
|---|---|
| Layar kosong | Cek `/dev/spidev1.1` ada, pin DC/RST/BLK sesuai, dan lihat log layanan (`logread -e dejede`) |
| Pesan "SPI belum aktif di device tree" | Aktifkan SPI di device tree lalu reboot |
| Menu DejedeMon tidak muncul | Logout/login LuCI, atau `rm -rf /tmp/luci-*` lalu `/etc/init.d/rpcd restart` |
| Logo selalu Cinematic | Cek folder `/usr/share/dejedemon/logos`. Gaya gambar otomatis jatuh ke Cinematic bila berkasnya hilang |
| Cuaca tidak muncul | Cek internet router, jalankan `--cek-cuaca`, atau isi kode wilayah BMKG manual |
| Tema/logo baru tidak ikut berputar | Tambahkan ke "Tema yang diputar" / "Logo yang diputar" di LuCI |

## Lisensi dan kredit

Logo DEJEDE milik pemiliknya. Data cuaca dari [BMKG](https://api.bmkg.go.id) dan [Open-Meteo](https://open-meteo.com). Tambahkan lisensi untuk kode sesuai kebutuhan Anda (misalnya MIT).
