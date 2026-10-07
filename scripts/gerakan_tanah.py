"""
gerakan_tanah.py — Data gerakan tanah PVMBG (Badan Geologi, Kementerian ESDM) dibandingkan dengan aset Pertagas.

Dua sumber, masing-masing opsional & berdiri sendiri:
  1. Zona Kerentanan Gerakan Tanah (ZKGT)     — layanan GIS ESDM One Map (statis, diperbarui tiap `refresh_hari`)
  2. Prakiraan Potensi Gerakan Tanah Bulanan  — layanan GIS ESDM One Map (berganti tiap bulan)

Cadangan manual: taruh file GeoJSON / SHP(.zip) di folder gerakan_tanah_raw/ dengan nama
  zkgt.geojson | zkgt.zip          → dipakai bila layanan ZKGT gagal
  prakiraan.geojson | prakiraan.zip → dipakai bila layanan prakiraan gagal

Hasil:
  data/gerakan_tanah/zkgt.geojson       — poligon zona di sekitar aset (sudah dipotong)
  data/gerakan_tanah/prakiraan.geojson  — poligon prakiraan bulan ini di sekitar aset
  ringkasan per wilayah dikembalikan ke fetch_all.py → data/latest.json["gerakan_tanah"]
"""
import io
import json
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

from pyproj import Geod
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import transform, unary_union

GEOD = Geod(ellps="WGS84")
# "bandang" = zona "Berpotensi Banjir Bandang/Aliran Bahan Rombakan" pada prakiraan PVMBG (Portal MBG)
LEVELS = ["tinggi", "bandang", "menengah", "rendah", "sangat_rendah"]
LABEL = {"tinggi": "Tinggi", "bandang": "Berpotensi banjir bandang", "menengah": "Menengah", "rendah": "Rendah",
         "sangat_rendah": "Sangat rendah"}


BULAN = {"januari": 1, "februari": 2, "maret": 3, "april": 4, "mei": 5, "juni": 6, "juli": 7, "agustus": 8,
         "september": 9, "oktober": 10, "november": 11, "desember": 12}


def periode_bulan(teks):
    """'Prakiraan Gerakan Tanah Bulan September 2026' → '2026-09' (None bila tidak terbaca)."""
    m = re.search(r"(" + "|".join(BULAN) + r")\s+(\d{4})", str(teks or ""), re.I)
    return f"{m.group(2)}-{BULAN[m.group(1).lower()]:02d}" if m else None


def norm_level(v):
    s = str(v or "").lower()
    if "sangat rendah" in s or "sangat_rendah" in s:
        return "sangat_rendah"
    if "tinggi" in s:
        return "tinggi"
    if "menengah" in s or "sedang" in s:
        return "menengah"
    if "rendah" in s:
        return "rendah"
    return None


# ------------------------------------------------------------------ ArcGIS REST
class ArcGIS:
    def __init__(self, get, cfg, log, sisa=None):
        self.get, self.cfg, self.log, self.sisa = get, cfg, log, sisa
        self.token = None
        user, pw = os.environ.get("ONEMAP_USERNAME", ""), os.environ.get("ONEMAP_PASSWORD", "")
        if user and pw:
            try:
                import requests
                r = requests.post(cfg["token_url"], data={"username": user, "password": pw, "client": "requestip",
                                                          "expiration": 60, "f": "json"}, timeout=60)
                self.token = r.json().get("token")
                self.log("  token ESDM One Map:", "OK" if self.token else r.text[:120])
            except Exception as e:  # noqa
                self.log("  token ESDM One Map gagal:", e)

    def q(self, url, params):
        p = {"f": "json", **params}
        if self.token:
            p["token"] = self.token
        if self.sisa and self.sisa() < 40:
            raise TimeoutError("anggaran waktu habis — data gerakan tanah dilewati untuk run ini")
        js = self.get(url, params=p, timeout=120).json()
        if isinstance(js, dict) and js.get("error"):
            raise RuntimeError(f"ArcGIS error {js['error'].get('code')}: {js['error'].get('message')}")
        return js

    def polygon_layers(self, service):
        info = self.q(service, {})
        out = []
        for lyr in info.get("layers", []):
            li = self.q(f"{service}/{lyr['id']}", {})
            if li.get("geometryType") == "esriGeometryPolygon":
                out.append(li | {"id": lyr["id"]})
        if not out:
            raise RuntimeError("tidak ada layer poligon di layanan")
        return out

    def query_envelope(self, service, layer, env):
        """Ambil semua fitur yang memotong envelope (xmin,ymin,xmax,ymax) — aman untuk batas maxRecordCount."""
        url = f"{service}/{layer['id']}/query"
        base = {"geometry": ",".join(f"{v:.5f}" for v in env), "geometryType": "esriGeometryEnvelope",
                "inSR": 4326, "spatialRel": "esriSpatialRelIntersects", "where": "1=1"}
        ids = self.q(url, base | {"returnIdsOnly": "true"}).get("objectIds") or []
        feats = []
        for i in range(0, len(ids), 150):
            chunk = ids[i:i + 150]
            js = self.q(url, {"objectIds": ",".join(map(str, chunk)), "outFields": "*", "returnGeometry": "true",
                              "outSR": 4326, "maxAllowableOffset": 0.0003})
            feats += js.get("features", [])
        return feats


