#!/usr/bin/env python3
"""
DejedeMon - aplikasi monitoring hardware dan cuaca yang berjalan di OLED/LCD TFT 240x240
(OpenWrt, Orange Pi Zero 3 + ST7789 via SPI)

Urutan scene berulang tiap SCENE_PERIOD detik: dashboard -> logo opening DEJEDE (4 gaya, bisa dipilih,
diputar bergantian, atau dimatikan) -> cuaca pixel-art -> dashboard. Perpindahan scene memakai
transisi iris / fade / pixel.

Semua pengaturan (scene, logo opening, cuaca & lokasi, 10 tema warna, ganti tema otomatis) dikontrol
dari LuCI: Services > DejedeMon  (/etc/config/tftmonitor). Berkas itu dibaca ulang otomatis,
jadi tidak perlu restart layanan.
"""
import fcntl
import json
import math
import os
import random
import re
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import traceback
from collections import deque

try:
    import gpiod
except ImportError:
    gpiod = None
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# ============================ KONFIGURASI ============================
SPI_DEV = "/dev/spidev1.1"  
SPI_SPEED_HZ = 20_000_000   
SPI_MODE = 3                

GPIO_CHIP = "gpiochip0"
PIN_DC = 73                 # PC9
PIN_RST = 70                # PC6  
PIN_BLK = 69                # PC5  

WIDTH = HEIGHT = 240
ROTATION = 0                
INTERVAL = 2.0              # Detik antar refresh

LAN_IF = "br-lan"           
WAN_IF = None               
LEASE_FILE = "/tmp/dhcp.leases"

MODEM_CMD = ["modeminfo"]
MODEM_INTERVAL = 10         
CELL_PREFIX = ("wwan", "usb", "rmnet", "ppp", "qmi", "3g", "4g")

PILL_STATIC = False

# --- Scene 2 (cuaca) & transisi ---
SCENE_PERIOD = 60           # detik: urutan scene diulang tiap periode (awal menit)
SCENE_ORDER = ("logo", "cuaca")                 # urutan setelah dashboard; hapus salah satu untuk menonaktifkan
SCENE_LEN = {"logo": 10.0, "cuaca": 15.0}        # detik tampil penuh (di luar transisi)
TRANS = 1.2                 # detik: durasi tiap transisi (diatur LuCI sesuai gaya)
TRANS_BLOCK = 8             # ukuran blok pixel transisi (harus membagi 240)
FAST_INTERVAL = 0.05        # jeda antar frame saat animasi (detik)

WEATHER_SOURCE = "bmkg"     # "bmkg" (utama, data resmi) atau "openmeteo" (saja)
WEATHER_PLACE = ("Tempursari", "Lumajang")   # (desa, kabupaten) atau (desa, kecamatan, kabupaten) -> kode adm4 dicari otomatis
WEATHER_ADM4 = ""           # isi kode BMKG (mis. "35.08.01.2001") kalau ingin menetapkan manual
WEATHER_NAME = "LUMAJANG"   # label cadangan bila hanya Open-Meteo
WEATHER_LAT = -8.1337       # koordinat cadangan (Open-Meteo: tekanan udara, terasa seperti, dll)
WEATHER_LON = 113.2225
LOCATION_FILE = "/etc/tft_lokasi.json"          # ubah lokasi tanpa edit kode (dibaca ulang tiap update)
LOCATION_CACHE = "/etc/tft_lokasi_cache.json"   # hasil pencarian kode desa (otomatis)
BMKG_URL = "https://api.bmkg.go.id/publik/prakiraan-cuaca?adm4={adm4}"
WILAYAH_URL = "https://raw.githubusercontent.com/cahyadsn/wilayah/master/db/wilayah.sql"   # daftar kode wilayah (hanya dibaca saat mencari kode)
WEATHER_INTERVAL = 600      # detik antar update cuaca
WEATHER_RETRY = 60          # detik antar percobaan ulang jika gagal
WEATHER_STALE = 1800        # data lebih tua dari ini dianggap kedaluwarsa (titik oranye)
WEATHER_FORCE = None        # uji tampilan: "clear","partly","cloudy","fog","drizzle","rain","heavy","storm","snow"
WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               "&current=temperature_2m,relative_humidity_2m,apparent_temperature,is_day,"
               "weather_code,pressure_msl,wind_speed_10m"
               "&daily=temperature_2m_max,temperature_2m_min&timezone=auto&forecast_days=1")

# =====================================================================

class Gpio:
    def __init__(self, chip, offsets):
        offsets = [o for o in offsets if o is not None]
        self.v2 = hasattr(gpiod, "request_lines")
        if self.v2:
            from gpiod.line import Direction, Value
            self._Value = Value
            cfg = {o: gpiod.LineSettings(direction=Direction.OUTPUT,
                                         output_value=Value.INACTIVE) for o in offsets}
            self.req = gpiod.request_lines("/dev/" + chip, consumer="tft_monitor", config=cfg)
        else:
            self.chip = gpiod.Chip(chip)
            self.lines = {}
            for o in offsets:
                line = self.chip.get_line(o)
                line.request(consumer="tft_monitor", type=gpiod.LINE_REQ_DIR_OUT, default_val=0)
                self.lines[o] = line

    def set(self, offset, value):
        if offset is None:
            return
        if self.v2:
            self.req.set_value(offset, self._Value.ACTIVE if value else self._Value.INACTIVE)
        else:
            self.lines[offset].set_value(1 if value else 0)

    def close(self):
        if self.v2:
            self.req.release()
        else:
            for line in self.lines.values():
                line.release()


SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04

ROTATIONS = {
    0: (0x00, 0, 0),
    90: (0x60, 0, 0),
    180: (0xC0, 0, 80),
    270: (0xA0, 80, 0),
}


class ST7789:
    def __init__(self, spi_dev, gpio, dc, rst, blk, rotation=0):
        self.gpio, self.dc, self.rst, self.blk = gpio, dc, rst, blk
        self.fd = os.open(spi_dev, os.O_WRONLY)
        fcntl.ioctl(self.fd, SPI_IOC_WR_MODE, struct.pack("B", SPI_MODE))
        fcntl.ioctl(self.fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
        fcntl.ioctl(self.fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", SPI_SPEED_HZ))
        try:
            with open("/sys/module/spidev/parameters/bufsiz") as f:
                self.chunk = int(f.read())
        except Exception:
            self.chunk = 4096
        self.madctl, self.xoff, self.yoff = ROTATIONS[rotation]

    def _write(self, data):
        mv = memoryview(data)
        for i in range(0, len(mv), self.chunk):
            os.write(self.fd, mv[i:i + self.chunk])

    def cmd(self, c, data=None):
        self.gpio.set(self.dc, 0)
        self._write(bytes([c]))
        if data:
            self.gpio.set(self.dc, 1)
            self._write(bytes(data))

    def init(self):
        if self.rst is not None:
            self.gpio.set(self.rst, 1)
            time.sleep(0.01)
            self.gpio.set(self.rst, 0)
            time.sleep(0.02)
            self.gpio.set(self.rst, 1)
            time.sleep(0.15)
        self.cmd(0x01)                      
        time.sleep(0.15)
        self.cmd(0x11)                      
        time.sleep(0.12)
        self.cmd(0x3A, [0x55])              
        self.cmd(0x36, [self.madctl])       
        self.cmd(0xB2, [0x0C, 0x0C, 0x00, 0x33, 0x33])  
        self.cmd(0xB7, [0x35])              
        self.cmd(0xBB, [0x19])              
        self.cmd(0xC0, [0x2C])
        self.cmd(0xC2, [0x01])
        self.cmd(0xC3, [0x12])
        self.cmd(0xC4, [0x20])
        self.cmd(0xC6, [0x0F])              
        self.cmd(0xD0, [0xA4, 0xA1])        
        self.cmd(0x21)                      
        self.cmd(0x13)                      
        self.cmd(0x29)                      
        time.sleep(0.05)
        self.backlight(True)

    def backlight(self, on):
        self.gpio.set(self.blk, 1 if on else 0)

    def _window(self):
        x0, x1 = self.xoff, self.xoff + WIDTH - 1
        y0, y1 = self.yoff, self.yoff + HEIGHT - 1
        self.cmd(0x2A, [x0 >> 8, x0 & 0xFF, x1 >> 8, x1 & 0xFF])
        self.cmd(0x2B, [y0 >> 8, y0 & 0xFF, y1 >> 8, y1 & 0xFF])
        self.cmd(0x2C)

    _T_R = bytes(v & 0xF8 for v in range(256))
    _T_GH = bytes(v >> 5 for v in range(256))
    _T_GL = bytes((v & 0x1C) << 3 for v in range(256))
    _T_B = bytes(v >> 3 for v in range(256))

    def _rgb565(self, image):
        # Seluruh operasi di level C (translate + OR integer besar): ~1 ms per frame
        rgb = image.tobytes()
        n = WIDTH * HEIGHT
        r, g, b = rgb[0::3], rgb[1::3], rgb[2::3]
        fb = int.from_bytes
        hi = (fb(r.translate(self._T_R), "big") | fb(g.translate(self._T_GH), "big")).to_bytes(n, "big")
        lo = (fb(g.translate(self._T_GL), "big") | fb(b.translate(self._T_B), "big")).to_bytes(n, "big")
        out = bytearray(n * 2)
        out[0::2] = hi
        out[1::2] = lo
        return out

    def show(self, image):
        out = self._rgb565(image)
        self._window()
        self.gpio.set(self.dc, 1)
        self._write(out)

    def close(self):
        os.close(self.fd)


def read_cpu():
    with open("/proc/stat") as f:
        vals = list(map(int, f.readline().split()[1:9]))
    return sum(vals), vals[3] + vals[4]      


def read_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":")
            info[k] = int(v.split()[0])      
    total = info["MemTotal"]
    avail = info.get("MemAvailable",
                     info.get("MemFree", 0) + info.get("Buffers", 0) + info.get("Cached", 0))
    return total // 1024, (total - avail) // 1024


def read_temp():
    for z in range(4):
        try:
            with open(f"/sys/class/thermal/thermal_zone{z}/temp") as f:
                return int(f.read()) / 1000.0
        except Exception:
            continue
    return None


def read_loadavg():
    with open("/proc/loadavg") as f:
        return f.read().split()[0]


def read_uptime():
    with open("/proc/uptime") as f:
        s = int(float(f.read().split()[0]))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{d}d {h:02d}:{m:02d}" if d else f"{h:02d}:{m:02d}:{s:02d}"


def read_net(ifname):
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                if line.strip().startswith(ifname + ":"):
                    p = line.split(":")[1].split()
                    return int(p[0]), int(p[8])      
    except Exception:
        pass
    return None


def get_ip(ifname):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        r = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", ifname[:15].encode()))
        return socket.inet_ntoa(r[20:24])
    except OSError:
        return "N/A"
    finally:
        s.close()


def default_iface():
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                p = line.split()
                if p[1] == "00000000" and int(p[3], 16) & 2:
                    return p[0]
    except Exception:
        pass
    return None


def read_leases():
    out = []
    try:
        with open(LEASE_FILE) as f:
            for line in f:
                p = line.split()
                if len(p) >= 4:
                    name = p[3] if p[3] != "*" else p[1][-8:].upper()
                    out.append((p[2], name))
    except Exception:
        pass
    out.sort(key=lambda e: tuple(int(x) if x.isdigit() else 0 for x in e[0].split(".")))
    return out


def fmt_mbs(bps):
    return f"{bps / 1048576:.1f}MB/s"


def fmt_mbit(bps):
    return f"{bps * 8 / 1e6:.1f}Mbps"


class Modem:
    def __init__(self):
        self.data = {"rssi": None, "label": "RSSI", "gen": "4G"}
        self._stop = False

    def start(self):
        if MODEM_CMD:
            threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop = True

    @staticmethod
    def _find(obj, names):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).lower() in names and not isinstance(v, (dict, list)):
                    return v
            for v in obj.values():
                r = Modem._find(v, names)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = Modem._find(v, names)
                if r is not None:
                    return r
        return None

    @staticmethod
    def _num(v):
        try:
            m = re.search(r"-?\d+(\.\d+)?", str(v))
            return float(m.group()) if m else None
        except Exception:
            return None

    @staticmethod
    def _gen(mode):
        m = str(mode).upper()
        if "NR" in m or "5G" in m:
            return "5G"
        if "LTE" in m or "4G" in m:
            return "4G"
        if any(x in m for x in ("WCDMA", "UMTS", "HSPA", "3G")):
            return "3G"
        if any(x in m for x in ("GSM", "EDGE", "GPRS", "2G")):
            return "2G"
        return "4G"

    def _read(self):
        r = subprocess.run(MODEM_CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=8, universal_newlines=True)
        txt = r.stdout
        rssi = mode = rsrp = None
        try:
            j = json.loads(txt)
            rssi = self._num(self._find(j, {"rssi"}))
            rsrp = self._num(self._find(j, {"rsrp"}))
            mode = self._find(j, {"mode", "network_type", "rat"})
        except ValueError:                      
            m = re.search(r"rssi\D{0,8}(-?\d+)", txt, re.I)
            rssi = float(m.group(1)) if m else None
            m = re.search(r"rsrp\D{0,8}(-?\d+)", txt, re.I)
            rsrp = float(m.group(1)) if m else None
            m = re.search(r"\b(LTE|NR5G|5G|WCDMA|UMTS|HSPA|GSM|EDGE)\b", txt, re.I)
            mode = m.group(1) if m else None
        if rssi is not None and rssi < 0:
            return {"rssi": rssi, "label": "RSSI", "gen": self._gen(mode)}
        if rsrp is not None and rsrp < 0:
            return {"rssi": rsrp, "label": "RSRP", "gen": self._gen(mode)}
        return None

    def _loop(self):
        while not self._stop:
            try:
                d = self._read()
                if d:
                    self.data = d
            except Exception:
                pass
            for _ in range(MODEM_INTERVAL * 10):
                if self._stop:
                    return
                time.sleep(0.1)


def signal_bars(dbm):
    if dbm is None:
        return 0
    for lvl, thr in ((5, -65), (4, -80), (3, -90), (2, -100), (1, -110)):
        if dbm >= thr:
            return lvl
    return 0


FONT_DIRS = [
    os.path.dirname(os.path.abspath(__file__)) + "/",
    "/usr/share/fonts/ttf/",
    "/usr/share/fonts/truetype/dejavu/",
    "/usr/share/fonts/",
]


def _find_font(name):
    for d in FONT_DIRS:
        if os.path.exists(d + name):
            return d + name
    return None


FONT_BOLD_EXTRA = True      
def _local_fonts():
    base = os.path.dirname(os.path.abspath(__file__))
    try:
        names = sorted(n for n in os.listdir(base) if n.lower().endswith((".ttf", ".otf")))
    except OSError:
        names = []
    heavy = ("bold", "black", "heavy", "extrabold", "semibold")
    bold = [os.path.join(base, n) for n in names if any(h in n.lower() for h in heavy)]
    reg = [os.path.join(base, n) for n in names if not any(h in n.lower() for h in heavy)]
    return (reg[0] if reg else None), (bold[0] if bold else None)


_loc_reg, _loc_bold = _local_fonts()
FONT_REG = _find_font("regular.ttf") or _loc_reg or _find_font("DejaVuSans.ttf")
FONT_BOLD = _find_font("bold.ttf") or _loc_bold or _find_font("DejaVuSans-Bold.ttf")
if FONT_REG is None:                         
    for root in ("/usr/share/fonts", "/usr/lib/fonts"):
        for dp, _dn, fn in os.walk(root):
            for f in fn:
                if f.lower().endswith(".ttf") and "bold" not in f.lower():
                    FONT_REG = os.path.join(dp, f)
                    break
            if FONT_REG:
                break
        if FONT_REG:
            break

if FONT_REG is None and FONT_BOLD:
    FONT_REG = FONT_BOLD
_fonts = {}
_scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
_canvas = None          


def get_font(size, bold=False):
    key = (size, bold)
    if key in _fonts:
        return _fonts[key]
    res = None
    try:
        if bold and FONT_BOLD:
            res = (ImageFont.truetype(FONT_BOLD, size), FONT_BOLD_EXTRA, 0.58 * size, 1.0)
        elif FONT_REG:
            res = (ImageFont.truetype(FONT_REG, size), bold, 0.58 * size, 1.0)
    except Exception:
        res = None
    if res is None:
        k = size / 10.0
        res = (ImageFont.load_default(), bold, 5.5 * k, k)
    _fonts[key] = res
    return res


def twidth(s, size, bold=False):
    f, faux, _, k = get_font(size, bold)
    return _scratch.textlength(s, font=f) * k + (1 if faux else 0)


def text(d, xy, s, size, fill, bold=False, anchor="la"):
    f, faux, half, k = get_font(size, bold)
    x, y = xy
    w = _scratch.textlength(s, font=f) * k
    if anchor[0] == "r":
        x -= w
    elif anchor[0] == "m":
        x -= w / 2
    if len(anchor) > 1 and anchor[1] == "m":
        y -= half
    if k == 1.0 or _canvas is None:
        d.text((x, y), s, font=f, fill=fill)
        if faux:
            d.text((x + 1, y), s, font=f, fill=fill)
        return
    bw, bh = int(_scratch.textlength(s, font=f)) + 2, 14
    tmp = Image.new("L", (bw, bh), 0)
    td = ImageDraw.Draw(tmp)
    td.text((0, 0), s, font=f, fill=255)
    if faux:
        td.text((1, 0), s, font=f, fill=255)
    new = (max(1, int(round(bw * k))), max(1, int(round(bh * k))))
    mask = tmp.resize(new, Image.NEAREST if k >= 1.9 else Image.BILINEAR)
    _canvas.paste(fill, (int(x), int(y)), mask)


