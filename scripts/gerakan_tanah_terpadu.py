"""
gerakan_tanah_terpadu.py — Model Gerakan Tanah & Longsor Terpadu PERTAGAS WASPADA (MGT-Pertagas).

Satu penilaian per titik sampel (tiap 250 m sepanjang jalur pipa) dan per fasilitas, menyatukan:
  • PVMBG – Badan Geologi : zona kerentanan gerakan tanah (ZKGT, kondisi geologi dasar) dan
                            prakiraan potensi gerakan tanah bulanan (resmi)
  • Model LHASA 1.1 internal : kemiringan lereng (Copernicus DEM GLO-90) dan pemicu hujan anteseden
                               7 hari (ARI ÷ ambang persentil-95 NASA)
  • ESA WorldCover 10 m 2021 : tutupan/tata guna lahan di koridor pipa

Sebelumnya PVMBG dan LHASA digabung PER AREA (zona tinggi di satu ruas + indikasi model di ruas lain yang
berjarak puluhan km tetap dianggap satu bahaya). Di sini keduanya digabung PER TITIK, sehingga yang dinilai
adalah tempat yang sama.

Tahap 1 — Kerentanan statis SK (1–5), overlay berbobot mengikuti parameter & bobot SNI 13-7124-2005
          (geologi 3 · kemiringan lereng 3 · tata guna lahan 2; curah hujan = pemicu, tahap 2):
              SK = bulat( (3·G + 3·L + 2·T) / 8 ),  lalu dibatasi lereng: SK ≤ L + 1
          G  geologi (ZKGT PVMBG)  : sangat rendah 1 · rendah 2 · menengah 4 · tinggi 5
          L  lereng (kelas LHASA)  : <5° 1 · 5–10° 2 · 10–15° 3 · 15–25° 4 · ≥25° 5 (persentil-90 lereng ±1 km)
          T  tutupan lahan         : pohon/mangrove/air 1 · semak/rawa 2 · rumput 3 · pertanian/terbangun 4 ·
                                     lahan terbuka 5 (akar pohon menahan lereng; lahan terbuka & pemotongan lereng
                                     untuk permukiman/pertanian melemahkannya)
          Komponen yang tidak tersedia di titik itu dikeluarkan dan bobotnya dinormalisasi ulang.
Tahap 2 — Pemicu hujan PH (0–3) dari rasio ARI/ARI95 model LHASA:
              < 0,75 → 0 · 0,75–1 → 1 (mendekati ambang) · 1–1,5 → 2 (melampaui) · ≥ 1,5 → 3 (jauh melampaui)
Tahap 3 — Tingkat bahaya harian = matriks SK × PH (perluasan pohon keputusan LHASA 1.1: hujan melampaui
          ambang & kerentanan 3–4 → sedang, kerentanan 5 → tinggi; kerentanan ≤ 2 hanya dinilai saat hujan
          jauh melampaui ambang; lahan datar < 5° tidak dinilai model harian).
                       PH 0    PH 1     PH 2     PH 3
              SK 1      –       –        –        –
              SK 2      –       –        –      rendah
              SK 3      –     rendah   sedang   sedang
              SK 4      –     rendah   sedang   tinggi
              SK 5      –     sedang   tinggi   tinggi
Tahap 4 — Lantai resmi PVMBG (prakiraan bulanan berlaku "bila curah hujan di atas normal" → dipicu PH):
              zona tinggi / banjir bandang : PH ≥ 2 → tinggi · PH 1 → sedang · PH 0 → "zona" (potensi, belum terpicu)
              zona menengah                : PH ≥ 2 → sedang · PH 1 → rendah
          Lahan datar (L = 1) di zona tinggi/menengah: hanya PH ≥ 2 yang dinaikkan, maks. rendah; PH < 2 → "zona"
          (zona tinggi) atau tidak ada (zona menengah) — poligon PVMBG berskala regional, mencegah alarm palsu di
          rawa/dataran. Alur banjir bandang tidak dibatasi (justru mengalir di dasar lembah).
          Tingkat akhir = yang terberat dari tahap 3 & 4; "sangat tinggi" bila model harian tinggi DAN titik berada
          di zona prakiraan tinggi/banjir bandang PVMBG (dua penilaian independen sepakat di tempat yang sama).

Keluaran (ditulis oleh lhasa.py):
  data/lhasa/terpadu.json          ringkasan per wilayah, ruas & fasilitas + metode (dibaca dashboard & klasifikasi.js)
  data/lhasa/terpadu_ruas.geojson  ruas pipa bertingkat (lapisan peta)

Bersifat indikatif untuk kesiapsiagaan; rujukan resmi tetap PVMBG. Bobot & matriks dapat diubah di
config/monitoring.json → gerakan_tanah_terpadu.
"""
import json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

