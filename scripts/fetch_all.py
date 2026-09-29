"""
fetch_all.py — Mengambil data harian lalu membandingkannya dengan aset Pertagas.

Sumber:
  * Hotspot  : SiPongi+ (KLHK/Kemenhut, satelit NASA MODIS & VIIRS) — cadangan: NASA FIRMS (butuh FIRMS_MAP_KEY)
  * Gempa    : BMKG InaTEWS open data (autogempa, gempaterkini M5+, gempadirasakan)
  * Cuaca    : BMKG API prakiraan cuaca (jika kode adm4 diisi) — cadangan/pelengkap: Open-Meteo
  * Udara    : Open-Meteo Air Quality (PM2.5, PM10) — indikator asap karhutla
  * Gerakan tanah : PVMBG — zona kerentanan & prakiraan bulanan (ESDM One Map), laporan kejadian (MAGMA) → scripts/gerakan_tanah.py

Hasil:
  data/latest.json                  → dibaca dashboard
  data/history/ringkasan.csv        → tren harian per area
  data/history/ringkasan.json       → tren 120 hari terakhir untuk grafik
  data/history/hotspot_YYYY-MM-DD.json → arsip hotspot dekat aset (bukti audit)
  data/gerakan_tanah/*.geojson       → zona kerentanan & prakiraan gerakan tanah PVMBG di sekitar aset

Setiap sumber berdiri sendiri: bila satu gagal, data lama sumber itu dipertahankan
dan ditandai "stale" sehingga dashboard tetap tampil.
"""
import csv
import io
import json
import math
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import Point, shape
from shapely.ops import nearest_points, transform, unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HIST = DATA / "history"
CFG = json.loads((ROOT / "config" / "monitoring.json").read_text(encoding="utf-8"))
WIB = timezone(timedelta(hours=7))
NOW = datetime.now(WIB)
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


def get(url, params=None, headers=None, timeout=45, tries=2):
    """GET dengan batas koneksi 15 dtk, batas baca `timeout`, maks. `tries` percobaan,
    dan tidak pernah melewati sisa anggaran waktu."""
    last = None
    for i in range(tries):
        left = sisa_waktu()
        if left < 20:
            raise WaktuHabis("anggaran waktu habis, sumber dilewati")
        try:
            r = requests.get(url, params=params, headers={**UA, **(headers or {})},
                             timeout=(CONNECT_TIMEOUT, min(timeout, left - 5)))
            r.raise_for_status()
            return r
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as e:
            last = e
            log(f"  tidak bisa terhubung ke server ({type(e).__name__}) — tidak dicoba ulang")
            break
        except Exception as e:  # noqa
            last = e
            log(f"  percobaan {i+1} gagal: {str(e)[:160]}")
            time.sleep(3)
    raise last


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


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


def classify(d, radii):
    if d <= radii["kritis"]:
        return "kritis"
    if d <= radii["waspada"]:
        return "waspada"
    if d <= radii["pantau"]:
        return "pantau"
    return "luar"


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
    out = []
    for f in feats or []:
        p = f.get("properties", f)
        geom = f.get("geometry") or {}
        coords = geom.get("coordinates") or [p.get("long") or p.get("lon"), p.get("lat")]
        lon, lat = fnum(coords[0]), fnum(coords[1])
        if lat is None or lon is None:
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
            out.append({"lat": round(float(row["latitude"]), 5), "lon": round(float(row["longitude"]), 5),
                        "conf": conf, "sumber": "FIRMS-" + src, "waktu_utc": t, "waktu": t,
                        "prov": "", "kab": "", "kec": "", "desa": ""})
    return out


