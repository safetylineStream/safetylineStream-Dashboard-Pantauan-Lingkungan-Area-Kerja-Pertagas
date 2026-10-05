"""
lhasa.py — Model potensi longsor harian internal mengikuti algoritma NASA LHASA (versi 1.1), dihitung
untuk SELURUH jalur pipa & fasilitas Pertagas.

Mengapa internal?  Layanan nowcast NASA (maps.nccs.nasa.gov) tidak bisa diakses dari GitHub Actions dan
berkasnya tidak diperbarui sejak akhir 2025. Algoritma LHASA 1.1 terbuka (github.com/nasa/LHASA, tag v1.1.1),
jadi dihitung ulang di sini dengan data yang bisa diambil setiap hari.

Algoritma LHASA 1.1 (Kirschbaum & Stanley 2018, Earth's Future):
  1. Indeks hujan anteseden 7 hari (ARI), hari terbaru diberi bobot terbesar (eksponen 2):
         ARI = Σ_{k=0..6} P_(hari-k) / (k+1)²  ÷  Σ_{k=0..6} 1/(k+1)²         [mm/hari]
  2. "Basah" bila ARI > ARI95 — persentil ke-95 ARI historis di sel itu (berkas ARI95.tif resmi NASA, 0,1°).
  3. Pohon keputusan dengan peta kerentanan longsor (kelas 1–5):
         basah & kerentanan > 2  → potensi SEDANG   (LHASA "moderate")
         basah & kerentanan > 4  → potensi TINGGI   (LHASA "high")
  Tambahan internal Pertagas (bukan bagian LHASA, untuk peringatan dini):
         ARI ≥ rasio_waspada × ARI95 & kerentanan > 2  → RENDAH / mendekati ambang

Sumber data:
  * Curah hujan harian 6 hari lalu + hari ini + besok : Open-Meteo (gratis, tanpa kunci)
  * Ambang ARI95                                      : lhasa_statis/ARI95_indonesia.tif (potongan ARI95.tif NASA)
  * Kerentanan longsor (dihitung sekali, disimpan)    : 1) peta kerentanan global NASA (Stanley & Kirschbaum 2017)
                                                        2) cadangan: kelas kemiringan lereng Copernicus DEM GLO-90
                                                        3) cadangan manual: lhasa_statis/kerentanan.tif (kelas 1–5)

Hasil:
  data/lhasa/lhasa.json             → ringkasan per wilayah (dibaca dashboard)
  data/lhasa/ruas.geojson           → ruas pipa berstatus rendah–tinggi (lapisan peta)
  data/lhasa/riwayat.csv            → riwayat harian per wilayah (bukti audit)
  data/lhasa/kerentanan_cache.json  → kelas kerentanan tiap titik sampel (dihitung ulang bila aset berubah)
"""
import csv
import hashlib
import json
import math
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo import haversine  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "lhasa"
STATIS = ROOT / "lhasa_statis"
CFG_ALL = json.loads((ROOT / "config" / "monitoring.json").read_text(encoding="utf-8"))
WIB = timezone(timedelta(hours=7))
NOW = datetime.now(WIB)
UA = {"User-Agent": "Pertagas-QMHSE-EnvDashboard/1.0 (+github actions; monitoring aset pipa)"}

BAWAAN = {
    "interval_m": 250,
    "koridor_piksel": 1,
    "rasio_waspada": 0.75,
    "kerentanan_sumber": ["manual", "nasa", "dem"],
    "kerentanan_nasa_url": "https://gpm.nasa.gov/sites/default/files/downloads/global-landslide-susceptibility-map-2-27-23.tif",
    "dem_url": "https://copernicus-dem-90m.s3.amazonaws.com/{n}/{n}.tif",
    "kelas_lereng_derajat": [5, 10, 15, 25],
    "openmeteo_url": "https://api.open-meteo.com/v1/forecast",
    "maks_ruas_per_wilayah": 40,
}
CFG = {**BAWAAN, **{k: v for k, v in CFG_ALL.get("lhasa", {}).items() if not k.startswith("_")}}
LEVELS = ["tinggi", "sedang", "rendah"]
W = np.array([1 / (k + 1) ** 2 for k in range(7)])  # bobot LHASA, k=0 = hari terbaru
CELL = 0.1  # grid ARI95 NASA


