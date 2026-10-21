"""
kapal.py — lalu lintas kapal (AIS) di atas pipa offshore OEJA.

Sumber: AISStream.io (websocket, gratis dengan API key; data AIS terestrial dari jaringan penerima
sukarela). Key dibaca dari env AISSTREAM_API_KEY (GitHub Secret) — TIDAK pernah ditulis ke repo.
Tanpa key, skrip berhenti tanpa membuat/mengubah data apa pun.

Alur per run:
  1. Langganan posisi kapal dalam bbox perairan Jawa Timur selama `jendela_detik`.
  2. Gabungkan dengan posisi run sebelumnya yang belum lebih tua dari `simpan_menit`
     (kapal berlabuh hanya memancar ±tiap 3 menit, kapal kelas B lebih jarang).
  3. Hitung jarak tegak lurus tiap kapal ke ruas pipa OFFSHORE (data/kapal/pipa_offshore.geojson).
  4. Klasifikasi: kritis  = ≤ radius kritis DAN diam/berlabuh (potensi jangkar jatuh/terseret)
                  waspada = ≤ radius kritis bergerak, atau ≤ radius waspada diam
                  pantau  = ≤ radius pantau
  5. Tulis data/kapal/oeja.json secara atomik.

Waktu posisi = waktu dari pesan AIS (bukan waktu run). Gagal ambil → file lama dibiarkan apa adanya.
"""
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "kapal"
OUT = DATA / "oeja.json"
PIPA = DATA / "pipa_offshore.geojson"
URL = "wss://stream.aisstream.io/v0/stream"

NAV = {0: "Berlayar (mesin)", 1: "Berlabuh jangkar", 2: "Tidak terkendali (NUC)", 3: "Olah gerak terbatas",
       4: "Terbatas sarat", 5: "Tambat", 6: "Kandas", 7: "Menangkap ikan", 8: "Berlayar (layar)",
       14: "AIS-SART", 15: "Tidak ditentukan"}
DIAM_NAV = {1, 2, 5, 6}  # status yang berarti kapal diam/berpotensi menurunkan jangkar


def tipe_kapal(t):
    try:
        t = int(t)
    except (TypeError, ValueError):
        return None
    if t == 30: return "Penangkap ikan"
    if t in (31, 32): return "Penarik (towing)"
    if t == 33: return "Kapal keruk"
    if t == 34: return "Penyelaman"
    if t == 35: return "Militer"
    if t in (36, 37): return "Pesiar/layar"
    if t == 50: return "Pandu"
    if t == 51: return "SAR"
    if t == 52: return "Tunda (tug)"
    if t == 53: return "Tender pelabuhan"
    if t == 55: return "Penegak hukum"
    if 40 <= t <= 49: return "Kecepatan tinggi"
    if 60 <= t <= 69: return "Penumpang"
    if 70 <= t <= 79: return "Kargo"
    if 80 <= t <= 89: return "Tanker"
    if 20 <= t <= 29: return "Wing-in-ground"
    if 90 <= t <= 99: return "Lainnya"
    return None


def parse_waktu(s):
    """'2026-10-21 07:31:02.123456789 +0000 UTC' → datetime UTC."""
    if not s:
        return None
    try:
        s = str(s).replace(" UTC", "").strip()
        tgl, jam = s.split(" ")[:2]
        jam = jam.split(".")[0]
        return datetime.fromisoformat(f"{tgl}T{jam}+00:00")
    except Exception:  # noqa
        return None


