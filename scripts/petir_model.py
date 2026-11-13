"""
petir_model.py — Potensi badai petir (prakiraan model) per sel 0,1° sepanjang jalur pipa & fasilitas.

Sumber : Open-Meteo Forecast API — CAPE (energi konvektif), kode cuaca WMO (95/96/99 = badai petir), curah hujan per jam.
Jenis  : PRAKIRAAN MODEL CUACA — BUKAN deteksi sambaran petir. Tidak dapat menyatakan kapan/di mana petir menyambar.
Spasial: grid 0,1° (±11 km); hanya sel yang dilalui pipa atau memuat fasilitas yang dihitung.

Tingkat per jam (ambang di config/monitoring.json → petir_model; ambang CAPE mengikuti kategori umum meteorologi):
  Tinggi = model memprakirakan badai petir (kode 95/96/99) DAN CAPE ≥ cape_tinggi
  Sedang = model memprakirakan badai petir, ATAU CAPE ≥ cape_tinggi dengan hujan ≥ hujan_sedang_mm/jam
  Rendah = CAPE ≥ cape_rendah dengan hujan ≥ hujan_rendah_mm/jam
Tingkat sel = tingkat tertinggi dalam produk ("hari_ini" = sisa hari ini, "besok" = sehari penuh, WIB).

Keluaran: data/petir_model/petir.json
"""
import sys
import time
import traceback
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from umum import DATA, ambil, baca_json, cfg, densify, iso, log, muat_aset, pusat_sel, sekarang, tulis_json  # noqa: E402

OUT = DATA / "petir_model"
CFG = cfg("petir_model", {
    "url": "https://api.open-meteo.com/v1/forecast",
    "interval_m": 250,
    "cape_rendah": 1000,
    "cape_tinggi": 2500,
    "hujan_rendah_mm": 0.5,
    "hujan_sedang_mm": 1.0,
})
UKURAN = 0.1
LEVELS = ["tinggi", "sedang", "rendah"]
NILAI = {None: 0, "rendah": 1, "sedang": 2, "tinggi": 3}
NAMA = {0: None, 1: "rendah", 2: "sedang", 3: "tinggi"}
BADAI = {95, 96, 99}


def level_jam(cape, kode, hujan):
    """Tingkat potensi petir satu jam → 0 (tidak ada) … 3 (tinggi)."""
    cape = cape or 0
    hujan = hujan or 0
    badai = kode is not None and int(kode) in BADAI
    if badai and cape >= CFG["cape_tinggi"]:
        return 3
    if badai or (cape >= CFG["cape_tinggi"] and hujan >= CFG["hujan_sedang_mm"]):
        return 2
    if cape >= CFG["cape_rendah"] and hujan >= CFG["hujan_rendah_mm"]:
        return 1
    return 0


def sel_aset(garis, fas):
    """→ {(lon,lat) pusat sel: {"pipa": {"AREA|aset": km}, "fas": [{area, aset}]}}"""
    sel, km_titik = {}, CFG["interval_m"] / 1000.0
    for g in garis:
        for x, y in densify(g["coords"], CFG["interval_m"]):
            s = sel.setdefault(pusat_sel(x, y, UKURAN), {"pipa": {}, "fas": []})
            k = f"{g['area']}|{g['aset']}"
            s["pipa"][k] = s["pipa"].get(k, 0.0) + km_titik
    for f in fas:
        sel.setdefault(pusat_sel(f["lon"], f["lat"], UKURAN), {"pipa": {}, "fas": []})["fas"].append({"area": f["area"], "aset": f["aset"]})
    return sel


def produk_sel(hourly, now):
    """Deret per jam satu sel → {"hari_ini": {...}, "besok": {...}}; jam = string 24 karakter ('-' = sudah lewat)."""
    t = hourly.get("time") or []
    cape, kode, hujan = hourly.get("cape") or [], hourly.get("weather_code") or [], hourly.get("precipitation") or []
    hari = {"hari_ini": now.strftime("%Y-%m-%d"), "besok": (now + timedelta(days=1)).strftime("%Y-%m-%d")}
    jam_kini = now.strftime("%Y-%m-%dT%H:00")
    out = {}
    for nama, tgl in hari.items():
        jam, cmaks, hmaks = ["-"] * 24, None, None
        for i, ts in enumerate(t):
            if not ts.startswith(tgl) or ts < jam_kini:
                continue
            c = cape[i] if i < len(cape) else None
            lv = level_jam(c, kode[i] if i < len(kode) else None, hujan[i] if i < len(hujan) else None)
            jam[int(ts[11:13])] = str(lv)
            if c is not None:
                cmaks = c if cmaks is None else max(cmaks, c)
            if i < len(hujan) and hujan[i] is not None:
                hmaks = hujan[i] if hmaks is None else max(hmaks, hujan[i])
        angka = [int(x) for x in jam if x != "-"]
        out[nama] = {"level": NAMA[max(angka)] if angka else None, "jam": "".join(jam),
                     "cape_maks": round(cmaks) if cmaks is not None else None, "hujan_maks": round(hmaks, 1) if hmaks is not None else None}
    return out, hari


