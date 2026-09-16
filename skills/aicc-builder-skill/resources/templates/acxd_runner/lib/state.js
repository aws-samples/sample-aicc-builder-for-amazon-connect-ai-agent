'use strict';
/**
 * Deploy state (.deploy-state.json) + bundle placeholder resolution.
 *
 * Placeholder conventions (see validate_acxd_consistency.py):
 *   {WEBHOOK_URL}       -> state.webhookUrl (CFN output, wire-webhook-urls)
 *   {KB:<name>}         -> state.knowledgeBases[name].knowledgeBaseId
 *   {GUARDRAIL:<name>}  -> state.guardrails[name].guardrailId
 */

const fs = require('fs');
const path = require('path');

const STATE_FILE = '.deploy-state.json';

function statePath(bundleDir) {
  return path.join(bundleDir, STATE_FILE);
}

function loadState(bundleDir) {
  const p = statePath(bundleDir);
  if (!fs.existsSync(p)) {
    return { knowledgeBases: {}, guardrails: {}, resources: [] };
  }
  const state = JSON.parse(fs.readFileSync(p, 'utf-8'));
  state.knowledgeBases = state.knowledgeBases || {};
  state.guardrails = state.guardrails || {};
  state.resources = state.resources || [];
  return state;
}

function saveState(bundleDir, state) {
  fs.writeFileSync(statePath(bundleDir), JSON.stringify(state, null, 2));
}

/** Record a created/updated resource for status + cleanup (dedup by kind+id). */
function recordResource(state, kind, id, extra = {}) {
  const existing = state.resources.find((r) => r.kind === kind && r.id === id);
  if (existing) {
    Object.assign(existing, extra);
  } else {
    state.resources.push({ kind, id, ...extra });
  }
}

class UnresolvedPlaceholderError extends Error {}

/** Deep-replace placeholders in any JSON-like structure. */
function resolvePlaceholders(value, state) {
  if (typeof value === 'string') {
    return resolveString(value, state);
  }
  if (Array.isArray(value)) {
    return value.map((v) => resolvePlaceholders(v, state));
  }
  if (value && typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = resolvePlaceholders(v, state);
    }
    return out;
  }
  return value;
}

function resolveString(s, state) {
  let out = s;
  if (out.includes('{WEBHOOK_URL}')) {
    if (!state.webhookUrl) {
      throw new UnresolvedPlaceholderError(
        '{WEBHOOK_URL} used but no webhook URL is recorded — run the ' +
        'deploy-cfn-backend and wire-webhook-urls steps first');
    }
    out = out.split('{WEBHOOK_URL}').join(state.webhookUrl);
  }
  const kbMatch = /^\{KB:(.+)\}$/.exec(out);
  if (kbMatch) {
    const entry = state.knowledgeBases[kbMatch[1]];
    if (!entry || !entry.knowledgeBaseId) {
      throw new UnresolvedPlaceholderError(
        `{KB:${kbMatch[1]}} is unresolved — run upsert-knowledge-bases first`);
    }
    return entry.knowledgeBaseId;
  }
  const gMatch = /^\{GUARDRAIL:(.+)\}$/.exec(out);
  if (gMatch) {
    const entry = state.guardrails[gMatch[1]];
    if (!entry || !entry.guardrailId) {
      throw new UnresolvedPlaceholderError(
        `{GUARDRAIL:${gMatch[1]}} is unresolved — run upsert-guardrails first`);
    }
    return entry.guardrailId;
  }
  return out;
}

module.exports = {
  STATE_FILE,
  loadState,
  saveState,
  recordResource,
  resolvePlaceholders,
  UnresolvedPlaceholderError,
};
