# PERTAGAS WASPADA
**Watch & Analytics for Spatial Pipeline Anomaly & Disaster Awareness** — dashboard pemantauan geohazard & lingkungan jalur pipa PT Pertamina Gas, by Fungsi HSSE Pertamina Gas.

Dashboard ini memantau **hotspot karhutla**, **gempa bumi & tsunami**, **gerakan tanah (PVMBG)**, **sambaran petir real-time (Vaisala Xweather)**, serta **cuaca, kelembapan dan kualitas udara** di sekitar jalur pipa dan fasilitas Pertagas per wilayah kerja. Datanya diperbarui otomatis oleh GitHub Actions dan ditampilkan lewat GitHub Pages.

```
 SiPongi+ (hotspot)  ─┐
 BMKG InaTEWS (gempa) ─┼─► GitHub Actions (06:00 & 17:00 WIB) ─► scripts/fetch_all.py
 BMKG / Open-Meteo    ─┤        │  bandingkan jarak ke aset (data/assets.geojson)
 PVMBG gerakan tanah  ─┘        │  potong zona gerakan tanah dengan jalur pipa & fasilitas
                                ▼
                     data/latest.json + data/history/  ──► index.html (GitHub Pages)
```

| Folder / file | Isi |
|---|---|
| `index.html` | Halaman dashboard (peta, KPI, ringkasan wilayah, cuaca, tren) |
| `assets_raw/` | File KMZ/KML/SHP aset per wilayah kerja (sumber asli) |
| `config/assets_sources.json` | Aturan folder mana di KMZ yang dianggap *jalur pipa* dan *fasilitas* |
| `config/monitoring.json` | Radius kritis/waspada/pantau, titik cuaca, provinsi pantauan |
| `scripts/build_assets.py` | Mengubah KMZ/SHP → `data/assets.geojson` |
| `scripts/fetch_all.py` | Mengambil data harian dan menghitung jarak ke aset |
| `scripts/gerakan_tanah.py` | Mengambil data gerakan tanah PVMBG dan menghitung panjang pipa/fasilitas di tiap zona |
| `scripts/petir.py` | Mengambil sambaran petir real-time Vaisala Xweather dan menghitung jaraknya ke jalur pipa & fasilitas → `data/petir/` (lihat A8) |
| `scripts/lhasa.py` | Menghitung potensi longsor harian (algoritma NASA LHASA 1.1) di sepanjang seluruh jalur pipa & fasilitas → `data/lhasa/` |
| `lhasa_statis/` | Ambang hujan ARI95 resmi NASA (potongan Indonesia) dan, opsional, peta kerentanan manual `kerentanan.tif` |
| `gerakan_tanah_raw/` | (Opsional) file peta gerakan tanah manual bila layanan online tidak bisa diakses |
| `.github/workflows/` | Jadwal otomatis (update harian & build aset) |

---

## ⚠️ Sebelum mulai: tentukan visibilitas repositori

Jalur pipa dan fasilitas adalah **data objek vital**. Pada repositori **Public**, semua orang di internet bisa melihat `assets_raw/` dan peta jalur pipa.

| Pilihan | Konsekuensi |
|---|---|
| **Private + GitHub Pages** (disarankan) | Butuh GitHub **Pro / Team / Enterprise**. Dengan *Enterprise Cloud*, akses halaman bisa dibatasi hanya untuk anggota organisasi. |
| **Public** | Gratis, tapi jalur pipa terbuka untuk umum. Minta persetujuan fungsi Security/IT dulu. |

---

## Bagian A — Memasang dashboard ke GitHub (sekali saja, ±20 menit)

### A1. Buat repositori
1. Masuk ke <https://github.com> → klik **+** (kanan atas) → **New repository**.
2. *Repository name*: misalnya `pertagas-env-dashboard`.
3. Pilih **Private** atau **Public** (lihat pertimbangan di atas). **Jangan** centang “Add a README”.
4. Klik **Create repository**.