LEVELS = ["sangat_tinggi", "tinggi", "sedang", "rendah", "zona"]        # urut terberat → teringan
RANK = {lv: i for i, lv in enumerate(LEVELS)}                           # 0 = terberat
LABEL = {"sangat_tinggi": "Sangat tinggi", "tinggi": "Tinggi", "sedang": "Sedang", "rendah": "Rendah",
         "zona": "Zona PVMBG (belum terpicu)"}
G_SKOR = {"sangat_rendah": 1, "rendah": 2, "menengah": 4, "tinggi": 5, "bandang": 5}
G_LABEL = {"sangat_rendah": "sangat rendah", "rendah": "rendah", "menengah": "menengah", "tinggi": "tinggi", "bandang": "banjir bandang"}
PRAK_URUT = {"tinggi": 4, "bandang": 4, "menengah": 3, "rendah": 2, "sangat_rendah": 1}
# Skor tata guna lahan (kode kelas ESA WorldCover → 1–5)
T_SKOR = {10: 1, 95: 1, 80: 1, 20: 2, 90: 2, 30: 3, 100: 3, 40: 4, 50: 4, 60: 5}
T_LABEL = {10: "tutupan pohon", 20: "semak belukar", 30: "padang rumput", 40: "lahan pertanian", 50: "lahan terbangun",
           60: "lahan terbuka", 80: "badan air", 90: "lahan basah", 95: "mangrove", 100: "lumut"}
L_LABEL = {1: "< 5°", 2: "5–10°", 3: "10–15°", 4: "15–25°", 5: "≥ 25°"}

BAWAAN = {
    "bobot": {"geologi": 3, "lereng": 3, "lahan": 2},
    "batas_pemicu": [0.75, 1.0, 1.5],
    # SK → tingkat untuk PH 0, 1, 2, 3
    "matriks": {"1": [None, None, None, None], "2": [None, None, None, "rendah"],
                "3": [None, "rendah", "sedang", "sedang"], "4": [None, "rendah", "sedang", "tinggi"],
                "5": [None, "sedang", "tinggi", "tinggi"]},
    "lahan_maks_jarak_m": 800,
    "maks_ruas_per_wilayah": 40,
}


def cfg():
    try:
        c = json.loads((ROOT / "config" / "monitoring.json").read_text(encoding="utf-8")).get("gerakan_tanah_terpadu", {})
    except Exception:  # noqa: BLE001
        c = {}
    return {**BAWAAN, **{k: v for k, v in c.items() if not k.startswith("_")}}


# ------------------------------------------------------------------ fungsi murni (diuji)
def skor_kerentanan(G, L, T, c=None):
    """→ (SK 1–5, nilai mentah). G/T boleh None (komponen dikeluarkan, bobot dinormalisasi)."""
    c = c or BAWAAN
    w = c["bobot"]
    num, den = w["lereng"] * L, w["lereng"]
    if G is not None:
        num += w["geologi"] * G
        den += w["geologi"]
    if T is not None:
        num += w["lahan"] * T
        den += w["lahan"]
    mentah = num / den
    sk = int(np.floor(mentah + 0.5))          # pembulatan setengah ke atas (bukan banker's rounding)
    sk = max(1, min(5, sk, int(L) + 1))       # lahan datar tidak bisa berkerentanan tinggi
    return sk, round(mentah, 2)


def pemicu(rasio, c=None):
    """Rasio ARI/ARI95 → PH 0–3."""
    if rasio is None or not np.isfinite(rasio):
        return 0
    b = (c or BAWAAN)["batas_pemicu"]
    return int(sum(rasio >= x for x in b))


def tingkat_harian(sk, ph, c=None):
    return (c or BAWAAN)["matriks"][str(int(sk))][int(ph)]


def lantai_pvmbg(prak, ph, L):
    """Tingkat minimum dari prakiraan bulanan PVMBG yang dipicu hujan."""
    if prak in ("tinggi", "bandang"):
        lv = "tinggi" if ph >= 2 else "sedang" if ph == 1 else "zona"
    elif prak == "menengah":
        lv = "sedang" if ph >= 2 else "rendah" if ph == 1 else None
    else:
        return None
    if prak != "bandang" and L <= 1:
        # lahan datar (< 5°): poligon PVMBG berskala regional, lereng lokal tidak mendukung longsor.
        # Hanya hujan yang melampaui ambang (PH ≥ 2) yang dinaikkan, itu pun maks. rendah;
        # di bawah ambang cukup "zona" (zona tinggi) atau tidak ada (zona menengah).
        if ph >= 2:
            lv = "rendah"
        else:
            lv = "zona" if prak == "tinggi" else None
    return lv


