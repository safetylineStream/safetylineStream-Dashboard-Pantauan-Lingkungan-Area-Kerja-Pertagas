L.TileLayer.BetterWMS = L.TileLayer.WMS.extend({

    onAdd: function (map) {
        // Triggered when the layer is added to a map.
        //   Register a click listener, then do all the upstream WMS things
        L.TileLayer.WMS.prototype.onAdd.call(this, map);
        map.on('click', this.getFeatureInfo, this);
    },

    onRemove: function (map) {
        // Triggered when the layer is removed from a map.
        //   Unregister a click listener, then do all the upstream WMS things
        L.TileLayer.WMS.prototype.onRemove.call(this, map);
        map.off('click', this.getFeatureInfo, this);
    },

    getFeatureInfo: function (evt) {
        // Make an AJAX request to the server and hope for the best
        var url = this.getFeatureInfoUrl(evt.latlng),
            showResults = L.Util.bind(this.showGetFeatureInfo, this),
            spinner = L.TileLayer.BetterWMS.showSpinner(evt.latlng, this._map);
        $.ajax({
            url: url,
            success: function (data, status, xhr) {
                spinner.remove();
                var err = typeof data === 'string' ? null : data;
                //Fix for blank popup window
                var doc = (new DOMParser()).parseFromString(data, "text/html");
                if (doc.body.innerHTML.trim().length > 0)
                    showResults(err, evt.latlng, data);
            },
            error: function (xhr, status, error) {
                spinner.remove();
                showResults(error);
            }
        });
    },

    getFeatureInfoUrl: function (latlng) {
        // Construct a GetFeatureInfo request URL given a point
        var point = this._map.latLngToContainerPoint(latlng, this._map.getZoom()),
            size = this._map.getSize(),

            params = {
                request: 'GetFeatureInfo',
                service: 'WMS',
                srs: 'EPSG:4326',
                styles: this.wmsParams.styles,
                transparent: this.wmsParams.transparent,
                version: this.wmsParams.version,
                format: this.wmsParams.format,
                bbox: this._map.getBounds().toBBoxString(),
                height: size.y,
                width: size.x,
                layers: this.wmsParams.layers,
                query_layers: this.wmsParams.layers,
                info_format: 'text/html'
            };

        params[params.version === '1.3.0' ? 'i' : 'x'] = point.x;
        params[params.version === '1.3.0' ? 'j' : 'y'] = point.y;

        return this._url + L.Util.getParamString(params, this._url, true);
    },

    showGetFeatureInfo: function (err, latlng, content) {
        if (err) { console.log(err); return; } // do nothing if there's an error

        var features = L.TileLayer.BetterWMS.parseFeatureInfo(content);
        if (!features.length) { return; }

        L.TileLayer.BetterWMS.injectStyle();

        L.popup({
            maxWidth: 340,
            className: 'geo-popup',
            autoPanPadding: L.point(24, 24)
        })
            .setLatLng(latlng)
            .setContent(L.TileLayer.BetterWMS.render(features))
            .openOn(this._map);
    }
});

/*
 * GeoServer's default GetFeatureInfo HTML is a table one column per database column: 12 of them for
 * geologi_litologi, wider than any popup, with its own headers truncated mid-word ("st_area(sh").
 * Half are internals - fid, objectid, srs_id, and two geometry measurements in decimal degrees that
 * mean nothing to anyone reading a map.
 *
 * So the response is re-read rather than re-styled. A wide table is the wrong shape for one feature
 * with a dozen attributes; a vertical list is the right one, and it fits a popup without scrolling.
 *
 * The alternative was a FreeMarker template in the GeoServer data directory, which is where this
 * belongs long-term. It is not done here because that GeoServer serves other consumers and this is
 * a change to how pmbgi presents data, not to what the API returns.
 */

/** Database bookkeeping and geometry math. Never useful to a reader, always present. */
L.TileLayer.BetterWMS.HIDDEN = /^(fid|gid|id|objectid.*|srs_id|shape.*|st_area.*|st_length.*|the_geom|geom)$/i;

/** Columns whose meaning is not guessable from the abbreviation. */
L.TileLayer.BetterWMS.LABELS = {
    namobj: 'Nama',
    umurobj: 'Umur',
    remark: 'Keterangan',
    fcode: 'Kode unsur',
    simobj: 'Simbol',
    metadata: 'Sumber',
    unsur: 'Unsur',
    keterangan: 'Keterangan',
    wilayah: 'Wilayah',
    zona: 'Zona',
    kelas: 'Kelas',
    nama: 'Nama',
    jenis: 'Jenis',
    sesar: 'Sesar',
    sliprate: 'Laju slip',
    magnitudo: 'Magnitudo'
};

