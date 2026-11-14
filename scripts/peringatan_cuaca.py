"""
peringatan_cuaca.py — Peringatan dini cuaca ekstrem BMKG (nowcast) terhadap jalur pipa & fasilitas.

Sumber : BMKG — umpan RSS peringatan dini aktif + berkas CAP (Common Alerting Protocol) per provinsi.
         https://www.bmkg.go.id/alerts/nowcast/id  →  .../id/<KODE>_alert.xml
Jenis  : PERINGATAN RESMI BMKG (nowcasting, berlaku ±1–3 jam). Wajib mencantumkan BMKG sebagai sumber.
Spasial: poligon wilayah (kecamatan) dalam CAP ditumpangkan ke aset → km pipa & fasilitas di dalam poligon.

Keluaran: data/peringatan_cuaca/peringatan.json  dan  data/peringatan_cuaca/poligon.geojson
Hanya peringatan yang poligonnya menyentuh zona `radius_tampil_km` dari aset yang disimpan.
Berkas hanya ditulis ulang bila isi berubah atau sudah melewati `heartbeat_menit` (menghemat commit/build Pages).
Bila pengambilan gagal, data terakhir dipertahankan dan ditandai.
"""
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geo import haversine  # noqa: E402
from umum import DATA, WIB, ambil, baca_json, cfg, iso, log, muat_aset, sekarang, tulis_json, umur_menit  # noqa: E402

OUT = DATA / "peringatan_cuaca"
CFG = cfg("peringatan_cuaca", {
    "rss_url": "https://www.bmkg.go.id/alerts/nowcast/id",
    "radius_tampil_km": 25,
    "heartbeat_menit": 60,
    "maks_peringatan": 40,
    "simplify_derajat": 0.001,
})


def _lokal(tag):
    return tag.rsplit("}", 1)[-1]


def _anak(el, nama):
    return [c for c in el if _lokal(c.tag) == nama]


def _teks(el, nama):
    for c in el:
        if _lokal(c.tag) == nama:
            return (c.text or "").strip()
    return ""


def parse_rss(xml_text):
    """→ [{judul, link, kode, terbit, ringkas}] dari umpan RSS peringatan dini aktif."""
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter():
        if _lokal(item.tag) != "item":
            continue
        link = _teks(item, "link")
        if not link:
            continue
        kode = link.rstrip("/").split("/")[-1].replace("_alert.xml", "").replace(".xml", "")
        out.append({"judul": _teks(item, "title"), "link": link, "kode": kode, "terbit": _teks(item, "pubDate"), "ringkas": _teks(item, "description")})
    return out


def parse_poligon(teks):
    """'lat,lon lat,lon …' (CAP) → [(lon, lat), …]."""
    pts = []
    for pasangan in (teks or "").split():
        b = pasangan.split(",")
        if len(b) >= 2:
            try:
                pts.append((float(b[1]), float(b[0])))
            except ValueError:
                continue
    return pts


def parse_cap(xml_text):
    """Berkas CAP → dict (event, headline, deskripsi, mulai, berakhir, severity, …, poligon[[ (lon,lat) ]])."""
    root = ET.fromstring(xml_text)
    infos = _anak(root, "info")
    if not infos:
        return None
    info = next((i for i in infos if _teks(i, "language").lower().startswith("id")), infos[0])
    poligon, wilayah = [], []
    for area in _anak(info, "area"):
        d = _teks(area, "areaDesc")
        if d:
            wilayah.append(d)
        for p in _anak(area, "polygon"):
            pts = parse_poligon(p.text)
            if len(pts) >= 3:
                poligon.append(pts)
    return {
        "id": _teks(root, "identifier"), "dikirim": _teks(root, "sent"),
        "event": _teks(info, "event"), "judul": _teks(info, "headline"), "deskripsi": _teks(info, "description"),
        "mulai": _teks(info, "effective") or _teks(info, "onset"), "berakhir": _teks(info, "expires"),
        "severity": _teks(info, "severity"), "urgency": _teks(info, "urgency"), "certainty": _teks(info, "certainty"),
        "infografik": _teks(info, "web"), "pengirim": _teks(info, "senderName"), "wilayah": "; ".join(wilayah)[:600],
        "poligon": poligon,
    }


