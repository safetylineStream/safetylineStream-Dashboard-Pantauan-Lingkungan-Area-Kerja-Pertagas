"""
Uji otomatis pipeline data dashboard.  Jalankan:  python -m pytest tests -q
(butuh: pip install -r requirements.txt pytest)

Uji ini TIDAK memanggil API asli — respons API ditiru supaya bisa menguji kondisi sukses,
gagal, kosong, dan rusak secara berulang. Data nyata tidak pernah ditimpa: setiap uji
memakai salinan repositori di folder sementara.
"""
import importlib
import json
import math
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import geo  # noqa: E402


# ------------------------------------------------------------------ geo.py
def test_haversine_nilai_acuan():
    # 1° bujur di ekuator = 2πR/360
    assert geo.haversine(0, 0, 0, 1) == pytest.approx(2 * math.pi * geo.R_BUMI_KM / 360, rel=1e-9)
    # simetris & nol
    assert geo.haversine(-6.2, 106.8, -7.25, 112.75) == pytest.approx(geo.haversine(-7.25, 112.75, -6.2, 106.8))
    assert geo.haversine(1.5, 101.4, 1.5, 101.4) == 0
    # Jakarta (Monas) – Surabaya (Tugu Pahlawan) ≈ 663 km (geodesik WGS84 ≈ 662,6 km)
    assert geo.haversine(-6.1754, 106.8272, -7.2458, 112.7378) == pytest.approx(662.6, abs=3)


def test_klasifikasi_radius_batas_inklusif():
    r = {"kritis": 1, "waspada": 3, "pantau": 5}
    assert geo.classify(0.0, r) == "kritis"
    assert geo.classify(1.0, r) == "kritis"
    assert geo.classify(1.0001, r) == "waspada"
    assert geo.classify(3.0, r) == "waspada"
    assert geo.classify(5.0, r) == "pantau"
    assert geo.classify(5.01, r) == "luar"
    assert geo.classify(None, r) == "luar"


def test_validasi_koordinat():
    assert geo.perbaiki_latlon(-3.1, 104.7) == (-3.1, 104.7, "ok")
    assert geo.perbaiki_latlon(104.7, -3.1) == (-3.1, 104.7, "tertukar")
    assert geo.perbaiki_latlon(0, 0)[2] == "invalid"
    assert geo.perbaiki_latlon(None, 104.7)[2] == "invalid"
    assert geo.perbaiki_latlon(float("nan"), 104.7)[2] == "invalid"
    assert geo.perbaiki_latlon(40.0, -74.0)[2] == "invalid"  # di luar Indonesia
    assert not geo.valid_ll(91, 0) and not geo.valid_ll(0, 181)


def test_tanggal_wib():
    assert geo.tanggal_wib("2026-10-04T18:28:00.000000Z") == "2026-10-05"
    assert geo.tanggal_wib("2026-10-04T16:59:00Z") == "2026-10-04"
    assert geo.tanggal_wib("") is None and geo.tanggal_wib("bukan tanggal") is None


def test_hotspot_berulang():
    hist = [{"lat": -3.1000, "lon": 104.7000, "waktu_utc": f"2026-10-0{d}T06:00:00Z"} for d in (1, 2, 3)]
    hist.append({"lat": -3.2, "lon": 104.7, "waktu_utc": "2026-09-30T06:00:00Z"})  # ±11 km — bukan lokasi sama
    idx = geo.IndeksTitik(hist)
    hs = {"lat": -3.1005, "lon": 104.7004, "waktu_utc": "2026-10-04T06:00:00Z"}  # ±70 m dari arsip
    assert geo.hari_berulang(hs, idx, 1.0) == 4
    jauh = {"lat": -3.13, "lon": 104.7, "waktu_utc": "2026-10-04T06:00:00Z"}  # ±3,3 km
    assert geo.hari_berulang(jauh, idx, 1.0) == 1


