'use strict';
// Second real deployment (2026-09-10): "metadata.knowledgeBase.name is required".
const test = require('node:test');
const assert = require('node:assert');
const { normalizeFlowForService } = require('../lib/steps');

test('knowledge_base node gets name from the {KB:*} placeholder and loses unknown keys', () => {
  const raw = { flowId: 'Fallback', nodes: { n1: { type: 'knowledge_base', metadata: {
    knowledgeBase: { knowledgeBaseId: '{KB:SunnyHotelFAQ}', scopeTags: [] }, maxRetries: 2 } } } };
  const resolved = JSON.parse(JSON.stringify(raw));
  resolved.nodes.n1.metadata.knowledgeBase.knowledgeBaseId = 'kb-1234';
  const out = normalizeFlowForService(raw, resolved);
  assert.deepStrictEqual(out.nodes.n1.metadata.knowledgeBase, { knowledgeBaseId: 'kb-1234', name: 'SunnyHotelFAQ' });
  assert.strictEqual(out.nodes.n1.metadata.maxRetries, 2);
});

test('an explicit name is kept and other node types are untouched', () => {
  const raw = { nodes: { a: { type: 'knowledge_base', metadata: { knowledgeBase: { knowledgeBaseId: '{KB:X}', name: 'Given' } } },
    b: { type: 'end', metadata: {} } } };
  const out = normalizeFlowForService(raw, JSON.parse(JSON.stringify(raw)));
  assert.strictEqual(out.nodes.a.metadata.knowledgeBase.name, 'Given');
  assert.deepStrictEqual(out.nodes.b, { type: 'end', metadata: {} });
});
