"""
fetch_all.py — Mengambil data harian lalu membandingkannya dengan aset Pertagas.

Sumber:
  * Hotspot  : SiPongi+ (KLHK/Kemenhut, satelit NASA MODIS & VIIRS) — cadangan: NASA FIRMS (butuh FIRMS_MAP_KEY)
  * Gempa    : BMKG InaTEWS open data (autogempa, gempaterkini M5+, gempadirasakan)
  * Cuaca    : BMKG API prakiraan cuaca (jika kode adm4 diisi) — cadangan/pelengkap: Open-Meteo
  * Udara    : Open-Meteo Air Quality (PM2.5, PM10) — indikator asap karhutla
  * Gerakan tanah : PVMBG — zona kerentanan & prakiraan bulanan (ESDM One Map) → scripts/gerakan_tanah.py

Hasil:
  data/latest.json                  → dibaca dashboard
  data/history/ringkasan.csv        → tren harian per area
  data/history/ringkasan.json       → tren 120 hari terakhir untuk grafik
  data/history/hotspot_YYYY-MM-DD.json → arsip hotspot dekat aset (bukti audit)
  data/gerakan_tanah/*.geojson       → zona kerentanan & prakiraan gerakan tanah PVMBG di sekitar aset

Setiap sumber berdiri sendiri: bila satu gagal, data lama sumber itu dipertahankan
dan ditandai "stale" sehingga dashboard tetap tampil.
"""
import contextlib
import csv
import io
import json
import math
import os
import re
import signal
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import nearest_points, transform, unary_union
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo import BBOX_INDONESIA, IndeksTitik, classify, hari_berulang, haversine, perbaiki_latlon, valid_ll  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HIST = DATA / "history"
CFG = json.loads((ROOT / "config" / "monitoring.json").read_text(encoding="utf-8"))
WIB = timezone(timedelta(hours=7))
NOW = datetime.now(WIB)
KUALITAS = {}  # catatan validasi data run ini (koordinat invalid/tertukar, duplikat) → latest.json
UA = {"User-Agent": "Pertagas-QMHSE-EnvDashboard/1.0 (+github actions; monitoring aset pipa)",
      "Accept": "application/json,text/plain,*/*"}

TO_M = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
TO_LL = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform


def log(*a):
    print(f"[{datetime.now(WIB):%H:%M:%S}]", *a, flush=True)


# Batas waktu total satu kali jalan. Bila habis, sumber yang belum diambil dilewati
# dan data lama dipakai — supaya satu server yang macet tidak menggagalkan semuanya.
T0 = time.time()
BUDGET_S = float(os.environ.get("BUDGET_MENIT", "12")) * 60
CONNECT_TIMEOUT = 15


class WaktuHabis(TimeoutError):
    pass


def sisa_waktu():
    return BUDGET_S - (time.time() - T0)


@contextlib.contextmanager
def batas_waktu(detik):
    """Hentikan blok bila melebihi `detik` (hanya di thread utama Linux/macOS; selain itu diabaikan)."""
    if not hasattr(signal, "SIGALRM") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _habis(signum, frame):
        raise requests.exceptions.Timeout(f"permintaan melebihi {detik:.0f} detik")
    lama = signal.signal(signal.SIGALRM, _habis)
    signal.setitimer(signal.ITIMER_REAL, max(1.0, detik))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, lama)


def get(url, params=None, headers=None, timeout=45, tries=2):
    """GET dengan batas koneksi 15 dtk, batas baca `timeout`, maks. `tries` percobaan,
    dan tidak pernah melewati sisa anggaran waktu."""
    last = None
    for i in range(tries):
        left = sisa_waktu()
        if left < 20:
            raise WaktuHabis("anggaran waktu habis, sumber dilewati")
        try:
            batas = min(timeout, left - 5)
            # Batas TOTAL waktu satu permintaan (koneksi + unduh). Server yang mengirim data
            # sangat pelan tidak memicu read-timeout biasa, jadi dipakai alarm sistem.
            with batas_waktu(batas):
                r = requests.get(url, params=params, headers={**UA, **(headers or {})},
                                 timeout=(CONNECT_TIMEOUT, batas))
            r.raise_for_status()
            return r
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError) as e:
            last = e
            log(f"  server tidak merespons/terlalu lambat ({type(e).__name__}: {str(e)[:80]}) — tidak dicoba ulang")
            break
        except Exception as e:  # noqa
            last = e
            log(f"  percobaan {i+1} gagal: {str(e)[:160]}")
            time.sleep(3)
    raise last


