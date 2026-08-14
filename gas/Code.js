// Set FIRMS_KEY in script properties (Script Properties) — no hardcoded fallback.
const FIRMS_KEY = PropertiesService.getScriptProperties().getProperty('FIRMS_KEY');

const PRESETS = {
  athens: [37.9838, 23.7275],
  thessaloniki: [40.6401, 22.9444],
  patras: [38.2466, 21.7346],
  heraklion: [35.3387, 25.1442],
  rhodes: [36.4349, 28.2176],
  korinthos: [37.9380, 22.9326]
};

const SOURCES = {
  viirs: 'VIIRS_SNPP_NRT',
  noaa20: 'VIIRS_NOAA20_NRT',
  noaa21: 'VIIRS_NOAA21_NRT',
  modis: 'MODIS_NRT',
  all: 'ALL'
};
const MULTI_SRCS = ['VIIRS_SNPP_NRT', 'VIIRS_NOAA20_NRT', 'VIIRS_NOAA21_NRT', 'MODIS_NRT'];
const MAX_RADIUS = 2000;
const CACHE_TTL = 600;        // data cache, seconds (jittered +0..60 on write)
const PLACE_TTL = 21600;      // place-name positive cache, 6h
const PLACE_NEG_TTL = 3600;   // place-name negative cache, 1h (quota burn fix)
const ZIP_TTL = 604800;       // zip geocode positive cache, 7d
const GEO_MAX_FIRES = 2;      // max reverse-geocodes per request
const GEO_MAX_DIST = 50;      // only geocode fires this close (km)

const API = 'https://firms.modaps.eosdis.nasa.gov/api/area/csv/{key}/{src}/{west},{south},{east},{north}/{days}';

function doGet(e) {
  const p = e.parameter || {};
  if (p.v === 'data') return dataResponse(p);
  if (p.v === 'zip') return zipResponse(p);
  // HtmlService template mode evaluates the <?!= ?> scriptlets in Index.html
  // (PRESETS + service URL injected into the page).
  // addMetaTag viewport targets the GAS iframe chrome (the in-page meta alone is not enough on mobile).
  const out = HtmlService.createTemplateFromFile('Index').evaluate();
  out.setTitle('firewatch');
  out.addMetaTag('viewport', 'width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover');
  return out;
}

function json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function dataResponse(p) {
  const area = p.area || 'athens';
  let radius = parseFloat(p.radius) || 60;
  const srcKey = SOURCES[p.src] ? p.src : 'viirs';
  const src = SOURCES[srcKey];
  const days = Math.min(10, Math.max(1, parseInt(p.days, 10) || 1));
  const minConf = Math.min(100, Math.max(1, parseInt(p.conf, 10) || 1));
  let lat, lon;
  if (p.lat && p.lon) {
    lat = parseFloat(p.lat); lon = parseFloat(p.lon);
  } else if (PRESETS[area]) {
    lat = PRESETS[area][0]; lon = PRESETS[area][1];
  } else {
    lat = 37.9838; lon = 23.7275;
  }
  radius = Math.min(radius, MAX_RADIUS);
  radius = Math.max(radius, 5);

  const cache = CacheService.getScriptCache();
  const cacheKey = firesKey(src, lat, lon, radius, days, minConf);
  let obj = null;
  const cached = cache.get(cacheKey);
  if (cached) {
    try { obj = JSON.parse(cached); } catch (_) { obj = null; } // corrupt entry = miss
  }
  if (!obj) {
    if (!FIRMS_KEY) {
      return json({ area, lat, lon, radius, src: srcKey, conf: minConf, fires: [],
                    error: 'FIRMS_KEY not configured in script properties',
                    updated: new Date().toISOString() });
    }
    try {
      obj = {
        area, lat, lon, radius, src: srcKey, conf: minConf,
        fires: fetchFires(src, lat, lon, radius, days, minConf),
        updated: new Date().toISOString()
      };
    } catch (err) {
      return json({ area, lat, lon, radius, src: srcKey, conf: minConf, fires: [],
                    error: String(err), updated: new Date().toISOString() });
    }
  }
  // Place names for the nearest close fires (Maps service, cached 6h pos / 1h neg).
  // Write back AFTER attach so the next cache hit skips Maps on the critical path.
  if (p.geo !== '0') attachPlaceNames(obj);
  try {
    cache.put(cacheKey, JSON.stringify(obj), CACHE_TTL + Math.floor(Math.random() * 60));
  } catch (_) {}
  return json(obj);
}

