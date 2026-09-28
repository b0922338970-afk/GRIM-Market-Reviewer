'use strict';
const {validSnapshot} = require('../server/public-snapshot');
const READ_PATH = '/rest/v1/grim_website_snapshot?id=eq.latest&select=snapshot,generated_at,source_observation,stale_after_seconds,updated_at';

function readConfig(env) {
  const raw = env.GRIM_PUBLIC_SUPABASE_URL;
  const key = env.GRIM_PUBLIC_SUPABASE_PUBLISHABLE_KEY;
  if (typeof raw !== 'string' || typeof key !== 'string' || /\s/.test(raw + key)) throw Error('CONFIG');
  const url = new URL(raw);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash ||
      url.pathname !== '/' || url.port || !url.hostname.endsWith('.supabase.co')) throw Error('CONFIG');
  const headers = {apikey: key, Accept: 'application/json'};
  if (/^sb_publishable_[A-Za-z0-9_-]+$/.test(key)) return {url: url.origin + READ_PATH, headers};
  // Decoding is a misuse guard, not JWT authentication. Supabase verifies the signature.
  if (!/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(key)) throw Error('CONFIG');
  const [head, claims] = key.split('.').slice(0,2).map(p => JSON.parse(Buffer.from(p,'base64url').toString('utf8')));
  if (claims.role !== 'anon' || !['HS256','RS256','ES256'].includes(head.alg)) throw Error('CONFIG');
  headers.Authorization = 'Bearer ' + key;
  return {url: url.origin + READ_PATH, headers};
}
async function readJson(response) {
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 131072) throw Error('OVERSIZE');
      chunks.push(Buffer.from(value));
    }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } finally {
    await reader.cancel();
  }
}
function createHandler({fetchImpl = globalThis.fetch, clock = Date.now, env = process.env} = {}) {
  return async function handler(req, res) {
    res.setHeader('Cache-Control','no-store');
    res.setHeader('Content-Type','application/json; charset=utf-8');
    res.setHeader('X-Content-Type-Options','nosniff');
    if (req.method !== 'GET') {
      res.setHeader('Allow','GET');
      res.statusCode = 405;
      return res.end(JSON.stringify({status:'UNAVAILABLE'}));
    }
    try {
      const config = readConfig(env);
      const response = await fetchImpl(config.url, {method:'GET', headers:config.headers,
        cache:'no-store', redirect:'error', signal:AbortSignal.timeout(5000)});
      if (!response.ok) {
        await response.body?.cancel();
        throw Error('UPSTREAM');
      }
      const rows = await readJson(response);
      if (!Array.isArray(rows) || rows.length !== 1) throw Error('NO_ROW');
      const row = rows[0], snapshot = row.snapshot;
      if (!validSnapshot(snapshot) || row.generated_at !== snapshot.generated_at ||
          row.source_observation !== snapshot.source_observation ||
          row.stale_after_seconds !== snapshot.stale_after_seconds) throw Error('SCHEMA');
      const now = Math.floor(clock()/1000);
      const stale = snapshot.stale || snapshot.runtime.stale || now < row.generated_at ||
        now > row.generated_at + row.stale_after_seconds || snapshot.source_updated_at === null ||
        now < snapshot.source_updated_at || now > snapshot.source_updated_at + row.stale_after_seconds;
      const updatedAt = typeof row.updated_at === 'string' &&
        /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(row.updated_at) &&
        Number.isFinite(Date.parse(row.updated_at)) ? row.updated_at : null;
      res.statusCode = 200;
      return res.end(JSON.stringify({status:'OK', snapshot, source_observation:row.source_observation,
        generated_at:row.generated_at, updated_at:updatedAt, stale}));
    } catch {
      res.statusCode = 503;
      return res.end(JSON.stringify({status:'UNAVAILABLE'}));
    }
  };
}
module.exports = createHandler();
module.exports.createHandler = createHandler;