def _knot(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v >= 102.3 or v < 0 else round(v, 1)  # 102.3 = tidak tersedia


def _deg(v, maks=360):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v < 0 or v >= maks else round(v, 1)


def olah_pesan(msg, kapal, statis):
    """Perbarui dict kapal/statis dari satu pesan AISStream. Mengembalikan True bila pesan posisi valid."""
    tipe = msg.get("MessageType")
    meta = msg.get("MetaData") or {}
    body = (msg.get("Message") or {}).get(tipe) or {}
    mmsi = meta.get("MMSI") or body.get("UserID")
    if not mmsi:
        return False
    mmsi = int(mmsi)
    nama = (meta.get("ShipName") or "").strip() or None
    if tipe in ("ShipStaticData", "StaticDataReport"):
        st = statis.setdefault(mmsi, {})
        if tipe == "ShipStaticData":
            st.update({k: v for k, v in {
                "nama": (body.get("Name") or "").strip() or nama, "tipe_kode": body.get("Type"),
                "callsign": (body.get("CallSign") or "").strip() or None, "imo": body.get("ImoNumber") or None,
                "tujuan": (body.get("Destination") or "").strip() or None,
                "draft_m": body.get("MaximumStaticDraught") or None}.items() if v})
            dim = body.get("Dimension") or {}
            pj = (dim.get("A") or 0) + (dim.get("B") or 0)
            if pj:
                st["panjang_m"] = pj
        else:
            rb = body.get("ReportB") or {}
            ra = body.get("ReportA") or {}
            if ra.get("Name"): st["nama"] = ra["Name"].strip()
            if rb.get("ShipType"): st["tipe_kode"] = rb["ShipType"]
            if rb.get("CallSign"): st["callsign"] = rb["CallSign"].strip()
        return False
    if tipe not in ("PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport"):
        return False
    lat = body.get("Latitude", meta.get("latitude"))
    lon = body.get("Longitude", meta.get("longitude"))
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0) or lat == 91 or lon == 181:
        return False
    t = parse_waktu(meta.get("time_utc"))
    if t is None:
        return False
    lama = kapal.get(mmsi)
    if lama and lama["_t"] >= t:
        return True
    kapal[mmsi] = {"mmsi": mmsi, "nama": nama or (lama or {}).get("nama"), "lat": round(lat, 5), "lon": round(lon, 5),
                   "sog": _knot(body.get("Sog")), "cog": _deg(body.get("Cog")), "heading": _deg(body.get("TrueHeading"), 360),
                   "nav": body.get("NavigationalStatus") if tipe == "PositionReport" else None,
                   "kelas": "A" if tipe == "PositionReport" else "B", "_t": t}
    return True


DIAG = {}  # diagnostik run terakhir (jenis pesan, uji global) — ditulis ke oeja.json, tanpa key


def _probe_global(key, detik=60):
    """Pesan dari wilayah Indonesia selama beberapa detik: membedakan masalah key/aliran vs cakupan wilayah.
    Mengembalikan ringkasan: jumlah pesan posisi, gagal parse, dan sebaran posisi per kotak 2°."""
    from collections import Counter
    from websockets.sync.client import connect
    n, gagal, sel, contoh = 0, 0, Counter(), None
    akhir = time.monotonic() + detik
    with connect(URL, open_timeout=30, close_timeout=5, max_size=2 ** 22) as ws:
        ws.send(json.dumps({"APIKey": key, "BoundingBoxes": [[[-12, 94], [7, 142]]], "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport"]}))
        while time.monotonic() < akhir:
            try:
                raw = ws.recv(timeout=max(1.0, akhir - time.monotonic()))
            except TimeoutError:
                break
            try:
                m = json.loads(raw)
            except Exception:  # noqa
                gagal += 1
                if contoh is None:
                    contoh = repr(raw[:80])
                continue
            md = m.get("MetaData") or {}
            if "latitude" in md:
                n += 1
                sel[f"{int(md['latitude'] // 2 * 2)},{int(md['longitude'] // 2 * 2)}"] += 1
    return {"pesan_posisi": n, "gagal_parse": gagal, "contoh_gagal": contoh, "sebaran_kotak2deg": dict(sel.most_common(25))}


