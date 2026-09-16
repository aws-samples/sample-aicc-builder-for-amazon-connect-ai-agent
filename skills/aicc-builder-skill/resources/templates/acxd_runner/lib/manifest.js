'use strict';
/**
 * Manifest loading + sanity validation.
 *
 * Full JSON-Schema validation happens at generation time (Python,
 * deploy_manifest.schema.json). The runner re-checks the invariants it
 * depends on so a hand-edited manifest fails fast with a clear message.
 */

const fs = require('fs');
const path = require('path');

const KNOWN_STEP_TYPES = [
  'deploy-cfn-backend',
  'wire-webhook-urls',
  'upsert-secrets',
  'upsert-slot-types',
  'upsert-context-variables',
  'upsert-data-requests',
  'upsert-flows',
  'upsert-knowledge-bases',
  'upsert-guardrails',
  'compose-application',
  'build-application',
  'deploy-application',
  'import-contact-flows',
  // Reserved (D4): schema-valid, but not executable until the ACXD SDK
  // exposes Scenario/Simulation/Evaluation APIs.
  'run-scenario',
  'run-simulation',
  'run-evaluation',
];

const RESERVED_STEP_TYPES = ['run-scenario', 'run-simulation', 'run-evaluation'];

const FILE_STEP_TYPES = [
  'upsert-secrets', 'upsert-slot-types', 'upsert-context-variables',
  'upsert-data-requests', 'upsert-flows', 'upsert-knowledge-bases',
  'upsert-guardrails', 'import-contact-flows',
];

class ManifestError extends Error {}

function validateManifest(manifest) {
  const problems = [];
  if (!manifest || typeof manifest !== 'object') {
    return ['manifest must be a JSON object'];
  }
  if (manifest.manifestVersion !== '1.0') {
    problems.push(`unsupported manifestVersion ${JSON.stringify(manifest.manifestVersion)} (expected "1.0")`);
  }
  if (!manifest.project || !/^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$/.test(manifest.project)) {
    problems.push('project must be kebab-case, 3-64 chars');
  }
  if (!Array.isArray(manifest.steps) || manifest.steps.length === 0) {
    problems.push('steps must be a non-empty array');
    return problems;
  }
  manifest.steps.forEach((step, i) => {
    if (!step || typeof step !== 'object') {
      problems.push(`steps[${i}] must be an object`);
      return;
    }
    if (!KNOWN_STEP_TYPES.includes(step.type)) {
      problems.push(`steps[${i}].type ${JSON.stringify(step.type)} is unknown`);
      return;
    }
    if (RESERVED_STEP_TYPES.includes(step.type)) {
      problems.push(
        `steps[${i}].type ${step.type} is reserved: the ACXD SDK does not ` +
        'expose test-asset APIs yet (decision D4). Remove this step.');
    }
    if (FILE_STEP_TYPES.includes(step.type)) {
      const files = step.params && step.params.files;
      if (!Array.isArray(files) || files.length === 0) {
        problems.push(`steps[${i}] (${step.type}) requires params.files (non-empty array)`);
      }
    }
    if (step.type === 'compose-application' && !(step.params && step.params.file)) {
      problems.push(`steps[${i}] (compose-application) requires params.file`);
    }
    if (step.type === 'deploy-cfn-backend' && !(step.params && step.params.templatePath)) {
      problems.push(`steps[${i}] (deploy-cfn-backend) requires params.templatePath`);
    }
  });
  return problems;
}

function loadManifest(manifestPath) {
  let raw;
  try {
    raw = fs.readFileSync(manifestPath, 'utf-8');
  } catch (e) {
    throw new ManifestError(`cannot read manifest ${manifestPath}: ${e.message}`);
  }
  let manifest;
  try {
    manifest = JSON.parse(raw);
  } catch (e) {
    throw new ManifestError(`manifest is not valid JSON: ${e.message}`);
  }
  const problems = validateManifest(manifest);
  if (problems.length > 0) {
    throw new ManifestError(`invalid manifest:\n  - ${problems.join('\n  - ')}`);
  }
  return manifest;
}

/** Expand bundle-relative file paths (supports trailing '*.json' globs). */
function resolveFiles(bundleDir, patterns) {
  const out = [];
  for (const pattern of patterns) {
    if (pattern.includes('*')) {
      const dir = path.join(bundleDir, path.dirname(pattern));
      const base = path.basename(pattern);
      const regex = new RegExp('^' + base.split('*').map(escapeRegex).join('.*') + '$');
      if (fs.existsSync(dir)) {
        for (const entry of fs.readdirSync(dir).sort()) {
          if (regex.test(entry)) out.push(path.join(dir, entry));
        }
      }
    } else {
      out.push(path.join(bundleDir, pattern));
    }
  }
  return out;
}

function escapeRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

module.exports = {
  KNOWN_STEP_TYPES,
  RESERVED_STEP_TYPES,
  FILE_STEP_TYPES,
  ManifestError,
  validateManifest,
  loadManifest,
  resolveFiles,
};
