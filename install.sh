#!/bin/sh
# Pasang DejedeMon (LuCI + logo + skrip). Jalankan DI ROUTER dari folder ini:
#   sh install.sh                      (cari tft_monitor.py lama otomatis)
#   sh install.sh /root/tft_monitor.py (tentukan lokasi skrip sendiri)
# Config /etc/config/tftmonitor yang sudah ada tidak ditimpa: hanya opsi baru (logo & 10 tema) yang ditambahkan.
set -e
D="$(cd "$(dirname "$0")" && pwd)"
TARGET="$1"

# --- LuCI (menu: Services > DejedeMon) ---
mkdir -p /www/luci-static/resources/view/dejedemon /usr/share/luci/menu.d /usr/share/rpcd/acl.d
cp "$D/htdocs/luci-static/resources/view/dejedemon/settings.js" /www/luci-static/resources/view/dejedemon/settings.js
cp "$D/root/usr/share/luci/menu.d/luci-app-dejedemon.json" /usr/share/luci/menu.d/luci-app-dejedemon.json
cp "$D/root/usr/share/rpcd/acl.d/luci-app-dejedemon.json"  /usr/share/rpcd/acl.d/luci-app-dejedemon.json

# hapus kontroler lama "TFT Monitor" agar menu tidak dobel
rm -rf /www/luci-static/resources/view/tftmonitor
rm -f /usr/share/luci/menu.d/luci-app-tftmonitor.json /usr/share/rpcd/acl.d/luci-app-tftmonitor.json

# --- logo opening (dipakai gaya sticker / mark / neon) ---
mkdir -p /usr/share/dejedemon/logos
cp "$D"/logos/*.png /usr/share/dejedemon/logos/

# --- config ---
if [ ! -f /etc/config/tftmonitor ]; then
	cp "$D/root/etc/config/tftmonitor" /etc/config/tftmonitor
else
	for kv in logo_enabled=1 logo_mode=rotate logo_style=cinematic logo_seconds=10; do
		k="${kv%%=*}"; v="${kv#*=}"
		uci -q get tftmonitor.main.$k >/dev/null || uci set tftmonitor.main.$k="$v"
	done
	if [ -z "$(uci -q get tftmonitor.main.logo_list)" ]; then
		for k in cinematic sticker mark neon; do uci add_list tftmonitor.main.logo_list="$k"; done
	fi
	# tema baru ikut diputar bila daftar tema pernah diisi manual
	if [ -n "$(uci -q get tftmonitor.main.theme_list)" ]; then
		for k in neongreen electric mocha; do
			uci -q get tftmonitor.main.theme_list | grep -qw "$k" || uci add_list tftmonitor.main.theme_list="$k"
		done
	fi
	uci commit tftmonitor
fi

# --- skrip utama ---
if [ -z "$TARGET" ]; then
	for c in /root/tft_monitor.py /usr/bin/tft_monitor.py /usr/local/bin/tft_monitor.py /opt/tft_monitor.py /etc/tft_monitor.py; do
		[ -f "$c" ] && TARGET="$c" && break
	done
fi
if [ -n "$TARGET" ]; then
	[ -f "$TARGET" ] && cp "$TARGET" "$TARGET.bak"
	cp "$D/tft_monitor.py" "$TARGET"
	chmod +x "$TARGET" 2>/dev/null || true
	echo "Skrip diperbarui: $TARGET (cadangan lama: $TARGET.bak)"
else
	echo "tft_monitor.py lama tidak ditemukan. Salin manual: cp $D/tft_monitor.py <lokasi-skrip-Anda>"
fi

rm -f /tmp/luci-indexcache* 2>/dev/null || true
rm -rf /tmp/luci-modulecache 2>/dev/null || true
/etc/init.d/rpcd restart

# restart layanan layar bila ada (nama init script berbeda-beda)
for s in /etc/init.d/*; do
	case "$(basename "$s")" in *tft*|*dejede*) "$s" restart 2>/dev/null && echo "Layanan di-restart: $(basename "$s")";; esac
done

echo "Selesai. Buka LuCI > Services > DejedeMon (logout/login bila menu belum muncul)."