# ------------------------------------------------------------------ fetch_all.py (salinan repo)
@pytest.fixture()
def fa(tmp_path, monkeypatch):
    """Muat fetch_all.py dari salinan repositori supaya data asli tidak tersentuh."""
    dst = tmp_path / "repo"
    shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns(".git", "assets_raw", "lhasa_statis", "__pycache__", "tests"))
    monkeypatch.syspath_prepend(str(dst / "scripts"))
    for m in ("fetch_all", "geo", "gerakan_tanah"):
        sys.modules.pop(m, None)
    mod = importlib.import_module("fetch_all")
    assert mod.ROOT == dst
    yield mod
    for m in ("fetch_all", "geo", "gerakan_tanah"):
        sys.modules.pop(m, None)


def test_tsunami_hanya_bila_dinyatakan(fa):
    assert fa.tsunami_dari_potensi("Berpotensi tsunami") is True
    assert fa.tsunami_dari_potensi("BERPOTENSI TSUNAMI untuk diteruskan pada masyarakat") is True
    assert fa.tsunami_dari_potensi("Tidak berpotensi tsunami") is False
    assert fa.tsunami_dari_potensi("Tidak  berpotensi  Tsunami") is False
    assert fa.tsunami_dari_potensi("Gempa ini dirasakan untuk diteruskan pada masyarakat") is False
    assert fa.tsunami_dari_potensi("") is False and fa.tsunami_dari_potensi(None) is False


