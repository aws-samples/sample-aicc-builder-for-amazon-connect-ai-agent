/**
 * Shared asset-tab grouping + labeling.
 *
 * Used by both the split-view AssetWorkspace (right pane) and the fullscreen
 * AssetFullscreenModal so they group/label assets identically. Keeping this in
 * one place is what lets the fullscreen view show the SAME tab strip + per-tab
 * file list the workspace does (so the user can browse every asset without
 * leaving fullscreen), instead of being locked to a single asset.
 */
import { Workflow, MessageSquare, BookOpen, FileCode, FileJson, Boxes, type LucideIcon } from 'lucide-react';
import type { AssetPreview } from '../types';

export interface TabMeta {
  icon: LucideIcon;
  label: string;
  labelKo: string;
}

export const TAB_META: Record<string, TabMeta> = {
  contact_flow: { icon: Workflow, label: 'Contact Flow', labelKo: 'Contact Flow' },
  prompt: { icon: MessageSquare, label: 'AI Prompt', labelKo: 'AI 프롬프트' },
  faq: { icon: BookOpen, label: 'FAQ', labelKo: 'FAQ' },
  lambda: { icon: FileCode, label: 'Lambda', labelKo: 'Lambda' },
  openapi: { icon: FileJson, label: 'OpenAPI', labelKo: 'OpenAPI' },
  cdk: { icon: Boxes, label: 'Infrastructure', labelKo: '인프라' },
  cloudformation: { icon: Boxes, label: 'Infrastructure', labelKo: '인프라' },
};

/** Stable, Connect-first tab order. */
export const TAB_ORDER = ['contact_flow', 'prompt', 'faq', 'openapi', 'lambda', 'cdk'];

/** Normalize asset types into a single tab id (faq+package → faq, cdk+cloudformation → cdk). */
export function tabIdFor(assetType: string): string | null {
  if (assetType === 'package') return 'faq';
  if (assetType === 'cloudformation') return 'cdk';
  if (TAB_META[assetType]) return assetType;
  return null;
}

/**
 * Short label for a per-file chip / list item: prefer operationId, else fileName.
 * EXCEPT FAQ/package docs, which all share the synthetic group operationId
 * 'knowledge_base' — there the meaningful per-file label is the fileName
 * (e.g. 06_breakfast.md), so don't collapse every item to "knowledge_base".
 */
export function itemLabel(p: AssetPreview): string {
  if (p.assetType === 'faq' || p.assetType === 'package' || p.operationId === 'knowledge_base') {
    return p.fileName || p.operationId || '';
  }
  return p.operationId || p.fileName || '';
}

export interface AssetTab {
  tabId: string;
  items: Array<{ key: string; preview: AssetPreview }>;
  /** Representative (newest) item. */
  key: string;
  preview: AssetPreview;
}

/**
 * Group all previews into tabs: one tab per normalized type, each holding its
 * full item list (newest first, de-duped by operationId|fileName). Returns tabs
 * in TAB_ORDER, only for types that have at least one asset.
 */
export function groupAssetTabs(assetPreviews: Record<string, AssetPreview>): AssetTab[] {
  const byTab = new Map<string, Array<{ key: string; preview: AssetPreview }>>();
  for (const [key, preview] of Object.entries(assetPreviews)) {
    // Skip any internal holder keys (prefixed __) that have no tab.
    if (key.startsWith('__')) continue;
    const tabId = tabIdFor(preview.assetType);
    if (!tabId) continue;
    const list = byTab.get(tabId) || [];
    list.push({ key, preview });
    byTab.set(tabId, list);
  }
  // Sort each tab's items newest-first; de-dup by operationId/fileName keeping newest.
  for (const [tabId, list] of byTab) {
    const seen = new Set<string>();
    const deduped = list
      .sort((a, b) => (b.preview.createdAt || 0) - (a.preview.createdAt || 0))
      .filter((it) => {
        const id = `${it.preview.operationId || ''}|${it.preview.fileName || ''}`;
        if (seen.has(id)) return false;
        seen.add(id);
        return true;
      });
    byTab.set(tabId, deduped);
  }
  return TAB_ORDER.filter((id) => byTab.has(id)).map((id) => ({
    tabId: id,
    items: byTab.get(id)!,
    key: byTab.get(id)![0].key,
    preview: byTab.get(id)![0].preview,
  }));
}
