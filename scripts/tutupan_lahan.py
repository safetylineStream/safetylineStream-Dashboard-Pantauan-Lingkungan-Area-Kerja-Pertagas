"""
tutupan_lahan.py — Tutupan lahan (land cover) di sekitar hotspot dan di sepanjang koridor pipa Pertagas.

Sumber utama: ESA WorldCover 10 m 2021 v200 (CC BY 4.0), dibaca langsung dari berkas Cloud-Optimized
GeoTIFF di AWS Open Data (esa-worldcover.s3.eu-central-1.amazonaws.com) — tanpa kunci API.
Kelas WorldCover: 10 pohon, 20 semak, 30 rumput, 40 pertanian, 50 terbangun, 60 terbuka/jarang vegetasi,
70 salju/es, 80 air, 90 lahan basah, 95 mangrove, 100 lumut.

Dipakai oleh klasifikasi multi-bahaya (klasifikasi.js): hotspot di lahan non-vegetasi (kota, industri,
pelabuhan, kilang) dianggap sumber panas non-vegetasi — tetap ditampilkan, tetapi tidak bisa memicu
status Kritis. Kebakaran lahan hanya relevan di area bervegetasi.

Pemakaian:
  * fetch_all.py memanggil tambah_tutupan(hotspots) untuk hotspot ≤ radius pantau (di-cache per lokasi).
  * python scripts/tutupan_lahan.py            → koridor pipa (dihitung ulang hanya bila aset berubah)
    python scripts/tutupan_lahan.py --paksa    → paksa hitung ulang
Hasil:
  data/tutupan_lahan/koridor.geojson   → ruas pipa per kelas tutupan lahan dominan (lapisan peta)
  data/tutupan_lahan/ringkasan.json    → km pipa per kelas per wilayah + info sumber & layanan peta
  data/tutupan_lahan/titik_cache.json  → cache kelas tutupan lahan lokasi hotspot
"""
import hashlib
import json
import math
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = DATA / "tutupan_lahan"
WIB = timezone(timedelta(hours=7))
UA = "Pertagas-QMHSE-EnvDashboard/1.0 (+github actions; monitoring aset pipa)"

VERSI = "ESA WorldCover 10 m 2021 v200"
WC_URL = ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
          "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif")
# Lapisan peta (WMS) resmi Terrascope/VITO untuk ditampilkan di dashboard
WMS = {"url": "https://services.terrascope.be/wms/v2", "layer": "WORLDCOVER_2021_MAP",
       "layer_cadangan": "WORLDCOVER_2020_MAP"}

KELAS = {
    10: ("Tutupan pohon (hutan/kebun berkayu)", "#006400"),
    20: ("Semak belukar", "#ffbb22"),
    30: ("Padang rumput/alang-alang", "#ffff4c"),
    40: ("Lahan pertanian (sawah/tegalan)", "#f096ff"),
    50: ("Lahan terbangun (permukiman/industri)", "#fa0000"),
    60: ("Lahan terbuka/jarang vegetasi", "#b4b4b4"),
    70: ("Salju/es", "#f0f0f0"),
    80: ("Badan air", "#0064c8"),
    90: ("Lahan basah herba (rawa)", "#0096a0"),
    95: ("Mangrove", "#00cf75"),
    100: ("Lumut", "#fae6a0"),
}
VEG_LEBAT = (10, 20, 90, 95)   # bahan bakar banyak — api mudah menjalar
VEG_RENDAH = (30, 40, 100)     # rumput, pertanian
KATEGORI = {
    "vegetasi_lebat": "Vegetasi lebat (hutan, kebun, semak, rawa, mangrove)",
    "vegetasi_rendah": "Vegetasi rendah (pertanian, rumput)",
    "non_vegetasi": "Non-vegetasi (terbangun, terbuka, air)",
}
BAWAAN = {"radius_hotspot_m": 500, "radius_koridor_m": 250, "interval_koridor_m": 500,
          "ambang_terbangun": 0.40, "ambang_vegetasi_min": 0.30, "ambang_vegetasi_lebat": 0.50}


def log(*a):
    print(f"[{datetime.now(WIB):%H:%M:%S}]", *a, flush=True)


def cfg():
    try:
        c = json.loads((ROOT / "config" / "monitoring.json").read_text(encoding="utf-8")).get("tutupan_lahan", {})
    except Exception:  # noqa
        c = {}
    return {**BAWAAN, **{k: v for k, v in c.items() if not k.startswith("_")}}