def fnum(v):
    try:
        return float(str(v).replace(",", ".").split()[0])
    except Exception:
        return None


# ---------------------------------------------------------------- aset
class Assets:
    def __init__(self):
        fc = json.loads((DATA / "assets.geojson").read_text(encoding="utf-8"))
        self.areas = fc.get("areas", {})
        self.items = []  # (geom_m, props)
        for f in fc["features"]:
            g = transform(TO_M, shape(f["geometry"]))
            self.items.append((g, f["properties"]))
        self.geoms = [g for g, _ in self.items]
        self.tree = STRtree(self.geoms)
        allg = unary_union(self.geoms)
        minx, miny, maxx, maxy = transform(TO_LL, allg).bounds
        self.bbox = (minx, miny, maxx, maxy)
        # indeks per wilayah untuk analisis jarak per tab wilayah
        self.by_area = {}
        for code in {p["area"] for _, p in self.items}:
            its = [(g, p) for g, p in self.items if p["area"] == code]
            self.by_area[code] = (STRtree([g for g, _ in its]), its)

    def nearest(self, lat, lon, area=None):
        """Kembalikan (jarak_km, props aset terdekat) — seluruh aset atau satu wilayah."""
        pt = Point(TO_M(lon, lat))
        tree, items = (self.tree, self.items) if area is None else self.by_area[area]
        idx = tree.nearest(pt)
        g, props = items[int(idx)]
        _, q = nearest_points(pt, g)
        qlon, qlat = TO_LL(q.x, q.y)
        return haversine(lat, lon, qlat, qlon), props

    def nearest_facility(self, lat, lon, area=None):
        best = (1e9, None)
        for g, p in self.items:
            if p["kind"] != "fasilitas" or (area and p["area"] != area):
                continue
            qlon, qlat = TO_LL(g.x, g.y)
            d = haversine(lat, lon, qlat, qlon)
            if d < best[0]:
                best = (d, p)
        return best

    def in_bbox(self, lat, lon, pad_deg):
        x0, y0, x1, y1 = self.bbox
        return x0 - pad_deg <= lon <= x1 + pad_deg and y0 - pad_deg <= lat <= y1 + pad_deg


# ---------------------------------------------------------------- hotspot
def fetch_sipongi():
    h = CFG["hotspot"]
    params = [("wilayah", "IN"), ("filterperiode", "false"), ("late", str(h.get("periode_jam", 24)))]
    params += [("satelit[]", s) for s in h["satelit"]]
    params += [("confidence[]", c) for c in h["confidence"]]
    params += [("provinsi", ""), ("kabkota", "")]
    r = get(h["sipongi_endpoint"], params=params, headers={"Referer": h["sipongi_referer"]}, timeout=90)
    js = r.json()
    feats = js.get("features") if isinstance(js, dict) else js
    if feats is None and isinstance(js, dict):
        feats = js.get("data") or []
    if not isinstance(feats, list):
        raise ValueError(f"format respons SiPongi tidak dikenal: {str(js)[:120]}")
    out = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        p = f.get("properties", f)
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or [p.get("long") or p.get("lon"), p.get("lat")]
        if not isinstance(coords, (list, tuple)) or len(coords) < 2:
            continue
        lat, lon, cek = perbaiki_latlon(fnum(coords[1]), fnum(coords[0]))
        if cek != "ok":
            KUALITAS["hotspot_" + cek] = KUALITAS.get("hotspot_" + cek, 0) + 1
        if lat is None:
            continue
        conf = str(p.get("confidence_level") or "").lower() or "unknown"
        out.append({
            "lat": round(lat, 5), "lon": round(lon, 5),
            "conf": conf, "sumber": p.get("sumber") or "",
            "waktu_utc": p.get("date_hotspot_ori") or "",
            "waktu": p.get("date_hotspot") or "",
            "prov": p.get("nama_provinsi") or "", "kab": p.get("kabkota") or "",
            "kec": p.get("kecamatan") or "", "desa": p.get("desa") or "",
        })
    return out


