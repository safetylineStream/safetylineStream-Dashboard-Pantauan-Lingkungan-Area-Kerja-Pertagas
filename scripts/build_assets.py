"""
build_assets.py — Mengubah file aset (KMZ / KML / SHP dalam .zip) menjadi
data/assets.geojson yang ringan untuk dashboard.

Jalankan ulang setiap kali file di assets_raw/ atau config/assets_sources.json berubah:
    python scripts/build_assets.py
"""
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from lxml import etree
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import linemerge

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "assets_raw"
CFG = json.loads((ROOT / "config" / "assets_sources.json").read_text(encoding="utf-8"))
OUT = ROOT / "data" / "assets.geojson"
NS = "{http://www.opengis.net/kml/2.2}"
TOL = CFG.get("simplify_tolerance_deg", 0.0003)


def clean_name(s):
    s = (s or "").strip()
    return re.sub(r"\s+", " ", s)


def match(path, patterns):
    if not patterns:
        return False
    if "*" in patterns:
        return True
    pats = {p.strip().lower() for p in patterns}
    return any(f.strip().lower() in pats for f in path)


def parse_coords(text):
    pts = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            try:
                pts.append((float(parts[0]), float(parts[1])))
            except ValueError:
                pass
    return pts


def read_kml_bytes(path: Path) -> bytes:
    if path.suffix.lower() == ".kml":
        return path.read_bytes()
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".kml"))
        return z.read(name)


def walk_kml(el, folder_path, out):
    for ch in el:
        tag = ch.tag
        if tag in (NS + "Folder", NS + "Document"):
            walk_kml(ch, folder_path + [clean_name(ch.findtext(NS + "name"))], out)
        elif tag == NS + "Placemark":
            name = clean_name(ch.findtext(NS + "name"))
            for g in ch.iter(NS + "LineString"):
                c = parse_coords(g.findtext(NS + "coordinates"))
                if len(c) >= 2:
                    out.append(("line", name, folder_path, LineString(c)))
            for g in ch.iter(NS + "Point"):
                c = parse_coords(g.findtext(NS + "coordinates"))
                if c:
                    out.append(("point", name, folder_path, Point(c[0])))


def features_from_kml(path):
    root = etree.fromstring(read_kml_bytes(path), etree.XMLParser(huge_tree=True, recover=True))
    out = []
    walk_kml(root, [], out)
    return out


def features_from_shp_zip(path, name_field=None):
    import shapefile  # pyshp
    from pyproj import CRS, Transformer
    from shapely.ops import transform

    out = []
    with zipfile.ZipFile(path) as z:
        shps = [n for n in z.namelist() if n.lower().endswith(".shp")]
        for shp in shps:
            base = shp[:-4]
            def get(ext):
                for n in z.namelist():
                    if n.lower() == (base + ext).lower():
                        return io.BytesIO(z.read(n))
                return None
            r = shapefile.Reader(shp=get(".shp"), dbf=get(".dbf"), shx=get(".shx"))
            prj = get(".prj")
            tf = None
            if prj:
                crs = CRS.from_wkt(prj.read().decode("utf-8", "ignore"))
                if crs.to_epsg() != 4326:
                    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
                    tf = lambda g: transform(t.transform, g)
            fields = [f[0] for f in r.fields[1:]]
            layer = Path(shp).stem
            for sr in r.iterShapeRecords():
                g = shape(sr.shape.__geo_interface__)
                if tf:
                    g = tf(g)
                rec = dict(zip(fields, sr.record))
                nm = str(rec.get(name_field) or rec.get("NAME") or rec.get("Name") or rec.get("NAMA") or layer)
                geoms = getattr(g, "geoms", [g])
                for gg in geoms:
                    if gg.geom_type == "LineString":
                        out.append(("line", nm, [layer], gg))
                    elif gg.geom_type == "Point":
                        out.append(("point", nm, [layer], gg))
                    elif gg.geom_type == "Polygon":
                        out.append(("line", nm, [layer], LineString(gg.exterior.coords)))
    return out


def main():
    feats = []
    seen_pts = set()
    summary = {}
    for spec in CFG["files"]:
        p = RAW / spec["file"]
        if not p.exists():
            print(f"[LEWATI] {spec['file']} tidak ditemukan di assets_raw/")
            continue
        area = spec["area"]
        if p.suffix.lower() == ".zip":
            raw = features_from_shp_zip(p, spec.get("name_field"))
        else:
            raw = features_from_kml(p)
        lines = [r for r in raw if r[0] == "line" and match(r[2], spec.get("lines"))]
        points = [r for r in raw if r[0] == "point" and match(r[2], spec.get("points"))]

        generic = {"", "viewuser", "untitled path", "path", "line"}
        for i, (_, name, fp, g) in enumerate(lines, 1):
            if (name or "").strip().lower() in generic:
                name = f"Jalur Pipa {area} #{i}"
            elif re.fullmatch(r"[\d.,\s]+", name or "") and fp:
                name = fp[-2] if len(fp) >= 2 and fp[-2].lower().startswith("seg") else fp[-1]
            s = g.simplify(TOL, preserve_topology=False)
            if s.is_empty or s.length == 0:
                continue
            feats.append({
                "type": "Feature",
                "properties": {"kind": "pipa", "area": area, "name": name or fp[-1], "grup": fp[-1] if fp else ""},
                "geometry": json.loads(json.dumps(mapping(s))),
            })
        for _, name, fp, g in points:
            key = (area, name.lower(), round(g.x, 4), round(g.y, 4))
            if key in seen_pts:
                continue
            seen_pts.add(key)
            feats.append({
                "type": "Feature",
                "properties": {"kind": "fasilitas", "area": area, "name": name or "(tanpa nama)", "grup": fp[-1] if fp else ""},
                "geometry": {"type": "Point", "coordinates": [round(g.x, 6), round(g.y, 6)]},
            })
        s = summary.setdefault(area, {"pipa": 0, "fasilitas": 0})
        s["pipa"] += len(lines)
        s["fasilitas"] += len(points)
        print(f"[OK] {spec['file']:<40} area={area:<5} pipa={len(lines):>4} fasilitas={len(points):>4}")

    # bulatkan koordinat agar file kecil
    def rnd(c):
        return [rnd(x) for x in c] if isinstance(c[0], (list, tuple)) else [round(c[0], 5), round(c[1], 5)]
    for f in feats:
        f["geometry"]["coordinates"] = rnd(f["geometry"]["coordinates"])

    fc = {"type": "FeatureCollection", "areas": CFG["areas"], "features": feats}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fc, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"\nTersimpan: {OUT.relative_to(ROOT)} ({OUT.stat().st_size/1024:.0f} KB), {len(feats)} fitur")
    print(json.dumps(summary, indent=1))
    if not feats:
        sys.exit("Tidak ada aset yang terbaca — periksa config/assets_sources.json")


if __name__ == "__main__":
    main()