def gabung_jam(daftar):
    """Maksimum per jam dari beberapa string jam 24 karakter."""
    out = []
    for h in range(len(daftar[0]) if daftar else 24):
        v = [int(s[h]) for s in daftar if s[h] != "-"]
        out.append(str(max(v)) if v else "-")
    return "".join(out)


def ringkas_area(sel_hasil, areas, produk):
    per = {}
    for a in areas:
        mine = [s for s in sel_hasil if a in s["area"]]
        if not mine:
            continue
        km = {lv: 0.0 for lv in LEVELS}
        n = {lv: 0 for lv in LEVELS}
        fasl = {lv: set() for lv in LEVELS}
        for s in mine:
            lv = s[produk]["level"]
            if not lv:
                continue
            n[lv] += 1
            km[lv] += sum(x["km"] for x in s["pipa"] if x["area"] == a)
            fasl[lv].update(f["aset"] for f in s["fas"] if f["area"] == a)
        capes = [s[produk]["cape_maks"] for s in mine if s[produk]["cape_maks"] is not None]
        per[a] = {"sel_total": len(mine), "sel": n, "pipa_km": {k: round(v, 1) for k, v in km.items()},
                  "fasilitas": {k: sorted(v)[:12] for k, v in fasl.items() if v}, "n_fasilitas": {k: len(v) for k, v in fasl.items()},
                  "terburuk": next((lv for lv in LEVELS if n[lv]), None), "jam": gabung_jam([s[produk]["jam"] for s in mine]),
                  "cape_maks": max(capes) if capes else None}
    return per


def main():
    t0 = time.time()
    lama = baca_json(OUT / "petir.json", {}) or {}
    try:
        areas, garis, fas = muat_aset()
        sel = sel_aset(garis, fas)
        kunci = list(sel)
        now = sekarang()
        log(f"Potensi petir: {len(kunci)} sel 0,1° dari Open-Meteo ...")
        hasil, hari, kosong = [], {}, 0
        for i in range(0, len(kunci), 100):
            part = kunci[i:i + 100]
            js = ambil(CFG["url"], params={"latitude": ",".join(str(k[1]) for k in part), "longitude": ",".join(str(k[0]) for k in part),
                                           "hourly": "cape,weather_code,precipitation", "forecast_days": 2, "timezone": "Asia/Jakarta"},
                       timeout=(20, 90)).json()
            js = js if isinstance(js, list) else [js]
            for k, j in zip(part, js):
                h = j.get("hourly") or {}
                if not any(v is not None for v in (h.get("cape") or [])):
                    kosong += 1
                pr, hari = produk_sel(h, now)
                s = sel[k]
                pipa = [{"area": n.split("|", 1)[0], "aset": n.split("|", 1)[1], "km": round(v, 1)} for n, v in sorted(s["pipa"].items(), key=lambda x: -x[1])]
                hasil.append({"lon": k[0], "lat": k[1], "area": sorted({p["area"] for p in pipa} | {f["area"] for f in s["fas"]}),
                              "pipa": pipa, "fas": s["fas"], **pr})
            time.sleep(1)
        if kunci and kosong / len(kunci) > 0.2:
            raise RuntimeError(f"{kosong} dari {len(kunci)} sel tanpa nilai CAPE — hasil tidak dapat diandalkan")
        produk = {p: {"tanggal": hari.get(p), "per_area": ringkas_area(hasil, areas, p)} for p in ("hari_ini", "besok")}
        ada = [s for s in hasil if s["hari_ini"]["level"] or s["besok"]["level"]]
        for s in ada:   # ringkas untuk peta: cukup nama aset utama
            s["pipa"] = s["pipa"][:4]
            s["fas"] = s["fas"][:8]
        tulis_json(OUT / "petir.json", {
            "model": "Open-Meteo Forecast (CAPE, kode cuaca, hujan per jam)", "sumber": "Open-Meteo Forecast API",
            "jenis": "Prakiraan model cuaca — bukan deteksi sambaran petir",
            "catatan": "Menunjukkan kapan atmosfer mendukung badai petir menurut model, pada grid ±11 km. Petir dapat terjadi di luar sel/jam yang ditandai "
                       "dan sebaliknya. Untuk keputusan kerja di lapangan gunakan pengamatan langsung (aturan 30-30) dan peringatan dini BMKG.",
            "diambil": iso(now), "ukuran_sel": UKURAN, "sel_total": len(hasil),
            "ambang": {k: CFG[k] for k in ("cape_rendah", "cape_tinggi", "hujan_rendah_mm", "hujan_sedang_mm")},
            "produk": produk, "sel": ada, "status": {"ok": True, "pesan": None}, "durasi_detik": round(time.time() - t0)})
        log(f"Selesai: {len(ada)} sel berpotensi dari {len(hasil)} sel.")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        lama["status"] = {"ok": False, "pesan": str(e)[:200], "diperiksa": iso()}
        lama.setdefault("sel", [])
        lama.setdefault("produk", {})
        tulis_json(OUT / "petir.json", lama)


if __name__ == "__main__":
    main()
