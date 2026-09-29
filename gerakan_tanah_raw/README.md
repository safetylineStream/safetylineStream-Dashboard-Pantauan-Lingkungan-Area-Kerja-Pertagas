# File cadangan gerakan tanah (opsional)

Folder ini hanya dipakai bila layanan GIS ESDM One Map tidak bisa diakses otomatis.

| Nama file | Isi |
|---|---|
| `prakiraan.geojson` atau `prakiraan.zip` (SHP) | Peta Prakiraan Potensi Gerakan Tanah bulan berjalan |
| `zkgt.geojson` atau `zkgt.zip` (SHP) | Peta Zona Kerentanan Gerakan Tanah |

Peta ini bisa diunduh dari ESDM One Map (geoportal.esdm.go.id) atau Portal MBG PVMBG (vsi.esdm.go.id/portalmbg), atau diekspor dari QGIS.
Kolom atribut harus memuat teks tingkat potensi/kerentanan: *Tinggi*, *Menengah*, *Rendah*, atau *Sangat Rendah*.
Setelah diunggah, jalankan workflow **Update Data Harian**.
