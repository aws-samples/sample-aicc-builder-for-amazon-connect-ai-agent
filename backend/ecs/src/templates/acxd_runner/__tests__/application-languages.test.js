'use strict';
// Live (2026-09-13): CreateApplication ignored settings.language* and created the
// application as en-US; the build snapshotted en-US while every flow was ko-KR and
// the Agentic CX block failed with "NLX Chat Streaming Failed" on the first contact.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { STEPS } = require('../lib/steps');

function fakeCtx(liveAfterCreate) {
  const calls = [];
  const sdk = new Proxy({}, { get: (_t, name) => function Cmd(input) { this.name = name; this.input = input; } });
  const client = {
    send: async (cmd) => {
      calls.push([cmd.name, cmd.input]);
      if (cmd.name === 'ListApplicationsCommand') return { items: [] };
      if (cmd.name === 'CreateApplicationCommand') return { applicationId: 'app-1' };
      if (cmd.name === 'GetApplicationCommand') return { applicationId: 'app-1', name: 'App', settings: liveAfterCreate.settings };
      if (cmd.name === 'UpdateApplicationCommand') { liveAfterCreate.settings = cmd.input.settings; return {}; }
      return {};
    },
  };
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'acxd-app-'));
  fs.mkdirSync(path.join(dir, 'assets', 'acxd'), { recursive: true });
  fs.writeFileSync(path.join(dir, 'assets', 'acxd', 'application.json'), JSON.stringify({
    name: 'App', flows: [{ flowId: 'WelcomeFlow' }],
    settings: { languageCode: 'ko-KR', languageCodes: ['ko-KR'], languageSettings: [{ languageCode: 'ko-KR' }] },
  }));
  const logs = [];
  return { ctx: { sdk, client, sleep: async () => {}, bundleDir: dir, state: { resources: [] }, log: (m) => logs.push(m) }, calls, logs };
}

test('compose-application re-applies the document languages when the service created en-US', async () => {
  const live = { settings: { languageCode: 'en-US', languageCodes: ['en-US'], languageSettings: [{ languageCode: 'en-US' }], guardrails: [{ guardrailId: 'g1' }] } };
  const { ctx, calls, logs } = fakeCtx(live);
  await STEPS['compose-application'].run(ctx, { file: 'assets/acxd/application.json' });
  const update = calls.find(([n]) => n === 'UpdateApplicationCommand');
  assert.ok(update, 'UpdateApplication must be issued');
  assert.deepStrictEqual(update[1].settings.languageCodes, ['ko-KR']);
  assert.strictEqual(update[1].settings.languageCode, 'ko-KR');
  assert.deepStrictEqual(update[1].settings.guardrails, [{ guardrailId: 'g1' }], 'other live settings are kept');
  assert.ok(logs.some((l) => /re-applied/.test(l)));
  assert.deepStrictEqual(ctx.state.applicationLanguageCodes, ['ko-KR']);
});

test('compose-application leaves a correctly created application alone', async () => {
  const live = { settings: { languageCode: 'ko-KR', languageCodes: ['ko-KR'], languageSettings: [{ languageCode: 'ko-KR' }] } };
  const { ctx, calls } = fakeCtx(live);
  await STEPS['compose-application'].run(ctx, { file: 'assets/acxd/application.json' });
  assert.ok(!calls.some(([n]) => n === 'UpdateApplicationCommand'));
});
