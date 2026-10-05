"""
geo.py — Fungsi GIS kecil yang dipakai bersama (dan diuji di tests/test_geo.py).

Semua jarak dihitung secara geodesik (Haversine, jari-jari bumi rata-rata IUGG 6371,0088 km).
Galat Haversine terhadap elipsoid WGS84 < 0,5 % — jauh di bawah ketelitian lokasi hotspot
(piksel VIIRS 375 m, MODIS 1 km) maupun episentrum gempa (beberapa km).
"""
import math
from datetime import datetime, timedelta, timezone

R_BUMI_KM = 6371.0088
WIB = timezone(timedelta(hours=7))

# Batas wajar wilayah Indonesia (+ margin) untuk validasi koordinat hotspot SiPongi/FIRMS.
BBOX_INDONESIA = (90.0, -15.0, 145.0, 10.0)  # lon_min, lat_min, lon_max, lat_max


def haversine(lat1, lon1, lat2, lon2):
    """Jarak lingkaran besar (km) antara dua titik lat/lon dalam derajat."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_BUMI_KM * math.asin(min(1.0, math.sqrt(a)))


def valid_ll(lat, lon, bbox=None):
    """True bila lat/lon berupa angka berhingga, dalam rentang global, dan (opsional) di dalam bbox."""
    if lat is None or lon is None:
        return False
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    if lat == 0 and lon == 0:  # "null island" — hampir pasti data kosong
        return False
    if bbox:
        x0, y0, x1, y1 = bbox
        return x0 <= lon <= x1 and y0 <= lat <= y1
    return True


def perbaiki_latlon(lat, lon, bbox=BBOX_INDONESIA):
    """Kembalikan (lat, lon, status) — status: 'ok', 'tertukar' (diperbaiki), atau 'invalid'."""
    if valid_ll(lat, lon, bbox):
        return float(lat), float(lon), "ok"
    if valid_ll(lon, lat, bbox):
        return float(lon), float(lat), "tertukar"
    return None, None, "invalid"


def classify(d, radii):
    """Kategori jarak: kritis ≤ radii['kritis'] < waspada ≤ radii['waspada'] < pantau ≤ radii['pantau'] < luar."""
    if d is None:
        return "luar"
    if d <= radii["kritis"]:
        return "kritis"
    if d <= radii["waspada"]:
        return "waspada"
    if d <= radii["pantau"]:
        return "pantau"
    return "luar"


def tanggal_wib(iso_utc):
    """'2026-10-04T18:28:00.000000Z' → '2026-10-05' (tanggal WIB). None bila tidak terbaca."""
    if not iso_utc:
        return None
    s = str(iso_utc).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.split(".")[0] + "+00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(WIB).strftime("%Y-%m-%d")


class IndeksTitik:
    """Indeks grid sederhana untuk mencari titik dalam radius r_km dengan cepat."""

    def __init__(self, titik, sel_deg=0.05):
        self.sel = sel_deg
        self.grid = {}
        for t in titik:  # t: dict dengan lat, lon, ...
            self.grid.setdefault(self._key(t["lat"], t["lon"]), []).append(t)

    def _key(self, lat, lon):
        return (math.floor(lat / self.sel), math.floor(lon / self.sel))

    def dalam_radius(self, lat, lon, r_km):
        n = int(math.ceil((r_km / 111.0) / self.sel)) + 1
        ky, kx = self._key(lat, lon)
        out = []
        for dy in range(-n, n + 1):
            for dx in range(-n, n + 1):
                for t in self.grid.get((ky + dy, kx + dx), ()):
                    if haversine(lat, lon, t["lat"], t["lon"]) <= r_km:
                        out.append(t)
        return out


def hari_berulang(hotspot, indeks, r_km=1.0):
    """Jumlah tanggal (WIB) berbeda dengan deteksi dalam radius r_km dari hotspot ini,
    termasuk deteksi hotspot itu sendiri."""
    hari = {tanggal_wib(t.get("waktu_utc")) for t in indeks.dalam_radius(hotspot["lat"], hotspot["lon"], r_km)}
    hari.add(tanggal_wib(hotspot.get("waktu_utc")))
    hari.discard(None)
    return len(hari)