L.TileLayer.BetterWMS.parseFeatureInfo = function (html) {
    var doc = (new DOMParser()).parseFromString(html, 'text/html');
    var out = [];

    Array.prototype.forEach.call(doc.querySelectorAll('table.featureInfo'), function (table) {
        var caption = table.querySelector('caption');
        var layer = caption ? caption.textContent.trim() : '';
        var rows = Array.prototype.slice.call(table.querySelectorAll('tr'));
        if (rows.length < 2) { return; }

        var keys = Array.prototype.map.call(rows[0].querySelectorAll('th'), function (th) {
            return th.textContent.trim();
        });

        rows.slice(1).forEach(function (row) {
            var cells = row.querySelectorAll('td');
            if (!cells.length) { return; }

            var fields = [];
            var symbol = null;
            var title = null;

            Array.prototype.forEach.call(cells, function (cell, i) {
                var key = keys[i] || '';
                var value = cell.textContent.trim();
                if (!value || L.TileLayer.BetterWMS.HIDDEN.test(key)) { return; }

                // Promoted out of the list and into the header, so not repeated below it.
                if (key.toLowerCase() === 'simobj') { symbol = value; return; }
                if (key.toLowerCase() === 'namobj' || key.toLowerCase() === 'nama') {
                    if (!title) { title = value; return; }
                }

                fields.push([key, value]);
            });

            // Layers without namobj still need a headline: take the first remaining value rather
            // than showing a blank header.
            if (!title && fields.length) { title = fields.shift()[1]; }
            if (!title && !symbol) { return; }

            out.push({ layer: layer, symbol: symbol, title: title, fields: fields });
        });
    });

    return out;
};

L.TileLayer.BetterWMS.render = function (features) {
    var esc = function (s) {
        return String(s).replace(/[&<>"]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
        });
    };

    var humanise = function (key) {
        var known = L.TileLayer.BetterWMS.LABELS[key.toLowerCase()];
        if (known) { return known; }
        return key.replace(/_/g, ' ').replace(/^./, function (c) { return c.toUpperCase(); });
    };

    // Codes and filenames are identifiers, not prose - set them monospaced so they are read
    // character by character.
    var isCode = function (key, value) {
        return /^(fcode|metadata|kode.*)$/i.test(key) || /\.(xml|shp|zip)$/i.test(value);
    };

    var html = features.map(function (f) {
        var parts = ['<div class="geo-feature">'];

        parts.push('<div class="geo-head">');
        if (f.symbol) {
            parts.push('<span class="geo-symbol">' + esc(f.symbol) + '</span>');
        }
        if (f.title) {
            parts.push('<h3 class="geo-title">' + esc(f.title) + '</h3>');
        }
        parts.push('</div>');

        if (f.fields.length) {
            parts.push('<dl class="geo-fields">');
            f.fields.forEach(function (pair) {
                parts.push(
                    '<dt>' + esc(humanise(pair[0])) + '</dt>' +
                    '<dd' + (isCode(pair[0], pair[1]) ? ' class="geo-code"' : '') + '>' +
                    esc(pair[1]) + '</dd>'
                );
            });
            parts.push('</dl>');
        }

        if (f.layer) {
            parts.push('<p class="geo-layer">' + esc(humanise(f.layer)) + '</p>');
        }

        parts.push('</div>');
        return parts.join('');
    }).join('');

    return '<div class="geo-popup-body">' + html + '</div>';
};

