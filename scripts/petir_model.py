"""
petir_model.py — Potensi badai petir (prakiraan model) KHUSUS fasilitas kunci:
stasiun kompresor gas, stasiun metering/regulasi, dan booster/pompa.

Mengapa hanya fasilitas itu?  Pipa tertanam praktis tidak terdampak sambaran petir, sedangkan stasiun
kompresor/metering/booster punya peralatan di atas tanah, instrumen, panel listrik dan pekerjaan luar ruang
(hot work, kerja di ketinggian, pengangkatan) yang perlu dijadwalkan di luar jam rawan petir.

Sumber : Open-Meteo Forecast API — CAPE (energi konvektif), kode cuaca WMO (95/96/99 = badai petir),
         curah hujan per jam. PRAKIRAAN MODEL — BUKAN deteksi sambaran petir.
Spasial: satu deret per fasilitas (sel 0,1° yang memuat fasilitas; beberapa fasilitas berdekatan berbagi sel).

Jenis fasilitas ditentukan dari nama aset (config/monitoring.json → petir_model.fasilitas):
  kompresor : SKG, kompresor/compressor, CPP
  metering  : metering, meter, SM, SMG, MRS, PRS, ORF, stasiun/station (penerima & distribusi gas)
  booster   : booster, pompa/pump
  dikecualikan: valve (LBCV/SBV/MLBV), manifold, jumper, tie-in, pig launcher/receiver, kantor, dsb.
  tambah/kecuali: daftar nama persis untuk koreksi manual.

Tingkat per jam (ambang di config):
  Tinggi = model memprakirakan badai petir (kode 95/96/99) DAN CAPE ≥ cape_tinggi
  Sedang = model memprakirakan badai petir, ATAU CAPE ≥ cape_tinggi dengan hujan ≥ hujan_sedang_mm/jam
  Rendah = CAPE ≥ cape_rendah dengan hujan ≥ hujan_rendah_mm/jam

Keluaran: data/petir_model/petir.json
"""
import re
import sys
import time
import traceback
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from umum import DATA, ambil, baca_json, cfg, iso, log, muat_aset, pusat_sel, sekarang, tulis_json  # noqa: E402

OUT = DATA / "petir_model"
CFG = cfg("petir_model", {
    "url": "https://api.open-meteo.com/v1/forecast",
    "cape_rendah": 1000,
    "cape_tinggi": 2500,
    "hujan_rendah_mm": 0.5,
    "hujan_sedang_mm": 1.0,
    "fasilitas": {},
})
POLA = {
    "kompresor": r"\bSKG\b|kompres|compress|\bCPP\b",
    "booster": r"booster|\bpump\b|pompa",
    "metering": r"meter|\bSMG?\b|\bMRS\b|\bPRS\b|\bORF\b|stasiun|station",
    "kecuali": r"LBCV|LBVC|\bSBV\b|MLBV|valve|manifold|jumper|tie[- ]?in|\bpig\b|launcher|receiver|kantor|future|acces|"
               r"rumah valve|box valve",
}
JENIS_NAMA = {"kompresor": "Stasiun kompresor", "metering": "Stasiun metering/regulasi", "booster": "Booster/pompa"}
UKURAN = 0.1
LEVELS = ["tinggi", "sedang", "rendah"]
NAMA = {0: None, 1: "rendah", 2: "sedang", 3: "tinggi"}
BADAI = {95, 96, 99}


def jenis_fasilitas(nama, c=None):
    """Nama aset → 'kompresor' | 'booster' | 'metering' | None (tidak dipantau)."""
    c = c if c is not None else (CFG.get("fasilitas") or {})
    nm = str(nama or "")
    if nm in (c.get("kecuali") or []):
        return None
    tambah = c.get("tambah") or {}
    if isinstance(tambah, dict) and nm in tambah:
        return tambah[nm]
    if isinstance(tambah, list) and nm in tambah:
        return "metering"
    pola = {**POLA, **(c.get("pola") or {})}
    if re.search(pola["kecuali"], nm, re.I):
        return None
    for j in ("kompresor", "booster", "metering"):
        if re.search(pola[j], nm, re.I):
            return j
    return None


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


def jam_rawan(jam, minimal=2):
    """'0000222330…' → '13–15, 17 WIB' (jam dengan tingkat ≥ minimal)."""
    idx = [h for h, x in enumerate(jam) if x != "-" and int(x) >= minimal]
    if not idx:
        return None
    rentang, a = [], idx[0]
    for p, q in zip(idx, idx[1:] + [None]):
        if q != p + 1:
            rentang.append(f"{a:02d}–{p + 1:02d}")
            a = q
    return ", ".join(rentang) + " WIB"