def ambil(key, bbox, detik, log=print):
    """Kumpulkan pesan AIS dari AISStream selama `detik`. Mengembalikan (kapal, statis, n_pesan)."""
    from collections import Counter
    from websockets.sync.client import connect
    w, s, e, n = bbox
    jenis, contoh_lain = Counter(), None
    sub = {"APIKey": key, "BoundingBoxes": [[[s, w], [n, e]]],
           "FilterMessageTypes": ["PositionReport", "StandardClassBPositionReport", "ExtendedClassBPositionReport",
                                  "ShipStaticData", "StaticDataReport"]}
    kapal, statis, n_msg = {}, {}, 0
    akhir = time.monotonic() + detik
    with connect(URL, open_timeout=30, close_timeout=5, max_size=2 ** 22) as ws:
        ws.send(json.dumps(sub))
        while time.monotonic() < akhir:
            try:
                raw = ws.recv(timeout=max(1.0, akhir - time.monotonic()))
            except TimeoutError:
                break
            try:
                m = json.loads(raw)
            except Exception:  # noqa
                jenis["gagal_parse"] += 1
                continue
            if isinstance(m, dict) and m.get("error"):
                raise RuntimeError(f"AISStream menolak langganan: {m['error']}")
            n_msg += 1
            jenis[(m.get("MessageType") if isinstance(m, dict) else None) or "?"] += 1
            if not olah_pesan(m, kapal, statis) and contoh_lain is None:
                contoh_lain = json.dumps(m)[:300] if isinstance(m, dict) else str(m)[:300]
    log(f"   {n_msg} pesan AIS, {len(kapal)} kapal dengan posisi; jenis: {dict(jenis)}")
    DIAG.clear()
    DIAG.update({"jenis_pesan": dict(jenis), "contoh_pesan_non_posisi": contoh_lain})
    if len(kapal) == 0:
        try:
            DIAG["uji_indonesia_60dtk"] = _probe_global(key)
            log(f"   uji Indonesia 60 dtk: {DIAG['uji_indonesia_60dtk']}")
        except Exception as ex:  # noqa
            DIAG["uji_global_galat"] = str(ex).replace(key, "***")[:200]
    return kapal, statis, n_msg


class IndeksPipa:
    """Jarak titik → ruas pipa offshore (m), di proyeksi Transverse Mercator lokal (galat < 0,1%)."""

    def __init__(self, fc, lon0):
        from pyproj import Transformer
        from shapely.geometry import shape
        from shapely.ops import transform
        from shapely.strtree import STRtree
        self.tf = Transformer.from_crs("EPSG:4326", f"+proj=tmerc +lon_0={lon0} +k=1 +ellps=WGS84", always_xy=True)
        self.ruas, geoms = [], []
        for f in fc.get("features", []):
            if f["properties"].get("kind", "pipa") != "pipa":
                continue
            g = transform(self.tf.transform, shape(f["geometry"]))
            for part in getattr(g, "geoms", [g]):
                geoms.append(part)
                self.ruas.append(f["properties"])
        self.geoms = geoms
        self.tree = STRtree(geoms) if geoms else None

    def terdekat(self, lat, lon):
        from shapely.geometry import Point
        if not self.tree:
            return None, None
        p = Point(*self.tf.transform(lon, lat))
        i = self.tree.nearest(p)
        return self.geoms[int(i)].distance(p), self.ruas[int(i)]


def klasifikasi(jarak_km, diam, r):
    if jarak_km is None:
        return "luar"
    if jarak_km <= r["kritis"]:
        return "kritis" if diam else "waspada"
    if jarak_km <= r["waspada"]:
        return "waspada" if diam else "pantau"
    if jarak_km <= r["pantau"]:
        return "pantau"
    return "luar"


def analisis(kapal, statis, pipa_fc, cfg, now):
    r, v_diam = cfg["radius_km"], cfg.get("sog_diam_knot", 0.5)
    w, s, e, n = cfg["bbox"]
    idx = IndeksPipa(pipa_fc, (w + e) / 2)
    hasil = []
    for k in kapal.values():
        st = statis.get(k["mmsi"], {})
        d_m, ruas = idx.terdekat(k["lat"], k["lon"])
        jarak = round(d_m / 1000, 3) if d_m is not None else None
        diam = (k.get("nav") in DIAM_NAV) or (k.get("sog") is not None and k["sog"] < v_diam)
        tk = st.get("tipe_kode")
        hasil.append({
            "mmsi": k["mmsi"], "nama": st.get("nama") or k.get("nama") or None, "imo": st.get("imo"),
            "callsign": st.get("callsign"), "tipe": tipe_kapal(tk), "tipe_kode": tk, "panjang_m": st.get("panjang_m"),
            "tujuan": st.get("tujuan"), "lat": k["lat"], "lon": k["lon"], "sog": k.get("sog"), "cog": k.get("cog"),
            "heading": k.get("heading"), "nav": k.get("nav"), "nav_label": NAV.get(k.get("nav")), "kelas": k.get("kelas"),
            "diam": bool(diam), "waktu_utc": k["_t"].isoformat(timespec="seconds"),
            "umur_menit": round((now - k["_t"]).total_seconds() / 60, 1),
            "jarak_pipa_km": jarak, "ruas": (ruas or {}).get("name"),
            "status": klasifikasi(jarak, diam, r)})
    urut = {"kritis": 0, "waspada": 1, "pantau": 2, "luar": 3}
    hasil.sort(key=lambda x: (urut[x["status"]], x["jarak_pipa_km"] if x["jarak_pipa_km"] is not None else 1e9))
    return hasil[: cfg.get("maks_kapal_simpan", 1500)]


