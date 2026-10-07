set -x
O=probe/out; rm -rf $O; mkdir -p $O
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"
curl -sSL -A "$UA" -m 60 -D $O/portal.headers "https://vsi.esdm.go.id/portalmbg/" -o $O/portal.html
ls -la $O
grep -oE '(src|href)="[^"]+"' $O/portal.html | sort -u > $O/links.txt
grep -oE "https?://[^\"' <>)]+" $O/portal.html | sort -u > $O/urls_html.txt
mkdir -p $O/js
for s in $(grep -oE 'src="[^"]+\.js[^"]*"' $O/portal.html | sed 's/src="//;s/"$//'); do
  case "$s" in http*) u="$s";; //*) u="https:$s";; /*) u="https://vsi.esdm.go.id$s";; *) u="https://vsi.esdm.go.id/portalmbg/$s";; esac
  f=$O/js/$(echo "$s" | sed 's#[/:?=&]#_#g' | cut -c1-120)
  curl -sSL -A "$UA" -m 60 "$u" -o "$f"; echo "$u $(wc -c <"$f")" >> $O/js_list.txt
done
cat $O/js/* 2>/dev/null | grep -oE "https?://[^\"' <>)\`]+" | sort -u > $O/urls_js.txt
cat $O/js/* $O/portal.html 2>/dev/null | grep -oE "[\"'\`][^\"'\`]*(api|MapServer|FeatureServer|wms|geojson|gerakan|prakiraan|tanah|json)[^\"'\`]*[\"'\`]" | sort -u | head -500 > $O/candidates.txt
