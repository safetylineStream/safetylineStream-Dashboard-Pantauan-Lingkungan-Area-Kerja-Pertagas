"""
gdacs.py — Siklon tropis & bencana besar (GDACS) terhadap jalur pipa & fasilitas.

Sumber : GDACS — Global Disaster Alert and Coordination System (PBB & Komisi Eropa/JRC), API publik tanpa kunci.
         https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH
Jenis  : Peringatan otomatis skala global (level Green/Orange/Red = perkiraan dampak kemanusiaan, bukan peringatan resmi BMKG).
Spasial: jarak geodesik kejadian → aset terdekat per wilayah; untuk siklon tropis, poligon lintasan/zona angin GDACS
         ditumpangkan ke aset. Kejadian di luar `radius_tampil_km` tidak disimpan.
         Gempa (EQ) dan kebakaran (WF) tidak diambil karena sudah dipantau dari BMKG dan SiPongi/FIRMS.

Skor peluang P (dipakai klasifikasi multi-bahaya, K = 3):
  Siklon : aset di zona angin Red (≥ 120 km/jam) 5 · Orange (≥ 90) 4 · Green (≥ 60) 3 · di kerucut prakiraan 2 · ≤ `tc_pantau_km` 1
  Banjir/gunung api : ≤ `dekat_km` & level Orange/Red 3 · ≤ `pantau_km` & Orange/Red 2 · ≤ `pantau_km` & Green 1

Keluaran: data/gdacs/gdacs.json  dan  data/gdacs/geometri.geojson (lintasan & zona siklon)
"""
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo import haversine  # noqa: E402
from umum import DATA, ambil, baca_json, cfg, iso, log, muat_aset, sekarang, tulis_json  # noqa: E402

OUT = DATA / "gdacs"
CFG = cfg("gdacs", {
    "url": "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH",
    "jenis": ["TC", "FL", "VO"],
    "hari_ke_belakang": 10,
    "kejadian_aktif_hari": 3,
    "radius_tampil_km": {"TC": 1500, "FL": 150, "VO": 150},
    "tc_pantau_km": 500,
    "dekat_km": {"FL": 25, "VO": 30},
    "pantau_km": {"FL": 100, "VO": 100},
})
JENIS = {"TC": "Siklon tropis", "FL": "Banjir", "VO": "Gunung api", "EQ": "Gempa bumi", "WF": "Kebakaran hutan", "DR": "Kekeringan"}
KELAS_TC = {"poly_red": (5, "zona angin ≥ 120 km/jam"), "poly_orange": (4, "zona angin ≥ 90 km/jam"),
            "poly_green": (3, "zona angin ≥ 60 km/jam"), "poly_cones": (2, "kerucut prakiraan lintasan")}


def waktu(s):
    """Tanggal GDACS ('2026-11-10T06:00:00', UTC) → datetime sadar zona."""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


class Aset:
    def __init__(self, garis, fas):
        from shapely.geometry import MultiLineString, MultiPoint
        from shapely.ops import unary_union
        self.per = {}
        for a in sorted({g["area"] for g in garis} | {f["area"] for f in fas}):
            bag = []
            ln = [g["coords"] for g in garis if g["area"] == a]
            pt = [(f["lon"], f["lat"]) for f in fas if f["area"] == a]
            if ln:
                bag.append(MultiLineString(ln))
            if pt:
                bag.append(MultiPoint(pt))
            self.per[a] = unary_union(bag)

    def jarak_km(self, geom):
        """Jarak geodesik (km) geometri → aset terdekat tiap wilayah. 0 bila bersinggungan."""
        from shapely.ops import nearest_points
        out = {}
        for a, g in self.per.items():
            if g.is_empty:
                continue
            if g.intersects(geom):
                out[a] = 0.0
                continue
            p, q = nearest_points(g, geom)
            out[a] = round(haversine(p.y, p.x, q.y, q.x), 1)
        return out