function zipResponse(p) {
  const cache = CacheService.getScriptCache();
  const country = p.country || 'Greece';
  const zk = 'zip_' + String(p.zip || '').toUpperCase() + '_' + country;
  const cached = cache.get(zk);
  if (cached) {
    try { return json(JSON.parse(cached)); } catch (_) {} // corrupt entry = miss
  }
  try {
    const g = Maps.newGeocoder();
    if (/^[A-Za-z]{2}$/.test(country)) g.setRegion(country.toUpperCase());
    const res = g.geocode(p.zip + ', ' + country);
    let out;
    if (!res.results || !res.results.length) {
      out = { error: 'no place found for ' + p.zip + ' in ' + country };
      cache.put(zk, JSON.stringify(out), PLACE_NEG_TTL);
      return json(out);
    }
    const r = res.results[0];
    const name = shortName(r.address_components) || r.formatted_address.split(',')[0];
    out = { lat: r.geometry.location.lat, lon: r.geometry.location.lng, name };
    cache.put(zk, JSON.stringify(out), ZIP_TTL);
    return json(out);
  } catch (err) {
    return json({ error: String(err) });
  }
}

function shortName(comps) {
  const pick = ['locality', 'postal_town', 'town', 'village', 'suburb', 'neighborhood',
                'administrative_area_level_3', 'administrative_area_level_2'];
  for (const want of pick) {
    for (const c of comps) {
      if (c.types.indexOf(want) >= 0) return c.long_name;
    }
  }
  return '';
}

// Attach human-readable names to the nearest close fires (<=2, <=50 km).
function attachPlaceNames(obj) {
  const cache = CacheService.getScriptCache();
  const fires = obj.fires || [];
  for (let i = 0; i < fires.length && i < GEO_MAX_FIRES; i++) {
    const f = fires[i];
    if (f.dist > GEO_MAX_DIST) break; // fires sorted by dist: nothing closer after this
    if (f.near) continue;
    const ck = 'place_' + f.lat.toFixed(3) + '_' + f.lon.toFixed(3);
    const hit = cache.get(ck);
    if (hit !== null) { f.near = hit; continue; }
    f.near = placeName(f.lat, f.lon);
  }
}

// Cached reverse geocode. Cached '' (negative) is valid — use !== null, not truthiness.
function placeName(lat, lon) {
  const ck = 'place_' + lat.toFixed(3) + '_' + lon.toFixed(3);
  const hit = CacheService.getScriptCache().get(ck);
  if (hit !== null) return hit;
  try {
    const res = Maps.newGeocoder().reverseGeocode(lat, lon);
    if (!res.results || !res.results.length) {
      CacheService.getScriptCache().put(ck, '', PLACE_NEG_TTL);
      return '';
    }
    const name = shortName(res.results[0].address_components);
    if (!name) {
      CacheService.getScriptCache().put(ck, '', PLACE_NEG_TTL);
      return '';
    }
    let code = '';
    for (const c of res.results[0].address_components) {
      if (c.types.indexOf('postal_code') >= 0) code = c.long_name;
    }
    const out = code ? name + ' ·' + code : name;
    CacheService.getScriptCache().put(ck, out, PLACE_TTL);
    return out;
  } catch (err) {
    // quota/rate errors: back off for an hour instead of hammering the service
    CacheService.getScriptCache().put(ck, '', PLACE_NEG_TTL);
    return '';
  }
}

function firesKey(src, lat, lon, radius, days, minConf) {
  return 'fires_' + src + '_' + lat.toFixed(3) + '_' + lon.toFixed(3) + '_' + radius + '_' + days + '_' + (minConf || 1);
}

function confValue(c) {
  if (c == null || c === '') return 100;
  const s = String(c).trim().toLowerCase();
  if (s === 'l' || s === 'low') return 30;
  if (s === 'n' || s === 'nominal') return 50;
  if (s === 'h' || s === 'high') return 80;
  const n = parseFloat(s);
  if (!isNaN(n)) return Math.max(0, Math.min(100, n));
  return 100;
}

function fetchFires(src, lat, lon, radius, days, minConf) {
  const b = bbox(lat, lon, radius);
  if (src === 'ALL') {
    let responses;
    try {
      responses = UrlFetchApp.fetchAll(MULTI_SRCS.map(s => ({
        url: apiUrl(s, b, days), muteHttpExceptions: true, timeout: 90000
      })));
    } catch (err) { return []; }
    const all = [];
    responses.forEach((resp, i) => {
      if (resp && resp.getResponseCode() === 200) {
        all.push(...parseRows(resp.getContentText(), lat, lon, radius, minConf));
      }
    });
    return mergeFires(all);
  }
  let resp;
  try {
    resp = UrlFetchApp.fetch(apiUrl(src, b, days), { muteHttpExceptions: true, timeout: 90000 });
  } catch (err) { return []; }
  if (resp.getResponseCode() !== 200) return [];
  return parseRows(resp.getContentText(), lat, lon, radius, minConf);
}

function apiUrl(src, b, days) {
  return API.replace('{key}', FIRMS_KEY).replace('{src}', src)
    .replace('{west}', b.west).replace('{south}', b.south)
    .replace('{east}', b.east).replace('{north}', b.north).replace('{days}', days);
}

