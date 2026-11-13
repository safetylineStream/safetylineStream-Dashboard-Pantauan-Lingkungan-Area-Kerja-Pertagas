"""
banjir_sungai.py — Indikasi potensi banjir sungai di sel sungai yang dilintasi/didekati jalur pipa.

Sumber : Open-Meteo Flood API — model GloFAS v4 (Copernicus/ECMWF), grid 0,05° (±5 km), debit harian (m³/s).
Jenis  : KELUARAN MODEL (bukan pengukuran pos duga air, bukan peringatan resmi).
Spasial: hanya sel yang (a) dilalui jalur pipa dan (b) benar-benar sel sungai menurut model
         (median debit 5 tahun ≥ `sungai_min_median_m3s`). Sel bukan sungai tidak dinilai dan tidak ditampilkan.

Tiga tahap (semua otomatis, tahan gagal):
  1. SARING  — titik tiap ±2,5 km sepanjang pipa ditanyakan ke API; API mengembalikan pusat sel GloFAS yang dipakai
               ("sungai terbesar dalam area 5 km"). Sel unik + km pipa per sel disimpan. Diulang bila aset berubah.
  2. AMBANG  — untuk sel berdebit (≥ `saring_min_m3s`), riwayat debit 5 tahun diunduh SEKALI lalu diringkas menjadi
               median, persentil 98, persentil 99,5 dan maksimum. Dicicil `sel_per_run` sel per run (hemat kuota API),
               sungai terbesar lebih dulu.
  3. PRAKIRAAN — debit `horizon_hari` ke depan dibandingkan ambang sel itu sendiri:
               Tinggi  = puncak ≥ maksimum 5 tahun · Sedang = ≥ persentil 99,5 · Rendah = ≥ persentil 98.

Keluaran: data/banjir/sel_statis.json (tahap 1–2) dan data/banjir/banjir.json (tahap 3).
"""
import hashlib
import os
import sys
import time
import traceback
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from umum import DATA, ambil, baca_json, cfg, densify, iso, kuantil, log, muat_aset, sekarang, tulis_json  # noqa: E402

OUT = DATA / "banjir"
STATIS = OUT / "sel_statis.json"
CFG = cfg("banjir_sungai", {
    "url": "https://flood-api.open-meteo.com/v1/flood",
    "interval_m": 250,          # kerapatan titik untuk menghitung km pipa per sel
    "langkah_titik": 10,        # tiap 10 titik (±2,5 km) ditanyakan ke API
    "saring_min_m3s": 5,        # sel dengan debit di bawah ini dianggap bukan sungai (tidak diunduh riwayatnya)
    "sungai_min_median_m3s": 10,
    "tahun_riwayat": 5,
    "sel_per_run": 40,
    "horizon_hari": 7,
    "saring_ulang_hari": 180,
    "persentil_rendah": 0.98,
    "persentil_sedang": 0.995,
})
LEVELS = ["tinggi", "sedang", "rendah"]
UKURAN = 0.05


def kunci(lat, lon):
    return f"{float(lat):.3f},{float(lon):.3f}"


def sidik_aset():
    return hashlib.sha1((DATA / "assets.geojson").read_bytes()).hexdigest()[:12]


def minta(lat, lon, **params):
    r = ambil(CFG["url"], params={"latitude": ",".join(f"{y:.4f}" for y in lat), "longitude": ",".join(f"{x:.4f}" for x in lon), **params},
              timeout=(20, 120))
    js = r.json()
    return js if isinstance(js, list) else [js]


def angka(arr):
    return [float(v) for v in (arr or []) if v is not None]


