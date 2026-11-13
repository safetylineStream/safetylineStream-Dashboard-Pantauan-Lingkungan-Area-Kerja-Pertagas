/*
 * klasifikasi.js — Rumusan klasifikasi status multi-bahaya PERTAGAS WASPADA.
 *
 *   Skor risiko R = P × K
 *     P (1–5) = peluang bahaya benar-benar mengenai aset dalam 24–72 jam (jarak hanya titik awal)
 *     K (1–5) = konsekuensi bila aset terkena (jenis aset)
 *   Status: Kritis R 15–25 · Waspada 8–14 · Pantau 3–7 · Normal 1–2
 *   Status AREA = R tertinggi di area itu (+ eskalasi bila ≥ 2 jenis bahaya Waspada berdekatan).
 *   Status satu area tidak memengaruhi area lain.
 *
 * Modul tambahan (peringatan dini BMKG, banjir sungai GloFAS, potensi petir model, GDACS) hanya dihitung
 * bila datanya mengenai/mendekati aset wilayah itu; skornya sengaja konservatif (maks. Waspada, kecuali
 * siklon tropis yang zona anginnya mengenai aset).
 *
 * Hotspot hanya dianggap titik api lahan bila berada di lahan bervegetasi. Hotspot di kawasan
 * terbangun/terbuka/air (ESA WorldCover) = sumber panas non-vegetasi: P maks. 2, tidak bisa memicu Kritis.
 * Api permukaan jarang merusak pipa tertanam → K hotspot = 2 untuk pipa, 4 untuk fasilitas ≤ 1 km.
 *
 * Dipakai di index.html (browser) dan diuji dengan Node (tests/klasifikasi.test.js).
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KlasifikasiWaspada = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const ATURAN = {
    ambang: { kritis: 15, waspada: 8, pantau: 3 },
    hotspot: {
      radiusMaksKm: 5,
      jarak: [[0.5, 4], [1, 3], [3, 2], [5, 1]],             // [batas km, P dasar]
      keyakinan: { high: 1, medium: 0, low: -1 },
      kering: { rhMaks: 60, hujan2HariMaks: 5 },              // +1
      hujanBasahMm: 20,                                       // −1 (prakiraan hari ini)
      berulang: -2,
      klaster: { radiusKm: 2, minTitikLain: 2 },              // ≥ 3 titik baru dalam 2 km → +1
      lahan: { vegetasi_lebat: 1, vegetasi_rendah: 0 },
      nonVegetasiMaksP: 2,
      K: { pipa: 2, fasilitas: 4, fasilitasKm: 1 },
    },
    gempa: { magMin: 5, jam: 72, K: 3 },
    gerakanTanah: { K: { pipa: 3, fasilitas: 4 }, hujanPemicuMm: 50 },
    cuaca: { K: 2 },
    // Modul tambahan (semua berbasis lokasi aset). Tidak ikut aturan gabungan.
    peringatan: { P: { biasa: 3, parah: 4 }, K: 2 },          // peringatan dini BMKG aktif yang poligonnya mengenai aset
    banjir: { P: { tinggi: 4, sedang: 3, rendah: 2 }, K: 3 }, // model GloFAS di sel sungai yang dilintasi pipa
    petir: { P: { tinggi: 3, sedang: 2 }, K: 2 },             // prakiraan model, bukan deteksi sambaran
    gdacs: { K: 3 },                                          // P dihitung pipeline (scripts/gdacs.py)
    tanpaGabungan: ['cuaca', 'cuaca_ekstrem', 'banjir', 'petir', 'gdacs'],
    gabungan: { radiusKm: 5 },
  };
  const URUT = { normal: 0, pantau: 1, waspada: 2, kritis: 3 };
  const NAIK = { normal: 'pantau', pantau: 'waspada', waspada: 'kritis', kritis: 'kritis' };

  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const num = v => (v === null || v === undefined || v === '' || !Number.isFinite(+v) ? null : +v);
  const f = (v, d = 1) => Number(v).toLocaleString('id-ID', { maximumFractionDigits: d, minimumFractionDigits: d });
  function hav(a1, o1, a2, o2) {
    const R = Math.PI / 180, p1 = a1 * R, p2 = a2 * R, dp = p2 - p1, dl = (o2 - o1) * R;
    const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return 2 * 6371.0088 * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function statusDariR(R) {
    const a = ATURAN.ambang;
    return R >= a.kritis ? 'kritis' : R >= a.waspada ? 'waspada' : R >= a.pantau ? 'pantau' : 'normal';
  }
  const hujanHari = (w, i) => num(w?.prakiraan?.[i]?.hujan_mm) ?? 0;
  function titikCuacaTerdekat(cuaca, lat, lon, area) {
    const c = (cuaca || []).filter(w => num(w.lat) != null && num(w.lon) != null);
    const pool = c.filter(w => w.area === area);
    let best = null, bd = Infinity;
    for (const w of (pool.length ? pool : c)) { const d = hav(lat, lon, +w.lat, +w.lon); if (d < bd) { bd = d; best = w; } }
    return best;
  }

  /* ------------------------------------------------------------ hotspot */
  function nilaiHotspot(h, ctx) {
    const A = ATURAN.hotspot, j = num(h.jarak_km);
    if (j == null || j > A.radiusMaksKm) return { P: 0, K: 0, R: 0, status: 'luar', faktor: [], lahan: h.tutupan?.kategori || null };
    const faktor = [];
    let P = (A.jarak.find(([b]) => j <= b) || [0, 1])[1];
    faktor.push({ teks: `jarak ${f(j, 2)} km`, nilai: P, dasar: true });
    const dk = A.keyakinan[String(h.conf || '').toLowerCase()];
    if (dk) { P += dk; faktor.push({ teks: `keyakinan satelit ${h.conf}`, nilai: dk }); }
    const w = titikCuacaTerdekat(ctx.cuaca, +h.lat, +h.lon, h.area);
    if (w) {
      const rh = num(w.rh), h2 = hujanHari(w, 0) + hujanHari(w, 1);
      if (rh != null && rh < A.kering.rhMaks && h2 < A.kering.hujan2HariMaks) {
        P += 1; faktor.push({ teks: `kondisi kering (RH ${f(rh, 0)}%, hujan 2 hari ${f(h2)} mm)`, nilai: 1 });
      }
      if (hujanHari(w, 0) >= A.hujanBasahMm) { P -= 1; faktor.push({ teks: `prakiraan hujan ${f(hujanHari(w, 0))} mm`, nilai: -1 }); }
    }
    if (h.persisten) {
      P += A.berulang; faktor.push({ teks: `berulang ${h.berulang_hari} hari di lokasi sama (indikasi sumber panas tetap)`, nilai: A.berulang });
    } else {
      const lain = ctx.hotspot.filter(o => o !== h && !o.persisten && num(o.jarak_km) != null && +o.jarak_km <= A.radiusMaksKm &&
        hav(+h.lat, +h.lon, +o.lat, +o.lon) <= A.klaster.radiusKm).length;
      if (lain >= A.klaster.minTitikLain) { P += 1; faktor.push({ teks: `klaster ${lain + 1} titik dalam ${A.klaster.radiusKm} km`, nilai: 1 }); }
    }
    const kat = h.tutupan?.kategori || null;
    if (kat && A.lahan[kat]) { P += A.lahan[kat]; faktor.push({ teks: `lahan ${kat === 'vegetasi_lebat' ? 'bervegetasi lebat' : kat}`, nilai: A.lahan[kat] }); }
    P = clamp(P, 1, 5);
    if (kat === 'non_vegetasi' && P > A.nonVegetasiMaksP) {
      faktor.push({ teks: `lahan non-vegetasi (${h.tutupan.label || 'terbangun/terbuka'}) → P maks. ${A.nonVegetasiMaksP}`, nilai: A.nonVegetasiMaksP - P });
      P = A.nonVegetasiMaksP;
    }
    if (!kat) faktor.push({ teks: 'tutupan lahan belum diketahui', nilai: 0 });
    const kenal = (ctx.dikenal || []).find(s => num(s.lat) != null && hav(+h.lat, +h.lon, +s.lat, +s.lon) <= (num(s.radius_km) ?? 1));
    if (kenal) { faktor.push({ teks: `sumber panas dikenal: ${kenal.nama || 'terverifikasi'}`, nilai: 1 - P }); P = 1; }
    const fasKm = num(h.jarak_fasilitas_km);
    const K = fasKm != null && fasKm <= A.K.fasilitasKm ? A.K.fasilitas : A.K.pipa;
    const R = P * K;
    return { P, K, R, status: statusDariR(R), faktor, lahan: kat, nonVegetasi: kat === 'non_vegetasi', dikenal: !!kenal };
  }

  /* ------------------------------------------------------------ gempa */
  function gempaDihitung(data, now) {
    const g = data.gempa || {}, seen = new Set(), out = [];
    for (const q of [g.terbaru, ...(g.terkini || []), ...(g.dirasakan || [])]) {
      if (!q || num(q.mag) == null) continue;
      const k = q.datetime || `${q.lat},${q.lon},${q.mag}`;
      if (seen.has(k)) continue; seen.add(k);
      const t = Date.parse(q.datetime);
      if (!Number.isFinite(t) || (now - t) / 3.6e6 > ATURAN.gempa.jam || +q.mag < ATURAN.gempa.magMin) continue;
      out.push(q);
    }
    return out;
  }
  function pGempa(M, j, tsunami, kedalaman) {
    let P = 0;
    if (tsunami || (M >= 6 && j <= 50)) P = 5;
    else if ((M >= 6 && j <= 100) || (M >= 5 && j <= 25)) P = 4;
    else if (M >= 5 && j <= 75) P = 3;
    else if (M >= 5 && j <= 150) P = 2;
    else if (M >= 5 && j <= 300) P = 1;
    const dz = parseInt(String(kedalaman ?? '').replace(/[^\d]/g, ''), 10);
    if (P && !tsunami && Number.isFinite(dz) && dz > 100) P -= 1;
    return P;
  }

  /* ------------------------------------------------------------ utama */
  function hitung({ data, lhasa, ruasLhasa, dikenal, now, tambahan }) {
    data = data || {};
    now = now instanceof Date ? now.getTime() : (now || Date.now());
    const hs = (data.hotspot && data.hotspot.dekat_aset) || [];
    const areas = Object.keys(data.areas || {});
    const today = new Date(now + 7 * 3.6e6).toISOString().slice(0, 10);
    const ctx = {
      cuaca: data.cuaca || [], hotspot: hs,
      dikenal: (dikenal || []).filter(s => !s.berlaku_sampai || String(s.berlaku_sampai) >= today),
    };
    const temuan = Object.fromEntries(areas.map(a => [a, []]));
    const tambah = (a, t) => { (temuan[a] = temuan[a] || []).push({ ...t, R: t.P * t.K, status: statusDariR(t.P * t.K) }); };

    // A. Hotspot
    for (const h of hs) {
      const r = nilaiHotspot(h, ctx);
      h.risiko = r;
      if (r.status === 'luar') continue;
      const lahan = h.tutupan ? `, ${String(h.tutupan.label || '').split(' (')[0].toLowerCase()}` : '';
      tambah(h.area, { jenis: 'hotspot', P: r.P, K: r.K, lokasi: [[+h.lat, +h.lon]], ref: h,
        teks: `Hotspot ${f(+h.jarak_km, 2)} km dari ${h.aset}${lahan}${h.persisten ? `, berulang ${h.berulang_hari} hari` : ''}` });
    }
    // B. Gempa & tsunami
    for (const q of gempaDihitung(data, now)) {
      for (const [a, p] of Object.entries(q.per_area || {})) {
        const j = num(p.jarak_km); if (j == null) continue;
        const P = pGempa(+q.mag, j, !!q.tsunami && j <= 300, q.kedalaman);
        if (P) tambah(a, { jenis: 'gempa', P, K: ATURAN.gempa.K, lokasi: null,
          teks: `${q.tsunami ? 'Gempa berpotensi tsunami' : 'Gempa'} M ${f(+q.mag)} berjarak ${f(j, 0)} km dari aset` });
      }
    }
    // C. Gerakan tanah (PVMBG) & longsor (LHASA)
    const PK = (data.gerakan_tanah && data.gerakan_tanah.prakiraan) || {};
    const LV = { tinggi: 2, sedang: 1 };
    for (const a of areas) {
      const pk = (PK.per_area || {})[a] || {};
      const ter = pk.terburuk, tinggi = ter === 'tinggi' || ter === 'bandang';
      const la = ['hari_ini', 'besok'].map(k => lhasa?.produk?.[k]?.per_area?.[a]?.terburuk || null)
        .reduce((x, y) => ((LV[y] || 0) > (LV[x] || 0) ? y : x), null);
      const hujanMaks = Math.max(0, ...ctx.cuaca.filter(w => w.area === a).map(w => hujanHari(w, 0)));
      let P = 0;
      if (la === 'tinggi' && tinggi) P = 5;
      else if (la === 'tinggi' || (la === 'sedang' && tinggi)) P = 4;
      else if (la === 'sedang' || (tinggi && hujanMaks >= ATURAN.gerakanTanah.hujanPemicuMm)) P = 3;
      else if (tinggi) P = 2;
      else if (ter === 'menengah') P = 1;
      if (!P) continue;
      const fas = (pk.fasilitas || []).filter(x => x.level === 'tinggi' || x.level === 'bandang');
      const km = pk.pipa_km || {};
      const seg = (pk.segmen || []).filter(s => tinggi ? (s.level === 'tinggi' || s.level === 'bandang') : s.level === ter);
      let teks;
      if (tinggi) {
        const kmv = (num(km.tinggi) || 0) + (num(km.bandang) || 0);
        teks = `${fas.length ? fas.map(x => x.aset).join(', ') + ' dan ' : ''}${f(kmv)} km pipa di zona prakiraan ${ter === 'tinggi' ? 'gerakan tanah tinggi' : 'banjir bandang'} PVMBG`;
      } else teks = `${f(num(km.menengah) || 0)} km pipa di zona prakiraan gerakan tanah menengah PVMBG`;
      if (la) teks += ` · model longsor LHASA ${la}`;
      // lokasi temuan = ruas zona PVMBG tinggi/bandang + ruas berindikasi longsor LHASA (untuk aturan gabungan)
      const lok = (tinggi ? [...seg, ...fas] : []).filter(s => num(s.lat) != null).map(s => [+s.lat, +s.lon]);
      if (la) for (const ft of (ruasLhasa && ruasLhasa.features) || []) {
        const pr = ft.properties || {};
        if (pr.area !== a || (LV[pr.level] || 0) < 1 || !['hari_ini', 'besok'].includes(pr.produk || 'hari_ini')) continue;
        for (const c of (ft.geometry && ft.geometry.coordinates) || []) if (Array.isArray(c) && c.length >= 2) lok.push([+c[1], +c[0]]);
      }
      tambah(a, { jenis: 'gerakan_tanah', P, K: fas.length ? ATURAN.gerakanTanah.K.fasilitas : ATURAN.gerakanTanah.K.pipa,
        lokasi: lok.length ? lok : null, teks });
    }
    // D. Cuaca & kualitas udara (dampak pada pekerjaan lapangan → K = 2)
    for (const a of areas) {
      let best = null;
      for (const w of ctx.cuaca.filter(w => w.area === a)) {
        const hj = hujanHari(w, 0), ang = Math.max(num(w.prakiraan?.[0]?.angin_max) || 0, num(w.hembusan_kmh) || 0), aqi = num(w.aqi_us) || 0;
        const c = [];
        if (hj > 150) c.push([5, `Prakiraan hujan ekstrem ${f(hj)} mm di ${w.nama}`]);
        else if (hj >= 100) c.push([4, `Prakiraan hujan sangat lebat ${f(hj)} mm di ${w.nama}`]);
        else if (hj >= 50) c.push([3, `Prakiraan hujan lebat ${f(hj)} mm di ${w.nama}`]);
        else if (hj >= 20) c.push([2, `Prakiraan hujan ${f(hj)} mm hari ini di ${w.nama}`]);
        if (ang >= 45) c.push([4, `Angin ${f(ang, 0)} km/jam di ${w.nama}`]);
        if (aqi > 150) c.push([3, `Kualitas udara tidak sehat (US AQI ${f(aqi, 0)}) di ${w.nama}`]);
        else if (aqi > 100) c.push([2, `Kualitas udara kurang sehat (US AQI ${f(aqi, 0)}) di ${w.nama}`]);
        for (const x of c) if (!best || x[0] > best.P || (x[0] === best.P && aqi > best.aqi)) best = { P: x[0], teks: x[1], aqi, lokasi: [[+w.lat, +w.lon]] };
      }
      if (best) tambah(a, { jenis: 'cuaca', P: best.P, K: ATURAN.cuaca.K, lokasi: null, teks: best.teks });
    }

    // E–H. Modul tambahan berbasis lokasi aset
    const T = tambahan || {};
    const jamWIB = iso => { const t = Date.parse(iso); return Number.isFinite(t) ? new Date(t + 7 * 3.6e6).toISOString().slice(11, 16) + ' WIB' : ''; };
    // E. Peringatan dini cuaca ekstrem BMKG yang masih berlaku dan poligonnya mengenai aset
    for (const w of (T.peringatan && T.peringatan.aktif) || []) {
      const akhir = Date.parse(w.berakhir);
      if (Number.isFinite(akhir) && akhir < now) continue;
      const parah = /severe|extreme/i.test(w.severity || '');
      for (const [a, v] of Object.entries(w.per_area || {})) {
        if (!(a in temuan)) continue;
        const nf = v.n_fasilitas || (v.fasilitas || []).length;
        tambah(a, { jenis: 'cuaca_ekstrem', P: parah ? ATURAN.peringatan.P.parah : ATURAN.peringatan.P.biasa, K: ATURAN.peringatan.K, lokasi: null,
          teks: `Peringatan dini BMKG: ${w.event || w.judul} — ${f(num(v.pipa_km) || 0)} km pipa${nf ? ` & ${nf} fasilitas` : ''} di wilayah peringatan${w.berakhir ? ` (s.d. ${jamWIB(w.berakhir)})` : ''}` });
      }
    }
    // F. Banjir sungai (model GloFAS) di sel sungai yang dilintasi pipa
    for (const [a, v] of Object.entries((T.banjir && T.banjir.per_area) || {})) {
      const P = ATURAN.banjir.P[v.terburuk];
      if (!P || !(a in temuan)) continue;
      const n = (v.sel || {})[v.terburuk] || 0, km = (v.pipa_km || {})[v.terburuk] || 0;
      tambah(a, { jenis: 'banjir', P, K: ATURAN.banjir.K, lokasi: null,
        teks: `Model banjir sungai ${v.terburuk}: ${n} sel sungai, ${f(km)} km pipa${num(v.rasio_maks) != null ? ` (puncak ${f(v.rasio_maks, 2)}× maksimum 5 tahun)` : ''}` });
    }
    // G. Potensi petir hari ini (prakiraan model)
    for (const [a, v] of Object.entries((T.petir && T.petir.produk && T.petir.produk.hari_ini && T.petir.produk.hari_ini.per_area) || {})) {
      const P = ATURAN.petir.P[v.terburuk];
      if (!P || !(a in temuan)) continue;
      const nf = (v.n_fasilitas || {})[v.terburuk] || 0;
      tambah(a, { jenis: 'petir', P, K: ATURAN.petir.K, lokasi: null,
        teks: `Potensi petir ${v.terburuk} hari ini (model): ${f((v.pipa_km || {})[v.terburuk] || 0)} km pipa${nf ? ` & ${nf} fasilitas` : ''}` });
    }
    // H. Siklon tropis & bencana besar (GDACS)
    for (const k of (T.gdacs && T.gdacs.kejadian) || []) {
      for (const [a, v] of Object.entries(k.per_area || {})) {
        const P = clamp(num(v.P) || 0, 0, 5);
        if (!P || !(a in temuan)) continue;
        tambah(a, { jenis: 'gdacs', P, K: ATURAN.gdacs.K, lokasi: null, teks: `${k.jenis_nama}${k.nama ? ' ' + k.nama : ''} (GDACS ${k.level}): ${v.ket}` });
      }
    }

    // Status area
    const area = {};
    for (const a of areas) {
      const t = (temuan[a] || []).sort((x, y) => y.R - x.R || URUT[y.status] - URUT[x.status]);
      const R = t.length ? t[0].R : 0;
      let status = R ? statusDariR(R) : 'normal', eskalasi = null;
      // cuaca tidak ikut aturan gabungan: pengaruhnya sudah masuk ke P hotspot (kering/hujan) dan P gerakan tanah (hujan)
      const w = t.filter(x => x.R >= ATURAN.ambang.waspada && !ATURAN.tanpaGabungan.includes(x.jenis));
      const jenis = [...new Set(w.map(x => x.jenis))];
      if (jenis.length >= 2) {
        const dekat = (x, y) => !x.lokasi || !y.lokasi || !x.lokasi.length || !y.lokasi.length ||
          x.lokasi.some(p => y.lokasi.some(q => hav(p[0], p[1], q[0], q[1]) <= ATURAN.gabungan.radiusKm));
        const pasang = w.some((x, i) => w.some((y, k) => k > i && x.jenis !== y.jenis && dekat(x, y)));
        if (pasang) { eskalasi = `${jenis.length} jenis bahaya ≥ Waspada berdekatan (${jenis.join(', ')})`; status = NAIK[status]; }
      }
      area[a] = { status, R, pemicu: t[0] || null, temuan: t, eskalasi };
    }
    const rekap = { kritis: [], waspada: [], pantau: [], normal: [] };
    for (const a of areas) rekap[area[a].status].push(a);
    return { aturan: ATURAN, area, rekap };
  }

  return { ATURAN, URUT, hitung, nilaiHotspot, statusDariR, pGempa };
});
