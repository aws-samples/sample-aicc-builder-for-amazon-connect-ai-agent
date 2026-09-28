'use strict';
// ACXD only (v3.1): the bundle has no CloudFormation stack. The data requests
// call the customer's own API, whose base URL arrives as WEBHOOK_URL or, when
// unset, as the default the interview recorded (params.defaultUrl).
const test = require('node:test');
const assert = require('node:assert');
const { STEPS } = require('../lib/steps');
const { validateManifest } = require('../lib/manifest');

function ctxWith(env) {
  const lines = [];
  return { env, state: {}, log: (l) => lines.push(l), lines, exec() { throw new Error('no aws cli in this deploy'); } };
}

test('source=env takes WEBHOOK_URL and never touches CloudFormation', async () => {
  const ctx = ctxWith({ WEBHOOK_URL: 'https://api.example.com/v1/', ACXD_SECRET_BACKENDAPIKEY: 'k-env' });
  await STEPS['wire-webhook-urls'].run(ctx, { source: 'env', defaultUrl: 'https://default.example.com' });
  assert.strictEqual(ctx.state.webhookUrl, 'https://api.example.com/v1');
  assert.strictEqual(ctx.backendApiKey, 'k-env');
  assert.strictEqual(ctx.state.cfnStackName, undefined);
});

test('source=env falls back to the interview default when WEBHOOK_URL is unset', async () => {
  const ctx = ctxWith({});
  await STEPS['wire-webhook-urls'].run(ctx, { source: 'env', defaultUrl: 'https://default.example.com/' });
  assert.strictEqual(ctx.state.webhookUrl, 'https://default.example.com');
  assert.ok(ctx.lines.some((l) => /manifest default/.test(l)));
});

test('source=env without any URL says what to export, not which CFN step is missing', async () => {
  const ctx = ctxWith({});
  await assert.rejects(() => STEPS['wire-webhook-urls'].run(ctx, { source: 'env' }), (err) => {
    assert.match(err.message, /WEBHOOK_URL is not set/);
    assert.doesNotMatch(err.message, /deploy-cfn-backend/);
    return true;
  });
});

test('source=env refuses a plain-http backend', async () => {
  const ctx = ctxWith({ WEBHOOK_URL: 'http://api.example.com' });
  await assert.rejects(() => STEPS['wire-webhook-urls'].run(ctx, { source: 'env' }), /https/);
});

test('the dry-run plan names WEBHOOK_URL for an ACXD-only bundle', () => {
  const lines = STEPS['wire-webhook-urls'].plan({}, { source: 'env', defaultUrl: 'https://d.example.com' });
  assert.match(lines[0], /WEBHOOK_URL/);
  assert.match(lines[0], /https:\/\/d\.example\.com/);
});

test('an ACXD-only manifest (no CloudFormation, no Contact Flow import) is valid', () => {
  const manifest = {
    manifestVersion: '1.0',
    project: 'selc-voice',
    sdk: { package: 'amazon-connect-acxd-sdk', version: '0.1.0' },
    steps: [
      { type: 'wire-webhook-urls', params: { source: 'env' } },
      { type: 'upsert-secrets', params: { files: ['assets/acxd/secrets/*.json'] } },
      { type: 'upsert-data-requests', params: { files: ['assets/acxd/data-requests/*.json'] } },
      { type: 'upsert-flows', params: { files: ['assets/acxd/flows/*.json'] } },
      { type: 'compose-application', params: { file: 'assets/acxd/application.json' } },
      { type: 'build-application', params: { version: '1.0' } },
      { type: 'deploy-application', params: { environment: 'development' } },
    ],
  };
  assert.deepStrictEqual(validateManifest(manifest), []);
});
