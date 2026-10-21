"""
pipa_offshore.py — turunkan bagian jalur pipa OEJA yang berada di LAUT (untuk modul kapal AIS).

Garis pantai: Natural Earth 1:10m (land + minor islands, domain publik), diunduh dari GitHub.
Ketelitian garis pantai ±beberapa ratus meter — cukup untuk memisahkan ruas offshore dari darat,
bukan untuk batas hukum. Hasil: data/kapal/pipa_offshore.geojson (dikomit; dijalankan ulang
hanya bila assets.geojson berubah).

  python tools/pipa_offshore.py
"""
import json
import sys
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform, unary_union

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "config" / "monitoring.json").read_text(encoding="utf-8"))["kapal"]
NE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/{}.geojson"


def main():
    w, s, e, n = CFG["bbox"]  # [barat, selatan, timur, utara]
    B = box(w, s, e, n)
    land = []
    for nm in ("ne_10m_land", "ne_10m_minor_islands"):
        r = requests.get(NE.format(nm), timeout=120)
        r.raise_for_status()
        for f in r.json()["features"]:
            g = shape(f["geometry"])
            if g.intersects(B.buffer(0.5)):
                land.append(g.intersection(B.buffer(0.5)))
    L = unary_union(land)
    tm = Transformer.from_crs("EPSG:4326", f"+proj=tmerc +lon_0={(w + e) / 2} +k=1 +ellps=WGS84", always_xy=True).transform
    A = json.loads((ROOT / "data" / "assets.geojson").read_text(encoding="utf-8"))
    out, tot = [], 0.0
    for f in A["features"]:
        p = f["properties"]
        if p.get("area") not in CFG["area"] or p.get("kind") != "pipa":
            continue
        g = shape(f["geometry"]).intersection(B).difference(L)
        if g.is_empty:
            continue
        km = transform(tm, g).length / 1000
        if km < CFG.get("min_ruas_km", 0.2):
            continue
        tot += km
        out.append({"type": "Feature", "properties": {"kind": "pipa", "name": p["name"], "area": p["area"], "grup": p.get("grup"),
                                                      "km": round(km, 2)},
                    "geometry": mapping(g.simplify(0.0002))})
    # koridor larangan jangkar/kewaspadaan (radius kritis) di sekitar ruas offshore, untuk ditampilkan di peta
    from pyproj import Transformer as _T
    inv = _T.from_crs(f"+proj=tmerc +lon_0={(w + e) / 2} +k=1 +ellps=WGS84", "EPSG:4326", always_xy=True).transform
    rk = CFG["radius_km"]["kritis"]
    if out:
        zona = unary_union([transform(tm, shape(f["geometry"])) for f in out]).buffer(rk * 1000, resolution=8)
        zona = transform(inv, zona).simplify(0.0003)
        out.append({"type": "Feature", "properties": {"kind": "zona", "radius_km": rk,
                                                      "name": f"Koridor {int(rk * 1000)} m pipa offshore"},
                    "geometry": mapping(zona)})
    fc = {"type": "FeatureCollection", "sumber_garis_pantai": "Natural Earth 1:10m (land + minor islands)",
          "bbox": CFG["bbox"], "total_km": round(tot, 1), "features": out}
    dst = ROOT / "data" / "kapal" / "pipa_offshore.geojson"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{sum(1 for f in out if f['properties']['kind']=='pipa')} ruas offshore, {tot:.1f} km → {dst.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