def log(*a):
    print(f"[{datetime.now(WIB):%H:%M:%S}]", *a, flush=True)


# ------------------------------------------------------------------ aset → titik sampel
def densify(coords, step_m):
    out = []
    for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
        n = max(1, int(math.ceil(haversine(y0, x0, y1, x1) * 1000 / step_m)))
        for i in range(n):
            t = i / n
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    if coords:
        out.append(tuple(coords[-1]))
    return out


def load_assets():
    from shapely.geometry import shape
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
        else:
            c = shape(g).centroid if g["type"] != "Point" else None
            lon, lat = (g["coordinates"][0], g["coordinates"][1]) if c is None else (c.x, c.y)
            facs.append({"area": p["area"], "aset": p["name"], "lon": lon, "lat": lat})
    return fc.get("areas", {}), lines, facs


def all_points(lines, facs):
    xs = [p[0] for l in lines for p in l["pts"]] + [f["lon"] for f in facs]
    ys = [p[1] for l in lines for p in l["pts"]] + [f["lat"] for f in facs]
    return np.array(xs), np.array(ys)


# ------------------------------------------------------------------ kerentanan (statis, di-cache)
def _sample_max(src, xs, ys, k):
    """Nilai maksimum dalam ±k piksel di sekitar tiap titik, membaca raster per blok wilayah."""
    import rasterio.windows
    res = np.full(len(xs), np.nan, dtype="float32")
    # kelompokkan titik per kotak 2° supaya jendela baca kecil
    keys = np.floor(xs / 2).astype(int) * 1000 + np.floor(ys / 2).astype(int)
    for key in np.unique(keys):
        idx = np.where(keys == key)[0]
        b = (xs[idx].min() - 0.05, ys[idx].min() - 0.05, xs[idx].max() + 0.05, ys[idx].max() + 0.05)
        win = rasterio.windows.from_bounds(*b, transform=src.transform).round_offsets().round_lengths()
        win = win.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
        arr = src.read(1, window=win).astype("float32")
        if src.nodata is not None:
            arr[arr == src.nodata] = np.nan
        inv = ~src.window_transform(win)
        h, w = arr.shape
        for i in idx:
            c, r = inv * (xs[i], ys[i])
            r, c = int(r), int(c)
            blk = arr[max(0, r - k):min(h, r + k + 1), max(0, c - k):min(w, c + k + 1)]
            if blk.size and np.isfinite(blk).any():
                res[i] = np.nanmax(blk)
    return res


def kerentanan_raster(path_or_url, xs, ys):
    import rasterio
    env = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
               GDAL_HTTP_TIMEOUT="60", GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_USERAGENT=UA["User-Agent"])
    src_path = ("/vsicurl/" + path_or_url) if str(path_or_url).startswith("http") else str(path_or_url)
    with rasterio.Env(**env), rasterio.open(src_path) as src:
        v = _sample_max(src, xs, ys, int(CFG["koridor_piksel"]))
    v[(v < 1) | (v > 5)] = np.nan
    if np.isfinite(v).mean() < 0.5:
        raise RuntimeError(f"hanya {np.isfinite(v).mean():.0%} titik bernilai 1–5 — format peta tidak sesuai")
    return np.nan_to_num(v, nan=1).round().astype(int)


def _baca_dem(url, env):
    """Baca satu petak DEM: langsung (HTTP range) atau, bila server tidak mendukung, unduh utuh."""
    import os
    import tempfile
    import rasterio
    try:
        with rasterio.Env(**env), rasterio.open("/vsicurl/" + url) as src:
            return src.read(1).astype("float32"), src.transform
    except Exception as e:  # noqa
        if "404" in str(e) or "403" in str(e):
            raise
    r = requests.get(url, headers=UA, timeout=(20, 120))
    r.raise_for_status()
    fd, tmp = tempfile.mkstemp(suffix=".tif")
    with os.fdopen(fd, "wb") as f:
        f.write(r.content)
    try:
        with rasterio.open(tmp) as src:
            return src.read(1).astype("float32"), src.transform
    finally:
        os.remove(tmp)