def ambil_kejadian():
    now = sekarang()
    feats, hal = [], 1
    while hal <= 5:
        r = ambil(CFG["url"], params={"eventlist": ";".join(CFG["jenis"]), "alertlevel": "Green;Orange;Red", "pageSize": 100, "pageNumber": hal,
                                      "fromDate": (now - timedelta(days=CFG["hari_ke_belakang"])).strftime("%Y-%m-%d"), "toDate": (now + timedelta(days=1)).strftime("%Y-%m-%d")},
                  timeout=(20, 60))
        if not r.text.strip():
            break
        f = (r.json() or {}).get("features") or []
        feats += f
        if len(f) < 100:
            break
        hal += 1
        time.sleep(1)
    return feats


def geometri_tc(url):
    """Unduh geometri siklon (lintasan, kerucut, zona angin) → [(kelas, shapely geom, properti ringkas)]."""
    from shapely.geometry import shape
    out = []
    js = ambil(url, timeout=(20, 60), coba=2).json()
    for f in (js or {}).get("features") or []:
        g, p = f.get("geometry"), f.get("properties") or {}
        if not g or g.get("type") == "Point":
            continue
        try:
            s = shape(g)
            if not s.is_valid:
                s = s.buffer(0)
        except Exception:  # noqa: BLE001
            continue
        if not s.is_empty:
            out.append((str(p.get("Class") or p.get("class") or "").lower(), s))
    return out


def nilai_tc(geoms, aset, titik, level):
    """→ {area: (P, keterangan, jarak_km)} untuk siklon tropis."""
    from shapely.geometry import Point
    hasil = {}
    polig = [(k, s) for k, s in geoms if s.geom_type in ("Polygon", "MultiPolygon")]
    for kelas, (P, ket) in KELAS_TC.items():
        for k, s in polig:
            if not k.startswith(kelas):
                continue
            for a, d in aset.jarak_km(s).items():
                if d == 0 and P > hasil.get(a, (0,))[0]:
                    hasil[a] = (P, f"aset berada di {ket}", 0.0)
    # poligon tanpa kelas yang dikenali → pakai level peringatan kejadian
    for k, s in polig:
        if any(k.startswith(x) for x in KELAS_TC):
            continue
        P = {"red": 4, "orange": 3}.get(level.lower(), 2)
        for a, d in aset.jarak_km(s).items():
            if d == 0 and P > hasil.get(a, (0,))[0]:
                hasil[a] = (P, "aset berada di area dampak siklon (GDACS)", 0.0)
    semua = [s for _, s in geoms] + [Point(titik)]
    for a in aset.per:
        d = min((aset.jarak_km(s).get(a, 1e9) for s in semua), default=1e9)
        if a not in hasil:
            hasil[a] = ((1 if d <= CFG["tc_pantau_km"] else 0), f"{d:,.0f} km dari lintasan/zona siklon".replace(",", "."), d)
    return hasil


def nilai_titik(jenis, jarak, level):
    naik = level.lower() in ("orange", "red")
    if jarak <= CFG["dekat_km"].get(jenis, 25) and naik:
        return 3
    if jarak <= CFG["pantau_km"].get(jenis, 100):
        return 2 if naik else 1
    return 0