def esri_to_shape(g):
    rings = g.get("rings") or []
    outers, holes = [], []
    for r in rings:
        if len(r) < 4:
            continue
        lr = Polygon(r)
        (holes if lr.exterior.is_ccw else outers).append(r)
    polys = [Polygon(o) for o in outers]
    polys = [p.buffer(0) for p in polys]
    for h in holes:
        hp = Polygon(h).buffer(0)
        for i, p in enumerate(polys):
            if p.contains(hp.representative_point()):
                polys[i] = p.difference(hp)
                break
    geom = unary_union(polys) if polys else None
    return geom


def renderer_labels(layer):
    """Peta nilai → label dari simbologi layer (mis. 1 → 'Tinggi')."""
    out = {}
    r = (layer.get("drawingInfo") or {}).get("renderer") or {}
    field = r.get("field1")
    for u in r.get("uniqueValueInfos") or []:
        out[str(u.get("value"))] = u.get("label")
    return field, out


def pick_level(attrs, rfield, rlabels):
    if rfield and str(attrs.get(rfield)) in rlabels:
        lv = norm_level(rlabels[str(attrs.get(rfield))])
        if lv:
            return lv
    for k, v in attrs.items():
        if isinstance(v, str):
            lv = norm_level(v)
            if lv and re.search(r"potensi|zona|kelas|kerentanan|ket|gt|rawan|tingkat", k, re.I):
                return lv
    for v in attrs.values():
        if isinstance(v, str) and norm_level(v):
            return norm_level(v)
    return None


# ------------------------------------------------------------------ file manual
def read_manual(path):
    feats = []
    if path.suffix.lower() in (".geojson", ".json"):
        fc = json.loads(path.read_text(encoding="utf-8"))
        for f in fc.get("features", []):
            feats.append((shape(f["geometry"]), f.get("properties") or {}))
    elif path.suffix.lower() == ".zip":
        import shapefile
        from pyproj import CRS, Transformer
        with zipfile.ZipFile(path) as z:
            shp = next(n for n in z.namelist() if n.lower().endswith(".shp"))
            base = shp[:-4]
            gb = lambda e: io.BytesIO(z.read(next(n for n in z.namelist() if n.lower() == (base + e).lower())))
            r = shapefile.Reader(shp=gb(".shp"), dbf=gb(".dbf"), shx=gb(".shx"))
            tf = None
            try:
                crs = CRS.from_wkt(gb(".prj").read().decode("utf-8", "ignore"))
                if crs.to_epsg() != 4326:
                    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform
                    tf = lambda g: transform(t, g)
            except StopIteration:
                pass
            fields = [f[0] for f in r.fields[1:]]
            for sr in r.iterShapeRecords():
                g = shape(sr.shape.__geo_interface__)
                feats.append((tf(g) if tf else g, dict(zip(fields, sr.record))))
    return feats


# ------------------------------------------------------------------ analisis
def km_len(geom):
    return GEOD.geometry_length(geom) / 1000.0 if geom and not geom.is_empty else 0.0