# ------------------------------------------------------------------ fungsi murni (diuji)
def nama_tile(lat, lon):
    """Nama tile WorldCover 3°×3° (pojok kiri-bawah), mis. (-1.2, 116.9) → 'S03E114'."""
    la = int(math.floor(lat / 3.0) * 3)
    lo = int(math.floor(lon / 3.0) * 3)
    return f"{'N' if la >= 0 else 'S'}{abs(la):02d}{'E' if lo >= 0 else 'W'}{abs(lo):03d}"


def kategori(persen, c=None):
    """Kategori tutupan lahan untuk penilaian hotspot dari porsi kelas WorldCover (dict kode → 0..1).

    non_vegetasi    : terbangun ≥ ambang_terbangun, atau total vegetasi < ambang_vegetasi_min
    vegetasi_lebat  : pohon + semak + rawa + mangrove ≥ ambang_vegetasi_lebat
    vegetasi_rendah : selain itu (pertanian, rumput, campuran)
    """
    c = c or BAWAAN
    p = {int(k): float(v) for k, v in (persen or {}).items()}
    if not p or sum(p.values()) <= 0:
        return None
    lebat = sum(p.get(k, 0) for k in VEG_LEBAT)
    rendah = sum(p.get(k, 0) for k in VEG_RENDAH)
    if p.get(50, 0) >= c["ambang_terbangun"] or lebat + rendah < c["ambang_vegetasi_min"]:
        return "non_vegetasi"
    if lebat >= c["ambang_vegetasi_lebat"]:
        return "vegetasi_lebat"
    return "vegetasi_rendah"


def ringkas_hist(nilai, c=None):
    """nilai: iterable kode kelas piksel → dict hasil (dominan, persen, kategori, label)."""
    try:
        import numpy as np
        u, cnt = np.unique(np.asarray(nilai).ravel(), return_counts=True)
        pasangan = zip(u.tolist(), cnt.tolist())
    except ImportError:
        pasangan = {}
        for v in nilai:
            pasangan[int(v)] = pasangan.get(int(v), 0) + 1
        pasangan = pasangan.items()
    hit = {int(k): int(v) for k, v in pasangan if int(k) in KELAS}
    n = sum(hit.values())
    if not n:
        return None
    persen = {k: round(x / n, 3) for k, x in sorted(hit.items(), key=lambda kv: -kv[1])}
    dom = next(iter(persen))
    kat = kategori(persen, c)
    return {"kode": dom, "label": KELAS[dom][0], "porsi": persen[dom], "persen": persen,
            "kategori": kat, "kategori_label": KATEGORI.get(kat)}


# ------------------------------------------------------------------ baca raster
def _env():
    return dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
                GDAL_HTTP_TIMEOUT=45, GDAL_HTTP_CONNECTTIMEOUT=20, GDAL_HTTP_MAX_RETRY=2,
                GDAL_HTTP_RETRY_DELAY=2, GDAL_HTTP_USERAGENT=UA, VSI_CACHE=True,
                VSI_CACHE_SIZE=256 * 1024 * 1024, GDAL_CACHEMAX=512)