def _jarak_bruteforce(lat, lon, fc):
    """Jarak geodesik minimum (WGS84, pyproj) dari titik ke semua aset — pembanding independen."""
    from pyproj import Geod
    geod = Geod(ellps="WGS84")
    best = 1e9
    for f in fc["features"]:
        g = f["geometry"]
        if g["type"] == "Point":
            best = min(best, geod.inv(lon, lat, *g["coordinates"][:2])[2] / 1000)
            continue
        parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        for part in parts:
            for (x0, y0, *_), (x1, y1, *_) in zip(part[:-1], part[1:]):
                # proyeksi ke segmen di bidang lokal (ekuidistan) lalu ukur geodesik
                kx = math.cos(math.radians(lat))
                ax, ay, bx, by = (x0 - lon) * kx, y0 - lat, (x1 - lon) * kx, y1 - lat
                dx, dy = bx - ax, by - ay
                t = 0 if dx == dy == 0 else max(0, min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
                qx, qy = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
                best = min(best, geod.inv(lon, lat, qx, qy)[2] / 1000)
    return best


def test_jarak_aset_sesuai_geodesik(fa):
    """Jarak hotspot→aset terdekat dari dashboard dibandingkan perhitungan geodesik WGS84 independen."""
    A = fa.Assets()
    fc = json.loads((fa.DATA / "assets.geojson").read_text(encoding="utf-8"))
    titik = [(-3.10084, 104.70421), (1.67074, 101.47412), (-3.18725, 104.6255), (-6.37, 108.36),
             (-1.11, 116.75), (5.0, 97.3), (-7.6, 112.9), (0.85, 101.39), (-2.0, 103.0), (-6.0, 106.0)]
    for lat, lon in titik:
        d, _ = A.nearest(lat, lon)
        ref = _jarak_bruteforce(lat, lon, fc)
        assert abs(d - ref) <= max(0.02, 0.005 * ref), (lat, lon, d, ref)  # ≤ 20 m atau ≤ 0,5 %


def test_parse_gempa_koordinat(fa):
    A = fa.Assets()
    g = {"Coordinates": "-3.10,104.70", "Magnitude": "5.2", "Kedalaman": "10 km", "Potensi": "Tidak berpotensi tsunami",
         "Tanggal": "05 Okt 2026", "Jam": "10:00:00 WIB", "DateTime": "2026-10-05T03:00:00+00:00", "Wilayah": "uji"}
    r = fa.parse_gempa(g, A)
    assert r["status"] == "kritis" and r["jarak_km"] < 1 and r["tsunami"] is False
    r2 = fa.parse_gempa({**g, "Magnitude": "4.1"}, A)
    assert r2["status"] == "waspada"  # di bawah magnitudo_min_kritis diturunkan
    r3 = fa.parse_gempa({**g, "Coordinates": "", "Lintang": "3.10 LS", "Bujur": "104.70 BT"}, A)
    assert r3["lat"] == pytest.approx(-3.10) and r3["lon"] == pytest.approx(104.70)
    with pytest.raises(ValueError):
        fa.parse_gempa({**g, "Coordinates": "abc", "Lintang": "", "Bujur": ""}, A)
    r4 = fa.parse_gempa({**g, "Magnitude": ""}, A)
    assert r4["mag"] is None and r4["status"] == "waspada"


def test_dedupe_hotspot(fa):
    h = {"lat": -3.1, "lon": 104.7, "conf": "high", "sumber": "NASA-SNPP", "waktu_utc": "2026-10-04T18:28:00Z", "prov": "X"}
    out = fa.dedupe_hotspots([h, dict(h), {**h, "sumber": "FIRMS-VIIRS_SNPP_NRT"}, {**h, "waktu_utc": "2026-10-04T06:00:00Z"}])
    assert len(out) == 3  # identik dibuang; satelit/waktu berbeda dipertahankan


# ------------------------------------------------------------------ main(): sukses, gagal, kosong, rusak
class Resp:
    def __init__(self, data=None, text=None):
        self._d, self.text = data, text if text is not None else json.dumps(data)

    def json(self):
        if self._d is None:
            raise ValueError("JSON rusak")
        return self._d

    def raise_for_status(self):
        pass


GEMPA = {"Infogempa": {"gempa": {"Coordinates": "-6.50,106.00", "Magnitude": "5.0", "Kedalaman": "10 km",
                                 "Potensi": "Tidak berpotensi tsunami", "Tanggal": "05 Okt 2026", "Jam": "10:00:00 WIB",
                                 "DateTime": "2026-10-05T03:00:00+00:00", "Wilayah": "uji", "Dirasakan": ""}}}


def fake_get_factory(mode):
    def fake_get(url, params=None, headers=None, timeout=45, tries=2):
        if mode == "gagal":
            raise ConnectionError("server mati (uji)")
        if "sipongi" in url:
            if mode == "kosong":
                return Resp({"type": "FeatureCollection", "features": []})
            if mode == "rusak":
                return Resp(None, text="<html>error</html>")
            feats = [{"geometry": {"coordinates": [104.70421, -3.10084]},
                      "properties": {"confidence_level": "medium", "sumber": "NASA-SNPP",
                                     "date_hotspot_ori": "2026-10-04T18:28:00.000000Z", "date_hotspot": "x",
                                     "nama_provinsi": "Sumatera Selatan"}},
                     {"geometry": {"coordinates": [-3.10084, 104.70421]},  # lat/lon tertukar
                      "properties": {"confidence_level": "high", "sumber": "NASA-NOAA20",
                                     "date_hotspot_ori": "2026-10-04T19:00:00.000000Z", "nama_provinsi": "Sumatera Selatan"}},
                     {"geometry": {"coordinates": [0, 0]}, "properties": {}}]  # invalid
            return Resp({"features": feats})
        if "bmkg" in url:
            if mode == "rusak":
                return Resp({"tidak": "sesuai"})
            return Resp(GEMPA if "autogempa" in url else {"Infogempa": {"gempa": [GEMPA["Infogempa"]["gempa"]]}})
        if "open-meteo" in url:
            n = len(str(params["latitude"]).split(","))
            if mode == "rusak":
                return Resp([{}] * n)
            cur = {"time": "2026-10-05T16:30", "temperature_2m": 30.1, "relative_humidity_2m": 70, "precipitation": 0.0,
                   "weather_code": 3, "wind_speed_10m": 9.0, "wind_direction_10m": 90, "wind_gusts_10m": 15,
                   "apparent_temperature": 33, "pm2_5": 12.0, "pm10": 20.0, "us_aqi": 50}
            daily = {"time": ["2026-10-05"], "temperature_2m_max": [32], "temperature_2m_min": [24], "precipitation_sum": [3.2],
                     "precipitation_probability_max": [60], "weather_code": [61], "wind_speed_10m_max": [12]}
            return Resp([{"current": cur, "daily": daily}] * n)
        raise AssertionError(url)
    return fake_get


def jalankan(fa, monkeypatch, mode):
    monkeypatch.setenv("BAGIAN", "utama")
    monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
    monkeypatch.setattr(fa, "get", fake_get_factory(mode))
    monkeypatch.setattr(fa.time, "sleep", lambda s: None)
    try:
        fa.main()
    except SystemExit:
        pass
    return json.loads((fa.DATA / "latest.json").read_text(encoding="utf-8"))


def test_main_sukses(fa, monkeypatch):
    d = jalankan(fa, monkeypatch, "sukses")
    hs = d["hotspot"]
    assert hs["stale"] is False and hs["total_nasional"] == 2  # invalid dibuang
    assert d["kualitas_data"].get("hotspot_tertukar") == 1 and d["kualitas_data"].get("hotspot_invalid") == 1
    assert all("persisten" in h and "berulang_hari" in h for h in hs["dekat_aset"])
    assert hs["dekat_aset"][0]["status"] == "kritis"
    assert d["gempa"]["stale"] is False and d["gempa"]["diperbarui"]
    assert d["cuaca"][0]["jenis"] == "model" and d["cuaca_diperbarui"]
    for k in ("sipongi", "bmkg_gempa", "cuaca"):
        assert d["status_sumber"][k]["ok"] and d["status_sumber"][k]["terakhir_ok"] == d["status_sumber"][k]["waktu"]


def test_main_api_gagal_mempertahankan_data_lama(fa, monkeypatch):
    lama = json.loads((fa.DATA / "latest.json").read_text(encoding="utf-8"))
    d = jalankan(fa, monkeypatch, "gagal")
    # data lama dipertahankan dan ditandai stale; waktu data TIDAK diganti waktu sekarang
    assert d["hotspot"]["stale"] is True and d["hotspot"]["diperbarui"] == lama["hotspot"]["diperbarui"]
    assert d["hotspot"]["dekat_aset"] == lama["hotspot"]["dekat_aset"]
    assert d["gempa"]["stale"] is True and d["cuaca"] == lama["cuaca"]
    for k in ("sipongi", "bmkg_gempa", "cuaca"):
        st = d["status_sumber"][k]
        assert st["ok"] is False and st["terakhir_ok"] != st["waktu"]


def test_main_respons_kosong(fa, monkeypatch):
    d = jalankan(fa, monkeypatch, "kosong")
    assert d["hotspot"]["stale"] is False and d["hotspot"]["total_nasional"] == 0 and d["hotspot"]["dekat_aset"] == []


def test_main_respons_rusak(fa, monkeypatch):
    lama = json.loads((fa.DATA / "latest.json").read_text(encoding="utf-8"))
    d = jalankan(fa, monkeypatch, "rusak")
    assert d["status_sumber"]["sipongi"]["ok"] is False and d["hotspot"]["stale"] is True
    assert d["status_sumber"]["bmkg_gempa"]["ok"] is False
    assert d["status_sumber"]["cuaca"]["ok"] is False and d["cuaca"] == lama["cuaca"]
    json.dumps(d)  # tetap JSON valid


def test_periode_bulan_pvmbg():
    import gerakan_tanah as gt
    assert gt.periode_bulan("Prakiraan Gerakan Tanah Bulan September 2026") == "2026-09"
    assert gt.periode_bulan("prakiraan oktober 2026") == "2026-10"
    assert gt.periode_bulan("tanpa bulan") is None


# ------------------------------------------------------------------ Portal MBG (prakiraan gerakan tanah)
def _zip_shapefile(records):
    """Buat shapefile ZIP tiruan seperti unduhan Portal MBG: records = [(coords, unsur, zona_perki)]."""
    import io, zipfile, shapefile
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGONZ)
    for f in ("OBJECTID", "Unsur", "Keterangan", "Tahun", "Wilayah", "Zona_Perki"):
        w.field(f, "C", 120)
    for i, (coords, unsur, perki) in enumerate(records):
        w.polyz([[(x, y, 0) for x, y in coords]])
        w.record(str(i), unsur, "uji", "2016", "Uji", perki)
    w.close()
    zb = io.BytesIO()
    with zipfile.ZipFile(zb, "w") as z:
        for ext, b in ((".shp", shp), (".shx", shx), (".dbf", dbf)):
            z.writestr("PROVINSI UJI" + ext, b.getvalue())
        z.writestr("PROVINSI UJI.prj", 'GEOGCS["GCS_WGS_1984"]')
    return zb.getvalue()


