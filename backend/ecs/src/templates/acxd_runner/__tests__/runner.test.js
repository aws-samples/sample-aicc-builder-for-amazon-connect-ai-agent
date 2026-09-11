'use strict';
/**
 * Runner unit tests (node:test — zero extra dependencies, Node 20+).
 * Run: node --test __tests__/   (from templates/acxd_runner/)
 *
 * The ACXD SDK is faked: steps receive command constructors via ctx.sdk,
 * so a Proxy that manufactures {__type, input} command objects plus a
 * FakeClient with programmable per-command handlers covers every path.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { validateManifest, resolveFiles, ManifestError, loadManifest } = require('../lib/manifest');
const { maskSecret, sendWithRetry, upsert, poll } = require('../lib/client');
const { loadState, saveState, recordResource, resolvePlaceholders,
        UnresolvedPlaceholderError } = require('../lib/state');
const { STEPS } = require('../lib/steps');
const runner = require('../runner');

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'acxd-bundle-'));
  for (const [rel, content] of Object.entries(files)) {
    const p = path.join(dir, rel);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, typeof content === 'string' ? content : JSON.stringify(content));
  }
  return dir;
}

function makeCtx(bundleDir, { client, handlers, env = {}, execLog } = {}) {
  return {
    bundleDir,
    project: 'test-proj',
    region: 'us-east-1',
    log: () => {},
    client: client || new FakeClient(handlers),
    sdk: fakeSdk,
    state: loadState(bundleDir),
    exec: (cmd, args) => { (execLog || []).push([cmd, ...args]); return execLog && execLog.out || ''; },
    sleep: async () => {},
    env,
  };
}

const MANIFEST = {
  manifestVersion: '1.0',
  project: 'acme-refunds',
  sdk: { package: 'amazon-connect-acxd-sdk', version: '0.1.0' },
  steps: [
    { type: 'upsert-flows', params: { files: ['assets/acxd/flows/*.json'] } },
    { type: 'compose-application', params: { file: 'assets/acxd/application.json' } },
    { type: 'build-application' },
    { type: 'deploy-application', params: { environment: 'development' } },
  ],
};

// ---------------------------------------------------------------------------
// manifest
// ---------------------------------------------------------------------------

test('validateManifest accepts a well-formed manifest', () => {
  assert.deepEqual(validateManifest(MANIFEST), []);
});

test('validateManifest rejects bad version/project/steps', () => {
  assert.ok(validateManifest({ ...MANIFEST, manifestVersion: '2.0' }).length);
  assert.ok(validateManifest({ ...MANIFEST, project: 'Bad_Name' }).length);
  assert.ok(validateManifest({ ...MANIFEST, steps: [] }).length);
  assert.ok(validateManifest({ ...MANIFEST, steps: [{ type: 'teleport' }] })
    .some((p) => p.includes('unknown')));
});

test('validateManifest rejects reserved test-asset steps (D4)', () => {
  const problems = validateManifest({
    ...MANIFEST, steps: [...MANIFEST.steps, { type: 'run-simulation' }],
  });
  assert.ok(problems.some((p) => p.includes('reserved')));
});

test('validateManifest requires files/file params where applicable', () => {
  assert.ok(validateManifest({ ...MANIFEST, steps: [{ type: 'upsert-flows' }] })
    .some((p) => p.includes('params.files')));
  assert.ok(validateManifest({ ...MANIFEST, steps: [{ type: 'compose-application' }] })
    .some((p) => p.includes('params.file')));
  assert.ok(validateManifest({ ...MANIFEST, steps: [{ type: 'deploy-cfn-backend' }] })
    .some((p) => p.includes('templatePath')));
});

test('loadManifest throws ManifestError on invalid JSON', () => {
  const dir = tmpBundle({ 'deploy-manifest.json': '{not json' });
  assert.throws(() => loadManifest(path.join(dir, 'deploy-manifest.json')), ManifestError);
});

test('resolveFiles expands globs deterministically', () => {
  const dir = tmpBundle({
    'assets/flows/b.json': {}, 'assets/flows/a.json': {}, 'assets/other.txt': 'x',
  });
  const out = resolveFiles(dir, ['assets/flows/*.json']);
  assert.deepEqual(out.map((p) => path.basename(p)), ['a.json', 'b.json']);
  assert.deepEqual(resolveFiles(dir, ['assets/other.txt']).length, 1);
});

// ---------------------------------------------------------------------------
// client helpers
// ---------------------------------------------------------------------------

test('maskSecret hides the API key secret part', () => {
  const masked = maskSecret('key acxd_live_abc123.SuperSecretPart rest');
  assert.ok(!masked.includes('SuperSecretPart'));
  assert.ok(masked.includes('acxd_live_abc123***') || masked.includes('***'));
});

test('sendWithRetry retries throttling then succeeds', async () => {
  let calls = 0;
  const delays = [];
  const client = { send: async () => {
    calls += 1;
    if (calls < 3) throw err('ThrottlingException');
    return { ok: true };
  }};
  const result = await sendWithRetry(client, {}, { sleep: async (ms) => delays.push(ms) });
  assert.equal(result.ok, true);
  assert.deepEqual(delays, [1000, 2000]);  // exponential backoff
});

test('sendWithRetry does not retry non-retryable errors', async () => {
  let calls = 0;
  const client = { send: async () => { calls += 1; throw err('ValidationException'); } };
  await assert.rejects(() => sendWithRetry(client, {}, { sleep: async () => {} }),
    /ValidationException/);
  assert.equal(calls, 1);
});

test('upsert: not-found → create; exists → update; conflict → update', async () => {
  const log = () => {};
  // not found → create
  let r = await upsert({
    label: 'x', log,
    get: async () => { throw err('ResourceNotFoundException'); },
    create: async () => 'created!',
    update: async () => 'updated!',
  });
  assert.equal(r.action, 'created');
  // exists → update
  r = await upsert({ label: 'x', log,
    get: async () => ({}), create: async () => 'c', update: async () => 'u' });
  assert.equal(r.action, 'updated');
  // create conflicts → update fallback
  r = await upsert({ label: 'x', log,
    get: async () => { throw err('ResourceNotFoundException'); },
    create: async () => { throw err('ConflictException'); },
    update: async () => 'u' });
  assert.equal(r.action, 'updated');
});

test('poll times out with a clear error', async () => {
  await assert.rejects(
    () => poll(async () => null, { intervalMs: 1, timeoutMs: 5, sleep: async () => {} }),
    /timed out/);
});

// ---------------------------------------------------------------------------
// state + placeholders
// ---------------------------------------------------------------------------

test('resolvePlaceholders substitutes WEBHOOK_URL / KB / GUARDRAIL deep in documents', () => {
  const state = {
    webhookUrl: 'https://api.example.com/dev',
    knowledgeBases: { 'Product FAQ': { knowledgeBaseId: 'kb-123' } },
    guardrails: { 'PII Filter': { guardrailId: 'g-456' } },
  };
  const doc = {
    webhook: { url: '{WEBHOOK_URL}/tools/get_order' },
    nested: [{ knowledgeBaseId: '{KB:Product FAQ}' }],
    settings: { guardrails: [{ guardrailId: '{GUARDRAIL:PII Filter}' }] },
    untouched: 'kb-raw-id',
  };
  const out = resolvePlaceholders(doc, state);
  assert.equal(out.webhook.url, 'https://api.example.com/dev/tools/get_order');
  assert.equal(out.nested[0].knowledgeBaseId, 'kb-123');
  assert.equal(out.settings.guardrails[0].guardrailId, 'g-456');
  assert.equal(out.untouched, 'kb-raw-id');
});

test('resolvePlaceholders throws on unresolved placeholders', () => {
  assert.throws(() => resolvePlaceholders({ u: '{WEBHOOK_URL}/x' },
    { knowledgeBases: {}, guardrails: {} }), UnresolvedPlaceholderError);
  assert.throws(() => resolvePlaceholders('{KB:Nope}',
    { knowledgeBases: {}, guardrails: {} }), UnresolvedPlaceholderError);
});

test('state round-trip + recordResource dedup', () => {
  const dir = tmpBundle({});
  const state = loadState(dir);
  recordResource(state, 'flow', 'MainFlow');
  recordResource(state, 'flow', 'MainFlow', { note: 'again' });
  saveState(dir, state);
  const back = loadState(dir);
  assert.equal(back.resources.length, 1);
  assert.equal(back.resources[0].note, 'again');
});

// ---------------------------------------------------------------------------
// steps
// ---------------------------------------------------------------------------

test('upsert-flows: creates when missing, resolves KB placeholders', async () => {
  const dir = tmpBundle({
    'assets/flows/main.json': {
      flowId: 'MainFlow',
      nodes: { n1: { nodeId: 'n1', type: 'knowledge_base',
                     metadata: { knowledgeBase: { knowledgeBaseId: '{KB:FAQ}' } } } },
    },
  });
  const ctx = makeCtx(dir, { handlers: {
    GetFlowCommand: err('ResourceNotFoundException'),
    CreateFlowCommand: {},
  }});
  ctx.state.knowledgeBases.FAQ = { knowledgeBaseId: 'kb-999' };
  await STEPS['upsert-flows'].run(ctx, { files: ['assets/flows/*.json'] });
  const created = ctx.client.calls('CreateFlowCommand');
  assert.equal(created.length, 1);
  assert.equal(created[0].input.nodes.n1.metadata.knowledgeBase.knowledgeBaseId, 'kb-999');
  assert.ok(ctx.state.resources.some((r) => r.kind === 'flow' && r.id === 'MainFlow'));
});

test('upsert-flows: updates when it already exists', async () => {
  const dir = tmpBundle({ 'f.json': { flowId: 'MainFlow', nodes: {} } });
  const ctx = makeCtx(dir, { handlers: { GetFlowCommand: {}, UpdateFlowCommand: {} } });
  await STEPS['upsert-flows'].run(ctx, { files: ['f.json'] });
  assert.equal(ctx.client.calls('UpdateFlowCommand').length, 1);
  assert.equal(ctx.client.calls('CreateFlowCommand').length, 0);
  // update must use flowIdentifier, not flowId
  assert.equal(ctx.client.calls('UpdateFlowCommand')[0].input.flowIdentifier, 'MainFlow');
  assert.ok(!('flowId' in ctx.client.calls('UpdateFlowCommand')[0].input));
});

test('upsert-data-requests resolves {WEBHOOK_URL} from state', async () => {
  const dir = tmpBundle({ 'dr.json': {
    dataRequestId: 'getOrder', type: 'object',
    webhook: { implementation: 'external', url: '{WEBHOOK_URL}/tools/get_order' },
  }});
  const ctx = makeCtx(dir, { handlers: {
    GetDataRequestCommand: err('ResourceNotFoundException'),
    CreateDataRequestCommand: {},
  }});
  ctx.state.webhookUrl = 'https://abc.execute-api.us-east-1.amazonaws.com/dev';
  await STEPS['upsert-data-requests'].run(ctx, { files: ['dr.json'] });
  assert.equal(ctx.client.calls('CreateDataRequestCommand')[0].input.webhook.url,
    'https://abc.execute-api.us-east-1.amazonaws.com/dev/tools/get_order');
});

test('upsert-knowledge-bases: create → articles → publish (poll to published)', async () => {
  const dir = tmpBundle({ 'kb.json': {
    name: 'FAQ', type: 'articles',
    articles: [
      { question: { text: 'Q1?' }, responses: [{ type: 'text', body: 'A1' }] },
      { question: { text: 'Q2?' }, responses: [{ type: 'text', body: 'A2' }] },
    ],
  }});
  let pubChecks = 0;
  const ctx = makeCtx(dir, { handlers: {
    ListKnowledgeBasesCommand: { items: [] },
    CreateKnowledgeBaseCommand: { knowledgeBaseId: 'kb-1' },
    ListKnowledgeBaseArticlesCommand: { items: [
      { articleId: 'a-1', question: { text: 'Q1?' } },  // Q1 exists → update
    ]},
    UpdateKnowledgeBaseArticleCommand: {},
    CreateKnowledgeBaseArticleCommand: {},
    PublishKnowledgeBaseCommand: { deploymentId: 'dep-1', status: 'scheduled' },
    GetKnowledgeBasePublicationCommand: () => {
      pubChecks += 1;
      return { deploymentId: 'dep-1', status: pubChecks < 2 ? 'scheduled' : 'published' };
    },
  }});
  await STEPS['upsert-knowledge-bases'].run(ctx, { files: ['kb.json'] });
  assert.equal(ctx.client.calls('UpdateKnowledgeBaseArticleCommand').length, 1);
  assert.equal(ctx.client.calls('CreateKnowledgeBaseArticleCommand').length, 1);
  assert.equal(ctx.state.knowledgeBases.FAQ.knowledgeBaseId, 'kb-1');
  assert.ok(pubChecks >= 2);
});

test('upsert-guardrails: smoke test failure aborts', async () => {
  const dir = tmpBundle({ 'g.json': {
    name: 'PII', trigger: 'output',
    rules: [{ name: 'r', detection: { method: 'keyword', keywords: ['ssn'] },
              enforcement: { action: 'mask' } }],
    smokeTests: [{ input: 'my ssn is 123', expectTriggered: true }],
  }});
  const ctx = makeCtx(dir, { handlers: {
    ListGuardrailsCommand: { items: [] },
    CreateGuardrailCommand: { guardrailId: 'g-1' },
    TestGuardrailCommand: { violations: [] },  // does NOT trigger → smoke fails
  }});
  await assert.rejects(
    () => STEPS['upsert-guardrails'].run(ctx, { files: ['g.json'] }),
    /smoke test failed/);
});

test('compose-application resolves placeholders and records appId', async () => {
  const dir = tmpBundle({ 'app.json': {
    name: 'RefundBot',
    settings: { guardrails: [{ guardrailId: '{GUARDRAIL:PII}' }] },
    flows: [{ flowId: 'MainFlow' }],
  }});
  const ctx = makeCtx(dir, { handlers: {
    ListApplicationsCommand: { items: [] },
    CreateApplicationCommand: { applicationId: 'app-1' },
  }});
  ctx.state.guardrails.PII = { guardrailId: 'g-1' };
  await STEPS['compose-application'].run(ctx, { file: 'app.json' });
  assert.equal(ctx.state.applicationId, 'app-1');
  const input = ctx.client.calls('CreateApplicationCommand')[0].input;
  assert.equal(input.settings.guardrails[0].guardrailId, 'g-1');
});

test('build-application polls to BUILT; FAILED throws with details', async () => {
  const dir = tmpBundle({});
  let checks = 0;
  const okCtx = makeCtx(dir, { handlers: {
    CreateApplicationBuildCommand: { buildId: 'b-1', status: 'PENDING' },
    GetApplicationBuildCommand: () => {
      checks += 1;
      return { buildId: 'b-1', status: checks < 2 ? 'PENDING' : 'BUILT' };
    },
  }});
  okCtx.state.applicationId = 'app-1';
  await STEPS['build-application'].run(okCtx, {});
  assert.equal(okCtx.state.buildId, 'b-1');

  const failCtx = makeCtx(dir, { handlers: {
    CreateApplicationBuildCommand: { buildId: 'b-2' },
    GetApplicationBuildCommand: { buildId: 'b-2', status: 'FAILED',
                                  description: 'flow X references missing slot' },
  }});
  failCtx.state.applicationId = 'app-1';
  await assert.rejects(() => STEPS['build-application'].run(failCtx, {}),
    /FAILED[\s\S]*missing slot/);
});

test('deploy-application requires build first, then polls to deployed', async () => {
  const dir = tmpBundle({});
  const ctx = makeCtx(dir, { handlers: {
    CreateApplicationDeploymentCommand: { deploymentId: 'd-1', deploymentStatus: 'pending' },
    GetApplicationDeploymentCommand: { deploymentId: 'd-1', deploymentStatus: 'deployed' },
  }});
  await assert.rejects(() => STEPS['deploy-application'].run(ctx, {}), /requires/);
  ctx.state.applicationId = 'app-1';
  ctx.state.buildId = 'b-1';
  await STEPS['deploy-application'].run(ctx, { environment: 'development' });
  assert.equal(ctx.state.deploymentId, 'd-1');
});

test('deploy-cfn-backend + wire-webhook-urls via aws cli', async () => {
  const dir = tmpBundle({ 'cloudformation/infrastructure.yaml': 'Resources: {}' });
  const execLog = [];
  execLog.out = 'https://abc.execute-api.us-east-1.amazonaws.com/dev\n';
  const ctx = makeCtx(dir, { execLog });
  await STEPS['deploy-cfn-backend'].run(ctx,
    { templatePath: 'cloudformation/infrastructure.yaml' });
  assert.equal(ctx.state.cfnStackName, 'test-proj-acxd-backend');
  assert.ok(execLog.some((c) => c.includes('cloudformation') && c.includes('deploy')));

  await STEPS['wire-webhook-urls'].run(ctx, {});
  assert.equal(ctx.state.webhookUrl, 'https://abc.execute-api.us-east-1.amazonaws.com/dev');
});

test('wire-webhook-urls fails without prior CFN deploy', async () => {
  const ctx = makeCtx(tmpBundle({}), {});
  await assert.rejects(() => STEPS['wire-webhook-urls'].run(ctx, {}), /deploy-cfn-backend/);
});

test('import-contact-flows skips gracefully without CONNECT_INSTANCE_ID', async () => {
  const dir = tmpBundle({ 'cf.json': { name: 'Inbound', content: {} } });
  const lines = [];
  const ctx = makeCtx(dir, {});
  ctx.log = (l) => lines.push(l);
  await STEPS['import-contact-flows'].run(ctx, { files: ['cf.json'] });
  assert.ok(lines.some((l) => l.includes('CONNECT_INSTANCE_ID')));
});

test('every manifest step type (non-reserved) has an implementation with plan+run', () => {
  const { KNOWN_STEP_TYPES, RESERVED_STEP_TYPES } = require('../lib/manifest');
  for (const type of KNOWN_STEP_TYPES) {
    if (RESERVED_STEP_TYPES.includes(type)) continue;
    assert.ok(STEPS[type], `missing step implementation: ${type}`);
    assert.equal(typeof STEPS[type].plan, 'function');
    assert.equal(typeof STEPS[type].run, 'function');
  }
});

// ---------------------------------------------------------------------------
// runner CLI
// ---------------------------------------------------------------------------

test('dry-run prints the full plan without touching the SDK', async () => {
  const dir = tmpBundle({
    'deploy-manifest.json': MANIFEST,
    'assets/acxd/flows/main.json': { flowId: 'MainFlow', nodes: {} },
    'assets/acxd/application.json': { name: 'RefundBot', settings: {} },
  });
  const lines = [];
  const origLog = console.log;
  console.log = (l) => lines.push(String(l));
  const cwd = process.cwd();
  try {
    process.chdir(dir);
    const code = await runner.cmdDeploy({ dryRun: true, manifest: 'deploy-manifest.json' }, dir);
    assert.equal(code, 0);
  } finally {
    console.log = origLog;
    process.chdir(cwd);
  }
  const text = lines.join('\n');
  assert.ok(text.includes('DRY RUN'));
  assert.ok(text.includes('upsert-flows'));
  assert.ok(text.includes('compose-application'));
  assert.ok(text.includes('deploy build to environment'));
});

test('status reports recorded resources', async () => {
  const dir = tmpBundle({});
  const state = loadState(dir);
  recordResource(state, 'application', 'app-1', { name: 'RefundBot' });
  saveState(dir, state);
  const lines = [];
  const origLog = console.log;
  console.log = (l) => lines.push(String(l));
  try {
    const code = runner.cmdStatus(dir);
    assert.equal(code, 0);
  } finally {
    console.log = origLog;
  }
  assert.ok(lines.join('\n').includes('app-1'));
});


test('upsert-data-requests resolves WEBHOOK_URL from env before runner state exists', async () => {
  const dir = tmpBundle({ 'dr.json': {
    dataRequestId: 'getOrder', type: 'object',
    webhook: { implementation: 'external', url: '{WEBHOOK_URL}/tools/get_order' },
  }});
  const ctx = makeCtx(dir, { env: { WEBHOOK_URL: 'https://env.example.com/base' }, handlers: {
    GetDataRequestCommand: err('ResourceNotFoundException'),
    CreateDataRequestCommand: {},
  }});
  await STEPS['upsert-data-requests'].run(ctx, { files: ['dr.json'] });
  assert.equal(ctx.client.calls('CreateDataRequestCommand')[0].input.webhook.url,
    'https://env.example.com/base/tools/get_order');
});

test('import-contact-flows updates a same-named Connect flow instead of creating another', async () => {
  const dir = tmpBundle({ 'contact-flow/contact_flow.json': {
    name: 'Inbound', type: 'CONTACT_FLOW', content: { Version: '2019-10-30', Actions: [] },
  }});
  const ctx = makeCtx(dir, { env: { CONNECT_INSTANCE_ID: 'instance-1' } });
  const calls = [];
  ctx.exec = (command, args) => {
    calls.push([command, ...args]);
    if (args.includes('list-contact-flows')) {
      return JSON.stringify({ ContactFlowSummaryList: [{ Name: 'Inbound', Id: 'flow-123' }] });
    }
    return '{}';
  };
  await STEPS['import-contact-flows'].run(ctx, { files: ['contact-flow/*.json'] });
  assert.ok(calls.some((call) => call.includes('update-contact-flow-content')));
  assert.ok(!calls.some((call) => call.includes('create-contact-flow')));
  assert.ok(ctx.state.resources.some((resource) =>
    resource.kind === 'contact-flow' && resource.id === 'flow-123'));
});


test('deploy-cfn-backend reuses a shared CloudFormation output when deploy.sh marks it ready', async () => {
  const ctx = makeCtx(tmpBundle({}), {
    env: {
      AICC_CFN_ALREADY_DEPLOYED: '1',
      WEBHOOK_URL: 'https://api.example.com/prod',
    },
  });
  const calls = [];
  ctx.exec = (command, args) => { calls.push([command, ...args]); return ''; };
  await STEPS['deploy-cfn-backend'].run(ctx, {
    templatePath: 'cloudformation/infrastructure.yaml', stackName: 'test-proj-stack',
  });
  assert.equal(ctx.state.cfnStackName, 'test-proj-stack');
  assert.equal(calls.length, 0);
});


test('upsert-secrets fills BackendApiKey from the CFN ApiKeyValue output when no env var is set', async () => {
  const dir = tmpBundle({ 'secret.json': {
    name: 'BackendApiKey', description: 'api key', valueEnv: 'ACXD_SECRET_BACKENDAPIKEY',
  }});
  const ctx = makeCtx(dir, { handlers: {
    ListSecretsCommand: { secrets: [] },
    CreateSecretCommand: {},
  }});
  ctx.state.cfnStackName = 'aicc-poc-stack';
  ctx.exec = (cmd, args) => {
    const q = args.join(' ');
    if (q.includes("OutputKey=='ApiEndpoint'")) return 'https://api.example.com/prod\n';
    if (q.includes("OutputKey=='ApiKeyValue'")) return 'k3y-from-cfn\n';
    return '';
  };
  await STEPS['wire-webhook-urls'].run(ctx, {});
  await STEPS['upsert-secrets'].run(ctx, { files: ['secret.json'] });
  const created = ctx.client.calls('CreateSecretCommand');
  assert.equal(created.length, 1);
  assert.equal(created[0].input.name, 'BackendApiKey');
  assert.equal(created[0].input.value, 'k3y-from-cfn');
  // the value lives in memory only, never in the persisted state
  assert.equal(JSON.stringify(ctx.state).includes('k3y-from-cfn'), false);
});