def terberat(*lv):
    c = [x for x in lv if x]
    return min(c, key=lambda x: RANK[x]) if c else None


def tingkat_akhir(sk, ph, prak, L, c=None):
    # lahan datar (< 5°) tidak dinilai oleh model harian; hanya alur banjir bandang PVMBG yang tetap berlaku
    h = tingkat_harian(sk, ph, c) if L >= 2 else None
    lv = terberat(h, lantai_pvmbg(prak, ph, L))
    if h == "tinggi" and prak in ("tinggi", "bandang") and ph >= 2:
        lv = "sangat_tinggi"
    return lv


def faktor_teks(p):
    """Penjelasan singkat penyebab di satu titik (untuk popup & laporan)."""
    out = []
    if p.get("rasio") is not None:
        out.append(f"hujan anteseden {round(p['rasio'] * 100)}% ambang")
    out.append(f"lereng {L_LABEL.get(p['L'], '?')}")
    if p.get("G"):
        out.append(f"geologi zona {G_LABEL.get(p['G'], p['G'])} PVMBG")
    if p.get("T") is not None:
        out.append(T_LABEL.get(p["T"], "lahan ?"))
    if p.get("prak") in ("tinggi", "bandang", "menengah"):
        out.append(f"prakiraan {G_LABEL[p['prak']]} PVMBG bulan ini")
    return " · ".join(out)


# ------------------------------------------------------------------ data spasial pendukung
def baca_pvmbg(path=None):
    """Poligon prakiraan PVMBG (properti level = prakiraan, level_kerentanan = ZKGT) → (geoms, prak, zkgt, periode)."""
    from shapely.geometry import shape
    p = Path(path or DATA / "gerakan_tanah" / "prakiraan.geojson")
    if not p.exists():
        return [], [], [], None
    fc = json.loads(p.read_text(encoding="utf-8"))
    g, pr, zk = [], [], []
    for f in fc.get("features", []):
        try:
            geom = shape(f["geometry"])
        except Exception:  # noqa: BLE001
            continue
        if geom.is_empty:
            continue
        props = f.get("properties") or {}
        g.append(geom)
        pr.append(props.get("level"))
        zk.append(props.get("level_kerentanan") or props.get("level"))
    return g, pr, zk, fc.get("periode")


def baca_lahan(path=None):
    """Ruas koridor tutupan lahan (tutupan_lahan.py) → (geoms, kode)."""
    from shapely.geometry import shape
    p = Path(path or DATA / "tutupan_lahan" / "koridor.geojson")
    if not p.exists():
        return [], []
    fc = json.loads(p.read_text(encoding="utf-8"))
    g, k = [], []
    for f in fc.get("features", []):
        try:
            g.append(shape(f["geometry"]))
            k.append(int(f["properties"]["kode"]))
        except Exception:  # noqa: BLE001
            continue
    return g, k


def tempel_pvmbg(xs, ys, geoms, prak, zkgt):
    """Tingkat prakiraan & ZKGT terberat di tiap titik (point-in-polygon, STRtree)."""
    from shapely import STRtree, points
    n = len(xs)
    P, Z = [None] * n, [None] * n
    if not geoms:
        return P, Z
    tree = STRtree(geoms)
    pts = points(np.column_stack([xs, ys]))
    ip, ig = tree.query(pts, predicate="intersects")
    for i, j in zip(ip.tolist(), ig.tolist()):
        a, b = prak[j], zkgt[j]
        if a and (P[i] is None or PRAK_URUT.get(a, 0) > PRAK_URUT.get(P[i], 0)):
            P[i] = a
        if b and (Z[i] is None or PRAK_URUT.get(b, 0) > PRAK_URUT.get(Z[i], 0)):
            Z[i] = b
    return P, Z


def tempel_lahan(xs, ys, geoms, kode, maks_m):
    """Kelas tutupan lahan ruas koridor terdekat (≤ maks_m) untuk tiap titik."""
    from shapely import STRtree, points
    n = len(xs)
    T = [None] * n
    if not geoms:
        return T
    tree = STRtree(geoms)
    pts = points(np.column_stack([xs, ys]))
    ip, ig = tree.query_nearest(pts, max_distance=maks_m / 111320.0, all_matches=False)
    for i, j in zip(ip.tolist(), ig.tolist()):
        T[i] = kode[j]
    return T