def fetch_firms(bbox):
    key = os.environ.get("FIRMS_MAP_KEY", "").strip()
    if not key:
        raise RuntimeError("FIRMS_MAP_KEY belum diisi (opsional)")
    x0, y0, x1, y1 = bbox
    area = f"{x0-0.5:.2f},{y0-0.5:.2f},{x1+0.5:.2f},{y1+0.5:.2f}"
    out = []
    for src in CFG["hotspot"]["firms_sources"]:
        url = f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{src}/{area}/1"
        r = get(url, timeout=60)
        for row in csv.DictReader(io.StringIO(r.text)):
            c = str(row.get("confidence", "")).lower()
            conf = {"h": "high", "n": "medium", "l": "low"}.get(c)
            if conf is None:
                v = fnum(c) or 0
                conf = "low" if v < 30 else ("medium" if v < 80 else "high")
            t = f"{row.get('acq_date')}T{str(row.get('acq_time','0')).zfill(4)[:2]}:{str(row.get('acq_time','0')).zfill(4)[2:]}:00Z"
            lat, lon, cek = perbaiki_latlon(fnum(row.get("latitude")), fnum(row.get("longitude")))
            if lat is None:
                KUALITAS["hotspot_invalid"] = KUALITAS.get("hotspot_invalid", 0) + 1
                continue
            out.append({"lat": round(lat, 5), "lon": round(lon, 5),
                        "conf": conf, "sumber": "FIRMS-" + src, "waktu_utc": t, "waktu": t,
                        "prov": "", "kab": "", "kec": "", "desa": ""})
    return out


def dedupe_hotspots(raw):
    """Buang rekaman identik (lokasi, waktu akuisisi & satelit sama) — mis. bila SiPongi & FIRMS digabung."""
    seen, out = set(), []
    for hs in raw:
        k = (round(hs["lat"], 4), round(hs["lon"], 4), str(hs.get("waktu_utc"))[:16], str(hs.get("sumber")).split("-")[-1].upper())
        if k in seen:
            continue
        seen.add(k)
        out.append(hs)
    if len(out) < len(raw):
        KUALITAS["hotspot_duplikat_dibuang"] = len(raw) - len(out)
    return out


def arsip_hotspot(hari):
    """Deteksi hotspot dekat aset dari arsip harian `hari` hari terakhir (tanpa duplikat)."""
    batas = (NOW - timedelta(days=hari)).strftime("%Y-%m-%d")
    seen, out = set(), []
    for f in sorted(HIST.glob("hotspot_*.json")):
        tgl = f.stem.replace("hotspot_", "")
        if tgl < batas:
            continue
        try:
            for t in json.loads(f.read_text(encoding="utf-8")):
                k = (t.get("lat"), t.get("lon"), t.get("waktu_utc"))
                if valid_ll(t.get("lat"), t.get("lon")) and k not in seen:
                    seen.add(k)
                    out.append(t)
        except Exception as e:  # noqa — arsip rusak tidak boleh menggagalkan run
            log(f"  arsip {f.name} dilewati: {e}")
    return out


