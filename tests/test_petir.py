"""Uji modul petir Xweather (tanpa jaringan): cakupan lingkaran kueri, parsing, jarak ke aset,
klasifikasi, all-clear, kegagalan sumber, penyamaran kredensial, dan penghematan commit."""
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import petir  # noqa: E402
from geo import haversine  # noqa: E402

NOW = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
ENV = {"XWEATHER_CLIENT_ID": "IDRAHASIA123", "XWEATHER_CLIENT_SECRET": "SECRETRAHASIA456", "PETIR_PUTARAN": "1"}
KM_LAT = 1 / 110.57  # derajat lintang per km di sekitar 7° LS


def aset_uji():
    return {"type": "FeatureCollection", "areas": {"OEJA": {"nama": "Uji Jatim"}, "OKA": {"nama": "Uji Kaltim"}}, "features": [
        {"type": "Feature", "properties": {"kind": "pipa", "area": "OEJA", "name": "Pipa uji"},
         "geometry": {"type": "LineString", "coordinates": [[113.0, -7.0], [114.0, -7.0]]}},
        {"type": "Feature", "properties": {"kind": "fasilitas", "area": "OEJA", "name": "SKG uji"},
         "geometry": {"type": "Point", "coordinates": [113.5, -7.0]}},
        {"type": "Feature", "properties": {"kind": "fasilitas", "area": "OKA", "name": "Terminal uji"},
         "geometry": {"type": "Point", "coordinates": [117.0, -1.0]}}]}


def strike(i, lat, lon, menit_lalu=1, tipe="cg", amp=-25000):
    t = NOW - timedelta(minutes=menit_lalu)
    return {"id": f"s{i}", "loc": {"lat": lat, "long": lon},
            "ob": {"timestamp": int(t.timestamp()), "dateTimeISO": t.isoformat(), "age": menit_lalu * 60,
                   "pulse": {"type": tipe, "peakamp": amp, "numSensors": 12}}}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    shutil.copytree(REPO / "config", tmp_path / "config")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "assets.geojson").write_text(json.dumps(aset_uji()))
    monkeypatch.setattr(petir, "ROOT", tmp_path)
    monkeypatch.setattr(petir, "DATA", tmp_path / "data" / "petir")
    monkeypatch.setattr(petir, "OUT", tmp_path / "data" / "petir" / "latest.json")
    monkeypatch.setattr(petir, "ARSIP", tmp_path / "data" / "petir" / "arsip")
    monkeypatch.setattr(petir, "ASET", tmp_path / "data" / "assets.geojson")
    return tmp_path


def fake_get(strikes, calls=None, tolak_filter=False, gagal=None):
    def get(url, params):
        if calls is not None:
            calls.append((url, dict(params)))
        if gagal:
            return 401, {"success": False, "error": {"code": "invalid_client", "description": gagal}, "response": []}
        if tolak_filter and "filter" in params:
            return 400, {"success": False, "error": {"code": "invalid_filter", "description": "bad filter"}}
        lat, lon = map(float, params["p"].split(",")[:2])
        r = float(str(params.get("radius", "100km")).replace("km", ""))
        hits = [s for s in strikes if haversine(lat, lon, s["loc"]["lat"], s["loc"]["long"]) <= r]
        if params.get("filter") == "cg":
            hits = [s for s in hits if s["ob"]["pulse"]["type"] == "cg"]
        if not hits:
            return 200, {"success": True, "error": {"code": "warn_no_data", "description": "No data"}, "response": []}
        return 200, {"success": True, "error": None, "response": hits}
    return get


def test_lingkaran_menutup_semua_aset_nyata():
    fc = json.loads((REPO / "data" / "assets.geojson").read_text())
    L = petir.lingkaran_cakupan(fc, 100, 30, 2)
    assert 0 < len(L) <= 40
    for s in petir.titik_sampel(fc, 2):
        # setiap titik aset ≤ 100 − 30 − 1 km dari pusat lingkaran → buffer 30 km tercakup penuh
        assert min(haversine(s["lat"], s["lon"], c["lat"], c["lon"]) for c in L) <= 69.05


