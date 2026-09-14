'use strict';
// Live (GreenCart, 2026-09-14): deploy.sh exported the backend key under the
// generic ACXD_SECRET_BACKENDAPIKEY while the bundle's secret carried the
// project-scoped name; the runner skipped it ("env not set") and every data
// request answered 403.
const test = require('node:test');
const assert = require('node:assert');
const { readBackendApiKey } = require('../lib/steps');

test('the generic env value serves the project-scoped *BackendApiKey secret', () => {
  const ctx = { env: { ACXD_SECRET_BACKENDAPIKEY: 'k-123' }, log() {} };
  readBackendApiKey(ctx, 'stack');
  assert.strictEqual(ctx.backendApiKey, 'k-123');
});

test('without the env value the key is read from the CloudFormation output', () => {
  const ctx = { env: {}, log() {}, run: () => '"k-cfn"' };
  // readCfnOutput shells out; a missing CLI must not throw — the runner logs and continues
  assert.doesNotThrow(() => readBackendApiKey(ctx, 'stack'));
});

test('the WEBHOOK_URL path of wire-webhook-urls still resolves the backend key', async () => {
  const { STEPS } = require('../lib/steps');
  const ctx = { env: { WEBHOOK_URL: 'https://x.example/dev/', ACXD_SECRET_BACKENDAPIKEY: 'k-env' }, state: { cfnStackName: 'stack' }, log() {} };
  await STEPS['wire-webhook-urls'].run(ctx, {});
  assert.strictEqual(ctx.state.webhookUrl, 'https://x.example/dev');
  assert.strictEqual(ctx.backendApiKey, 'k-env');
});
