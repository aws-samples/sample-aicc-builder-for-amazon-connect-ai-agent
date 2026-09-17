import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { transform } from 'esbuild';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

async function loadTypeScriptModule(relativePath, rewrite = (source) => source) {
  const source = rewrite(await readFile(path.join(ROOT, relativePath), 'utf8'));
  const { code } = await transform(source, { loader: 'ts', format: 'esm', target: 'es2020' });
  const encoded = Buffer.from(code).toString('base64');
  return import(`data:text/javascript;base64,${encoded}`);
}

const runtimeTarget = await loadTypeScriptModule('src/lib/runtimeTarget.ts');
const assetTabs = await loadTypeScriptModule('src/lib/assetTabs.ts', (source) =>
  source.replace(
    /import\s*\{[\s\S]*?type LucideIcon,\s*\}\s*from 'lucide-react';/,
    `const Workflow = null;
const MessageSquare = null;
const BookOpen = null;
const FileCode = null;
const FileJson = null;
const Boxes = null;
const ShieldCheck = null;
type LucideIcon = unknown;`,
  ),
);

test('runtime target selector supports radiogroup keyboard navigation', () => {
  assert.equal(runtimeTarget.runtimeTargetForKey('classic', 'ArrowRight'), 'acxd');
  assert.equal(runtimeTarget.runtimeTargetForKey('acxd', 'ArrowRight'), 'classic');
  assert.equal(runtimeTarget.runtimeTargetForKey('classic', 'ArrowLeft'), 'acxd');
  assert.equal(runtimeTarget.runtimeTargetForKey('acxd', 'Home'), 'classic');
  assert.equal(runtimeTarget.runtimeTargetForKey('classic', 'End'), 'acxd');
  assert.equal(runtimeTarget.runtimeTargetForKey('classic', 'Enter'), null);
});

test('tabIdFor maps ACXD asset types to the intended grouped tabs', () => {
  const expected = {
    acxd_flow: 'acxd_flows',
    acxd_slot_type: 'acxd_slot_types',
    acxd_data_request: 'acxd_data_requests',
    acxd_guardrail: 'acxd_guardrails',
    acxd_knowledge_base: 'acxd_knowledge_base',
    acxd_application: 'acxd_application',
    acxd_context_variable: 'acxd_application',
  };

  for (const [assetType, tabId] of Object.entries(expected)) {
    assert.equal(assetTabs.tabIdFor(assetType), tabId);
  }
  assert.equal(assetTabs.tabIdFor('contact_flow'), 'contact_flow');
  assert.equal(assetTabs.tabIdFor('unknown_asset'), null);
});
