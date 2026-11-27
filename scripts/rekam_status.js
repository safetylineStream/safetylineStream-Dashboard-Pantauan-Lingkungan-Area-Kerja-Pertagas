#!/usr/bin/env node
/*
 * rekam_status.js — Rekam status harian tiap area operasi (klasifikasi multi-bahaya) ke
 * data/history/status_area.json, supaya dashboard bisa menampilkan "Berubah sejak kemarin" dan
 * riwayat status 30 hari.
 *
 * Memakai rumusan yang SAMA dengan dashboard (klasifikasi.js) dan berkas data yang sama.
 * Satu entri per tanggal (WIB); run berikutnya pada tanggal yang sama menimpa entri hari itu, sehingga
 * entri suatu tanggal = kondisi terakhir yang terekam pada hari tersebut.
 *
 * Pemakaian:
 *   node scripts/rekam_status.js                      → data repositori ini, waktu sekarang
 *   node scripts/rekam_status.js --akar DIR --waktu ISO  → (isi ulang riwayat) data dari folder lain, waktu tertentu
 *   node scripts/rekam_status.js --keluar FILE        → tulis ke berkas lain
 */
'use strict';
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..');
const K = require(path.join(REPO, 'klasifikasi.js'));
const arg = n => { const i = process.argv.indexOf(n); return i > 0 ? process.argv[i + 1] : null; };
const AKAR = path.resolve(arg('--akar') || REPO);
const KELUAR = path.resolve(arg('--keluar') || path.join(REPO, 'data', 'history', 'status_area.json'));
const NOW = arg('--waktu') ? new Date(arg('--waktu')) : new Date();
const SIMPAN_HARI = 90;
const pendek = t => (t && t.length > 140 ? t.slice(0, 139) + '…' : t);

function baca(rel) {
  try { return JSON.parse(fs.readFileSync(path.join(AKAR, rel), 'utf8')); } catch (e) { return null; }
}
const tglWIB = d => new Date(d.getTime() + 7 * 3.6e6).toISOString().slice(0, 10);

function main() {
  const data = baca('data/latest.json');
  if (!data || !data.areas) { console.error('data/latest.json tidak ada/rusak — tidak merekam.'); process.exit(0); }
  const dk = baca('config/sumber_panas_dikenal.json');
  const terpadu = baca('data/lhasa/terpadu.json');
  const pd = baca('data/peringatan_cuaca/peringatan.json');
  const r = K.hitung({
    data, lhasa: baca('data/lhasa/lhasa.json'), ruasLhasa: baca('data/lhasa/ruas.geojson'), terpadu,
    dikenal: (dk && Array.isArray(dk.lokasi)) ? dk.lokasi : [], now: NOW,
    tambahan: { peringatan: pd, banjir: baca('data/banjir/banjir.json'), petir: baca('data/petir_model/petir.json'), gdacs: baca('data/gdacs/gdacs.json') },
  });
  const area = {};
  for (const [a, v] of Object.entries(r.area)) {
    area[a] = {
      status: v.status, R: v.R, pemicu: v.pemicu ? pendek(v.pemicu.teks) : null, jenis: v.pemicu ? v.pemicu.jenis : null,
      eskalasi: v.eskalasi || null,
      klaster: Object.fromEntries(Object.entries(v.klaster || {}).map(([k, x]) => [k, x.status === 'normal' ? { status: 'normal', R: x.R }
        : { status: x.status, R: x.R, teks: x.temuan[0] ? pendek(x.temuan[0].teks) : null }])),
    };
  }
  let riw = {};
  try { riw = JSON.parse(fs.readFileSync(KELUAR, 'utf8')); } catch (e) { riw = {}; }
  const hari = riw.hari || {};
  const tgl = tglWIB(NOW);
  hari[tgl] = { waktu: NOW.toISOString(), data_utama: data.generated_at || null,
    metode: terpadu && terpadu.produk ? 'terpadu' : 'lama', rekap: Object.fromEntries(Object.entries(r.rekap).map(([k, v]) => [k, v.length])), area };
  const urut = Object.keys(hari).sort().slice(-SIMPAN_HARI);
  const out = {
    catatan: 'Status harian per area (klasifikasi multi-bahaya klasifikasi.js). Satu entri per tanggal WIB = kondisi terakhir yang terekam hari itu. ' +
      'metode "lama" = gerakan tanah dinilai per area (sebelum model terpadu).',
    diperbarui: NOW.toISOString(), hari: Object.fromEntries(urut.map(k => [k, hari[k]])),
  };
  fs.mkdirSync(path.dirname(KELUAR), { recursive: true });
  fs.writeFileSync(KELUAR, JSON.stringify(out));
  console.log(`Status ${tgl} terekam (${hari[tgl].metode}): ` + Object.entries(area).map(([a, v]) => `${a} ${v.status}${v.R ? ' R' + v.R : ''}`).join(' · '));
}
main();