def analyse_hotspots(raw, assets):
    h = CFG["hotspot"]
    radii = h["radius_km"]
    keep = h.get("simpan_hotspot_sampai_km", 25)
    pc = h.get("persisten", {})
    p_hari, p_r, p_min = pc.get("jendela_hari", 7), pc.get("radius_km", 1.0), pc.get("min_hari", 3)
    pad = keep / 100.0 + 0.1
    near, per_prov = [], {}
    raw = dedupe_hotspots(raw)
    for hs in raw:
        if hs["prov"]:
            per_prov[hs["prov"]] = per_prov.get(hs["prov"], 0) + 1
        if not assets.in_bbox(hs["lat"], hs["lon"], pad):
            continue
        d, props = assets.nearest(hs["lat"], hs["lon"])
        if d > keep:
            continue
        fd, fp = assets.nearest_facility(hs["lat"], hs["lon"])
        near.append({**hs, "jarak_km": round(d, 2), "area": props["area"],
                     "aset": props["name"], "jenis_aset": props["kind"],
                     "fasilitas": fp["name"] if fp else "", "jarak_fasilitas_km": round(fd, 1) if fp else None,
                     "status": classify(d, radii)})
    # Titik panas berulang: deteksi di lokasi yang sama (≤ p_r km) pada ≥ p_min tanggal berbeda dalam
    # p_hari hari terakhir. Bisa sumber panas industri/flare, atau kebakaran yang berlangsung lama
    # (mis. gambut) — TIDAK disembunyikan, hanya diberi tanda supaya diverifikasi.
    idx = IndeksTitik(arsip_hotspot(p_hari) + near)
    for x in near:
        x["berulang_hari"] = hari_berulang(x, idx, p_r)
        x["persisten"] = x["berulang_hari"] >= p_min
    near.sort(key=lambda x: x["jarak_km"])
    return near, dict(sorted(per_prov.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------- gempa
BMKG_TEWS = "https://data.bmkg.go.id/DataMKG/TEWS/"


def tsunami_dari_potensi(teks):
    """True hanya bila teks BMKG menyatakan berpotensi tsunami (bukan 'Tidak berpotensi tsunami')."""
    t = re.sub(r"\s+", " ", str(teks or "")).strip().lower()
    return bool(re.search(r"(?<!tidak )berpotensi tsunami", t))


def parse_gempa(g, assets):
    try:
        lat, lon = [fnum(x) for x in str(g["Coordinates"]).split(",")[:2]]
    except (KeyError, ValueError):
        lat = lon = None
    if not valid_ll(lat, lon):
        # BMKG: Coordinates = "lat,lon". Bila kosong/rusak pakai Lintang/Bujur teks (mis. "3.14 LS").
        def dari_teks(v, neg):
            x = fnum(v)
            return None if x is None else (-x if neg in str(v).upper() else x)
        lat, lon = dari_teks(g.get("Lintang"), "LS"), dari_teks(g.get("Bujur"), "BB")
    if not valid_ll(lat, lon):
        raise ValueError(f"koordinat gempa tidak valid: {g.get('Coordinates')}")
    d, props = assets.nearest(lat, lon)
    fd, fp = assets.nearest_facility(lat, lon)
    mag = fnum(g.get("Magnitude"))
    radii = CFG["gempa"]["radius_km"]

    def status_of(dist):
        s_ = classify(dist, radii)
        if s_ == "kritis" and (mag is None or mag < CFG["gempa"].get("magnitudo_min_kritis", 5)):
            s_ = "waspada"
        return s_

    st = status_of(d)
    per_area = {}
    for code in assets.by_area:
        da, pa = assets.nearest(lat, lon, code)
        fda, fpa = assets.nearest_facility(lat, lon, code)
        per_area[code] = {"jarak_km": round(da, 1), "aset": pa["name"],
                          "fasilitas": fpa["name"] if fpa else "", "status": status_of(da)}
    potensi = g.get("Potensi", "") or ""
    tsunami = tsunami_dari_potensi(potensi)
    return {
        "waktu": f"{g.get('Tanggal','')} {g.get('Jam','')}", "datetime": g.get("DateTime"),
        "lat": lat, "lon": lon, "mag": mag, "kedalaman": g.get("Kedalaman"),
        "wilayah": g.get("Wilayah"), "potensi": potensi, "dirasakan": g.get("Dirasakan", ""),
        "shakemap": (BMKG_TEWS + g["Shakemap"]) if g.get("Shakemap") else "",
        "tsunami": tsunami, "jarak_km": round(d, 1), "area": props["area"],
        "aset": props["name"], "jenis_aset": props["kind"],
        "fasilitas": fp["name"] if fp else "", "jarak_fasilitas_km": round(fd, 1) if fp else None, "status": st,
        "per_area": per_area,
    }


def fetch_gempa(assets, prev):
    """Tiga daftar BMKG diambil terpisah: bila satu gagal, daftar lain tetap dipakai dan daftar
    yang gagal memakai data sebelumnya (ditandai di `gagal`)."""
    out, gagal = {}, {}
    for key, fn in [("terbaru", "autogempa.json"), ("terkini", "gempaterkini.json"), ("dirasakan", "gempadirasakan.json")]:
        try:
            js = get(BMKG_TEWS + fn).json()["Infogempa"]["gempa"]
            lst = js if isinstance(js, list) else [js]
            hasil = []
            for g in lst:
                try:
                    hasil.append(parse_gempa(g, assets))
                except Exception as e:  # noqa — satu rekaman rusak tidak menggagalkan daftar
                    KUALITAS["gempa_dilewati"] = KUALITAS.get("gempa_dilewati", 0) + 1
                    log(f"  rekaman gempa dilewati: {e}")
            out[key] = hasil[0] if key == "terbaru" else hasil
            if key == "terbaru" and not hasil:
                out[key] = None
        except Exception as e:  # noqa
            gagal[key] = str(e)[:160]
            log(f"  BMKG {fn} gagal: {e}")
            out[key] = prev.get(key) if key == "terbaru" else (prev.get(key) or [])
    if len(gagal) == 3:
        raise RuntimeError("semua data gempa BMKG gagal: " + "; ".join(gagal.values()))
    out["gagal"] = gagal
    return out


# ---------------------------------------------------------------- cuaca
WMO = {0: "Cerah", 1: "Cerah berawan", 2: "Berawan sebagian", 3: "Berawan tebal", 45: "Berkabut", 48: "Kabut beku",
       51: "Gerimis ringan", 53: "Gerimis", 55: "Gerimis lebat", 61: "Hujan ringan", 63: "Hujan sedang", 65: "Hujan lebat",
       80: "Hujan lokal ringan", 81: "Hujan lokal sedang", 82: "Hujan lokal lebat", 95: "Hujan petir", 96: "Hujan petir + es", 99: "Hujan petir + es"}
ARAH = ["U", "TL", "T", "TG", "S", "BD", "B", "BL"]


def arah_angin(deg):
    return "-" if deg is None else ARAH[int((deg % 360) / 45 + 0.5) % 8]


def fetch_openmeteo(points):
    lat = ",".join(str(p["lat"]) for p in points)
    lon = ",".join(str(p["lon"]) for p in points)
    w = get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": lat, "longitude": lon, "timezone": "Asia/Jakarta", "forecast_days": 3, "wind_speed_unit": "kmh",
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weather_code,wind_speed_10m_max",
    }).json()
    w = w if isinstance(w, list) else [w]
    if len(w) != len(points) or any(not isinstance(x, dict) or "current" not in x for x in w):
        raise ValueError("respons Open-Meteo tidak lengkap/tidak sesuai jumlah titik")
    try:
        aq = get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
            "latitude": lat, "longitude": lon, "timezone": "Asia/Jakarta", "current": "pm2_5,pm10,us_aqi"}).json()
        aq = aq if isinstance(aq, list) else [aq]
        if len(aq) != len(points):
            raise ValueError("jumlah titik kualitas udara tidak sesuai")
    except Exception as e:  # noqa
        log("  kualitas udara gagal:", e)
        aq = [{}] * len(points)
    res = []
    for p, wi, ai in zip(points, w, aq):
        c = wi.get("current", {})
        d = wi.get("daily", {})
        a = (ai or {}).get("current", {})
        res.append({
            **{k: p[k] for k in ("area", "nama", "lat", "lon")},
            # Open-Meteo = keluaran MODEL cuaca (bukan stasiun pengamatan). `waktu` = jam data (WIB),
            # nilai "current" adalah data 15-menitan; hujan = akumulasi 15 menit sebelumnya.
            "sumber": "Open-Meteo", "jenis": "model", "waktu": c.get("time"),
            "pm_sumber": "CAMS global (~45 km) via Open-Meteo" if a else None, "pm_waktu": a.get("time"),
            "suhu": c.get("temperature_2m"), "terasa": c.get("apparent_temperature"),
            "rh": c.get("relative_humidity_2m"), "hujan_mm": c.get("precipitation"),
            "cuaca": WMO.get(c.get("weather_code"), f"Kode {c.get('weather_code')}"),
            "angin_kmh": c.get("wind_speed_10m"), "angin_arah": arah_angin(c.get("wind_direction_10m")),
            "hembusan_kmh": c.get("wind_gusts_10m"),
            "pm25": a.get("pm2_5"), "pm10": a.get("pm10"), "aqi_us": a.get("us_aqi"),
            "prakiraan": [{"tanggal": d["time"][i], "cuaca": WMO.get(d["weather_code"][i], "-"),
                           "tmin": d["temperature_2m_min"][i], "tmax": d["temperature_2m_max"][i],
                           "hujan_mm": d["precipitation_sum"][i], "peluang_hujan": d["precipitation_probability_max"][i],
                           "angin_max": d["wind_speed_10m_max"][i]} for i in range(len(d.get("time", [])))],
        })
    return res


