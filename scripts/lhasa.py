"""
lhasa.py — Potensi longsor harian NASA LHASA 2 dibandingkan dengan SELURUH jalur pipa & fasilitas Pertagas.

Sumber: NASA Goddard — Landslide Hazard Assessment for Situational Awareness (LHASA) versi 2
        https://maps.nccs.nasa.gov/download/landslides/latest/  (today.tif = nowcast, tomorrow.tif = prakiraan besok)
        Nilai piksel = peluang terjadinya longsor (0–1), resolusi 30 detik busur (± 1 km), dipicu curah hujan
        satelit GPM IMERG + kelembapan tanah. NASA memperbarui ± 4x sehari "best effort".

Cara kerja:
  1. Setiap jalur pipa dipecah menjadi titik sampel tiap `interval_m` meter; fasilitas dipakai apa adanya.
  2. Nilai LHASA diambil di setiap titik (maksimum piksel di koridor `koridor_piksel` sekeliling titik).
  3. Titik dikelompokkan menurut ambang NASA (rendah ≥ 0,1 · sedang ≥ 0,5 · tinggi ≥ 0,9) → panjang pipa (km)
     per tingkat, ruas pipa yang terdampak, dan fasilitas yang terdampak, per wilayah kerja.

Raster dibaca langsung dari server NASA (Cloud-Optimized GeoTIFF, hanya potongan wilayah aset yang diunduh).
Bila gagal, berkas utuh (± 130 MB) diunduh sekali lalu dibaca lokal.

Hasil:
  data/lhasa/lhasa.json        → ringkasan per wilayah (dibaca dashboard)
  data/lhasa/ruas.geojson      → ruas pipa yang masuk tingkat rendah–tinggi (lapisan peta)
  data/lhasa/riwayat.csv       → riwayat harian per wilayah (bukti audit / tren)

Jalankan:  python scripts/lhasa.py
"""
import csv
import json
import math
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import numpy as np
import requests
from shapely.geometry import shape

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "lhasa"
CFG_ALL = json.loads((ROOT / "config" / "monitoring.json").read_text(encoding="utf-8"))
WIB = timezone(timedelta(hours=7))
NOW = datetime.now(WIB)
UA = {"User-Agent": "Pertagas-QMHSE-EnvDashboard/1.0 (+github actions; monitoring aset pipa)"}

BAWAAN = {
    "produk": {
        "hari_ini": "https://maps.nccs.nasa.gov/download/landslides/latest/today.tif",
        "besok": "https://maps.nccs.nasa.gov/download/landslides/latest/tomorrow.tif",
    },
    "ambang": {"rendah": 0.1, "sedang": 0.5, "tinggi": 0.9},
    "interval_m": 250,
    "koridor_piksel": 1,
    "maks_umur_jam": 48,
    "maks_ruas_per_wilayah": 40,
}
CFG = {**BAWAAN, **CFG_ALL.get("lhasa", {})}
LEVELS = ["tinggi", "sedang", "rendah"]


def log(*a):
    print(f"[{datetime.now(WIB):%H:%M:%S}]", *a, flush=True)


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def level_of(p):
    if p is None or not np.isfinite(p):
        return None
    a = CFG["ambang"]
    if p >= a["tinggi"]:
        return "tinggi"
    if p >= a["sedang"]:
        return "sedang"
    if p >= a["rendah"]:
        return "rendah"
    return None


# ------------------------------------------------------------------ aset → titik sampel
def densify(coords, step_m):
    """Titik sampel tiap step_m sepanjang garis (lon, lat). Mengembalikan list (lon, lat, km_kumulatif)."""
    out = []
    for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
        d = haversine(y0, x0, y1, x1) * 1000
        n = max(1, int(math.ceil(d / step_m)))
        for i in range(n):
            t = i / n
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    if coords:
        out.append(tuple(coords[-1]))
    return out


