// Uji rumusan klasifikasi multi-bahaya (node tests/klasifikasi.test.js) — tanpa jaringan.
const assert = require('assert');
const K = require('../klasifikasi.js');

const now = Date.parse('2026-11-06T03:00:00Z');
const cuacaBasah = [{ area: 'A', lat: -1, lon: 117, rh: 90, prakiraan: [{ hujan_mm: 2 }, { hujan_mm: 3 }] }];
const cuacaKering = [{ area: 'A', lat: -1, lon: 117, rh: 45, prakiraan: [{ hujan_mm: 0 }, { hujan_mm: 1 }] }];
const hs = (o) => ({ area: 'A', lat: -1.001, lon: 117.001, jarak_km: 0.3, jarak_fasilitas_km: 5, conf: 'medium', status: 'kritis', aset: 'Pipa X', ...o });
const run = (hotspot, extra = {}) => K.hitung({ data: { areas: { A: {}, B: {} }, hotspot: { dekat_aset: hotspot }, cuaca: extra.cuaca || cuacaBasah, ...extra.data }, now, ...extra.args });

// 1. Ambang status
assert.strictEqual(K.statusDariR(15), 'kritis');
assert.strictEqual(K.statusDariR(8), 'waspada');
assert.strictEqual(K.statusDariR(3), 'pantau');
assert.strictEqual(K.statusDariR(2), 'normal');

// 2. Satu hotspot dekat pipa di lahan basah TIDAK otomatis Kritis (pipa tertanam K = 2)
let r = run([hs({ tutupan: { kategori: 'vegetasi_rendah' } })]);
assert.strictEqual(r.area.A.status, 'waspada');
assert.strictEqual(r.area.A.R, 8);
assert.strictEqual(r.area.B.status, 'normal', 'area lain tidak terpengaruh');

// 3. Hotspot di lahan non-vegetasi: P maks. 2 → tidak bisa Kritis walau dekat fasilitas, kering, keyakinan tinggi
r = run([hs({ conf: 'high', jarak_fasilitas_km: 0.4, tutupan: { kategori: 'non_vegetasi', label: 'Lahan terbangun' } })], { cuaca: cuacaKering });
assert.strictEqual(r.area.A.pemicu.P, 2);
assert.strictEqual(r.area.A.status, 'waspada'); // 2 × 4 = 8
assert.ok(r.area.A.temuan[0].ref.risiko.nonVegetasi);

// 4. Hotspot di hutan, kering, keyakinan tinggi, dekat fasilitas → Kritis
r = run([hs({ conf: 'high', jarak_fasilitas_km: 0.4, tutupan: { kategori: 'vegetasi_lebat' } })], { cuaca: cuacaKering });
assert.strictEqual(r.area.A.status, 'kritis');

// 5. Hotspot berulang → −2; sumber panas dikenal → P = 1
r = run([hs({ persisten: true, berulang_hari: 5 })]);
assert.strictEqual(r.area.A.pemicu.P, 2);
r = run([hs({})], { args: { dikenal: [{ nama: 'Flare', lat: -1.001, lon: 117.001, radius_km: 1 }] } });
assert.strictEqual(r.area.A.pemicu.P, 1);

// 6. Hotspot > 5 km tidak dinilai
r = run([hs({ jarak_km: 8, status: 'luar' })]);
assert.strictEqual(r.area.A.status, 'normal');

// 7. Gempa M ≥ 6 dekat → P 5; gempa lama / kecil diabaikan
const q = (o) => ({ mag: 6.2, datetime: '2026-11-06T01:00:00+07:00', kedalaman: '10 km', per_area: { A: { jarak_km: 30 } }, ...o });
r = run([], { data: { gempa: { terkini: [q({})] } } });
assert.strictEqual(r.area.A.R, 15);
assert.strictEqual(r.area.A.status, 'kritis');
r = run([], { data: { gempa: { terkini: [q({ datetime: '2026-10-30T01:00:00+07:00' }), q({ mag: 4.5 })] } } });
assert.strictEqual(r.area.A.status, 'normal');

// 8. Prakiraan PVMBG tinggi tanpa pemicu = P 2; dengan LHASA tinggi = P 5
const gt = { prakiraan: { per_area: { A: { terburuk: 'tinggi', pipa_km: { tinggi: 3 }, fasilitas: [], segmen: [] } } } };
r = run([], { data: { gerakan_tanah: gt } });
assert.strictEqual(r.area.A.R, 6);
r = run([], { data: { gerakan_tanah: gt }, args: { lhasa: { produk: { hari_ini: { per_area: { A: { terburuk: 'tinggi' } } } } } } });
assert.strictEqual(r.area.A.R, 15);

