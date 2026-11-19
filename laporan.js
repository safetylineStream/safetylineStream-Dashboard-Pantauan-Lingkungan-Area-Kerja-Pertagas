/*
 * laporan.js — "Unduh laporan saat ini" (PDF) untuk PERTAGAS WASPADA.
 *
 *   • Tab Ringkasan  → Rangkuman 8 area operasi untuk direksi/manajemen.
 *   • Tab wilayah    → Laporan detail area operasi tersebut.
 *
 * PDF dibuat seketika di browser dari data yang sedang tampil di dashboard (DATA, KLAS, LS, PD, PT, BJ, GD,
 * HIST, ASSETS) — tanpa server. Pustaka jsPDF + AutoTable dan font Carlito (OFL) dimuat dari folder
 * vendor/ & fonts/ hanya saat tombol diklik pertama kali.
 *
 * Bergantung pada variabel & fungsi global di index.html (dimuat setelah skrip utama).
 */
(function () {
  'use strict';

  const LIB = { jspdf: 'vendor/jspdf.umd.min.js', autotable: 'vendor/jspdf.plugin.autotable.min.js' };
  const FONT = { normal: 'fonts/Carlito-Regular.ttf', bold: 'fonts/Carlito-Bold.ttf' };
  const LOGO = 'img/logo-pertamina-gas.png';

  // ---------------------------------------------------------------- warna & tata letak (mm, A4 tegak)
  const C = {
    blue: [11, 78, 162], navy: [11, 58, 110], green: [140, 191, 63], red: [215, 38, 46],
    ink: [29, 35, 48], grey: [95, 102, 114], line: [217, 221, 227], panel: [244, 245, 247], rowhi: [253, 236, 236],
    kritis: [198, 40, 40], waspada: [232, 116, 59], pantau: [240, 180, 41], normal: [46, 125, 50], luar: [154, 161, 171],
    pipa: [11, 78, 162], ungu: [123, 31, 162],
  };
  const ST = {
    kritis: { label: 'KRITIS', bg: C.kritis, fg: [255, 255, 255] },
    waspada: { label: 'WASPADA', bg: C.waspada, fg: [255, 255, 255] },
    pantau: { label: 'PANTAU', bg: C.pantau, fg: C.ink },
    normal: { label: 'NORMAL', bg: C.normal, fg: [255, 255, 255] },
  };
  const PW = 210, PH = 297, M = 15, CW = PW - 2 * M, TOP1 = 12, TOPN = 24, BOT = PH - 18, IND = 7;
  const JL = { hotspot: 'Hotspot', gempa: 'Gempa/tsunami', gerakan_tanah: 'Gerakan tanah/longsor', cuaca: 'Cuaca/udara',
    cuaca_ekstrem: 'Peringatan dini BMKG', banjir: 'Banjir sungai (model)', petir: 'Petir (model)', gdacs: 'Siklon/bencana (GDACS)' };
  const LVL = { tinggi: 'Tinggi', sedang: 'Sedang', rendah: 'Rendah', menengah: 'Menengah', bandang: 'Banjir bandang', sangat_rendah: 'Sangat rendah' };

  // ---------------------------------------------------------------- utilitas
  const G = n => { try { return window.eval(n); } catch (e) { return undefined; } }; // akses let/const global index.html
  const num = v => (v === null || v === undefined || v === '' || !Number.isFinite(+v) ? null : +v);
  const nf = (v, d = 0) => (num(v) == null ? '–' : Number(v).toLocaleString('id-ID', { maximumFractionDigits: d, minimumFractionDigits: d }));
  const koor = (la, lo) => `${nf(la, 4)}; ${nf(lo, 4)}`;
  const tz = { timeZone: 'Asia/Jakarta' };
  function tglPanjang(d) { return d.toLocaleDateString('id-ID', { ...tz, weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }); }
  function jamWIB(d) { return d.toLocaleTimeString('id-ID', { ...tz, hour: '2-digit', minute: '2-digit' }).replace('.', ':') + ' WIB'; }
  function parseT(v) { const f = G('parseT'); return f ? f(v) : (v ? new Date(v) : null); }
  function waktu(iso) { const d = parseT(iso); return d ? `${d.toLocaleDateString('id-ID', { ...tz, day: 'numeric', month: 'short' })} ${jamWIB(d)}` : '–'; }
  function hariTgl(t) { return t ? new Date(t + 'T00:00:00+07:00').toLocaleDateString('id-ID', { ...tz, weekday: 'short', day: 'numeric', month: 'short' }) : '–'; }
  // teks dari data: buang karakter di luar font (emoji, simbol) agar tidak tampil sebagai kotak kosong
  const bersih = s => String(s ?? '').replace(/[▲◆✓↻⚠○◐▣]/g, '').replace(/[^\u0009\u000A -ſ‐-›←-↓−≈≠≤≥■-●]/g, '').replace(/\s+/g, ' ').trim();

  function muatSkrip(src) {
    return new Promise((ok, gagal) => {
      const s = document.createElement('script'); s.src = src; s.async = true;
      s.onload = ok; s.onerror = () => gagal(new Error('gagal memuat ' + src)); document.head.appendChild(s);
    });
  }
  async function b64(url) {
    const r = await fetch(url, { cache: 'force-cache' });
    if (!r.ok) throw new Error(`gagal memuat ${url} (HTTP ${r.status})`);
    const buf = new Uint8Array(await r.arrayBuffer());
    let s = ''; for (let i = 0; i < buf.length; i += 0x8000) s += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    return btoa(s);
  }
  let ASET = null;
  async function siapkan() {
    if (!(window.jspdf && window.jspdf.jsPDF)) await muatSkrip(LIB.jspdf);
    if (!window.jspdf.jsPDF.API.autoTable) await muatSkrip(LIB.autotable);
    if (!ASET) {
      const [n, b, l] = await Promise.all([b64(FONT.normal), b64(FONT.bold), b64(LOGO).catch(() => null)]);
      ASET = { n, b, logo: l };
    }
  }

  // ---------------------------------------------------------------- pengumpulan data
  function konteks() {
    const DATA = G('DATA') || {}, KLAS = G('KLAS'), AREAS = G('AREAS') || [];
    const hs = (DATA.hotspot && DATA.hotspot.dekat_aset) || [];
    const hsSt = h => (h.risiko && h.risiko.status) || h.status;
    const quakesFn = G('quakes'), umur = G('umurJam');
    const qs = quakesFn ? quakesFn('all').filter(g => { const a = umur(g.datetime); return a != null && a <= 24 * 7; }) : [];
    return {
      DATA, KLAS, AREAS, hs, hsSt, qs,
      LS: G('LS'), PD: G('PD'), PT: G('PT'), BJ: G('BJ'), GD: G('GD'), HIST: G('HIST') || [], ASSETS: G('ASSETS') || { features: [] },
      TL: G('TL') || {}, sumber: (G('sources') || (() => []))(),
      pdArea: G('pdArea') || (() => []), lsArea: (k, p) => ((((G('LS') || {}).produk || {})[p] || {}).per_area || {})[k] || null,
      ptArea: (k, p) => ((((G('PT') || {}).produk || {})[p] || {}).per_area || {})[k] || null,
      bjArea: k => { const B = G('BJ'); return (B && B.per_area && B.per_area[k]) || null; },
      now: new Date(),
    };
  }
  function ringkasArea(X, k) {
    const D = X.DATA, A = (X.KLAS && X.KLAS.area && X.KLAS.area[k]) || { status: 'normal', R: 0, temuan: [] };
    const h = X.hs.filter(x => x.area === k && x.status !== 'luar');
    const by = s => h.filter(x => x.status === s).length;
    const wx = (D.cuaca || []).filter(w => w.area === k);
    const aqi = wx.map(w => num(w.aqi_us)).filter(v => v != null);
    const hj = wx.map(w => num(w.prakiraan && w.prakiraan[0] && w.prakiraan[0].hujan_mm)).filter(v => v != null);
    const q = X.qs.filter(g => g.per_area && g.per_area[k]).sort((a, b) => a.per_area[k].jarak_km - b.per_area[k].jarak_km)[0] || null;
    const P = ((D.gerakan_tanah || {}).prakiraan || {}), pk = (P.per_area || {})[k] || null;
    const gd = ((X.GD && X.GD.kejadian) || []).filter(e => e.per_area && e.per_area[k] && (num(e.per_area[k].P) || 0) > 0);
    return {
      k, nama: (D.areas[k] || {}).nama || k, A, status: A.status || 'normal', R: A.R || 0,
      h, n1: by('kritis'), n3: by('waspada'), n5: by('pantau'),
      nRisk: h.filter(x => ['kritis', 'waspada'].includes(X.hsSt(x))).length,
      nNon: h.filter(x => x.risiko && x.risiko.nonVegetasi).length, nRep: h.filter(x => x.persisten).length,
      wx, aqiMax: aqi.length ? Math.max(...aqi) : null, aqiMin: aqi.length ? Math.min(...aqi) : null,
      hjMin: hj.length ? Math.min(...hj) : null, hjMax: hj.length ? Math.max(...hj) : null,
      q, pk, P, lsH: X.lsArea(k, 'hari_ini'), lsB: X.lsArea(k, 'besok'),
      pd: X.pdArea(k), pt: X.ptArea(k, 'hari_ini'), ptB: X.ptArea(k, 'besok'), bj: X.bjArea(k), gd,
      provinsi: (D.hotspot && D.hotspot.provinsi_area && D.hotspot.provinsi_area[k]) || {},
    };
  }
  const rentang = (a, b, d = 0, u = '') => (a == null ? '–' : (nf(a, d) === nf(b, d) ? nf(a, d) : `${nf(a, d)}–${nf(b, d)}`) + u);
  const kmLv = (o, lv) => num(o && o.pipa_km && o.pipa_km[lv]) || 0;
  const aqiKat = v => v == null ? '' : v <= 50 ? 'baik' : v <= 100 ? 'sedang' : v <= 150 ? 'tidak sehat bagi kelompok sensitif' : v <= 200 ? 'tidak sehat' : v <= 300 ? 'sangat tidak sehat' : 'berbahaya';
  function lokasiHs(h) { return bersih([h.desa, h.kec, h.kab].filter(Boolean).join(', ').replace(/Kelurahan |Desa /g, '')); }
  // temuan hotspot yang berdekatan (≤ 3 km, satu area) digabung menjadi satu klaster agar tabel & rekomendasi tidak berulang
  function kelompok(temuan) {
    const hav = (a, b) => { const r = Math.PI / 180, q = Math.sin((b.lat - a.lat) * r / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin((b.lon - a.lon) * r / 2) ** 2; return 12742 * Math.asin(Math.sqrt(q)); };
    const out = [], kl = [];
    for (const t of temuan) {
      if (t.jenis !== 'hotspot' || !t.ref) { out.push({ ...t, n: 1 }); continue; }
      const h = t.ref, p = { lat: +h.lat, lon: +h.lon }, area = t.area || h.area;
      const kena = kl.filter(c => c.area === area && c.anggota.some(x => hav(p, x.p) <= 3));
      let c = kena[0];
      if (!c) { c = { area, anggota: [] }; kl.push(c); }
      for (const o of kena.slice(1)) { c.anggota.push(...o.anggota); kl.splice(kl.indexOf(o), 1); }
      c.anggota.push({ t, p });
    }
    for (const c of kl) {
      const ts = c.anggota.map(x => x.t).sort((a, b) => b.R - a.R || a.ref.jarak_km - b.ref.jarak_km), u = ts[0], h = u.ref, js = ts.map(x => +x.ref.jarak_km);
      const n = ts.length, rep = ts.filter(x => x.ref.persisten).length;
      out.push({ ...u, n, jMin: Math.min(...js), jMax: Math.max(...js), lokasiTeks: lokasiHs(h),
        teks: n > 1 ? `${n} hotspot ${nf(Math.min(...js), 2)}–${nf(Math.max(...js), 2)} km dari ${bersih(h.aset)} (${lokasiHs(h).split(',').slice(0, 2).join(',')})${rep ? `, ${rep} berulang` : ''}` : `${bersih(u.teks)} (${lokasiHs(h).split(',').slice(0, 2).join(',')})` });
    }
    return out.sort((a, b) => b.R - a.R || (b.n || 1) - (a.n || 1));
  }

  // ---------------------------------------------------------------- penyusun dokumen
  class Laporan {
    constructor(judulPendek, judulDoc) {
      const { jsPDF } = window.jspdf;
      this.d = new jsPDF({ unit: 'mm', format: 'a4', compress: true });
      const d = this.d;
      d.addFileToVFS('Carlito-Regular.ttf', ASET.n); d.addFont('Carlito-Regular.ttf', 'Carlito', 'normal');
      d.addFileToVFS('Carlito-Bold.ttf', ASET.b); d.addFont('Carlito-Bold.ttf', 'Carlito', 'bold');
      d.setFont('Carlito', 'normal');
      d.setProperties({ title: judulDoc, author: 'Fungsi HSSE PT Pertamina Gas', subject: 'PERTAGAS WASPADA', creator: 'PERTAGAS WASPADA' });
      this.judulPendek = judulPendek; this.y = TOP1;
    }
    f(st = 'normal', size = 9.6, col = C.ink) { this.d.setFont('Carlito', st); this.d.setFontSize(size); this.d.setTextColor(...col); }
    halamanBaru() { this.d.addPage(); this.y = TOPN; }
    perlu(h) { if (this.y + h > BOT) this.halamanBaru(); }
    spasi(mm) { this.y += mm; }

    // teks kaya sederhana: **tebal**; mengembalikan tinggi
    teks(str, x, w, o = {}) {
      const d = this.d, size = o.size || 9.6, lh = (o.lh || 1.32) * size * 0.3528, col = o.col || C.ink;
      const tok = []; String(str).split('**').forEach((s, i) => s.split(/(\s+)/).forEach(p => p && tok.push({ t: p, b: i % 2 === 1 || !!o.bold })));
      const lines = []; let cur = [], cw = 0;
      const lebar = t => { d.setFont('Carlito', t.b ? 'bold' : 'normal'); d.setFontSize(size); return d.getTextWidth(t.t); };
      for (const t of tok) {
        const sp = /^\s+$/.test(t.t);
        if (sp) { if (cur.length) { cur.push({ t: ' ', b: t.b }); cw += lebar({ t: ' ', b: t.b }); } continue; }
        const tw = lebar(t);
        if (cw + tw > w && cur.length) { while (cur.length && cur[cur.length - 1].t === ' ') cur.pop(); lines.push(cur); cur = []; cw = 0; }
        cur.push(t); cw += tw;
      }
      if (cur.length) lines.push(cur);
      if (o.ukurSaja) return lines.length * lh;
      let yy = this.y + size * 0.3528 * 0.8;
      for (const ln of lines) {
        let xx = x;
        for (const t of ln) { this.f(t.b ? 'bold' : 'normal', size, col); d.text(t.t, xx, yy); xx += lebar(t); }
        yy += lh;
      }
      return lines.length * lh;
    }
    para(str, o = {}) {
      const x = M + (o.indent ?? IND), w = CW - (o.indent ?? IND);
      const h = this.teks(str, x, w, { ...o, ukurSaja: true }); this.perlu(h);
      this.teks(str, x, w, o); this.y += h + (o.after ?? 1.2);
    }
    butir(items, o = {}) {
      for (const it of items) {
        if (!it) continue;
        const x = M + IND + 4.5, w = CW - IND - 4.5, size = o.size || 9.6;
        const h = this.teks(it, x, w, { size, ukurSaja: true }); this.perlu(h + 0.5);
        this.f('normal', size, C.ink); this.d.text('•', M + IND + 0.6, this.y + size * 0.3528 * 0.8);
        this.teks(it, x, w, { size }); this.y += h + 1.1;
      }
    }
    h1(n, t) {
      this.perlu(44); this.y += 4;
      this.f('bold', 13.5, C.blue); this.d.text(`${n}. ${t.toUpperCase()}`, M, this.y + 4.6);
      this.y += 7.6;
    }
    h2(n, t, st) {
      this.perlu(24); this.y += 1.6;
      this.f('bold', 9.8, C.blue); this.d.text(String(n), M, this.y + 3.5);
      const nw = this.d.getTextWidth(String(n)); this.f('bold', 9.8, C.ink); this.d.text(t.toUpperCase(), M + Math.max(IND, nw + 2.2), this.y + 3.5);
      if (st) this.lencana(st, M + CW - 21, this.y - 0.2, 21, 5);
      this.y += 6;
    }
    lencana(st, x, y, w = 21, h = 5) {
      const s = ST[st] || ST.normal, d = this.d;
      d.setFillColor(...s.bg); d.roundedRect(x, y, w, h, 0.9, 0.9, 'F');
      const cx = x + 3.2, cy = y + h / 2, r = 1.15;
      d.setFillColor(...s.fg); d.setDrawColor(...s.fg);
      if (st === 'kritis') d.triangle(cx - r, cy + r * 0.85, cx + r, cy + r * 0.85, cx, cy - r, 'F');
      else if (st === 'waspada') d.lines([[r, r], [-r, r], [-r, -r]], cx, cy - r, [1, 1], 'F', true);
      else if (st === 'pantau') d.circle(cx, cy, r * 0.85, 'F');
      else { d.setLineWidth(0.45); d.lines([[0.75, 0.85], [1.45, -1.9]], cx - 1.05, cy, [1, 1], 'S'); }
      this.f('bold', h * 1.55, s.fg); this.d.text(s.label, x + 5.4 + (w - 5.4) / 2 - 0.6, cy + h * 0.19, { align: 'center' });
    }
    logoWaspada(x, y, s) { // logo WASPADA (img/logo-waspada.svg) digambar ulang sebagai vektor; s = mm per 100 unit
      const d = this.d, k = s / 100, P = (pts, col) => {
        d.setFillColor(...col); const [x0, y0] = pts[0];
        d.lines(pts.slice(1).map((p, i) => [(p[0] - pts[i][0]) * k, (p[1] - pts[i][1]) * k]), x + x0 * k, y + y0 * k, [1, 1], 'F', true);
      };
      P([[20, 40], [50, 40], [90, 150], [60, 150]], [11, 58, 110]); P([[96, 150], [128, 62], [158, 62], [126, 150]], [0, 108, 183]);
      P([[164, 62], [194, 62], [226, 150], [196, 150]], [173, 195, 43]); P([[232, 150], [279, 20], [309, 20], [262, 150]], [237, 50, 55]);
      d.setFillColor(11, 58, 110); d.roundedRect(x + 20 * k, y + 168 * k, 290 * k, 10 * k, 5 * k, 5 * k, 'F');
    }
    kepala(kicker, judul, subjudul, catatan) {
      const d = this.d;
      if (ASET.logo) d.addImage('data:image/png;base64,' + ASET.logo, 'PNG', M, this.y + 2, 55, 55 * 96 / 504);
      this.f('bold', 7.4, C.grey); d.text(kicker, PW - M, this.y + 2.6, { align: 'right' });
      this.f('bold', 17, C.blue); const wW = d.getTextWidth('WASPADA'), wP = d.getTextWidth('PERTAGAS');
      this.f('normal', 7.6, C.grey); const wA = d.getTextWidth('Watch & Analytics for Spatial Pipeline Anomaly & Disaster Awareness');
      this.logoWaspada(PW - M - Math.max(wA, wW + wP + 1.6) - 16.5, this.y + 3.6, 4.6);
      this.f('bold', 17, C.blue); d.text('PERTAGAS', PW - M - wW - 1.6, this.y + 9.6, { align: 'right' });
      d.setTextColor(...C.red); d.text('WASPADA', PW - M, this.y + 9.6, { align: 'right' });
      this.f('normal', 7.6, C.grey); d.text('Watch & Analytics for Spatial Pipeline Anomaly & Disaster Awareness', PW - M, this.y + 13.4, { align: 'right' });
      this.y += 21;
      this.f('bold', 19.5, C.ink); const jl = d.splitTextToSize(judul, CW); d.text(jl, M, this.y + 5.5); this.y += 7.6 * jl.length;
      if (subjudul) { this.f('normal', 11, C.ink); d.text(subjudul, M, this.y + 3.5); this.y += 5.4; }
      for (const c of catatan.filter(Boolean)) { const h = this.teks(c, M, CW, { size: 8.4, col: C.grey, ukurSaja: true }); this.teks(c, M, CW, { size: 8.4, col: C.grey }); this.y += h + 0.6; }
      this.y += 2.5;
    }
    spanduk(st, judul, isi) {
      const d = this.d, s = ST[st] || ST.normal, x = M + 11, w = CW - 15;
      const hIsi = this.teks(isi, x, w, { size: 9.4, ukurSaja: true }), h = 6.2 + hIsi + 5.4;
      this.perlu(h);
      d.setFillColor(...s.bg); d.roundedRect(M, this.y, CW, h, 1.4, 1.4, 'F');
      const y0 = this.y; this.lencanaIkon(st, M + 5.5, y0 + h / 2, s.fg);
      this.y += 3.4; this.f('bold', 12, s.fg); d.text(judul, x, this.y + 3.4); this.y += 5.6;
      this.teks(isi, x, w, { size: 9.4, col: s.fg }); this.y = y0 + h + 3;
    }
    lencanaIkon(st, cx, cy, col) {
      const d = this.d, r = 2.4; d.setFillColor(...col); d.setDrawColor(...col);
      if (st === 'kritis') d.triangle(cx - r, cy + r * 0.85, cx + r, cy + r * 0.85, cx, cy - r, 'F');
      else if (st === 'waspada') d.lines([[r, r], [-r, r], [-r, -r]], cx, cy - r, [1, 1], 'F', true);
      else if (st === 'pantau') d.circle(cx, cy, r * 0.8, 'F');
      else { d.setLineWidth(0.8); d.lines([[1.4, 1.6], [2.8, -3.6]], cx - 2, cy, [1, 1], 'S'); }
    }
    ubin(tiles) {
      const d = this.d, gap = 2, w = (CW - gap * (tiles.length - 1)) / tiles.length;
      const hs = tiles.map(t => 3.2 + this.teks(t.lbl, 0, w - 7, { size: 7.2, bold: true, ukurSaja: true }) + 8.4 + this.teks(t.sub, 0, w - 7, { size: 7.6, ukurSaja: true }) + 3);
      const h = Math.max(...hs); this.perlu(h);
      tiles.forEach((t, i) => {
        const x = M + i * (w + gap); d.setFillColor(...C.panel); d.roundedRect(x, this.y, w, h, 1.2, 1.2, 'F');
        const y0 = this.y; this.y = y0 + 3.2;
        this.y += this.teks(t.lbl, x + 3.5, w - 7, { size: 7.2, bold: true, col: C.grey });
        this.f('bold', 16.5, t.col || C.ink); const v = d.splitTextToSize(String(t.val), w - 6)[0]; d.text(v, x + 3.5, this.y + 6);
        this.y += 8.4; this.teks(t.sub, x + 3.5, w - 7, { size: 7.6, col: C.grey }); this.y = y0;
      });
      this.y += h + 2;
    }
    tabel(head, rows, colW, o = {}) {
      const d = this.d, self = this, x = M + (o.indent ?? IND), w = CW - (o.indent ?? IND);
      const sum = colW.reduce((a, b) => a + (b || 0), 0), sisa = colW.filter(c => !c).length;
      const cols = colW.map(c => c || (w - sum) / sisa);
      this.perlu(o.minH || 18);
      d.autoTable({
        startY: this.y, margin: { left: x, right: M, top: TOPN, bottom: PH - BOT }, tableWidth: w,
        head: [head], body: rows, theme: 'plain', rowPageBreak: 'avoid',
        styles: { font: 'Carlito', fontSize: o.size || 8.4, cellPadding: { top: 1.3, bottom: 1.3, left: 1.4, right: 1.4 }, textColor: C.ink, lineColor: C.line, lineWidth: 0, valign: 'top', overflow: 'linebreak' },
        headStyles: { fontStyle: 'bold', textColor: C.grey, fontSize: (o.size || 8.4) - 0.3 },
        columnStyles: Object.fromEntries(cols.map((c, i) => [i, { cellWidth: c, ...((o.colStyle || {})[i] || {}) }])),
        didParseCell(a) {
          const r = a.cell.raw;
          if (r && typeof r === 'object' && r.badge) { a.cell.text = ['']; a.cell.styles.minCellHeight = 6.6; }
          if (r && typeof r === 'object' && r.bold) a.cell.styles.fontStyle = 'bold';
          if (a.section === 'body' && o.sorot && o.sorot(a.row.index)) a.cell.styles.fillColor = C.rowhi;
          if (a.section === 'body' && o.warna) { const w = o.warna(a.row.index, a.column.index); if (w) a.cell.styles.textColor = w; }
        },
        didDrawCell(a) {
          const r = a.cell.raw;
          if (a.section === 'body' && r && typeof r === 'object' && r.badge) self.lencana(r.badge, a.cell.x + 1.2, a.cell.y + 1.2, Math.min(21, a.cell.width - 2.4), 4.6);
          d.setDrawColor(...(a.section === 'head' ? C.ink : C.line)); d.setLineWidth(a.section === 'head' ? 0.35 : 0.15);
          d.line(a.cell.x, a.cell.y + a.cell.height, a.cell.x + a.cell.width, a.cell.y + a.cell.height);
        },
      });
      this.y = d.lastAutoTable.finalY + (o.after ?? 2.5);
    }
    catatan(t) { this.para(t, { size: 8.2, col: C.grey, after: 1.6 }); }
    kotak(t) {
      const x = M + IND, w = CW - IND, h = this.teks(t, x + 3.5, w - 7, { size: 8.2, ukurSaja: true }) + 6;
      this.perlu(h); this.d.setFillColor(...C.panel); this.d.rect(x, this.y, w, h, 'F');
      const y0 = this.y; this.y += 3; this.teks(t, x + 3.5, w - 7, { size: 8.2, col: C.grey }); this.y = y0 + h + 2;
    }
    // grafik batang bertumpuk hotspot ≤5 km per hari
    grafik(hari, seri, judul) {
      const d = this.d, x0 = M + IND, w = CW - IND, h = 52; this.perlu(h + 8);
      this.f('bold', 8.8, C.ink); d.text(judul, x0, this.y + 3); this.y += 5;
      const leg = [['≤ 1 km', C.kritis], ['1–3 km', C.waspada], ['3–5 km', C.pantau]];
      let lx = x0; leg.forEach(([t, c]) => { d.setFillColor(...c); d.rect(lx, this.y + 0.4, 2.6, 2.6, 'F'); this.f('normal', 7.8, C.grey); d.text(t, lx + 3.6, this.y + 2.6); lx += 6 + d.getTextWidth(t) + 4; });
      this.y += 5;
      const ph = h - 14, base = this.y + ph, n = hari.length, slot = w / Math.max(n, 1), bw = Math.min(14, slot * 0.6);
      const tot = hari.map((_, i) => seri[0][i] + seri[1][i] + seri[2][i]), mx = Math.max(1, ...tot) * 1.15;
      d.setDrawColor(...C.line); d.setLineWidth(0.2); d.line(x0, base, x0 + w, base);
      hari.forEach((t, i) => {
        let yy = base; const cx = x0 + slot * i + slot / 2;
        [0, 1, 2].forEach(s => { const v = seri[s][i], hh = ph * v / mx; if (v > 0) { d.setFillColor(...[C.kritis, C.waspada, C.pantau][s]); d.rect(cx - bw / 2, yy - hh, bw, hh, 'F'); } yy -= hh; });
        this.f(i === n - 1 ? 'bold' : 'normal', 8, C.ink); d.text(String(tot[i]), cx, yy - 1.2, { align: 'center' });
        this.f('normal', 7.6, C.grey); d.text(hariPendek(t), cx, base + 3.6, { align: 'center' });
      });
      this.y = base + 7;
    }
    // peta skematis vektor: panel seluruh area + panel perbesaran di sekitar hotspot berisiko tertinggi
    peta(X, k, R) {
      const d = this.d, x0 = M + IND, w = CW - IND, feats = (X.ASSETS.features || []).filter(f => f.properties && f.properties.area === k);
      const pts = []; feats.forEach(f => { const g = f.geometry; if (!g) return; if (g.type === 'Point') pts.push(g.coordinates); else (g.type === 'LineString' ? [g.coordinates] : g.type === 'MultiLineString' ? g.coordinates : []).forEach(c => c.forEach(p => pts.push(p))); });
      R.h.forEach(h => pts.push([+h.lon, +h.lat]));
      if (!pts.length) { this.catatan('Geometri aset wilayah ini tidak tersedia.'); return; }
      let bb = [Infinity, Infinity, -Infinity, -Infinity];
      pts.forEach(([lo, la]) => { bb = [Math.min(bb[0], lo), Math.min(bb[1], la), Math.max(bb[2], lo), Math.max(bb[3], la)]; });
      const pl = (bb[2] - bb[0]) * 0.06 + 0.02, pa = (bb[3] - bb[1]) * 0.06 + 0.02; bb = [bb[0] - pl, bb[1] - pa, bb[2] + pl, bb[3] + pa];
      { const kx0 = Math.cos(((bb[1] + bb[3]) / 2) * Math.PI / 180), minH = 0.32 * (bb[2] - bb[0]) * kx0, hh = bb[3] - bb[1]; if (hh < minH) { bb[1] -= (minH - hh) / 2; bb[3] += (minH - hh) / 2; } }
      const urut = { normal: 0, luar: 0, pantau: 1, waspada: 2, kritis: 3 };
      const fokus = R.h.slice().sort((a, b) => (urut[X.hsSt(b)] - urut[X.hsSt(a)]) || ((b.risiko?.R || 0) - (a.risiko?.R || 0)) || (a.jarak_km - b.jarak_km))[0];
      const zoom = fokus && urut[X.hsSt(fokus)] >= 1;
      const ukuran = (b, wMax, hMax) => { const kx = Math.cos(((b[1] + b[3]) / 2) * Math.PI / 180), s = Math.min(wMax / ((b[2] - b[0]) * kx), hMax / (b[3] - b[1])); return [(b[2] - b[0]) * kx * s, (b[3] - b[1]) * s]; };
      if (!zoom) {
        const [mw, mh] = ukuran(bb, w, 120); this.perlu(mh + 16);
        this.panel(X, k, R, feats, bb, x0 + (w - mw) / 2, this.y, w, 120, 'Seluruh jalur ' + k);
        this.y += mh + 6;
      } else {
        const r = 0.065, zb = [+fokus.lon - r / Math.cos(fokus.lat * Math.PI / 180), +fokus.lat - r, +fokus.lon + r / Math.cos(fokus.lat * Math.PI / 180), +fokus.lat + r];
        const wa = (w - 4) * 0.5, [m1w, m1h] = ukuran(bb, wa, 112), [m2w, m2h] = ukuran(zb, wa, 112), h = Math.max(m1h, m2h);
        this.perlu(h + 16);
        this.panel(X, k, R, feats, bb, x0 + (wa - m1w) / 2, this.y, wa, 112, 'Seluruh jalur ' + k, zb);
        this.panel(X, k, R, feats, zb, x0 + wa + 4 + (wa - m2w) / 2, this.y, wa, 112, `Perbesaran: ${lokasiHs(fokus).split(',').slice(0, 2).join(',')}`, null, true);
        this.y += h + 6;
      }
      // legenda
      const leg = [['line', C.pipa, 'Pipa'], ['seg', C.ungu, 'Ruas zona PVMBG tinggi'], ['sq', C.ink, 'Fasilitas'], ['dot', C.kritis, 'Hotspot Kritis'], ['dot', C.waspada, 'Waspada'], ['dot', C.pantau, 'Pantau'], ['dot', C.normal, 'Normal (R ≤ 2)'], ['ring', C.ungu, 'Gempa 7 hari']];
      let lx = x0; this.f('normal', 7.2, C.grey);
      leg.forEach(([t, c, l]) => {
        const lw = d.getTextWidth(l) + 9; if (lx + lw > x0 + w) { lx = x0; this.y += 4; }
        d.setDrawColor(...c); d.setFillColor(...c);
        if (t === 'line' || t === 'seg') { d.setLineWidth(t === 'seg' ? 1.4 : 0.5); d.line(lx, this.y + 1.6, lx + 4.5, this.y + 1.6); }
        else if (t === 'sq') d.rect(lx + 1.6, this.y + 0.9, 1.4, 1.4, 'F'); else if (t === 'dot') d.circle(lx + 2.2, this.y + 1.6, 1.1, 'F'); else { d.setLineWidth(0.5); d.circle(lx + 2.2, this.y + 1.6, 1.4, 'S'); }
        this.f('normal', 7.2, C.grey); d.text(l, lx + 6, this.y + 2.6); lx += lw + 3;
      });
      this.y += 6;
    }
    panel(X, k, R, feats, bb, ox, oy, wMax, hMax, judul, kotakZoom, besar) {
      const d = this.d, [a0, b0, a1, b1] = bb, kx = Math.cos(((b0 + b1) / 2) * Math.PI / 180);
      const s = Math.min(wMax / ((a1 - a0) * kx), hMax / (b1 - b0)), mw = (a1 - a0) * kx * s, mh = (b1 - b0) * s;
      const P = (lo, la) => [ox + (lo - a0) * kx * s, oy + (b1 - la) * s];
      const dalam = (lo, la) => lo >= a0 && lo <= a1 && la >= b0 && la <= b1;
      d.setFillColor(255, 255, 255); d.setDrawColor(...C.line); d.setLineWidth(0.25); d.rect(ox, oy, mw, mh, 'FD');
      // kisi derajat (label di tepi atas & kiri)
      d.setDrawColor(236, 238, 241); d.setLineWidth(0.15); this.f('normal', 6.2, C.grey);
      const step = niceStep(Math.max(a1 - a0, b1 - b0) / 3), dg = step < 0.01 ? 3 : step < 0.1 ? 2 : 1;
      for (let lo = Math.ceil(a0 / step) * step; lo < a1; lo += step) { const [px] = P(lo, b0); d.line(px, oy, px, oy + mh); d.text(`${nf(lo, dg)}° BT`, px + 0.6, oy + 2.6); }
      for (let la = Math.ceil(b0 / step) * step; la < b1; la += step) { const [, py] = P(a0, la); d.line(ox, py, ox + mw, py); d.text(`${nf(Math.abs(la), dg)}° ${la < 0 ? 'LS' : 'LU'}`, ox + 0.8, py - 0.6); }
      const garis = c => { let prev = null, pIn = false; for (const p of c) { const q = P(p[0], p[1]), qi = dalam(p[0], p[1]); if (prev && (Math.abs(q[0] - prev[0]) + Math.abs(q[1] - prev[1]) < 0.2)) continue; if (prev && (qi || pIn)) { const cl = (v, lo, hi) => Math.max(lo, Math.min(hi, v)); d.line(cl(prev[0], ox, ox + mw), cl(prev[1], oy, oy + mh), cl(q[0], ox, ox + mw), cl(q[1], oy, oy + mh)); } prev = q; pIn = qi; } };
      d.setDrawColor(...C.ungu); d.setLineWidth(besar ? 2.2 : 1.6);
      ((R.pk && R.pk.segmen) || []).filter(sg => (sg.level === 'tinggi' || sg.level === 'bandang') && Array.isArray(sg.garis)).forEach(sg => garis(sg.garis));
      d.setDrawColor(...C.pipa); d.setLineWidth(besar ? 0.7 : 0.45);
      feats.forEach(f => { const g = f.geometry; if (!g) return; (g.type === 'LineString' ? [g.coordinates] : g.type === 'MultiLineString' ? g.coordinates : []).forEach(garis); });
      d.setFillColor(...C.ink);
      const fasDalam = feats.filter(f => f.geometry && f.geometry.type === 'Point' && dalam(...f.geometry.coordinates));
      fasDalam.forEach(f => { const [px, py] = P(...f.geometry.coordinates); d.rect(px - 0.6, py - 0.6, 1.2, 1.2, 'F'); });
      X.qs.forEach(g => { if (!dalam(+g.lon, +g.lat)) return; const [px, py] = P(+g.lon, +g.lat); d.setDrawColor(...C.ungu); d.setLineWidth(0.5); d.circle(px, py, 1.6 + Math.max(0, (+g.mag || 0) - 3) * 0.6, 'S'); });
      const urut = { normal: 0, luar: 0, pantau: 1, waspada: 2, kritis: 3 };
      R.h.filter(h => dalam(+h.lon, +h.lat)).sort((a, b) => urut[X.hsSt(a)] - urut[X.hsSt(b)]).forEach(h => {
        const st = X.hsSt(h), [px, py] = P(+h.lon, +h.lat);
        d.setFillColor(...(C[st] || C.luar)); d.setDrawColor(255, 255, 255); d.setLineWidth(0.25); d.circle(px, py, (besar ? 1.5 : 1.1) * (st === 'kritis' ? 1.2 : 1), 'FD');
      });
      // label fasilitas: di panel perbesaran semua fasilitas dalam bingkai (maks. 8), di panel utama hanya fasilitas prioritas
      const nama = new Set();
      if (besar) fasDalam.forEach(f => nama.add(f.properties.name));
      else { R.h.filter(h => ['kritis', 'waspada'].includes(X.hsSt(h)) && h.fasilitas).forEach(h => nama.add(h.fasilitas)); ((R.pk && R.pk.fasilitas) || []).filter(f => f.level === 'tinggi' || f.level === 'bandang').forEach(f => nama.add(f.aset)); }
      const dipakai = []; let nL = 0;
      for (const nm of nama) {
        if (nL >= (besar ? 8 : 5)) break;
        const f = fasDalam.find(x => x.properties.name === nm); if (!f) continue;
        const [px, py] = P(...f.geometry.coordinates); if (dipakai.some(([qx, qy]) => Math.abs(qx - px) < 20 && Math.abs(qy - py) < 3.5)) continue;
        dipakai.push([px, py]); nL++; this.f('bold', 6.4, C.ink); const kanan = px + 28 > ox + mw;
        d.text(bersih(nm), kanan ? px - 1.2 : px + 1.2, py - 1, { align: kanan ? 'right' : 'left' });
      }
      if (kotakZoom) { const [zx0, zy0] = P(kotakZoom[0], kotakZoom[3]), [zx1, zy1] = P(kotakZoom[2], kotakZoom[1]); d.setDrawColor(...C.ink); d.setLineWidth(0.35); d.setLineDashPattern([1, 0.8], 0); d.rect(zx0, zy0, Math.max(zx1 - zx0, 1.5), Math.max(zy1 - zy0, 1.5), 'S'); d.setLineDashPattern([], 0); }
      // skala, utara, judul panel
      const kmPerMm = 111.32 / s, target = niceStep(kmPerMm * Math.min(22, mw * 0.35)), bar = target / kmPerMm;
      d.setFillColor(255, 255, 255); d.rect(ox + mw - bar - 6.5, oy + mh - 6.6, bar + 5.5, 5.2, 'F');
      d.setDrawColor(...C.ink); d.setLineWidth(0.4); d.line(ox + mw - bar - 4, oy + mh - 2.6, ox + mw - 4, oy + mh - 2.6);
      this.f('normal', 6.4, C.ink); d.text(`${nf(target, target < 1 ? 1 : 0)} km`, ox + mw - 4 - bar / 2, oy + mh - 3.6, { align: 'center' });
      d.setFillColor(...C.ink); d.triangle(ox + mw - 4.5, oy + 4.2, ox + mw - 6, oy + 7.8, ox + mw - 3, oy + 7.8, 'F'); this.f('bold', 6.2, C.ink); d.text('U', ox + mw - 4.5, oy + 10.2, { align: 'center' });
      this.f('bold', 7.6, C.ink); d.text(d.splitTextToSize(judul, mw)[0], ox, oy + mh + 3.4);
    }
    selesai(namaBerkas, waktuBuat) {
      const d = this.d, n = d.getNumberOfPages();
      for (let i = 1; i <= n; i++) {
        d.setPage(i);
        d.setFillColor(...C.blue); d.rect(0, 0, PW * 0.5, 2.2, 'F'); d.setFillColor(...C.green); d.rect(PW * 0.5, 0, PW * 0.25, 2.2, 'F'); d.setFillColor(...C.red); d.rect(PW * 0.75, 0, PW * 0.25, 2.2, 'F');
        d.setDrawColor(...C.line); d.setLineWidth(0.2); d.line(M, PH - 13, PW - M, PH - 13);
        this.f('normal', 7.4, C.grey);
        d.text(`PERTAGAS WASPADA · ${this.judulPendek} · dibuat ${waktuBuat} · Fungsi HSSE PT Pertamina Gas`, M, PH - 9);
        d.text(`Halaman ${i} dari ${n}`, PW - M, PH - 9, { align: 'right' });
        if (i > 1) {
          this.f('bold', 8.6, C.ink); d.text(`${this.judulPendek} · PERTAGAS WASPADA`, M, 11.5);
          this.f('normal', 7.8, C.grey); d.text(`Dibuat ${waktuBuat} dari data dashboard saat itu`, M, 15.5);
          if (ASET.logo) d.addImage('data:image/png;base64,' + ASET.logo, 'PNG', PW - M - 34, 8.6, 34, 34 * 96 / 504);
        }
      }
      d.save(namaBerkas);
    }
  }
  function niceStep(v) { const p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p; return (m < 1.5 ? 1 : m < 3.5 ? 2 : m < 7.5 ? 5 : 10) * p; }
  const hariPendek = t => new Date(t + 'T00:00:00+07:00').toLocaleDateString('id-ID', { ...tz, day: 'numeric', month: 'short' });

  // ---------------------------------------------------------------- bagian bersama
  function trenHarian(X, area) {
    const per = {};
    for (const r of X.HIST) { if (area && r.area !== area) continue; const o = per[r.tanggal] || (per[r.tanggal] = [0, 0, 0]); o[0] += +r.kritis || 0; o[1] += +r.waspada || 0; o[2] += +r.pantau || 0; }
    const hari = Object.keys(per).sort().slice(-8);
    return { hari, seri: [0, 1, 2].map(i => hari.map(t => per[t][i])) };
  }
  function bagianSumber(L, X, n) {
    L.h1(n, 'Status sumber data');
    L.h2(`${n}.1`, 'Rincian sumber data');
    const LBL = { fresh: 'Refresh', delayed: 'On Progress', stale: 'On Progress', none: 'Tidak tersedia' };
    const W = { fresh: C.normal, delayed: C.waspada, stale: C.waspada, none: C.grey };
    const rows = X.sumber.map(r => [{ content: bersih(r.nama), bold: true }, bersih(r.src), bersih(String(r.data || '').replace(/<[^>]+>/g, '')), { content: LBL[r.st] || r.st, bold: true }]);
    L.tabel(['Data', 'Sumber', 'Waktu / cakupan data', 'Status'], rows, [36, 52, 0, 22], {
      size: 8, warna: (r, c) => (c === 3 ? W[X.sumber[r].st] : null),
    });
    L.h2(`${n}.2`, 'Penafian');
    L.kotak('Laporan ini dibuat otomatis dari dashboard PERTAGAS WASPADA pada saat tombol unduh ditekan, sebagai sistem pemantauan dan pendukung keputusan. '
      + 'Hotspot adalah deteksi titik panas satelit, bukan konfirmasi kebakaran; cuaca, kualitas udara, indikasi longsor, banjir sungai dan potensi petir adalah keluaran model. '
      + 'Rekomendasi adalah usulan otomatis berdasarkan data dan perlu disesuaikan dengan penilaian tim lapangan. Informasi ini tidak menggantikan peringatan resmi BMKG, PVMBG, BNPB/BPBD dan Kemenhut.');
  }
  function catatanData(X) {
    const lambat = X.sumber.filter(r => r.st === 'stale' || r.st === 'delayed').map(r => bersih(r.nama));
    return lambat.length ? `Catatan data: ${lambat.join(', ')} berstatus on progress — lihat bagian Status sumber data.` : 'Semua sumber data utama berstatus refresh saat laporan dibuat.';
  }
  function rekomendasiArea(X, R) {
    const out = [], A = R.A, top = (A.temuan || []).filter(t => t.R >= 8);
    const hsTop = kelompok(top.filter(t => t.jenis === 'hotspot' && t.ref)).slice(0, 3);
    if (hsTop.length) out.push(`**Verifikasi lapangan hotspot** di ${hsTop.map(t => `${t.ref.kec ? bersih(t.ref.kec) + ', ' + bersih(t.ref.kab) : lokasiHs(t.ref)} (${t.n > 1 ? `${t.n} titik, ` : ''}${nf(t.jMin ?? t.ref.jarak_km, 2)} km dari ${bersih(t.ref.aset)})`).join('; ')}; koordinasikan dengan Manggala Agni/Daops dan BPBD setempat bila api terkonfirmasi.`);
    if (top.some(t => t.jenis === 'gempa')) out.push(`**Cek visual ROW dan fasilitas** di sekitar ${R.q ? bersih(R.q.per_area[R.k].aset) : 'aset terdekat'} pasca gempa.`);
    if ((A.temuan || []).some(t => t.jenis === 'gerakan_tanah' && t.R >= 6)) out.push('**Siagakan patroli lereng** pada ruas zona prakiraan gerakan tanah tinggi PVMBG / indikasi longsor model, terutama saat hujan lebat.');
    if (R.pd.length) out.push(`**Teruskan ${R.pd.length} peringatan dini BMKG yang aktif** ke tim lapangan; tunda pekerjaan di ketinggian dan di ruang terbuka selama peringatan berlaku.`);
    if (R.pt && R.pt.terburuk === 'tinggi') out.push('**Potensi petir tinggi (model):** batasi pekerjaan di ruang terbuka saat badai dan pastikan sistem proteksi petir fasilitas berfungsi.');
    if (R.bj && ['tinggi', 'sedang'].includes(R.bj.terburuk)) out.push('**Pantau crossing sungai** yang dilintasi pipa: potensi debit tinggi menurut model GloFAS.');
    if (R.gd.length) out.push(`**Siaga kejadian GDACS:** ${R.gd.map(e => bersih(e.jenis_nama + (e.nama ? ' ' + e.nama : ''))).join(', ')}.`);
    if (R.aqiMax != null && R.aqiMax > 150) out.push(`**Lindungi pekerja dari asap/polusi:** US AQI maks ${nf(R.aqiMax)} (${aqiKat(R.aqiMax)}); sediakan masker partikulat dan kurangi pekerjaan berat di luar ruang.`);
    return out;
  }

  // ---------------------------------------------------------------- LAPORAN DETAIL AREA
  function laporanArea(X, k) {
    const R = ringkasArea(X, k), A = R.A, D = X.DATA, now = X.now;
    const L = new Laporan(`Laporan ${k}`, `Laporan PERTAGAS WASPADA — ${R.nama} — ${tglPanjang(now)}`);
    L.X = X;
    L.kepala('LAPORAN AREA OPERASI · DIBUAT OTOMATIS', R.nama + ` (${k})`, 'Kondisi lingkungan & geohazard jalur pipa dan fasilitas',
      [`${tglPanjang(now)}, ${jamWIB(now)} · data dashboard diproses ${waktu(D.generated_at)} · Fungsi HSSE PT Pertamina Gas`, catatanData(X)]);
    const pem = A.pemicu ? bersih(A.pemicu.teks) : 'Tidak ada temuan bahaya.';
    L.spanduk(R.status, `STATUS ${k}: ${ST[R.status].label}${R.R ? ` (R ${R.R})` : ''}`,
      `Pemicu utama: ${pem}.${A.eskalasi ? ` Naik satu tingkat: ${bersih(A.eskalasi)}.` : ''} Status = skor risiko tertinggi R = P × K dari klasifikasi multi-bahaya dashboard.`);
    const qd = R.q ? R.q.per_area[k] : null;
    L.ubin([
      { lbl: 'HOTSPOT ≤ 5 KM DARI ASET', val: String(R.h.length), sub: `≤ 1 km: ${R.n1} · 1–3 km: ${R.n3} · 3–5 km: ${R.n5}`, col: R.n1 ? C.kritis : C.ink },
      { lbl: 'HOTSPOT BERISIKO (R ≥ 8)', val: String(R.nRisk), sub: `${R.nNon} di lahan non-vegetasi · ${R.nRep} berulang`, col: R.nRisk ? C.waspada : C.normal },
      { lbl: 'GEMPA TERDEKAT (7 HARI)', val: R.q ? `M ${nf(R.q.mag, 1)}` : '–', sub: R.q ? `${nf(qd.jarak_km)} km dari ${bersih(qd.aset)}` : 'tidak ada gempa tercatat' },
      { lbl: 'PVMBG ZONA TINGGI', val: R.pk ? `${nf(kmLv(R.pk, 'tinggi') + kmLv(R.pk, 'bandang'), 1)} km` : '–', sub: R.pk ? `menengah ${nf(kmLv(R.pk, 'menengah'), 1)} km · ${bersih(R.P.periode || '')}` : 'belum dianalisis' },
      { lbl: 'KUALITAS UDARA (US AQI)', val: rentang(R.aqiMin, R.aqiMax), sub: R.aqiMax != null ? `maks. ${aqiKat(R.aqiMax)} (model)` : 'tidak tersedia', col: R.aqiMax > 150 ? C.waspada : C.ink },
    ]);

    // 1. RINGKASAN
    L.h1(1, 'Ringkasan');
    L.h2('1.1', 'Status & pemicu');
    const lain = (A.temuan || []).slice(1).filter(t => t.R >= 3), cnt = {};
    lain.forEach(t => cnt[t.jenis] = (cnt[t.jenis] || 0) + 1);
    L.butir([
      `Status **${ST[R.status].label}**${R.R ? ` (R ${R.R})` : ''} — ${pem}`,
      lain.length ? `Temuan lain R ≥ 3: ${Object.entries(cnt).map(([j, v]) => `${JL[j] || j} ${v}`).join(' · ')}.` : 'Tidak ada temuan lain dengan R ≥ 3.',
    ]);
    L.h2('1.2', 'Hotspot');
    const pv = Object.entries(R.provinsi).map(([p, v]) => `${p} ${nf(v)}`).join(' · ');
    L.butir([
      `**${R.h.length} titik** dalam radius 5 km dari aset (≤ 1 km: ${R.n1}, 1–3 km: ${R.n3}, 3–5 km: ${R.n5}); **${R.nRisk}** berstatus risiko Waspada/Kritis.`,
      R.h.length ? `${R.nNon} titik di lahan non-vegetasi (kemungkinan sumber panas industri/kota) dan ${R.nRep} titik berulang di lokasi yang sama.` : null,
      pv ? `Hotspot 24 jam di provinsi wilayah: ${pv}.` : null,
    ]);
    L.h2('1.3', 'Gempa, longsor & gerakan tanah');
    L.butir([
      R.q ? `Gempa terdekat 7 hari: **M ${nf(R.q.mag, 1)}** (${waktu(R.q.datetime)}), ${nf(qd.jarak_km)} km dari ${bersih(qd.aset)}; ${R.q.tsunami ? '**berpotensi tsunami**' : 'tidak berpotensi tsunami'}.` : 'Tidak ada gempa tercatat dalam 7 hari terakhir.',
      `Indikasi longsor (model): hari ini ${R.lsH && R.lsH.terburuk ? `**${LVL[R.lsH.terburuk]}** pada ${nf(kmLv(R.lsH, 'tinggi') + kmLv(R.lsH, 'sedang') + kmLv(R.lsH, 'rendah'), 1)} km pipa` : 'tidak ada indikasi'}; besok ${R.lsB && R.lsB.terburuk ? `**${LVL[R.lsB.terburuk]}** pada ${nf(kmLv(R.lsB, 'tinggi') + kmLv(R.lsB, 'sedang') + kmLv(R.lsB, 'rendah'), 1)} km pipa` : 'tidak ada indikasi'}.`,
      R.pk ? `Prakiraan gerakan tanah PVMBG (${bersih(R.P.periode || '')}): ${nf(kmLv(R.pk, 'tinggi'), 1)} km pipa zona tinggi, ${nf(kmLv(R.pk, 'menengah'), 1)} km menengah${kmLv(R.pk, 'bandang') ? `, ${nf(kmLv(R.pk, 'bandang'), 1)} km berpotensi banjir bandang` : ''}.` : 'Prakiraan gerakan tanah PVMBG belum tersedia untuk wilayah ini.',
    ]);
    L.h2('1.4', 'Cuaca, udara & peringatan');
    L.butir([
      R.wx.length ? `Prakiraan hujan hari ini ${rentang(R.hjMin, R.hjMax, 1, ' mm')} di ${R.wx.length} titik pantau; US AQI ${rentang(R.aqiMin, R.aqiMax)} (maks. ${aqiKat(R.aqiMax)}).` : 'Data cuaca tidak tersedia.',
      R.pd.length ? `**${R.pd.length} peringatan dini BMKG aktif** mengenai aset: ${R.pd.map(w => bersih(w.event || w.judul)).join('; ')}.` : 'Tidak ada peringatan dini cuaca BMKG yang aktif mengenai aset.',
      `Potensi petir hari ini: ${R.pt && R.pt.terburuk ? `**${LVL[R.pt.terburuk]}** (${nf(kmLv(R.pt, R.pt.terburuk), 1)} km pipa)` : 'tidak ada'} · banjir sungai: ${R.bj && R.bj.terburuk ? LVL[R.bj.terburuk] : 'tidak ada'}${R.gd.length ? ` · GDACS: ${R.gd.length} kejadian relevan` : ''}.`,
    ]);

    // 2. PENENTU STATUS
    L.h1(2, 'Penentu status (klasifikasi multi-bahaya)');
    const semuaT = kelompok((A.temuan || []).filter(x => x.R >= 1)), t = semuaT.slice(0, 12);
    if (t.length) {
      L.tabel(['Jenis bahaya', 'Status', 'P × K = R', 'Keterangan'],
        t.map(x => [JL[x.jenis] || x.jenis, { badge: x.status }, `${x.P} × ${x.K} = ${x.R}${x.n > 1 ? '\n(tertinggi)' : ''}`, bersih(x.teks)]), [34, 24, 20, 0], { sorot: i => i === 0 });
      L.catatan(`Hotspot yang berdekatan (≤ 3 km) digabung menjadi satu klaster; skor klaster = skor titik tertinggi.${semuaT.length > t.length ? ` Ditampilkan ${t.length} dari ${semuaT.length} baris.` : ''}`);
    } else L.para('Tidak ada temuan bahaya di wilayah ini.');
    L.catatan('R = P × K. P (1–5) = peluang bahaya mengenai aset dalam 24–72 jam; K (1–5) = konsekuensi menurut jenis aset. Kritis R 15–25 · Waspada 8–14 · Pantau 3–7 · Normal 1–2.');

    // 3. PETA
    L.h1(3, 'Peta sebaran');
    L.h2('3.1', 'Hotspot, gempa & zona gerakan tanah terhadap aset');
    L.peta(X, k, R);
    L.catatan('Peta skematis vektor tanpa peta dasar (WGS84). Warna hotspot = status risiko hasil klasifikasi, bukan sekadar jarak.');

    // 4. HOTSPOT
    L.h1(4, 'Hotspot ≤ 5 km dari aset');
    if (R.h.length) {
      const urut = { kritis: 3, waspada: 2, pantau: 1, normal: 0, luar: 0 };
      const hs = R.h.slice().sort((a, b) => (urut[X.hsSt(b)] - urut[X.hsSt(a)]) || ((b.risiko?.R || 0) - (a.risiko?.R || 0)) || (a.jarak_km - b.jarak_km));
      const tampil = hs.slice(0, 30);
      L.tabel(['#', 'Status', 'Jarak (km)', 'Aset / fasilitas terdekat', 'Lokasi', 'Lahan', 'Satelit · waktu', 'Koordinat'],
        tampil.map((h, i) => [String(i + 1), { badge: X.hsSt(h) === 'luar' ? 'normal' : X.hsSt(h) }, `${nf(h.jarak_km, 2)}${h.risiko?.R ? `\nR ${h.risiko.R}` : ''}`,
          `${bersih(h.aset)}${h.fasilitas ? `\n${bersih(h.fasilitas)} ${nf(h.jarak_fasilitas_km, 1)} km` : ''}`, lokasiHs(h),
          `${h.tutupan ? bersih(String(h.tutupan.label || '').split(' (')[0]) : '–'}${h.persisten ? `\nberulang ${h.berulang_hari} hari` : ''}`,
          `${bersih(String(h.sumber || '').replace('NASA-', ''))} ${h.conf || ''}\n${waktu(h.waktu_utc)}`, koor(h.lat, h.lon)]),
        [6, 23, 14, 36, 0, 24, 24, 19], { size: 7.6, sorot: i => X.hsSt(tampil[i]) === 'kritis' });
      if (hs.length > tampil.length) L.catatan(`Ditampilkan ${tampil.length} dari ${hs.length} titik (prioritas status risiko tertinggi). Daftar lengkap ada di dashboard.`);
    } else L.para('Tidak ada hotspot dalam radius 5 km dari aset wilayah ini.');

    // 5. GEMPA
    L.h1(5, 'Gempa bumi (7 hari terakhir)');
    const qs = X.qs.filter(g => g.per_area && g.per_area[k]).sort((a, b) => a.per_area[k].jarak_km - b.per_area[k].jarak_km).slice(0, 6);
    if (qs.length) L.tabel(['Waktu', 'M', 'Kedalaman', 'Wilayah', 'Jarak ke aset', 'Tsunami'],
      qs.map(g => [waktu(g.datetime), nf(g.mag, 1), bersih(g.kedalaman), bersih(g.wilayah), `${nf(g.per_area[k].jarak_km)} km · ${bersih(g.per_area[k].aset)}`, g.tsunami ? 'Berpotensi' : 'Tidak']),
      [26, 9, 17, 0, 46, 16], { size: 8 });
    else L.para('Tidak ada gempa tercatat dalam 7 hari terakhir.');

    // 6. LONGSOR & GERAKAN TANAH
    L.h1(6, 'Longsor & gerakan tanah');
    L.h2('6.1', 'Indikasi longsor harian (model LHASA)');
    const ruas = [...((R.lsH && R.lsH.ruas) || []).map(r => ({ ...r, hari: 'Hari ini' })), ...((R.lsB && R.lsB.ruas) || []).map(r => ({ ...r, hari: 'Besok' }))];
    L.butir([
      `Hari ini (${hariTgl(X.LS?.produk?.hari_ini?.tanggal)}): ${R.lsH && R.lsH.terburuk ? `${LVL[R.lsH.terburuk]} — tinggi ${nf(kmLv(R.lsH, 'tinggi'), 1)} km, sedang ${nf(kmLv(R.lsH, 'sedang'), 1)} km, rendah ${nf(kmLv(R.lsH, 'rendah'), 1)} km pipa` : 'tidak ada indikasi'}; rasio hujan anteseden maks. ${nf((R.lsH?.rasio_maks || 0) * 100)}% ambang.`,
      `Besok (${hariTgl(X.LS?.produk?.besok?.tanggal)}): ${R.lsB && R.lsB.terburuk ? `${LVL[R.lsB.terburuk]} — ${nf(kmLv(R.lsB, 'tinggi') + kmLv(R.lsB, 'sedang') + kmLv(R.lsB, 'rendah'), 1)} km pipa` : 'tidak ada indikasi'}.`,
    ]);
    if (ruas.length) L.tabel(['Hari', 'Ruas pipa', 'Tingkat', 'Panjang (km)', 'Rasio hujan', 'Kerentanan', 'Koordinat'],
      ruas.slice(0, 10).map(r => [r.hari, bersih(r.aset), LVL[r.level] || r.level, nf(r.km, 2), `${nf((r.rasio || 0) * 100)}%`, String(r.kerentanan ?? '–'), koor(r.lat, r.lon)]), [16, 0, 18, 18, 18, 18, 30], { size: 8 });
    L.h2('6.2', `Prakiraan gerakan tanah PVMBG${R.P.periode ? ' — ' + bersih(R.P.periode) : ''}`);
    if (R.pk) {
      const fas = (R.pk.fasilitas || []).filter(f => ['tinggi', 'bandang', 'menengah'].includes(f.level));
      L.butir([
        `Panjang pipa: tinggi ${nf(kmLv(R.pk, 'tinggi'), 1)} km · banjir bandang ${nf(kmLv(R.pk, 'bandang'), 1)} km · menengah ${nf(kmLv(R.pk, 'menengah'), 1)} km · rendah ${nf(kmLv(R.pk, 'rendah'), 1)} km (dari ${nf(R.pk.total_pipa_km, 1)} km dianalisis).`,
        fas.length ? `Fasilitas di zona ≥ menengah: ${fas.map(f => `${bersih(f.aset)} (${LVL[f.level]})`).join(', ')}.` : 'Tidak ada fasilitas di zona menengah ke atas.',
      ]);
      const seg = (R.pk.segmen || []).filter(s => s.level === 'tinggi' || s.level === 'bandang').sort((a, b) => b.km - a.km).slice(0, 10);
      if (seg.length) L.tabel(['Ruas pipa di zona tinggi / banjir bandang', 'Tingkat', 'Panjang (km)', 'Koordinat'], seg.map(s => [bersih(s.aset), LVL[s.level], nf(s.km, 2), koor(s.lat, s.lon)]), [0, 26, 22, 34], { size: 8 });
    } else L.para('Belum tersedia untuk wilayah ini.');

    // 7. CUACA & UDARA
    L.h1(7, 'Cuaca & kualitas udara (model)');
    if (R.wx.length) {
      const tg = (R.wx[0].prakiraan || []).slice(0, 3).map(p => p.tanggal);
      L.tabel(['Titik pantau', `Kondisi ${waktu(R.wx[0].waktu)}`, 'Angin / hembusan (km/jam)', 'PM2.5 / US AQI', ...tg.map(t => `Hujan ${hariTgl(t)}`)],
        R.wx.map(w => [{ content: bersih(w.nama), bold: true }, `${bersih(w.cuaca)}; ${nf(w.suhu)}°C, RH ${nf(w.rh)}%`, `${nf(w.angin_kmh)} / ${nf(w.hembusan_kmh)}`, `${nf(w.pm25)} / ${nf(w.aqi_us)}`,
          ...tg.map((t, i) => { const p = (w.prakiraan || [])[i] || {}; return `${nf(p.hujan_mm, 1)} mm (${nf(p.peluang_hujan)}%)`; })]),
        [38, 34, 20, 18, 0, 0, 0], { size: 7.8 });
      L.catatan('Open-Meteo (model cuaca) dan CAMS ±45 km (PM2.5, US AQI rata-rata 24 jam — bukan ISPU).');
    } else L.para('Data cuaca tidak tersedia.');

    // 8. PERINGATAN & MODUL TAMBAHAN
    L.h1(8, 'Peringatan dini & bahaya tambahan');
    L.h2('8.1', 'Peringatan dini cuaca BMKG (aktif)');
    if (R.pd.length) L.tabel(['Kejadian', 'Berlaku', 'Aset terdampak'], R.pd.map(w => [bersih(w.judul || w.event), `${waktu(w.mulai)} – ${waktu(w.berakhir)}`,
      (() => { const v = w.per_area[k] || {}; return `${nf(v.pipa_km, 1)} km pipa${v.n_fasilitas ? `, ${v.n_fasilitas} fasilitas` : ''}`; })()]), [0, 52, 40], { size: 8 });
    else L.para('Tidak ada peringatan aktif yang mengenai aset wilayah ini.');
    L.h2('8.2', 'Potensi petir, banjir sungai & GDACS (model/otomatis)');
    L.butir([
      `Petir hari ini: ${R.pt && R.pt.terburuk ? `${LVL[R.pt.terburuk]} — ${nf(kmLv(R.pt, R.pt.terburuk), 1)} km pipa${R.pt.n_fasilitas && R.pt.n_fasilitas[R.pt.terburuk] ? `, ${R.pt.n_fasilitas[R.pt.terburuk]} fasilitas` : ''}` : 'tidak ada'}; besok: ${R.ptB && R.ptB.terburuk ? LVL[R.ptB.terburuk] : 'tidak ada'}.`,
      `Banjir sungai (GloFAS): ${R.bj ? (R.bj.terburuk ? `${LVL[R.bj.terburuk]} — ${nf(kmLv(R.bj, R.bj.terburuk), 1)} km pipa di ${nf(R.bj.sel_sungai)} sel sungai` : `tidak ada potensi (${nf(R.bj.sel_sungai)} sel sungai dipantau)`) : 'tidak tersedia'}.`,
      R.gd.length ? `GDACS: ${R.gd.map(e => `${bersih(e.jenis_nama)}${e.nama ? ' ' + bersih(e.nama) : ''} (${e.level}) — ${bersih(e.per_area[k].ket)}`).join('; ')}.` : 'GDACS: tidak ada siklon tropis atau bencana besar yang relevan.',
    ]);

    // 9. TREN
    L.h1(9, 'Tren hotspot');
    const tr = trenHarian(X, k);
    if (tr.hari.length) { L.grafik(tr.hari, tr.seri, `Hotspot ≤ 5 km dari aset ${k} per hari (menurut jarak)`); L.catatan('Nilai per hari = pengambilan terakhir hari itu; hari ini dapat bertambah.'); }
    else L.para('Riwayat belum tersedia.');

    // 10. REKOMENDASI
    L.h1(10, 'Rekomendasi tindak lanjut');
    const rk = rekomendasiArea(X, R);
    L.h2('10.1', 'Usulan tindakan');
    L.butir(rk.length ? rk : ['Tidak ada temuan yang memerlukan tindakan khusus; lanjutkan pemantauan rutin.']);
    L.h2('10.2', 'Pelaporan');
    L.butir(['Tetap rujuk peringatan resmi BMKG/BNPB/PVMBG.']);

    bagianSumber(L, X, 11);
    L.selesai(`Laporan_PERTAGAS_WASPADA_${k}_${stamp(now)}.pdf`, `${tglPendek(now)} ${jamWIB(now)}`);
  }

  // ---------------------------------------------------------------- RANGKUMAN DIREKSI
  function laporanRingkasan(X) {
    const D = X.DATA, now = X.now, Rs = X.AREAS.map(k => ringkasArea(X, k));
    const rk = (X.KLAS && X.KLAS.rekap) || { kritis: [], waspada: [], pantau: [], normal: X.AREAS };
    const terburuk = rk.kritis.length ? 'kritis' : rk.waspada.length ? 'waspada' : rk.pantau.length ? 'pantau' : 'normal';
    const L = new Laporan('Rangkuman Direksi', `Rangkuman PERTAGAS WASPADA — ${tglPanjang(now)}`);
    L.X = X;
    L.kepala('RANGKUMAN UNTUK DIREKSI & MANAJEMEN · DIBUAT OTOMATIS', `Kondisi Lingkungan & Geohazard ${X.AREAS.length} Area Operasi`, null,
      [`${tglPanjang(now)}, ${jamWIB(now)} · data dashboard diproses ${waktu(D.generated_at)} · Fungsi HSSE PT Pertamina Gas`, catatanData(X)]);
    const sum = (f) => Rs.reduce((a, r) => a + f(r), 0);
    const n1 = sum(r => r.n1), n5 = sum(r => r.h.length), nRisk = sum(r => r.nRisk), nPd = sum(r => r.pd.length);
    const daftar = st => rk[st].length ? `${rk[st].length} (${rk[st].join(', ')})` : '0';
    L.spanduk(terburuk, `STATUS KESELURUHAN: ${ST[terburuk].label}`,
      `Kritis ${daftar('kritis')} · Waspada ${daftar('waspada')} · Pantau ${daftar('pantau')} · Normal ${rk.normal.length}. `
      + `Status tiap area dihitung sendiri dari skor risiko tertinggi (R = P × K) klasifikasi multi-bahaya. Hotspot adalah deteksi satelit, bukan konfirmasi kebakaran.`);
    const qMin = X.qs.slice().sort((a, b) => a.jarak_km - b.jarak_km)[0];
    const aqi150 = Rs.filter(r => r.aqiMax > 150);
    L.ubin([
      { lbl: 'AREA KRITIS / WASPADA', val: `${rk.kritis.length} / ${rk.waspada.length}`, sub: `dari ${X.AREAS.length} area; Pantau ${rk.pantau.length}, Normal ${rk.normal.length}`, col: ST[terburuk].bg },
      { lbl: 'HOTSPOT ≤ 5 KM DARI ASET', val: String(n5), sub: `≤ 1 km: ${n1} · berisiko (R ≥ 8): ${nRisk}`, col: nRisk ? C.waspada : C.ink },
      { lbl: 'HOTSPOT NASIONAL 24 JAM', val: nf(D.hotspot?.total_nasional), sub: bersih(D.hotspot?.sumber || 'SiPongi+') },
      { lbl: 'PERINGATAN DINI BMKG', val: String(nPd), sub: 'peringatan aktif mengenai aset (jumlah per area)', col: nPd ? C.waspada : C.ink },
      { lbl: 'GEMPA TERDEKAT (7 HARI)', val: qMin ? `M ${nf(qMin.mag, 1)}` : '–', sub: qMin ? `${nf(qMin.jarak_km)} km dari ${bersih(qMin.aset)} (${qMin.area})` : 'tidak ada gempa tercatat' },
    ]);

    // 1. RINGKASAN EKSEKUTIF
    L.h1(1, 'Ringkasan eksekutif');
    L.h2('1.1', 'Kondisi umum');
    L.butir([
      `**${rk.kritis.length ? `${rk.kritis.length} dari ${X.AREAS.length} area berstatus Kritis` : 'Tidak ada area berstatus Kritis'}**; Waspada ${daftar('waspada')}; Pantau ${daftar('pantau')}; Normal ${rk.normal.length}.`,
      `Hotspot dalam radius 5 km dari aset: **${n5} titik** (≤ 1 km: ${n1}), ${nRisk} di antaranya berstatus risiko Waspada/Kritis setelah memperhitungkan tutupan lahan, cuaca dan keberulangan.`,
    ]);
    const perhatian = Rs.filter(r => ['kritis', 'waspada'].includes(r.status)).sort((a, b) => b.R - a.R);
    L.h2('1.2', 'Area yang perlu perhatian');
    L.butir(perhatian.length ? perhatian.map(r => `**${r.k} — ${ST[r.status].label} (R ${r.R}):** ${bersih(r.A.pemicu ? r.A.pemicu.teks : '')}${r.A.eskalasi ? ' · naik satu tingkat (bahaya gabungan)' : ''}.`)
      : ['Tidak ada area berstatus Waspada atau Kritis.']);
    L.h2('1.3', 'Geohazard');
    const ls = Rs.filter(r => r.lsH && r.lsH.terburuk), gt = Rs.filter(r => r.pk && (kmLv(r.pk, 'tinggi') + kmLv(r.pk, 'bandang')) > 0);
    L.butir([
      qMin ? `Gempa terdekat 7 hari: **M ${nf(qMin.mag, 1)}** ${bersih(qMin.wilayah)} (${waktu(qMin.datetime)}), ${nf(qMin.jarak_km)} km dari ${bersih(qMin.aset)} (${qMin.area}); ${X.qs.some(g => g.tsunami) ? '**ada gempa berpotensi tsunami**' : 'tidak ada potensi tsunami'}.` : 'Tidak ada gempa tercatat dalam 7 hari terakhir.',
      ls.length ? `Indikasi longsor hari ini (model): ${ls.map(r => `${r.k} ${LVL[r.lsH.terburuk].toLowerCase()} (${nf(kmLv(r.lsH, 'tinggi') + kmLv(r.lsH, 'sedang') + kmLv(r.lsH, 'rendah'), 1)} km)`).join(', ')}.` : 'Model longsor tidak memberi indikasi hari ini.',
      gt.length ? `Prakiraan gerakan tanah PVMBG zona tinggi/banjir bandang: ${gt.map(r => `${r.k} ${nf(kmLv(r.pk, 'tinggi') + kmLv(r.pk, 'bandang'), 1)} km`).join(', ')}.` : 'Tidak ada pipa di zona prakiraan gerakan tanah tinggi PVMBG.',
    ]);
    L.h2('1.4', 'Cuaca, udara & peringatan dini');
    const pt = Rs.filter(r => r.pt && r.pt.terburuk === 'tinggi'), bj = Rs.filter(r => r.bj && ['tinggi', 'sedang'].includes(r.bj.terburuk)), gdA = Rs.filter(r => r.gd.length);
    L.butir([
      aqi150.length ? `Udara tidak sehat (US AQI > 150) di ${aqi150.length} area: ${aqi150.sort((a, b) => b.aqiMax - a.aqiMax).map(r => `${r.k} ${nf(r.aqiMax)}`).join(', ')}.` : 'Tidak ada area dengan US AQI > 150.',
      nPd ? `Peringatan dini BMKG aktif mengenai aset: ${Rs.filter(r => r.pd.length).map(r => `${r.k} ${r.pd.length}`).join(', ')}.` : 'Tidak ada peringatan dini cuaca BMKG yang aktif mengenai aset.',
      `Potensi petir tinggi hari ini: ${pt.length ? pt.map(r => r.k).join(', ') : 'tidak ada'} · banjir sungai sedang/tinggi: ${bj.length ? bj.map(r => r.k).join(', ') : 'tidak ada'}${gdA.length ? ` · GDACS relevan: ${gdA.map(r => r.k).join(', ')}` : ''}.`,
    ]);

    // 2. STATUS PER AREA
    L.h1(2, 'Status per area operasi');
    L.h2('2.1', 'Rekapitulasi status');
    L.butir(['kritis', 'waspada', 'pantau', 'normal'].map(s => `**${ST[s].label[0] + ST[s].label.slice(1).toLowerCase()} (${rk[s].length}):** ${rk[s].length ? rk[s].join(', ') : '–'}`));
    L.h2('2.2', 'Rincian per area');
    const ord = Rs.slice().sort((a, b) => ({ kritis: 3, waspada: 2, pantau: 1, normal: 0 }[b.status] - { kritis: 3, waspada: 2, pantau: 1, normal: 0 }[a.status]) || b.R - a.R);
    L.tabel(['Area', 'Status', 'R', 'Pemicu utama', 'US AQI maks', 'Hujan hari ini'],
      ord.map(r => [{ content: `${r.k}\n${bersih(r.nama.replace('Operation ', ''))}`, bold: false }, { badge: r.status }, r.R ? String(r.R) : '–',
        r.A.pemicu ? bersih(r.A.pemicu.teks) : 'Tidak ada temuan', r.aqiMax == null ? '–' : nf(r.aqiMax), rentang(r.hjMin, r.hjMax, 1, ' mm')]),
      [26, 23, 8, 0, 16, 24], { size: 8, sorot: i => ord[i].status === 'kritis' });

    // 3. LOKASI PRIORITAS
    L.h1(3, 'Lokasi prioritas tindak lanjut');
    const semua = [];
    Rs.forEach(r => kelompok((r.A.temuan || []).filter(t => t.R >= 8).map(t => ({ ...t, area: r.k }))).forEach(t => semua.push({ ...t, area: r.k })));
    semua.sort((a, b) => b.R - a.R || (b.n || 1) - (a.n || 1));
    if (semua.length) {
      const tp = semua.slice(0, 15);
      L.tabel(['#', 'Area', 'Jenis', 'Status', 'R', 'Keterangan', 'Koordinat'],
        tp.map((t, i) => [String(i + 1), t.area, JL[t.jenis] || t.jenis, { badge: t.status }, String(t.R), bersih(t.teks),
          t.ref ? koor(t.ref.lat, t.ref.lon) : (t.lokasi && t.lokasi[0] ? koor(t.lokasi[0][0], t.lokasi[0][1]) : '–')]),
        [6, 12, 26, 23, 7, 0, 30], { size: 7.8, sorot: i => tp[i].status === 'kritis' });
      L.catatan(`Temuan berstatus Waspada/Kritis; hotspot yang berdekatan (≤ 3 km) digabung menjadi satu klaster, koordinat = titik dengan skor tertinggi.${semua.length > 15 ? ` Ditampilkan 15 dari ${semua.length} baris.` : ''} Rincian per area tersedia di laporan detail area.`);
    } else L.para('Tidak ada temuan berstatus Waspada atau Kritis.');

    // 4. TREN
    L.h1(4, 'Tren hotspot');
    const tr = trenHarian(X, null);
    if (tr.hari.length) { L.grafik(tr.hari, tr.seri, 'Hotspot ≤ 5 km dari aset Pertagas per hari, seluruh area (menurut jarak)'); L.catatan('Nilai per hari = pengambilan terakhir hari itu; hari ini dapat bertambah.'); }

    // 5. REKOMENDASI
    L.h1(5, 'Rekomendasi tindak lanjut');
    let n = 0;
    for (const r of perhatian) { const rr = rekomendasiArea(X, r); if (!rr.length) continue; n++; L.h2(`5.${n}`, `${r.k} — ${ST[r.status].label}`); L.butir(rr.slice(0, 4)); }
    n++; L.h2(`5.${n}`, 'Umum');
    L.butir([
      aqi150.length ? `Sediakan masker partikulat bagi pekerja lapangan di ${aqi150.map(r => r.k).join(', ')} (US AQI > 150).` : null,
      'Tetap rujuk peringatan resmi BMKG/BNPB/PVMBG. Laporan detail tiap area dapat diunduh dari tab wilayah di dashboard.',
    ]);

    bagianSumber(L, X, 6);
    L.selesai(`Rangkuman_PERTAGAS_WASPADA_${stamp(now)}.pdf`, `${tglPendek(now)} ${jamWIB(now)}`);
  }

  const stamp = d => { const p = Object.fromEntries(new Intl.DateTimeFormat('en-CA', { ...tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).formatToParts(d).map(x => [x.type, x.value])); return `${p.year}-${p.month}-${p.day}_${p.hour}${p.minute}`; };
  const tglPendek = d => d.toLocaleDateString('id-ID', { ...tz, day: 'numeric', month: 'short', year: 'numeric' });

  // ---------------------------------------------------------------- tombol
  async function unduh(target) {
    const btn = document.getElementById('btnPdf'), lama = btn && btn.innerHTML;
    try {
      if (btn) { btn.disabled = true; btn.textContent = 'Menyusun PDF…'; }
      await siapkan();
      const X = konteks();
      if (!X.AREAS.length) throw new Error('data dashboard belum dimuat');
      if (target && X.AREAS.includes(target)) laporanArea(X, target); else laporanRingkasan(X);
    } catch (e) {
      console.error('laporan PDF', e);
      alert('Laporan PDF gagal dibuat: ' + (e && e.message ? e.message : e));
    } finally { if (btn) { btn.disabled = false; btn.innerHTML = lama; } }
  }
  function labelTombol() {
    const btn = document.getElementById('btnPdf'); if (!btn) return;
    const st = G('state'), OVv = G('OV'), tab = st && st.tab;
    const area = tab && tab !== OVv;
    btn.innerHTML = area ? `↓ Unduh laporan ${tab} (PDF)` : '↓ Unduh rangkuman direksi (PDF)';
    btn.title = area ? `Buat laporan PDF detail ${tab} dari data yang sedang tampil` : 'Buat laporan PDF rangkuman seluruh area operasi untuk direksi dari data yang sedang tampil';
  }
  function pasang() {
    const btn = document.getElementById('btnPdf'); if (!btn) return;
    btn.onclick = () => { const st = G('state'), OVv = G('OV'); unduh(st && st.tab !== OVv ? st.tab : null); };
    labelTombol();
    const t = document.getElementById('viewTitle');
    if (t && window.MutationObserver) new MutationObserver(labelTombol).observe(t, { childList: true, subtree: true });
  }
  window.LaporanWaspada = { unduh, area: k => unduh(k), ringkasan: () => unduh(null) };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', pasang); else pasang();
})();