# ------------------------------------------------------------------ tahap 1
def saring(garis, lama):
    """Petakan jalur pipa ke sel GloFAS. → dict sel {kunci: {lat, lon, saring_maks, aset: {"AREA|aset": km}, hist}}."""
    langkah, tanya, milik = CFG["langkah_titik"], [], []
    for gi, g in enumerate(garis):
        pts = densify(g["coords"], CFG["interval_m"])
        idx = list(range(0, len(pts), langkah))
        if idx[-1] != len(pts) - 1:
            idx.append(len(pts) - 1)
        awal = len(tanya)
        tanya += [pts[i] for i in idx]
        km_titik = CFG["interval_m"] / 1000.0
        for i in range(len(pts)):
            j = min(len(idx) - 1, int(round(i / langkah)))
            if abs(idx[-1] - i) < abs(idx[j] - i):
                j = len(idx) - 1
            milik.append((awal + j, gi, km_titik))
    log(f"Saring: {len(tanya)} titik pertanyaan dari {len(garis)} ruas pipa ...")
    sel_titik = [None] * len(tanya)
    sel = {}
    for i in range(0, len(tanya), 100):
        part = tanya[i:i + 100]
        js = minta([p[1] for p in part], [p[0] for p in part], daily="river_discharge", past_days=7, forecast_days=7)
        for k, j in enumerate(js):
            v = angka((j.get("daily") or {}).get("river_discharge"))
            key = kunci(j["latitude"], j["longitude"])
            s = sel.setdefault(key, {"lat": round(float(j["latitude"]), 4), "lon": round(float(j["longitude"]), 4), "saring_maks": None, "aset": {}})
            if v:
                s["saring_maks"] = round(max(v + ([s["saring_maks"]] if s["saring_maks"] is not None else [])), 2)
            sel_titik[i + k] = key
        time.sleep(1.2)
    for q, gi, km in milik:
        key = sel_titik[q]
        if key is None:
            continue
        nama = f"{garis[gi]['area']}|{garis[gi]['aset']}"
        sel[key]["aset"][nama] = sel[key]["aset"].get(nama, 0.0) + km
    for key, s in sel.items():
        s["aset"] = {k: round(v, 2) for k, v in s["aset"].items()}
        s["hist"] = ((lama or {}).get(key) or {}).get("hist")
    log(f"Saring: {len(sel)} sel GloFAS unik; {sum(1 for s in sel.values() if (s['saring_maks'] or 0) >= CFG['saring_min_m3s'])} sel berdebit ≥ {CFG['saring_min_m3s']} m³/s")
    return sel


# ------------------------------------------------------------------ tahap 2
def ringkas_riwayat(nilai):
    v = sorted(angka(nilai))
    if len(v) < 365:
        return None
    return {"median": round(kuantil(v, 0.5), 2), "p_rendah": round(kuantil(v, CFG["persentil_rendah"]), 2),
            "p_sedang": round(kuantil(v, CFG["persentil_sedang"]), 2), "maks": round(v[-1], 2), "n": len(v)}


def lengkapi_ambang(sel, jatah):
    perlu = sorted((s for s in sel.values() if s.get("hist") is None and (s["saring_maks"] or 0) >= CFG["saring_min_m3s"]),
                   key=lambda s: -(s["saring_maks"] or 0))
    if not perlu or jatah <= 0:
        return 0
    kerja = perlu[:jatah]
    akhir = (sekarang() - timedelta(days=3)).date()
    awal = akhir.replace(year=akhir.year - CFG["tahun_riwayat"])
    log(f"Ambang: mengunduh riwayat {awal}..{akhir} untuk {len(kerja)} dari {len(perlu)} sel yang belum berambang ...")
    selesai = 0
    for i in range(0, len(kerja), 10):
        part = kerja[i:i + 10]
        try:
            js = minta([s["lat"] for s in part], [s["lon"] for s in part], daily="river_discharge", start_date=str(awal), end_date=str(akhir))
        except Exception as e:  # noqa: BLE001
            log(f"  riwayat gagal ({str(e)[:140]}) — dilanjutkan run berikutnya")
            break
        for s, j in zip(part, js):
            h = ringkas_riwayat((j.get("daily") or {}).get("river_discharge"))
            s["hist"] = {**h, "dari": str(awal), "sampai": str(akhir)} if h else {"kosong": True}
            selesai += 1
        time.sleep(4)
    return selesai