def bmkg_now(adm4):
    """Ambil prakiraan BMKG terdekat dengan jam sekarang untuk satu kode adm4."""
    js = get("https://api.bmkg.go.id/publik/prakiraan-cuaca", params={"adm4": adm4}, timeout=30).json()
    rows = [x for day in js["data"][0]["cuaca"] for x in day]

    def t_utc(x):
        # local_datetime mengikuti zona lokasi (WIB/WITA/WIT) → bandingkan memakai utc_datetime
        if x.get("utc_datetime"):
            return datetime.fromisoformat(x["utc_datetime"]).replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(x["local_datetime"]).replace(tzinfo=WIB)
    best = min(rows, key=lambda x: abs(t_utc(x) - NOW))
    # BMKG = PRAKIRAAN per 3 jam (jam terdekat), bukan pengamatan. ws dalam km/jam.
    return {"sumber": "BMKG", "jenis": "prakiraan", "waktu": t_utc(best).astimezone(WIB).strftime("%Y-%m-%dT%H:%M"),
            "suhu": best.get("t"), "rh": best.get("hu"),
            "cuaca": best.get("weather_desc"), "angin_kmh": best.get("ws"), "angin_arah": best.get("wd"),
            "jarak_pandang": best.get("vs_text"), "lokasi_bmkg": js.get("lokasi", {}).get("desa")}