def analyse(zones, assets_ll, areas):
    """zones: list of (geom_ll, level). Kembalikan ringkasan per wilayah."""
    by_level = {}
    for g, lv in zones:
        by_level.setdefault(lv, []).append(g)
    union = {lv: unary_union(gs).buffer(0) for lv, gs in by_level.items()}
    res = {}
    for code in areas:
        lines = [(g, p) for g, p in assets_ll if p["area"] == code and p["kind"] == "pipa"]
        pts = [(g, p) for g, p in assets_ll if p["area"] == code and p["kind"] == "fasilitas"]
        total = sum(km_len(g) for g, _ in lines)
        km = {lv: 0.0 for lv in LEVELS}
        segs = []
        for g, p in lines:
            for lv in LEVELS:
                if lv in union:
                    inter = g.intersection(union[lv])
                    L = km_len(inter)
                    if L > 0.01:
                        km[lv] += L
                        if lv in ("tinggi", "bandang", "menengah"):
                            segs.append({"aset": p["name"], "level": lv, "km": round(L, 2)})
        fas = []
        for g, p in pts:
            for lv in LEVELS:
                if lv in union and union[lv].contains(g):
                    fas.append({"aset": p["name"], "level": lv, "lat": round(g.y, 5), "lon": round(g.x, 5)})
                    break
        worst = next((lv for lv in LEVELS if km[lv] > 0.01 or any(f["level"] == lv for f in fas)), None)
        segs.sort(key=lambda s: (LEVELS.index(s["level"]), -s["km"]))
        fas.sort(key=lambda f: LEVELS.index(f["level"]))
        res[code] = {"total_pipa_km": round(total, 1), "pipa_km": {k: round(v, 2) for k, v in km.items()},
                     "fasilitas": fas, "segmen": segs[:30], "terburuk": worst}
    return res