def load_assets():
    fc = json.loads((DATA / "assets.geojson").read_text(encoding="utf-8"))
    lines, facs = [], []
    for f in fc["features"]:
        p, g = f["properties"], f["geometry"]
        if g["type"] in ("LineString", "MultiLineString"):
            parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
            for part in parts:
                pts = densify([tuple(c[:2]) for c in part], CFG["interval_m"])
                if len(pts) < 2:
                    continue
                seg_km = [haversine(a[1], a[0], b[1], b[0]) for a, b in zip(pts[:-1], pts[1:])]
                lines.append({"area": p["area"], "aset": p["name"], "pts": pts, "seg_km": seg_km})
        elif g["type"] == "Point":
            facs.append({"area": p["area"], "aset": p["name"], "lon": g["coordinates"][0], "lat": g["coordinates"][1]})
        else:  # poligon fasilitas → titik tengah
            c = shape(g).centroid
            facs.append({"area": p["area"], "aset": p["name"], "lon": c.x, "lat": c.y})
    return fc.get("areas", {}), lines, facs


# ------------------------------------------------------------------ raster
def info_berkas(url):
    """Tanggal pembaruan berkas di server NASA (header Last-Modified)."""
    try:
        r = requests.head(url, headers=UA, timeout=30, allow_redirects=True)
        r.raise_for_status()
        lm = r.headers.get("Last-Modified")
        return (parsedate_to_datetime(lm).astimezone(WIB) if lm else None), int(r.headers.get("Content-Length") or 0)
    except Exception as e:  # noqa
        log("  HEAD gagal:", str(e)[:120])
        return None, 0


class Sampler:
    """Membaca raster per wilayah (jendela) lalu mengambil nilai di titik-titik."""

    def __init__(self, src):
        self.src = src
        self.nodata = src.nodata
        self.cache = {}

    def window_for(self, key, lons, lats, pad_deg=0.05):
        import rasterio.windows
        if key in self.cache:
            return self.cache[key]
        b = (min(lons) - pad_deg, min(lats) - pad_deg, max(lons) + pad_deg, max(lats) + pad_deg)
        win = rasterio.windows.from_bounds(*b, transform=self.src.transform).round_offsets().round_lengths()
        win = win.intersection(rasterio.windows.Window(0, 0, self.src.width, self.src.height))
        arr = self.src.read(1, window=win, masked=False).astype("float32")
        if self.nodata is not None and np.isfinite(self.nodata):
            arr[arr == self.nodata] = np.nan
        arr[(arr < 0) | (arr > 1.0001)] = np.nan
        tr = self.src.window_transform(win)
        self.cache[key] = (arr, tr)
        return arr, tr

    def values(self, key, lons, lats):
        arr, tr = self.window_for(key, lons, lats)
        k = int(CFG["koridor_piksel"])
        inv = ~tr
        res = np.full(len(lons), np.nan, dtype="float32")
        h, w = arr.shape
        for i, (x, y) in enumerate(zip(lons, lats)):
            c, r = inv * (x, y)
            r, c = int(math.floor(r)), int(math.floor(c))
            r0, r1, c0, c1 = max(0, r - k), min(h, r + k + 1), max(0, c - k), min(w, c + k + 1)
            if r0 >= r1 or c0 >= c1:
                continue
            blk = arr[r0:r1, c0:c1]
            if np.isfinite(blk).any():
                res[i] = np.nanmax(blk)
            else:
                res[i] = 0.0  # piksel tertutup (p < 0,01 di-mask NASA) = sangat kecil
        return res


