"""Uji modul kapal AIS (tanpa jaringan): parsing pesan, jarak ke pipa offshore, klasifikasi, kegagalan sumber."""
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import kapal  # noqa: E402

NOW = datetime(2026, 10, 21, 7, 40, tzinfo=timezone.utc)


def pos(mmsi, lat, lon, sog=10.0, nav=0, t="2026-10-21 07:38:00.123 +0000 UTC", tipe="PositionReport"):
    return {"MessageType": tipe, "MetaData": {"MMSI": mmsi, "ShipName": f"KAPAL {mmsi}  ", "latitude": lat, "longitude": lon, "time_utc": t},
            "Message": {tipe: {"UserID": mmsi, "Latitude": lat, "Longitude": lon, "Sog": sog, "Cog": 90, "TrueHeading": 511,
                               "NavigationalStatus": nav}}}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for d in ("config",):
        shutil.copytree(REPO / d, tmp_path / d)
    (tmp_path / "data" / "kapal").mkdir(parents=True)
    # pipa uji: garis lurus timur–barat di lat -7.0 dari 113.0 ke 114.0
    pipa = {"type": "FeatureCollection", "total_km": 110.4, "features": [{"type": "Feature", "properties": {"name": "Pipa uji 28\""},
            "geometry": {"type": "LineString", "coordinates": [[113.0, -7.0], [114.0, -7.0]]}}]}
    (tmp_path / "data" / "kapal" / "pipa_offshore.geojson").write_text(json.dumps(pipa))
    monkeypatch.setattr(kapal, "ROOT", tmp_path)
    monkeypatch.setattr(kapal, "DATA", tmp_path / "data" / "kapal")
    monkeypatch.setattr(kapal, "OUT", tmp_path / "data" / "kapal" / "oeja.json")
    monkeypatch.setattr(kapal, "PIPA", tmp_path / "data" / "kapal" / "pipa_offshore.geojson")
    return tmp_path


def test_parse_pesan_dan_nilai_tidak_valid():
    k, s = {}, {}
    assert kapal.olah_pesan(pos(1, -7.0, 113.5, sog=102.3), k, s)
    assert k[1]["sog"] is None and k[1]["heading"] is None and k[1]["nama"] == "KAPAL 1"
    assert not kapal.olah_pesan(pos(2, 91, 181), k, s)          # 91/181 = posisi tidak tersedia
    assert not kapal.olah_pesan(pos(3, 0, 0), k, s)
    # posisi lebih lama tidak menimpa yang baru
    kapal.olah_pesan(pos(1, -7.5, 113.5, t="2026-10-21 07:00:00 +0000 UTC"), k, s)
    assert k[1]["lat"] == -7.0
    kapal.olah_pesan({"MessageType": "ShipStaticData", "MetaData": {"MMSI": 1},
                      "Message": {"ShipStaticData": {"Name": "MT UJI", "Type": 80, "ImoNumber": 9000001,
                                                     "Dimension": {"A": 100, "B": 20}}}}, k, s)
    assert s[1]["nama"] == "MT UJI" and s[1]["panjang_m"] == 120 and kapal.tipe_kapal(80) == "Tanker"


def test_jarak_dan_klasifikasi():
    r = {"kritis": 0.5, "waspada": 1.0, "pantau": 2.0}
    idx = kapal.IndeksPipa({"features": [{"properties": {"name": "P"}, "geometry": {"type": "LineString", "coordinates": [[113.49, -7.0], [113.51, -7.0]]}}]}, 113.5)
    d, ruas = idx.terdekat(-7.0 + 0.3 / 110.6, 113.5)  # ±300 m utara pipa
    assert abs(d - 300) < 5 and ruas["name"] == "P"
    assert kapal.klasifikasi(0.3, True, r) == "kritis"
    assert kapal.klasifikasi(0.3, False, r) == "waspada"
    assert kapal.klasifikasi(0.8, True, r) == "waspada"
    assert kapal.klasifikasi(0.8, False, r) == "pantau"
    assert kapal.klasifikasi(1.9, False, r) == "pantau"
    assert kapal.klasifikasi(2.5, True, r) == "luar"


def test_main_tanpa_key_tidak_menulis(repo):
    assert kapal.main(env={}) == 0
    assert not (repo / "data" / "kapal" / "oeja.json").exists()


def test_main_sukses_gabung_dan_gagal(repo):
    def fetch1(key, bbox, detik):
        k, s = {}, {}
        kapal.olah_pesan(pos(11, -7.0 + 0.2 / 110.6, 113.4, sog=0.1, nav=1), k, s)   # berlabuh 200 m dari pipa
        kapal.olah_pesan(pos(12, -7.0 - 0.7 / 110.6, 113.6, sog=12), k, s)            # bergerak 700 m
        kapal.olah_pesan(pos(13, -6.6, 113.6, sog=8), k, s)                           # jauh
        return k, s, 3
    assert kapal.main(fetch=fetch1, now=NOW, env={"AISSTREAM_API_KEY": "rahasia"}) == 0
    d = json.loads((repo / "data" / "kapal" / "oeja.json").read_text())
    st = {v["mmsi"]: v["status"] for v in d["kapal"]}
    assert st == {11: "kritis", 12: "pantau", 13: "luar"}
    assert d["kapal"][0]["mmsi"] == 11 and d["kapal"][0]["waktu_utc"] == "2026-10-21T07:38:00+00:00"
    assert "rahasia" not in json.dumps(d)

    # run berikut hanya melihat kapal 12 → kapal 11 & 13 dibawa (masih ≤ 90 menit)
    def fetch2(key, bbox, detik):
        k, s = {}, {}
        kapal.olah_pesan(pos(12, -7.0, 113.6, sog=0.2, nav=0, t="2026-10-21 08:05:00 +0000 UTC"), k, s)
        return k, s, 1
    assert kapal.main(fetch=fetch2, now=NOW + timedelta(minutes=30), env={"AISSTREAM_API_KEY": "rahasia"}) == 0
    d = json.loads((repo / "data" / "kapal" / "oeja.json").read_text())
    st = {v["mmsi"]: v["status"] for v in d["kapal"]}
    assert st[12] == "kritis" and st[11] == "kritis" and 13 in st and d["jumlah"]["terlihat_run_ini"] == 1

    # sumber gagal: data lama tetap, status ditandai gagal, waktu data tidak diganti
    def gagal(key, bbox, detik):
        raise RuntimeError("koneksi ditolak key=rahasia")
    assert kapal.main(fetch=gagal, now=NOW + timedelta(minutes=60), env={"AISSTREAM_API_KEY": "rahasia"}) == 1
    d2 = json.loads((repo / "data" / "kapal" / "oeja.json").read_text())
    assert d2["diperbarui"] == d["diperbarui"] and d2["status"]["ok"] is False and "rahasia" not in d2["status"]["pesan"]

    # posisi yang terlalu tua (> simpan_menit) dibuang
    def kosong(key, bbox, detik):
        return {}, {}, 0
    assert kapal.main(fetch=kosong, now=NOW + timedelta(hours=4), env={"AISSTREAM_API_KEY": "rahasia"}) == 0
    d3 = json.loads((repo / "data" / "kapal" / "oeja.json").read_text())
    assert d3["kapal"] == [] and d3["jumlah"]["total"] == 0