def fetch_cuaca():
    pts = CFG["titik_cuaca"]
    res = fetch_openmeteo(pts)
    for p, r in zip(pts, res):
        if p.get("adm4"):
            try:
                r.update(bmkg_now(p["adm4"]))
                time.sleep(1.2)  # batas BMKG: 60 permintaan / menit
            except Exception as e:  # noqa
                log(f"  BMKG {p['nama']} gagal, pakai Open-Meteo: {e}")
    return res


# ---------------------------------------------------------------- riwayat
def update_history(areas, hotspots, total_nasional):
    HIST.mkdir(parents=True, exist_ok=True)
    today = NOW.strftime("%Y-%m-%d")
    path = HIST / "ringkasan.csv"
    rows = []
    if path.exists():
        rows = [r for r in csv.DictReader(path.open(encoding="utf-8")) if r["tanggal"] != today]
    for code in areas:
        c = {s: sum(1 for h in hotspots if h["area"] == code and h["status"] == s) for s in ("kritis", "waspada", "pantau")}
        # jumlah hotspot ≤ radius pantau yang berulang di lokasi sama (lihat analyse_hotspots)
        c["persisten"] = sum(1 for h in hotspots if h["area"] == code and h["status"] != "luar" and h.get("persisten"))
        rows.append({"tanggal": today, "area": code, **c, "total_nasional": total_nasional})
    rows.sort(key=lambda r: (r["tanggal"], r["area"]))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["tanggal", "area", "kritis", "waspada", "pantau", "persisten", "total_nasional"],
                           restval="")
        w.writeheader()
        w.writerows(rows)
    cutoff = (NOW - timedelta(days=120)).strftime("%Y-%m-%d")
    recent = [r for r in rows if r["tanggal"] >= cutoff]
    (HIST / "ringkasan.json").write_text(json.dumps(recent, ensure_ascii=False), encoding="utf-8")
    (HIST / f"hotspot_{today}.json").write_text(
        json.dumps([h for h in hotspots if h["status"] != "luar"], ensure_ascii=False, indent=0), encoding="utf-8")


