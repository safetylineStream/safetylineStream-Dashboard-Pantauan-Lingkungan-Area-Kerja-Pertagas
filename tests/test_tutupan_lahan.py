"""Uji fungsi murni tutupan lahan (tanpa jaringan)."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import tutupan_lahan as tl  # noqa: E402


def test_nama_tile_worldcover():
    assert tl.nama_tile(-1.2, 116.9) == "S03E114"
    assert tl.nama_tile(1.6, 101.4) == "N00E099"
    assert tl.nama_tile(-6.9, 112.6) == "S09E111"
    assert tl.nama_tile(5.2, 97.0) == "N03E096"
    assert tl.nama_tile(-3.0, 105.0) == "S03E105"


def test_kategori_terbangun_dan_vegetasi():
    assert tl.kategori({50: 0.6, 40: 0.4}) == "non_vegetasi"          # kawasan industri/kota
    assert tl.kategori({80: 0.5, 50: 0.3, 10: 0.2}) == "non_vegetasi"  # pelabuhan: air + terbangun
    assert tl.kategori({10: 0.7, 40: 0.3}) == "vegetasi_lebat"         # hutan/kebun
    assert tl.kategori({40: 0.6, 10: 0.2, 50: 0.2}) == "vegetasi_rendah"  # sawah dengan kampung
    assert tl.kategori({}) is None


def test_ringkas_hist_mengabaikan_nodata():
    r = tl.ringkas_hist([0, 0, 50, 50, 50, 40])
    assert r["kode"] == 50 and r["porsi"] == 0.75 and r["kategori"] == "non_vegetasi"
    assert tl.ringkas_hist([0, 0, 0]) is None


def test_tambah_tutupan_tanpa_rasterio_tidak_gagal(monkeypatch, tmp_path):
    monkeypatch.setattr(tl, "OUT", tmp_path)

    def gagal(*a, **k):
        raise ImportError("rasterio")
    monkeypatch.setattr(tl, "kelas_titik", gagal)
    hs = [{"lat": -1.0, "lon": 117.0, "status": "kritis"}, {"lat": -2.0, "lon": 117.0, "status": "luar"}]
    assert tl.tambah_tutupan(hs) == 0
    assert "tutupan" not in hs[0]
    assert not (tmp_path / "titik_cache.json").exists()


def test_tambah_tutupan_memakai_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(tl, "OUT", tmp_path)
    panggil = []

    def palsu(titik, radius_m, **k):
        panggil.append(len(titik))
        return [tl.ringkas_hist([50, 50, 10]) for _ in titik]
    monkeypatch.setattr(tl, "kelas_titik", palsu)
    hs = [{"lat": -1.0, "lon": 117.0, "status": "waspada"}]
    assert tl.tambah_tutupan(hs) == 1 and hs[0]["tutupan"]["kategori"] == "non_vegetasi"
    hs2 = [{"lat": -1.0, "lon": 117.0, "status": "waspada"}]
    assert tl.tambah_tutupan(hs2) == 1 and panggil == [1]  # kedua kali dari cache