# ------------------------------------------------------------------ tahap 3
def level_dari(puncak, h):
    if puncak is None or not h or h.get("kosong"):
        return None
    if puncak >= h["maks"]:
        return "tinggi"
    if puncak >= h["p_sedang"]:
        return "sedang"
    if puncak >= h["p_rendah"]:
        return "rendah"
    return None


def sel_sungai(sel):
    return {k: s for k, s in sel.items() if s.get("hist") and not s["hist"].get("kosong") and s["hist"]["median"] >= CFG["sungai_min_median_m3s"]}


def nilai_prakiraan(daily, hari_ini):
    """→ (debit_kini, puncak, tgl_puncak, puncak_ensemble_maks) mulai tanggal hari_ini (yyyy-mm-dd)."""
    t, q, qm = daily.get("time") or [], daily.get("river_discharge") or [], daily.get("river_discharge_max") or []
    depan = [(t[i], q[i], qm[i] if i < len(qm) else None) for i in range(len(t)) if t[i] >= hari_ini and i < len(q) and q[i] is not None]
    if not depan:
        return None, None, None, None
    pk = max(depan, key=lambda x: x[1])
    ens = [x[2] for x in depan if x[2] is not None]
    return round(depan[0][1], 1), round(pk[1], 1), pk[0], (round(max(ens), 1) if ens else None)


def prakiraan(sungai, fas):
    hari_ini = sekarang().strftime("%Y-%m-%d")
    daftar, hasil = list(sungai.items()), []
    for i in range(0, len(daftar), 100):
        part = daftar[i:i + 100]
        js = minta([s["lat"] for _, s in part], [s["lon"] for _, s in part], daily="river_discharge,river_discharge_max",
                   past_days=1, forecast_days=CFG["horizon_hari"] + 1)
        for (key, s), j in zip(part, js):
            kini, puncak, tgl, ens = nilai_prakiraan(j.get("daily") or {}, hari_ini)
            h = s["hist"]
            aset = [{"area": k.split("|", 1)[0], "aset": k.split("|", 1)[1], "km": v} for k, v in sorted(s["aset"].items(), key=lambda x: -x[1])]
            dekat = [f for f in fas if abs(f["lat"] - s["lat"]) <= UKURAN / 2 and abs(f["lon"] - s["lon"]) <= UKURAN / 2]
            hasil.append({
                "id": key, "lat": s["lat"], "lon": s["lon"], "area": sorted({a["area"] for a in aset} | {f["area"] for f in dekat}),
                "aset": aset[:6], "pipa_km": round(sum(a["km"] for a in aset), 1), "fasilitas": [{"area": f["area"], "aset": f["aset"]} for f in dekat][:8],
                "median": h["median"], "p_rendah": h["p_rendah"], "p_sedang": h["p_sedang"], "maks": h["maks"],
                "debit_kini": kini, "puncak": puncak, "puncak_tgl": tgl, "puncak_ens_maks": ens,
                "rasio": round(puncak / h["maks"], 2) if puncak is not None and h["maks"] else None,
                "level": level_dari(puncak, h), "level_ens": level_dari(ens, h),
            })
        time.sleep(1.2)
    return hasil


def ringkas_area(hasil, areas):
    per = {}
    for a in areas:
        sel = [s for s in hasil if a in s["area"]]
        if not sel:
            continue
        km = {lv: round(sum(x["km"] for s in sel if s["level"] == lv for x in s["aset"] if x["area"] == a), 1) for lv in LEVELS}
        n = {lv: sum(1 for s in sel if s["level"] == lv) for lv in LEVELS}
        puncak = max((s for s in sel if s["rasio"] is not None), key=lambda s: s["rasio"], default=None)
        per[a] = {"sel_sungai": len(sel), "sel": n, "pipa_km": km, "terburuk": next((lv for lv in LEVELS if n[lv]), None),
                  "rasio_maks": puncak["rasio"] if puncak else None, "sel_puncak": puncak["id"] if puncak else None,
                  "fasilitas": sorted({f["aset"] for s in sel if s["level"] for f in s["fasilitas"] if f["area"] == a})[:10]}
    return per