def test_portalmbg_level_dan_potong_koridor():
    import portalmbg
    from shapely.geometry import box
    from shapely.strtree import STRtree
    assert portalmbg.level_prakiraan("Berpotensi Banjir Bandang/Aliran Bahan Rombakan") == "bandang"
    assert portalmbg.level_prakiraan("Sangat  Rendah") == "sangat_rendah"
    assert portalmbg.level_prakiraan("Danau") is None and portalmbg.level_kerentanan("Danau/Situ") is None
    assert portalmbg.nama_periode("2026-10") == "Prakiraan Gerakan Tanah Bulan Oktober 2026"
    assert portalmbg.bulan_sebelumnya(2026, 1) == (2025, 12)
    sq = lambda x, y: [(x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1), (x, y)]
    data = _zip_shapefile([(sq(0, 0), "Tinggi", "Tinggi"), (sq(5, 5), "Rendah", "Rendah"),
                           (sq(0, 0), "Danau/Situ", "Danau"), (sq(0.5, 0), "Alur Aliran Bahan Rombakan",
                                                               "Berpotensi Banjir Bandang/Aliran Bahan Rombakan")])
    koridor = [box(0.25, 0.25, 0.75, 0.75)]
    prak, ker, n_all, n_in = portalmbg.proses_zip(data, STRtree(koridor), koridor, simplify=0)
    assert n_all == 4 and n_in == 2  # di luar koridor & danau dibuang
    assert sorted(f["level"] for f in prak) == ["bandang", "tinggi"]
    assert sorted(f["level_kerentanan"] for f in ker) == ["bandang", "tinggi"]
    from shapely.geometry import shape
    assert all(shape(f["geometry"]).within(box(0.25, 0.25, 0.75, 0.75).buffer(1e-9)) for f in prak)