def proses(feats, aset, ambil_geom=geometri_tc, now=None):
    """Fitur GDACS → (kejadian[], fitur_geojson[])."""
    from shapely.geometry import Point, mapping
    now = now or sekarang()
    kejadian, gj, lihat = [], [], set()
    for f in feats:
        p, g = f.get("properties") or {}, f.get("geometry") or {}
        jenis = str(p.get("eventtype") or "").upper()
        if jenis not in CFG["jenis"] or g.get("type") != "Point":
            continue
        kunci = (jenis, p.get("eventid"))
        if kunci in lihat:
            continue
        lihat.add(kunci)
        sampai = waktu(p.get("todate"))
        aktif = str(p.get("iscurrent")).lower() == "true"
        if not aktif and (not sampai or (now - sampai).total_seconds() > CFG["kejadian_aktif_hari"] * 86400):
            continue
        lon, lat = float(g["coordinates"][0]), float(g["coordinates"][1])
        level = str(p.get("alertlevel") or "Green")
        jarak = aset.jarak_km(Point(lon, lat))
        if not jarak or min(jarak.values()) > CFG["radius_tampil_km"].get(jenis, 150):
            continue
        sev = p.get("severitydata") or {}
        url = p.get("url") or {}
        per_area, geoms = {}, []
        if jenis == "TC":
            try:
                geoms = ambil_geom(url.get("geometry")) if url.get("geometry") else []
            except Exception as e:  # noqa: BLE001
                log(f"  geometri siklon {p.get('eventid')} gagal: {str(e)[:120]}")
            for a, (P, ket, d) in nilai_tc(geoms, aset, (lon, lat), level).items():
                per_area[a] = {"P": P, "jarak_km": round(d, 1), "ket": ket}
            for kelas, s in geoms:
                s2 = s.simplify(0.02)
                if not s2.is_empty:
                    gj.append({"type": "Feature", "properties": {"eventid": p.get("eventid"), "kelas": kelas, "nama": p.get("eventname") or p.get("name")},
                               "geometry": mapping(s2)})
        else:
            for a, d in jarak.items():
                per_area[a] = {"P": nilai_titik(jenis, d, level), "jarak_km": d, "ket": f"{d:,.0f} km dari aset".replace(",", ".")}
        kejadian.append({
            "id": f"{jenis}{p.get('eventid')}", "jenis": jenis, "jenis_nama": JENIS.get(jenis, jenis), "nama": p.get("eventname") or p.get("name") or "",
            "deskripsi": (p.get("description") or p.get("name") or "")[:240], "level": level, "level_episode": p.get("episodealertlevel"),
            "negara": p.get("country"), "lat": round(lat, 3), "lon": round(lon, 3), "mulai": p.get("fromdate"), "sampai": p.get("todate"),
            "aktif": aktif, "keparahan": sev.get("severitytext"), "tautan": url.get("report"),
            "lokasi_perkiraan": jenis == "FL", "jarak_min_km": round(min(jarak.values()), 1), "per_area": per_area,
        })
    kejadian.sort(key=lambda k: (-max((v["P"] for v in k["per_area"].values()), default=0), k["jarak_min_km"]))
    return kejadian, gj


def main():
    t0 = time.time()
    lama = baca_json(OUT / "gdacs.json", {}) or {}
    try:
        areas, garis, fas = muat_aset()
        feats = ambil_kejadian()
        log(f"GDACS: {len(feats)} kejadian global {CFG['hari_ke_belakang']} hari terakhir ({', '.join(CFG['jenis'])})")
        kejadian, gj = proses(feats, Aset(garis, fas))
        tulis_json(OUT / "gdacs.json", {
            "sumber": "GDACS — Global Disaster Alert and Coordination System (PBB & Komisi Eropa)", "url": "https://www.gdacs.org/",
            "jenis": "Peringatan otomatis skala global — bukan peringatan resmi BMKG/BNPB",
            "catatan": "Level Green/Orange/Red GDACS adalah perkiraan dampak kemanusiaan. Lokasi banjir GDACS berupa titik perkiraan, bukan batas genangan.",
            "diambil": iso(), "jenis_dipantau": CFG["jenis"], "radius_tampil_km": CFG["radius_tampil_km"], "global": len(feats),
            "kejadian": kejadian, "status": {"ok": True, "pesan": None}, "durasi_detik": round(time.time() - t0)})
        tulis_json(OUT / "geometri.geojson", {"type": "FeatureCollection", "features": gj})
        log(f"Selesai: {len(kejadian)} kejadian dalam radius pantau aset.")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        lama["status"] = {"ok": False, "pesan": str(e)[:200], "diperiksa": iso()}
        lama.setdefault("kejadian", [])
        tulis_json(OUT / "gdacs.json", lama)


if __name__ == "__main__":
    main()