def waktu(s):
    try:
        dt = datetime.fromisoformat(str(s).strip())
        return dt if dt.tzinfo else dt.replace(tzinfo=WIB)
    except Exception:  # noqa: BLE001
        return None


def _km_geom(g):
    """Panjang geodesik (km) geometri garis hasil irisan."""
    if g.is_empty:
        return 0.0
    if g.geom_type == "LineString":
        c = list(g.coords)
        return sum(haversine(a[1], a[0], b[1], b[0]) for a, b in zip(c[:-1], c[1:]))
    if hasattr(g, "geoms"):
        return sum(_km_geom(x) for x in g.geoms)
    return 0.0


class Aset:
    """Indeks spasial aset untuk menumpangkan poligon peringatan."""

    def __init__(self, garis, fas, radius_km):
        from shapely.geometry import LineString, MultiLineString, MultiPoint, Point
        from shapely.ops import unary_union
        self.garis = [{**g, "geom": LineString(g["coords"])} for g in garis]
        self.fas = [{**f, "geom": Point(f["lon"], f["lat"])} for f in fas]
        bagian = []
        if self.garis:
            bagian.append(MultiLineString([g["coords"] for g in garis]))
        if self.fas:
            bagian.append(MultiPoint([(f["lon"], f["lat"]) for f in fas]))
        self.zona = unary_union(bagian).buffer(radius_km / 111.0).simplify(0.01) if bagian else None

    def tumpang(self, poligon):
        """poligon: [[(lon,lat)…]…] → (per_area, geometri gabungan yang menyentuh zona aset | None)."""
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        from shapely.prepared import prep
        geoms = []
        for pts in poligon:
            try:
                p = Polygon(pts)
                if not p.is_valid:
                    p = p.buffer(0)
                if not p.is_empty and (self.zona is None or p.intersects(self.zona)):
                    geoms.append(p)
            except Exception:  # noqa: BLE001
                continue
        if not geoms:
            return {}, None
        u = unary_union(geoms)
        pu, per = prep(u), {}
        for g in self.garis:
            if not pu.intersects(g["geom"]):
                continue
            km = _km_geom(g["geom"].intersection(u))
            if km <= 0.01:
                continue
            a = per.setdefault(g["area"], {"pipa_km": 0.0, "pipa": {}, "fasilitas": []})
            a["pipa_km"] += km
            a["pipa"][g["aset"]] = a["pipa"].get(g["aset"], 0.0) + km
        for f in self.fas:
            if pu.contains(f["geom"]):
                per.setdefault(f["area"], {"pipa_km": 0.0, "pipa": {}, "fasilitas": []})["fasilitas"].append(f["aset"])
        for a in per.values():
            a["pipa_km"] = round(a["pipa_km"], 1)
            a["pipa"] = [{"aset": k, "km": round(v, 1)} for k, v in sorted(a["pipa"].items(), key=lambda x: -x[1])[:8]]
            a["n_fasilitas"] = len(a["fasilitas"])
            a["fasilitas"] = sorted(set(a["fasilitas"]))[:15]
        return per, u


def geojson_poligon(u, props, tol):
    from shapely.geometry import mapping
    g = u.simplify(tol) if tol else u

    def bulat(c):
        return [bulat(x) for x in c] if isinstance(c[0], (list, tuple)) else [round(c[0], 4), round(c[1], 4)]
    m = mapping(g)
    return {"type": "Feature", "properties": props, "geometry": {"type": m["type"], "coordinates": bulat(m["coordinates"])}}