def test_portalmbg_run_fallback_bulan_dan_cache(tmp_path, monkeypatch):
    import portalmbg
    from datetime import datetime, timezone, timedelta
    from shapely.geometry import box
    sq = [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]
    zipdata = _zip_shapefile([(sq, "Menengah", "Tinggi")])
    calls = []

    class R:
        def __init__(self, code, content=b"", lm="Tue, 29 Sep 2026 13:56:35 GMT"):
            self.status_code, self.content, self.headers = code, content, {"Last-Modified": lm}

    def head(url, **kw):
        calls.append(("HEAD", url))
        return R(404) if "/2026/10/" in url else R(200)

    def get(url, **kw):
        calls.append(("GET", url))
        if "/2026/10/" in url:
            raise RuntimeError("404 Client Error")
        return R(200, zipdata)
    import requests
    monkeypatch.setattr(requests, "head", head)
    WIB = timezone(timedelta(hours=7))
    ctx = {"log": lambda *a: None, "NOW": datetime(2026, 10, 2, 5, tzinfo=WIB), "DATA": tmp_path, "get": get,
           "CFG": {"gerakan_tanah": {"portalmbg": {"url": "https://x/{tahun}/{bulan}/{provinsi}", "provinsi": ["16"]}}}}
    h = portalmbg.run(ctx, box(0.2, 0.2, 0.8, 0.8), "sig1")
    assert h["periode_bulan"] == "2026-09" and h["selesai"] == 1 and len(h["prakiraan"]) == 1 and h["prakiraan"][0][1] == "tinggi"
    assert h["zkgt"][0][1] == "menengah"
    n_get = sum(1 for c in calls if c[0] == "GET")
    h2 = portalmbg.run(ctx, box(0.2, 0.2, 0.8, 0.8), "sig1")  # run kedua: terbit sama → tanpa unduh ulang
    assert sum(1 for c in calls if c[0] == "GET") == n_get and h2["selesai"] == 1