def kelas_titik(titik, radius_m, faktor=1, batas_detik=None, c=None):
    """titik: list (lat, lon). Hasil: list dict|None sejajar dengan input.

    Titik dikelompokkan per tile WorldCover lalu per sel 0,05° supaya satu jendela baca melayani banyak
    titik. faktor > 1 membaca versi kecil (overview) — cukup untuk kelas dominan koridor.
    """
    import numpy as np
    import rasterio
    from rasterio.windows import Window, from_bounds

    c = c or cfg()
    t0 = time.time()
    hasil = [None] * len(titik)
    grup = {}
    for i, (la, lo) in enumerate(titik):
        grup.setdefault(nama_tile(la, lo), {}).setdefault((math.floor(lo / 0.05), math.floor(la / 0.05)), []).append(i)
    gagal = []
    with rasterio.Env(**_env()):
        for tile, sel in grup.items():
            if batas_detik and time.time() - t0 > batas_detik:
                log(f"  tutupan lahan: batas waktu {batas_detik:.0f} dtk tercapai, sisa titik dilewati")
                break
            try:
                u = WC_URL.format(tile=tile)
                src = rasterio.open(("/vsicurl/" + u) if u.startswith("http") else u)
            except Exception as e:  # noqa — tile laut/tidak ada
                gagal.append(f"{tile}: {str(e)[:80]}")
                continue
            with src:
                for idx in sel.values():
                    if batas_detik and time.time() - t0 > batas_detik:
                        break
                    las = [titik[i][0] for i in idx]
                    los = [titik[i][1] for i in idx]
                    kx = 111.32 * math.cos(math.radians(sum(las) / len(las)))
                    dy, dx = radius_m / 111320.0, radius_m / (kx * 1000.0)
                    b = (min(los) - dx, min(las) - dy, max(los) + dx, max(las) + dy)
                    win = from_bounds(*b, transform=src.transform).round_offsets().round_lengths()
                    win = win.intersection(Window(0, 0, src.width, src.height))
                    if win.width <= 0 or win.height <= 0:
                        continue
                    shape = (max(1, int(win.height // faktor)), max(1, int(win.width // faktor)))
                    try:
                        arr = src.read(1, window=win, out_shape=shape)
                    except Exception as e:  # noqa
                        gagal.append(f"{tile}: {str(e)[:80]}")
                        continue
                    wt = src.window_transform(win)
                    sx, sy = wt.a * win.width / shape[1], wt.e * win.height / shape[0]
                    rx, ry = max(1, int(round(dx / abs(sx)))), max(1, int(round(dy / abs(sy))))
                    yy, xx = np.ogrid[-ry:ry + 1, -rx:rx + 1]
                    lingkar = (xx / rx) ** 2 + (yy / ry) ** 2 <= 1.0
                    for i in idx:
                        cc = int((titik[i][1] - wt.c) / sx)
                        rr = int((titik[i][0] - wt.f) / sy)
                        r0, r1, c0, c1 = rr - ry, rr + ry + 1, cc - rx, cc + rx + 1
                        if r1 <= 0 or c1 <= 0 or r0 >= shape[0] or c0 >= shape[1]:
                            continue
                        blk = arr[max(0, r0):min(shape[0], r1), max(0, c0):min(shape[1], c1)]
                        m = lingkar[max(0, -r0):max(0, -r0) + blk.shape[0], max(0, -c0):max(0, -c0) + blk.shape[1]]
                        hasil[i] = ringkas_hist(blk[m].ravel(), c)
    if gagal:
        log(f"  tutupan lahan: {len(gagal)} kegagalan baca, contoh: {gagal[:3]}")
    return hasil


# ------------------------------------------------------------------ hotspot (dipanggil fetch_all.py)
def tambah_tutupan(hotspots, batas_detik=90):
    """Tambahkan h['tutupan'] ke hotspot ≤ radius pantau. Gagal/rasterio tidak ada → dilewati tanpa error."""
    target = [h for h in hotspots if h.get("status") != "luar"]
    if not target:
        return 0
    c = cfg()
    f = OUT / "titik_cache.json"
    try:
        cache = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except Exception:  # noqa
        cache = {}
    if cache.get("_versi") != VERSI or cache.get("_radius_m") != c["radius_hotspot_m"]:
        cache = {"_versi": VERSI, "_radius_m": c["radius_hotspot_m"]}
    key = lambda h: f"{float(h['lat']):.4f},{float(h['lon']):.4f}"  # noqa: E731
    baru = [h for h in target if key(h) not in cache]
    tulis = False
    if baru:
        try:
            res = kelas_titik([(float(h["lat"]), float(h["lon"])) for h in baru], c["radius_hotspot_m"],
                              batas_detik=batas_detik, c=c)
        except ImportError as e:
            log(f"  tutupan lahan dilewati (pustaka belum terpasang: {e})")
            res = [None] * len(baru)
        except Exception as e:  # noqa
            log(f"  tutupan lahan gagal: {e}")
            res = [None] * len(baru)
        for h, r in zip(baru, res):
            if r:
                cache[key(h)] = r
                tulis = True
    n = 0
    for h in target:
        r = cache.get(key(h))
        if r:
            h["tutupan"] = {k: r[k] for k in ("kode", "label", "porsi", "kategori", "kategori_label")}
            h["tutupan"]["persen"] = dict(list(r["persen"].items())[:4])
            n += 1
    try:
        if not tulis:
            raise StopIteration
        OUT.mkdir(parents=True, exist_ok=True)
        # batasi ukuran cache: buang entri tertua bila > 5000
        ks = [k for k in cache if not k.startswith("_")]
        for k in ks[:-5000]:
            cache.pop(k, None)
        f.write_text(json.dumps(cache, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except StopIteration:
        pass
    except Exception as e:  # noqa
        log(f"  cache tutupan lahan tidak tersimpan: {e}")
    log(f"  tutupan lahan: {n}/{len(target)} hotspot terklasifikasi ({len(baru)} lokasi baru)")
    return n


# ------------------------------------------------------------------ layanan penutupan lahan KLHK (opsional)
def cari_klhk():
    """Cari layanan peta 'Penutupan Lahan' KLHK terbaru di geoportal KLHK. Gagal → None (lapisan tidak dipasang)."""
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = UA
    bases = ["https://geoportal.menlhk.go.id/server/rest/services", "https://geoportal.menlhk.go.id/arcgis/rest/services"]
    for base in bases:
        try:
            r = s.get(base, params={"f": "json"}, timeout=(15, 30))
            r.raise_for_status()
            root = r.json()
        except Exception as e:  # noqa
            log(f"  KLHK {base}: {str(e)[:120]}")
            continue
        kandidat = []
        folders = [None] + [f for f in root.get("folders", []) if re.search(r"KLHK|PL|Penutupan|Tutupan", f, re.I)]
        for fd in folders:
            try:
                js = root if fd is None else s.get(f"{base}/{fd}", params={"f": "json"}, timeout=(15, 30)).json()
            except Exception:  # noqa
                continue
            for sv in js.get("services", []):
                nm = sv.get("name", "")
                m = re.search(r"(Penutupan|Tutupan)_?Lahan[^/]*?(\d{4})", nm, re.I)
                if m and sv.get("type") == "MapServer":
                    kandidat.append((int(m.group(2)), nm))
        log(f"  KLHK {base}: {len(kandidat)} layanan penutupan lahan ditemukan {sorted(kandidat)[-5:]}")
        for tahun, nm in sorted(kandidat, reverse=True)[:4]:
            url = f"{base}/{nm}/MapServer"
            try:
                info = s.get(url, params={"f": "json"}, timeout=(15, 30)).json()
                if "error" in info:
                    raise ValueError(info["error"].get("message"))
                tes = s.get(f"{url}/export", params={"bbox": "12580000,-150000,12600000,-130000", "bboxSR": 3857,
                                                     "imageSR": 3857, "size": "64,64", "format": "png32",
                                                     "transparent": "true", "f": "image"}, timeout=(15, 45))
                if tes.ok and tes.headers.get("content-type", "").startswith("image"):
                    log(f"  KLHK dipakai: {url} (tahun {tahun})")
                    return {"url": url, "tahun": tahun, "nama": nm.split("/")[-1].replace("_", " "),
                            "sumber": "KLHK — Penutupan Lahan (geoportal.menlhk.go.id)"}
                log(f"  KLHK {url}: export tidak mengembalikan gambar ({tes.status_code})")
            except Exception as e:  # noqa
                log(f"  KLHK {url}: {str(e)[:120]}")
    return None


# ------------------------------------------------------------------ koridor pipa
def _sig(c):
    h = hashlib.sha1((DATA / "assets.geojson").read_bytes())
    h.update(json.dumps([VERSI, c["radius_koridor_m"], c["interval_koridor_m"], c["ambang_terbangun"],
                         c["ambang_vegetasi_min"], c["ambang_vegetasi_lebat"]]).encode())
    return h.hexdigest()[:16]


def koridor(paksa=False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from geo import haversine
    c = cfg()
    OUT.mkdir(parents=True, exist_ok=True)
    ring_f = OUT / "ringkasan.json"
    lama = json.loads(ring_f.read_text(encoding="utf-8")) if ring_f.exists() else {}
    sig = _sig(c)
    sekarang = datetime.now(WIB).isoformat(timespec="seconds")
    klhk = cari_klhk()
    if not paksa and lama.get("signature") == sig and (OUT / "koridor.geojson").exists():
        log("Koridor tidak berubah (aset & parameter sama) — hanya info layanan yang diperbarui.")
        lama.update({"klhk": klhk or lama.get("klhk"), "diperiksa": sekarang})
        ring_f.write_text(json.dumps(lama, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0

    fc = json.loads((DATA / "assets.geojson").read_text(encoding="utf-8"))
    step = c["interval_koridor_m"]
    lines = []
    for f in fc["features"]:
        p, g = f["properties"], f["geometry"]
        if g["type"] not in ("LineString", "MultiLineString"):
            continue
        for part in ([g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]):
            pts = []
            for (x0, y0), (x1, y1) in zip([q[:2] for q in part[:-1]], [q[:2] for q in part[1:]]):
                n = max(1, int(math.ceil(haversine(y0, x0, y1, x1) * 1000 / step)))
                pts += [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(n)]
            if part:
                pts.append(tuple(part[-1][:2]))
            if len(pts) >= 2:
                lines.append({"area": p["area"], "aset": p["name"], "pts": pts})
    semua = [(y, x) for l in lines for (x, y) in l["pts"]]
    log(f"Koridor: {len(lines)} ruas, {len(semua)} titik sampel tiap {step} m, radius {c['radius_koridor_m']} m")
    t = time.time()
    res = kelas_titik(semua, c["radius_koridor_m"], faktor=2, batas_detik=40 * 60, c=c)
    ok = sum(1 for r in res if r)
    log(f"  {ok}/{len(semua)} titik terklasifikasi dalam {time.time() - t:.0f} dtk")
    if ok < 0.5 * len(semua):
        raise RuntimeError(f"terlalu sedikit titik terklasifikasi ({ok}/{len(semua)}) — hasil lama dipertahankan")

    feats, per_area, off = [], {}, 0
    for l in lines:
        n = len(l["pts"])
        rr = res[off:off + n]
        off += n
        pa = per_area.setdefault(l["area"], {"total_km": 0.0, "kelas_km": {}, "kategori_km": {}})
        j = 0
        while j < n - 1:
            r = rr[j] or rr[j + 1]
            if not r:
                j += 1
                continue
            k = r["kode"]
            e = j
            while e + 1 < n - 1 and (rr[e + 1] or {}).get("kode") == k:
                e += 1
            co = [[round(x, 5), round(y, 5)] for x, y in l["pts"][j:e + 2]]
            km = sum(haversine(a[1], a[0], b[1], b[0]) for a, b in zip(co[:-1], co[1:]))
            pa["total_km"] += km
            pa["kelas_km"][str(k)] = pa["kelas_km"].get(str(k), 0) + km
            kat = r.get("kategori") or "?"
            pa["kategori_km"][kat] = pa["kategori_km"].get(kat, 0) + km
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": co},
                          "properties": {"area": l["area"], "aset": l["aset"], "kode": k, "kelas": KELAS[k][0],
                                         "porsi": round(sum((x or {}).get("porsi", 0) for x in rr[j:e + 2] if x and x.get("kode") == k)
                                                        / max(1, sum(1 for x in rr[j:e + 2] if x and x.get("kode") == k)), 2),
                                         "kategori": r.get("kategori"), "km": round(km, 2)}})
            j = e + 1
    for pa in per_area.values():
        pa["total_km"] = round(pa["total_km"], 1)
        pa["kelas_km"] = {k: round(v, 1) for k, v in sorted(pa["kelas_km"].items(), key=lambda kv: -kv[1])}
        pa["kategori_km"] = {k: round(v, 1) for k, v in pa["kategori_km"].items()}
    (OUT / "koridor.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                                     ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    ring = {"diperbarui": sekarang, "diperiksa": sekarang, "signature": sig, "sumber": VERSI,
            "lisensi": "© ESA WorldCover project 2021 / Contains modified Copernicus Sentinel data (2021) — CC BY 4.0",
            "parameter": {k: c[k] for k in BAWAAN}, "kelas": {str(k): {"label": v[0], "warna": v[1]} for k, v in KELAS.items()},
            "kategori": KATEGORI, "wms": WMS, "klhk": klhk, "per_area": per_area}
    ring_f.write_text(json.dumps(ring, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"Selesai: {len(feats)} ruas tutupan lahan, {sum(p['total_km'] for p in per_area.values()):.0f} km pipa")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(koridor(paksa="--paksa" in sys.argv))
    except Exception as e:  # noqa
        import traceback
        traceback.print_exc()
        log(f"GAGAL: {e}")
        sys.exit(1)