def gabung_lama(kapal, statis, lama, now, simpan_menit):
    """Bawa posisi run sebelumnya yang masih ≤ simpan_menit dan tidak tertimpa posisi baru."""
    batas = now - timedelta(minutes=simpan_menit)
    for v in (lama or {}).get("kapal", []):
        try:
            t = datetime.fromisoformat(v["waktu_utc"])
        except Exception:  # noqa
            continue
        if t < batas:
            continue
        m = int(v["mmsi"])
        if m not in kapal or kapal[m]["_t"] < t:
            kapal[m] = {"mmsi": m, "nama": v.get("nama"), "lat": v["lat"], "lon": v["lon"], "sog": v.get("sog"),
                        "cog": v.get("cog"), "heading": v.get("heading"), "nav": v.get("nav"), "kelas": v.get("kelas"), "_t": t}
        st = statis.setdefault(m, {})
        for a, b in (("nama", "nama"), ("imo", "imo"), ("callsign", "callsign"), ("tipe_kode", "tipe_kode"),
                     ("panjang_m", "panjang_m"), ("tujuan", "tujuan")):
            if v.get(b) and a not in st:
                st[a] = v[b]


def tulis(obj):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(OUT)


def main(fetch=None, now=None, env=None):
    env = os.environ if env is None else env
    cfg = json.loads((ROOT / "config" / "monitoring.json").read_text(encoding="utf-8"))["kapal"]
    key = (env.get("AISSTREAM_API_KEY") or "").strip()
    if not cfg.get("aktif") or not key:
        print("Modul kapal tidak aktif (AISSTREAM_API_KEY belum diset). Tidak ada data yang diubah.")
        return 0
    if not PIPA.exists():
        print("data/kapal/pipa_offshore.geojson belum ada — jalankan tools/pipa_offshore.py")
        return 1
    now = now or datetime.now(timezone.utc)
    lama = None
    if OUT.exists():
        try:
            lama = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:  # noqa
            lama = None
    fetch = fetch or ambil
    try:
        kapal, statis, n_msg = fetch(key, cfg["bbox"], cfg["jendela_detik"])
    except Exception as e:  # noqa — sumber gagal: data lama dibiarkan, hanya status yang ditandai
        pesan = str(e).replace(key, "***")[:200]
        print(f"Gagal mengambil AIS: {pesan}")
        if lama:
            lama["status"] = {"ok": False, "waktu": now.isoformat(timespec="seconds"), "pesan": pesan,
                              "terakhir_ok": (lama.get("status") or {}).get("terakhir_ok") or lama.get("diperbarui")}
            tulis(lama)
        return 1
    n_baru = len(kapal)
    gabung_lama(kapal, statis, lama, now, cfg.get("simpan_menit", 90))
    pipa_fc = json.loads(PIPA.read_text(encoding="utf-8"))
    hasil = analisis(kapal, statis, pipa_fc, cfg, now)
    ring = {k: sum(1 for x in hasil if x["status"] == k) for k in ("kritis", "waspada", "pantau", "luar")}
    obj = {
        "diperbarui": now.isoformat(timespec="seconds"),
        "sumber": "AISStream.io (AIS terestrial)",
        "jendela_detik": cfg["jendela_detik"], "simpan_menit": cfg.get("simpan_menit", 90),
        "bbox": cfg["bbox"], "radius_km": cfg["radius_km"], "sog_diam_knot": cfg.get("sog_diam_knot", 0.5),
        "pipa_offshore_km": pipa_fc.get("total_km"),
        "jumlah": {"total": len(hasil), "terlihat_run_ini": n_baru, "pesan": n_msg, **ring},
        "status": {"ok": True, "waktu": now.isoformat(timespec="seconds"), "terakhir_ok": now.isoformat(timespec="seconds")},
        "kapal": hasil,
    }
    if DIAG:
        obj["diagnostik"] = {k: (v.replace(key, "***") if isinstance(v, str) else v) for k, v in DIAG.items()}
    tulis(obj)
    print(f"Kapal: {len(hasil)} (baru {n_baru}) · kritis {ring['kritis']} · waspada {ring['waspada']} · pantau {ring['pantau']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