def test_parse_sambaran():
    s = petir.parse_sambaran(strike(1, -7.0, 113.5))
    assert s["tipe"] == "CG" and s["arus_ka"] == -25.0 and s["sensor"] == 12
    assert s["waktu_utc"] == (NOW - timedelta(minutes=1)).isoformat(timespec="seconds")
    assert petir.parse_sambaran({"id": "x", "loc": {"lat": 0, "long": 0}, "ob": {"timestamp": 1}}) is None
    assert petir.parse_sambaran({"id": "x", "loc": {"lat": 95, "long": 113}, "ob": {"timestamp": 1}}) is None
    assert petir.parse_sambaran({"id": "x", "loc": {"lat": -7, "long": 113}, "ob": {"timestamp": "bukan"}}) is None


def test_tanpa_kredensial_tidak_mengubah_apa_pun(repo):
    assert petir.main(get=fake_get([]), now_fn=lambda: NOW, env={}) == 0
    assert not (repo / "data" / "petir").exists()


def test_klasifikasi_jarak_ringkasan_dan_arsip(repo):
    strikes = [strike(1, -7.0 + 0.5 * KM_LAT, 113.5),          # ±0,5 km dari pipa & SKG → kritis + dekat aset
               strike(2, -7.0 + 12 * KM_LAT, 113.2),           # ±12 km → waspada
               strike(3, -7.0 - 25 * KM_LAT, 113.8),           # ±25 km → pantau
               strike(4, -7.0 - 60 * KM_LAT, 113.8),           # ±60 km → di luar, tidak disimpan
               strike(5, -7.0 + 2 * KM_LAT, 113.6, tipe="ic"),  # IC → diabaikan (filter cg)
               strike(6, -1.0 + 3 * KM_LAT, 117.0, menit_lalu=45)]  # OKA, 45 menit lalu → tersimpan, status wilayah sudah aman
    calls = []
    assert petir.main(get=fake_get(strikes, calls), now_fn=lambda: NOW, env=ENV) == 0
    out = json.loads((repo / "data" / "petir" / "latest.json").read_text())
    by = {s["id"]: s for s in out["sambaran"]}
    assert set(by) == {"s1", "s2", "s3", "s6"}
    assert by["s1"]["status"] == "kritis" and by["s1"]["dekat_aset"] and abs(by["s1"]["jarak_km"] - 0.5) < 0.05
    assert by["s1"]["fasilitas"] == "SKG uji" and by["s1"]["area"] == "OEJA"
    assert by["s2"]["status"] == "waspada" and by["s3"]["status"] == "pantau" and by["s6"]["area"] == "OKA"
    assert out["area"]["OEJA"]["status"] == "kritis" and out["area"]["OEJA"]["jumlah_30m"] == 3
    assert out["area"]["OEJA"]["aman_setelah_utc"] == (NOW - timedelta(minutes=1) + timedelta(minutes=30)).isoformat(timespec="seconds")
    assert out["area"]["OKA"]["status"] is None and out["area"]["OKA"]["jumlah_simpan"] == 1
    assert out["jumlah"]["kueri"] == len(calls) == len(out["lingkaran"]) and out["mode"] == "standard"
    assert all(c[1]["filter"] == "cg" and c[1]["radius"] == "100km" and c[0].endswith("/closest") for c in calls)
    teks = (repo / "data" / "petir" / "latest.json").read_text()
    assert "RAHASIA" not in teks
    arsip = json.loads((repo / "data" / "petir" / "arsip" / "2026-11-02.json").read_text())
    assert arsip["jumlah"]["total"] == 4 and arsip["jumlah"]["kritis"] == 2  # s1 + s6 (3 km dari terminal OKA)