### A2. Unggah file
**Cara 1: lewat browser (tanpa instal apa pun)**
1. Ekstrak ZIP paket ini di komputer Anda.
2. Di halaman repositori baru, klik tautan **uploading an existing file**.
3. Buka folder hasil ekstrak, pilih **semua isinya** (termasuk folder `.github`), lalu seret ke halaman GitHub.
   - Folder `.github` tersembunyi di Mac: tekan `Cmd + Shift + .` di Finder. Di Windows: *View → Hidden items*.
   - Pastikan struktur di GitHub adalah `index.html` di akar repositori, **bukan** di dalam subfolder.
4. Klik **Commit changes**.
5. Cek bahwa folder `.github/workflows` sudah ada di repositori. Jika belum ada, buat manual: **Add file → Create new file**, beri nama `.github/workflows/update-data.yml`, lalu salin isi file tersebut. Ulangi untuk `build-assets.yml`.

**Cara 2: lewat Git (bila sudah terbiasa)**
```bash
cd pertagas-env-dashboard
git init -b main
git add .
git commit -m "Dashboard pemantauan lingkungan - versi awal"
git remote add origin https://github.com/<akun-anda>/pertagas-env-dashboard.git
git push -u origin main
```

### A3. Izinkan workflow menyimpan data
**Settings → Actions → General**:
- *Actions permissions*: **Allow all actions and reusable workflows**
- *Workflow permissions*: **Read and write permissions** → **Save**

### A4. Aktifkan GitHub Pages
**Settings → Pages**:
- *Source*: **Deploy from a branch**
- *Branch*: `main`, folder `/ (root)` → **Save**

Setelah 1–2 menit, alamat dashboard muncul di halaman itu, misalnya `https://<akun-anda>.github.io/pertagas-env-dashboard/`.

### A5. Jalankan update pertama
1. Buka tab **Actions** → pilih **Update Data Harian** → **Run workflow** → **Run workflow**.
2. Tunggu hingga tanda ✅ hijau muncul (±1–2 menit).
3. Muat ulang dashboard. Banner kuning “Data contoh” akan hilang dan angka berganti data asli.

Setelah itu data diperbarui **otomatis setiap hari pukul 06:00 dan 17:00 WIB**.

### A6. (Opsional) Cadangan NASA FIRMS
Jika server SiPongi sedang tidak bisa diakses, sistem bisa memakai NASA FIRMS, yang memakai satelit yang sama.
1. Daftar MAP_KEY gratis di <https://firms.modaps.eosdis.nasa.gov/api/map_key/>.
2. **Settings → Secrets and variables → Actions → New repository secret**. Isi *Name* `FIRMS_MAP_KEY` dan *Secret* dengan kunci Anda.

### A7. (Disarankan) Akses data gerakan tanah PVMBG
Data gerakan tanah diambil dari peta PVMBG di ESDM One Map:

| Data | Cara mengaktifkan | Secret GitHub |
|---|---|---|
| **Zona kerentanan** dan **prakiraan potensi gerakan tanah bulanan** (peta ESDM One Map) | Sistem mencoba mengambilnya otomatis. Jika layanan meminta login, daftar akun gratis di ESDM One Map (geoportal.esdm.go.id) | `ONEMAP_USERNAME`, `ONEMAP_PASSWORD` |

Jika belum ada kredensial, dashboard tetap berjalan; cadangan manual ada di bagian B7.

### A8. (Opsional, berbayar) Deteksi sambaran petir real-time — Vaisala Xweather
Modul petir mengambil data sambaran dari **Vaisala Xweather Lightning API** (jaringan GLD360) lalu menghitung jarak tiap sambaran ke jalur pipa & fasilitas terdekat. Data Xweather Lightning **berbayar** (ada uji coba gratis 30 hari); pastikan langganan Anda mencakup endpoint `lightning`.

