"""Uji model gerakan tanah terpadu (PVMBG + LHASA + tutupan lahan) — tanpa jaringan."""
import sys
from pathlib import Path

import numpy as np
from shapely.geometry import LineString, box

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import gerakan_tanah_terpadu as mgt  # noqa: E402


def test_kerentanan_bobot_sni_dan_batas_lereng():
    # (3·4 + 3·3 + 2·4) / 8 = 3,625 → 4 ; batas lereng L + 1 = 4
    assert mgt.skor_kerentanan(4, 3, 4)[0] == 4
    # lereng curam, geologi tinggi, lahan terbuka → 5
    assert mgt.skor_kerentanan(5, 5, 5)[0] == 5
    # zona PVMBG tinggi tetapi lahan datar → dibatasi maks. 2
    assert mgt.skor_kerentanan(5, 1, 4)[0] == 2
    # hutan menurunkan kerentanan dibanding lahan pertanian pada lereng & geologi yang sama
    assert mgt.skor_kerentanan(4, 4, 1)[0] < mgt.skor_kerentanan(4, 4, 4)[0] or mgt.skor_kerentanan(4, 4, 1)[1] < mgt.skor_kerentanan(4, 4, 4)[1]
    # komponen kosong → bobot dinormalisasi (hanya lereng)
    assert mgt.skor_kerentanan(None, 4, None) == (4, 4.0)
    # pembulatan setengah ke atas, bukan banker's rounding: (3·2+3·3+2·3)/8 = 2,625 → 3 ; 2,5 → 3
    assert mgt.skor_kerentanan(None, 3, 2, {"bobot": {"geologi": 3, "lereng": 1, "lahan": 1}})[0] == 3


def test_pemicu_hujan():
    assert mgt.pemicu(0.5) == 0
    assert mgt.pemicu(0.75) == 1
    assert mgt.pemicu(1.0) == 2
    assert mgt.pemicu(1.6) == 3
    assert mgt.pemicu(None) == 0
    assert mgt.pemicu(float("nan")) == 0


def test_matriks_konsisten_dengan_lhasa():
    # LHASA 1.1: hujan > ambang & kerentanan 3–4 → sedang; 5 → tinggi
    assert mgt.tingkat_harian(3, 2) == "sedang"
    assert mgt.tingkat_harian(4, 2) == "sedang"
    assert mgt.tingkat_harian(5, 2) == "tinggi"
    # mendekati ambang (tambahan Pertagas) → rendah
    assert mgt.tingkat_harian(3, 1) == "rendah"
    # tanpa pemicu hujan → tidak ada indikasi harian
    assert all(mgt.tingkat_harian(s, 0) is None for s in range(1, 6))
    # kerentanan rendah hanya dinilai saat hujan jauh melampaui ambang
    assert mgt.tingkat_harian(2, 2) is None and mgt.tingkat_harian(2, 3) == "rendah"


def test_lantai_pvmbg_dan_tingkat_akhir():
    # zona tinggi PVMBG tanpa pemicu hujan = "zona" (potensi bulanan, belum terpicu)
    assert mgt.tingkat_akhir(3, 0, "tinggi", 3) == "zona"
    # zona tinggi + hujan melampaui ambang → minimal tinggi
    assert mgt.tingkat_akhir(2, 2, "tinggi", 3) == "tinggi"
    # model harian tinggi DAN zona tinggi PVMBG di titik yang sama → sangat tinggi
    assert mgt.tingkat_akhir(5, 2, "tinggi", 5) == "sangat_tinggi"
    # zona menengah + mendekati ambang → rendah
    assert mgt.tingkat_akhir(2, 1, "menengah", 2) == "rendah"
    # lahan datar di zona tinggi: dibatasi rendah walau hujan melampaui ambang
    assert mgt.tingkat_akhir(2, 2, "tinggi", 1) == "rendah"
    # lahan datar di zona menengah dengan hujan di bawah ambang: tidak ada (cegah alarm palsu di rawa/dataran)
    assert mgt.tingkat_akhir(1, 1, "menengah", 1) is None
    # lahan datar di zona tinggi dengan hujan di bawah ambang: hanya "zona"
    assert mgt.tingkat_akhir(1, 1, "tinggi", 1) == "zona"
    # alur banjir bandang tidak dibatasi lereng
    assert mgt.tingkat_akhir(2, 2, "bandang", 1) == "tinggi"
    # lahan datar di luar zona PVMBG tidak dinilai model harian
    assert mgt.tingkat_akhir(2, 3, None, 1) is None