def test_buffer_lama_dan_kedaluwarsa(repo):
    petir.main(get=fake_get([strike(1, -7.0 + 5 * KM_LAT, 113.5)]), now_fn=lambda: NOW, env=ENV)
    # 50 menit kemudian: tidak ada sambaran baru → sambaran lama masih disimpan, status wilayah sudah aman
    t2 = NOW + timedelta(minutes=50)
    petir.main(get=fake_get([]), now_fn=lambda: t2, env=ENV)
    out = json.loads((repo / "data" / "petir" / "latest.json").read_text())
    assert [s["id"] for s in out["sambaran"]] == ["s1"] and out["area"]["OEJA"]["status"] is None
    # 2 jam kemudian: melewati simpan_menit (90) → dibuang
    petir.main(get=fake_get([]), now_fn=lambda: NOW + timedelta(hours=2), env=ENV)
    out = json.loads((repo / "data" / "petir" / "latest.json").read_text())
    assert out["sambaran"] == []


def test_tidak_tulis_ulang_bila_tidak_berubah(repo):
    petir.main(get=fake_get([]), now_fn=lambda: NOW, env=ENV)
    f = repo / "data" / "petir" / "latest.json"
    t0 = json.loads(f.read_text())["diperbarui"]
    petir.main(get=fake_get([]), now_fn=lambda: NOW + timedelta(minutes=10), env=ENV)
    assert json.loads(f.read_text())["diperbarui"] == t0           # tidak berubah → tidak ada commit
    petir.main(get=fake_get([]), now_fn=lambda: NOW + timedelta(minutes=31), env=ENV)
    assert json.loads(f.read_text())["diperbarui"] != t0           # heartbeat 30 menit


def test_gagal_api_data_lama_dipertahankan_dan_kunci_disamarkan(repo):
    petir.main(get=fake_get([strike(1, -7.0 + 5 * KM_LAT, 113.5)]), now_fn=lambda: NOW, env=ENV)
    rc = petir.main(get=fake_get([], gagal="client IDRAHASIA123 tidak valid"), now_fn=lambda: NOW + timedelta(minutes=5), env=ENV)
    assert rc == 1
    out = json.loads((repo / "data" / "petir" / "latest.json").read_text())
    assert out["status"]["ok"] is False and "invalid_client" in out["status"]["pesan"]
    assert "IDRAHASIA123" not in json.dumps(out) and out["status"]["terakhir_ok"] == NOW.isoformat(timespec="seconds")
    assert [s["id"] for s in out["sambaran"]] == ["s1"]


def test_filter_ditolak_server_dicoba_ulang_tanpa_filter(repo):
    calls = []
    strikes = [strike(1, -7.0 + 3 * KM_LAT, 113.5), strike(2, -7.0 + 2 * KM_LAT, 113.6, tipe="ic")]
    assert petir.main(get=fake_get(strikes, calls, tolak_filter=True), now_fn=lambda: NOW, env=ENV) == 0
    out = json.loads((repo / "data" / "petir" / "latest.json").read_text())
    assert [s["id"] for s in out["sambaran"]] == ["s1"]        # IC tetap disaring lokal
    assert any("filter" not in c[1] for c in calls)


def test_mode_enterprise_memakai_within_per_wilayah(repo):
    cfg = json.loads((repo / "config" / "monitoring.json").read_text())
    cfg["petir"]["mode"] = "enterprise"
    (repo / "config" / "monitoring.json").write_text(json.dumps(cfg))
    calls = []

    def get(url, params):
        calls.append((url, dict(params)))
        s, w, n, e = map(float, params["p"].split(","))
        hits = [x for x in [strike(1, -7.0 + 1 * KM_LAT, 113.5)] if s <= x["loc"]["lat"] <= n and w <= x["loc"]["long"] <= e]
        return 200, {"success": True, "response": hits}
    assert petir.main(get=get, now_fn=lambda: NOW, env=ENV) == 0
    out = json.loads((repo / "data" / "petir" / "latest.json").read_text())
    assert len(calls) == 2 and all(u.endswith("/within") and p["from"] == "-15minutes" for u, p in calls)
    assert out["mode"] == "enterprise" and out["lingkaran"] == [] and out["sambaran"][0]["status"] == "kritis"
