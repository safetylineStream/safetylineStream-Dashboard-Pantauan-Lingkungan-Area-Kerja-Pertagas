"""Uji modul bahaya tambahan (peringatan dini BMKG, banjir sungai, potensi petir, GDACS) — tanpa jaringan."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import banjir_sungai as bj  # noqa: E402
import gdacs as gd  # noqa: E402
import peringatan_cuaca as pc  # noqa: E402
import petir_model as pm  # noqa: E402
import umum  # noqa: E402

WIB = timezone(timedelta(hours=7))
GARIS = [{"area": "A", "aset": "Pipa A1", "coords": [(100.0, 0.0), (100.5, 0.0)]},
         {"area": "B", "aset": "Pipa B1", "coords": [(110.0, -7.0), (110.0, -7.5)]}]
FAS = [{"area": "A", "aset": "Stasiun A", "lon": 100.1, "lat": 0.0}, {"area": "B", "aset": "Stasiun B", "lon": 110.0, "lat": -7.2}]

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Peringatan Dini</title>
<item><title>Hujan Lebat dan Petir di Riau</title><link>https://www.bmkg.go.id/alerts/nowcast/id/CRI20261113001_alert.xml</link>
<description>Berpotensi hujan lebat</description><pubDate>Fri, 13 Nov 2026 14:00:00 +0700</pubDate></item></channel></rss>"""


def cap(berakhir, poligon="-0.1,100.05 -0.1,100.2 0.1,100.2 0.1,100.05 -0.1,100.05"):
    return f"""<?xml version="1.0"?><alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>X1</identifier><sent>2026-11-13T14:00:00+07:00</sent>
<info><language>id-ID</language><event>Hujan Lebat dan Petir</event><urgency>Immediate</urgency><severity>Moderate</severity><certainty>Observed</certainty>
<effective>2026-11-13T14:00:00+07:00</effective><expires>{berakhir}</expires><senderName>BMKG</senderName><headline>Hujan Lebat dan Petir di Riau</headline>
<description>Hujan lebat disertai petir</description><web>https://nowcasting.bmkg.go.id/x.jpg</web>
<area><areaDesc>Riau</areaDesc><polygon>{poligon}</polygon></area></info></alert>"""


def test_parse_rss_dan_cap():
    it = pc.parse_rss(RSS)
    assert len(it) == 1 and it[0]["kode"] == "CRI20261113001"
    c = pc.parse_cap(cap("2026-11-13T17:00:00+07:00"))
    assert c["event"] == "Hujan Lebat dan Petir" and c["severity"] == "Moderate"
    assert c["poligon"][0][0] == (100.05, -0.1)  # CAP = lat,lon → (lon, lat)


def test_peringatan_ditumpangkan_ke_aset():
    aset = pc.Aset(GARIS, FAS, 25)
    now = datetime(2026, 11, 13, 15, 0, tzinfo=WIB)
    it = pc.parse_rss(RSS)[0]
    aktif, fitur = pc.proses([(it, pc.parse_cap(cap("2026-11-13T17:00:00+07:00")))], aset, now)
    assert len(aktif) == 1 and aktif[0]["kena"] and aktif[0]["provinsi"] == "Riau"
    a = aktif[0]["per_area"]["A"]
    assert 15 < a["pipa_km"] < 18            # 0,15° bujur di khatulistiwa ≈ 16,7 km
    assert a["fasilitas"] == ["Stasiun A"] and "B" not in aktif[0]["per_area"]
    assert fitur[0]["geometry"]["type"] == "Polygon"
    assert pc.ringkas_area(aktif, ["A", "B"])["A"]["jumlah"] == 1


def test_peringatan_kedaluwarsa_dan_jauh_dibuang():
    aset = pc.Aset(GARIS, FAS, 25)
    now = datetime(2026, 11, 13, 18, 0, tzinfo=WIB)
    it = pc.parse_rss(RSS)[0]
    assert pc.proses([(it, pc.parse_cap(cap("2026-11-13T17:00:00+07:00")))], aset, now) == ([], [])
    jauh = cap("2026-11-13T23:00:00+07:00", "5,120 5,121 6,121 6,120 5,120")
    assert pc.proses([(it, pc.parse_cap(jauh))], aset, now) == ([], [])
    # dekat (≤ 25 km) tetapi tidak mengenai aset → disimpan sebagai konteks, kena = False
    dekat = cap("2026-11-13T23:00:00+07:00", "0.05,100.0 0.05,100.5 0.15,100.5 0.15,100.0 0.05,100.0")
    aktif, _ = pc.proses([(it, pc.parse_cap(dekat))], aset, now)
    assert len(aktif) == 1 and aktif[0]["kena"] is False


def test_banjir_ambang_dan_level():
    nilai = [10.0] * 1000 + [50.0] * 20 + [100.0] * 5 + [200.0]
    h = bj.ringkas_riwayat(nilai)
    assert h["median"] == 10.0 and h["maks"] == 200.0 and h["p_rendah"] <= h["p_sedang"] <= h["maks"]
    assert bj.level_dari(250, h) == "tinggi" and bj.level_dari(200, h) == "tinggi"
    assert bj.level_dari(h["p_sedang"], h) == "sedang" and bj.level_dari(h["p_rendah"], h) == "rendah"
    assert bj.level_dari(11, h) is None and bj.level_dari(None, h) is None and bj.level_dari(999, {"kosong": True}) is None
    assert bj.ringkas_riwayat([1.0] * 100) is None  # riwayat terlalu pendek