def main():
    t0 = time.time()
    areas, garis, fas = muat_aset()
    statis = baca_json(STATIS, {}) or {}
    lama = baca_json(OUT / "banjir.json", {}) or {}
    status = {"ok": True, "pesan": None}
    try:
        sidik = sidik_aset()
        from umum import umur_menit
        um = umur_menit(statis.get("saring_pada"))
        if statis.get("sidik_aset") != sidik or not statis.get("sel") or um is None or um > CFG["saring_ulang_hari"] * 1440:
            statis = {"sidik_aset": sidik, "saring_pada": iso(), "sel": saring(garis, statis.get("sel"))}
            tulis_json(STATIS, statis)
        jatah = int(os.environ.get("BANJIR_SEL_PER_RUN") or CFG["sel_per_run"])
        if lengkapi_ambang(statis["sel"], jatah):
            tulis_json(STATIS, statis)
        sel = statis["sel"]
        sungai = sel_sungai(sel)
        menunggu = sum(1 for s in sel.values() if s.get("hist") is None and (s["saring_maks"] or 0) >= CFG["saring_min_m3s"])
        log(f"Prakiraan: {len(sungai)} sel sungai (menunggu ambang: {menunggu}) ...")
        hasil = prakiraan(sungai, fas) if sungai else []
        hasil.sort(key=lambda s: (-(LEVELS[::-1].index(s["level"]) + 1 if s["level"] else 0), -(s["rasio"] or 0)))
        tulis_json(OUT / "banjir.json", {
            "model": "GloFAS v4 (Copernicus/ECMWF) via Open-Meteo Flood API", "sumber": "Open-Meteo Flood API — GloFAS v4, grid 0,05°",
            "jenis": "Keluaran model hidrologi — bukan pengukuran pos duga air dan bukan peringatan resmi",
            "catatan": "Tingkat dihitung dari puncak debit prakiraan dibanding riwayat debit model di sel yang sama: Tinggi = ≥ maksimum "
                       f"{CFG['tahun_riwayat']} tahun, Sedang = ≥ persentil {CFG['persentil_sedang'] * 100:g}, Rendah = ≥ persentil {CFG['persentil_rendah'] * 100:g}. "
                       "Sel 5 km mewakili sungai terbesar di area itu; sungai kecil dan genangan lokal tidak terwakili.",
            "diambil": iso(), "horizon_hari": CFG["horizon_hari"], "tahun_riwayat": CFG["tahun_riwayat"], "ukuran_sel": UKURAN,
            "persentil": {"rendah": CFG["persentil_rendah"], "sedang": CFG["persentil_sedang"]}, "sungai_min_median_m3s": CFG["sungai_min_median_m3s"],
            "statis": {"sel_dilalui_pipa": len(sel), "sel_sungai": len(sungai), "menunggu_ambang": menunggu, "lengkap": menunggu == 0},
            "sel": hasil, "per_area": ringkas_area(hasil, areas), "status": status, "durasi_detik": round(time.time() - t0)})
        log(f"Selesai: {sum(1 for s in hasil if s['level'])} sel berindikasi dari {len(hasil)} sel sungai.")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        lama["status"] = {"ok": False, "pesan": str(e)[:200], "diperiksa": iso()}
        lama.setdefault("sel", [])
        lama.setdefault("per_area", {})
        tulis_json(OUT / "banjir.json", lama)


if __name__ == "__main__":
    main()