# ------------------------------------------------------------------ perhitungan utama
def hitung(areas, lines, facs, kelas, rasio_produk, tanggal_produk=None, sumber_kerentanan=None,
           pvmbg=None, lahan=None, c=None, now_iso=None):
    """
    areas          : dict kode → info wilayah
    lines, facs    : keluaran lhasa.load_assets() (titik sampel berurutan: semua titik garis, lalu fasilitas)
    kelas          : array kelas lereng/kerentanan LHASA (1–5) sejajar titik sampel
    rasio_produk   : {"hari_ini": array rasio ARI/ARI95, "besok": array} sejajar titik sampel
    pvmbg, lahan   : (opsional, untuk uji) hasil baca_pvmbg() / baca_lahan()
    → (dict JSON, list fitur GeoJSON)
    """
    c = c or cfg()
    xs = np.array([p[0] for l in lines for p in l["pts"]] + [f["lon"] for f in facs], dtype="float64")
    ys = np.array([p[1] for l in lines for p in l["pts"]] + [f["lat"] for f in facs], dtype="float64")
    n = len(xs)
    if len(kelas) != n:
        raise ValueError(f"jumlah kelas lereng ({len(kelas)}) ≠ jumlah titik sampel ({n})")
    pg, pp, pz, periode = pvmbg if pvmbg is not None else baca_pvmbg()
    lg, lk = lahan if lahan is not None else baca_lahan()
    PRAK, ZK = tempel_pvmbg(xs, ys, pg, pp, pz)
    T = tempel_lahan(xs, ys, lg, lk, c["lahan_maks_jarak_m"])
    L = [int(v) for v in kelas]
    G = [G_SKOR.get(z) if z else None for z in ZK]
    Tsk = [T_SKOR.get(t) if t is not None else None for t in T]
    SK = np.zeros(n, dtype=int)
    for i in range(n):
        SK[i] = skor_kerentanan(G[i], L[i], Tsk[i], c)[0]

    # batas titik per ruas
    off, bounds = 0, []
    for l in lines:
        bounds.append((off, len(l["pts"])))
        off += len(l["pts"])
    foff = off

    def titik(i, rasio):
        r = float(rasio[i]) if np.isfinite(rasio[i]) else None
        return {"SK": int(SK[i]), "L": L[i], "G": ZK[i], "T": T[i], "prak": PRAK[i],
                "rasio": round(r, 2) if r is not None else None, "PH": pemicu(r, c)}

    produk, fitur = {}, []
    for nama, rasio in rasio_produk.items():
        rasio = np.asarray(rasio, dtype="float64")
        PH = np.array([pemicu(float(r) if np.isfinite(r) else None, c) for r in rasio], dtype=int)
        LV = [tingkat_akhir(SK[i], PH[i], PRAK[i], L[i], c) for i in range(n)]
        per_area = {}
        for code in areas:
            km = {lv: 0.0 for lv in LEVELS}
            sk_km = {str(s): 0.0 for s in range(1, 6)}
            l_km = {str(s): 0.0 for s in range(1, 6)}
            g_km, t_km = {}, {}
            total, rmax, ruas = 0.0, 0.0, []
            for l, (o, m) in zip(lines, bounds):
                if l["area"] != code:
                    continue
                total += sum(l["seg_km"])
                sl = []
                for j in range(m - 1):
                    a, b = o + j, o + j + 1
                    seg = l["seg_km"][j]
                    sl.append(terberat(LV[a], LV[b]))
                    sk_km[str(max(SK[a], SK[b]))] += seg
                    l_km[str(max(L[a], L[b]))] += seg
                    gk = ZK[a] or ZK[b] or "tidak_ada"
                    g_km[gk] = g_km.get(gk, 0.0) + seg
                    tk = str(T[a] if T[a] is not None else T[b] if T[b] is not None else "tidak_ada")
                    t_km[tk] = t_km.get(tk, 0.0) + seg
                fin = rasio[o:o + m][np.isfinite(rasio[o:o + m])]
                if fin.size:
                    rmax = max(rmax, float(fin.max()))
                j = 0
                while j < len(sl):
                    if sl[j] is None:
                        j += 1
                        continue
                    e = j
                    while e + 1 < len(sl) and sl[e + 1] == sl[j]:
                        e += 1
                    seg_km = sum(l["seg_km"][j:e + 1])
                    km[sl[j]] += seg_km
                    idx = list(range(o + j, o + e + 2))
                    # titik penentu = yang terberat (tingkat, lalu pemicu, lalu kerentanan)
                    ip = min(idx, key=lambda q: (RANK.get(LV[q], 9), -PH[q], -SK[q]))
                    pt = titik(ip, rasio)
                    coords = [[round(v, 5) for v in l["pts"][t - o]] for t in idx]
                    step = max(1, len(coords) // 40)
                    garis = coords[::step] + ([coords[-1]] if (len(coords) - 1) % step else [])
                    mid = coords[len(coords) // 2]
                    info = {"aset": l["aset"], "level": sl[j], "km": round(seg_km, 2), "lat": mid[1], "lon": mid[0],
                            **pt, "faktor": faktor_teks(pt), "garis": garis}
                    ruas.append(info)
                    fitur.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                                  "properties": {"area": code, "produk": nama,
                                                 **{k: v for k, v in info.items() if k not in ("lat", "lon", "garis")}}})
                    j = e + 1
            fas = []
            for i, f in enumerate(facs):
                if f["area"] != code:
                    continue
                g = foff + i
                if np.isfinite(rasio[g]):
                    rmax = max(rmax, float(rasio[g]))
                if LV[g]:
                    pt = titik(g, rasio)
                    fas.append({"aset": f["aset"], "level": LV[g], "lat": round(f["lat"], 5), "lon": round(f["lon"], 5),
                                **pt, "faktor": faktor_teks(pt)})
            if total == 0 and not any(f["area"] == code for f in facs):
                continue
            ruas.sort(key=lambda r: (RANK[r["level"]], -r["PH"], -r["SK"], -r["km"]))
            fas.sort(key=lambda r: (RANK[r["level"]], -r["PH"], -r["SK"]))
            worst = next((lv for lv in LEVELS if km[lv] > 0 or any(x["level"] == lv for x in fas)), None)
            per_area[code] = {
                "total_pipa_km": round(total, 1), "pipa_km": {k: round(v, 2) for k, v in km.items()},
                "kerentanan_km": {k: round(v, 1) for k, v in sk_km.items()},
                "komponen_km": {"lereng": {k: round(v, 1) for k, v in l_km.items()},
                                "geologi": {k: round(v, 1) for k, v in sorted(g_km.items(), key=lambda kv: -kv[1])},
                                "lahan": {k: round(v, 1) for k, v in sorted(t_km.items(), key=lambda kv: -kv[1])}},
                "rasio_maks": round(rmax, 2), "terburuk": worst, "jumlah_ruas": len(ruas),
                "ruas": ruas[:c["maks_ruas_per_wilayah"]], "fasilitas": fas,
            }
        produk[nama] = {"tanggal": (tanggal_produk or {}).get(nama), "stale": False, "per_area": per_area}

    cakup = {"pvmbg": round(float(np.mean([z is not None for z in ZK])) if n else 0, 3),
             "lahan": round(float(np.mean([t is not None for t in T])) if n else 0, 3)}
    out = {
        "model": "Model Gerakan Tanah & Longsor Terpadu (MGT-Pertagas) v1",
        "jenis": "Indikasi model — gabungan PVMBG, model LHASA & tutupan lahan; bukan pengganti informasi resmi PVMBG",
        "diambil": now_iso, "titik": n, "cakupan": cakup, "periode_pvmbg": periode,
        "sumber": {"geologi": "PVMBG – zona kerentanan gerakan tanah (Portal MBG)",
                   "prakiraan": f"PVMBG – {periode}" if periode else "PVMBG – prakiraan bulanan",
                   "lereng": sumber_kerentanan or "Copernicus DEM GLO-90 (kelas lereng LHASA)",
                   "lahan": "ESA WorldCover 10 m 2021 v200 (koridor pipa)",
                   "hujan": "Hujan anteseden 7 hari Open-Meteo vs ambang ARI95 NASA (model LHASA)"},
        "metode": {"bobot": c["bobot"], "batas_pemicu": c["batas_pemicu"], "matriks": c["matriks"],
                   "skor_geologi": G_SKOR, "skor_lahan": {str(k): v for k, v in T_SKOR.items()},
                   "label_lahan": {str(k): v for k, v in T_LABEL.items()}, "label_lereng": {str(k): v for k, v in L_LABEL.items()},
                   "rujukan": ["SNI 13-7124-2005 (parameter & bobot zona kerentanan gerakan tanah)",
                               "Kirschbaum & Stanley 2018 — NASA LHASA 1.1",
                               "Stanley dkk. 2021 — LHASA 2.0 (Frontiers in Earth Science)",
                               "ESA WorldCover 2021 v200"]},
        "label": LABEL, "produk": produk,
    }
    return out, fitur
