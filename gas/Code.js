var FIRMS_KEY = PropertiesService.getScriptProperties().getProperty('FIRMS_KEY') ||
    '82b260ac3b61b77e4b4215e94bbe4a43';

var PRESETS = {
  athens:     [37.9838, 23.7275],
  thessaloniki: [40.6401, 22.9444],
  patras:     [38.2466, 21.7346],
  heraklion:  [35.3387, 25.1442],
  rhodes:     [36.4349, 28.2176],
  korinthos:  [37.9380, 22.9326]
};

var SOURCES = {
  viirs: 'VIIRS_SNPP_NRT',
  noaa20: 'VIIRS_NOAA20_NRT',
  noaa21: 'VIIRS_NOAA21_NRT',
  modis: 'MODIS_NRT',
  all: 'ALL'
};
var MULTI_SRCS = ['VIIRS_SNPP_NRT', 'VIIRS_NOAA20_NRT', 'VIIRS_NOAA21_NRT', 'MODIS_NRT'];
var MAX_RADIUS = {
  VIIRS_SNPP_NRT: 2000, VIIRS_NOAA20_NRT: 2000, VIIRS_NOAA21_NRT: 2000,
  MODIS_NRT: 2000, ALL: 2000
};
var CACHE_TTL = 600;

var API = 'https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{src}/{west},{south},{east},{north}/{days}';

