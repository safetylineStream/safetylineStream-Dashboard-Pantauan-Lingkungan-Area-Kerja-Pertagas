"""
gerakan_tanah.py — Data gerakan tanah PVMBG (Badan Geologi, Kementerian ESDM) dibandingkan dengan aset Pertagas.

Tiga sumber, masing-masing opsional & berdiri sendiri:
  1. Zona Kerentanan Gerakan Tanah (ZKGT)     — layanan GIS ESDM One Map (statis, diperbarui tiap `refresh_hari`)
  2. Prakiraan Potensi Gerakan Tanah Bulanan  — layanan GIS ESDM One Map (berganti tiap bulan)
  3. Laporan kejadian (tanggapan) gerakan tanah — API MAGMA Indonesia, butuh APP_ID stakeholder dari PVMBG

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
from datetime import datetime, timedelta
from pathlib import Path

from pyproj import Geod
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import transform, unary_union

GEOD = Geod(ellps="WGS84")
LEVELS = ["tinggi", "menengah", "rendah", "sangat_rendah"]
LABEL = {"tinggi": "Tinggi", "menengah": "Menengah", "rendah": "Rendah", "sangat_rendah": "Sangat rendah"}


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
        js = self.get(url, params=p, timeout=60).json()
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
                        if lv in ("tinggi", "menengah"):
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


# ------------------------------------------------------------------ kejadian MAGMA
def fetch_magma(get, cfg, log, now):
    app_id, secret = os.environ.get("MAGMA_APP_ID", ""), os.environ.get("MAGMA_SECRET_KEY", "")
    if not (app_id and secret):
        raise RuntimeError("MAGMA_APP_ID/MAGMA_SECRET_KEY belum diisi (ajukan APP_ID stakeholder ke PVMBG)")
    import requests
    base = cfg["magma_api"].rstrip("/")
    tok = requests.post(f"{base}/login/stakeholder", data={"app_id": app_id, "secret_key": secret}, timeout=60).json()
    if not tok.get("token"):
        raise RuntimeError(f"login MAGMA gagal: {str(tok)[:150]}")
    h = {"Authorization": f"Bearer {tok['token']}"}
    start = (now - timedelta(days=cfg.get("kejadian_hari", 60))).strftime("%Y-%m-%d")
    end = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    out = []
    for page in range(1, 11):
        js = get(f"{base}/v1/home/gerakan-tanah/filter", params={"start_date": start, "end_date": end, "page": page},
                 headers=h, timeout=60).json()
        data = js.get("data") or []
        for d in data:
            try:
                out.append({"id": d.get("id"), "judul": d.get("judul"), "waktu": d.get("local_datetime"),
                            "zona": d.get("time_zone"), "lat": float(d["latitude"]), "lon": float(d["longitude"]),
                            "provinsi": d.get("provinsi"), "kab": d.get("kabupaten_kota"), "kec": d.get("kecamatan"),
                            "desa": d.get("kelurahan"), "kerentanan": d.get("kerentanan"),
                            "rekomendasi": re.sub(r"<[^>]+>", " ", d.get("rekomendasi") or "")[:600],
                            "url": (d.get("share") or {}).get("url")})
            except (KeyError, TypeError, ValueError):
                continue
        last = (js.get("meta") or {}).get("last_page") or (1 if len(data) < 15 else page + 1)
        if page >= last or not data:
            break
    log(f"  MAGMA: {len(out)} laporan kejadian gerakan tanah")
    return out


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
    out = {"sumber": "PVMBG – Badan Geologi (ESDM One Map / MAGMA Indonesia)", "koridor_km": gcfg.get("koridor_km", 2)}

    # Layer yang belum punya data diambil lebih dulu, supaya tidak selalu kalah oleh layer lain
    def prioritas(lay):
        m = pv.get(lay["id"]) or {}
        ada = (outdir / f"{lay['id']}.geojson").exists() and m.get("tersedia") and not m.get("demo")
        return (1 if ada else 0, m.get("diambil") or "")
    for lay in sorted(gcfg["layanan"], key=prioritas):
        lid = lay["id"]
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
            last_err = None
            for url in lay["urls"]:
                try:
                    log(f"PVMBG {lay['nama']}: {url}")
                    layers = arc.polygon_layers(url)
                    lyr = layers[0]
                    rfield, rlabels = renderer_labels(lyr)
                    raw = {}
                    for env in area_envelopes(assets_ll):
                        for f in arc.query_envelope(url, lyr, env):
                            oid = next((f["attributes"][k] for k in f["attributes"] if k.lower() in ("objectid", "fid", "objectid_1")), id(f))
                            raw[oid] = f
                    zones = []
                    for f in raw.values():
                        g = esri_to_shape(f.get("geometry") or {})
                        lv = pick_level(f["attributes"], rfield, rlabels)
                        if g is not None and lv:
                            zones.append((g, lv, f["attributes"]))
                    src, periode = f"ESDM One Map · {lyr.get('name')}", lyr.get("name")
                    break
                except TimeoutError as e:
                    last_err = e
                    log("   ", e)
                    break
                except Exception as e:  # noqa
                    last_err = e
                    log("   gagal:", str(e)[:200])
            if zones is None:
                raise last_err or RuntimeError("layanan tidak tersedia")
        except Exception as e:  # noqa
            status[f"pvmbg_{lid}"] = {"ok": False, "pesan": str(e)[:200]}
            for ext in (".geojson", ".json", ".zip"):
                p = manual / f"{lid}{ext}"
                if p.exists():
                    log(f"  memakai file manual {p.name}")
                    zones = [(g, norm_level(pick_level(pr, None, {})), pr) for g, pr in read_manual(p)]
                    zones = [z for z in zones if z[1]]
                    src, periode = f"File manual {p.name}", datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
                    status[f"pvmbg_{lid}"] = {"ok": True, "pesan": f"file manual {p.name}"}
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
                    "diambil": NOW.isoformat(timespec="minutes"), "jumlah_poligon": len(clipped), "per_area": per_area,
                    "wilayah": sorted(areas)}
        status.setdefault(f"pvmbg_{lid}", {"ok": True, "jumlah": len(clipped)})

    # Laporan kejadian (MAGMA)
    try:
        ev = fetch_magma(get, gcfg, log, NOW)
        rad = gcfg["kejadian_radius_km"]
        for e in ev:
            d, p = A.nearest(e["lat"], e["lon"])
            e["jarak_km"], e["area"], e["aset"] = round(d, 1), p["area"], p["name"]
            e["status"] = "kritis" if d <= rad["kritis"] else "waspada" if d <= rad["waspada"] else "pantau" if d <= rad["pantau"] else "luar"
            e["per_area"] = {}
            for code in A.by_area:
                da, pa = A.nearest(e["lat"], e["lon"], code)
                e["per_area"][code] = {"jarak_km": round(da, 1), "aset": pa["name"]}
        ev.sort(key=lambda e: e["jarak_km"])
        out["kejadian"] = {"tersedia": True, "hari": gcfg.get("kejadian_hari", 60), "radius_km": rad, "data": ev}
        status["magma_gertan"] = {"ok": True, "jumlah": len(ev)}
    except Exception as e:  # noqa
        msg = str(e)[:200]
        out["kejadian"] = {"tersedia": False, "pesan": msg, "radius_km": gcfg["kejadian_radius_km"],
                           "hari": gcfg.get("kejadian_hari", 60),
                           "data": [] if (pv.get("kejadian") or {}).get("demo") else (pv.get("kejadian") or {}).get("data", [])}
        status["magma_gertan"] = {"ok": False, "pesan": msg}

    return out, status
