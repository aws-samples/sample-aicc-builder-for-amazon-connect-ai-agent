'use strict';
/**
 * Contract facts established live in a Connect Customer account (2026-09-13):
 *
 *  - a Data Request's secret is resolved only from
 *    webhook.environments.{production,development}, so {WEBHOOK_URL} must be
 *    substituted THERE too, not only at the top level (D2)
 *  - the secret reference {Name:NLX.Secret} must survive placeholder resolution
 *    untouched (D1)
 *  - deploy.sh and a runner-only deploy must converge on ONE CloudFormation
 *    stack (P1)
 *  - replacing an application deployment rotates the deploymentKey the Agentic
 *    CX block stores as its Alias, so the state file must say so (A1)
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { loadState } = require('../lib/state');
const { STEPS, resolveStackName, resolveAssetPlaceholders } = require('../lib/steps');

const fakeSdk = new Proxy({}, {
  get(_t, name) {
    if (typeof name !== 'string') return undefined;
    return class {
      constructor(input) { this.__type = name; this.input = input; }
    };
  },
});

class FakeClient {
  constructor(handlers = {}) { this.handlers = handlers; this.sent = []; }
  async send(cmd) {
    this.sent.push(cmd);
    const handler = this.handlers[cmd.__type];
    if (typeof handler === 'function') return handler(cmd.input, this);
    if (handler instanceof Error) throw handler;
    return handler || {};
  }
  calls(type) { return this.sent.filter((c) => c.__type === type); }
}

function err(name) { return Object.assign(new Error(name), { name }); }

function tmpBundle(files = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'acxd-contract-'));
  for (const [rel, content] of Object.entries(files)) {
    const p = path.join(dir, rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, typeof content === 'string' ? content : JSON.stringify(content));
  }
  return dir;
}

function makeCtx(bundleDir, { handlers, env = {}, log } = {}) {
  const lines = [];
  return {
    bundleDir,
    project: 'test-proj',
    region: 'ap-northeast-2',
    lines,
    log: log || ((l) => lines.push(l)),
    client: new FakeClient(handlers),
    sdk: fakeSdk,
    state: loadState(bundleDir),
    exec: () => '',
    sleep: async () => {},
    env,
  };
}

const SECRET_HEADER = { key: 'x-api-key', value: '{BackendApiKey:NLX.Secret}', sensitive: true };

function dataRequestDoc() {
  const url = '{WEBHOOK_URL}/tools/get_cleaning_price';
  return {
    dataRequestId: 'getCleaningPrice',
    type: 'object',
    webhook: {
      implementation: 'external',
      method: 'POST',
      url,
      headers: [SECRET_HEADER],
      sendContext: true,
      environments: {
        production: { url, headers: [SECRET_HEADER] },
        development: { url, headers: [SECRET_HEADER] },
      },
    },
  };
}

// ---------------------------------------------------------------------------
// D2 — {WEBHOOK_URL} inside the environment blocks
// ---------------------------------------------------------------------------

test('upsert-data-requests resolves {WEBHOOK_URL} inside webhook.environments.*', async () => {
  const dir = tmpBundle({ 'dr.json': dataRequestDoc() });
  const ctx = makeCtx(dir, { handlers: {
    GetDataRequestCommand: err('ResourceNotFoundException'),
    CreateDataRequestCommand: {},
  }});
  ctx.state.webhookUrl = 'https://abc.execute-api.ap-northeast-2.amazonaws.com/dev';
  await STEPS['upsert-data-requests'].run(ctx, { files: ['dr.json'] });

  const sent = ctx.client.calls('CreateDataRequestCommand')[0].input.webhook;
  const expected = 'https://abc.execute-api.ap-northeast-2.amazonaws.com/dev/tools/get_cleaning_price';
  assert.equal(sent.url, expected);
  assert.equal(sent.environments.production.url, expected);
  assert.equal(sent.environments.development.url, expected);
  assert.equal(JSON.stringify(sent).includes('{WEBHOOK_URL}'), false);
});

test('the {Name:NLX.Secret} header survives placeholder resolution in every position', () => {
  const ctx = makeCtx(tmpBundle({}), { env: { WEBHOOK_URL: 'https://api.example.com/prod' } });
  const resolved = resolveAssetPlaceholders(dataRequestDoc(), ctx);
  for (const headers of [resolved.webhook.headers,
                         resolved.webhook.environments.production.headers,
                         resolved.webhook.environments.development.headers]) {
    assert.deepEqual(headers, [{ key: 'x-api-key', value: '{BackendApiKey:NLX.Secret}', sensitive: true }]);
  }
  // dynamic:true would make the runtime expect a per-request value → never set
  assert.equal(JSON.stringify(resolved).includes('dynamic'), false);
});

// ---------------------------------------------------------------------------
// P1 — one stack for deploy.sh and for a runner-only deploy
// ---------------------------------------------------------------------------

test('resolveStackName: AICC_STACK_NAME > PROJECT_NAME > manifest > legacy default', () => {
  const base = { project: 'manifest-proj' };
  assert.equal(
    resolveStackName({ ...base, env: { AICC_STACK_NAME: 'gaon-stack', PROJECT_NAME: 'other' } },
      { stackName: 'aicc-poc-stack' }),
    'gaon-stack');
  assert.equal(
    resolveStackName({ ...base, env: { PROJECT_NAME: 'gaon' } }, { stackName: 'aicc-poc-stack' }),
    'gaon-stack');
  assert.equal(
    resolveStackName({ ...base, env: {} }, { stackName: 'aicc-poc-stack' }),
    'aicc-poc-stack');
  assert.equal(resolveStackName({ ...base, env: {} }, {}), 'manifest-proj-acxd-backend');
});

test('deploy-cfn-backend uses the stack deploy.sh exported, and says when it differs', async () => {
  const dir = tmpBundle({ 'cloudformation/infrastructure.yaml': 'Resources: {}' });
  const ctx = makeCtx(dir, { env: {
    PROJECT_NAME: 'gaon',
    AICC_STACK_NAME: 'gaon-stack',
    AICC_CFN_ALREADY_DEPLOYED: '1',
    WEBHOOK_URL: 'https://api.example.com/dev',
  }});
  await STEPS['deploy-cfn-backend'].run(ctx, {
    templatePath: 'cloudformation/infrastructure.yaml', stackName: 'aicc-poc-stack',
  });
  assert.equal(ctx.state.cfnStackName, 'gaon-stack');
  const text = ctx.lines.join('\n');
  assert.match(text, /CloudFormation stack: gaon-stack/);
  assert.match(text, /manifest names 'aicc-poc-stack'/);
});

test('the dry-run plan names the stack it would use', () => {
  const ctx = makeCtx(tmpBundle({}), { env: { PROJECT_NAME: 'gaon' } });
  const lines = STEPS['deploy-cfn-backend'].plan(ctx, {
    templatePath: 'cloudformation/infrastructure.yaml', stackName: 'aicc-poc-stack',
  });
  assert.match(lines.join('\n'), /stack 'gaon-stack'/);
});

// ---------------------------------------------------------------------------
// A1 — deployment replacement rotates the alias
// ---------------------------------------------------------------------------

function deployCtx(dir, { updateFails }) {
  return makeCtx(dir, { handlers: {
    ListApplicationDeploymentsCommand: { items: [{ deploymentId: 'old-dep', environment: 'development' }] },
    UpdateApplicationDeploymentCommand: updateFails
      ? err('InternalServerException')
      : {},
    DeleteApplicationDeploymentCommand: {},
    CreateApplicationDeploymentCommand: { deploymentId: 'new-dep' },
    GetApplicationDeploymentCommand: { deploymentId: 'new-dep', deploymentStatus: 'deployed' },
  }});
}

test('a REPLACED deployment records aliasRotated with the old and new ids, and says how to rebind', async () => {
  const dir = tmpBundle({});
  const ctx = deployCtx(dir, { updateFails: true });
  ctx.state.applicationId = 'app-1';
  ctx.state.buildId = 'b-1';
  ctx.state.applicationLanguageCodes = ['ko-KR'];

  await STEPS['deploy-application'].run(ctx, { environment: 'development' });

  assert.equal(ctx.state.aliasRotated, true);
  assert.equal(ctx.state.aliasRotation.previousDeploymentId, 'old-dep');
  assert.equal(ctx.state.aliasRotation.deploymentId, 'new-dep');
  assert.equal(ctx.state.aliasRotation.environment, 'development');
  assert.equal(ctx.state.deploymentId, 'new-dep');

  const text = ctx.lines.join('\n');
  assert.match(text, /ALIAS ROTATED/);
  assert.match(text, /Alias dropdown/);
  assert.match(text, /Save -> Publish/);
  assert.match(text, /--rebind-alias/);
  assert.match(text, /ACXD_ALIAS_ID=<deploymentKey>/);
  assert.match(text, /type=deployments/);   // where the key can be read
});

test('an in-place promotion keeps the key and clears a flag left by an earlier run', async () => {
  const dir = tmpBundle({});
  const ctx = deployCtx(dir, { updateFails: false });
  ctx.state.applicationId = 'app-1';
  ctx.state.buildId = 'b-1';
  ctx.state.aliasRotated = true;                     // stale flag from a previous deploy
  ctx.state.aliasRotation = { previousDeploymentId: 'x', deploymentId: 'y' };
  ctx.client.handlers.GetApplicationDeploymentCommand =
    { deploymentId: 'old-dep', deploymentStatus: 'deployed' };

  await STEPS['deploy-application'].run(ctx, { environment: 'development' });

  assert.equal(ctx.state.aliasRotated, false);
  assert.equal('aliasRotation' in ctx.state, false);
  assert.equal(ctx.state.deploymentId, 'old-dep');
  assert.equal(ctx.lines.join('\n').includes('ALIAS ROTATED'), false);
});

test('a supplied ACXD_ALIAS_ID is flagged as the OLD key once the deployment was replaced', () => {
  const { bindAgenticCx } = require('../lib/steps');
  const lines = [];
  const ctx = {
    state: { applicationId: 'app-1', aliasRotated: true },
    env: { ACXD_WORKSPACE_ID: 'ws-1', ACXD_ALIAS_ID: 'oldKey123' },
    log: (l) => lines.push(l),
  };
  const flow = { Actions: [{ Type: 'ConnectParticipantWithAgenticCX', Parameters: { AgentConfiguration: {
    WorkspaceId: '{ACXD_WORKSPACE_ID}', ApplicationId: '{ACXD_APPLICATION_ID}', Alias: '{ACXD_ALIAS_ID}' } } }] };
  bindAgenticCx(flow, ctx);
  assert.match(lines.join('\n'), /OLD deployment key/);
});