1. Masuk ke akun Xweather (https://account.xweather.com) → **Apps** → buat aplikasi baru. Catat **Client ID** dan **Client Secret**.
2. Di GitHub: **Settings → Secrets and variables → Actions → New repository secret**, buat dua secret:
   - `XWEATHER_CLIENT_ID` = Client ID
   - `XWEATHER_CLIENT_SECRET` = Client Secret
3. Tab **Actions → Update Petir (Xweather) → Run workflow** (isi *putaran* = `1` untuk uji). Bila ✅, muat ulang dashboard: kartu **⚡ Sambaran petir** terisi dan chip **Petir** di kanan atas berwarna hijau.
4. Setelah itu workflow berjalan otomatis **tiap 10 menit**; tiap jadwal mengambil data 3× (jeda 4 menit) dan hanya membuat commit bila ada perubahan atau sudah 30 menit (menjaga batas build GitHub Pages ±10/jam).

**Cara kerja & kuota.** Akses standar Xweather hanya memberi data **5 menit terakhir** dalam lingkaran **maks 100 km**. Skrip otomatis menyebar lingkaran kueri sehingga seluruh jalur pipa + radius pantau 30 km tertutup — untuk aset saat ini **20 lingkaran per pengambilan** (tercatat di `data/petir/latest.json` → `jumlah.kueri`). Dengan 3 pengambilan per 10 menit ≈ **8.640 kueri/hari**. Endpoint lightning memakai *access multiplier* (standar ±10×), jadi periksa batas akses harian paket Anda. Untuk menghemat: kurangi `petir.polling.jumlah` atau ubah jadwal cron di `.github/workflows/update-petir.yml`.
Bila memiliki add-on **Lightning Enterprise**, ubah `petir.mode` menjadi `"enterprise"`: skrip memakai kueri kotak (`within`) per wilayah — hanya 8 kueri per pengambilan — dengan riwayat 15 menit sehingga tidak ada celah antar-pengambilan.

**Mode LIVE di browser (opsional, benar-benar real-time).** Data terjadwal tiba di dashboard dengan jeda ±10–15 menit (jadwal Actions + build Pages). Untuk pemantauan saat badai, dashboard dapat mengambil data langsung dari Xweather setiap 2 menit selama halaman dibuka:
1. Di Xweather buat **aplikasi kedua** khusus browser dengan *namespace/domain* `safetylinestream.github.io` (kunci hanya diterima dari domain itu).
2. Isi `config/monitoring.json` → `petir.browser.client_id` dan `client_secret` dengan kunci aplikasi kedua tersebut.
3. ⚠️ Kunci ini **terlihat publik** di repo. **Jangan pernah** memakai kunci server (secret Actions) di sini. Cek langsung hanya berjalan di tab wilayah (2–8 kueri per putaran) dan berhenti saat tab browser tidak aktif, supaya kuota tidak boros.

Tanpa secret, workflow petir selesai tanpa mengubah data dan kartu petir menampilkan "Belum aktif".

---

## Bagian B — Operasional harian (command & operate)

### B1. Membaca dashboard
| Elemen | Arti | Tindak lanjut yang disarankan |
|---|---|---|
| ▲ **Kritis** (≤ 1 km dari pipa/fasilitas) | Titik panas sangat dekat aset | Informasikan ke area terkait untuk *ground check* segera; siagakan APAR/regu, koordinasi Manggala Agni/BPBD |
| ◆ **Waspada** (1–3 km) | Berpotensi merambat ke ROW | Patroli ROW, pantau arah angin di panel cuaca |
| ● **Pantau** (3–5 km) | Perlu diperhatikan | Monitor tren; koordinasi dengan pemda/BPBD |
| ↻ **Berulang N hari** | Hotspot terdeteksi di lokasi yang sama (≤ 1 km) pada ≥ 3 tanggal berbeda dalam 7 hari. Sering berupa sumber panas tetap (flare/industri), tetapi bisa juga kebakaran yang berlangsung lama (mis. gambut) | Verifikasi sekali di lapangan apakah sumber panas tetap; bila ya, catat agar tim tidak terus dipanggil. Titik tetap ditampilkan — tidak pernah disembunyikan |
| **Status saat ini** (kartu paling atas) | Kondisi terburuk dari: hotspot dekat aset, gempa ≤ 24 jam dekat aset, potensi tsunami BMKG, indikasi longsor model, prakiraan PVMBG zona tinggi, cuaca ekstrem (kriteria BMKG: hujan ≥ 50 mm/hari, angin ≥ 25 knot) dan kualitas udara | Klik baris untuk membuka tab wilayahnya |
| Gempa: status per jarak ke aset | Kritis ≤ 50 km dan M ≥ 5, waspada ≤ 150 km, pantau ≤ 300 km | Inspeksi pasca-gempa pada fasilitas terdekat sesuai prosedur |
| ⚠ **Berpotensi tsunami** | Dari rilis BMKG | Ikuti ERP dan instruksi resmi BMKG/BPBD |
| Gerakan tanah — **prakiraan Tinggi/Menengah** | Ruas pipa atau fasilitas berada di zona potensi gerakan tanah bulan ini (PVMBG, dipengaruhi curah hujan) | Patroli ROW lebih sering saat hujan lebat; cek retakan tanah, amblesan, tiang/patok miring, dan kondisi *crossing* sungai/lereng |
| Gerakan tanah — **zona kerentanan Tinggi** | Kondisi geologi dasar yang rentan (tidak berubah bulanan) | Masukkan ke penilaian risiko geohazard/integritas pipa; prioritaskan inspeksi dan monitoring pergerakan tanah |
| ⚡ Petir **Kritis** (sambaran ≤ 8 km dari pipa/fasilitas dalam 30 menit) | Setara aturan 30-30 (kilat–guruh < 30 detik ≈ 10 km) | **Hentikan** pekerjaan luar ruang, *hot work*, kerja di ketinggian, *loading/unloading* dan *venting*; personel berlindung. Lanjutkan **30 menit** setelah sambaran terakhir ≤ 16 km (kartu menampilkan *Perkiraan aman*) |
| ⚡ Petir **Waspada** (≤ 16 km) / **Pantau** (≤ 30 km) | Badai petir mendekat | Siapkan penghentian pekerjaan; supervisor memantau arah badai |
| ⚡ Sambaran **≤ 1 km** dari aset | Kemungkinan sambaran langsung/imbas ke aset | Setelah badai reda: periksa proteksi katodik & *insulating joint*, instrumen/SCADA, panel listrik, vent/flare dan *grounding* |
| RH, suhu, angin, PM2.5 | RH rendah + angin kencang = risiko rambatan api naik; PM2.5 tinggi = indikasi asap | Pertimbangkan pembatasan *hot work* / pekerjaan lapangan |

- **Tab wilayah** di bagian atas: **Ringkasan semua** menampilkan kartu tiap wilayah (klik kartu untuk membuka tabnya), lalu satu tab untuk tiap wilayah kerja (ONSA, OCSA, ODA, OSSA, OWJA). Angka kecil di tab menunjukkan jumlah hotspot dengan status terburuk di wilayah itu. Tanda ✓ berarti tidak ada hotspot dalam radius.
- Di tab wilayah, peta langsung terfokus ke wilayah tersebut. Jarak gempa dihitung ke aset **wilayah itu**, dan panel cuaca menampilkan kondisi terkini serta prakiraan 3 hari tiap titik pantau. Grafik tren dipecah per status, dan grafik provinsi hanya memuat provinsi tempat wilayah itu beroperasi.
- Tab bisa dibuka langsung lewat alamat, misalnya `.../pertagas-env-dashboard/#OSSA`. Tautan ini praktis untuk dikirim ke tim area.
- Pilih **Kepercayaan** untuk menyaring hotspot berkeyakinan rendah.
- Klik baris hotspot atau gempa untuk langsung menuju lokasinya di peta. Popup hotspot memuat tautan Google Maps untuk tim lapangan.
- Ikon lapisan di kanan atas peta: ganti ke **Citra satelit**, atau nyalakan **Titik cuaca**.
- Hotspot adalah **indikasi titik panas dari satelit**, bukan kepastian kebakaran. Selalu verifikasi di lapangan.

### B2. Update manual di luar jadwal
**Actions → Update Data Harian → Run workflow.**

### B3. Mengecek apakah update berjalan
- Chip di kanan atas dashboard menunjukkan **kesegaran tiap sumber berdasarkan umur data**, bukan sekadar hasil run terakhir: 🟢 refresh · 🟡 terlambat (satu jadwal terlewat atau pengambilan terakhir gagal) · 🔴 kedaluwarsa · ○ belum tersedia. Ambang berbeda per sumber — lihat tabel **Sumber & status data** di bagian bawah dashboard.
- Bila pengambilan gagal, dashboard tetap menampilkan **data terakhir yang berhasil** beserta waktunya ("Pengambilan terakhir berhasil"); waktunya tidak pernah diganti dengan waktu sekarang.
- **Actions**: ✅ berarti sukses, ❌ berarti gagal. Klik run yang gagal untuk melihat log.
- Riwayat harian tersimpan di `data/history/ringkasan.csv` (bisa dibuka di Excel). Arsip hotspot dekat aset tersimpan di `data/history/hotspot_YYYY-MM-DD.json` sebagai bukti audit.

### B4. Mengubah pengaturan (edit langsung di GitHub, ikon ✏️)
| Ingin mengubah | File | Bagian |
|---|---|---|
| Radius kritis/waspada/pantau | `config/monitoring.json` | `hotspot.radius_km`, `gempa.radius_km` |
| Titik cuaca (tambah/hapus lokasi) | `config/monitoring.json` | `titik_cuaca` |
| Memakai data cuaca **BMKG resmi** | `config/monitoring.json` | isi `adm4` (lihat B5) |
| Provinsi di grafik | `config/monitoring.json` | `provinsi_pantauan` (tab Ringkasan), `provinsi_area` (tab wilayah) |
| Radius & aturan petir (kritis/waspada/pantau, all-clear, mode, kunci LIVE) | `config/monitoring.json` | `petir` |
| Jam update | `.github/workflows/update-data.yml` | `cron` (jam UTC = WIB − 7) |

Contoh: update setiap 3 jam → ganti kedua baris cron dengan `- cron: "0 */3 * * *"`.

### B5. Memakai data cuaca BMKG per kelurahan
Secara bawaan, cuaca diambil dari Open-Meteo (berbasis koordinat). Untuk memakai prakiraan resmi BMKG:
1. Buka <https://www.bmkg.go.id/cuaca/prakiraan-cuaca>, lalu cari kelurahan/desa lokasi fasilitas.
2. Salin kode wilayah dari alamat halaman, misalnya `.../prakiraan-cuaca/14.72.02.1003`.
3. Isikan ke `"adm4": "14.72.02.1003"` pada titik cuaca yang sesuai.

Jika BMKG gagal diakses, sistem otomatis kembali memakai Open-Meteo. PM2.5 tetap diambil dari Open-Meteo.

### B6. Menambah atau memperbarui aset wilayah kerja (KMZ/SHP)
1. Unggah file ke folder `assets_raw/`:
   - KMZ/KML: unggah langsung.
   - Shapefile: satukan `.shp`, `.shx`, `.dbf`, `.prj` dalam **satu file .zip**.
2. Edit `config/assets_sources.json`:
   - Tambahkan kode area di `areas`, misalnya `"OEJA": {"nama": "Operation East Java Area"}`.
   - Tambahkan baris di `files`:
     ```json
     {"file": "OEJA_Pipeline.kmz", "area": "OEJA", "lines": ["*"], "points": ["Station"]}
     ```
     `lines` dan `points` diisi **nama folder** di Google Earth yang berisi jalur pipa dan fasilitas. `"*"` berarti semua.
     Jika satu folder berisi campuran ruas baru dan ruas yang sudah ada di file lain, tambahkan `"line_names": ["NAMA RUAS 1", "NAMA RUAS 2"]` agar hanya ruas tersebut yang diambil (contoh: `OSSA_Peta_Pipa_Gas_Pendopo.kmz`).
3. Commit. Workflow **Build Aset** akan berjalan otomatis dan memperbarui peta.
4. Di `config/monitoring.json`, tambahkan titik cuaca area baru (`titik_cuaca`) dan provinsinya (`provinsi_area`). Tab wilayah baru akan muncul otomatis.

### B7. Gerakan tanah: cara kerja & cadangan manual
- **Analisis:** peta zona dari PVMBG dipotong dengan jalur pipa dan fasilitas. Hasilnya panjang pipa (km) di tiap tingkat (Tinggi, Menengah, Rendah, Sangat rendah), daftar fasilitas di zona menengah–tinggi, dan segmen pipa yang terdampak. Poligon yang ditampilkan di peta hanya yang berada dalam koridor `koridor_km` (bawaan 5 km) di sekitar aset.
- **Jadwal pengambilan:** prakiraan diambil setiap run. Zona kerentanan cukup diambil ulang setiap 30 hari (`refresh_hari`) karena datanya jarang berubah.
- **Cadangan manual:** jika layanan online tidak bisa diakses, unduh peta dari ESDM One Map atau Portal MBG PVMBG, lalu unggah ke `gerakan_tanah_raw/` dengan nama `prakiraan.geojson`/`prakiraan.zip` (SHP) dan `zkgt.geojson`/`zkgt.zip`. Kolom atributnya harus memuat teks *Tinggi / Menengah / Rendah / Sangat Rendah*. Setelah diunggah, jalankan **Update Data Harian**.
- **Pengaturan** ada di `config/monitoring.json` → `gerakan_tanah` (alamat layanan, koridor analisis).

### B8. Potensi longsor harian (model LHASA internal)
Layanan nowcast NASA LHASA tidak bisa diakses dari GitHub Actions dan berkasnya tidak diperbarui sejak akhir 2025. Karena itu dashboard menghitung sendiri dengan **algoritma terbuka NASA LHASA 1.1** (Kirschbaum & Stanley 2018):

1. **Hujan anteseden 7 hari (ARI)**: curah hujan 6 hari terakhir + hari ini (Open-Meteo), hari terbaru diberi bobot terbesar: `ARI = Σ P(hari−k)/(k+1)² ÷ Σ 1/(k+1)²`, k = 0–6.
2. **Ambang**: ARI dibandingkan dengan **ARI95** — persentil ke-95 ARI historis per sel 0,1° dari berkas resmi NASA (`lhasa_statis/ARI95_indonesia.tif`, satuan 0,1 mm).
3. **Kerentanan lereng** kelas 1–5 di sepanjang pipa (dihitung sekali lalu disimpan di `data/lhasa/kerentanan_cache.json`). Sumber dicoba berurutan: file manual `lhasa_statis/kerentanan.tif` → peta kerentanan global NASA → pendekatan kemiringan lereng dari Copernicus DEM (kelas: < 5°, 5–10°, 10–15°, 15–25°, ≥ 25°).
4. **Pohon keputusan LHASA**: hujan > ambang & kerentanan kelas 3–4 → **Sedang**; hujan > ambang & kelas 5 → **Tinggi**. Tambahan Pertagas (bukan bagian LHASA): hujan ≥ 75% ambang & kelas ≥ 3 → **Rendah** (mendekati ambang).

- **Produk:** *hari ini* dan *besok* (memakai prakiraan hujan besok).
- **Jadwal:** workflow **Update Potensi Longsor (NASA LHASA)** pukul 05:15 dan 16:15 WIB. Jalankan manual: tab **Actions** → pilih workflow → **Run workflow**.
- **Pengaturan:** `config/monitoring.json` → `lhasa`. Setelah mengubah aset atau sumber kerentanan, kerentanan dihitung ulang otomatis.
- **Keterbatasan:** ambang ARI95 NASA dibuat dari hujan satelit IMERG, sedangkan hujan harian di sini dari model cuaca Open-Meteo, jadi nilainya bisa sedikit bergeser. Hasilnya indikatif untuk kesiapsiagaan dan wajib diverifikasi di lapangan; rujukan resmi tetap PVMBG.
- Riwayat harian per wilayah tersimpan di `data/lhasa/riwayat.csv`.

---

### B9. Waktu, cek langsung & uji otomatis
- Semua waktu di dashboard ditampilkan dalam **WIB**, juga bila komputer pengguna memakai WITA/WIT.
- Browser pengguna mengecek langsung **gempa terbaru BMKG** tiap 10 menit dan **cuaca/kualitas udara** titik pantau saat tab wilayah dibuka. Bila cek langsung gagal (mis. jaringan kantor memblokir), dashboard otomatis memakai data terjadwal dan menuliskannya.
- Data dimuat ulang otomatis tiap 15 menit tanpa mereset tab dan posisi peta.
- Uji otomatis pipeline (jarak geodesik, validasi koordinat, kegagalan API, respons kosong/rusak): `pip install -r requirements.txt pytest` lalu `python -m pytest tests -q`. Uji ini juga dijalankan workflow **Uji Otomatis** setiap ada perubahan skrip.

## Bagian C — Pemecahan masalah

| Gejala | Penyebab & solusi |
|---|---|
| Dashboard 404 | Pages belum aktif atau baru diaktifkan. Tunggu 2 menit dan cek Settings → Pages |
| Actions gagal di langkah “Simpan hasil” (error 403) | Workflow permissions belum *Read and write* (langkah A3) |
| Chip **SiPongi+: gagal** | Server SiPongi sedang gangguan atau berubah alamat. Data lama tetap tampil. Isi `FIRMS_MAP_KEY` sebagai cadangan |
| Jadwal tidak jalan | Jadwal GitHub bisa terlambat 5–30 menit. Pada repo **public**, jadwal dinonaktifkan GitHub setelah 60 hari tanpa aktivitas; aktifkan lagi di tab Actions |
| Chip **PVMBG Prakiraan GT / Zona GT: gagal** | Layanan ESDM One Map meminta login atau sedang gangguan. Isi `ONEMAP_USERNAME`/`ONEMAP_PASSWORD`, atau pakai file manual (B7). Arahkan kursor ke chip untuk melihat pesan errornya |
| Chip **Petir** merah / pesan `invalid_client` atau `unauthorized` | Client ID/Secret salah, langganan tidak mencakup endpoint lightning, atau namespace aplikasi Xweather tidak cocok. Periksa secret (A8) dan paket Xweather Anda |
| Kartu petir: "kueri mencapai batas 1000 sambaran" | Badai sangat aktif di dalam satu lingkaran 100 km; sambaran terdekat tetap terambil (diurutkan dari terdekat), sambaran jauh bisa terlewat |
| Peta kosong saat file dibuka dari komputer | Browser memblokir `fetch` dari file lokal. Buka lewat GitHub Pages, atau jalankan `python -m http.server` lalu buka `http://localhost:8000` |

## Catatan sumber data
- **SiPongi+** (Kemenhut): hotspot 24 jam dari satelit NASA MODIS, SNPP, NOAA-20 dan NOAA-21. Endpoint `opsroom.sipongidata.my.id` adalah layanan data di balik peta SiPongi dan tidak didokumentasikan resmi, jadi bisa berubah sewaktu-waktu. Karena itu disediakan cadangan FIRMS.
- **BMKG InaTEWS** (`data.bmkg.go.id/DataMKG/TEWS/`): data terbuka gempa terbaru, M 5.0+ terkini, dan gempa dirasakan, termasuk keterangan potensi tsunami. Cantumkan BMKG sebagai sumber.
- **Cuaca**: BMKG (`api.bmkg.go.id`, prakiraan per 3 jam, bila `adm4` diisi) dan Open-Meteo. Keduanya **model/prakiraan**, bukan pengamatan stasiun. Kualitas udara dari Open-Meteo Air Quality (model CAMS global ±45 km); PM2.5/PM10 dalam µg/m³ dan US AQI (EPA, rata-rata 24 jam) — **bukan ISPU** stasiun KLHK. Catatan lisensi: API gratis Open-Meteo ditujukan untuk penggunaan non-komersial; untuk penggunaan korporat pertimbangkan langganan API Open-Meteo atau sumber BMKG.
- **PVMBG – Badan Geologi**: *Prakiraan Potensi Gerakan Tanah Bulanan* diambil dari **Portal MBG** PVMBG — `https://vsi.esdm.go.id/portalmbg/api/download-asset-forecast/gertan/forecast/vector/{tahun}/{bulan}/{kode_provinsi}` (shapefile ZIP per provinsi, kode provinsi BPS; diatur di `config/monitoring.json` → `gerakan_tanah.portalmbg`). Atribut `Zona_Perki` = zona prakiraan bulan itu (Tinggi, Menengah, Rendah, Berpotensi banjir bandang/aliran bahan rombakan); atribut `Unsur` = zona kerentanan gerakan tanah dasar. Bila bulan berjalan belum terbit, dipakai bulan sebelumnya dan dashboard menandainya. Hasil per provinsi disimpan di `data/gerakan_tanah/cache_portal/` dan hanya diunduh ulang bila tanggal terbitnya berubah. Layanan GIS ESDM One Map tetap dipakai sebagai cadangan bila Portal MBG gagal.
- **Petir**: Vaisala Xweather Lightning API (`data.api.xweather.com/lightning`, jaringan GLD360). Default hanya sambaran awan-ke-tanah (CG); lokasi sambaran memiliki ketidakpastian ±beberapa ratus meter. Sambaran ≤ 30 km dari aset diarsipkan per hari (WIB) di `data/petir/arsip/YYYY-MM-DD.json` sebagai bukti audit. Data berlisensi — cantumkan Vaisala Xweather sebagai sumber.
- **Potensi longsor**: algoritma NASA LHASA 1.1 (github.com/nasa/LHASA, tag v1.1.1) dihitung internal; ambang ARI95 NASA, hujan Open-Meteo, kerentanan dari peta global NASA atau Copernicus DEM GLO-90 (© DLR/Airbus, ESA). Bersifat indikatif, bukan pengganti informasi resmi PVMBG.
- Jarak dihitung dari titik hotspot/episentrum ke **jalur pipa atau fasilitas terdekat** pada `data/assets.geojson` secara geodesik (Haversine; diuji terhadap perhitungan WGS84 independen, selisih < 20 m). Geometri pipa telah disederhanakan (±30 m) supaya halaman ringan. Lokasi hotspot sendiri punya ketidakpastian sebesar pikselnya (VIIRS ±375 m, MODIS ±1 km).


## Lalu lintas kapal OEJA (AIS)
Khusus tab **OEJA** (pipa offshore 28" Pagerungan – Porong, ±378 km di laut). Kartu ini punya dua tampilan:

1. **Kapal × pipa offshore** — peta dashboard sendiri berisi ruas pipa offshore, koridor 500 m, dan posisi kapal AIS. Tiap kapal diberi status dari jarak tegak lurus ke pipa offshore:
   kritis = diam/berlabuh (SOG < 0,5 kn atau status berlabuh/tambat/NUC/kandas) ≤ 0,5 km · waspada = melintas ≤ 0,5 km atau diam ≤ 1 km · pantau ≤ 2 km. Ambang di `config/monitoring.json` → `kapal`.
   - Ruas offshore diturunkan dari `assets.geojson` dikurangi daratan Natural Earth 1:10m: `python tools/pipa_offshore.py` → `data/kapal/pipa_offshore.geojson` (jalankan ulang bila aset berubah).
   - Posisi kapal: `scripts/kapal.py` via workflow **Update Kapal AIS** (tiap 30 menit) dari **AISStream.io** → `data/kapal/oeja.json`. Posisi lama ≤ 90 menit dibawa ke run berikutnya (kapal berlabuh jarang memancar); waktu yang ditampilkan selalu waktu pesan AIS.
   - **Aktivasi:** daftar gratis di https://aisstream.io (login GitHub) → buat API key → GitHub repo *Settings → Secrets and variables → Actions → New repository secret* bernama `AISSTREAM_API_KEY`. Tanpa secret, workflow selesai tanpa mengubah data dan kartu menampilkan "Belum aktif".
2. **Peta live VesselFinder** — widget peta resmi VesselFinder (`aismap.js`), real-time. Peta pihak ketiga ini tidak bisa diberi overlay pipa. Dashboard tidak melakukan scraping VesselFinder (dilarang ketentuan mereka).

Keterbatasan: AIS terestrial tidak menjangkau semua perairan; kapal kecil/nelayan sering tidak memancarkan AIS. Modul ini pendukung keputusan, bukan pengganti patroli ROW laut atau koordinasi dengan KSOP/Distrik Navigasi.