def analyse_hotspots(raw, assets):
    h = CFG["hotspot"]
    radii = h["radius_km"]
    keep = h.get("simpan_hotspot_sampai_km", 25)
    pad = keep / 100.0 + 0.1
    near, per_prov = [], {}
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
    near.sort(key=lambda x: x["jarak_km"])
    return near, dict(sorted(per_prov.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------- gempa
BMKG_TEWS = "https://data.bmkg.go.id/DataMKG/TEWS/"


def parse_gempa(g, assets):
    lat, lon = [fnum(x) for x in g["Coordinates"].split(",")]
    d, props = assets.nearest(lat, lon)
    fd, fp = assets.nearest_facility(lat, lon)
    mag = fnum(g.get("Magnitude")) or 0
    radii = CFG["gempa"]["radius_km"]

    def status_of(dist):
        s_ = classify(dist, radii)
        if s_ == "kritis" and mag < CFG["gempa"].get("magnitudo_min_kritis", 5):
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
    tsunami = ("berpotensi tsunami" in potensi.lower() and "tidak" not in potensi.lower())
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


def fetch_gempa(assets):
    out = {}
    for key, fn in [("terbaru", "autogempa.json"), ("terkini", "gempaterkini.json"), ("dirasakan", "gempadirasakan.json")]:
        js = get(BMKG_TEWS + fn).json()["Infogempa"]["gempa"]
        lst = js if isinstance(js, list) else [js]
        out[key] = [parse_gempa(g, assets) for g in lst]
    out["terbaru"] = out["terbaru"][0] if out["terbaru"] else None
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
    try:
        aq = get("https://air-quality-api.open-meteo.com/v1/air-quality", params={
            "latitude": lat, "longitude": lon, "timezone": "Asia/Jakarta", "current": "pm2_5,pm10,us_aqi"}).json()
        aq = aq if isinstance(aq, list) else [aq]
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
            "sumber": "Open-Meteo", "waktu": c.get("time"),
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
    best = min(rows, key=lambda x: abs(datetime.fromisoformat(x["local_datetime"]).replace(tzinfo=WIB) - NOW))
    return {"sumber": "BMKG", "waktu": best["local_datetime"], "suhu": best.get("t"), "rh": best.get("hu"),
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
        rows.append({"tanggal": today, "area": code, **c, "total_nasional": total_nasional})
    rows.sort(key=lambda r: (r["tanggal"], r["area"]))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["tanggal", "area", "kritis", "waspada", "pantau", "total_nasional"])
        w.writeheader()
        w.writerows(rows)
    cutoff = (NOW - timedelta(days=120)).strftime("%Y-%m-%d")
    recent = [r for r in rows if r["tanggal"] >= cutoff]
    (HIST / "ringkasan.json").write_text(json.dumps(recent, ensure_ascii=False), encoding="utf-8")
    (HIST / f"hotspot_{today}.json").write_text(
        json.dumps([h for h in hotspots if h["status"] != "luar"], ensure_ascii=False, indent=0), encoding="utf-8")


# ---------------------------------------------------------------- main
def main():
    assets = Assets()
    prev = {}
    if (DATA / "latest.json").exists():
        prev = json.loads((DATA / "latest.json").read_text(encoding="utf-8"))
    status = {}
    out = {"generated_at": NOW.isoformat(timespec="minutes"), "areas": assets.areas,
           "radius": {"hotspot": CFG["hotspot"]["radius_km"], "gempa": CFG["gempa"]["radius_km"]},
           # nilai awal = data lama bertanda stale; ditimpa bila pengambilan baru berhasil
           "hotspot": {**prev.get("hotspot", {}), "stale": True},
           "gempa": {**prev.get("gempa", {}), "stale": True},
           "cuaca": prev.get("cuaca", []),
           "gerakan_tanah": {**prev.get("gerakan_tanah", {}), "stale": True}}
    if prev.get("demo"):
        out["demo"] = True

    def simpan():
        """Tulis latest.json setelah tiap sumber — hasil parsial tetap tersimpan."""
        out["status_sumber"] = status
        (DATA / "latest.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # Hotspot
    raw, src = None, None
    try:
        log("Hotspot: SiPongi ...")
        raw, src = fetch_sipongi(), "SiPongi+ (Kemenhut)"
        log(f"  {len(raw)} titik nasional")
    except Exception as e:  # noqa
        status["sipongi"] = {"ok": False, "pesan": str(e)[:200]}
        log("  SiPongi gagal:", e)
    if raw is None or os.environ.get("FIRMS_ALWAYS") == "1":
        try:
            log("Hotspot: NASA FIRMS ...")
            f = fetch_firms(assets.bbox)
            raw = (raw or []) + f
            src = (src + " + " if src else "") + "NASA FIRMS"
            status["firms"] = {"ok": True, "jumlah": len(f)}
        except Exception as e:  # noqa
            status["firms"] = {"ok": False, "pesan": str(e)[:200]}
            log("  FIRMS:", e)
    if raw is not None:
        near, per_prov = analyse_hotspots(raw, assets)
        status.setdefault("sipongi", {"ok": True, "jumlah": len(raw)})
        pantau = set(CFG["provinsi_pantauan"])
        out["hotspot"] = {"sumber": src, "periode_jam": CFG["hotspot"]["periode_jam"], "diperbarui": NOW.isoformat(timespec="minutes"),
                          "total_nasional": len(raw), "per_provinsi": per_prov,
                          "provinsi_pantauan": {p: per_prov.get(p, 0) for p in CFG["provinsi_pantauan"]},
                          "provinsi_area": {a: {p: per_prov.get(p, 0) for p in ps} for a, ps in CFG.get("provinsi_area", {}).items()},
                          "provinsi_lain": sum(v for k, v in per_prov.items() if k not in pantau),
                          "dekat_aset": near, "stale": False}
        update_history(assets.areas, near, len(raw))
    else:
        out["hotspot"] = {**prev.get("hotspot", {}), "stale": True}

    simpan()

    # Gempa
    try:
        log("Gempa: BMKG ...")
        out["gempa"] = {**fetch_gempa(assets), "sumber": "BMKG InaTEWS", "stale": False}
        status["bmkg_gempa"] = {"ok": True}
    except Exception as e:  # noqa
        traceback.print_exc()
        status["bmkg_gempa"] = {"ok": False, "pesan": str(e)[:200]}
        out["gempa"] = {**prev.get("gempa", {}), "stale": True}

    simpan()

    # Cuaca
    try:
        log("Cuaca & kualitas udara ...")
        out["cuaca"] = fetch_cuaca()
        status["cuaca"] = {"ok": True, "jumlah_titik": len(out["cuaca"])}
    except Exception as e:  # noqa
        traceback.print_exc()
        status["cuaca"] = {"ok": False, "pesan": str(e)[:200]}
        out["cuaca"] = prev.get("cuaca", [])

    simpan()

    # Gerakan tanah (PVMBG) — paling akhir karena paling berat
    try:
        import gerakan_tanah
        log("Gerakan tanah (PVMBG) ...")
        out["gerakan_tanah"], st = gerakan_tanah.run({"get": get, "log": log, "CFG": CFG, "ROOT": ROOT, "DATA": DATA,
                                                      "NOW": NOW, "assets": assets, "prev": prev,
                                                      "sisa_waktu": sisa_waktu})
        status.update(st)
    except Exception as e:  # noqa
        traceback.print_exc()
        status["pvmbg"] = {"ok": False, "pesan": str(e)[:200]}
        out["gerakan_tanah"] = {**prev.get("gerakan_tanah", {}), "stale": True}

    # Selama masih ada bagian yang berasal dari data contoh, tetap tandai sebagai demo
    if prev.get("demo"):
        fresh = all(status.get(k, {}).get("ok") for k in ("bmkg_gempa", "cuaca")) and not out["hotspot"].get("stale")
        if fresh:
            out.pop("demo", None)
        else:
            out["demo"] = True
    simpan()
    log(f"Durasi: {time.time() - T0:.0f} detik")
    log("Selesai →", DATA / "latest.json")
    log(json.dumps(status, ensure_ascii=False))
    if not any(v.get("ok") for v in status.values()):
        sys.exit("Semua sumber gagal")


if __name__ == "__main__":
    main()
