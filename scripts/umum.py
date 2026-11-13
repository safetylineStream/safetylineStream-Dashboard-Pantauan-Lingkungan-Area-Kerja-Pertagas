"""
umum.py — utilitas bersama modul bahaya tambahan PERTAGAS WASPADA
(peringatan_cuaca.py, banjir_sungai.py, petir_model.py, gdacs.py).

Semua modul itu berbasis spasial: setiap data ditumpangkan ke jalur pipa & fasilitas di data/assets.geojson.
"""
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from geo import haversine

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG_ALL = json.loads((ROOT / "config" / "monitoring.json").read_text(encoding="utf-8"))
WIB = timezone(timedelta(hours=7))
UA = {"User-Agent": "Pertagas-QMHSE-EnvDashboard/1.0 (+github actions; monitoring aset pipa)"}


def log(*a):
    print(f"[{datetime.now(WIB):%H:%M:%S}]", *a, flush=True)


def cfg(nama, bawaan):
    """Pengaturan modul = nilai bawaan ditimpa blok config/monitoring.json (kunci berawalan _ diabaikan)."""
    return {**bawaan, **{k: v for k, v in CFG_ALL.get(nama, {}).items() if not k.startswith("_")}}


def sekarang():
    return datetime.now(WIB)


def iso(dt=None):
    return (dt or sekarang()).strftime("%Y-%m-%dT%H:%M+07:00")


def ambil(url, params=None, timeout=(15, 60), coba=3, jeda=6, headers=None):
    """GET dengan percobaan ulang. HTTP 429 (batas laju) ditunggu lebih lama. Mengembalikan objek Response."""
    akhir = None
    for i in range(coba):
        try:
            r = requests.get(url, params=params, headers={**UA, **(headers or {})}, timeout=timeout)
            if r.status_code == 429:
                raise RuntimeError("HTTP 429 — batas laju penyedia data tercapai")
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            akhir = e
            if i < coba - 1:
                tunggu = jeda * (i + 1) * (5 if "429" in str(e) else 1)
                log(f"  percobaan {i + 1} gagal ({str(e)[:140]}) — ulang dalam {tunggu} dtk")
                time.sleep(tunggu)
    raise akhir


def baca_json(path, bawaan=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return bawaan


def tulis_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def umur_menit(iso_str):
    try:
        return (sekarang() - datetime.fromisoformat(iso_str)).total_seconds() / 60
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ aset
def densify(coords, step_m):
    """Sisipkan titik sepanjang garis sehingga jarak antar-titik ≤ step_m."""
    out = []
    for (x0, y0), (x1, y1) in zip(coords[:-1], coords[1:]):
        n = max(1, int(math.ceil(haversine(y0, x0, y1, x1) * 1000 / step_m)))
        for i in range(n):
            t = i / n
            out.append((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t))
    if coords:
        out.append(tuple(coords[-1]))
    return out


def muat_aset(path=None):
    """→ (areas, garis, fasilitas). garis: {area, aset, coords[(lon,lat)]}; fasilitas: {area, aset, lon, lat}."""
    fc = json.loads(Path(path or DATA / "assets.geojson").read_text(encoding="utf-8"))
    garis, fas = [], []
    for f in fc.get("features", []):
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        t = g.get("type")
        if t in ("LineString", "MultiLineString"):
            for part in ([g["coordinates"]] if t == "LineString" else g["coordinates"]):
                c = [(float(x[0]), float(x[1])) for x in part]
                if len(c) >= 2:
                    garis.append({"area": p.get("area"), "aset": p.get("name"), "coords": c})
        elif t == "Point":
            fas.append({"area": p.get("area"), "aset": p.get("name"), "lon": float(g["coordinates"][0]), "lat": float(g["coordinates"][1])})
    return fc.get("areas", {}), garis, fas


def panjang_km(coords):
    return sum(haversine(a[1], a[0], b[1], b[0]) for a, b in zip(coords[:-1], coords[1:]))


def pusat_sel(lon, lat, ukuran):
    """Pusat sel grid berukuran `ukuran` derajat yang memuat titik (lon, lat)."""
    return (round(math.floor(lon / ukuran) * ukuran + ukuran / 2, 4), round(math.floor(lat / ukuran) * ukuran + ukuran / 2, 4))


def kuantil(urut, q):
    """Kuantil (0–1) dari daftar yang SUDAH terurut naik, interpolasi linear."""
    if not urut:
        return None
    k = (len(urut) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return urut[lo] + (urut[hi] - urut[lo]) * (k - lo)
