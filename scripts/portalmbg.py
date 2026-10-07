"""
portalmbg.py — Prakiraan Gerakan Tanah Bulanan & zona kerentanan dari Portal MBG PVMBG (vsi.esdm.go.id/portalmbg).

Sumber resmi yang dipakai peta "Prakiraan Gertan" di Portal MBG:
  GET https://vsi.esdm.go.id/portalmbg/api/download-asset-forecast/gertan/forecast/vector/{tahun}/{bulan}/{kode_provinsi}
  → shapefile ZIP per provinsi (WGS84). Bulan tanpa nol di depan (10, bukan 010/09). Bulan yang belum terbit → HTTP 404.

Setiap poligon memuat dua atribut yang dipakai dashboard:
  Zona_Perki → zona PRAKIRAAN gerakan tanah bulan itu (Tinggi / Menengah / Rendah /
               "Berpotensi Banjir Bandang/Aliran Bahan Rombakan"; "Danau" diabaikan)
  Unsur      → zona KERENTANAN gerakan tanah dasar (Tinggi / Menengah / Rendah / Sangat Rendah /
               "Alur Aliran Bahan Rombakan"; Danau/Situ/Waduk diabaikan)

Hasil tiap provinsi (sudah dipotong ke koridor aset) disimpan di data/gerakan_tanah/cache_portal/<kode>.json,
sehingga run berikutnya tidak mengunduh ulang bila bulan & tanggal terbit (Last-Modified) sama, dan
unduhan yang terpotong waktu bisa dilanjutkan.
"""
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime

from shapely import force_2d, make_valid
from shapely.geometry import box, mapping, shape
from shapely.prepared import prep
from shapely.strtree import STRtree

BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus", "September", "Oktober",
         "November", "Desember"]
UA = {"User-Agent": "Mozilla/5.0 (compatible; Pertagas-WASPADA/1.0; +github actions)",
      "Referer": "https://vsi.esdm.go.id/portalmbg/"}


def level_prakiraan(v):
    s = re.sub(r"\s+", " ", str(v or "")).strip().lower()
    if "bandang" in s or "rombakan" in s:
        return "bandang"
    return {"tinggi": "tinggi", "menengah": "menengah", "rendah": "rendah", "sangat rendah": "sangat_rendah"}.get(s)


def level_kerentanan(v):
    return level_prakiraan(v)  # kelas yang sama; Danau/Situ/Waduk → None


def bulan_sebelumnya(y, m):
    return (y - 1, 12) if m == 1 else (y, m - 1)


def _geom2d(g):
    g = force_2d(g)
    if not g.is_valid:
        g = make_valid(g)
    return g


def proses_zip(data, koridor_tree, koridor_parts, simplify=0.0002):
    """Baca shapefile ZIP, kembalikan (fitur_prakiraan, fitur_kerentanan) yang sudah dipotong ke koridor aset."""
    import shapefile
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    shp = next(n for n in names if n.lower().endswith(".shp"))
    base = shp[:-4]

    def part(ext):
        return io.BytesIO(z.read(next(n for n in names if n.lower() == (base + ext).lower())))
    r = shapefile.Reader(shp=part(".shp"), dbf=part(".dbf"), shx=part(".shx"), encoding="utf-8", encodingErrors="replace")
    fields = [f[0] for f in r.fields[1:]]
    low = {f.lower(): i for i, f in enumerate(fields)}
    iP, iU = low.get("zona_perki"), low.get("unsur")
    iK, iW, iT = low.get("keterangan"), low.get("wilayah"), low.get("tahun")
    if iP is None:
        raise RuntimeError(f"kolom Zona_Perki tidak ada di {shp} (kolom: {fields})")
    prak, ker, n_in = [], [], 0
    for sr in r.iterShapeRecords():
        rec = sr.record
        lvP = level_prakiraan(rec[iP])
        lvU = level_kerentanan(rec[iU]) if iU is not None else None
        if not (lvP or lvU) or not sr.shape.points:
            continue
        idx = koridor_tree.query(box(*sr.shape.bbox))
        if len(idx) == 0:
            continue
        g = _geom2d(shape(sr.shape.__geo_interface__))
        hits = [koridor_parts[int(i)] for i in idx]
        clip = None
        for k in hits:
            if k.intersects(g):
                c = g.intersection(k)
                clip = c if clip is None else clip.union(c)
        if clip is None or clip.is_empty:
            continue
        clip = clip.simplify(simplify, preserve_topology=True)
        if clip.is_empty:
            continue
        n_in += 1
        props = {"zona_prakiraan": str(rec[iP]), "zona_kerentanan": str(rec[iU]) if iU is not None else "",
                 "keterangan": (str(rec[iK])[:200] if iK is not None else ""),
                 "wilayah": str(rec[iW]) if iW is not None else "", "tahun_peta": str(rec[iT]) if iT is not None else ""}
        gm = mapping(clip)
        if lvP:
            prak.append({"level": lvP, "props": props, "geometry": gm})
        if lvU:
            ker.append({"level": lvU, "props": props, "geometry": gm})
    return prak, ker, len(r), n_in