def pilih_fasilitas(fas, c=None):
    out = []
    for f in fas:
        j = jenis_fasilitas(f["aset"], c)
        if j:
            out.append({**f, "jenis": j})
    return out


def ringkas_area(hasil, areas, produk):
    per = {}
    for a in areas:
        mine = [f for f in hasil if f["area"] == a]
        if not mine:
            continue
        by = {lv: [f["aset"] for f in mine if f[produk]["level"] == lv] for lv in LEVELS}
        capes = [f[produk]["cape_maks"] for f in mine if f[produk]["cape_maks"] is not None]
        jam = gabung_jam([f[produk]["jam"] for f in mine])
        per[a] = {"n_fasilitas": len(mine), "fasilitas": {k: v[:15] for k, v in by.items() if v},
                  "n": {k: len(v) for k, v in by.items()}, "terburuk": next((lv for lv in LEVELS if by[lv]), None),
                  "jam": jam, "jam_rawan": jam_rawan(jam), "cape_maks": max(capes) if capes else None,
                  "jenis": {j: sum(1 for f in mine if f["jenis"] == j) for j in JENIS_NAMA}}
    return per


def main():
    t0 = time.time()
    lama = baca_json(OUT / "petir.json", {}) or {}
    try:
        areas, _garis, fas = muat_aset()
        pilih = pilih_fasilitas(fas)
        sel = {}
        for f in pilih:
            sel.setdefault(pusat_sel(f["lon"], f["lat"], UKURAN), []).append(f)
        kunci = list(sel)
        now = sekarang()
        log(f"Potensi petir: {len(pilih)} fasilitas kunci ({', '.join(f'{JENIS_NAMA[j]} {sum(1 for f in pilih if f['jenis'] == j)}' for j in JENIS_NAMA)}) "
            f"dalam {len(kunci)} sel 0,1° dari Open-Meteo ...")
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
                for f in sel[k]:
                    hasil.append({"area": f["area"], "aset": f["aset"], "jenis": f["jenis"], "lat": round(f["lat"], 5), "lon": round(f["lon"], 5),
                                  "sel": [k[0], k[1]], **pr})
            time.sleep(1)
        if kunci and kosong / len(kunci) > 0.2:
            raise RuntimeError(f"{kosong} dari {len(kunci)} sel tanpa nilai CAPE — hasil tidak dapat diandalkan")
        produk = {p: {"tanggal": hari.get(p), "per_area": ringkas_area(hasil, areas, p)} for p in ("hari_ini", "besok")}
        hasil.sort(key=lambda f: (f["area"], {"tinggi": 0, "sedang": 1, "rendah": 2, None: 3}[f["hari_ini"]["level"]], f["aset"]))
        tulis_json(OUT / "petir.json", {
            "model": "Open-Meteo Forecast (CAPE, kode cuaca, hujan per jam)", "sumber": "Open-Meteo Forecast API",
            "jenis": "Prakiraan model cuaca — bukan deteksi sambaran petir",
            "cakupan": "Hanya stasiun kompresor gas, stasiun metering/regulasi dan booster/pompa",
            "catatan": "Menunjukkan jam ketika atmosfer mendukung badai petir menurut model di sekitar fasilitas (grid ±11 km). Petir dapat terjadi "
                       "di luar jam yang ditandai dan sebaliknya. Untuk keputusan kerja di lapangan gunakan pengamatan langsung (aturan 30-30) dan "
                       "peringatan dini BMKG.",
            "diambil": iso(now), "ukuran_sel": UKURAN, "n_fasilitas": len(hasil), "sel_total": len(kunci),
            "jenis_nama": JENIS_NAMA,
            "ambang": {k: CFG[k] for k in ("cape_rendah", "cape_tinggi", "hujan_rendah_mm", "hujan_sedang_mm")},
            "produk": produk, "fasilitas": hasil, "status": {"ok": True, "pesan": None}, "durasi_detik": round(time.time() - t0)})
        log(f"Selesai: {sum(1 for f in hasil if f['hari_ini']['level'])} fasilitas berpotensi hari ini dari {len(hasil)}.")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        lama["status"] = {"ok": False, "pesan": str(e)[:200], "diperiksa": iso()}
        lama.setdefault("fasilitas", [])
        lama.setdefault("produk", {})
        tulis_json(OUT / "petir.json", lama)


if __name__ == "__main__":
    main()