// 9. Eskalasi: dua jenis bahaya Waspada berdekatan → naik satu tingkat
const gt2 = { prakiraan: { per_area: { A: { terburuk: 'tinggi', pipa_km: { tinggi: 1 }, fasilitas: [{ aset: 'LBCV', level: 'tinggi', lat: -1.002, lon: 117.002 }], segmen: [] } } } };
r = run([hs({ tutupan: { kategori: 'vegetasi_rendah' } })], { data: { gerakan_tanah: gt2 } });
assert.strictEqual(r.area.A.R, 8);
assert.strictEqual(r.area.A.status, 'kritis');
assert.ok(r.area.A.eskalasi);

// 10. Modul tambahan berbasis lokasi aset (peringatan BMKG, banjir sungai, petir model, GDACS)
const tb = (tambahan) => run([], { args: { tambahan } });
const akan = new Date(now + 3.6e6).toISOString(), lewat = new Date(now - 3.6e6).toISOString();
r = tb({ peringatan: { aktif: [{ event: 'Hujan Lebat', severity: 'Moderate', berakhir: akan, per_area: { A: { pipa_km: 12.3, fasilitas: ['X'] } } }] } });
assert.strictEqual(r.area.A.R, 6); assert.strictEqual(r.area.A.status, 'pantau'); assert.strictEqual(r.area.A.pemicu.jenis, 'cuaca_ekstrem');
assert.strictEqual(r.area.B.status, 'normal', 'peringatan hanya berlaku untuk area yang asetnya terkena');
r = tb({ peringatan: { aktif: [{ event: 'Hujan Lebat', severity: 'Moderate', berakhir: lewat, per_area: { A: { pipa_km: 12.3 } } }] } });
assert.strictEqual(r.area.A.status, 'normal', 'peringatan kedaluwarsa tidak dihitung');
r = tb({ peringatan: { aktif: [{ event: 'Hujan Ekstrem', severity: 'Severe', berakhir: akan, per_area: { A: { pipa_km: 1 } } }] } });
assert.strictEqual(r.area.A.R, 8);
r = tb({ banjir: { per_area: { A: { terburuk: 'tinggi', sel: { tinggi: 2 }, pipa_km: { tinggi: 4.5 }, rasio_maks: 1.2 }, B: { terburuk: null } } } });
assert.strictEqual(r.area.A.R, 12); assert.strictEqual(r.area.A.status, 'waspada'); assert.strictEqual(r.area.B.status, 'normal');
r = tb({ banjir: { per_area: { A: { terburuk: 'rendah', sel: { rendah: 1 }, pipa_km: { rendah: 1 } } } } });
assert.strictEqual(r.area.A.R, 6);
r = tb({ petir: { produk: { hari_ini: { per_area: { A: { terburuk: 'tinggi', pipa_km: { tinggi: 30 }, n_fasilitas: { tinggi: 2 } } } }, besok: { per_area: { B: { terburuk: 'tinggi' } } } } } });
assert.strictEqual(r.area.A.R, 6); assert.strictEqual(r.area.B.status, 'normal', 'hanya produk hari ini yang dihitung');
r = tb({ petir: { produk: { hari_ini: { per_area: { A: { terburuk: 'rendah' } } } } } });
assert.strictEqual(r.area.A.status, 'normal');
r = tb({ gdacs: { kejadian: [{ jenis_nama: 'Siklon tropis', nama: 'X', level: 'Red', per_area: { A: { P: 5, ket: 'aset berada di zona angin ≥ 120 km/jam' }, B: { P: 0, ket: 'jauh' } } }] } });
assert.strictEqual(r.area.A.R, 15); assert.strictEqual(r.area.A.status, 'kritis'); assert.strictEqual(r.area.B.status, 'normal');
// modul tambahan tidak memicu eskalasi gabungan
r = run([hs({ tutupan: { kategori: 'vegetasi_rendah' } })], { args: { tambahan: { banjir: { per_area: { A: { terburuk: 'tinggi', sel: { tinggi: 1 }, pipa_km: { tinggi: 1 } } } } } } });
assert.strictEqual(r.area.A.R, 12); assert.strictEqual(r.area.A.status, 'waspada'); assert.strictEqual(r.area.A.eskalasi, null);

console.log('klasifikasi.test.js: semua uji lulus');
