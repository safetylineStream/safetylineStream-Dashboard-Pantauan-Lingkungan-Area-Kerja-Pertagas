"""
petir.py — deteksi sambaran petir real-time di sekitar jalur pipa & fasilitas Pertagas.

Sumber: Vaisala Xweather Lightning API (jaringan deteksi petir global GLD360).
Kredensial dibaca dari env XWEATHER_CLIENT_ID dan XWEATHER_CLIENT_SECRET (GitHub Secrets) — TIDAK
pernah ditulis ke repo maupun ke log. Tanpa kredensial, skrip berhenti tanpa membuat/mengubah data.

Mode (config/monitoring.json → petir.mode):
  standard   — akses lightning standar Xweather: aksi `closest`, lingkaran maks 100 km, data 5 menit
               terakhir, maks 1000 sambaran per kueri. Lingkaran kueri disebar otomatis sehingga seluruh
               jalur pipa & fasilitas (plus radius pantau) tertutup.
  enterprise — add-on Lightning Enterprise: aksi `within` dengan kotak (bbox) per wilayah dan riwayat
               `lookback_enterprise_menit` menit, maks 50.000 sambaran per kueri.

Alur per putaran (satu job bisa beberapa putaran, lihat petir.polling):
  1. Ambil sambaran (default hanya cloud-to-ground / CG — yang bisa mengenai aset).
  2. Hitung jarak geodesik ke jalur pipa/fasilitas terdekat, keseluruhan dan per wilayah.
  3. Klasifikasi: kritis ≤ radius kritis (praktik 30-30: hentikan pekerjaan luar ruang),
                  waspada ≤ radius waspada, pantau ≤ radius pantau.
  4. Gabung dengan sambaran putaran sebelumnya yang belum lebih tua dari `simpan_menit`.
  5. Ringkas per wilayah: status 30 menit terakhir (all-clear = sambaran terakhir + all_clear_menit).
  6. Tulis data/petir/latest.json (atomik) + arsip harian data/petir/arsip/YYYY-MM-DD.json (WIB).

Waktu sambaran = waktu dari jaringan deteksi (bukan waktu run). Gagal ambil → data lama dibiarkan,
hanya status sumber yang ditandai gagal.
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo import WIB, IndeksTitik, classify, haversine, valid_ll  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "petir"
OUT = DATA / "latest.json"
ARSIP = DATA / "arsip"
ASET = ROOT / "data" / "assets.geojson"
UA = {"User-Agent": "Pertagas-HSSE-WASPADA/1.0 (+github actions; pemantauan petir aset pipa)"}
URUT = {"kritis": 0, "waspada": 1, "pantau": 2, "luar": 3}


class GagalAmbil(RuntimeError):
    pass


# ---------------------------------------------------------------- aset
class IndeksAset:
    """Jarak titik → aset terdekat (km, geodesik), keseluruhan dan per wilayah."""

    def __init__(self, fc):
        from pyproj import Transformer
        from shapely.geometry import shape
        from shapely.ops import transform
        from shapely.strtree import STRtree
        self._to_m = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
        self._to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
        self.fc = fc
        self.areas = fc.get("areas", {})
        self.items = [(transform(self._to_m, shape(f["geometry"])), f["properties"]) for f in fc["features"]]
        self.tree = STRtree([g for g, _ in self.items])
        self.by_area, self.bbox_area = {}, {}
        for code in sorted({p["area"] for _, p in self.items}):
            its = [(g, p) for g, p in self.items if p["area"] == code]
            self.by_area[code] = (STRtree([g for g, _ in its]), its)
            xs, ys = [], []
            for f in fc["features"]:
                if f["properties"]["area"] != code:
                    continue
                for x, y in _coords(f["geometry"]):
                    xs.append(x)
                    ys.append(y)
            self.bbox_area[code] = (min(xs), min(ys), max(xs), max(ys))
        self.fas = [(p, g) for g, p in self.items if p.get("kind") == "fasilitas"]

    def terdekat(self, lat, lon, area=None):
        from shapely.geometry import Point
        from shapely.ops import nearest_points
        pt = Point(self._to_m(lon, lat))
        tree, items = (self.tree, self.items) if area is None else self.by_area[area]
        g, props = items[int(tree.nearest(pt))]
        _, q = nearest_points(pt, g)
        qlon, qlat = self._to_ll(q.x, q.y)
        return haversine(lat, lon, qlat, qlon), props

    def fasilitas_terdekat(self, lat, lon):
        best = (None, None)
        for p, g in self.fas:
            qlon, qlat = self._to_ll(g.x, g.y)
            d = haversine(lat, lon, qlat, qlon)
            if best[0] is None or d < best[0]:
                best = (d, p)
        return best

    def dekat_bbox(self, lat, lon, area, pad_km):
        x0, y0, x1, y1 = self.bbox_area[area]
        pad_lat = pad_km / 111.0
        pad_lon = pad_km / (111.0 * max(0.2, math.cos(math.radians(lat))))
        return x0 - pad_lon <= lon <= x1 + pad_lon and y0 - pad_lat <= lat <= y1 + pad_lat


def _coords(g):
    t, c = g["type"], g["coordinates"]
    if t == "Point":
        return [c]
    if t in ("LineString", "MultiPoint"):
        return c
    if t in ("MultiLineString", "Polygon"):
        return [p for part in c for p in part]
    if t == "MultiPolygon":
        return [p for poly in c for ring in poly for p in ring]
    return []


# ---------------------------------------------------------------- lingkaran kueri (mode standard)
def titik_sampel(fc, jarak_km=2.0):
    """Titik di sepanjang semua jalur pipa (tiap ±jarak_km) + semua fasilitas, dalam lat/lon."""
    out = []
    for f in fc["features"]:
        g = f["geometry"]
        a = f["properties"].get("area")
        if g["type"] == "Point":
            out.append({"lat": g["coordinates"][1], "lon": g["coordinates"][0], "area": a})
            continue
        parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"] if g["type"] == "MultiLineString" else []
        for c in parts:
            for i, pt in enumerate(c):
                x, y = pt[0], pt[1]
                if i == 0:
                    out.append({"lat": y, "lon": x, "area": a})
                    continue
                x0, y0 = c[i - 1][0], c[i - 1][1]
                d = haversine(y0, x0, y, x)
                n = max(1, int(math.ceil(d / jarak_km)))
                for k in range(1, n + 1):
                    out.append({"lat": y0 + (y - y0) * k / n, "lon": x0 + (x - x0) * k / n, "area": a})
    return out


def lingkaran_cakupan(fc, r_kueri_km, r_pantau_km, jarak_sampel_km=2.0):
    """Sebar lingkaran kueri (greedy set cover) sehingga setiap titik dalam r_pantau_km dari aset
    tercakup lingkaran berjari-jari r_kueri_km. Mengembalikan [{lat, lon, r_km, area:[...]}]."""
    rc = r_kueri_km - r_pantau_km - jarak_sampel_km / 2.0
    if rc <= 0:
        raise ValueError("radius_kueri_km harus lebih besar dari radius pantau + jarak sampel")
    sampel = titik_sampel(fc, jarak_sampel_km)
    for i, s in enumerate(sampel):
        s["i"] = i
    idx = IndeksTitik(sampel, sel_deg=0.25)
    belum = set(range(len(sampel)))
    hasil = []
    urut = sorted(sampel, key=lambda s: (s["lon"], s["lat"]))
    for s in urut:
        if s["i"] not in belum:
            continue
        kandidat = idx.dalam_radius(s["lat"], s["lon"], rc)
        step = max(1, len(kandidat) // 150)
        best, best_cov = s, None
        for c in kandidat[::step]:
            cov = [t["i"] for t in idx.dalam_radius(c["lat"], c["lon"], rc) if t["i"] in belum]
            if best_cov is None or len(cov) > len(best_cov):
                best, best_cov = c, cov
        if not best_cov or s["i"] not in best_cov:  # jaga-jaga: pastikan titik awal tercakup
            best = s
            best_cov = [t["i"] for t in idx.dalam_radius(s["lat"], s["lon"], rc) if t["i"] in belum]
        belum.difference_update(best_cov)
        hasil.append(best)
    # buang lingkaran yang seluruh cakupannya sudah ditutup lingkaran lain (hemat kuota API)
    cak = [{t["i"] for t in idx.dalam_radius(c["lat"], c["lon"], rc)} for c in hasil]
    hitung = {}
    for cv in cak:
        for i in cv:
            hitung[i] = hitung.get(i, 0) + 1
    simpan = []
    for k in sorted(range(len(hasil)), key=lambda k: len(cak[k])):
        if all(hitung[i] > 1 for i in cak[k]):
            for i in cak[k]:
                hitung[i] -= 1
        else:
            simpan.append(k)
    out = []
    for k in sorted(simpan):
        c = hasil[k]
        areas = sorted({sampel[i]["area"] for i in cak[k] if sampel[i]["area"]})
        out.append({"lat": round(c["lat"], 4), "lon": round(c["lon"], 4), "r_km": r_kueri_km, "area": areas})
    return out


# ---------------------------------------------------------------- API Xweather
def _params(kred, extra):
    return {"client_id": kred[0], "client_secret": kred[1], **extra}


def http_get(url, params, timeout=30):
    import requests
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"success": False, "error": {"code": f"http_{r.status_code}", "description": r.text[:160]}}


def kueri(url, params, filt, get=http_get):
    """Satu kueri Xweather. Mengembalikan (daftar sambaran mentah, filter_api_dipakai).
    Bila server menolak parameter filter, diulang sekali tanpa filter (penyaringan dilakukan lokal)."""
    p = dict(params)
    if filt:
        p["filter"] = filt
    status, js = get(url, p)
    err = (js or {}).get("error") or {}
    if js and js.get("success") is False and filt and err.get("code") in ("invalid_filter", "invalid_query", "invalid_request"):
        p.pop("filter", None)
        status, js = get(url, p)
        err = (js or {}).get("error") or {}
        filt = None
    if not js or (js.get("success") is False and err.get("code") != "warn_no_data"):
        raise GagalAmbil(f"{err.get('code') or 'http_' + str(status)}: {err.get('description') or ''}".strip(": "))
    resp = js.get("response") or []
    if isinstance(resp, dict):
        resp = [resp]
    return resp, bool(filt)


def ambil(kred, cfg, lingkaran, aset, now, get=http_get, log=print):
    """Ambil semua sambaran sesuai mode. Mengembalikan (sambaran_mentah_unik, info)."""
    base = cfg.get("endpoint", "https://data.api.xweather.com/lightning").rstrip("/")
    filt = (cfg.get("filter") or "").strip() or None
    mentah, n_req, terpotong, gagal = {}, 0, 0, []
    filter_lokal = False
    if cfg.get("mode") == "enterprise":
        pad = cfg["radius_km"]["pantau"] + 5
        lim = int(cfg.get("limit_enterprise", 50000))
        tugas = []
        for a, (x0, y0, x1, y1) in aset.bbox_area.items():
            dl = pad / 111.0
            tugas.append((a, f"{base}/within", {"p": f"{y0 - dl:.4f},{x0 - dl:.4f},{y1 + dl:.4f},{x1 + dl:.4f}",
                                                 "from": f"-{int(cfg.get('lookback_enterprise_menit', 15))}minutes",
                                                 "limit": lim}))
    else:
        lim = int(cfg.get("limit", 1000))
        tugas = [(",".join(c["area"]), f"{base}/closest", {"p": f"{c['lat']},{c['lon']}", "radius": f"{c['r_km']}km", "limit": lim})
                 for c in lingkaran]
    for label, url, extra in tugas:
        try:
            resp, pakai_filter = kueri(url, _params(kred, extra), filt, get=get)
        except GagalAmbil as e:
            gagal.append(f"{label}: {e}")
            n_req += 1
            if len(gagal) >= 3 and not mentah:  # kredensial/langganan bermasalah → hentikan, hemat kuota
                break
            continue
        n_req += 1
        if filt and not pakai_filter:
            filter_lokal = True
        if len(resp) >= lim:
            terpotong += 1
        for r in resp:
            if r.get("id") is not None:
                mentah[str(r["id"])] = r
    if gagal and not mentah and len(gagal) >= min(3, len(tugas)):
        raise GagalAmbil("; ".join(gagal[:3]))
    log(f"   {n_req} kueri, {len(mentah)} sambaran unik, {terpotong} kueri terpotong limit, {len(gagal)} gagal")
    return list(mentah.values()), {"kueri": n_req, "terpotong": terpotong, "gagal": gagal[:10],
                                   "filter_lokal": filter_lokal, "filter": filt}


def parse_sambaran(r):
    """Objek Xweather → dict sambaran ringkas. None bila tidak valid."""
    loc, ob = r.get("loc") or {}, r.get("ob") or {}
    lat, lon = loc.get("lat"), loc.get("long", loc.get("lon"))
    if not valid_ll(lat, lon):
        return None
    ts = ob.get("timestamp")
    try:
        t = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        iso = ob.get("dateTimeISO")
        try:
            t = datetime.fromisoformat(str(iso)).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None
    pulse = ob.get("pulse") or {}
    tipe = str(pulse.get("type") or "").upper() or None
    amp = pulse.get("peakamp")
    try:
        amp = round(float(amp) / 1000.0, 1)
    except (TypeError, ValueError):
        amp = None
    return {"id": str(r.get("id")), "lat": round(float(lat), 4), "lon": round(float(lon), 4),
            "waktu_utc": t.isoformat(timespec="seconds"), "tipe": tipe, "arus_ka": amp,
            "sensor": pulse.get("numSensors"), "_t": t}


# ---------------------------------------------------------------- analisis
def analisis(raw, aset, cfg, filter_lokal=False):
    r = cfg["radius_km"]
    keep = r["pantau"]
    dekat = cfg.get("sambaran_dekat_km", 1.0)
    hanya_cg = (cfg.get("filter") or "").lower() == "cg"
    out, n_total, n_cg, n_ic = [], 0, 0, 0
    for x in raw:
        s = parse_sambaran(x)
        if not s:
            continue
        n_total += 1
        if s["tipe"] == "CG":
            n_cg += 1
        elif s["tipe"] == "IC":
            n_ic += 1
        if hanya_cg and s["tipe"] == "IC":
            continue
        per_area = {}
        for a in aset.by_area:
            if not aset.dekat_bbox(s["lat"], s["lon"], a, keep + 1):
                continue
            d, _ = aset.terdekat(s["lat"], s["lon"], a)
            if d <= keep:
                per_area[a] = round(d, 2)
        if not per_area:
            continue
        d, props = aset.terdekat(s["lat"], s["lon"])
        fd, fp = aset.fasilitas_terdekat(s["lat"], s["lon"])
        s.update({"jarak_km": round(d, 2), "area": props["area"], "aset": props.get("name"), "jenis_aset": props.get("kind"),
                  "fasilitas": (fp or {}).get("name"), "jarak_fasilitas_km": round(fd, 1) if fd is not None else None,
                  "per_area": per_area, "status": classify(d, r), "dekat_aset": d <= dekat})
        out.append(s)
    return out, {"total": n_total, "cg": n_cg, "ic": n_ic}


def gabung_lama(baru, lama, now, simpan_menit):
    batas = now - timedelta(minutes=simpan_menit)
    semua = {}
    for v in (lama or {}).get("sambaran", []):
        try:
            t = datetime.fromisoformat(v["waktu_utc"])
        except (KeyError, TypeError, ValueError):
            continue
        if t >= batas:
            semua[v["id"]] = {**v, "_t": t}
    for s in baru:
        if s["_t"] >= batas:
            semua[s["id"]] = s
    hasil = sorted(semua.values(), key=lambda s: (URUT[s["status"]], -s["_t"].timestamp()))
    return hasil


def ringkas_area(sambaran, areas, cfg, now):
    """Status tiap wilayah berdasarkan sambaran dalam jendela all_clear_menit terakhir."""
    r, ac = cfg["radius_km"], cfg.get("all_clear_menit", 30)
    batas = now - timedelta(minutes=ac)
    out = {}
    for a in areas:
        hit = [(s["_t"], d) for s in sambaran for aa, d in s.get("per_area", {}).items() if aa == a]
        aktif = [(t, d) for t, d in hit if t >= batas]
        st = None
        for t, d in aktif:
            c = classify(d, r)
            if c != "luar" and (st is None or URUT[c] < URUT[st]):
                st = c
        terakhir = max((t for t, _ in hit), default=None)
        dekat = min((d for _, d in aktif), default=None)
        last_crit = max((t for t, d in aktif if classify(d, r) in ("kritis", "waspada")), default=None)
        out[a] = {"status": st, "jumlah_30m": len(aktif), "jumlah_simpan": len(hit),
                  "terdekat_km": dekat, "terakhir_utc": terakhir.isoformat(timespec="seconds") if terakhir else None,
                  "aman_setelah_utc": (last_crit + timedelta(minutes=ac)).isoformat(timespec="seconds") if last_crit else None}
    return out


def _bersih(s):
    return {k: v for k, v in s.items() if not k.startswith("_")}


def arsipkan(sambaran, now):
    """Simpan sambaran (≤ radius pantau) ke arsip harian WIB — bukti audit, dedup per id."""
    ARSIP.mkdir(parents=True, exist_ok=True)
    per_hari = {}
    for s in sambaran:
        per_hari.setdefault(s["_t"].astimezone(WIB).strftime("%Y-%m-%d"), []).append(_bersih(s))
    for hari, daftar in per_hari.items():
        f = ARSIP / f"{hari}.json"
        lama = {}
        if f.exists():
            try:
                lama = {x["id"]: x for x in json.loads(f.read_text(encoding="utf-8")).get("sambaran", [])}
            except Exception:  # noqa
                lama = {}
        n0 = len(lama)
        for x in daftar:
            lama.setdefault(x["id"], x)
        if len(lama) == n0 and f.exists():
            continue
        isi = sorted(lama.values(), key=lambda x: x["waktu_utc"])
        ring = {k: sum(1 for x in isi if x["status"] == k) for k in ("kritis", "waspada", "pantau")}
        per_area = {}
        for x in isi:
            per_area[x["area"]] = per_area.get(x["area"], 0) + 1
        obj = {"tanggal_wib": hari, "diperbarui": now.isoformat(timespec="seconds"), "jumlah": {"total": len(isi), **ring},
               "per_area": per_area, "sambaran": isi}
        _tulis(f, obj)


def _tulis(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def sidik(obj):
    """Ringkasan isi yang bermakna (untuk memutuskan perlu commit atau tidak)."""
    return json.dumps({"ids": sorted(s["id"] for s in obj.get("sambaran", [])),
                       "area": {k: v.get("status") for k, v in (obj.get("area") or {}).items()},
                       "ok": (obj.get("status") or {}).get("ok")}, sort_keys=True)


# ---------------------------------------------------------------- satu putaran
def putaran(kred, cfg, aset, lingkaran, now, lama, get=http_get, log=print):
    raw, info = ambil(kred, cfg, lingkaran, aset, now, get=get, log=log)
    baru, jml = analisis(raw, aset, cfg, info["filter_lokal"])
    semua = gabung_lama(baru, lama, now, cfg.get("simpan_menit", 90))
    areas = list(aset.by_area)
    ring = {k: sum(1 for s in semua if s["status"] == k) for k in ("kritis", "waspada", "pantau")}
    standar = cfg.get("mode") != "enterprise"
    obj = {
        "diperbarui": now.isoformat(timespec="seconds"),
        "sumber": "Vaisala Xweather Lightning API (GLD360)",
        "mode": "enterprise" if not standar else "standard",
        "jendela_data_menit": 5 if standar else int(cfg.get("lookback_enterprise_menit", 15)),
        "filter": info["filter"], "radius_km": cfg["radius_km"], "sambaran_dekat_km": cfg.get("sambaran_dekat_km", 1.0),
        "all_clear_menit": cfg.get("all_clear_menit", 30), "simpan_menit": cfg.get("simpan_menit", 90),
        "jumlah": {"kueri": info["kueri"], "terpotong": info["terpotong"], "terdeteksi_run_ini": jml["total"],
                   "cg_run_ini": jml["cg"], "ic_run_ini": jml["ic"], "baru_dekat_aset": len(baru),
                   "dekat_aset": len(semua), **ring},
        "area": ringkas_area(semua, areas, cfg, now),
        "lingkaran": [[c["lat"], c["lon"], c["r_km"], c["area"]] for c in lingkaran] if standar else [],
        "status": {"ok": True, "waktu": now.isoformat(timespec="seconds"), "terakhir_ok": now.isoformat(timespec="seconds"),
                   "pesan": ("Sebagian kueri gagal: " + "; ".join(info["gagal"][:3])) if info["gagal"] else ""},
        "sambaran": [_bersih(s) for s in semua],
    }
    arsipkan(baru, now)
    return obj


def baca_lama():
    if not OUT.exists():
        return None
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:  # noqa
        return None


def main(get=http_get, now_fn=None, env=None, sleep=time.sleep, log=print):
    env = os.environ if env is None else env
    cfg = json.loads((ROOT / "config" / "monitoring.json").read_text(encoding="utf-8")).get("petir") or {}
    cid, csec = (env.get("XWEATHER_CLIENT_ID") or "").strip(), (env.get("XWEATHER_CLIENT_SECRET") or "").strip()
    if not cfg.get("aktif") or not cid or not csec:
        log("Modul petir tidak aktif (XWEATHER_CLIENT_ID/XWEATHER_CLIENT_SECRET belum diset). Tidak ada data yang diubah.")
        return 0
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    aset = IndeksAset(json.loads(ASET.read_text(encoding="utf-8")))
    lingkaran = []
    if cfg.get("mode") != "enterprise":
        lingkaran = lingkaran_cakupan(aset.fc, cfg.get("radius_kueri_km", 100), cfg["radius_km"]["pantau"], cfg.get("jarak_sampel_km", 2))
        log(f"Petir mode standard: {len(lingkaran)} lingkaran kueri r={cfg.get('radius_kueri_km', 100)} km menutup semua aset")
    pol = cfg.get("polling") or {}
    n_put = max(1, int(env.get("PETIR_PUTARAN") or pol.get("jumlah", 1)))
    jeda = float(pol.get("jeda_detik", 240))
    hb = float(cfg.get("heartbeat_menit", 30))
    kode = 0
    for i in range(n_put):
        if i:
            sleep(jeda)
        now = now_fn()
        lama = baca_lama()
        try:
            obj = putaran((cid, csec), cfg, aset, lingkaran, now, lama, get=get, log=log)
        except Exception as e:  # noqa — sumber gagal: data lama dibiarkan, hanya status yang ditandai
            pesan = str(e).replace(cid, "***").replace(csec, "***")[:240]
            log(f"Gagal mengambil petir: {pesan}")
            if lama:
                lama["status"] = {"ok": False, "waktu": now.isoformat(timespec="seconds"), "pesan": pesan,
                                  "terakhir_ok": (lama.get("status") or {}).get("terakhir_ok") or lama.get("diperbarui")}
                _tulis(OUT, lama)
            kode = 1
            continue
        obj["status"]["pesan"] = obj["status"]["pesan"].replace(cid, "***").replace(csec, "***")
        try:
            umur = (now - datetime.fromisoformat(lama["diperbarui"])).total_seconds() / 60 if lama else 1e9
        except Exception:  # noqa
            umur = 1e9
        if lama and sidik(obj) == sidik(lama) and umur < hb:
            log(f"Putaran {i + 1}/{n_put}: tidak ada perubahan bermakna (data terakhir {umur:.0f} menit lalu) — berkas tidak ditulis ulang.")
            continue
        _tulis(OUT, obj)
        j = obj["jumlah"]
        log(f"Putaran {i + 1}/{n_put}: {j['terdeteksi_run_ini']} sambaran terdeteksi · dekat aset {j['dekat_aset']} "
            f"(kritis {j['kritis']} · waspada {j['waspada']} · pantau {j['pantau']}) · {j['kueri']} kueri")
        kode = 0
    return kode


if __name__ == "__main__":
    sys.exit(main())