# ---------------------------------------------------------------- main
GT_KEYS = ("pvmbg", "pvmbg_prakiraan", "pvmbg_zkgt", "magma_gertan")  # magma_gertan: modul dihapus, kunci lama dibersihkan


def main():
    """BAGIAN=utama → hotspot, gempa, cuaca · BAGIAN=gerakan_tanah → data PVMBG saja · BAGIAN=semua (bawaan)."""
    bagian = os.environ.get("BAGIAN", "semua").strip().lower()
    utama, gt = bagian in ("utama", "semua"), bagian in ("gerakan_tanah", "semua")
    log(f"Mode: {bagian} · anggaran waktu {BUDGET_S/60:.0f} menit")
    assets = Assets()
    prev = {}
    if (DATA / "latest.json").exists():
        try:
            prev = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
        except Exception as e:  # noqa — berkas lama rusak: mulai dari kosong, jangan gagal
            log("  latest.json lama tidak terbaca:", e)
    prev_status = prev.get("status_sumber", {})
    # status bagian yang tidak dijalankan kali ini dibawa dari run sebelumnya
    status = {k: v for k, v in prev_status.items() if (k in GT_KEYS) != gt} if bagian != "semua" else {}
    status.pop("magma_gertan", None)  # modul MAGMA sudah dihapus
    durasi = dict(prev.get("durasi_detik", {})) if bagian != "semua" else {}
    sekarang = NOW.isoformat(timespec="minutes")

    def catat_status(nama, ok, **kw):
        """Status per sumber: waktu percobaan terakhir + waktu terakhir BERHASIL (tidak pernah
        diganti dengan waktu sekarang bila gagal)."""
        lama = prev_status.get(nama, {})
        status[nama] = {"ok": ok, "waktu": sekarang,
                        "terakhir_ok": sekarang if ok else lama.get("terakhir_ok") or (lama.get("waktu") if lama.get("ok") else None),
                        **kw}

    out = {"generated_at": sekarang if utama else prev.get("generated_at", sekarang),
           "areas": assets.areas,
           "radius": {"hotspot": CFG["hotspot"]["radius_km"], "gempa": CFG["gempa"]["radius_km"],
                      "gempa_magnitudo_min_kritis": CFG["gempa"].get("magnitudo_min_kritis", 5.0)},
           "hotspot": {**prev.get("hotspot", {}), **({"stale": True} if utama else {})},
           "gempa": {**prev.get("gempa", {}), **({"stale": True} if utama else {})},
           "cuaca": prev.get("cuaca", []),
           "cuaca_diperbarui": prev.get("cuaca_diperbarui"),
           "gerakan_tanah": {**{k: v for k, v in prev.get("gerakan_tanah", {}).items() if k != "kejadian"},
                             **({"stale": True} if gt else {})}}
    if gt:
        out["gerakan_tanah_diperbarui"] = sekarang
    elif prev.get("gerakan_tanah_diperbarui"):
        out["gerakan_tanah_diperbarui"] = prev["gerakan_tanah_diperbarui"]
    if prev.get("demo"):
        out["demo"] = True

    def simpan():
        """Tulis latest.json setelah tiap sumber — hasil parsial tetap tersimpan."""
        out["status_sumber"] = status
        out["durasi_detik"] = durasi
        out["kualitas_data"] = KUALITAS
        tmp = DATA / "latest.json.tmp"
        tmp.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(DATA / "latest.json")  # tulis atomik: dashboard tidak pernah membaca berkas setengah jadi

    def catat(nama, t0):
        durasi[nama] = round(time.time() - t0)
        log(f"  ⏱ {nama}: {durasi[nama]} detik")

    if utama:
        # Hotspot
        t = time.time()
        raw, src = None, None
        try:
            log("Hotspot: SiPongi ...")
            raw, src = fetch_sipongi(), "SiPongi+ (Kemenhut)"
            log(f"  {len(raw)} titik nasional")
            catat_status("sipongi", True, jumlah=len(raw))
        except Exception as e:  # noqa
            catat_status("sipongi", False, pesan=str(e)[:200])
            log("  SiPongi gagal:", e)
        if raw is None or os.environ.get("FIRMS_ALWAYS") == "1":
            try:
                log("Hotspot: NASA FIRMS ...")
                f = fetch_firms(assets.bbox)
                raw = (raw or []) + f
                src = (src + " + " if src else "") + "NASA FIRMS"
                catat_status("firms", True, jumlah=len(f))
            except Exception as e:  # noqa
                catat_status("firms", False, pesan=str(e)[:200])
                log("  FIRMS:", e)
        if raw is not None:
            near, per_prov = analyse_hotspots(raw, assets)
            pantau = set(CFG["provinsi_pantauan"])
            out["hotspot"] = {"sumber": src, "periode_jam": CFG["hotspot"]["periode_jam"], "diperbarui": sekarang,
                              "jenis": "Deteksi titik panas satelit (bukan konfirmasi kebakaran)",
                              "persisten_aturan": CFG["hotspot"].get("persisten", {"jendela_hari": 7, "radius_km": 1.0, "min_hari": 3}),
                              "total_nasional": len(raw), "per_provinsi": per_prov,
                              "provinsi_pantauan": {p: per_prov.get(p, 0) for p in CFG["provinsi_pantauan"]},
                              "provinsi_area": {a: {p: per_prov.get(p, 0) for p in ps} for a, ps in CFG.get("provinsi_area", {}).items()},
                              "provinsi_lain": sum(v for k, v in per_prov.items() if k not in pantau),
                              "dekat_aset": near, "stale": False}
            update_history(assets.areas, near, len(raw))
        catat("hotspot", t)
        simpan()

        # Gempa
        t = time.time()
        try:
            log("Gempa: BMKG ...")
            g = fetch_gempa(assets, prev.get("gempa", {}))
            out["gempa"] = {**g, "sumber": "BMKG InaTEWS", "stale": bool(g["gagal"]),
                            "diperbarui": sekarang}
            catat_status("bmkg_gempa", True, **({"pesan": "sebagian gagal: " + ", ".join(g["gagal"])} if g["gagal"] else {}))
        except Exception as e:  # noqa
            traceback.print_exc()
            catat_status("bmkg_gempa", False, pesan=str(e)[:200])
        catat("gempa", t)
        simpan()

        # Cuaca
        t = time.time()
        try:
            log("Cuaca & kualitas udara ...")
            out["cuaca"] = fetch_cuaca()
            out["cuaca_diperbarui"] = sekarang
            catat_status("cuaca", True, jumlah_titik=len(out["cuaca"]))
        except Exception as e:  # noqa
            traceback.print_exc()
            catat_status("cuaca", False, pesan=str(e)[:200])
        catat("cuaca", t)
        simpan()

    if gt:
        # Gerakan tanah (PVMBG) — dijalankan di workflow tersendiri karena servernya lambat
        t = time.time()
        for k in GT_KEYS:
            status.pop(k, None)
        try:
            import gerakan_tanah
            log("Gerakan tanah (PVMBG) ...")
            out["gerakan_tanah"], st = gerakan_tanah.run({"get": get, "log": log, "CFG": CFG, "ROOT": ROOT, "DATA": DATA,
                                                          "NOW": NOW, "assets": assets, "prev": prev,
                                                          "sisa_waktu": sisa_waktu})
            for k, v in st.items():
                catat_status(k, v.pop("ok"), **v)
        except Exception as e:  # noqa
            traceback.print_exc()
            catat_status("pvmbg", False, pesan=str(e)[:200])
        catat("gerakan_tanah", t)

    # Selama masih ada bagian yang berasal dari data contoh, tetap tandai sebagai demo
    if prev.get("demo") and utama:
        fresh = all(status.get(k, {}).get("ok") for k in ("bmkg_gempa", "cuaca")) and not out["hotspot"].get("stale")
        if fresh:
            out.pop("demo", None)
    simpan()
    log(f"Durasi total: {time.time() - T0:.0f} detik")
    log("Selesai →", DATA / "latest.json")
    log(json.dumps(status, ensure_ascii=False))
    jalan = [k for k in status if (k in GT_KEYS) == gt or bagian == "semua"]
    if jalan and not any(status[k].get("ok") for k in jalan):
        sys.exit("Semua sumber gagal")


if __name__ == "__main__":
    main()