def _aset():
    # dua ruas: A di lereng (kelas 4), B datar (kelas 1); satu fasilitas di ruas A
    pa = [(110.0 + i * 0.002, -7.0) for i in range(6)]
    pb = [(111.0 + i * 0.002, -7.0) for i in range(6)]
    seg = lambda pts: [0.22] * (len(pts) - 1)  # noqa: E731
    lines = [{"area": "X", "aset": "Pipa A", "pts": pa, "seg_km": seg(pa)},
             {"area": "X", "aset": "Pipa B", "pts": pb, "seg_km": seg(pb)}]
    facs = [{"area": "X", "aset": "Stasiun A", "lon": 110.004, "lat": -7.0}]
    kelas = np.array([4] * 6 + [1] * 6 + [4])
    pvmbg = ([box(109.99, -7.01, 110.005, -6.99)], ["tinggi"], ["tinggi"], "Prakiraan Gerakan Tanah Bulan November 2026")
    lahan = ([LineString(pa), LineString(pb)], [40, 10])
    return lines, facs, kelas, pvmbg, lahan


def test_hitung_ujung_ke_ujung():
    lines, facs, kelas, pvmbg, lahan = _aset()
    rasio = {"hari_ini": np.array([1.6] * 13), "besok": np.array([0.3] * 13)}
    out, fitur = mgt.hitung({"X": {}}, lines, facs, kelas, rasio, {"hari_ini": "2026-11-27", "besok": "2026-11-28"},
                            pvmbg=pvmbg, lahan=lahan, now_iso="2026-11-27T11:00+07:00")
    a = out["produk"]["hari_ini"]["per_area"]["X"]
    # ruas A: lereng 4 + zona tinggi + pertanian + hujan jauh > ambang → model harian tinggi & zona tinggi → sangat tinggi
    assert a["terburuk"] == "sangat_tinggi"
    assert a["pipa_km"]["sangat_tinggi"] > 0
    # ruas B datar & hutan → tidak dinilai
    assert all(r["aset"] == "Pipa A" for r in a["ruas"])
    # fasilitas di dalam zona tinggi ikut dinilai, lengkap dengan koordinat & penjelasan
    f = a["fasilitas"][0]
    assert f["aset"] == "Stasiun A" and f["level"] == "sangat_tinggi" and f["lat"] == -7.0 and "lereng" in f["faktor"]
    # ruas punya koordinat & garis untuk dituju di peta
    r = a["ruas"][0]
    assert r["lat"] is not None and len(r["garis"]) >= 2 and r["G"] == "tinggi" and r["T"] == 40
    # besok tanpa hujan: hanya "zona" PVMBG yang tersisa
    b = out["produk"]["besok"]["per_area"]["X"]
    assert b["terburuk"] == "zona"
    assert out["cakupan"]["lahan"] == 1.0 and 0 < out["cakupan"]["pvmbg"] < 1
    assert fitur and all(f["properties"]["produk"] in ("hari_ini", "besok") for f in fitur)


def test_tanpa_data_pvmbg_dan_lahan_tetap_jalan():
    lines, facs, kelas, _, _ = _aset()
    rasio = {"hari_ini": np.array([1.2] * 13)}
    out, _ = mgt.hitung({"X": {}}, lines, facs, kelas, rasio, pvmbg=([], [], [], None), lahan=([], []))
    a = out["produk"]["hari_ini"]["per_area"]["X"]
    # hanya lereng (kelas 4) + hujan melampaui ambang → sedang
    assert a["terburuk"] == "sedang"
    assert out["cakupan"] == {"pvmbg": 0.0, "lahan": 0.0}
