set -x
O=probe/out2; rm -rf $O; mkdir -p $O
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
W="https://vsi.esdm.go.id/data/api/public/geohazard/map-layer/wms"
H=(-A "$UA" -H "Referer: https://vsi.esdm.go.id/portalmbg/" -m 120 -sS -L)
c(){ n=$1; shift; curl "${H[@]}" -D $O/$n.h -o $O/$n.body "$@"; echo "$n $(head -1 $O/$n.h) $(grep -i '^content-type' $O/$n.h | tail -1) $(wc -c <$O/$n.body)" >> $O/summary.txt; }
c caps "$W?service=WMS&request=GetCapabilities&version=1.3.0"
grep -o -E '<Name>[^<]*prakiraan[^<]*</Name>' $O/caps.body | sort -u > $O/prakiraan_layers.txt
grep -o -E '<Name>pmbgi:[^<]*</Name>' $O/caps.body | sort -u > $O/all_layers.txt
WF="https://vsi.esdm.go.id/data/api/public/geohazard/map-layer/wfs"
c wfs_caps "$WF?service=WFS&request=GetCapabilities"
c wfs_on_wms "$W?service=WFS&version=1.0.0&request=GetFeature&typeName=pmbgi:prakiraan_2026_10&outputFormat=application/json&maxFeatures=3"
c wfs_path "$WF?service=WFS&version=1.0.0&request=GetFeature&typeName=pmbgi:prakiraan_2026_10&outputFormat=application/json&maxFeatures=3"
c wfs_ows "https://vsi.esdm.go.id/data/api/public/geohazard/map-layer/ows?service=WFS&version=1.0.0&request=GetFeature&typeName=pmbgi:prakiraan_2026_10&outputFormat=application/json&maxFeatures=3"
c describe "$W?service=WMS&version=1.1.1&request=DescribeLayer&layers=pmbgi:prakiraan_2026_10"
# GetFeatureInfo at Ogan Ilir / Bogor
c gfi_bogor "$W?service=WMS&version=1.1.1&request=GetFeatureInfo&layers=pmbgi:prakiraan_2026_10&query_layers=pmbgi:prakiraan_2026_10&styles=&srs=EPSG:4326&bbox=106.5,-6.8,107.0,-6.3&width=101&height=101&x=50&y=50&info_format=application/json&feature_count=5"
c gfi_aceh "$W?service=WMS&version=1.1.1&request=GetFeatureInfo&layers=pmbgi:prakiraan_2026_10&query_layers=pmbgi:prakiraan_2026_10&styles=&srs=EPSG:4326&bbox=97.6,4.4,98.1,4.9&width=101&height=101&x=50&y=50&info_format=application/json&feature_count=5"
c map_png "$W?service=WMS&version=1.1.1&request=GetMap&layers=pmbgi:prakiraan_2026_10&styles=&srs=EPSG:4326&bbox=105,-8,109,-5.5&width=800&height=500&format=image/png&transparent=true"
c map_png_sep "$W?service=WMS&version=1.1.1&request=GetMap&layers=pmbgi:prakiraan_2026_9&styles=&srs=EPSG:4326&bbox=105,-8,109,-5.5&width=800&height=500&format=image/png&transparent=true"
B="https://vsi.esdm.go.id/portalmbg/api/download-asset-forecast/gertan/forecast"
c dl_vec_ind "$B/vector/2026/10/IND"
c dl_ras_ind "$B/raster/2026/10/IND"
for p in 11 12 16 32 35 64; do c dl_vec_$p "$B/vector/2026/10/$p"; done
# get-province needs CSRF/cookies
curl "${H[@]}" -c $O/cj -o /dev/null "https://vsi.esdm.go.id/portalmbg/"
TOK=$(grep XSRF-TOKEN $O/cj | awk '{print $7}' | python3 -c "import sys,urllib.parse;print(urllib.parse.unquote(sys.stdin.read().strip()))")
for id in 1 11 16 32; do curl "${H[@]}" -b $O/cj -H "X-XSRF-TOKEN: $TOK" -H "Content-Type: application/json" -X POST -d "{\"id\":\"$id\"}" -o $O/prov_$id.body -D $O/prov_$id.h "https://vsi.esdm.go.id/portalmbg/api/get-province"; echo "prov $id $(head -1 $O/prov_$id.h) $(head -c 300 $O/prov_$id.body)" >> $O/summary.txt; done
for f in $O/*.body; do file "$f" >> $O/types.txt; done
# keep files small for git
find $O -name '*.body' -size +5M -exec sh -c 'head -c 2000000 "$1" > "$1.head"; rm "$1"' _ {} \;
# rerun 1791356148