def buka_raster(url):
    """Coba baca langsung dari server (COG); bila gagal unduh utuh. Mengembalikan (dataset, path_tmp|None)."""
    import rasterio
    env = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
               GDAL_HTTP_TIMEOUT="60", GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="5",
               GDAL_HTTP_USERAGENT=UA["User-Agent"], VSI_CACHE="TRUE")
    try:
        with rasterio.Env(**env):
            ds = rasterio.open("/vsicurl/" + url)
            ds.read(1, window=rasterio.windows.Window(0, 0, 1, 1))  # uji baca
        log("  dibaca langsung dari server (COG)")
        return ds, env, None
    except Exception as e:  # noqa
        log("  baca langsung gagal, unduh utuh:", str(e)[:160])
    fd, tmp = tempfile.mkstemp(suffix=".tif")
    os.close(fd)
    with requests.get(url, headers=UA, stream=True, timeout=(20, 120)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    log(f"  terunduh {os.path.getsize(tmp)/1e6:.0f} MB")
    return rasterio.open(tmp), {}, tmp


def tanggal_dari_tag(ds):
    try:
        tags = {**ds.tags(), **ds.tags(1)}
        for k in ("TIFFTAG_DATETIME", "RangeBeginningDate", "date", "DATE", "time"):
            if tags.get(k):
                return str(tags[k])
    except Exception:  # noqa
        pass
    return None


# ------------------------------------------------------------------ analisis
def analisis(url, areas, lines, facs):
    import rasterio
    tag = None
    diperbarui, ukuran = info_berkas(url)
    ds, env, tmp = buka_raster(url)
    try:
        with rasterio.Env(**env):
            sm = Sampler(ds)
            tag = tanggal_dari_tag(ds)
            per_area, ruas_fc = {}, []
            for code in areas:
                L = [l for l in lines if l["area"] == code]
                F = [f for f in facs if f["area"] == code]
                if not L and not F:
                    continue
                allx = [p[0] for l in L for p in l["pts"]] + [f["lon"] for f in F]
                ally = [p[1] for l in L for p in l["pts"]] + [f["lat"] for f in F]
                sm.window_for(code, allx, ally)
                km = {k: 0.0 for k in LEVELS}
                total, pmax, ruas = 0.0, 0.0, []
                for l in L:
                    xs, ys = [p[0] for p in l["pts"]], [p[1] for p in l["pts"]]
                    v = sm.values(code, xs, ys)
                    # nilai ruas antar-titik = maksimum kedua ujungnya
                    sv = np.fmax(v[:-1], v[1:])
                    total += sum(l["seg_km"])
                    if np.isfinite(sv).any():
                        pmax = max(pmax, float(np.nanmax(sv)))
                    lv = [level_of(float(x)) for x in sv]
                    # kelompokkan ruas berurutan dengan tingkat yang sama
                    i = 0
                    while i < len(lv):
                        if lv[i] is None:
                            i += 1
                            continue
                        j = i
                        while j + 1 < len(lv) and lv[j + 1] == lv[i]:
                            j += 1
                        seg_km = sum(l["seg_km"][i:j + 1])
                        km[lv[i]] += seg_km
                        coords = [list(map(lambda c: round(c, 5), l["pts"][t])) for t in range(i, j + 2)]
                        mid = coords[len(coords) // 2]
                        p_seg = float(np.nanmax(sv[i:j + 1]))
                        ruas.append({"aset": l["aset"], "level": lv[i], "km": round(seg_km, 2), "p_maks": round(p_seg, 3),
                                     "lat": mid[1], "lon": mid[0]})
                        ruas_fc.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                                        "properties": {"area": code, "aset": l["aset"], "level": lv[i],
                                                       "p_maks": round(p_seg, 3), "km": round(seg_km, 2)}})
                        i = j + 1
                fas = []
                if F:
                    fv = sm.values(code, [f["lon"] for f in F], [f["lat"] for f in F])
                    for f, p in zip(F, fv):
                        if np.isfinite(p):
                            pmax = max(pmax, float(p))
                        lvl = level_of(float(p))
                        if lvl:
                            fas.append({"aset": f["aset"], "level": lvl, "p": round(float(p), 3),
                                        "lat": round(f["lat"], 5), "lon": round(f["lon"], 5)})
                rank = {k: i for i, k in enumerate(LEVELS)}
                ruas.sort(key=lambda r: (rank[r["level"]], -r["p_maks"], -r["km"]))
                fas.sort(key=lambda r: (rank[r["level"]], -r["p"]))
                worst = next((k for k in LEVELS if km[k] > 0 or any(f["level"] == k for f in fas)), None)
                per_area[code] = {"total_pipa_km": round(total, 1), "pipa_km": {k: round(v, 2) for k, v in km.items()},
                                  "p_maks": round(pmax, 3), "terburuk": worst, "jumlah_ruas": len(ruas),
                                  "ruas": ruas[:CFG["maks_ruas_per_wilayah"]], "fasilitas": fas}
                log(f"  {code}: pipa {total:.0f} km · tinggi {km['tinggi']:.1f} · sedang {km['sedang']:.1f} · "
                    f"rendah {km['rendah']:.1f} km · p maks {pmax:.2f} · fasilitas {len(fas)}")
    finally:
        ds.close()
        if tmp:
            os.remove(tmp)
    umur_jam = (NOW - diperbarui).total_seconds() / 3600 if diperbarui else None
    return {"url": url, "berkas_diperbarui": diperbarui.isoformat(timespec="minutes") if diperbarui else None,
            "tag_tanggal": tag, "umur_jam": round(umur_jam, 1) if umur_jam is not None else None,
            "kedaluwarsa": bool(umur_jam is not None and umur_jam > CFG["maks_umur_jam"]),
            "per_area": per_area}, ruas_fc