def test_banjir_hanya_sel_sungai():
    sel = {"a": {"hist": {"median": 50, "p_rendah": 1, "p_sedang": 2, "maks": 3}}, "b": {"hist": {"median": 2, "p_rendah": 1, "p_sedang": 2, "maks": 3}},
           "c": {"hist": None}, "d": {"hist": {"kosong": True}}}
    assert list(bj.sel_sungai(sel)) == ["a"]


def test_banjir_nilai_prakiraan_mulai_hari_ini():
    d = {"time": ["2026-11-12", "2026-11-13", "2026-11-14", "2026-11-15"], "river_discharge": [900, 100, 300, None], "river_discharge_max": [900, 120, 420, 500]}
    assert bj.nilai_prakiraan(d, "2026-11-13") == (100, 300, "2026-11-14", 420)
    assert bj.nilai_prakiraan({"time": [], "river_discharge": []}, "2026-11-13") == (None, None, None, None)


def test_petir_level_per_jam():
    assert pm.level_jam(3000, 95, 5) == 3
    assert pm.level_jam(800, 95, 0) == 2          # model memprakirakan badai petir
    assert pm.level_jam(2600, 61, 2) == 2         # CAPE tinggi + hujan
    assert pm.level_jam(1500, 61, 1) == 1
    assert pm.level_jam(1500, 3, 0) == 0          # CAPE saja tanpa hujan tidak dihitung
    assert pm.level_jam(None, None, None) == 0


def test_petir_produk_dan_sel():
    now = datetime(2026, 11, 13, 10, 0, tzinfo=WIB)
    t = [f"2026-11-13T{h:02d}:00" for h in range(24)] + [f"2026-11-14T{h:02d}:00" for h in range(24)]
    cape = [0] * 48
    kode = [0] * 48
    hujan = [0] * 48
    cape[8], kode[8] = 3000, 95        # 08:00 sudah lewat → tidak dihitung
    cape[15], kode[15], hujan[15] = 3000, 95, 4
    kode[24 + 14] = 95
    pr, hari = pm.produk_sel({"time": t, "cape": cape, "weather_code": kode, "precipitation": hujan}, now)
    assert hari == {"hari_ini": "2026-11-13", "besok": "2026-11-14"}
    assert pr["hari_ini"]["level"] == "tinggi" and pr["hari_ini"]["jam"][8] == "-" and pr["hari_ini"]["jam"][15] == "3"
    assert pr["besok"]["level"] == "sedang" and pr["besok"]["jam"][14] == "2"
    sel = pm.sel_aset(GARIS, FAS)
    assert (100.05, 0.05) in sel and abs(sum(sum(s["pipa"].values()) for s in sel.values()) - 111.2) < 3
    assert pm.gabung_jam(["-01", "-20", "--3"]) == "-23"


def gd_fitur(jenis, eid, lon, lat, level="Orange", aktif="true", todate="2026-11-13T00:00:00"):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"eventtype": jenis, "eventid": eid, "eventname": f"{jenis}-{eid}", "alertlevel": level, "iscurrent": aktif,
                           "fromdate": "2026-11-10T00:00:00", "todate": todate, "country": "Indonesia",
                           "severitydata": {"severitytext": "uji"}, "url": {"report": "https://www.gdacs.org/r", "geometry": "https://x/geom"}}}


def test_gdacs_jarak_dan_skor():
    from shapely.geometry import Polygon
    aset = gd.Aset(GARIS, FAS)
    now = datetime(2026, 11, 13, 12, 0, tzinfo=WIB)
    feats = [gd_fitur("FL", 1, 100.2, 0.1),                     # ±11 km dari pipa A, Orange → P 3
             gd_fitur("VO", 2, 110.6, -7.2, level="Green"),     # ±66 km dari B, Green → P 1
             gd_fitur("FL", 3, 130.0, 5.0),                     # jauh → dibuang
             gd_fitur("EQ", 4, 100.2, 0.1),                     # jenis tidak dipantau
             gd_fitur("FL", 5, 100.2, 0.1, aktif="false", todate="2026-10-01T00:00:00"),  # sudah lama berakhir
             gd_fitur("TC", 6, 108.0, -9.0, level="Red")]
    zona = [("poly_orange_0", Polygon([(109.5, -8), (110.5, -8), (110.5, -7.3), (109.5, -7.3)])),
            ("poly_cones", Polygon([(109, -9), (111, -9), (111, -6.9), (109, -6.9)]))]
    kej, gj = gd.proses(feats, aset, ambil_geom=lambda url: zona, now=now)
    per = {k["id"]: k for k in kej}
    assert set(per) == {"FL1", "VO2", "TC6"}
    assert per["FL1"]["per_area"]["A"]["P"] == 3 and per["FL1"]["per_area"]["B"]["P"] == 0
    assert per["VO2"]["per_area"]["B"]["P"] == 1
    assert per["TC6"]["per_area"]["B"]["P"] == 4 and per["TC6"]["per_area"]["B"]["jarak_km"] == 0   # di zona angin Orange
    assert per["TC6"]["per_area"]["A"]["P"] == 0                                                   # > 500 km
    assert kej[0]["id"] == "TC6" and len(gj) == 2
    assert gd.nilai_titik("VO", 20, "Red") == 3 and gd.nilai_titik("VO", 80, "Red") == 2 and gd.nilai_titik("VO", 120, "Red") == 0


def test_utilitas():
    assert umum.pusat_sel(100.07, -0.02, 0.1) == (100.05, -0.05)
    assert umum.kuantil([1, 2, 3, 4, 5], 0.5) == 3 and umum.kuantil([], 0.5) is None
    assert abs(umum.panjang_km([(100.0, 0.0), (101.0, 0.0)]) - 111.19) < 0.1