def run(ctx, corridor, assets_sig):
    """Ambil prakiraan bulan berjalan (fallback: bulan sebelumnya) untuk provinsi di config.
    Mengembalikan dict: {"provinsi": {kode: meta}, "prakiraan": [(geom, level, props)], "zkgt": [...],
    "periode_bulan": "YYYY-MM", "selesai": n, "total": n, "pesan": str}."""
    import requests
    log, NOW, DATA, gcfg = ctx["log"], ctx["NOW"], ctx["DATA"], ctx["CFG"]["gerakan_tanah"]
    pcfg = gcfg["portalmbg"]
    sisa = ctx.get("sisa_waktu") or (lambda: 1e9)
    cdir = DATA / "gerakan_tanah" / "cache_portal"
    cdir.mkdir(parents=True, exist_ok=True)
    parts = list(getattr(corridor, "geoms", [corridor]))
    tree = STRtree(parts)
    parts_prep = parts  # intersects/intersection per part sudah cukup cepat untuk ukuran ini

    y, m = NOW.year, NOW.month
    kandidat = [(y, m), bulan_sebelumnya(y, m)]
    hasil = {"provinsi": {}, "prakiraan": [], "zkgt": [], "selesai": 0, "total": len(pcfg["provinsi"]), "pesan": ""}
    gagal = []
    for kode in pcfg["provinsi"]:
        cf = cdir / f"{kode}.json"
        cache = None
        if cf.exists():
            try:
                cache = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:  # noqa
                cache = None
        dipakai = None
        for (ty, tm) in kandidat:
            url = pcfg["url"].format(tahun=ty, bulan=tm, provinsi=kode)
            ym = f"{ty}-{tm:02d}"
            # Cache masih berlaku? (bulan & tanggal terbit sama, aset tidak berubah) → cek ringan dengan HEAD
            lm = None
            try:
                h = requests.head(url, headers=UA, timeout=(15, 40), allow_redirects=True)
                if h.status_code == 404:
                    continue
                lm = h.headers.get("Last-Modified")
            except Exception as e:  # noqa — HEAD gagal: lanjut GET
                log(f"   HEAD {kode} {ym} gagal: {str(e)[:80]}")
            if cache and cache.get("periode_bulan") == ym and cache.get("sig") == assets_sig and lm and cache.get("terbit") == lm:
                dipakai = cache
                log(f"   provinsi {kode} {ym}: cache masih berlaku (terbit {lm})")
                break
            if sisa() < 120:
                raise TimeoutError("anggaran waktu habis")
            try:
                t0 = datetime.now()
                r = ctx["get"](url, headers=UA, timeout=300)
            except Exception as e:  # noqa
                if "404" in str(e):
                    continue
                gagal.append(f"{kode}: {str(e)[:80]}")
                log(f"   provinsi {kode} {ym}: unduh gagal {str(e)[:120]}")
                break
            prak, ker, n_all, n_in = proses_zip(r.content, tree, parts_prep, pcfg.get("simplify", 0.0002))
            lm = r.headers.get("Last-Modified") or lm
            dipakai = {"kode": kode, "periode_bulan": ym, "terbit": lm, "sig": assets_sig,
                       "diambil": NOW.isoformat(timespec="minutes"), "poligon_provinsi": n_all, "poligon_koridor": n_in,
                       "ukuran_mb": round(len(r.content) / 1e6, 1), "prakiraan": prak, "zkgt": ker}
            cf.write_text(json.dumps(dipakai, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            log(f"   provinsi {kode} {ym}: {n_all} poligon, {n_in} di koridor aset "
                f"({len(r.content)/1e6:.1f} MB, {(datetime.now()-t0).seconds} dtk)")
            break
        if dipakai is None:
            if cache:  # sumber gagal → pakai hasil sebelumnya, ditandai
                dipakai = {**cache, "stale": True}
                gagal.append(f"{kode}: memakai data sebelumnya ({cache.get('periode_bulan')})")
            else:
                gagal.append(f"{kode}: belum ada data")
                continue
        hasil["selesai"] += 1
        hasil["provinsi"][kode] = {k: dipakai.get(k) for k in ("periode_bulan", "terbit", "diambil", "poligon_koridor", "stale")}
        for key in ("prakiraan", "zkgt"):
            for f in dipakai.get(key, []):
                hasil[key].append((shape(f["geometry"]), f["level"], {**f["props"], "provinsi": kode}))
    per = sorted({v["periode_bulan"] for v in hasil["provinsi"].values() if v.get("periode_bulan")})
    hasil["periode_bulan"] = per[0] if per else None  # bulan tertua bila campuran (jujur)
    hasil["periode_campuran"] = len(per) > 1
    hasil["pesan"] = "; ".join(gagal)
    return hasil


def nama_periode(ym):
    if not ym:
        return None
    y, m = ym.split("-")
    return f"Prakiraan Gerakan Tanah Bulan {BULAN[int(m) - 1]} {y}"


def assets_signature(assets_path, koridor_km, simplify):
    h = hashlib.sha1(assets_path.read_bytes())
    h.update(f"{koridor_km}|{simplify}".encode())
    return h.hexdigest()[:12]