def kerentanan_dem(xs, ys):
    """Pendekatan kerentanan dari kemiringan lereng (Copernicus DEM GLO-90, 3 detik busur)."""
    import rasterio
    env = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT="60", GDAL_HTTP_MAX_RETRY="3")
    batas = CFG["kelas_lereng_derajat"]
    res = np.ones(len(xs), dtype=int)
    tiles = {}
    for i, (x, y) in enumerate(zip(xs, ys)):
        tiles.setdefault((math.floor(y), math.floor(x)), []).append(i)
    blk = 11  # ± 1 km
    ok = 0
    for (la, lo), idx in tiles.items():
        n = f"Copernicus_DSM_COG_30_{'N' if la >= 0 else 'S'}{abs(la):02d}_00_{'E' if lo >= 0 else 'W'}{abs(lo):03d}_00_DEM"
        url = CFG["dem_url"].format(n=n)
        try:
            z, tr = _baca_dem(url, env)
        except Exception as e:  # noqa — petak laut tidak ada di server
            log(f"    DEM {n}: tidak tersedia ({str(e)[:80]})")
            continue
        ok += 1
        dy = abs(tr.e) * 110540.0
        dx = abs(tr.a) * 111320.0 * math.cos(math.radians(la + 0.5))
        gy, gx = np.gradient(z, dy, dx)
        slope = np.degrees(np.arctan(np.hypot(gx, gy)))
        H, Wd = (slope.shape[0] // blk) * blk, (slope.shape[1] // blk) * blk
        p90 = np.percentile(slope[:H, :Wd].reshape(H // blk, blk, Wd // blk, blk).swapaxes(1, 2).reshape(H // blk, Wd // blk, -1), 90, axis=2)
        inv = ~tr
        for i in idx:
            c, r = inv * (xs[i], ys[i])
            r, c = min(int(r) // blk, p90.shape[0] - 1), min(int(c) // blk, p90.shape[1] - 1)
            s = p90[max(0, r - 1):r + 2, max(0, c - 1):c + 2].max()
            res[i] = 1 + sum(s >= b for b in batas)
        log(f"    DEM {n}: {len(idx)} titik")
    if ok == 0:
        raise RuntimeError("tidak ada petak DEM yang bisa diakses")
    return res


def get_kerentanan(xs, ys):
    sig = hashlib.sha1((DATA / "assets.geojson").read_bytes()
                       + json.dumps([CFG["interval_m"], CFG["koridor_piksel"], CFG["kerentanan_sumber"],
                                     CFG["kelas_lereng_derajat"]]).encode()).hexdigest()
    cache = OUT / "kerentanan_cache.json"
    if cache.exists():
        c = json.loads(cache.read_text(encoding="utf-8"))
        manual = STATIS / "kerentanan.tif"
        if c.get("sig") == sig and len(c["kelas"]) == len(xs) and not (manual.exists() and c.get("sumber_id") != "manual"):
            log(f"Kerentanan: dari cache ({c['sumber']})")
            return np.array(c["kelas"]), c["sumber"], c["sumber_id"]
    sumber = {
        "manual": ("Peta kerentanan manual (lhasa_statis/kerentanan.tif)", lambda: kerentanan_raster(STATIS / "kerentanan.tif", xs, ys)),
        "nasa": ("Peta Kerentanan Longsor Global NASA (Stanley & Kirschbaum 2017)", lambda: kerentanan_raster(CFG["kerentanan_nasa_url"], xs, ys)),
        "dem": ("Pendekatan kemiringan lereng — Copernicus DEM GLO-90", lambda: kerentanan_dem(xs, ys)),
    }
    for sid in CFG["kerentanan_sumber"]:
        if sid == "manual" and not (STATIS / "kerentanan.tif").exists():
            continue
        nama, fn = sumber[sid]
        log(f"Kerentanan: mencoba {nama} ...")
        try:
            t = time.time()
            kelas = fn()
            log(f"  OK ({time.time()-t:.0f} dtk) · sebaran kelas 1–5: {np.bincount(kelas, minlength=6)[1:].tolist()}")
            cache.write_text(json.dumps({"sig": sig, "sumber": nama, "sumber_id": sid, "dibuat": NOW.isoformat(timespec="minutes"),
                                         "kelas": kelas.tolist()}, separators=(",", ":")), encoding="utf-8")
            return kelas, nama, sid
        except Exception as e:  # noqa
            log("  gagal:", str(e)[:300])
    raise RuntimeError("Semua sumber kerentanan gagal (NASA, DEM, manual)")


# ------------------------------------------------------------------ hujan & ARI
def ari95_titik(xs, ys):
    import rasterio
    with rasterio.open(STATIS / "ARI95_indonesia.tif") as src:
        v = np.array([s[0] for s in src.sample(zip(xs, ys))], dtype="float64")
    v[(v <= 0) | ~np.isfinite(v)] = np.nan
    return v / 10.0  # 0,1 mm → mm


def hujan_sel(xs, ys):
    """Curah hujan harian per sel 0,1°: 6 hari lalu, hari ini, besok (Open-Meteo)."""
    cx = np.round(np.floor(xs / CELL) * CELL + CELL / 2, 3)
    cy = np.round(np.floor(ys / CELL) * CELL + CELL / 2, 3)
    keys = list(dict.fromkeys(zip(cx.tolist(), cy.tolist())))
    log(f"Hujan: {len(keys)} sel 0,1° dari Open-Meteo ...")
    data, tanggal = {}, None
    for i in range(0, len(keys), 100):
        part = keys[i:i + 100]
        for coba in range(3):
            try:
                r = requests.get(CFG["openmeteo_url"], params={
                    "latitude": ",".join(str(k[1]) for k in part), "longitude": ",".join(str(k[0]) for k in part),
                    "daily": "precipitation_sum", "past_days": 6, "forecast_days": 2, "timezone": "Asia/Jakarta"},
                    headers=UA, timeout=(20, 90))
                r.raise_for_status()
                js = r.json()
                break
            except Exception as e:  # noqa
                if coba == 2:
                    raise
                log("  ulang:", str(e)[:120])
                time.sleep(10)
        js = js if isinstance(js, list) else [js]
        for k, j in zip(part, js):
            d = j["daily"]
            tanggal = d["time"]
            data[k] = [x if x is not None else np.nan for x in d["precipitation_sum"]]
        time.sleep(1)
    P = np.array([data[k] for k in zip(cx.tolist(), cy.tolist())], dtype="float64")  # (titik, 8 hari)
    # Hujan kosong dihitung 0 di ari(); bila terlalu banyak yang kosong hasilnya akan meremehkan
    # potensi longsor tanpa terlihat — lebih baik gagal dan memakai hasil sebelumnya (ditandai stale).
    kosong = float(np.mean(~np.isfinite(P[:, :7])))
    if kosong > 0.10:
        raise RuntimeError(f"{kosong:.0%} nilai curah hujan kosong dari Open-Meteo — hasil tidak dapat diandalkan")
    return P, tanggal, len(keys)


def ari(P, akhir):
    """ARI dengan hari terbaru = kolom `akhir` (6 = hari ini, 7 = besok)."""
    win = P[:, akhir - 6:akhir + 1][:, ::-1]  # kolom 0 = hari terbaru
    win = np.where(np.isfinite(win), win, 0.0)
    return (win * W).sum(axis=1) / W.sum()


def klasifikasi(rasio, kelas):
    lv = np.full(len(rasio), None, dtype=object)
    rentan = kelas > 2
    lv[(rasio >= CFG["rasio_waspada"]) & rentan] = "rendah"
    lv[(rasio > 1) & rentan] = "sedang"
    lv[(rasio > 1) & (kelas > 4)] = "tinggi"
    return lv


# ------------------------------------------------------------------ ringkasan per wilayah
def ringkas(areas, lines, facs, lv, rasio, ar, a95, kelas):
    rank = {k: i for i, k in enumerate(LEVELS)}
    per_area, fc = {}, []
    off, n_line_pts = 0, []
    for l in lines:
        n_line_pts.append((off, len(l["pts"])))
        off += len(l["pts"])
    foff = off
    for code in areas:
        km = {k: 0.0 for k in LEVELS}
        kkm = {str(c): 0.0 for c in range(1, 6)}
        total, rmax, ruas = 0.0, 0.0, []
        for l, (o, n) in zip(lines, n_line_pts):
            if l["area"] != code:
                continue
            total += sum(l["seg_km"])
            # nilai ruas antar-titik = yang terburuk dari kedua ujung
            sl = []
            for j in range(n - 1):
                a, b = lv[o + j], lv[o + j + 1]
                cand = [x for x in (a, b) if x]
                sl.append(min(cand, key=lambda x: rank[x]) if cand else None)
                kkm[str(int(max(kelas[o + j], kelas[o + j + 1])))] += l["seg_km"][j]
            rmax = max(rmax, float(np.nanmax(rasio[o:o + n])) if np.isfinite(rasio[o:o + n]).any() else 0)
            j = 0
            while j < len(sl):
                if sl[j] is None:
                    j += 1
                    continue
                e = j
                while e + 1 < len(sl) and sl[e + 1] == sl[j]:
                    e += 1
                seg_km = sum(l["seg_km"][j:e + 1])
                km[sl[j]] += seg_km
                sel = slice(o + j, o + e + 2)
                coords = [[round(c, 5) for c in l["pts"][t]] for t in range(j, e + 2)]
                mid = coords[len(coords) // 2]
                imax = o + j + int(np.nanargmax(np.nan_to_num(rasio[sel], nan=0)))
                info = {"aset": l["aset"], "level": sl[j], "km": round(seg_km, 2), "rasio": round(float(rasio[imax]), 2),
                        "ari": round(float(ar[imax]), 1), "ari95": round(float(a95[imax]), 1), "kerentanan": int(kelas[sel].max()),
                        "lat": mid[1], "lon": mid[0]}
                ruas.append(info)
                fc.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                           "properties": {"area": code, **{k: v for k, v in info.items() if k not in ("lat", "lon")}}})
                j = e + 1
        fas = []
        for i, f in enumerate(facs):
            if f["area"] != code:
                continue
            g = foff + i
            if np.isfinite(rasio[g]):
                rmax = max(rmax, float(rasio[g]))
            if lv[g]:
                fas.append({"aset": f["aset"], "level": lv[g], "rasio": round(float(rasio[g]), 2), "ari": round(float(ar[g]), 1),
                            "ari95": round(float(a95[g]), 1), "kerentanan": int(kelas[g]),
                            "lat": round(f["lat"], 5), "lon": round(f["lon"], 5)})
        if total == 0 and not any(f["area"] == code for f in facs):
            continue
        ruas.sort(key=lambda r: (rank[r["level"]], -r["rasio"], -r["km"]))
        fas.sort(key=lambda r: (rank[r["level"]], -r["rasio"]))
        worst = next((k for k in LEVELS if km[k] > 0 or any(f["level"] == k for f in fas)), None)
        per_area[code] = {"total_pipa_km": round(total, 1), "pipa_km": {k: round(v, 2) for k, v in km.items()},
                          "kerentanan_km": {k: round(v, 1) for k, v in kkm.items()},
                          "rasio_maks": round(rmax, 2), "terburuk": worst, "jumlah_ruas": len(ruas),
                          "ruas": ruas[:CFG["maks_ruas_per_wilayah"]], "fasilitas": fas}
        log(f"  {code}: tinggi {km['tinggi']:.1f} · sedang {km['sedang']:.1f} · rendah {km['rendah']:.1f} km · "
            f"rasio maks {rmax:.2f} · fasilitas {len(fas)}")
    return per_area, fc


def update_riwayat(per_area):
    path = OUT / "riwayat.csv"
    tgl = NOW.strftime("%Y-%m-%d")
    fields = ["tanggal", "area", "tinggi_km", "sedang_km", "rendah_km", "rasio_maks"]
    rows = []
    if path.exists():
        rows = [r for r in csv.DictReader(path.open(encoding="utf-8")) if r.get("tanggal") != tgl and "rasio_maks" in r]
    for code, a in per_area.items():
        rows.append({"tanggal": tgl, "area": code, "tinggi_km": a["pipa_km"]["tinggi"], "sedang_km": a["pipa_km"]["sedang"],
                     "rendah_km": a["pipa_km"]["rendah"], "rasio_maks": a["rasio_maks"]})
    rows.sort(key=lambda r: (r["tanggal"], r["area"]))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    prev = json.loads((OUT / "lhasa.json").read_text(encoding="utf-8")) if (OUT / "lhasa.json").exists() else {}
    areas, lines, facs = load_assets()
    xs, ys = all_points(lines, facs)
    log(f"Aset: {len(lines)} ruas pipa · {len(xs)} titik sampel (tiap {CFG['interval_m']} m) · {len(facs)} fasilitas")
    out = {"model": "LHASA 1.1 (NASA) dihitung internal", "sumber": "Internal Pertagas — algoritma NASA LHASA 1.1",
           "jenis": "Indikasi model — bukan observasi dan bukan produk resmi NASA",
           "catatan": ("Ambang ARI95 NASA diturunkan dari hujan satelit IMERG, sedangkan hujan harian di sini dari model "
                       "Open-Meteo; perbedaan sumber hujan dapat menggeser hasil. Produk 'hari ini' memakai hujan 6 hari "
                       "terakhir + total hujan hari ini (sebagian masih prakiraan); 'besok' memakai prakiraan hujan besok. "
                       "Hasil menunjukkan potensi bahaya, bukan kepastian kejadian longsor."),
           "diambil": NOW.isoformat(timespec="minutes"), "interval_m": CFG["interval_m"],
           "koridor_piksel": CFG["koridor_piksel"], "rasio_waspada": CFG["rasio_waspada"], "produk": {}, "status": {}}
    try:
        kelas, ksumber, kid = get_kerentanan(xs, ys)
        out["kerentanan_sumber"] = ksumber
        out["kerentanan_id"] = kid
        a95 = ari95_titik(xs, ys)
        a95 = np.where(np.isfinite(a95), a95, np.nanmedian(a95))
        P, tanggal, nsel = hujan_sel(xs, ys)
        out["hujan_sumber"] = f"Open-Meteo ({nsel} sel 0,1°)"
        out["tanggal_hujan"] = tanggal
        fc_all = []
        for key, akhir in (("hari_ini", 6), ("besok", 7)):
            log(f"Model {key} (hari terakhir {tanggal[akhir]}):")
            ar = ari(P, akhir)
            rasio = ar / a95
            lv = klasifikasi(rasio, kelas)
            per_area, fc = ringkas(areas, lines, facs, lv, rasio, ar, a95, kelas)
            for f in fc:
                f["properties"]["produk"] = key
            fc_all += fc
            out["produk"][key] = {"tanggal": tanggal[akhir], "stale": False, "per_area": per_area}
            out["status"][key] = {"ok": True}
        (OUT / "ruas.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": fc_all},
                                                     ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        update_riwayat(out["produk"]["hari_ini"]["per_area"])
    except Exception as e:  # noqa
        traceback.print_exc()
        for key in ("hari_ini", "besok"):
            out["status"][key] = {"ok": False, "pesan": str(e)[:400]}
            if key in prev.get("produk", {}) and "per_area" in prev["produk"][key]:
                out["produk"][key] = {**prev["produk"][key], "stale": True}
        for k in ("kerentanan_sumber", "kerentanan_id", "hujan_sumber", "tanggal_hujan"):
            if k in prev and k not in out:
                out[k] = prev[k]
    out["durasi_detik"] = round(time.time() - t0)
    (OUT / "lhasa.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"Selesai dalam {out['durasi_detik']} detik →", OUT / "lhasa.json")
    log(json.dumps(out["status"], ensure_ascii=False))
    if not out["status"].get("hari_ini", {}).get("ok"):
        sys.exit("Model LHASA gagal dihitung")


if __name__ == "__main__":
    main()
