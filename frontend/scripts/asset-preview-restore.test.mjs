import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// Bundle the real store (zustand included) so the test exercises the shipped
// updateAssetPreview logic, not a copy of it.
const bundled = await build({
  entryPoints: [path.join(ROOT, 'src/stores/builderStore.ts')],
  bundle: true,
  format: 'esm',
  platform: 'node',
  write: false,
  logLevel: 'silent',
});
const { useBuilderStore } = await import(
  `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString('base64')}`
);

const HOUR_AGO = Date.now() - 3_600_000;

test('filling a restored placeholder with its S3 content is an in-place update, not a regeneration', () => {
  // Live (2026-09-26): each page load re-inserted every restored asset as a
  // "재생성됨" card (the KB zip bytes shown as text) and autosave stored the copy.
  const store = useBuilderStore.getState();
  store.clearAssetPreviews();
  const base = { assetType: 'package', fileName: 'kb.zip', isComplete: true, createdAt: HOUR_AGO, s3Key: 'assets/s/package/kb.zip' };
  store.updateAssetPreview({ ...base, content: '' });
  store.updateAssetPreview({ ...base, content: 'PK\u0003\u0004 zip bytes' });
  const previews = Object.values(useBuilderStore.getState().assetPreviews);
  assert.equal(previews.length, 1);
  assert.equal(previews[0].isRegeneration, undefined);
  assert.equal(previews[0].content, 'PK\u0003\u0004 zip bytes');
});

test('a changed asset arriving whole after 30 s is still shown as a regeneration', async () => {
  const store = useBuilderStore.getState();
  store.clearAssetPreviews();
  store.updateAssetPreview({ assetType: 'acxd_flow', fileName: 'BookCleaning.json', content: '{"v":1}', isComplete: true, createdAt: HOUR_AGO });
  // preview keys carry a millisecond suffix; a real regeneration is never in the same ms
  await new Promise((resolve) => setTimeout(resolve, 5));
  store.updateAssetPreview({ assetType: 'acxd_flow', fileName: 'BookCleaning.json', content: '{"v":2}', isComplete: true });
  const previews = Object.values(useBuilderStore.getState().assetPreviews);
  assert.equal(previews.length, 2);
  const regenerated = previews.find((p) => p.isRegeneration);
  assert.ok(regenerated, 'the new version is a regeneration card');
  assert.equal(regenerated.previousContent, '{"v":1}');
});

test('re-delivering the newest version (reconnect lazy-load) adds no copy', async () => {
  // Live (Hanbit, 2026-09-26): book_appointment.md reached 298 copies — each
  // reconnect re-delivered v2 and it was compared with v1.
  const store = useBuilderStore.getState();
  store.clearAssetPreviews();
  const base = { assetType: 'operation_spec', operationId: 'book', fileName: 'book.md', isComplete: true, s3Key: 'assets/s/operation_spec/book/book.md' };
  store.updateAssetPreview({ ...base, content: 'v1', createdAt: HOUR_AGO });
  await new Promise((resolve) => setTimeout(resolve, 5));
  store.updateAssetPreview({ ...base, content: 'v2' });            // a real edit: the regeneration card
  for (let i = 0; i < 5; i++) {
    await new Promise((resolve) => setTimeout(resolve, 2));
    store.updateAssetPreview({ ...base, content: '' });            // reconnect: REST placeholder
    store.updateAssetPreview({ ...base, content: 'v2' });          // reconnect: S3 lazy-load
  }
  const previews = Object.values(useBuilderStore.getState().assetPreviews);
  assert.equal(previews.length, 2, `expected v1 + v2 only, got ${previews.length}`);
  assert.equal(previews.filter((p) => p.isRegeneration).length, 1);
});