def area_envelopes(assets_ll, pad=0.05, tile=1.5):
    """Grid envelope ±1,5° yang hanya menutupi daerah sekitar aset (menghindari unduhan besar)."""
    envs = set()
    for g, _ in assets_ll:
        x0, y0, x1, y1 = g.bounds
        x0 -= pad; y0 -= pad; x1 += pad; y1 += pad
        ix0, iy0 = int((x0 + 180) // tile), int((y0 + 90) // tile)
        ix1, iy1 = int((x1 + 180) // tile), int((y1 + 90) // tile)
        for i in range(ix0, ix1 + 1):
            for j in range(iy0, iy1 + 1):
                envs.add((i, j))
    out = []
    for i, j in sorted(envs):
        out.append((i * tile - 180, j * tile - 90, (i + 1) * tile - 180, (j + 1) * tile - 90))
    return out


def clip_zone(zones, corridor):
    out = []
    for g, lv, props in zones:
        c = g.intersection(corridor)
        if not c.is_empty:
            out.append((c.simplify(0.0002, preserve_topology=True), lv, props))
    return out


def to_fc(zones, periode=None):
    feats = []
    for g, lv, props in zones:
        keep = {k: v for k, v in props.items() if isinstance(v, (str, int, float)) and not re.match(r"(?i)^(objectid|fid|shape)", k)}
        feats.append({"type": "Feature", "properties": {"level": lv, "label": LABEL.get(lv, lv), **{k: keep[k] for k in list(keep)[:8]}},
                      "geometry": mapping(g)})
    return {"type": "FeatureCollection", "periode": periode, "features": feats}


# ------------------------------------------------------------------ unduhan bertahap (bisa dilanjutkan)
class Sebagian(RuntimeError):
    """Unduhan belum lengkap; bagian yang sudah selesai tetap tersimpan."""
    def __init__(self, selesai, total, alasan):
        super().__init__(f"proses {selesai}/{total} bagian — dilanjutkan pada run berikutnya ({alasan})")
        self.selesai, self.total = selesai, total


def _tile_key(env):
    return f"{env[0]:.1f}_{env[1]:.1f}"


def _tile_hash(assets_ll, env):
    from shapely.geometry import box
    import hashlib
    b = box(*env)
    h = hashlib.md5()
    for g, p in assets_ll:
        if g.intersects(b):
            h.update(f"{p['area']}|{p['name']}|{[round(v, 3) for v in g.bounds]}".encode())
    return h.hexdigest()[:12]


def ambil_bertahap(arc, lay, assets_ll, corridor, outdir, NOW, log):
    """Unduh layer per kotak (tile). Tiap tile yang selesai langsung disimpan ke
    data/gerakan_tanah/cache_<id>/, sehingga bila waktu habis, run berikutnya melanjutkan
    tile yang belum ada. Tile diunduh ulang bila kedaluwarsa (refresh_hari), layer berganti
    (mis. prakiraan bulan baru), atau aset di dalam tile berubah."""
    from shapely.geometry import box
    lid = lay["id"]
    cdir = outdir / f"cache_{lid}"
    cdir.mkdir(parents=True, exist_ok=True)
    last_err, url, lyr = None, None, None
    for u in lay["urls"]:
        try:
            log(f"PVMBG {lay['nama']}: {u}")
            lyr = arc.polygon_layers(u)[0]
            url = u
            break
        except TimeoutError:
            raise
        except Exception as e:  # noqa
            last_err = e
            log("   gagal:", str(e)[:200])
    if url is None:
        raise last_err or RuntimeError("layanan tidak tersedia")
    rfield, rlabels = renderer_labels(lyr)
    envs = area_envelopes(assets_ll)
    # Tile tetap berlaku selama layer (mis. "Prakiraan ... Bulan September 2026") dan aset di dalamnya
    # tidak berubah. Sebelumnya tile kedaluwarsa setelah `refresh_hari` (3 hari) sehingga run berikutnya
    # mengunduh ulang tile lama dan tidak pernah sampai ke tile yang belum ada (macet di 11–12/25).
    maks_hari = lay.get("tile_maks_hari", 35)

    def tile_valid(env):
        f = cdir / f"{_tile_key(env)}.json"
        if not f.exists():
            return False
        try:
            c = json.loads(f.read_text(encoding="utf-8"))
            umur = (NOW - datetime.fromisoformat(c["diambil"])).days
            return c.get("hash") == _tile_hash(assets_ll, env) and c.get("layer") == lyr.get("name") and umur < maks_hari
        except Exception:  # noqa
            return False
    valid = {env: tile_valid(env) for env in envs}
    urutan = sorted(envs, key=lambda e: valid[e])  # tile yang belum ada/kedaluwarsa diunduh lebih dulu
    # progres = tile yang sudah valid (tersimpan) + yang diunduh di run ini
    total, selesai, baru, gagal, alasan = len(envs), sum(valid.values()), 0, 0, ""
    try:
        for env in urutan:
            key, hsh = _tile_key(env), _tile_hash(assets_ll, env)
            f = cdir / f"{key}.json"
            if valid[env]:
                continue
            koridor_tile = corridor.intersection(box(*env))
            feats = []
            try:
                hasil = arc.query_envelope(url, lyr, env)
            except TimeoutError:
                raise
            except Exception as e:  # noqa — server lambat untuk tile ini: lewati, coba lagi di run berikutnya
                gagal += 1
                alasan = str(e)[:120]
                log(f"   tile {key} dilewati: {alasan}")
                continue
            for ft in hasil:
                g = esri_to_shape(ft.get("geometry") or {})
                lv = pick_level(ft["attributes"], rfield, rlabels)
                if g is None or not lv:
                    continue
                g = g.intersection(koridor_tile)
                if g.is_empty:
                    continue
                attrs = ft["attributes"]
                oid = next((attrs[k] for k in attrs if k.lower() in ("objectid", "fid", "objectid_1")), None)
                keep = {k: v for k, v in attrs.items() if isinstance(v, (str, int, float)) and not re.match(r"(?i)^(objectid|fid|shape)", k)}
                feats.append({"oid": oid, "level": lv, "props": dict(list(keep.items())[:8]),
                              "geometry": mapping(g.simplify(0.0002, preserve_topology=True))})
            f.write_text(json.dumps({"hash": hsh, "layer": lyr.get("name"), "diambil": NOW.isoformat(timespec="minutes"),
                                     "features": feats}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            selesai += 1
            baru += 1
            log(f"   tile {selesai}/{total} ({key}) tersimpan: {len(feats)} poligon")
    except TimeoutError as e:
        raise Sebagian(selesai, total, "waktu run habis")
    if gagal:
        raise Sebagian(selesai, total, f"{gagal} bagian belum dijawab server")
    # semua tile lengkap → gabungkan
    zones, seen = [], set()
    for env in envs:
        c = json.loads((cdir / f"{_tile_key(env)}.json").read_text(encoding="utf-8"))
        for ft in c["features"]:
            g = shape(ft["geometry"])
            zones.append((g, ft["level"], ft["props"]))
    log(f"   lengkap: {total} bagian ({baru} baru diunduh), {len(zones)} poligon")
    return zones, f"ESDM One Map · {lyr.get('name')}", lyr.get("name")


# ------------------------------------------------------------------ utama
def run(ctx):
    """ctx: dict dengan get, log, CFG, ROOT, DATA, NOW, assets (Assets dari fetch_all), prev (latest.json lama)."""
    get, log, CFG, ROOT, DATA, NOW = (ctx[k] for k in ("get", "log", "CFG", "ROOT", "DATA", "NOW"))
    A, prev = ctx["assets"], ctx.get("prev") or {}
    gcfg = CFG["gerakan_tanah"]
    outdir = DATA / "gerakan_tanah"
    outdir.mkdir(parents=True, exist_ok=True)
    manual = ROOT / "gerakan_tanah_raw"
    status = {}
    pv = prev.get("gerakan_tanah") or {}

    fc = json.loads((DATA / "assets.geojson").read_text(encoding="utf-8"))
    assets_ll = [(shape(f["geometry"]), f["properties"]) for f in fc["features"]]
    areas = list(fc.get("areas", {}).keys())

    # koridor analisis di sekitar aset (buffer dalam meter di EPSG:3857)
    from pyproj import Transformer
    to_m = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
    to_ll = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
    buf_m = gcfg.get("koridor_km", 2) * 1000
    corridor = transform(to_ll, unary_union([transform(to_m, g).buffer(buf_m, 8) for g, _ in assets_ll]))

    arc = None
    out = {"sumber": "PVMBG – Badan Geologi", "koridor_km": gcfg.get("koridor_km", 2)}
    selesai_portal = set()

    # ---- Sumber utama: Portal MBG PVMBG (unduhan vektor prakiraan bulanan per provinsi) ----
    pcfg = gcfg.get("portalmbg") or {}
    if pcfg.get("aktif"):
        import portalmbg
        try:
            log("PVMBG Portal MBG: prakiraan gerakan tanah bulanan per provinsi ...")
            sig = portalmbg.assets_signature(DATA / "assets.geojson", gcfg.get("koridor_km", 2), pcfg.get("simplify", 0.0002))
            hp = portalmbg.run(ctx, corridor, sig)
            ym = hp["periode_bulan"]
            src = "PVMBG – Portal MBG (vsi.esdm.go.id/portalmbg)"
            prog = f"{hp['selesai']}/{hp['total']}"
            for lid, nama, zones in (("prakiraan", "Prakiraan Potensi Gerakan Tanah Bulanan", hp["prakiraan"]),
                                     ("zkgt", "Zona Kerentanan Gerakan Tanah", hp["zkgt"])):
                if hp["selesai"] == 0:
                    break
                periode = portalmbg.nama_periode(ym) if lid == "prakiraan" else "Zona kerentanan gerakan tanah (atribut Unsur, peta PVMBG)"
                if lid == "prakiraan":
                    # SATU berkas peta untuk dua lapisan: properti level (prakiraan) & level_kerentanan (zona kerentanan)
                    feats = [{"type": "Feature", "geometry": mapping(g),
                              "properties": {"level": lv, "label": LABEL.get(lv, ""), "level_kerentanan": lvk,
                                             **{k: v for k, v in pr.items() if k in ("zona_prakiraan", "zona_kerentanan", "keterangan", "wilayah", "provinsi")}}}
                             for g, lv, lvk, pr in hp["gabungan"]]
                    (outdir / "prakiraan.geojson").write_text(json.dumps({"type": "FeatureCollection", "periode": periode, "features": feats},
                                                                         ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                    (outdir / "zkgt.geojson").unlink(missing_ok=True)  # zona kerentanan dibaca dari berkas yang sama
                per_area = analyse([(g, lv) for g, lv, _ in zones], assets_ll, areas)
                lengkap = hp["selesai"] == hp["total"]
                out[lid] = {"tersedia": True, "nama": nama, "sumber": src + (f" · {periode}" if lid == "prakiraan" else ""),
                            "periode": periode, "periode_bulan": ym if lid == "prakiraan" else None,
                            "periode_campuran": hp["periode_campuran"] if lid == "prakiraan" else None,
                            "diambil": NOW.isoformat(timespec="minutes"), "jumlah_poligon": len(zones), "per_area": per_area,
                            "wilayah": sorted(areas), "provinsi": hp["provinsi"], "stale": not lengkap,
                            "berkas_peta": "prakiraan.geojson", "properti_level": "level" if lid == "prakiraan" else "level_kerentanan",
                            "terbit": max((v.get("terbit") or "" for v in hp["provinsi"].values()), default="") or None}
                status[f"pvmbg_{lid}"] = {"ok": True, "jumlah": len(zones), "sumber": "portalmbg", "progres": prog,
                                          **({"pesan": "sebagian provinsi: " + hp["pesan"]} if hp["pesan"] else {})}
                selesai_portal.add(lid)
            log(f"  Portal MBG selesai: {prog} provinsi, periode {ym}, prakiraan {len(hp['prakiraan'])} poligon")
        except Exception as e:  # noqa — gagal: jatuh ke layanan ESDM One Map di bawah
            import traceback
            traceback.print_exc()
            log("  Portal MBG gagal:", str(e)[:200])
            for lid in ("prakiraan", "zkgt"):
                status[f"pvmbg_{lid}"] = {"ok": False, "pesan": f"Portal MBG: {str(e)[:150]}"}

    # Layer yang belum punya data diambil lebih dulu, supaya tidak selalu kalah oleh layer lain
    def prioritas(lay):
        m = pv.get(lay["id"]) or {}
        ada = (outdir / f"{lay['id']}.geojson").exists() and m.get("tersedia") and not m.get("demo")
        return (1 if ada else 0, m.get("diambil") or "")
    for lay in gcfg["layanan"]:  # cadangan: ESDM One Map (hanya untuk layer yang gagal dari Portal MBG)
        lid = lay["id"]
        if lid in selesai_portal or not pcfg.get("cadangan_onemap", True):
            if lid not in selesai_portal and lid not in out:
                meta = pv.get(lid) or {}
                out[lid] = {**meta, "stale": True} if meta.get("tersedia") else {"tersedia": False, "nama": lay["nama"]}
            continue
        portal_err = status.pop(f"pvmbg_{lid}", None)  # status Portal MBG yang gagal diganti hasil cadangan
        cache = outdir / f"{lid}.geojson"
        meta = pv.get(lid) or {}
        age_ok = False
        if cache.exists() and meta.get("diambil"):
            try:
                age_ok = (NOW - datetime.fromisoformat(meta["diambil"])).days < lay.get("refresh_hari", 1)
            except ValueError:
                pass
        if meta.get("wilayah") != sorted(areas):
            age_ok = False  # ada wilayah kerja baru/berubah → ambil ulang supaya ikut dianalisis
        if age_ok and not meta.get("demo"):
            out[lid] = meta
            status[f"pvmbg_{lid}"] = {"ok": True, "pesan": "cache masih berlaku"}
            continue

        zones, src, periode = None, None, None
        try:
            if arc is None:
                arc = ArcGIS(get, gcfg, log, ctx.get("sisa_waktu"))
            zones, src, periode = ambil_bertahap(arc, lay, assets_ll, corridor, outdir, NOW, log)
        except Exception as e:  # noqa
            # unduhan bertahap belum lengkap / layanan gagal
            raise_msg = str(e)[:200]
            status[f"pvmbg_{lid}"] = {"ok": False, "pesan": ((portal_err or {}).get("pesan", "") + " | One Map: " if portal_err else "") + raise_msg}
            if isinstance(e, Sebagian):
                status[f"pvmbg_{lid}"].update({"sebagian": True, "progres": f"{e.selesai}/{e.total}"})
            zones = None
            for ext in (".geojson", ".json", ".zip"):
                pm = manual / f"{lid}{ext}"
                if pm.exists():
                    log(f"  memakai file manual {pm.name}")
                    zones = [(g, norm_level(pick_level(pr, None, {})), pr) for g, pr in read_manual(pm)]
                    zones = [z for z in zones if z[1]]
                    src, periode = f"File manual {pm.name}", datetime.fromtimestamp(pm.stat().st_mtime).strftime("%Y-%m-%d")
                    status[f"pvmbg_{lid}"] = {"ok": True, "pesan": f"file manual {pm.name}"}
                    break
        if zones is None:
            if meta and not meta.get("demo"):
                out[lid] = {**meta, "stale": True}
            else:  # jangan pertahankan data contoh
                out[lid] = {"tersedia": False, "nama": lay["nama"]}
                cache.unlink(missing_ok=True)
            continue

        clipped = clip_zone(zones, corridor)
        cache.write_text(json.dumps(to_fc(clipped, periode), ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        per_area = analyse([(g, lv) for g, lv, _ in clipped], assets_ll, areas)
        out[lid] = {"tersedia": True, "nama": lay["nama"], "sumber": src, "periode": periode,
                    "periode_bulan": periode_bulan(periode) if lid == "prakiraan" else None,
                    "diambil": NOW.isoformat(timespec="minutes"), "jumlah_poligon": len(clipped), "per_area": per_area,
                    "wilayah": sorted(areas)}
        status.setdefault(f"pvmbg_{lid}", {"ok": True, "jumlah": len(clipped)})

    return out, status