L.TileLayer.BetterWMS.injectStyle = function () {
    if (document.getElementById('geo-popup-style')) { return; }

    var style = document.createElement('style');
    style.id = 'geo-popup-style';
    style.textContent = [
        /* The stamp. On a printed geological sheet every polygon carries its unit symbol, and the
           legend maps symbol to lithology to age - it is the first thing read, so it leads here.
           Portal yellow, from the navbar. Deliberately NOT colour-coded by geological age: that
           scale is standardised by the ICS, and an invented one would be confidently wrong. */
        '.geo-popup .geo-symbol{display:inline-flex;align-items:center;justify-content:center;',
        'min-width:2.75rem;height:2.75rem;padding:0 .5rem;border-radius:3px;',
        'background:#020000;color:#fee50f;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;',
        'font-size:1.0625rem;font-weight:600;letter-spacing:.04em;flex:none}',

        '.geo-popup .leaflet-popup-content-wrapper{border-radius:6px;padding:0;',
        'box-shadow:0 6px 28px rgba(2,0,0,.22)}',
        /* Do NOT override the width Leaflet measures and sets inline here. Doing that left the
           popup with no definite width, the grid below collapsed its value column to min-content,
           and every value wrapped one character per line. The width belongs on .geo-popup-body. */
        '.geo-popup .leaflet-popup-content{margin:0;line-height:1.45}',

        '.geo-popup-body{box-sizing:border-box;width:300px;max-width:100%;',
        'padding:1rem 1.125rem 0.875rem}',
        '.geo-feature + .geo-feature{margin-top:.875rem;padding-top:.875rem;',
        'border-top:1px solid rgba(2,0,0,.10)}',

        '.geo-popup .geo-head{display:flex;align-items:center;gap:.625rem;margin-bottom:.75rem}',
        '.geo-popup .geo-title{margin:0;font-size:1rem;font-weight:650;line-height:1.25;color:#020000}',

        /* minmax(0,1fr), not 1fr: a bare 1fr floors at min-content, which is the widest unbreakable
           word - and with break-anywhere that floor becomes one character. */
        '.geo-popup .geo-fields{margin:0;display:grid;grid-template-columns:5.5rem minmax(0,1fr);',
        'column-gap:.875rem;row-gap:.5rem;align-items:baseline}',
        /* Labels as quiet eyebrows: the value is the content, the label is signposting. */
        '.geo-popup .geo-fields dt{margin:0;font-size:.6875rem;font-weight:600;text-transform:uppercase;',
        'letter-spacing:.05em;color:#6b6b66;line-height:1.5}',
        '.geo-popup .geo-fields dd{margin:0;font-size:.875rem;color:#1c1c19;overflow-wrap:break-word}',
        /* Filenames have no spaces, so they need break-all to wrap at all - safe only because the
           column now has a real width to break against. */
        '.geo-popup .geo-fields dd.geo-code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;',
        'font-size:.75rem;color:#4a4a45;word-break:break-all;line-height:1.4}',

        '.geo-popup .geo-layer{margin:.875rem 0 0;padding-top:.625rem;',
        'border-top:1px solid rgba(2,0,0,.08);font-size:.6875rem;text-transform:uppercase;',
        'letter-spacing:.06em;color:#8a8a83}',

        '@media (prefers-reduced-motion:reduce){.geo-popup .leaflet-popup{transition:none}}'
    ].join('');

    document.head.appendChild(style);
};

/** Dropped at the click point the instant a GetFeatureInfo request goes out, gone the instant it
 *  lands. Same stamp as the popup's geo-symbol - ink ring, portal yellow lead - so the wait reads
 *  as the same object as the answer that follows it, not a generic spinner bolted on. */
L.TileLayer.BetterWMS.showSpinner = function (latlng, map) {
    L.TileLayer.BetterWMS.injectSpinnerStyle();
    var icon = L.divIcon({
        className: 'geo-spinner-icon',
        html: '<span class="geo-spinner"></span>',
        iconSize: [26, 26],
        iconAnchor: [13, 13]
    });
    return L.marker(latlng, {
        icon: icon,
        interactive: false,
        keyboard: false,
        zIndexOffset: 1000
    }).addTo(map);
};

L.TileLayer.BetterWMS.injectSpinnerStyle = function () {
    if (document.getElementById('geo-spinner-style')) { return; }

    var style = document.createElement('style');
    style.id = 'geo-spinner-style';
    style.textContent = [
        '.geo-spinner-icon{background:none;border:none}',
        '.geo-spinner{display:block;width:26px;height:26px;border-radius:50%;',
        'border:3px solid #020000;border-top-color:#fee50f;',
        'animation:geo-spin .7s linear infinite}',
        '@media (prefers-reduced-motion:reduce){.geo-spinner{animation:none;',
        'border-top-color:#020000;opacity:.55}}',
        '@keyframes geo-spin{to{transform:rotate(360deg)}}'
    ].join('');

    document.head.appendChild(style);
};

L.tileLayer.betterWms = function (t, i) {
    return new L.TileLayer.BetterWMS(t, i);
};
