'use strict';
/**
 * ACXD SDK client factory, retry wrapper, and log masking.
 *
 * Security: the API key is read from the environment at runtime only
 * (ACXD_API_KEY). It is never written to disk or logged — maskSecret()
 * guards every log line that could contain it.
 */

// ACXD control-plane region (preview GA region). Override with ACXD_REGION.
const DEFAULT_ACXD_REGION = 'us-west-2';

const MAX_RETRIES = 5;
const BASE_DELAY_MS = 1000;

function maskSecret(text) {
  if (!text) return text;
  // acxd_live_<prefix>.<secret> — keep the prefix, mask the secret.
  return String(text).replace(/(acxd_live_[A-Za-z0-9]{0,6})[^\s"']*/g, '$1***');
}

function requiredEnv(name) {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is not set. Export it before running (it is intentionally ` +
      'never stored in the bundle).');
  }
  return value;
}

/** Create the real SDK client. Kept behind a function so tests inject fakes. */
async function createClient() {
  const apiKey = requiredEnv('ACXD_API_KEY');
  const workspaceId = requiredEnv('ACXD_WORKSPACE_ID');
  const sdk = require('amazon-connect-acxd-sdk');

  // The ACXD control-plane API is REGIONAL and follows the workspace: a
  // workspace created from a Seoul Connect instance answers on
  // ap-northeast-2, not us-west-2. Calling the wrong region returns
  // UnauthorizedException (not a 404), which looks exactly like a bad key —
  // a live deploy died at its first ACXD step this way. So: honor an
  // explicit ACXD_REGION, otherwise PROBE the candidates with a cheap
  // authenticated call and use the region that accepts this key+workspace.
  if (process.env.ACXD_REGION) {
    const region = process.env.ACXD_REGION;
    const client = new sdk.AgenticCXDesignerClient({ apiKey, workspaceId, region });
    return { client, sdk, region };
  }
  const candidates = [];
  // The Connect instance's region (the workspace usually lives with it) …
  if (process.env.AWS_DEFAULT_REGION) candidates.push(process.env.AWS_DEFAULT_REGION);
  if (process.env.AWS_REGION && !candidates.includes(process.env.AWS_REGION)) {
    candidates.push(process.env.AWS_REGION);
  }
  // … then the launch regions.
  for (const r of [DEFAULT_ACXD_REGION, 'us-east-1']) {
    if (!candidates.includes(r)) candidates.push(r);
  }
  let lastErr = null;
  for (const region of candidates) {
    const client = new sdk.AgenticCXDesignerClient({ apiKey, workspaceId, region });
    try {
      await client.send(new sdk.ListFlowsCommand({ maxResults: 1 }));
      console.log(`  = ACXD region auto-detected: ${region}`);
      return { client, sdk, region };
    } catch (err) {
      lastErr = err;
      if (err && err.name === 'UnauthorizedException') continue; // wrong region OR bad key — try next
      // Any other error means we reached the right region (throttle, etc.).
      console.log(`  = ACXD region auto-detected: ${region} (via ${err.name})`);
      return { client, sdk, region };
    }
  }
  const tried = candidates.join(', ');
  throw new Error(
    `Unauthorized in every candidate ACXD region (${tried}). Either the API ` +
    `key is invalid/expired, or the workspace lives in another region — set ` +
    `ACXD_REGION explicitly (the region of the Connect instance the ` +
    `workspace was created from). Original error: ${lastErr && lastErr.name}`);
}

function isRetryable(err) {
  return err && (err.name === 'ThrottlingException' || err.$retryable === true);
}

function isNotFound(err) {
  return err && err.name === 'ResourceNotFoundException';
}

function isConflict(err) {
  return err && err.name === 'ConflictException';
}

/**
 * client.send with exponential backoff on throttling.
 * ``sleep`` is injectable for tests.
 */
async function sendWithRetry(client, command, { retries = MAX_RETRIES, sleep = defaultSleep } = {}) {
  let attempt = 0;
  for (;;) {
    try {
      return await client.send(command);
    } catch (err) {
      if (!isRetryable(err) || attempt >= retries) throw err;
      const delay = BASE_DELAY_MS * 2 ** attempt;
      attempt += 1;
      await sleep(delay);
    }
  }
}

function defaultSleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Generic idempotent upsert:
 *   try get → exists → update; not found → create; create-conflict → update.
 * ``ops`` are closures so each step decides which SDK commands to use.
 */
/**
 * True for the ACXD API's "shell record" failure: the resource is listed and
 * gettable, but its backing tree is absent, so writes fail with
 *   The item is not found by key {"customerId":"...","treeId":"x-Omni"}
 * Distinct from a plain not-found (which Get would have raised).
 */
function isMissingBackingRecord(err) {
  const msg = String((err && err.message) || '');
  return /item is not found by key/i.test(msg) || /treeId/.test(msg);
}

async function upsert({ get, create, update, recreate, label, log }) {
  let exists = false;
  if (get) {
    try {
      await get();
      exists = true;
    } catch (err) {
      if (!isNotFound(err)) throw err;
    }
  }
  if (exists) {
    try {
      const result = await update();
      log(`  ~ updated ${label}`);
      return { action: 'updated', result };
    } catch (err) {
      // Shell record: Get finds the resource but its backing tree is missing,
      // so Update fails with "The item is not found by key {...}". Seen live
      // after a create that failed schema validation part-way through.
      if (recreate && isMissingBackingRecord(err)) {
        const result = await recreate();
        log(`  + recreated ${label} (previous attempt left an incomplete record)`);
        return { action: 'recreated', result };
      }
      throw err;
    }
  }
  try {
    const result = await create();
    log(`  + created ${label}`);
    return { action: 'created', result };
  } catch (err) {
    if (isConflict(err) && update) {
      const result = await update();
      log(`  ~ updated ${label} (create conflicted)`);
      return { action: 'updated', result };
    }
    throw err;
  }
}

/** Poll ``check`` until it returns a truthy terminal result or times out. */
async function poll(check, { intervalMs = 5000, timeoutMs = 300000, sleep = defaultSleep } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const result = await check();
    if (result) return result;
    if (Date.now() >= deadline) {
      throw new Error(`polling timed out after ${timeoutMs}ms`);
    }
    await sleep(intervalMs);
  }
}

module.exports = {
  DEFAULT_ACXD_REGION,
  isMissingBackingRecord,
  maskSecret,
  requiredEnv,
  createClient,
  sendWithRetry,
  upsert,
  poll,
  isNotFound,
  isConflict,
  isRetryable,
  MAX_RETRIES,
  BASE_DELAY_MS,
};