# ------------------------------------------------------------------ riwayat
def update_riwayat(hasil):
    path = OUT / "riwayat.csv"
    tgl = NOW.strftime("%Y-%m-%d")
    fields = ["tanggal", "area", "tinggi_km", "sedang_km", "rendah_km", "p_maks", "berkas_diperbarui"]
    rows = []
    if path.exists():
        rows = [r for r in csv.DictReader(path.open(encoding="utf-8")) if r["tanggal"] != tgl]
    for code, a in hasil["per_area"].items():
        rows.append({"tanggal": tgl, "area": code, "tinggi_km": a["pipa_km"]["tinggi"], "sedang_km": a["pipa_km"]["sedang"],
                     "rendah_km": a["pipa_km"]["rendah"], "p_maks": a["p_maks"], "berkas_diperbarui": hasil["berkas_diperbarui"]})
    rows.sort(key=lambda r: (r["tanggal"], r["area"]))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    prev = {}
    if (OUT / "lhasa.json").exists():
        prev = json.loads((OUT / "lhasa.json").read_text(encoding="utf-8"))
    areas, lines, facs = load_assets()
    log(f"Aset: {len(lines)} ruas pipa · {sum(len(l['pts']) for l in lines)} titik sampel · {len(facs)} fasilitas")
    out = {"sumber": "NASA LHASA 2 (Goddard Space Flight Center)", "diambil": NOW.isoformat(timespec="minutes"),
           "ambang": CFG["ambang"], "interval_m": CFG["interval_m"], "koridor_piksel": CFG["koridor_piksel"],
           "maks_umur_jam": CFG["maks_umur_jam"], "produk": {}, "status": {}}
    fc_all = []
    for key, url in CFG["produk"].items():
        log(f"LHASA {key}: {url}")
        try:
            res, fc = analisis(url, areas, lines, facs)
            res["stale"] = False
            out["produk"][key] = res
            for f in fc:
                f["properties"]["produk"] = key
            fc_all += fc
            pesan = (f"berkas NASA belum diperbarui sejak {res['berkas_diperbarui']}" if res["kedaluwarsa"] else "")
            out["status"][key] = {"ok": not res["kedaluwarsa"], "pesan": pesan}
        except Exception as e:  # noqa
            traceback.print_exc()
            out["status"][key] = {"ok": False, "pesan": str(e)[:200]}
            if key in prev.get("produk", {}):
                out["produk"][key] = {**prev["produk"][key], "stale": True}
    # ruas lama dipertahankan bila produk gagal diambil
    if (OUT / "ruas.geojson").exists():
        old = json.loads((OUT / "ruas.geojson").read_text(encoding="utf-8")).get("features", [])
        gagal = {k for k, v in out["produk"].items() if v.get("stale")}
        fc_all += [f for f in old if f["properties"].get("produk") in gagal]
    (OUT / "ruas.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": fc_all},
                                                 ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    out["durasi_detik"] = round(time.time() - t0)
    (OUT / "lhasa.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if "hari_ini" in out["produk"] and not out["produk"]["hari_ini"].get("stale"):
        update_riwayat(out["produk"]["hari_ini"])
    log(f"Selesai dalam {out['durasi_detik']} detik →", OUT / "lhasa.json")
    log(json.dumps(out["status"], ensure_ascii=False))
    if not any("per_area" in v and not v.get("stale") for v in out["produk"].values()):
        sys.exit("Semua produk LHASA gagal diambil")


if __name__ == "__main__":
    main()