def fit(s, maxw, size, bold=False, minsize=6):
    while size > minsize and twidth(s, size, bold) > maxw:
        size -= 1
    return size


def ellipsize(s, maxw, size, bold=False):
    if twidth(s, size, bold) <= maxw:
        return s
    while s and twidth(s + "..", size, bold) > maxw:
        s = s[:-1]
    return s + ".."


# ============================ TEMA WARNA ============================
# Tiap tema: bg/panel/edge = latar & bingkai, a/b/c/d = empat warna aksen.
# Tema aktif dipilih lewat LuCI (/etc/config/tftmonitor) - tetap atau ganti otomatis tiap N menit.
THEMES = {
    # --- palet dasar ---
    "cyber":   {"label": "Cyber Cyan",         "bg": (3, 9, 16),   "panel": (8, 26, 36),  "edge": (28, 78, 92),
                "a": (0, 230, 255),   "b": (255, 140, 30),  "c": (120, 255, 60),  "d": (50, 130, 255)},
    "emerald": {"label": "Emerald",            "bg": (2, 11, 7),   "panel": (6, 29, 19),  "edge": (22, 92, 58),
                "a": (45, 235, 125),  "b": (255, 196, 64),  "c": (170, 255, 90),  "d": (40, 180, 170)},
    "gold":    {"label": "Black & Gold",       "bg": (8, 6, 3),    "panel": (24, 19, 10), "edge": (104, 84, 36),
                "a": (236, 194, 96),  "b": (255, 150, 60),  "c": (255, 232, 160), "d": (190, 150, 70)},
    "sunset":  {"label": "Sunset",             "bg": (15, 6, 4),   "panel": (36, 16, 10), "edge": (112, 54, 28),
                "a": (255, 152, 56),  "b": (255, 84, 120),  "c": (255, 214, 96),  "d": (255, 110, 70)},
    "crimson": {"label": "Crimson",            "bg": (14, 3, 5),   "panel": (36, 10, 15), "edge": (116, 36, 46),
                "a": (255, 72, 84),   "b": (255, 176, 64),  "c": (255, 130, 170), "d": (255, 100, 100)},
    "ice":     {"label": "Chrome Silver",      "bg": (5, 10, 16),  "panel": (15, 24, 35), "edge": (72, 94, 118),
                "a": (196, 220, 244), "b": (120, 176, 236), "c": (232, 242, 252), "d": (128, 164, 214)},
    # --- cyberpunk neon ---
    "violet":  {"label": "Cyberpunk Neon Ungu", "bg": (9, 3, 20),  "panel": (27, 9, 52),  "edge": (112, 38, 172),
                "a": (191, 64, 255),  "b": (255, 43, 180),  "c": (0, 235, 255),   "d": (140, 70, 255)},
    "neongreen": {"label": "Cyberpunk Neon Hijau", "bg": (2, 10, 4), "panel": (5, 29, 12), "edge": (18, 112, 40),
                "a": (57, 255, 20),   "b": (255, 232, 0),   "c": (0, 255, 200),   "d": (0, 200, 90)},
    # --- warna tren sekarang ---
    "electric": {"label": "Electric Blue",     "bg": (2, 6, 20),   "panel": (8, 20, 54),  "edge": (30, 72, 156),
                "a": (46, 125, 255),  "b": (255, 120, 60),  "c": (90, 224, 255),  "d": (112, 92, 255)},
    "mocha":   {"label": "Mocha Mousse",       "bg": (14, 9, 7),   "panel": (35, 24, 18), "edge": (112, 82, 64),
                "a": (208, 164, 128), "b": (242, 204, 142), "c": (244, 228, 208), "d": (170, 124, 98)},
}
THEME_KEY = None
WHITE = (245, 245, 245)
GREY = (150, 160, 165)


def _mix(c1, c2, k):
    return tuple(int(a + (b - a) * k) for a, b in zip(c1, c2))


def apply_theme(key):
    """Terapkan tema: semua warna dashboard/cuaca/logo dibaca ulang dari variabel global ini."""
    global BG, PANEL, EDGE, CYAN, ORANGE, GREEN, BLUE, DIM, THEME_KEY
    global LINE, LINE_DOT, PILL_FILL, PILL_EDGE, CPU_OUT, CPU_FILL, RAM_FILL, RAM_BAR, RAM_ROWS, GLASS
    t = THEMES.get(key) or THEMES["cyber"]
    THEME_KEY = key if key in THEMES else "cyber"
    BG, PANEL, EDGE = t["bg"], t["panel"], t["edge"]
    CYAN, ORANGE, GREEN, BLUE = t["a"], t["b"], t["c"], t["d"]
    DIM = _mix(BG, EDGE, 0.55)
    LINE = _mix(BG, EDGE, 0.40)
    LINE_DOT = _mix(BG, EDGE, 0.80)
    PILL_FILL = _mix(BG, BLUE, 0.17)
    PILL_EDGE = BLUE
    CPU_OUT = scale(GREEN, 0.75)
    CPU_FILL = _mix(BG, GREEN, 0.07)
    RAM_FILL = _mix(BG, BLUE, 0.13)
    RAM_BAR = scale(BLUE, 0.82)
    RAM_ROWS = (BLUE, scale(BLUE, 0.9), _mix(BLUE, CYAN, 0.6))
    GLASS = scale(BG, 0.5)


def color_for(pct):
    if pct < 60:
        return (110, 240, 50)
    if pct < 85:
        return (255, 190, 0)
    return (255, 60, 60)


def rrect(d, box, radius=0, **kw):
    try:
        d.rounded_rectangle(box, radius=radius, **kw)
    except Exception:
        kw.pop("width", None)
        d.rectangle(box, **kw)


def scale(c, k):
    return tuple(int(v * k) for v in c)


apply_theme("cyber")


def panel(d, box):
    rrect(d, box, radius=6, fill=PANEL, outline=EDGE)


def dot(d, cx, cy, color, r=5):
    d.ellipse((cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2), fill=scale(color, 0.25))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)


def arrow(d, x, y, up, color):
    if up:
        d.polygon([(x + 4, y), (x + 9, y + 6), (x + 5.5, y + 6), (x + 5.5, y + 10),
                   (x + 2.5, y + 10), (x + 2.5, y + 6), (x - 1, y + 6)], fill=color)
    else:
        d.polygon([(x + 4, y + 10), (x + 9, y + 4), (x + 5.5, y + 4), (x + 5.5, y),
                   (x + 2.5, y), (x + 2.5, y + 4), (x - 1, y + 4)], fill=color)


def thermometer(d, x, y, color):
    rrect(d, (x + 3, y, x + 7, y + 9), radius=2, outline=color)
    d.ellipse((x + 1, y + 8, x + 9, y + 16), fill=color)
    d.rectangle((x + 4, y + 4, x + 6, y + 10), fill=color)


def draw_background(d):
    line = LINE
    d.line((0, 125, 240, 125), fill=line)
    d.line((125, 30, 125, 214), fill=line)
    d.line((60, 123, 60, 127), fill=line)
    d.line((185, 123, 185, 127), fill=line)
    d.line((0, 214, 240, 214), fill=line)
    for x, y in ((60, 125), (125, 125), (185, 125), (125, 214)):
        d.ellipse((x - 2, y - 2, x + 2, y + 2), fill=LINE_DOT)


def draw_header(d, m):
    title = "DEJEDE"
    text(d, (6, 1), title, fit(title, 160, 20, True), WHITE, True)
    sub = "085236578999"
    text(d, (7, 23), sub, fit(sub, 150, 11, True), CYAN, True)

    lvl = signal_bars(m["rssi"])
    for i in range(5):
        h = 4 + i * 3
        x = 174 + i * 8
        box = (x, 21 - h, x + 5, 21)
        if i < lvl:
            d.rectangle(box, fill=CYAN)
        else:
            d.rectangle(box, outline=(60, 80, 90), fill=DIM)
    text(d, (237, 2), m["gen"], 16, CYAN, True, "ra")
    rssi = f"{m['sig_label']}: {m['rssi']:.0f} dBm" if m["rssi"] is not None \
        else f"{m['sig_label']}: N/A"
    text(d, (237, 22), rssi, 11, GREY, False, "ra")


def draw_system(d, m):
    panel(d, (4, 37, 122, 123))
    text(d, (10, 40), "SYSTEM", 13, WHITE, True)
    dot(d, 111, 47, (60, 255, 70) if m["cpu"] < 85 else (255, 60, 60), 4)

    rrect(d, (10, 57, 116, 77), radius=3, outline=CPU_OUT, fill=CPU_FILL)
    n = 12
    hist = [None] * (n - len(m["cpu_hist"])) + list(m["cpu_hist"])
    for i, v in enumerate(hist):
        if v is None:
            continue
        h = max(2, int(17 * v / 100))
        x = 13 + i * 8
        d.rectangle((x, 76 - h, x + 5, 76), fill=scale(color_for(v), 0.4 + 0.6 * i / (n - 1)))
    text(d, (114, 58), f"{m['cpu']:.0f}%", 10, WHITE, True, "ra")

    text(d, (10, 81), "CPU", 13, GREEN, True)
    text(d, (50, 79), m["load"], 15, WHITE, True)

    d.line((9, 99, 117, 99), fill=EDGE)
    temp = f"{m['temp']:.0f} C" if m["temp"] is not None else "N/A"
    text(d, (10, 104), "TEMP", 13, ORANGE, True)
    text(d, (57, 102), temp, 15, WHITE, True)
    thermometer(d, 106, 103, ORANGE)