function bbox(lat, lon, radius) {
  const dlat = radius / 111.32;
  const dlon = radius / (111.32 * Math.cos(lat * Math.PI / 180));
  return { west: lon - dlon, south: lat - dlat, east: lon + dlon, north: lat + dlat };
}

function parseRows(csv, lat, lon, radius, minConf) {
  const rows = Utilities.parseCsv(csv);
  if (rows.length < 2) return [];
  const header = rows[0];
  let bi = header.indexOf('bright_ti4');
  if (bi < 0) bi = header.indexOf('brightness');
  const idx = {
    lat: header.indexOf('latitude'), lon: header.indexOf('longitude'),
    frp: header.indexOf('frp'), bright: bi, conf: header.indexOf('confidence'),
    sat: header.indexOf('satellite'), dn: header.indexOf('daynight'),
    date: header.indexOf('acq_date'), time: header.indexOf('acq_time')
  };
  const need = minConf == null ? 1 : minConf;
  const fires = [];
  for (let i = 1; i < rows.length; i++) {
    const r = rows[i];
    const flat = parseFloat(r[idx.lat]), flon = parseFloat(r[idx.lon]);
    if (isNaN(flat) || isNaN(flon)) continue;
    const d = haversineKm(lat, lon, flat, flon);
    if (d > radius + 0.1) continue;
    const rawConf = r[idx.conf] || '';
    if (confValue(rawConf) < need) continue;
    fires.push({
      lat: flat, lon: flon, dist: d,
      frp: parseFloat(r[idx.frp]) || 0,
      bright: parseFloat(r[idx.bright]) || 0,
      conf: rawConf, sat: r[idx.sat] || '',
      dn: r[idx.dn] || '', acq_date: r[idx.date] || '', acq_time: r[idx.time] || '0000',
      bearing: bearingDeg(lat, lon, flat, flon)
    });
  }
  fires.sort((a, b) => a.dist - b.dist);
  return fires.slice(0, 25);
}

// Cluster detections from different satellites within 1.5 km, keep strongest,
// list all satellites that saw the fire (same behavior as the Python tool).
function mergeFires(fires) {
  const merged = [];
  const sorted = fires.slice().sort((a, b) => b.frp - a.frp);
  for (const f of sorted) {
    let hit = null;
    for (const m of merged) {
      if (haversineKm(f.lat, f.lon, m.lat, m.lon) <= 1.5) { hit = m; break; }
    }
    if (hit) {
      if (hit.sat.indexOf(f.sat) < 0) hit.sat = hit.sat ? hit.sat + ',' + f.sat : f.sat;
    } else {
      merged.push(f);
    }
  }
  merged.sort((a, b) => a.dist - b.dist);
  return merged.slice(0, 25);
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
            Math.sin(dLon / 2) * Math.sin(dLon / 2);
  return 2 * R * Math.asin(Math.sqrt(a));
}

function bearingDeg(lat1, lon1, lat2, lon2) {
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const y = Math.sin(dLon) * Math.cos(lat2 * Math.PI / 180);
  const x = Math.cos(lat1 * Math.PI / 180) * Math.sin(lat2 * Math.PI / 180) -
            Math.sin(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.cos(dLon);
  return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

// Tooling: store/verify FIRMS_KEY via the Apps Script API (clasp run setKey "...").
function setKey(key) {
  PropertiesService.getScriptProperties().setProperty('FIRMS_KEY', key);
  return PropertiesService.getScriptProperties().getProperty('FIRMS_KEY') || 'missing';
}

// Run once in the editor to set up a 10-minute refresh trigger for preset caches.
function setup() {
  ScriptApp.newTrigger('warmCache').timeBased().everyMinutes(10).create();
}

// Pre-warm preset caches so typical first requests hit the cache. Shares
// firesKey() with dataResponse so the warm set can never silently desync.
function warmCache() {
  const lock = LockService.getScriptLock();
  if (!lock.tryLock(0)) { Logger.log('warmCache skipped: lock busy'); return; }
  try {
    const cache = CacheService.getScriptCache();
    for (const name of Object.keys(PRESETS)) {
      const [lat, lon] = PRESETS[name];
      for (const radius of [30, 60, 120, 250]) {
        try {
          const fires = fetchFires('VIIRS_SNPP_NRT', lat, lon, radius, 1);
          cache.put(firesKey('VIIRS_SNPP_NRT', lat, lon, radius, 1, 1), JSON.stringify({
            area: name, lat, lon, radius, src: 'viirs', fires, updated: new Date().toISOString()
          }), CACHE_TTL + Math.floor(Math.random() * 60));
        } catch (err) {
          Logger.log('warmCache ' + name + ' r=' + radius + ': ' + err);
        }
      }
    }
  } finally {
    lock.releaseLock();
  }
}