def proses(daftar_cap, aset, now=None):
    """daftar_cap: [(item_rss, cap_dict)] → (aktif[], fitur_geojson[]). Peringatan kedaluwarsa & jauh dari aset dibuang."""
    now = now or sekarang()
    aktif, fitur = [], []
    for item, cap in daftar_cap:
        if not cap or not cap["poligon"]:
            continue
        akhir = waktu(cap["berakhir"])
        if akhir and akhir < now:
            continue
        per, u = aset.tumpang(cap["poligon"])
        if u is None:
            continue
        prov = item["judul"].rsplit(" di ", 1)[-1] if " di " in item["judul"] else item["judul"]
        aktif.append({
            "kode": item["kode"], "provinsi": prov, "judul": cap["judul"] or item["judul"], "event": cap["event"],
            "deskripsi": (cap["deskripsi"] or item["ringkas"])[:900], "mulai": cap["mulai"], "berakhir": cap["berakhir"],
            "severity": cap["severity"], "urgency": cap["urgency"], "certainty": cap["certainty"],
            "infografik": cap["infografik"], "tautan": item["link"], "kena": bool(per), "per_area": per,
        })
        fitur.append(geojson_poligon(u, {"kode": item["kode"], "kena": bool(per), "berakhir": cap["berakhir"]}, CFG["simplify_derajat"]))
    aktif.sort(key=lambda x: (not x["kena"], x["provinsi"]))
    return aktif, fitur


def ringkas_area(aktif, areas):
    out = {}
    for a in areas:
        kena = [x for x in aktif if a in x["per_area"]]
        if not kena:
            continue
        fas = sorted({f for x in kena for f in x["per_area"][a]["fasilitas"]})
        out[a] = {"jumlah": len(kena), "pipa_km": round(max(x["per_area"][a]["pipa_km"] for x in kena), 1), "n_fasilitas": len(fas),
                  "berakhir": max((x["berakhir"] for x in kena if x["berakhir"]), default=None), "kode": [x["kode"] for x in kena]}
    return out


def main():
    t0 = time.time()
    lama = baca_json(OUT / "peringatan.json", {}) or {}
    areas, garis, fas = muat_aset()
    aset = Aset(garis, fas, CFG["radius_tampil_km"])
    try:
        items = parse_rss(ambil(CFG["rss_url"], timeout=(15, 40)).text)[:CFG["maks_peringatan"]]
        log(f"RSS BMKG: {len(items)} peringatan dini aktif nasional")
        daftar, gagal = [], 0
        for it in items:
            try:
                daftar.append((it, parse_cap(ambil(it["link"], timeout=(15, 40), coba=2).text)))
            except Exception as e:  # noqa: BLE001
                gagal += 1
                log(f"  CAP {it['kode']} gagal: {str(e)[:120]}")
            time.sleep(0.3)
        if items and gagal == len(items):
            raise RuntimeError("semua berkas CAP gagal diambil")
        aktif, fitur = proses(daftar, aset)
        inti = {"aktif": aktif, "per_area": ringkas_area(aktif, areas), "nasional": len(items), "cap_gagal": gagal}
        log(f"Dekat aset: {len(aktif)} peringatan, {sum(1 for x in aktif if x['kena'])} mengenai aset")
        sama = lama.get("ok") and all(lama.get(k) == v for k, v in inti.items())
        um = umur_menit(lama.get("diambil"))
        if sama and um is not None and um < CFG["heartbeat_menit"]:
            log(f"Tidak ada perubahan (pengecekan terakhir {um:.0f} menit lalu) — berkas tidak ditulis ulang.")
            return
        tulis_json(OUT / "peringatan.json", {
            "sumber": "BMKG — Peringatan Dini Cuaca (nowcasting)", "jenis": "Peringatan resmi BMKG, berlaku ±1–3 jam",
            "url": "https://www.bmkg.go.id/cuaca/peringatan-dini-cuaca", "radius_tampil_km": CFG["radius_tampil_km"],
            "diambil": iso(), "ok": True, "pesan": None, **inti, "durasi_detik": round(time.time() - t0)})
        tulis_json(OUT / "poligon.geojson", {"type": "FeatureCollection", "features": fitur})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        um = umur_menit(lama.get("diperiksa"))
        if lama.get("ok") is False and um is not None and um < CFG["heartbeat_menit"]:
            return  # sudah ditandai gagal belum lama — jangan commit berulang
        lama.update({"ok": False, "pesan": str(e)[:200], "diperiksa": iso()})
        lama.setdefault("aktif", [])
        lama.setdefault("per_area", {})
        lama.setdefault("sumber", "BMKG — Peringatan Dini Cuaca (nowcasting)")
        tulis_json(OUT / "peringatan.json", lama)


if __name__ == "__main__":
    main()
