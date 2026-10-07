set -x
pip install -q pyshp shapely pyproj >/dev/null 2>&1
O=probe/out3; rm -rf $O; mkdir -p $O; T=$RUNNER_TEMP/z; mkdir -p $T
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
B="https://vsi.esdm.go.id/portalmbg/api/download-asset-forecast/gertan/forecast/vector"
for ym in 2026/10 2026/9 2026/11 2026/09; do p=16; f=$T/$(echo $ym|tr / _)_$p.zip
  curl -sS -L -A "$UA" -m 300 -D $f.h -o $f "$B/$ym/$p"; echo "$ym/$p $(head -1 $f.h) $(grep -i -E '^(content-disposition|content-length|last-modified)' $f.h|tr '\r\n' '  ') size=$(wc -c <$f) md5=$(md5sum $f|cut -c1-8)" >> $O/summary.txt; done
for p in 11 12 14 15 16 31 32 33 35 36 64; do f=$T/2026_10_$p.zip; t0=$(date +%s)
  curl -sS -L -A "$UA" -m 600 -D $f.h -o $f "$B/2026/10/$p"; echo "prov $p $(head -1 $f.h) size=$(wc -c <$f) dt=$(( $(date +%s)-t0 ))s" >> $O/summary.txt; done
python3 - <<'PY' >> $O/zipinfo.txt 2>&1
import zipfile,glob,io,shapefile,collections,os
for f in sorted(glob.glob(os.environ['RUNNER_TEMP']+'/z/*.zip')):
    try:
        z=zipfile.ZipFile(f)
    except Exception as e:
        print(f,'NOT ZIP',e); continue
    names=z.namelist(); print('\n##',os.path.basename(f),len(names),names[:12])
    for shp in [n for n in names if n.lower().endswith('.shp')]:
        b=shp[:-4]; g=lambda e: io.BytesIO(z.read(next(n for n in names if n.lower()==(b+e).lower())))
        try:
            prj=g('.prj').read().decode()[:160]
        except StopIteration: prj='(no prj)'
        r=shapefile.Reader(shp=g('.shp'),dbf=g('.dbf'),shx=g('.shx'))
        flds=[x[0] for x in r.fields[1:]]
        print(' shp',shp,'n=',len(r),'type',r.shapeTypeName,'bbox',[round(v,3) for v in r.bbox]); print(' prj',prj); print(' fields',flds)
        for k in [x for x in flds if x.lower() in ('zona_perki','unsur','periode','bulan','tahun','wilayah','kecamatan','kabupaten','kab_kota','prakiraan')]:
            c=collections.Counter(str(rec[flds.index(k)]) for rec in r.iterRecords()); print('  ',k,c.most_common(12))
        print('  sample',dict(zip(flds,[str(v)[:60] for v in r.record(0)])))
PY
# 1791356408
