'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { bindAgenticCx } = require('../lib/steps');

test('Agentic CX block receives the deployed workspace/application ids and a visible alias placeholder', () => {
  const logs = [];
  const ctx = { state: { applicationId: 'app-1' }, env: { ACXD_WORKSPACE_ID: 'ws-1' }, log: (l) => logs.push(l) };
  const flow = { Actions: [{ Identifier: 'AgenticCXPlaceholder', Type: 'ConnectParticipantWithAgenticCX',
    Parameters: { AgentConfiguration: { WorkspaceId: '{ACXD_WORKSPACE_ID}', ApplicationId: '{ACXD_APPLICATION_ID}', Alias: '{ACXD_ALIAS_ID}' } } }] };
  const out = bindAgenticCx(flow, ctx);
  assert.deepStrictEqual(out.Actions[0].Parameters.AgentConfiguration, { WorkspaceId: 'ws-1', ApplicationId: 'app-1', Alias: 'SELECT_ALIAS_IN_CONSOLE' });
  assert.ok(logs.some((l) => /ACXD_ALIAS_ID not set/.test(l)));
  const withAlias = bindAgenticCx(flow, { ...ctx, env: { ...ctx.env, ACXD_ALIAS_ID: 'alias-9' }, log: () => {} });
  assert.strictEqual(withAlias.Actions[0].Parameters.AgentConfiguration.Alias, 'alias-9');
});