function doGet(e) {
  var p = e.parameter || {};
  if (p.v === 'data') return dataResponse(p);
  if (p.v === 'zip') return zipResponse(p);
  return HtmlService.createTemplateFromFile('Index').evaluate()
    .setTitle('firewatch')
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function dataResponse(p) {
  var area = p.area || 'athens';
  var radius = parseFloat(p.radius) || 60;
  var srcKey = SOURCES[p.src] ? p.src : 'viirs';
  var src = SOURCES[srcKey];
  var days = Math.min(10, Math.max(1, parseInt(p.days) || 1));
  var lat, lon;
  if (p.gps === '1' && p.lat && p.lon) {
    lat = parseFloat(p.lat); lon = parseFloat(p.lon);
  } else if (p.lat && p.lon) {
    lat = parseFloat(p.lat); lon = parseFloat(p.lon);
  } else if (PRESETS[area]) {
    lat = PRESETS[area][0]; lon = PRESETS[area][1];
  } else {
    lat = 37.9838; lon = 23.7275;
  }
  radius = Math.min(radius, MAX_RADIUS[src]);
  radius = Math.max(radius, 5);

  var cache = CacheService.getScriptCache();
  var cacheKey = 'fires_' + src + '_' + lat.toFixed(3) + '_' + lon.toFixed(3) + '_' + radius + '_' + days;
  var obj;
  var cached = cache.get(cacheKey);
  if (cached) {
    obj = JSON.parse(cached);
  } else {
    try {
      obj = {
        area: area, lat: lat, lon: lon, radius: radius, src: srcKey,
        fires: fetchFires(src, lat, lon, radius, days),
        updated: new Date().toISOString()
      };
      cache.put(cacheKey, JSON.stringify(obj), CACHE_TTL);
    } catch (err) {
      return json({ area: area, lat: lat, lon: lon, radius: radius, src: srcKey,
                    fires: [], error: String(err), updated: new Date().toISOString() });
    }
  }
  // Place names for the nearest fires (Maps service, cached 6h)
  if (p.geo !== '0') {
    var fires = obj.fires || [];
    for (var i = 0; i < fires.length && i < 4; i++) {
      var f = fires[i];
      if (f.near) continue;
      var ck = 'place_' + f.lat.toFixed(3) + '_' + f.lon.toFixed(3);
      var hit = cache.get(ck);
      if (hit) { f.near = hit; continue; }
      var near = nearName(f.lat, f.lon);
      if (near) f.near = near;
    }
  }
  return json(obj);
}

function zipResponse(p) {
  try {
    var country = p.country || 'Greece';
    var g = Maps.newGeocoder();
    if (/^[A-Za-z]{2}$/.test(country)) g.setRegion(country.toUpperCase());
    var res = g.geocode(p.zip + ', ' + country);
    if (!res.results || !res.results.length) {
      return json({ error: 'no place found for ' + p.zip + ' in ' + country });
    }
    var r = res.results[0];
    var name = shortName(r.address_components) || r.formatted_address.split(',')[0];
    return json({ lat: r.geometry.location.lat, lon: r.geometry.location.lng, name: name });
  } catch (err) {
    return json({ error: String(err) });
  }
}

function shortName(comps) {
  var pick = ['locality', 'postal_town', 'town', 'village', 'suburb', 'neighborhood',
              'administrative_area_level_3', 'administrative_area_level_2'];
  for (var i = 0; i < pick.length; i++) {
    for (var j = 0; j < comps.length; j++) {
      if (comps[j].types.indexOf(pick[i]) >= 0) return comps[j].long_name;
    }
  }
  return '';
}

function nearName(lat, lon) {
  try {
    var res = Maps.newGeocoder().reverseGeocode(lat, lon);
    if (!res.results || !res.results.length) return null;
    var comps = res.results[0].address_components;
    var name = shortName(comps);
    if (!name) return null;
    var code = '';
    for (var i = 0; i < comps.length; i++) {
      if (comps[i].types.indexOf('postal_code') >= 0) code = comps[i].long_name;
    }
    var out = code ? name + ' ·' + code : name;
    CacheService.getScriptCache().put('place_' + lat.toFixed(3) + '_' + lon.toFixed(3), out, 21600);
    return out;
  } catch (_) { return null; }
}

function fetchFires(src, lat, lon, radius, days) {
  var dlat = radius / 111.32;
  var dlon = radius / (111.32 * Math.cos(lat * Math.PI / 180));
  var west = lon - dlon, south = lat - dlat, east = lon + dlon, north = lat + dlat;
  if (src === 'ALL') {
    var all = [];
    for (var i = 0; i < MULTI_SRCS.length; i++) {
      all = all.concat(fetchOne(MULTI_SRCS[i], lat, lon, radius, west, south, east, north, days));
    }
    return mergeFires(all);
  }
  return fetchOne(src, lat, lon, radius, west, south, east, north, days);
}

function fetchOne(src, lat, lon, radius, west, south, east, north, days) {
  var url = API.replace('{key}', FIRMS_KEY).replace('{src}', src)
    .replace('{west}', west).replace('{south}', south)
    .replace('{east}', east).replace('{north}', north).replace('{days}', days);
  var resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  if (resp.getResponseCode() !== 200) return [];
  var rows = Utilities.parseCsv(resp.getContentText());
  if (rows.length < 2) return [];
  var header = rows[0];
  var bi = header.indexOf('bright_ti4'); if (bi < 0) bi = header.indexOf('brightness');
  var idx = {
    lat: header.indexOf('latitude'), lon: header.indexOf('longitude'),
    frp: header.indexOf('frp'), bright: bi, conf: header.indexOf('confidence'),
    sat: header.indexOf('satellite'), dn: header.indexOf('daynight'),
    date: header.indexOf('acq_date'), time: header.indexOf('acq_time')
  };
  var fires = [];
  for (var i = 1; i < rows.length; i++) {
    var r = rows[i];
    var flat = parseFloat(r[idx.lat]), flon = parseFloat(r[idx.lon]);
    if (isNaN(flat) || isNaN(flon)) continue;
    var d = haversineKm(lat, lon, flat, flon);
    if (d > radius + 0.1) continue;
    fires.push({
      lat: flat, lon: flon, dist: d,
      frp: parseFloat(r[idx.frp]) || 0,
      bright: parseFloat(r[idx.bright]) || 0,
      conf: r[idx.conf] || '', sat: r[idx.sat] || '',
      dn: r[idx.dn] || '', acq_date: r[idx.date] || '', acq_time: r[idx.time] || '0000',
      bearing: bearingDeg(lat, lon, flat, flon)
    });
  }
  fires.sort(function(a, b) { return a.dist - b.dist; });
  return fires.slice(0, 25);
}

// Cluster detections from different satellites within 1.5 km, keep strongest,
// list all satellites that saw the fire (same behavior as the Python tool).
function mergeFires(fires) {
  var merged = [];
  var sorted = fires.slice().sort(function(a, b) { return b.frp - a.frp; });
  for (var i = 0; i < sorted.length; i++) {
    var f = sorted[i];
    var hit = null;
    for (var j = 0; j < merged.length; j++) {
      if (haversineKm(f.lat, f.lon, merged[j].lat, merged[j].lon) <= 1.5) { hit = merged[j]; break; }
    }
    if (hit) {
      if (hit.sat.indexOf(f.sat) < 0) hit.sat = hit.sat ? hit.sat + ',' + f.sat : f.sat;
    } else {
      merged.push(f);
    }
  }
  merged.sort(function(a, b) { return a.dist - b.dist; });
  return merged.slice(0, 25);
}

function haversineKm(lat1, lon1, lat2, lon2) {
  var R = 6371;
  var dLat = (lat2 - lat1) * Math.PI / 180;
  var dLon = (lon2 - lon1) * Math.PI / 180;
  var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
          Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
          Math.sin(dLon / 2) * Math.sin(dLon / 2);
  return 2 * R * Math.asin(Math.sqrt(a));
}

function bearingDeg(lat1, lon1, lat2, lon2) {
  var dLon = (lon2 - lon1) * Math.PI / 180;
  var y = Math.sin(dLon) * Math.cos(lat2 * Math.PI / 180);
  var x = Math.cos(lat1 * Math.PI / 180) * Math.sin(lat2 * Math.PI / 180) -
          Math.sin(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.cos(dLon);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

// Run once in the editor to set up a 10-minute refresh trigger for preset caches.
function setup() {
  ScriptApp.newTrigger('warmCache').timeBased().everyMinutes(10).create();
}

function warmCache() {
  var cache = CacheService.getScriptCache();
  for (var name in PRESETS) {
    var lat = PRESETS[name][0], lon = PRESETS[name][1];
    for (var r = 0; r < 4; r++) {
      var radius = [30, 60, 120, 250][r];
      var key = 'fires_VIIRS_SNPP_NRT_' + lat.toFixed(3) + '_' + lon.toFixed(3) + '_' + radius + '_1';
      try {
        var fires = fetchFires('VIIRS_SNPP_NRT', lat, lon, radius, 1);
        cache.put(key, JSON.stringify({
          area: name, lat: lat, lon: lon, radius: radius, src: 'viirs',
          fires: fires, updated: new Date().toISOString()
        }), CACHE_TTL);
      } catch (_) {}
      Utilities.sleep(500);
    }
  }
}