def draw_memory(d, m):
    panel(d, (128, 37, 236, 123))
    text(d, (134, 40), "MEMORY", 14, WHITE, True)
    dot(d, 224, 47, BLUE, 4)

    bx0, by0, bx1, by1 = 134, 59, 232, 75
    rrect(d, (bx0, by0, bx1, by1), radius=2, outline=BLUE, fill=RAM_FILL)
    fw = int((bx1 - bx0 - 2) * max(0, min(100, m["ram_pct"])) / 100)
    if fw > 0:
        d.rectangle((bx0 + 1, by0 + 1, bx0 + 1 + fw, by1 - 1), fill=RAM_BAR)
    text(d, (bx0 + max(fw, 36) - 3, (by0 + by1) // 2), f"{m['ram_pct']:.0f}%", 12, WHITE, True, "rm")

    rows = (("TOTAL", f"{m['ram_total']} MB", RAM_ROWS[0]),
            ("USED", f"{m['ram_used']} MB", RAM_ROWS[1]),
            ("FREE", f"{m['ram_total'] - m['ram_used']} MB", RAM_ROWS[2]))
    for i, (lab, val, col) in enumerate(rows):
        y = 79 + i * 14
        text(d, (134, y), lab, 12, col, True)
        vs = fit(val, 98 - twidth(lab, 12, True) - 4, 12, True, 8)
        text(d, (232, y), val, vs, WHITE, True, "ra")


def draw_dhcp(d, m):
    panel(d, (4, 127, 122, 212))
    text(d, (10, 130), "DHCP", 12, WHITE, True)
    text(d, (116, 128), str(len(m["leases"])), 15, CYAN, True, "ra")

    leases = m["leases"]
    if leases:
        ip, name = leases[(m["frame"] // 2) % len(leases)]
        text(d, (10, 148), ip, fit(ip, 106, 12, True), WHITE, True)
        text(d, (10, 164), ellipsize(name, 106, 11), 11, (190, 205, 212))
    else:
        text(d, (10, 150), "No leases", 11, GREY)

    d.line((9, 180, 117, 180), fill=EDGE)
    for y, lab, val, col, up in ((183, "UP", m["lan_up"], CYAN, True),
                                 (196, "DOWN", m["lan_down"], ORANGE, False)):
        text(d, (10, y), lab, 11, col, True)
        ax = 10 + twidth(lab, 11, True) + 4
        arrow(d, ax, y + 2, up, col)
        vs = fit(val, 116 - (ax + 12), 11, True, 8)
        text(d, (116, y), val, vs, WHITE, True, "ra")


def draw_network(d, m):
    panel(d, (128, 127, 236, 212))
    text(d, (134, 130), "NETWORK", 12, WHITE, True)

    wan = m["wan_name"]
    wan_val = f"{m['gen']} {wan}" if m["is_cell"] else (wan or "N/A")
    for y, lab, val in ((147, "WAN", wan_val), (161, "LAN", LAN_IF)):
        text(d, (134, y), lab, 11, CYAN, True)
        lw = twidth(lab, 11, True)
        text(d, (134 + lw + 5, y), val, fit(val, 98 - lw - 5, 11, True, 8), WHITE, True)

    d.line((133, 178, 231, 178), fill=EDGE)
    for y, lab, val, col in ((181, "UP", m["wan_up"], CYAN), (195, "DOWN", m["wan_down"], ORANGE)):
        text(d, (134, y), lab, 11, col, True)
        lw = twidth(lab, 11, True)
        text(d, (232, y), val, fit(val, 98 - lw - 4, 11, True, 8), WHITE, True, "ra")


def draw_pill(d, box, label, value):
    x0, y0, x1, y1 = box
    rrect(d, box, radius=(y1 - y0) // 2, fill=PILL_FILL, outline=PILL_EDGE, width=2)
    s = f"{label} {value}" if value else label
    size = fit(s, x1 - x0 - 16, 13, True)
    text(d, ((x0 + x1) // 2, (y0 + y1) // 2), s, size, WHITE, True, "mm")


def render(m):
    global _canvas
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    _canvas = img
    d = ImageDraw.Draw(img)
    draw_background(d)
    draw_header(d, m)
    draw_system(d, m)
    draw_memory(d, m)
    draw_dhcp(d, m)
    draw_network(d, m)
    if PILL_STATIC:
        draw_pill(d, (14, 216, 112, 238), "INFO", "")
        draw_pill(d, (128, 216, 226, 238), "LOGS", "")
    else:
        draw_pill(d, (6, 216, 118, 238), "IP", m["ip"])
        draw_pill(d, (122, 216, 234, 238), "UP", m["uptime"])
    return img


# ==================== TAMPILAN KEDUA: CUACA PIXEL-ART (animasi dinamis) ====================
# Langit digambar di kanvas 80x80 lalu diperbesar 3x (NEAREST) => gaya pixel-art penuh layar.
# Teks & panel digambar tajam di atasnya. Semua animasi = fungsi dari waktu (tanpa state).
SKY_G = 80
GROUND_Y = 70
WET = ("drizzle", "rain", "heavy", "storm")

KIND_LABEL = {"clear": "Cerah", "partly": "Cerah Berawan", "cloudy": "Mendung",
              "fog": "Berkabut", "drizzle": "Gerimis", "rain": "Hujan",
              "heavy": "Hujan Lebat", "storm": "Badai Petir", "snow": "Salju"}

SKY_DAY = {
    "clear": ((38, 118, 232), (150, 214, 255)),
    "partly": ((52, 128, 222), (160, 208, 248)),
    "cloudy": ((104, 124, 146), (176, 190, 204)),
    "fog": ((140, 150, 158), (206, 210, 212)),
    "drizzle": ((78, 100, 124), (140, 160, 178)),
    "rain": ((48, 64, 86), (104, 124, 144)),
    "heavy": ((30, 40, 58), (78, 94, 112)),
    "storm": ((22, 24, 40), (66, 62, 88)),
    "snow": ((140, 160, 188), (222, 232, 242)),
}
WEATHER_KINDS = tuple(SKY_DAY)

ACCENT = {"clear": (255, 214, 60), "partly": (255, 214, 60), "cloudy": (200, 215, 230),
          "fog": (220, 225, 230), "drizzle": (120, 200, 255), "rain": (90, 180, 255),
          "heavy": (70, 150, 255), "storm": (255, 230, 90), "snow": (230, 245, 255)}

DAYS_ID = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
MONTHS_ID = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")

_WMO = {0: ("clear", "Cerah"), 1: ("partly", "Cerah Berawan"), 2: ("partly", "Berawan"),
        3: ("cloudy", "Mendung"), 45: ("fog", "Berkabut"), 48: ("fog", "Berkabut"),
        51: ("drizzle", "Gerimis"), 53: ("drizzle", "Gerimis"), 55: ("drizzle", "Gerimis"),
        56: ("drizzle", "Gerimis Beku"), 57: ("drizzle", "Gerimis Beku"),
        61: ("rain", "Hujan Ringan"), 63: ("rain", "Hujan"), 65: ("heavy", "Hujan Lebat"),
        66: ("rain", "Hujan Beku"), 67: ("heavy", "Hujan Beku"),
        71: ("snow", "Salju"), 73: ("snow", "Salju"), 75: ("snow", "Salju Lebat"),
        77: ("snow", "Salju"), 80: ("rain", "Hujan Sesaat"), 81: ("rain", "Hujan Sesaat"),
        82: ("heavy", "Hujan Deras"), 85: ("snow", "Salju"), 86: ("snow", "Salju Lebat"),
        95: ("storm", "Badai Petir"), 96: ("storm", "Petir & Es"), 99: ("storm", "Petir & Es")}


def wmo_kind(code):
    try:
        return _WMO.get(int(code), ("cloudy", "Mendung"))
    except (TypeError, ValueError):
        return ("cloudy", "Mendung")


def http_get(url, timeout=10):
    """urllib dulu; kalau OpenWrt tidak punya modul/SSL-nya, jatuh ke wget/curl."""
    err = None
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        err = e
    for cmd in (["wget", "-q", "-O", "-", "-T", str(timeout), url],
                ["curl", "-fsS", "-m", str(timeout), url]):
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=timeout + 5, universal_newlines=True)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout
        except Exception:
            continue
    raise err or RuntimeError("http_get gagal")


ADM4_RE = re.compile(r"\d{2}\.\d{2}\.\d{2}\.\d{4}")


def bmkg_kind(desc):
    d = str(desc or "").lower()
    if "petir" in d:
        return "storm"
    if "lebat" in d or "deras" in d:
        return "heavy"
    if "gerimis" in d or "ringan" in d:
        return "drizzle"
    if "hujan" in d:
        return "rain"
    if any(w in d for w in ("kabut", "kabur", "asap")):
        return "fog"
    if "cerah berawan" in d:
        return "partly"
    if "berawan" in d:
        return "cloudy"
    if "cerah" in d:
        return "clear"
    return "cloudy"


def _flt(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def http_lines(url, timeout=30):
    """Baca respons baris demi baris (hemat RAM untuk berkas besar). urllib, lalu fallback wget/curl."""
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            for raw in r:
                yield raw.decode("utf-8", "replace")
        return
    except Exception as e:
        err = e
    for cmd in (["wget", "-q", "-O", "-", "-T", str(timeout), url], ["curl", "-fsS", "-m", "120", url]):
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        except OSError:
            continue
        n = 0
        for line in p.stdout:
            n += 1
            yield line
        p.wait()
        if n:
            return
    raise err


_ROW_RE = re.compile(r"\('(\d{2}(?:\.\d{2}){0,2}(?:\.\d{4})?)','((?:[^'\\]|\\.|'')*)'\)")


def find_adm4(desa, kab, kec=None):
    """Cari kode adm4 dari nama. Kembali: daftar (kode, desa, kecamatan, kabupaten) yang cocok."""
    low = lambda x: x.strip().lower()
    kabs, kecs, vils = {}, {}, []
    for line in http_lines(WILAYAH_URL):
        for code, name in _ROW_RE.findall(line):
            name = name.replace("\\'", "'").replace("''", "'")
            n = code.count(".")
            if n == 1:
                kabs[code] = name
            elif n == 2:
                kecs[code] = name
            elif n == 3 and low(desa) in low(name):
                vils.append((code, name))
    out = []
    for code, name in vils:
        kcode, bcode = code.rsplit(".", 1)[0], code[:5]
        kname, bname = kecs.get(kcode, ""), kabs.get(bcode, "")
        if low(kab) not in low(bname) or (kec and low(kec) not in low(kname)):
            continue
        out.append((code, name, kname, bname))
    out.sort(key=lambda h: (low(h[1]) != low(desa), len(h[1])))        # nama persis didahulukan
    exact = [h for h in out if low(h[1]) == low(desa)]
    return exact or out


def resolve_adm4(desa, kab, kec=None):
    c = find_adm4(desa, kab, kec)
    if not c:
        raise RuntimeError(f"kode wilayah '{desa}, {kab}' tidak ditemukan")
    if len(c) > 1:
        print(f"Ada {len(c)} desa bernama '{desa}' di {kab}; memakai {c[0][2]} ({c[0][0]}). "
              f"Tambahkan kecamatan agar tepat.")
    return c[0][0]


class Weather:
    """Cuaca: BMKG (utama, per desa) + Open-Meteo (pelengkap: tekanan, terasa seperti, siang/malam)."""

    def __init__(self):
        self.snap = None
        self.loc = None
        self.loc_key = None
        self._stop = False
        self._wake = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._stop = True
        self._wake.set()

    def refresh(self):
        """Lokasi/sumber diubah dari LuCI: lupakan cache lokasi & ambil data baru segera."""
        self.loc = self.loc_key = None
        self._wake.set()

    def snapshot(self):
        s = self.snap
        if not s:
            return None
        s = dict(s)
        s["fresh"] = (time.time() - s["ts"]) < WEATHER_STALE
        return s

    def _location(self):
        pl = tuple(WEATHER_PLACE)
        cfg = {"desa": pl[0], "kec": pl[1] if len(pl) == 3 else "", "kab": pl[-1], "adm4": WEATHER_ADM4}
        if not LOCATION_FROM_UCI:                 # kompatibel dengan --lokasi versi lama
            try:
                with open(LOCATION_FILE) as f:
                    cfg.update({k: v for k, v in json.load(f).items() if v})
            except (OSError, ValueError):
                pass
        cfg.setdefault("kec", "")
        key = (cfg.get("adm4"), cfg["desa"], cfg["kec"], cfg["kab"])
        if self.loc and self.loc_key == key:
            return self.loc
        adm4 = cfg.get("adm4")
        if not adm4 and WEATHER_SOURCE == "bmkg":
            try:
                with open(LOCATION_CACHE) as f:
                    c = json.load(f)
                if tuple(str(c.get(k, "")).lower() for k in ("desa", "kec", "kab")) == \
                        tuple(cfg[k].lower() for k in ("desa", "kec", "kab")):
                    adm4 = c.get("adm4")
            except (OSError, ValueError):
                pass
            if not adm4:
                try:
                    adm4 = resolve_adm4(cfg["desa"], cfg["kab"], cfg["kec"] or None)
                    print(f"Kode wilayah {cfg['desa']}, {cfg['kab']} = {adm4}")
                    try:
                        with open(LOCATION_CACHE, "w") as f:
                            json.dump({"desa": cfg["desa"], "kec": cfg["kec"], "kab": cfg["kab"], "adm4": adm4}, f)
                    except OSError:
                        pass
                except Exception as e:
                    print(f"Cari kode wilayah gagal (coba lagi nanti): {e!r}")
        loc = {"adm4": adm4, "desa": cfg["desa"], "kab": cfg["kab"],
               "lat": _flt(cfg.get("lat")) or WEATHER_LAT, "lon": _flt(cfg.get("lon")) or WEATHER_LON}
        if adm4 or WEATHER_SOURCE != "bmkg":
            self.loc, self.loc_key = loc, key        # kalau gagal cari kode: jangan di-cache, ulangi di update berikut
        return loc

    def _fetch_bmkg(self, loc):
        j = json.loads(http_get(BMKG_URL.format(adm4=loc["adm4"])))
        lk = j.get("lokasi") or {}
        ents = []
        for blk in j.get("data", []):
            for day in blk.get("cuaca", []):
                ents.extend(day if isinstance(day, list) else [day])
        now, parsed = time.time(), []
        for e in ents:
            try:
                parsed.append((time.mktime(time.strptime(str(e["local_datetime"])[:19], "%Y-%m-%d %H:%M:%S")), e))
            except (KeyError, ValueError):
                continue
        if not parsed:
            raise RuntimeError("BMKG: tidak ada data prakiraan")
        _, cur = min(parsed, key=lambda p: abs(p[0] - now))
        today = time.strftime("%Y-%m-%d")
        ts_ = [_flt(e.get("t")) for _, e in parsed if str(e.get("local_datetime", "")).startswith(today)]
        ts_ = [x for x in ts_ if x is not None]
        desc = str(cur.get("weather_desc") or "")
        return {"kind": bmkg_kind(desc), "label": desc.title() or "Mendung", "temp": _flt(cur.get("t")),
                "feels": None, "hum": _flt(cur.get("hu")), "wind": _flt(cur.get("ws")), "press": None,
                "tmax": max(ts_) if ts_ else None, "tmin": min(ts_) if ts_ else None,
                "day": 5.75 <= time.localtime().tm_hour + time.localtime().tm_min / 60.0 < 17.75,
                "name": str(lk.get("desa") or loc["desa"]).upper(), "src": "BMKG",
                "lat": _flt(lk.get("lat") or lk.get("latitude")), "lon": _flt(lk.get("lon") or lk.get("longitude"))}

    def _fetch_om(self, lat, lon, name):
        j = json.loads(http_get(WEATHER_URL.format(lat=lat, lon=lon)))
        c, dly = j["current"], j.get("daily", {})
        kind, label = wmo_kind(c.get("weather_code"))

        def first(k):
            v = dly.get(k)
            return v[0] if v else None
        return {"kind": kind, "label": label, "temp": c.get("temperature_2m"),
                "feels": c.get("apparent_temperature"), "hum": c.get("relative_humidity_2m"),
                "wind": c.get("wind_speed_10m"), "press": c.get("pressure_msl"),
                "tmax": first("temperature_2m_max"), "tmin": first("temperature_2m_min"),
                "day": bool(c.get("is_day", 1)), "name": name, "src": "Open-Meteo"}

    def _fetch(self):
        loc = self._location()
        snap = None
        if WEATHER_SOURCE == "bmkg" and loc.get("adm4"):
            try:
                snap = self._fetch_bmkg(loc)
            except Exception as e:
                print(f"BMKG gagal, pakai Open-Meteo: {e!r}")
        lat = (snap or {}).get("lat") or loc["lat"]
        lon = (snap or {}).get("lon") or loc["lon"]
        try:
            om = self._fetch_om(lat, lon, WEATHER_NAME if not snap else snap["name"])
        except Exception as e:
            if snap is None:
                raise
            print(f"Open-Meteo (pelengkap) gagal: {e!r}")
            om = None
        if snap is None:
            snap = om
        elif om:                                   # lengkapi yang tidak ada di BMKG
            snap["press"], snap["feels"], snap["day"] = om["press"], om["feels"], om["day"]
            for k in ("tmax", "tmin"):
                if snap[k] is None:
                    snap[k] = om[k]
        snap["ts"] = time.time()
        return snap

    def _loop(self):
        while not self._stop:
            if not WEATHER_ENABLED:                   # scene cuaca dimatikan dari LuCI: hemat kuota
                self._wake.wait(5)
                self._wake.clear()
                continue
            ok = False
            try:
                self.snap = self._fetch()
                ok = True
            except Exception as e:
                print(f"Ambil cuaca gagal: {e!r}")
            self._wake.wait(WEATHER_INTERVAL if ok else WEATHER_RETRY)
            self._wake.clear()


def sample_weather(kind="rain", day=None):
    if day is None:
        day = 6 <= time.localtime().tm_hour < 18
    return {"kind": kind, "label": KIND_LABEL[kind], "temp": 29.4, "feels": 33.0, "hum": 78,
            "wind": 12.0, "press": 1008, "tmax": 33.0, "tmin": 25.0, "day": day,
            "fresh": True, "ts": time.time()}


# ---------- elemen langit ----------
_sky_cache = {}
_glass_cache = {}
_STAR_RND = random.Random(11)
STARS = [(_STAR_RND.randint(1, 78), _STAR_RND.randint(1, 44), _STAR_RND.random() * 6.28)
         for _ in range(34)]


def sky_colors(kind, day):
    top, bot = SKY_DAY[kind]
    if day:
        return top, bot
    if kind == "clear":
        return (4, 6, 26), (26, 38, 86)
    if kind == "partly":
        return (6, 9, 30), (34, 44, 92)
    tint = (2, 4, 14)
    return tuple(tuple(min(255, int(v * 0.3) + b) for v, b in zip(c, tint)) for c in (top, bot))


def sky_gradient(kind, day):
    key = (kind, day)
    if key not in _sky_cache:
        top, bot = sky_colors(kind, day)
        img = Image.new("RGB", (SKY_G, SKY_G))
        d = ImageDraw.Draw(img)
        bands = 16                                   # gradasi berundak = nuansa pixel-art
        for y in range(SKY_G):
            k = int(y * bands / SKY_G) / (bands - 1)
            d.line((0, y, SKY_G - 1, y), fill=tuple(int(top[i] + (bot[i] - top[i]) * k) for i in range(3)))
        _sky_cache[key] = img
    return _sky_cache[key].copy()


def cloud_cols(kind, day):
    base = {"partly": ((250, 252, 255), (196, 208, 226)), "cloudy": ((206, 214, 224), (150, 162, 180)),
            "fog": ((222, 226, 230), (176, 184, 192)), "drizzle": ((166, 178, 192), (118, 130, 146)),
            "rain": ((128, 140, 156), (86, 98, 114)), "heavy": ((100, 110, 126), (66, 74, 90)),
            "storm": ((76, 78, 98), (44, 46, 64)),
            "snow": ((214, 222, 232), (168, 180, 196))}.get(kind, ((220, 225, 235), (160, 170, 185)))
    return base if day else tuple(scale(c, 0.42) for c in base)


def _drift(i, t, speed, mg):
    return ((i * 31 + t * speed) % (SKY_G + 2 * mg)) - mg


def px_cloud(d, cx, cy, s, col, shade):
    w, h = 26 * s, 9 * s

    def draw(oy, c):
        d.ellipse((cx - w / 2, cy - h * 0.45 + oy, cx + w / 2, cy + h * 0.55 + oy), fill=c)
        for bx, by, r in ((-0.22, -0.45, 0.6), (0.10, -0.78, 0.85), (0.34, -0.30, 0.5)):
            x, y, rr = cx + w * bx, cy + h * by + oy, h * r
            d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=c)
        d.rectangle((cx - w * 0.42, cy + oy, cx + w * 0.42, cy + h * 0.55 + oy), fill=c)
    draw(1.5, shade)
    draw(0, col)


def px_sun(d, cx, cy, r, t):
    ang = t * 0.5
    for i in range(12):
        a = ang + i * math.pi / 6
        r0, r1 = r + 3, r + 5 + (2 if (i + int(t * 2)) % 2 == 0 else 0)
        d.line((cx + math.cos(a) * r0, cy + math.sin(a) * r0,
                cx + math.cos(a) * r1, cy + math.sin(a) * r1), fill=(255, 214, 60))
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 206, 40))
    k = r * 0.45
    d.ellipse((cx - r * 0.55 - k, cy - r * 0.55 - k, cx - r * 0.55 + k, cy - r * 0.55 + k), fill=(255, 240, 130))


def px_moon(img, d, cx, cy, r):
    d.ellipse((cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2), fill=(44, 56, 110))     # halo
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(238, 236, 208))
    sx, sy = cx + r * 0.55, cy - r * 0.35
    bg = img.getpixel((min(SKY_G - 1, int(cx + r * 1.6)), int(sy)))
    rr = r * 0.95
    d.ellipse((sx - rr - 3, sy - rr - 3, sx + rr + 3, sy + rr + 3), fill=bg)            # potong => sabit
    for dx, dy, q in ((-5, 2, 1), (-3, 6, 2)):                                           # kawah
        d.ellipse((cx + dx - q, cy + dy - q, cx + dx + q, cy + dy + q), fill=(206, 204, 176))


def px_stars(d, t, n):
    for i, (x, y, ph) in enumerate(STARS[:n]):
        v = 0.5 + 0.5 * math.sin(t * 2.0 + ph)
        c = scale((255, 255, 235), 0.30 + 0.70 * v)
        d.point((x, y), fill=c)
        if v > 0.92 and i % 3 == 0:
            for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                d.point((x + ox, y + oy), fill=scale(c, 0.55))


def px_shooting(d, t):
    cyc = t % 11.0
    if cyc < 0.6:
        k = cyc / 0.6
        x, y = 72 - k * 38, 5 + k * 22
        d.line((x, y, x + 6, y - 3.5), fill=(255, 255, 240))
        d.point((int(x), int(y)), fill=(255, 255, 255))


def px_ground(d, kind, day):
    if kind == "snow":
        top, body = (246, 250, 255), (220, 230, 242)
    elif kind in WET:
        top, body = (92, 112, 138), (34, 44, 60)
    elif kind == "fog":
        top, body = (150, 158, 164), (120, 128, 134)
    else:
        top, body = (96, 176, 84), (54, 128, 62)
    if not day:
        top, body = scale(top, 0.35), scale(body, 0.35)
    d.rectangle((0, GROUND_Y, SKY_G, SKY_G), fill=body)
    d.line((0, GROUND_Y, SKY_G, GROUND_Y), fill=top)


def px_rain(d, t, n, speed, length, slant, col):
    for i in range(n):
        sp = speed * (0.75 + (i * 7 % 5) * 0.1)
        y = ((i * 53 % SKY_G) + t * sp) % GROUND_Y
        x = ((i * 37 % SKY_G) - y * slant) % SKY_G
        c = scale(col, 0.7 + 0.3 * (i % 3) / 2)
        d.line((x, y, x - slant * length, y + length), fill=c)
    for k in range(max(4, n // 8)):                           # riak air di tanah
        ph = (t * 1.6 + k * 0.31) % 1.0
        if ph < 0.8:
            xk, wid = (k * 29 + 11) % SKY_G, 1 + int(ph * 4)
            d.line((xk - wid, GROUND_Y + 1, xk + wid, GROUND_Y + 1), fill=scale(col, 0.9 - ph * 0.5))


def px_snow(d, t, n=48):
    for i in range(n):
        sp = 5 + (i * 3 % 5) * 2.2
        y = ((i * 47 % SKY_G) + t * sp) % GROUND_Y
        x = ((i * 29 % SKY_G) + math.sin(t * 0.9 + i) * 2.5) % SKY_G
        xi, yi = int(x), int(y)
        d.point((xi, yi), fill=(255, 255, 255))
        if i % 4 == 0:
            for ox, oy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                d.point((xi + ox, yi + oy), fill=(214, 228, 246))


def px_fog(img, t):
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for k in range(8):
        y = 12 + k * 7
        x = math.sin(t * 0.4 + k * 1.3) * 10
        od.rectangle((x - 12, y, x + SKY_G + 12, y + 3), fill=(228, 231, 233, 96 if k % 2 else 60))
        od.ellipse((x + 14 + k * 6, y - 2, x + 40 + k * 6, y + 6), fill=(236, 238, 240, 70))
    return Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")


def px_lightning(img, t):
    period = 4.6
    idx, ph = int(t // period), t % period
    big, small = ph < 0.12, 0.22 < ph < 0.30
    if not (big or small):
        return img
    img = Image.blend(img, Image.new("RGB", img.size, (235, 238, 255)), 0.45 if big else 0.25)
    d = ImageDraw.Draw(img)
    rnd = random.Random(idx)
    x, y = rnd.randint(18, 62), 18
    pts = [(x, y)]
    while y < GROUND_Y:
        y += rnd.randint(5, 9)
        x = max(4, min(SKY_G - 5, x + rnd.randint(-6, 6)))
        pts.append((x, y))
    d.line(pts, fill=(255, 244, 160), width=2)
    d.line(pts, fill=(255, 255, 255), width=1)
    return img


def render_sky(kind, day, t):
    img = sky_gradient(kind, day)
    d = ImageDraw.Draw(img)
    cc, cs = cloud_cols(kind, day)
    if not day and kind in ("clear", "partly"):
        px_stars(d, t, 34 if kind == "clear" else 18)
        if kind == "clear":
            px_shooting(d, t)
    if kind == "clear":
        px_sun(d, 40, 25, 11, t) if day else px_moon(img, d, 40, 25, 11)
    elif kind == "partly":
        px_sun(d, 30, 21, 9, t) if day else px_moon(img, d, 30, 21, 9)
    px_ground(d, kind, day)

    if kind == "drizzle":
        px_rain(d, t, 24, 30, 2, 0.15, (176, 206, 238))
    elif kind == "rain":
        px_rain(d, t, 50, 54, 3, 0.25, (150, 192, 244))
    elif kind in ("heavy", "storm"):
        px_rain(d, t, 84, 74, 4, 0.38, (150, 184, 236))
    elif kind == "snow":
        px_snow(d, t)

    if kind == "partly":
        px_cloud(d, 47 + math.sin(t * 0.5) * 3, 31, 1.35, cc, cs)
        px_cloud(d, _drift(0, t, 2.5, 16), 11, 0.85, cc, cs)
        px_cloud(d, _drift(2, t, 1.8, 14), 46, 0.8, cc, cs)
    elif kind in ("cloudy", "fog"):
        for i, (cy, s, sp) in enumerate(((11, 1.5, 2.2), (25, 1.9, 1.5), (40, 1.6, 2.8), (54, 1.4, 1.9))):
            px_cloud(d, _drift(i, t, sp, int(13 * s) + 2), cy, s, cc, cs)
    elif kind in WET or kind == "snow":
        low = 3 if kind in ("heavy", "storm") else 0
        bc, bs = scale(cc, 0.8), scale(cs, 0.78)
        for i, cy in enumerate((6, 12, 5, 13, 6, 11, 5)):               # lapisan belakang
            px_cloud(d, i * 13 - 3 + math.sin(t * 0.25 + i * 1.7) * 3, cy + low, 1.6, bc, bs)
        for i, cy in enumerate((15, 22, 14, 23, 15)):                    # lapisan depan (tepi bergerigi)
            px_cloud(d, 3 + i * 19 + math.sin(t * 0.35 + i) * 4, cy + low, 1.45, cc, cs)

    if kind == "fog":
        img = px_fog(img, t)
    if kind == "storm":
        img = px_lightning(img, t)
    return img


# ---------- panel kaca & util gambar ----------
def glass(img, box, alpha=120, radius=6, tint=None):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    key = (w, h, alpha, radius)
    mask = _glass_cache.get(key)
    if mask is None:
        mask = Image.new("L", (w, h), 0)
        rrect(ImageDraw.Draw(mask), (0, 0, w - 1, h - 1), radius=radius, fill=alpha)
        _glass_cache[key] = mask
    img.paste(tint or GLASS, (x0, y0), mask)


def stext(d, xy, s, size, fill, bold=False, anchor="la"):
    text(d, (xy[0] + 1, xy[1] + 1), s, size, (0, 0, 0), bold, anchor)
    text(d, xy, s, size, fill, bold, anchor)


def pin_icon(d, x, y, color):
    d.ellipse((x, y, x + 8, y + 8), fill=color)
    d.polygon([(x + 1, y + 6), (x + 7, y + 6), (x + 4, y + 13)], fill=color)
    d.ellipse((x + 3, y + 3, x + 5, y + 5), fill=(20, 30, 40))


def seg_bar(d, x, y, frac, color, t, n=9):
    """Bar bergaya pixel: n kotak kecil, kotak terakhir berkedip."""
    frac = max(0.0, min(1.0, frac))
    lit = frac * n
    for i in range(n):
        bx = x + i * 7
        if i < int(lit):
            c = color
        elif i < lit and int(t * 3) % 2 == 0:
            c = scale(color, 0.8)
        else:
            c = (30, 44, 56)
        d.rectangle((bx, y, bx + 5, y + 3), fill=c)


def _num(v, fmt="{:.0f}"):
    return fmt.format(v) if v is not None else "--"


def render_secondary_dashboard(m, wx=None, t=None):
    """Scene 2: layar penuh cuaca pixel-art, animasinya mengikuti kondisi cuaca nyata."""
    global _canvas
    t = time.time() if t is None else t
    if WEATHER_FORCE:
        wx = sample_weather(WEATHER_FORCE)
    lt = time.localtime(t)
    if not wx:
        wx = {"kind": "clear", "label": "Menunggu data", "temp": None, "feels": None, "hum": None,
              "wind": None, "press": None, "tmax": None, "tmin": None,
              "day": 6 <= lt.tm_hour < 18, "fresh": False}
    kind, day = wx["kind"], wx["day"]
    loc = wx.get("name") or WEATHER_NAME
    img = render_sky(kind, day, t).resize((WIDTH, HEIGHT), Image.NEAREST)
    _canvas = img
    d = ImageDraw.Draw(img)

    # sudut-sudut pixel berdenyut
    acc = scale(CYAN, 0.65 + 0.35 * math.sin(t * 3))
    for cx, cy, sx, sy in ((1, 1, 1, 1), (238, 1, -1, 1), (1, 238, 1, -1), (238, 238, -1, -1)):
        d.line((cx, cy, cx + sx * 10, cy), fill=acc, width=2)
        d.line((cx, cy, cx, cy + sy * 10), fill=acc, width=2)

    # bar atas: lokasi + status data + jam
    glass(img, (5, 5, 235, 25), 110, 8)
    pin_icon(d, 11, 8, (255, 90, 80))
    stext(d, (25, 6), loc, fit(loc, 100, 13, True), WHITE, True)
    nx = 25 + twidth(loc, fit(loc, 100, 13, True), True) + 8
    if wx.get("fresh"):
        dot(d, nx, 15, (60, 255, 90), 2)
    elif int(t * 2) % 2 == 0:
        dot(d, nx, 15, (255, 140, 30), 2)
    wm = twidth("00", 16, True)
    stext(d, (228, 5), time.strftime("%M", lt), 16, WHITE, True, "ra")
    colon_col = WHITE if int(t * 2) % 2 == 0 else (120, 130, 140)
    stext(d, (228 - wm - 2, 4), ":", 16, colon_col, True, "ra")
    stext(d, (228 - wm - 2 - twidth(":", 16, True) - 2, 5), time.strftime("%H", lt), 16, WHITE, True, "ra")

    # blok suhu (kaca tipis supaya terbaca di atas langit apa pun)
    glass(img, (5, 122, 235, 192), 78, 8)
    temp = wx.get("temp")
    ts = _num(temp)
    stext(d, (12, 124), ts, 40, WHITE, True)
    stext(d, (12 + twidth(ts, 40, True) + 2, 128), "°C", 18, (220, 235, 245), True)
    label = wx["label"]
    stext(d, (230, 126), label, fit(label, 118, 16, True, 9),
          ACCENT["storm"] if kind == "storm" else WHITE, True, "ra")
    if wx.get("feels") is not None:
        stext(d, (230, 146), f"Terasa {_num(wx.get('feels'))}°", 12, (215, 228, 238), False, "ra")
    stext(d, (230, 160), f"Maks {_num(wx.get('tmax'))}°  Min {_num(wx.get('tmin'))}°", 11,
          (190, 206, 220), False, "ra")
    date = f"{DAYS_ID[lt.tm_wday]}, {lt.tm_mday} {MONTHS_ID[lt.tm_mon - 1]} {lt.tm_year}"
    stext(d, (120, 178), date, fit(date, 150, 11), (225, 235, 242), False, "ma")
    stext(d, (231, 180), wx.get("src") or "", 9, (170, 190, 205), True, "ra")

    # tiga kartu info di bawah
    press = wx.get("press")
    tiles = (("LEMBAB", f"{_num(wx.get('hum'))}%", (wx.get("hum") or 0) / 100.0, CYAN),
             ("ANGIN", f"{_num(wx.get('wind'))} km/j", (wx.get("wind") or 0) / 40.0, GREEN),
             ("TEKANAN", f"{_num(press)} hPa", ((press or 990) - 990) / 40.0, ORANGE))
    for i, (lab, val, frac, col) in enumerate(tiles):
        x0 = 7 + i * 77
        glass(img, (x0, 196, x0 + 72, 236), 120, 7)
        stext(d, (x0 + 5, 199), lab, 10, col, True)
        stext(d, (x0 + 5, 212), val, fit(val, 62, 14, True, 8), WHITE, True)
        seg_bar(d, x0 + 5, 229, frac, col, t)
    return img


# ==================== SCENE LOGO DEJEDE (bumper sinematik 10 detik) ====================
# Mengikuti storyboard (semua digambar prosedural - tanpa berkas gambar):
#   0.0-2.8   PUSARAN energi hijau: partikel mengorbit & berputar mengecil ke inti yang menyala
#   1.2-4.2   RAKIT: 10 pelat armor hijau terbang masuk berputar, dipegang lengan robot, lalu menyatu
#             (percikan saat menempel); lensa logam bertingkat terbentuk; unit flash turun di atas
#   4.5-5.5   MORPH: komponen mekanik memudar jadi logo 3D bersih (ekstrusi + bevel); teks DEJEDE
#             logam gelap 3D naik huruf demi huruf
#   5.9-7.0   KROM: logo berubah jadi perak berkilau, garis cahaya menyapu di bawah teks
#   7.0-8.6   FLARE: ledakan cahaya di lensa (sinar + garis anamorfik), logo kembali HIJAU menyala
#   8.6-10    AKHIR: logo hijau 3D, latar nebula, kilau menyapu pelan
# Durasi animasi menyesuaikan "Lama bumper logo" (diskalakan bila bukan 10 detik).
LOGO_BRAND = (8, 176, 66)           # hijau logo asli
LOGO_FOLLOW_THEME = False           # True = warna logo mengikuti tema aktif
LOGO_DUR = 10.0                     # durasi rancangan animasi (detik)
LOGO_SCALE = 0.14                   # satuan logo asli (kanvas 1800) -> piksel layar
LOGO_OX = 120 - 969.5 * LOGO_SCALE
LOGO_OY = 12 - 204 * LOGO_SCALE
LOGO_RECTS = ((300, 412, 434, 1345), (300, 412, 797, 547), (434, 339, 569, 412),   # dinding kiri, pita atas, tonjolan
              (662, 204, 797, 547), (662, 204, 1125, 339), (990, 204, 1125, 547),  # kaki kiri, palang, kaki kanan
              (990, 412, 1490, 547), (1355, 204, 1490, 1345), (300, 1210, 1490, 1345),
              (1355, 204, 1639, 339))                                               # + blok flash kanan atas
LOGO_LENS = (884, 879, 260, 112)            # pusat x, y, radius luar, radius dalam
LOGO_TEXT_Y = 177                           # posisi atas teks DEJEDE
ARM_PLATES = (0, 3, 6, 8, 9)                # pelat yang dipegang lengan robot
_logo = {}
_logo_styles = {}
_lutq_cache = {}
_glyphs = {}


def _lx(v):
    return v * LOGO_SCALE + LOGO_OX


def _ly(v):
    return v * LOGO_SCALE + LOGO_OY


def _lut(k):
    return [int(i * k) for i in range(256)]


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def _ease_out(p):
    return 1.0 - (1.0 - p) ** 3


def _ease_io(p):
    return 0.5 - 0.5 * math.cos(math.pi * p)


def _lutq(a):
    """Tabel alfa (dikuantisasi, di-cache) untuk Image.point()."""
    q = int(_clamp(a) * 48 + 0.5)
    t = _lutq_cache.get(q)
    if t is None:
        t = _lutq_cache[q] = [int(i * q / 48.0) for i in range(256)]
    return t


def _reveal_lut(R, F):
    """Tabel untuk peta jarak: 255 di dalam radius R, memudar halus (smoothstep) sejauh F piksel."""
    out = []
    for v in range(256):
        t = _clamp((R - v) / F)
        out.append(int(255 * t * t * (3 - 2 * t)))
    return out


def _dist_img(cx, cy, size=None):
    """Gambar L: nilai tiap piksel = jarak (px) ke (cx, cy), maksimum 255."""
    im = Image.new("L", size or (WIDTH, HEIGHT), 255)
    dd = ImageDraw.Draw(im)
    for r in range(254, -1, -1):
        dd.ellipse((cx - r, cy - r, cx + r, cy + r), fill=r)
    return im


def _shape(img, cx, cy, r, color, alpha, width=None, a0=0.0, a1=360.0):
    """Cakram / cincin / busur anti-alias (digambar 2x pada tambalan kecil lalu diperkecil)."""
    if alpha <= 0.01 or r <= 0:
        return
    n = int(2 * (r + 4)) + 2
    ox, oy = int(cx - n / 2), int(cy - n / 2)
    c = ((cx - ox) * 2, (cy - oy) * 2)
    big = Image.new("L", (n * 2, n * 2), 0)
    bd = ImageDraw.Draw(big)
    box = (c[0] - 2 * r, c[1] - 2 * r, c[0] + 2 * r, c[1] + 2 * r)
    if width is None:
        bd.ellipse(box, fill=255)
    elif a1 - a0 >= 359.9:
        bd.ellipse(box, outline=255, width=max(1, int(width * 2)))
    else:
        bd.arc(box, a0, a1, fill=255, width=max(1, int(width * 2)))
    img.paste(color, (ox, oy), big.resize((n, n), Image.BOX).point(_lutq(alpha)))


def _glow_sprite(R):
    """Sprite L: bola cahaya lembut (terang di pusat, memudar kuadratik)."""
    s = _glyphs.get(("glow", R))
    if s is None:
        s = Image.new("L", (2 * R, 2 * R), 0)
        sd = ImageDraw.Draw(s)
        for r in range(R, 0, -1):
            sd.ellipse((R - r, R - r, R + r, R + r), fill=int(255 * (1 - r / R) ** 2.2))
        _glyphs[("glow", R)] = s
    return s


def _glow(img, cx, cy, R, color, alpha):
    if alpha > 0.01:
        img.paste(color, (int(cx - R), int(cy - R)), _glow_sprite(R).point(_lutq(alpha)))


def _rays(img, cx, cy, n, length, color, alpha, rot, jit):
    """Sinar lens-flare (segitiga runcing), digambar 2x agar halus."""
    if alpha <= 0.01:
        return
    size = int(2 * length) + 8
    ox, oy = int(cx - size / 2), int(cy - size / 2)
    c0, c1 = (cx - ox) * 2, (cy - oy) * 2
    big = Image.new("L", (size * 2, size * 2), 0)
    bd = ImageDraw.Draw(big)
    for i in range(n):
        a = rot + i * 2 * math.pi / n + (jit[i % len(jit)] - 0.5) * 0.25
        ln = length * 2 * (0.5 + 0.5 * jit[(i * 3 + 1) % len(jit)])
        w = (0.8 + 1.2 * jit[(i * 5 + 2) % len(jit)]) * 2
        px, py, nx, ny = math.cos(a), math.sin(a), -math.sin(a), math.cos(a)
        bd.polygon([(c0 + nx * w, c1 + ny * w), (c0 + px * ln, c1 + py * ln), (c0 - nx * w, c1 - ny * w)], fill=255)
    img.paste(color, (ox, oy), big.resize((size, size), Image.BOX).point(_lutq(alpha)))


def _streak(img, cx, cy, half, color, alpha):
    """Garis cahaya anamorfik horizontal (memudar ke ujung)."""
    if alpha <= 0.01 or half < 2:
        return
    band = Image.new("L", (WIDTH, 1))
    band.putdata([int(255 * max(0.0, 1 - abs(x - cx) / half) ** 1.7) for x in range(WIDTH)])
    band = band.resize((WIDTH, 3), Image.NEAREST)
    img.paste(color, (0, int(cy) - 1), band.point(_lutq(0.35 * alpha)))
    img.paste((255, 255, 255), (0, int(cy)), band.crop((0, 0, WIDTH, 1)).point(_lutq(0.9 * alpha)))


def _glyph(s, size, bold):
    key = (s, size, bold)
    g = _glyphs.get(key)
    if g is None:
        f, faux, _half, k = get_font(size, bold)
        w = int(_scratch.textlength(s, font=f)) + 3
        h = (size * 3) // 2 + 2 if k == 1.0 else 14
        g = Image.new("L", (w, h), 0)
        gd = ImageDraw.Draw(g)
        gd.text((0, 0), s, font=f, fill=255)
        if faux:
            gd.text((1, 0), s, font=f, fill=255)
        if k != 1.0:
            g = g.resize((max(1, int(round(w * k))), max(1, int(round(h * k)))),
                         Image.NEAREST if k >= 1.9 else Image.BILINEAR)
        _glyphs[key] = g
    return g


def _glyph_edge(s, size, bold):
    """Tepi atas huruf (untuk kilap bevel teks 3D)."""
    key = ("edge", s, size, bold)
    e = _glyphs.get(key)
    if e is None:
        g = _glyph(s, size, bold)
        e = _glyphs[key] = ImageChops.subtract(g, ImageChops.offset(g, 0, 1))
    return e


def _logo_masks():
    """Topeng logo (anti-alias via supersampling) + data bantu animasi, dibuat sekali."""
    if _logo:
        return _logo
    k = 4
    big = WIDTH * k
    body = Image.new("L", (big, big), 0)
    bd = ImageDraw.Draw(body)
    for r in LOGO_RECTS:
        bd.rectangle((_lx(r[0]) * k, _ly(r[1]) * k, _lx(r[2]) * k - 1, _ly(r[3]) * k - 1), fill=255)
    cx, cy, ro, ri = LOGO_LENS
    lcx, lcy = _lx(cx), _ly(cy)
    for rad, fillv in ((ro, 255), (ri, 0)):
        rr = rad * LOGO_SCALE * k
        bd.ellipse((lcx * k - rr, lcy * k - rr, lcx * k + rr, lcy * k + rr), fill=fillv)
    allm = body.resize((WIDTH, HEIGHT), Image.LANCZOS)

    diag = Image.new("L", (WIDTH, HEIGHT), 0)               # nilai = (x + 0.45*y): untuk kilau menyapu
    dd = ImageDraw.Draw(diag)
    span = WIDTH * 1.45
    for c in range(int(span) + 1):
        dd.line((c, 0, c - 0.45 * HEIGHT, HEIGHT), fill=int(c * 255 / span), width=3)

    rnd = random.Random(7)
    orb, rings = [], []                                     # partikel orbit pusaran + garis cincin
    for a, b, tilt, n, sp in ((34, 10, -0.5, 18, 2.6), (52, 15, 0.35, 22, -2.1), (70, 20, -0.15, 26, 1.7),
                              (88, 26, 0.65, 28, -1.4), (106, 30, -0.85, 30, 1.15), (124, 38, 0.1, 32, -0.9)):
        rings.append((a, b, tilt))
        for i in range(n):
            orb.append((a, b, tilt, rnd.random() * 6.283, sp * rnd.uniform(0.8, 1.2),
                        rnd.choice((1, 1, 1, 2)), rnd.uniform(0.5, 1.0)))
    for j in range(90):                                     # debu spiral
        orb.append((6 + j * 0.9, (6 + j * 0.9) * 0.55, rnd.uniform(-0.2, 0.2), j * 0.62, 2.4 / (1 + j * 0.05),
                    1, rnd.uniform(0.4, 0.9)))
    stars = [(rnd.uniform(3, 237), rnd.uniform(3, 237), rnd.random() * 6.283, rnd.uniform(0.8, 2.4),
              rnd.choice((1, 1, 1, 2))) for _ in range(46)]
    poses = []                                              # pose awal tiap pelat (di luar layar)
    for i in range(len(LOGO_RECTS)):
        a = rnd.uniform(0, 6.283)
        dist = rnd.uniform(150, 200)
        poses.append((120 + dist * math.cos(a), 110 + dist * math.sin(a), rnd.choice((-1, 1)) * rnd.uniform(70, 230)))
    _logo.update(all=allm, glow=allm.filter(ImageFilter.GaussianBlur(10)), ext=ImageChops.offset(allm, 2, 3),
                 hl=ImageChops.subtract(allm, ImageChops.offset(allm, 1, 1)),
                 sh=ImageChops.subtract(allm, ImageChops.offset(allm, -1, -1)),
                 diag=diag, span=span, lx=lcx, ly=lcy, orb=orb, rings=rings, stars=stars, poses=poses,
                 jit=[rnd.random() for _ in range(16)])
    return _logo


def _logo_base():
    return scale(CYAN, 0.85) if LOGO_FOLLOW_THEME else LOGO_BRAND


def _plate_sprite(w, h, base):
    """Pelat armor: gradien logam hijau, tepi gelap (garis sambungan), kilap atas/kiri, paku keling."""
    k = 4
    W, H = max(2, int(round(w))), max(2, int(round(h)))
    big = Image.new("RGB", (W * k, H * k))
    bd = ImageDraw.Draw(big)
    top, bot = _mix(base, (255, 255, 255), 0.30), scale(base, 0.60)
    for y in range(H * k):
        bd.line((0, y, W * k, y), fill=_mix(top, bot, y / float(H * k)))
    edge = scale(base, 0.32)
    bd.rectangle((0, 0, W * k - 1, H * k - 1), outline=edge, width=k)
    light = _mix(base, (255, 255, 255), 0.62)
    bd.line((k, k, W * k - k, k), fill=light, width=max(1, k // 2))
    bd.line((k, k, k, H * k - k), fill=light, width=max(1, k // 2))
    horizontal = W >= H
    n = int(max(W, H) // 26)
    for i in range(1, n + 1):
        t = (i / float(n + 1)) * (W if horizontal else H)
        px, py = (t * k, H * k / 2) if horizontal else (W * k / 2, t * k)
        bd.ellipse((px - 1.3 * k, py - 1.3 * k, px + 1.3 * k, py + 1.3 * k), fill=edge)
        bd.ellipse((px - 0.7 * k, py - 0.9 * k, px + 0.5 * k, py + 0.3 * k), fill=light)
    spr = big.resize((W, H), Image.LANCZOS).convert("RGBA")
    return spr


def _logo_style():
    """Warna logo, latar (vinyet + grid + nebula) dan sprite pelat; di-cache per tema."""
    base = _logo_base()
    key = (base, BG, PANEL, CYAN)
    st = _logo_styles.get(key)
    if st:
        return st
    if len(_logo_styles) > 4:
        _logo_styles.clear()
    L = _logo_masks()
    hi = _mix(base, (255, 255, 255), 0.55)
    # latar: vinyet + grid tipis
    dv = _dist_img(120, 105).point([min(255, int(i * 1.55)) for i in range(256)])
    center, edge = _mix(BG, PANEL, 0.75), scale(BG, 0.55)
    bg = Image.composite(Image.new("RGB", (WIDTH, HEIGHT), edge), Image.new("RGB", (WIDTH, HEIGHT), center), dv)
    gd = ImageDraw.Draw(bg)
    gl = _mix(bg.getpixel((120, 120)), CYAN, 0.10)
    for v in (60, 120, 180):
        gd.line((v, 0, v, HEIGHT), fill=gl)
        gd.line((0, v, WIDTH, v), fill=gl)
    # nebula (dibuat kecil lalu diperbesar: murah)
    nb = Image.new("RGB", (170, 170), (0, 0, 0))
    nd = ImageDraw.Draw(nb)
    rnd = random.Random(11)
    cols = (scale(CYAN, 0.20), scale(base, 0.24), scale(_mix(CYAN, base, 0.5), 0.18))
    for _i in range(14):
        r = rnd.uniform(17, 40)
        x, y = rnd.uniform(0, 170), rnd.uniform(0, 170)
        nd.ellipse((x - r, y - r, x + r, y + r), fill=rnd.choice(cols))
    nb = nb.filter(ImageFilter.GaussianBlur(13)).resize((340, 340), Image.BICUBIC)
    # isi logo: hijau (atas terang -> bawah gelap) dan perak (pita terang-gelap seperti krom)
    y0, y1 = _ly(204), _ly(1345)
    fg, fs = Image.new("RGB", (WIDTH, HEIGHT)), Image.new("RGB", (WIDTH, HEIGHT))
    gd1, gd2 = ImageDraw.Draw(fg), ImageDraw.Draw(fs)
    top, bot = _mix(base, (255, 255, 255), 0.24), scale(base, 0.70)
    bands = ((0.0, 232), (0.22, 150), (0.45, 236), (0.62, 120), (0.82, 205), (1.0, 96))
    for y in range(HEIGHT):
        t = _clamp((y - y0) / (y1 - y0))
        gd1.line((0, y, WIDTH - 1, y), fill=_mix(top, bot, t))
        for (ta, va), (tb, vb) in zip(bands, bands[1:]):
            if ta <= t <= tb:
                v = va + (vb - va) * _ease_io((t - ta) / (tb - ta))
                break
        gd2.line((0, y, WIDTH - 1, y), fill=(int(v * 0.96), int(v), min(255, int(v * 1.05))))
    sprites = []
    for r in LOGO_RECTS:
        sprites.append(_plate_sprite((r[2] - r[0]) * LOGO_SCALE, (r[3] - r[1]) * LOGO_SCALE, base))
    st = _logo_styles[key] = {"base": base, "hi": hi, "bg": bg, "neb": nb, "fill_g": fg, "fill_s": fs,
                              "sprites": sprites, "star": _mix(hi, (255, 255, 255), 0.35),
                              "txt": (232, 238, 240)}
    return st


def _ik(B, T, L1, L2, sign):
    """Siku lengan 2 ruas dari pangkal B ke ujung T."""
    dx, dy = T[0] - B[0], T[1] - B[1]
    dist = max(1e-3, math.hypot(dx, dy))
    d = min(dist, L1 + L2 - 0.5)
    a = (L1 * L1 - L2 * L2 + d * d) / (2 * d)
    h = math.sqrt(max(0.0, L1 * L1 - a * a))
    ux, uy = dx / dist, dy / dist
    return (B[0] + ux * a - sign * uy * h, B[1] + uy * a + sign * ux * h)


def _arms(img, arms):
    """Lengan robot (garis tebal logam + sendi + capit), digambar 2x agar halus."""
    if not arms:
        return
    S2 = 2
    body = Image.new("L", (WIDTH * S2, HEIGHT * S2), 0)
    shine = Image.new("L", (WIDTH * S2, HEIGHT * S2), 0)
    bd, sd = ImageDraw.Draw(body), ImageDraw.Draw(shine)
    for B, T, sign, alpha in arms:
        dist = math.hypot(T[0] - B[0], T[1] - B[1])
        Lk = 0.56 * dist + 8
        E = _ik(B, T, Lk, Lk, sign)
        pts = [(B[0] * S2, B[1] * S2), (E[0] * S2, E[1] * S2), (T[0] * S2, T[1] * S2)]
        wd = max(2, int(6 * S2 * alpha))
        bd.line(pts, fill=255, width=wd, joint="curve")
        sd.line([(p[0] - 1, p[1] - 1) for p in pts], fill=255, width=max(1, int(1.4 * S2 * alpha)))
        for P, rr in ((pts[0], 4.2), (pts[1], 3.4)):
            bd.ellipse((P[0] - rr * S2, P[1] - rr * S2, P[0] + rr * S2, P[1] + rr * S2), fill=255)
        ang = math.atan2(T[1] - E[1], T[0] - E[0])
        for da in (-0.6, 0.6):                                 # capit
            bd.line((T[0] * S2, T[1] * S2, (T[0] + 8 * math.cos(ang + da)) * S2, (T[1] + 8 * math.sin(ang + da)) * S2),
                    fill=255, width=2 * S2)
    img.paste((60, 70, 76), (0, 0), body.resize((WIDTH, HEIGHT), Image.BOX))
    img.paste((196, 210, 216), (0, 0), shine.resize((WIDTH, HEIGHT), Image.BOX).point(_lutq(0.9)))


def _mech_lens(img, cx, cy, R, a, rot, base, hi):
    """Lensa logam bertingkat: rumah, cincin krom, alur berputar, laras, kaca hijau menyala."""
    if a <= 0.01 or R < 3:
        return
    _shape(img, cx, cy, R, (22, 26, 30), a)
    _shape(img, cx, cy, R * 0.93, (150, 160, 166), 0.85 * a, max(1.0, R * 0.07))
    _shape(img, cx, cy, R * 0.93, (238, 246, 248), 0.9 * a, max(1.0, R * 0.04), 195, 275)
    _shape(img, cx, cy, R * 0.80, (44, 50, 55), a)
    d = ImageDraw.Draw(img)
    for i in range(24):
        t = math.radians(rot + i * 15)
        d.line((cx + R * 0.70 * math.cos(t), cy + R * 0.70 * math.sin(t), cx + R * 0.78 * math.cos(t),
                cy + R * 0.78 * math.sin(t)), fill=scale((124, 136, 142), a))
    _shape(img, cx, cy, R * 0.66, (14, 17, 19), a)
    _shape(img, cx, cy, R * 0.62, (84, 94, 100), 0.8 * a, max(1.0, R * 0.04))
    _shape(img, cx, cy, R * 0.46, (6, 18, 15), a)
    _shape(img, cx, cy, R * 0.36, base, 0.55 * a)
    _shape(img, cx, cy, R * 0.14, hi, 0.95 * a)
    _shape(img, cx - R * 0.18, cy - R * 0.20, R * 0.07, (255, 255, 255), 0.8 * a)


def _flash_unit(img, a, slide):
    """Unit flash logam kecil di atas menara kamera."""
    if a <= 0.01:
        return
    x0, x1 = _lx(800), _lx(990)
    y1 = _ly(204) + 1
    y0 = y1 - 9 - slide
    y1 = y1 - slide
    d = ImageDraw.Draw(img)
    d.rectangle((x0, y0, x1, y1), fill=scale((78, 86, 92), a))
    d.line((x0, y0, x1, y0), fill=scale((176, 186, 192), a))
    d.line((x0, y1, x1, y1), fill=scale((26, 30, 34), a))
    d.rectangle((x0 + 4, y0 + 2, x1 - 4, y0 + 5), fill=scale((196, 210, 214), a))


def _text3d(img, x, y, ch, size, a, hot):
    """Huruf logam gelap 3D: ekstrusi + muka + tepi atas berkilau (hot = semburat hijau)."""
    if a <= 0.01:
        return
    m = _glyph(ch, size, True)
    xi, yi = int(round(x)), int(round(y))
    for dx, dy in ((3, 3), (2, 2), (1, 1)):
        img.paste((8, 12, 14), (xi + dx, yi + dy), m.point(_lutq(a * 0.9)))
    img.paste((84, 96, 102), (xi, yi), m.point(_lutq(a)))
    img.paste(_mix((176, 188, 194), (150, 255, 190), hot), (xi, yi), _glyph_edge(ch, size, True).point(_lutq(a * 0.9)))


def render_logo_cinematic(m=None, u=0.0):
    """Gaya logo 1 (cinematic): bumper sinematik 10 detik (lihat ringkasan alur di atas)."""
    global _canvas
    dur = SCENE_LEN.get("logo", LOGO_DUR)
    u = max(0.0, u) * LOGO_DUR / max(4.0, dur)               # skala bila durasi di LuCI bukan 10 dtk
    L, S = _logo_masks(), _logo_style()
    base, hi = S["base"], S["hi"]
    lx, ly = L["lx"], L["ly"]
    img = S["bg"].copy()
    neb = _ease_io(_clamp((u - 4.8) / 2.6))
    if neb > 0.01:                                           # nebula melayang pelan
        ox, oy = int(50 + 18 * math.sin(u * 0.17)), int(50 + 14 * math.cos(u * 0.13))
        img = Image.blend(img, ImageChops.add(img, S["neb"].crop((ox, oy, ox + WIDTH, oy + HEIGHT))), neb)
    _canvas = img
    d = ImageDraw.Draw(img)

    # bintang berkedip
    sf = _clamp(u / 0.8)
    for x, y, ph, sp, sz in L["stars"]:
        b = (0.25 + 0.75 * (0.5 + 0.5 * math.sin(u * sp + ph))) * sf * (0.6 if sz == 1 else 0.9)
        col = scale(S["star"], b)
        if sz == 1:
            d.point((x, y), fill=col)
        else:
            d.ellipse((x - 0.8, y - 0.8, x + 0.8, y + 0.8), fill=col)

    # ---- 1. pusaran energi + inti menyala ----
    kv = _ease_out(_clamp(u / 0.6)) * (1.0 - _clamp((u - 2.0) / 1.2))
    kc = _ease_out(_clamp(u / 0.5)) * (1.0 - _clamp((u - 3.4) / 0.8))
    if kv > 0.01:
        shrink = 1.0 - 0.45 * _ease_io(_clamp((u - 0.6) / 1.6))
        spin = 1.0 + 1.5 * _clamp((u - 0.4) / 1.6)
        pc = _mix(hi, (255, 255, 255), 0.25)
        for a_, b_, tilt in L["rings"]:                      # garis cincin orbit tipis
            pts = []
            for j in range(41):
                t = j * math.pi / 20.0
                ex, ey = a_ * shrink * math.cos(t), b_ * shrink * math.sin(t)
                pts.append((lx + ex * math.cos(tilt) - ey * math.sin(tilt), ly + ex * math.sin(tilt) + ey * math.cos(tilt)))
            d.line(pts, fill=scale(base, 0.42 * kv), width=1)
        for a_, b_, tilt, ph, sp, sz, br in L["orb"]:
            for tr in (0, 1, 2, 3):                          # ekor pendek di belakang partikel
                ang = ph + u * sp * spin - tr * 0.08 * (1 if sp > 0 else -1)
                ex, ey = a_ * shrink * math.cos(ang), b_ * shrink * math.sin(ang)
                x = lx + ex * math.cos(tilt) - ey * math.sin(tilt)
                y = ly + ex * math.sin(tilt) + ey * math.cos(tilt)
                depth = 0.5 + 0.5 * (0.5 + 0.5 * math.sin(ang))
                col = scale(pc, min(1.0, br * 1.25) * depth * kv * (1.0 - 0.22 * tr))
                if tr == 0 and sz == 2:
                    d.ellipse((x - 1.3, y - 1.3, x + 1.3, y + 1.3), fill=col)
                else:
                    d.point((x, y), fill=col)
    if kc > 0.01:
        pulse = 0.85 + 0.15 * math.sin(u * 7.0)
        _glow(img, lx, ly, 92, base, 0.60 * kc * pulse)
        _glow(img, lx, ly, 44, hi, 0.85 * kc)
        _glow(img, lx, ly, 14, (255, 255, 255), kc * pulse)
        _rays(img, lx, ly, 10, 44 + 18 * kv, hi, 0.28 * kc, u * 0.4, L["jit"])

    # ---- 2. rakit: pelat armor + lengan robot + lensa + unit flash ----
    sprites, poses = S["sprites"], L["poses"]
    arms, sparks = [], []
    if 1.0 <= u < 5.6:
        for i, r in enumerate(LOGO_RECTS):
            t0 = 1.2 + 0.19 * i
            p = _clamp((u - t0) / 1.0)
            if p <= 0:
                continue
            fx, fy = _lx((r[0] + r[2]) / 2.0), _ly((r[1] + r[3]) / 2.0)
            sx, sy, rot0 = poses[i]
            e = _ease_out(p)
            x, y, rot = fx + (sx - fx) * (1 - e), fy + (sy - fy) * (1 - e), rot0 * (1 - e)
            spr = sprites[i]
            if abs(rot) > 0.3:
                spr = spr.rotate(rot, Image.BICUBIC, expand=True)
            img.paste(spr, (int(round(x - spr.size[0] / 2.0)), int(round(y - spr.size[1] / 2.0))), spr)
            if i in ARM_PLATES:
                ai = _clamp((u - (t0 - 0.2)) / 0.3)
                ao = _clamp((u - (t0 + 1.0)) / 0.7)
                if ai > 0 and ao < 1:
                    B = (_clamp(sx, 4, 236), _clamp(sy, 4, 236))
                    T = (x + (B[0] - x) * _ease_io(ao), y + (B[1] - y) * _ease_io(ao))
                    arms.append((B, T, 1 if i % 2 else -1, min(ai, 1 - ao)))
            q = _clamp((u - (t0 + 1.0)) / 0.45)
            if 0 < q < 1:
                sparks.append((fx, fy, q))
        _arms(img, arms)
        for fx, fy, q in sparks:
            _shape(img, fx, fy, 5 + 15 * q, hi, 0.85 * (1 - q), 1.3)
            _glow(img, fx, fy, 16, hi, 0.6 * (1 - q))
        # lensa logam terbentuk, lalu memudar saat berubah jadi logo bersih
        lk = _ease_out(_clamp((u - 2.4) / 1.2))
        la = 1.0 - _ease_io(_clamp((u - 4.7) / 0.8))
        _mech_lens(img, lx, ly, LOGO_LENS[2] * LOGO_SCALE * lk, la, u * 45.0, base, hi)
        fa = _ease_out(_clamp((u - 3.2) / 0.7)) * (1.0 - _ease_io(_clamp((u - 4.7) / 0.7)))
        _flash_unit(img, fa, 14 * (1 - _ease_out(_clamp((u - 3.2) / 0.7))))

    # ---- 3. logo 3D bersih (morph dari rakitan) ----
    au = _ease_io(_clamp((u - 4.5) / 0.9))
    ks = _ease_io(_clamp((u - 5.9) / 0.9)) * (1.0 - _ease_io(_clamp((u - 7.1) / 0.9)))      # 0 hijau .. 1 krom
    kg = _ease_io(_clamp((u - 7.1) / 0.9))                                                    # hijau menyala kembali
    t7 = _clamp((u - 7.0) / 1.7)
    kf = math.sin(math.pi * t7) ** 1.2 if 0 < t7 < 1 else 0.0                                 # ledakan flare
    if au > 0.01:
        g = au * (0.10 + 0.20 * kg * (0.8 + 0.2 * math.sin(u * 2.2)) + 0.40 * kf)
        img.paste(base, (0, 0), L["glow"].point(_lutq(g)))
        ext = _mix(scale(base, 0.30), (40, 46, 52), ks)
        img.paste(ext, (0, 0), L["ext"].point(_lutq(au * 0.95)))
        fill = Image.blend(S["fill_g"], S["fill_s"], ks) if ks > 0.01 else S["fill_g"]
        img.paste(fill, (0, 0), L["all"].point(_lutq(au)))
        img.paste((255, 255, 255), (0, 0), L["hl"].point(_lutq(au * 0.55)))
        img.paste((0, 0, 0), (0, 0), L["sh"].point(_lutq(au * 0.40)))

    # ---- 4. teks DEJEDE logam gelap 3D ----
    word, size = "DEJEDE", fit("DEJEDE", 170, 32, True)
    ws = [twidth(ch, size, True) for ch in word]
    track = 3.0
    x = 120 - (sum(ws) + track * (len(word) - 1)) / 2
    for i, ch in enumerate(word):
        a = _ease_out(_clamp((u - (5.0 + i * 0.10)) / 0.6))
        _text3d(img, x, LOGO_TEXT_Y + 10 * (1 - a), ch, size, a, kg)
        x += ws[i] + track

    # ---- 5. efek cahaya: garis krom, flare, kilau ----
    ts = _clamp((u - 6.3) / 0.7)
    if 0 < ts < 1:
        _streak(img, 120, LOGO_TEXT_Y + 33, 105 + 20 * ts, (210, 232, 238), math.sin(math.pi * ts))
    if kf > 0.01:
        _glow(img, lx, ly, 70, base, 0.85 * kf)
        _glow(img, lx, ly, 34, hi, 0.95 * kf)
        _glow(img, lx, ly, 14, (255, 255, 255), kf)
        _rays(img, lx, ly, 14, 82, hi, 0.55 * kf, u * 0.22, L["jit"])
        _rays(img, lx, ly, 8, 52, (255, 255, 255), 0.5 * kf, -u * 0.3 + 0.4, L["jit"])
        _streak(img, lx, ly, 126, hi, 0.9 * kf)
    if u > 8.6:
        q = ((u - 8.6) % 3.2) / 1.3                           # kilau menyapu pelan di logo akhir
        if q < 1.0:
            pos = -30 + _ease_io(q) * (L["span"] + 60)
            lut = [int(255 * max(0.0, 1 - abs(v * L["span"] / 255.0 - pos) / 22.0) ** 2) for v in range(256)]
            img.paste((210, 255, 225), (0, 0), ImageChops.multiply(L["diag"].point(lut), L["all"]).point(_lutq(0.45)))
        _streak(img, lx, ly, 70 + 20 * math.sin(u * 1.3), hi, 0.22)
    return img
# ======================================================================================


# ==================== LOGO OPENING DEJEDE: 4 GAYA ====================
# Dipilih / diputar / dimatikan dari LuCI (Services > DejedeMon > Logo Opening):
#   cinematic : bumper 3D prosedural di atas (rakit pelat -> krom -> flare), tanpa berkas gambar
#   sticker   : stiker DEJEDE (LOGOKU_COBA2.png) mendarat, berpendar, kilau menyapu
#   mark      : logo goresan tangan (LOGOKUMARK1.png): kamera terbuka dari lensa, flash, "dejede" tersapu masuk
#   neon      : logo yang sama jadi papan neon (warna ikut tema), berkedip lalu menyala stabil
# Berkas gambar dicari di /usr/share/dejedemon/logos lalu folder "logos" di sebelah skrip ini.
# Bila berkas tidak ada, gaya itu otomatis diganti "cinematic".
LOGO_STYLES = {"cinematic": "Cinematic 3D", "sticker": "Sticker Peel", "mark": "Hand-drawn Mark", "neon": "Neon Sign"}
LOGO_MODE = "fixed"                  # "fixed" = satu gaya | "rotate" = bergantian tiap kemunculan
LOGO_FIXED = "cinematic"
LOGO_LIST = ()                       # gaya yang diputar (kosong = semua)
LOGO_DIRS = ("/usr/share/dejedemon/logos", os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos"))
LOGO_FILES = {"sticker": "LOGOKU_COBA2.png", "mark": "LOGOKUMARK1.png", "neon": "LOGOKUMARK1.png"}
LOGO_STACKED = True                  # mark/neon: kamera di atas, tulisan di bawah (False = tata letak melebar asli)
LOGO_TAGLINE = "PHOTO & VIDEO"       # teks kecil di bawah logo mark/neon ("" = tanpa teks)
_logo_src = {}
_logo_art = {}


def _logo_path(style):
    fn = LOGO_FILES.get(style)
    for d in LOGO_DIRS:
        p = os.path.join(d, fn) if fn else None
        if p and os.path.isfile(p):
            return p
    return None


def _logo_src_img(style):
    """Muat gambar logo (RGBA, dipangkas ke area terlihat). None bila berkas tidak ada; dicoba lagi tiap 30 dtk."""
    now = time.monotonic()
    ent = _logo_src.get(style)
    if ent and (ent[0] is not None or now - ent[1] < 30.0):
        return ent[0]
    im, p = None, _logo_path(style)
    if p:
        try:
            im = Image.open(p).convert("RGBA")
            bb = im.split()[3].point(lambda v: 255 if v > 10 else 0).getbbox()
            if bb:
                im = im.crop(bb)
        except Exception:
            im = None
            print(f"Gagal memuat logo {p}:\n" + traceback.format_exc())
    _logo_src[style] = (im, now)
    return im


def active_logo_style(t=None):
    """Gaya logo untuk kemunculan saat ini (tanpa state: rotate = fungsi dari nomor periode scene)."""
    avail = [k for k in LOGO_STYLES if k == "cinematic" or _logo_src_img(k) is not None]
    if LOGO_MODE == "rotate":
        lst = [k for k in (LOGO_LIST or tuple(LOGO_STYLES)) if k in avail] or ["cinematic"]
        t = time.time() if t is None else t
        return lst[int(t // max(1, SCENE_PERIOD)) % len(lst)]
    return LOGO_FIXED if LOGO_FIXED in avail else "cinematic"


def _logo_accent():
    return CYAN if active_logo_style() == "neon" else _logo_base()


def _art_put(key, art):
    if len(_logo_art) > 8:
        _logo_art.clear()
    _logo_art[key] = art
    return art


def _fit_img(im, maxw, maxh):
    s = min(maxw / float(im.size[0]), maxh / float(im.size[1]))
    return im.resize((max(1, int(round(im.size[0] * s))), max(1, int(round(im.size[1] * s)))), Image.LANCZOS)


def _green_mask(im):
    r, g = im.split()[0], im.split()[1]
    return ImageChops.subtract(g, r).point([min(255, v * 5) for v in range(256)])


def _recolor_green(im, color):
    """Ganti warna hijau logo dengan warna lain (putih/hitam/abu tetap)."""
    mask = _green_mask(im)
    out = Image.composite(Image.new("RGB", im.size, color), im.convert("RGB"), mask).convert("RGBA")
    out.putalpha(im.split()[3])
    return out


def _diag_img(w, h):
    """Gambar L: nilai = (x + y) dinormalisasi, untuk kilau diagonal."""
    im = Image.new("L", (w, h), 0)
    dd = ImageDraw.Draw(im)
    span = w + h
    for k in range(span):
        dd.line((k, 0, k - h, h), fill=int(255 * k / span))
    return im


def _shine(img, diag, alpha, x, y, pos, span, strength=0.55, color=(255, 255, 255)):
    lut = [int(255 * max(0.0, 1 - abs(v * span / 255.0 - pos) / 22.0) ** 2) for v in range(256)]
    img.paste(color, (x, y), ImageChops.multiply(diag.point(lut), alpha).point(_lutq(strength)))


def _logo_bg(u):
    """Latar logo bersama (vinyet + grid dari tema, bintang berkedip)."""
    global _canvas
    S, L = _logo_style(), _logo_masks()
    img = S["bg"].copy()
    _canvas = img
    d = ImageDraw.Draw(img)
    sf = _clamp(u / 0.8)
    for x, y, ph, sp, sz in L["stars"]:
        b = (0.25 + 0.75 * (0.5 + 0.5 * math.sin(u * sp + ph))) * sf * (0.6 if sz == 1 else 0.9)
        col = scale(S["star"], b)
        if sz == 1:
            d.point((x, y), fill=col)
        else:
            d.ellipse((x - 0.8, y - 0.8, x + 0.8, y + 0.8), fill=col)
    return img, d, S


def _tagline(img, d, y, u, t0):
    if not LOGO_TAGLINE:
        return
    a = _ease_out(_clamp((u - t0) / 0.7))
    if a <= 0.01:
        return
    size, track = 9, 2.5
    ws = [twidth(ch, size) for ch in LOGO_TAGLINE]
    x = 120 - (sum(ws) + track * (len(ws) - 1)) / 2
    col = _mix(BG, (196, 210, 214), a * 0.85)
    for ch, w in zip(LOGO_TAGLINE, ws):
        if ch != " ":
            text(d, (x, y + 4 * (1 - a)), ch, size, col)
        x += w + track


# ---------- gaya 2: STICKER ----------
def _art_sticker():
    key = ("sticker", LOGO_FOLLOW_THEME, CYAN)
    A = _logo_art.get(key)
    if A:
        return A
    spr = _fit_img(_logo_src_img("sticker"), 212, 212)
    if LOGO_FOLLOW_THEME:
        spr = _recolor_green(spr, scale(CYAN, 0.85))
    w, h = spr.size
    pad = 26
    gm = Image.new("L", (w + 2 * pad, h + 2 * pad), 0)
    gm.paste(spr.split()[3], (pad, pad))
    return _art_put(key, {"spr": spr, "w": w, "h": h, "glow": gm.filter(ImageFilter.GaussianBlur(12)),
                          "diag": _diag_img(w, h), "alpha": spr.split()[3]})


def render_logo_sticker(u, dur):
    A = _art_sticker()
    img, d, S = _logo_bg(u)
    w, h = A["w"], A["h"]
    p = _clamp(u / 1.1)
    c1 = 1.70158
    e = 1 + (c1 + 1) * (p - 1) ** 3 + c1 * (p - 1) ** 2               # ease-out-back: mendarat lalu mengendap
    sc, rot, fa = 0.2 + 0.8 * e, -26.0 * (1 - _ease_out(p)), _clamp(u / 0.35)
    cx, cy = 120, 118 + 2.5 * math.sin(u * 1.6) * _clamp((u - 1.0) / 0.6)
    pulse = 0.78 + 0.22 * math.sin(u * 2.4)
    g = A["glow"]
    if abs(sc - 1) > 0.01:
        g = g.resize((max(1, int(g.size[0] * sc)), max(1, int(g.size[1] * sc))), Image.BILINEAR)
    img.paste(CYAN, (int(cx - g.size[0] / 2), int(cy - g.size[1] / 2)), g.point(_lutq(0.6 * fa * pulse)))
    si = A["spr"]
    if abs(sc - 1) > 0.01:
        si = si.resize((max(1, int(w * sc)), max(1, int(h * sc))), Image.BILINEAR)
    if abs(rot) > 0.3:
        si = si.rotate(rot, Image.BICUBIC, expand=True)
    if fa < 1.0:
        si = si.copy()
        si.putalpha(si.split()[3].point(_lutq(fa)))
    sx, sy = int(round(cx - si.size[0] / 2.0)), int(round(cy - si.size[1] / 2.0))
    img.paste(si, (sx, sy), si)
    if 0.8 < u < 1.8:                                                  # riak cahaya saat mendarat
        q = (u - 0.8) / 1.0
        _shape(img, 120, 118, 40 + 100 * _ease_out(q), CYAN, 0.55 * (1 - q), 1.5)
    if u > 1.3:                                                        # kilau menyapu berkala
        q = ((u - 1.3) % 2.8) / 1.2
        if q < 1.0:
            _shine(img, A["diag"], A["alpha"], sx, sy, -30 + _ease_io(q) * (w + h + 60), w + h)
    return img


# ---------- gaya 3 & 4: MARK (goresan tangan) dan NEON ----------
def _mark_parts():
    """Pisahkan kamera (hijau) dari tulisan (putih) lalu atur tata letak di layar 240x240; dibuat sekali."""
    key = ("parts", LOGO_STACKED)
    P = _logo_art.get(key)
    if P:
        return P
    src = _logo_src_img("mark")
    a = src.split()[3]
    gm = _green_mask(src)
    icon_a, text_a = ImageChops.multiply(a, gm), ImageChops.multiply(a, ImageChops.invert(gm))
    thr = lambda m: m.point(lambda v: 255 if v > 24 else 0).getbbox()
    ib, tb = thr(icon_a), thr(text_a)
    if LOGO_STACKED:
        iw, ih = _fit_img(icon_a.crop(ib), 104, 84).size
        tw, th = _fit_img(text_a.crop(tb), 196, 62).size
        tagh = 12 if LOGO_TAGLINE else 0
        top = (HEIGHT - (ih + 14 + th + (12 + tagh if tagh else 0))) // 2 - 2
        ix, iy, tx, ty = (WIDTH - iw) // 2, top, (WIDTH - tw) // 2, top + ih + 14
        tag_y = ty + th + 12
    else:
        f = 212.0 / src.size[0]
        iw, ih = int(round((ib[2] - ib[0]) * f)), int(round((ib[3] - ib[1]) * f))
        tw, th = int(round((tb[2] - tb[0]) * f)), int(round((tb[3] - tb[1]) * f))
        left, top = (WIDTH - int(src.size[0] * f)) // 2, 100 - int(src.size[1] * f) // 2
        ix, iy = left + int(round(ib[0] * f)), top + int(round(ib[1] * f))
        tx, ty = left + int(round(tb[0] * f)), top + int(round(tb[1] * f))
        tag_y = top + int(src.size[1] * f) + 20
    parts = {"src": src, "ib": ib, "tb": tb, "icon": (ix, iy, iw, ih), "text": (tx, ty, tw, th), "tag_y": tag_y,
             "icon_a": icon_a.crop(ib).resize((iw, ih), Image.LANCZOS),
             "text_a": text_a.crop(tb).resize((tw, th), Image.LANCZOS)}
    return _art_put(key, parts)


def _art_mark():
    key = ("mark", LOGO_FOLLOW_THEME, CYAN, LOGO_STACKED)
    A = _logo_art.get(key)
    if A:
        return A
    P = _mark_parts()
    src = _recolor_green(P["src"], _logo_base()) if LOGO_FOLLOW_THEME else P["src"]
    ix, iy, iw, ih = P["icon"]
    tx, ty, tw, th = P["text"]
    icon = src.crop(P["ib"]).resize((iw, ih), Image.LANCZOS)
    icon.putalpha(P["icon_a"])
    text_ = Image.new("RGBA", (tw, th), (246, 248, 248, 255))
    text_.putalpha(P["text_a"])
    pad = 18
    ig = Image.new("L", (iw + 2 * pad, ih + 2 * pad), 0)
    ig.paste(P["icon_a"], (pad, pad))
    lens = (iw / 2.0, ih * 0.62)
    A = {"icon": icon, "text": text_, "pad": pad, "iglow": ig.filter(ImageFilter.GaussianBlur(10)),
         "idist": _dist_img(int(lens[0]), int(lens[1]), (iw, ih)), "lens": (ix + lens[0], iy + lens[1]),
         "idiag": _diag_img(iw, ih), "tdiag": _diag_img(tw, th)}
    return _art_put(key, A)


def render_logo_mark(u, dur):
    P, A = _mark_parts(), _art_mark()
    img, d, S = _logo_bg(u)
    base, hi = S["base"], S["hi"]
    ix, iy, iw, ih = P["icon"]
    tx, ty, tw, th = P["text"]
    pad = A["pad"]
    breathe = 0.7 + 0.3 * math.sin(u * 2.2)
    ki = _clamp((u - 0.2) / 1.0)
    if ki > 0:                                                         # kamera terbuka dari lensa (iris lembut)
        R = _ease_out(ki) * max(iw, ih) * 0.95
        mask = ImageChops.multiply(P["icon_a"], A["idist"].point(_reveal_lut(R, 10.0)))
        img.paste(base, (ix - pad, iy - pad), A["iglow"].point(_lutq(0.55 * _ease_out(ki) * breathe)))
        layer = A["icon"].copy()
        layer.putalpha(mask)
        img.paste(layer, (ix, iy), layer)
    q = (u - 1.0) / 0.8
    if 0 < q < 1:                                                      # flash di lensa
        lx, ly = A["lens"]
        _glow(img, lx, ly, 46, hi, 0.9 * math.sin(math.pi * q))
        _streak(img, lx, ly, 104, hi, math.sin(math.pi * q))
    tp = _ease_io(_clamp((u - 1.3) / 1.1))
    if tp > 0:                                                         # tulisan tersapu masuk dari kiri
        front = -14 + tp * (tw + 28)
        strip = Image.new("L", (tw, 1))
        strip.putdata([int(255 * _clamp((front - x) / 14.0)) for x in range(tw)])
        mask = ImageChops.multiply(P["text_a"], strip.resize((tw, th), Image.NEAREST))
        layer = A["text"].copy()
        layer.putalpha(mask)
        img.paste(layer, (tx, ty + int(round(6 * (1 - tp)))), layer)
    _tagline(img, d, P["tag_y"], u, 2.3)
    if u > 2.8:                                                        # kilau menyapu kamera lalu tulisan
        q = ((u - 2.8) % 3.4) / 1.5
        if q < 1.0:
            e = _ease_io(q)
            _shine(img, A["idiag"], P["icon_a"], ix, iy, -30 + e * (iw + ih + 60), iw + ih, 0.6, (210, 255, 225))
            _shine(img, A["tdiag"], P["text_a"], tx, ty, -30 + e * (tw + th + 60), tw + th, 0.5)
    return img


def _art_neon():
    key = ("neon", CYAN, ORANGE, LOGO_STACKED)
    A = _logo_art.get(key)
    if A:
        return A
    P = _mark_parts()
    tubes = []
    for part, col, seed in (("icon", CYAN, 3), ("text", ORANGE, 8)):
        x, y, w, h = P[part]
        m = Image.new("L", (WIDTH, HEIGHT), 0)
        m.paste(P[part + "_a"], (x, y))
        core = ImageChops.subtract(m, m.filter(ImageFilter.MinFilter(5))).filter(ImageFilter.GaussianBlur(0.6))
        tubes.append({"core": core, "g1": core.filter(ImageFilter.GaussianBlur(3)),
                      "g2": core.filter(ImageFilter.GaussianBlur(10)), "col": col,
                      "hot": _mix(col, (255, 255, 255), 0.72), "off": scale(col, 0.30), "seed": seed})
    return _art_put(key, {"tubes": tubes})


def _neon_level(u, seed):
    if u >= 2.4:
        return 0.94 + 0.06 * math.sin(u * 7.3 + seed)                  # dengung halus
    ramp = _clamp(u / 2.4)
    r = random.Random(seed * 1009 + int(u * 14)).random()              # kedip acak deterministik
    return 1.0 if r < 0.22 + 0.78 * ramp * ramp else 0.06


def render_logo_neon(u, dur):
    global _canvas
    P, A = _mark_parts(), _art_neon()
    img, d, S = _logo_bg(u)
    img = Image.blend(img, Image.new("RGB", (WIDTH, HEIGHT), scale(BG, 0.4)), 0.35)
    for t in A["tubes"]:
        lv = _neon_level(u, t["seed"])
        img.paste(t["off"], (0, 0), t["core"].point(_lutq(0.55)))      # tabung padam tetap samar
        img.paste(t["col"], (0, 0), t["g2"].point(_lutq(0.55 * lv)))
        img.paste(t["col"], (0, 0), t["g1"].point(_lutq(0.85 * lv)))
        img.paste(t["hot"], (0, 0), t["core"].point(_lutq(lv)))
    _canvas = img
    _tagline(img, ImageDraw.Draw(img), P["tag_y"], u, 2.6)
    return img


def render_logo(m=None, u=0.0):
    """Scene logo opening: gaya mengikuti pengaturan LuCI (tetap / berputar)."""
    st = active_logo_style()
    if st != "cinematic":
        try:
            return {"sticker": render_logo_sticker, "mark": render_logo_mark,
                    "neon": render_logo_neon}[st](max(0.0, u), SCENE_LEN.get("logo", LOGO_DUR))
        except Exception:
            if "logo_" + st not in _failed:
                _failed.add("logo_" + st)
                print(f"Logo '{st}' GAGAL, pakai cinematic:\n" + traceback.format_exc())
    return render_logo_cinematic(m, u)
# ======================================================================================


# ==================== TRANSISI PIXEL antar scene ====================
_GN = WIDTH // TRANS_BLOCK
_trans_order = None


def _order():
    global _trans_order
    if _trans_order is None:
        rnd = random.Random(2026)
        _trans_order = [0.62 * (bx + by) / (2.0 * (_GN - 1)) + 0.38 * rnd.random()
                        for by in range(_GN) for bx in range(_GN)]
    return _trans_order


def pixel_transition(old, new, p, accent):
    """Dissolve blok-pixel menyapu diagonal + acak; tepi depannya berkedip bergaya checker."""
    p = max(0.0, min(1.0, p))
    p = p * p * (3 - 2 * p)
    th, band = p * 1.08, 0.08
    order = _order()
    m_new, m_a, m_b = (Image.new("L", (_GN, _GN)) for _ in range(3))
    m_new.putdata([255 if v < th - band else 0 for v in order])
    edge = [(th - band <= v < th) for v in order]
    m_a.putdata([255 if e and (i % _GN + i // _GN) % 2 == 0 else 0 for i, e in enumerate(edge)])
    m_b.putdata([255 if e and (i % _GN + i // _GN) % 2 == 1 else 0 for i, e in enumerate(edge)])
    big = lambda im: im.resize((WIDTH, HEIGHT), Image.NEAREST)
    out = Image.composite(new, old, big(m_new))
    out.paste(accent, None, big(m_a))
    out.paste((255, 255, 255), None, big(m_b))
    return out
# ======================================================================================


_DS = None


def iris_transition(old, new, p, accent):
    """Iris kamera: scene baru terbuka dari tengah layar, pinggirnya lembut + cincin cahaya tipis."""
    global _DS
    if _DS is None:
        _DS = _dist_img(120, 120)
    p = _ease_io(_clamp(p))
    R, F = p * 192.0, 16.0
    out = Image.composite(new, old, _DS.point(_reveal_lut(R, F)))
    if 0.02 < p < 0.98:
        a = math.sin(math.pi * p)
        _shape(out, 120, 120, max(1.0, R - F * 0.5), accent, 0.85 * a, 1.5)
        _shape(out, 120, 120, max(1.0, R - F * 1.7), accent, 0.35 * a, 1.0)
    return out


def scene_transition(old, new, p, accent):
    if TRANS_STYLE == "fade":
        return Image.blend(old, new, _ease_io(_clamp(p)))
    if TRANS_STYLE == "pixel":
        return pixel_transition(old, new, p, accent)
    return iris_transition(old, new, p, accent)


def _legacy_font(size):
    for path in ("/usr/share/fonts/ttf/DejaVuSans.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/DejaVuSans.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _rtext(d, x_right, y, s, fill, font):
    w = d.textlength(s, font=font)
    d.text((x_right - w, y), s, fill=fill, font=font)


def _legacy_bar(d, x, y, w, h, pct):
    pct = max(0, min(100, pct))
    d.rectangle((x, y, x + w, y + h), outline=(80, 80, 80))
    fw = int((w - 2) * pct / 100)
    if fw > 0:
        d.rectangle((x + 1, y + 1, x + 1 + fw, y + h - 1), fill=color_for(pct))


def render_legacy(m):
    ft, fb, fs = _legacy_font(18), _legacy_font(15), _legacy_font(13)
    img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, WIDTH, 32), fill=(0, 102, 204))
    d.text((8, 6), "OPENWRT MONITOR", fill=(255, 255, 255), font=ft)
    d.text((8, 40), f"IP  {m['ip']}", fill=(0, 255, 255), font=fb)
    d.text((8, 60), time.strftime("%d-%m-%Y  %H:%M:%S"), fill=(150, 150, 150), font=fs)
    d.text((8, 84), "CPU", fill=(255, 255, 0), font=fb)
    _rtext(d, 232, 84, f"{m['cpu']:.0f}%", (255, 255, 255), fb)
    _legacy_bar(d, 8, 104, 224, 10, m["cpu"])
    d.text((8, 122), "RAM", fill=(0, 255, 0), font=fb)
    _rtext(d, 232, 122, f"{m['ram_used']}/{m['ram_total']}MB", (255, 255, 255), fb)
    _legacy_bar(d, 8, 142, 224, 10, m["ram_pct"])
    temp = f"{m['temp']:.0f}C" if m["temp"] is not None else "N/A"
    d.text((8, 162), f"TEMP {temp}", fill=(255, 140, 0), font=fb)
    _rtext(d, 232, 162, f"LOAD {m['load']}", (200, 200, 200), fb)
    d.text((8, 186), f"RX {m['lan_down']}", fill=(80, 180, 255), font=fs)
    _rtext(d, 232, 186, f"TX {m['lan_up']}", (255, 120, 200), fs)
    d.text((8, 210), f"UP  {m['uptime']}", fill=(200, 200, 200), font=fb)
    return img


def safe(fn, default, *args):
    try:
        return fn(*args)
    except Exception:
        return default


def render_minimal(m):
    img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default()
    lines = [("OPENWRT MONITOR", (255, 255, 255)),
             (f"IP  {m.get('ip', 'N/A')}", (0, 255, 255)),
             (f"CPU {m.get('cpu', 0):.0f}%", (255, 255, 0)),
             (f"RAM {m.get('ram_used', 0)}/{m.get('ram_total', 0)}MB", (0, 255, 0)),
             (f"UP  {m.get('uptime', '--')}", (200, 200, 200))]
    for i, (t, c) in enumerate(lines):
        d.text((8, 10 + i * 20), t, fill=c, font=f)
    return img


_last_used = None
_failed = set()


def render_main(m, quiet=False):
    """Scene 1: dashboard utama (dengan fallback bertingkat)."""
    global _last_used
    for name, fn in (("baru", render), ("cadangan", render_legacy), ("minimal", render_minimal)):
        try:
            img = fn(m)
            if not quiet and name != _last_used:
                print(f"Tampilan aktif: {name}")
                _last_used = name
            return img
        except Exception:
            if name not in _failed:
                _failed.add(name)
                print(f"Render '{name}' GAGAL:\n" + traceback.format_exc())
    return Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 80))


def _timeline():
    """Daftar segmen jadwal: ('trans', dari, ke, t0, t1) / ('scene', nama, None, t0, t1)."""
    segs, t, prev, start = [], 0.0, "main", {}
    for name in SCENE_ORDER:
        segs.append(("trans", prev, name, t, t + TRANS))
        t += TRANS
        start[name] = t
        segs.append(("scene", name, None, t, t + SCENE_LEN[name]))
        t += SCENE_LEN[name]
        prev = name
    if SCENE_ORDER:
        segs.append(("trans", prev, "main", t, t + TRANS))
        t += TRANS
    return segs, t, start


_SEGS, _SEQ_END, _SCENE_START = _timeline()


def _trans_col(name):
    return _logo_accent() if name == "logo" else CYAN if name == "cuaca" else ORANGE


def animating():
    """True selama urutan scene + transisi (atau crossfade tema) berlangsung => loop dipercepat."""
    if time.time() - _fade["t0"] < THEME_FADE + 0.1:
        return True
    return (time.time() % SCENE_PERIOD) < _SEQ_END + 0.15


def _scene_img(name, m, wx, sec):
    if name == "main":
        return render_main(m, True)
    if name == "logo":
        return render_logo(m, max(0.0, sec - _SCENE_START["logo"]))
    return render_secondary_dashboard(m, wx)


def _build_frame(m, wx=None):
    global _last_used
    sec = time.time() % SCENE_PERIOD
    if sec < _SEQ_END:
        try:
            for kind, a, b, t0, t1 in _SEGS:
                if t0 <= sec < t1:
                    if kind == "trans":
                        return scene_transition(_scene_img(a, m, wx, sec), _scene_img(b, m, wx, sec),
                                                (sec - t0) / (t1 - t0), _trans_col(b))
                    if _last_used != a:
                        print(f"Tampilan aktif: {a}")
                        _last_used = a
                    return _scene_img(a, m, wx, sec)
        except Exception:
            if "scene" not in _failed:
                _failed.add("scene")
                print("Render scene GAGAL:\n" + traceback.format_exc())
    return render_main(m)


# ==================== KONFIGURASI LuCI (/etc/config/tftmonitor) ====================
# Berkas dibaca ulang otomatis begitu berubah (cek tiap ~2 detik): tak perlu restart layanan.
CFG_FILE = "/etc/config/tftmonitor"      # nama berkas UCI tetap "tftmonitor" (kompatibel dengan instalasi lama)
TRANS_STYLE = "iris"                 # "iris" | "fade" | "pixel"
WEATHER_ENABLED = True
LOCATION_FROM_UCI = False            # True = lokasi dari LuCI; berkas LOCATION_FILE lama diabaikan
THEME_MODE = "fixed"                 # "fixed" | "auto"
THEME_FIXED = "cyber"
THEME_LIST = ()
THEME_INTERVAL = 5                   # menit
THEME_ORDER = "seq"                  # "seq" | "random"
THEME_FADE = 0.9                     # detik: crossfade saat tema berganti
_fade = {"t0": -99.0, "img": None}
_last_frame = None
_cfg_mtime = None
_cfg_check = 0.0


def read_uci(path, section="main"):
    """Baca satu section bernama dari berkas UCI -> dict (list => list Python)."""
    out, cur = {}, None
    try:
        with open(path) as f:
            for raw in f:
                try:
                    parts = shlex.split(raw, comments=True)
                except ValueError:
                    continue
                if not parts:
                    continue
                if parts[0] == "config":
                    cur = parts[2] if len(parts) > 2 else None
                elif cur == section and len(parts) >= 3 and parts[0] == "option":
                    out[parts[1]] = parts[2]
                elif cur == section and len(parts) >= 3 and parts[0] == "list":
                    if not isinstance(out.get(parts[1]), list):
                        out[parts[1]] = []
                    out[parts[1]].append(parts[2])
    except OSError:
        pass
    return out


def uci_set(opts):
    """Tulis opsi ke /etc/config/tftmonitor lewat perintah uci (dipakai --lokasi)."""
    try:
        for k, v in opts.items():
            subprocess.run(["uci", "set", f"tftmonitor.main.{k}={v}"], check=True, timeout=10)
        subprocess.run(["uci", "commit", "tftmonitor"], check=True, timeout=10)
        return True
    except Exception:
        return False


def _b(v, default=True):
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "on", "yes", "enabled")


def _i(v, default, lo, hi):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def apply_config():
    global SCENE_PERIOD, SCENE_ORDER, SCENE_LEN, TRANS, TRANS_STYLE, _SEGS, _SEQ_END, _SCENE_START
    global WEATHER_ENABLED, WEATHER_SOURCE, WEATHER_PLACE, WEATHER_ADM4, WEATHER_LAT, WEATHER_LON
    global WEATHER_NAME, WEATHER_INTERVAL, WEATHER_STALE, WEATHER_FORCE, LOCATION_FROM_UCI
    global THEME_MODE, THEME_FIXED, THEME_LIST, THEME_INTERVAL, THEME_ORDER, LOGO_FOLLOW_THEME
    global LOGO_MODE, LOGO_FIXED, LOGO_LIST
    c = read_uci(CFG_FILE)

    # --- scene & transisi ---
    logo_on = _b(c.get("logo_enabled"), True)
    WEATHER_ENABLED = _b(c.get("weather_enabled"), True)
    SCENE_ORDER = tuple(n for n, on in (("logo", logo_on), ("cuaca", WEATHER_ENABLED)) if on)
    ls = _i(c.get("logo_seconds"), 10, 5, 30)
    if ls in (7, 8):                  # nilai bawaan paket lama -> pakai 10 dtk sesuai rancangan baru
        ls = 10
    SCENE_LEN = {"logo": float(ls),
                 "cuaca": float(_i(c.get("weather_seconds"), 15, 5, 60))}
    st = str(c.get("transition", "iris"))
    TRANS_STYLE = st if st in ("iris", "fade", "pixel") else "iris"
    TRANS = {"iris": 1.2, "fade": 0.8, "pixel": 1.0}[TRANS_STYLE]
    _SEGS, _SEQ_END, _SCENE_START = _timeline()
    SCENE_PERIOD = max(_i(c.get("scene_period"), 60, 20, 3600), int(_SEQ_END) + 10)

    # --- logo opening (gaya: tetap atau bergantian; logo_enabled=0 mematikan scene logo) ---
    LOGO_MODE = "rotate" if str(c.get("logo_mode", "fixed")) == "rotate" else "fixed"
    LOGO_FIXED = str(c.get("logo_style", "cinematic"))
    if LOGO_FIXED not in LOGO_STYLES:
        LOGO_FIXED = "cinematic"
    ll = c.get("logo_list", [])
    ll = ll.split() if isinstance(ll, str) else ll
    LOGO_LIST = tuple(k for k in ll if k in LOGO_STYLES)

    # --- cuaca ---
    src = str(c.get("weather_source", "bmkg"))
    WEATHER_SOURCE = src if src in ("bmkg", "openmeteo") else "bmkg"
    desa, kec, kab = (str(c.get(k, "")).strip() for k in ("desa", "kec", "kab"))
    WEATHER_ADM4 = str(c.get("adm4", "")).strip()
    LOCATION_FROM_UCI = bool(desa or WEATHER_ADM4)
    if desa and kab:
        WEATHER_PLACE = (desa, kec, kab) if kec else (desa, kab)
    la, lo = _flt(c.get("lat")), _flt(c.get("lon"))
    if la is not None and lo is not None and -90 <= la <= 90 and -180 <= lo <= 180:
        WEATHER_LAT, WEATHER_LON = la, lo
    WEATHER_NAME = (desa or kab or "CUACA").upper()
    WEATHER_INTERVAL = _i(c.get("weather_interval"), 10, 1, 180) * 60
    WEATHER_STALE = max(1800, WEATHER_INTERVAL * 3)
    fc = str(c.get("weather_force", ""))
    WEATHER_FORCE = fc if fc in WEATHER_KINDS else None

    # --- tema ---
    THEME_MODE = "auto" if str(c.get("theme_mode", "fixed")) == "auto" else "fixed"
    THEME_FIXED = str(c.get("theme", "cyber"))
    tl = c.get("theme_list", [])
    tl = tl.split() if isinstance(tl, str) else tl
    THEME_LIST = tuple(k for k in tl if k in THEMES)
    THEME_INTERVAL = _i(c.get("theme_interval"), 5, 1, 1440)
    THEME_ORDER = "random" if str(c.get("theme_order", "seq")) == "random" else "seq"
    LOGO_FOLLOW_THEME = _b(c.get("logo_follow_theme"), False)
    theme_tick(force=True)


def config_tick(weather=None):
    """Cek berkas konfigurasi (maks. tiap 2 dtk); terapkan bila berubah."""
    global _cfg_mtime, _cfg_check
    now = time.monotonic()
    if now - _cfg_check < 2.0:
        return
    _cfg_check = now
    try:
        mt = os.stat(CFG_FILE).st_mtime
    except OSError:
        mt = None
    if mt == _cfg_mtime:
        return
    _cfg_mtime = mt
    key = lambda: (WEATHER_ENABLED, WEATHER_SOURCE, tuple(WEATHER_PLACE), WEATHER_ADM4, WEATHER_LAT, WEATHER_LON)
    before = key()
    apply_config()
    if weather is not None and key() != before:
        weather.refresh()                 # lokasi/sumber berubah -> ambil cuaca baru sekarang juga


def current_theme_key(t=None):
    """Tema untuk saat ini: tetap, atau fungsi dari waktu (tanpa state => tahan restart)."""
    t = time.time() if t is None else t
    if THEME_MODE != "auto":
        return THEME_FIXED if THEME_FIXED in THEMES else "cyber"
    lst = list(THEME_LIST) or list(THEMES)
    n = len(lst)
    if n == 1:
        return lst[0]
    slot = int(t // (THEME_INTERVAL * 60))
    if THEME_ORDER == "random" and n >= 3:
        def perm(cyc):
            p = lst[:]
            random.Random(cyc * 104729 + 7).shuffle(p)
            return p
        cyc, i = divmod(slot, n)
        p = perm(cyc)
        if cyc > 0 and p[0] == perm(cyc - 1)[-1]:      # jangan ulang tema yang sama berturut-turut
            p[0], p[1] = p[1], p[0]
        return p[i]
    return lst[slot % n]


def warm_logo():
    """Siapkan cache gaya logo yang aktif (gambar, glow, tabung neon) agar scene logo langsung mulus."""
    st = active_logo_style()
    if st == "sticker":
        _art_sticker()
    elif st == "mark":
        _art_mark()
    elif st == "neon":
        _art_neon()


def theme_tick(force=False):
    """Ganti tema bila sudah waktunya. Pergantian otomatis menunggu saat animasi scene tidak berjalan."""
    key = current_theme_key()
    if key == THEME_KEY:
        return
    if force or not animating():
        old = _last_frame
        apply_theme(key)
        try:
            _logo_style()             # siapkan latar/sprite logo saat idle (hindari jeda di scene logo)
            warm_logo()
        except Exception:
            pass
        if old is not None:
            _fade["t0"], _fade["img"] = time.time(), old        # crossfade halus dari tampilan lama


def build_frame(m, wx=None):
    global _last_frame
    theme_tick()
    img = _build_frame(m, wx)
    if _fade["img"] is not None:
        f = (time.time() - _fade["t0"]) / THEME_FADE
        if f < 1.0:
            img = Image.blend(_fade["img"], img, _ease_io(_clamp(f)))
        else:
            _fade["img"] = None
    _last_frame = img
    return img


running = True


def stop(*_):
    global running
    running = False


def sample_metrics():
    return {
        "ip": "192.168.1.1", "cpu": 35, "cpu_hist": deque([62, 55, 48, 52, 40, 33, 38, 30, 41, 36, 31, 35]),
        "ram_total": 512, "ram_used": 318, "ram_pct": 62.1, "temp": 48, "load": "0.72",
        "uptime": "2d 03:41", "gen": "4G", "rssi": -75, "sig_label": "RSSI",
        "wan_name": "wwan0", "is_cell": True, "wan_up": "10.2Mbps", "wan_down": "68.5Mbps",
        "lan_up": "1.2 MB/s", "lan_down": "8.4 MB/s", "frame": 0,
        "leases": [("192.168.1.10", "Laptop"), ("192.168.1.55", "Phone"), ("192.168.1.60", "TV")] * 4,
    }


def main():
    config_tick(None)
    if "--preview" in sys.argv:
        i = sys.argv.index("--preview")
        out = sys.argv[i + 1] if len(sys.argv) > i + 1 else "preview.png"
        img = render(sample_metrics())
        img.save(out)
        img.resize((WIDTH * 3, HEIGHT * 3), Image.NEAREST).save(out.replace(".png", "_3x.png"))
        print("Preview disimpan:", out)
        return

    if "--cek-cuaca" in sys.argv:
        print(json.dumps(Weather()._fetch(), indent=2, ensure_ascii=False))
        return
    if "--lokasi" in sys.argv:
        i = sys.argv.index("--lokasi")
        parts = [p.strip() for p in " ".join(sys.argv[i + 1:]).split(",")]
        if len(parts) not in (2, 3) or not all(parts):
            print('Pakai: --lokasi "Desa, Kabupaten"  atau  "Desa, Kecamatan, Kabupaten"')
            print('Contoh: --lokasi "Tempursari, Lumajang"')
            return
        desa, kec, kab = parts[0], (parts[1] if len(parts) == 3 else None), parts[-1]
        print("Mengunduh daftar kode wilayah (sekali saja, beberapa MB)...")
        c = find_adm4(desa, kab, kec)
        if not c:
            print(f"Tidak ditemukan: {desa}, {kab}. Cek ejaan (huruf besar/kecil bebas).")
            return
        if len(c) > 1:
            print("Ada lebih dari satu hasil, ulangi dengan menambah kecamatan:")
            for code, dn, kn, bn in c:
                print(f'  {code}  {dn}, Kec. {kn}, {bn}   ->  --lokasi "{dn}, {kn}, {kab}"')
            return
        code, dn, kn, bn = c[0]
        try:
            with open(LOCATION_CACHE, "w") as f:
                json.dump({"desa": desa, "kec": kec or "", "kab": kab, "adm4": code}, f)
        except OSError:
            pass
        if uci_set({"desa": desa, "kec": kec or "", "kab": kab, "adm4": ""}):
            print(f"Lokasi disimpan di {CFG_FILE} (terlihat juga di LuCI): {dn}, Kec. {kn}, {bn} -> {code}")
        else:
            with open(LOCATION_FILE, "w") as f:
                json.dump({"desa": desa, "kec": kec or "", "kab": kab, "adm4": code}, f)
            print(f"Lokasi disimpan di {LOCATION_FILE}: {dn}, Kec. {kn}, {bn} -> {code}")
        print("Berlaku otomatis dalam beberapa detik, tanpa restart.")
        return
    if "--preview-logo" in sys.argv:
        i = sys.argv.index("--preview-logo")
        global LOGO_MODE, LOGO_FIXED
        LOGO_MODE = "fixed"
        for arg in sys.argv[i + 1:]:
            if arg in THEMES:
                apply_theme(arg)
            elif arg in LOGO_STYLES:
                LOGO_FIXED = arg
        st = active_logo_style()
        SCENE_LEN["logo"] = 10.0
        us = ((0.4, 1.2, 2.0, 2.8, 3.5, 4.3, 5.0, 5.8, 6.6, 7.5, 8.2, 9.6) if st == "cinematic"
              else (0.1, 0.3, 0.55, 0.8, 1.1, 1.4, 1.8, 2.2, 2.6, 3.2, 4.6, 6.0))
        sheet = Image.new("RGB", (WIDTH * 4, HEIGHT * 3))
        for n, u in enumerate(us):
            sheet.paste(render_logo(None, u), ((n % 4) * WIDTH, (n // 4) * HEIGHT))
        sheet.save(f"logo_preview_{st}.png")
        print(f"Preview disimpan: logo_preview_{st}.png  (pakai: --preview-logo [tema] [{'|'.join(LOGO_STYLES)}])")
        return
    if "--preview-theme" in sys.argv:
        keys = list(THEMES)
        sheet = Image.new("RGB", (WIDTH * len(keys), HEIGHT * 3))
        for n, k in enumerate(keys):
            apply_theme(k)
            sheet.paste(render(sample_metrics()), (n * WIDTH, 0))
            sheet.paste(render_secondary_dashboard(sample_metrics(), sample_weather("partly", True)), (n * WIDTH, HEIGHT))
            sheet.paste(render_logo(None, 9.2), (n * WIDTH, 2 * HEIGHT))
        sheet.save("themes_preview.png")
        print("Preview disimpan: themes_preview.png  ->", ", ".join(keys))
        return
    if "--preview-weather" in sys.argv:
        i = sys.argv.index("--preview-weather")
        which = sys.argv[i + 1] if len(sys.argv) > i + 1 and not sys.argv[i + 1].startswith("-") else "all"
        for k in (WEATHER_KINDS if which == "all" else (which,)):
            for day in (True, False):
                fn = f"weather_{k}_{'day' if day else 'night'}.png"
                render_secondary_dashboard(sample_metrics(), sample_weather(k, day)).save(fn)
                print("Preview disimpan:", fn)
        return

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("Starting DejedeMon (monitor hardware & cuaca TFT 240x240)...")

    try:
        mode = os.stat(SPI_DEV).st_mode
    except FileNotFoundError:
        print(f"{SPI_DEV} tidak ada. SPI belum aktif di device tree.")
        raise SystemExit(1)
    if not stat.S_ISCHR(mode):
        print(f"{SPI_DEV} bukan device SPI asli.")
        raise SystemExit(1)

    try:
        gpio = Gpio(GPIO_CHIP, [PIN_DC, PIN_RST, PIN_BLK])
    except Exception as e:
        print(f"Gagal init GPIO: {e!r}")
        raise SystemExit(1)

    try:
        lcd = ST7789(SPI_DEV, gpio, PIN_DC, PIN_RST, PIN_BLK, ROTATION)
    except Exception as e:
        print(f"Gagal konfigurasi SPI: {e!r}")
        raise SystemExit(1)

    lcd.init()
    try:                                     
        lcd.show(render_minimal({"ip": "starting...", "cpu": 0, "ram_used": 0,
                                 "ram_total": 0, "uptime": "--"}))
        print("Splash terkirim ke layar.")
    except Exception:
        print("Splash GAGAL:\n" + traceback.format_exc())

    try:
        _logo_style()
        warm_logo()
    except Exception:
        print("Pemanasan logo gagal:\n" + traceback.format_exc())
    modem = Modem()
    modem.start()
    weather = Weather()
    weather.start()

    prev_total, prev_idle = read_cpu()
    prev_lan = read_net(LAN_IF)
    prev_wan_name = WAN_IF or default_iface()
    prev_wan = read_net(prev_wan_name) if prev_wan_name else None
    prev_t = time.monotonic()
    cpu_hist = deque(maxlen=12)
    frame = 0
    metrics = None
    last_sample = 0.0

    while running:
        loop_start = time.monotonic()
        try:
            config_tick(weather)
            if metrics is None or loop_start - last_sample >= INTERVAL - 0.05:
                total, idle = safe(read_cpu, (prev_total, prev_idle))
                dt_total, dt_idle = total - prev_total, idle - prev_idle
                cpu = 100.0 * (dt_total - dt_idle) / dt_total if dt_total > 0 else 0.0
                prev_total, prev_idle = total, idle
                cpu_hist.append(cpu)

                ram_total, ram_used = safe(read_mem, (0, 0))

                now = time.monotonic()
                dt = now - prev_t
                wan_name = WAN_IF or default_iface()
                lan = read_net(LAN_IF)
                wan = read_net(wan_name) if wan_name else None
                lan_up = lan_down = wan_up = wan_down = "N/A"
                if lan and prev_lan and dt > 0:
                    lan_up = fmt_mbs(max(0, lan[0] - prev_lan[0]) / dt)
                    lan_down = fmt_mbs(max(0, lan[1] - prev_lan[1]) / dt)
                if wan and prev_wan and wan_name == prev_wan_name and dt > 0:
                    wan_up = fmt_mbit(max(0, wan[1] - prev_wan[1]) / dt)
                    wan_down = fmt_mbit(max(0, wan[0] - prev_wan[0]) / dt)
                prev_lan, prev_wan, prev_wan_name, prev_t = lan, wan, wan_name, now

                sig = modem.data
                is_cell = bool(wan_name) and (wan_name.startswith(CELL_PREFIX)
                                              or sig["rssi"] is not None)
                metrics = ({
                    "ip": safe(get_ip, "N/A", LAN_IF),
                    "cpu": cpu,
                    "cpu_hist": cpu_hist,
                    "ram_used": ram_used,
                    "ram_total": ram_total,
                    "ram_pct": (100.0 * ram_used / ram_total) if ram_total else 0.0,
                    "temp": read_temp(),
                    "load": safe(read_loadavg, "0.00"),
                    "uptime": safe(read_uptime, "--"),
                    "gen": sig["gen"],
                    "rssi": sig["rssi"],
                    "sig_label": sig["label"],
                    "wan_name": wan_name or "",
                    "is_cell": is_cell,
                    "wan_up": wan_up,
                    "wan_down": wan_down,
                    "lan_up": lan_up,
                    "lan_down": lan_down,
                    "leases": read_leases(),
                    "frame": frame,
                })
                last_sample = loop_start
                frame += 1
            img = build_frame(metrics, weather.snapshot())
            lcd.show(img)
        except Exception:
            print("Error loop:\n" + traceback.format_exc())

        if animating():
            wait = FAST_INTERVAL
        else:                                  # bangun tepat di pergantian menit agar scene 2 tidak telat
            wait = min(INTERVAL, SCENE_PERIOD - (time.time() % SCENE_PERIOD) + 0.02)
        while running and time.monotonic() - loop_start < wait:
            time.sleep(0.02)

    modem.stop()
    weather.stop()
    try:
        lcd.show(Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0)))
        lcd.backlight(False)
    except Exception:
        pass
    lcd.close()
    gpio.close()


if __name__ == "__main__":
    main()