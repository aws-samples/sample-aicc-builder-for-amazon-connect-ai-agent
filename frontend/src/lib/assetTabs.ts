/**
 * Shared asset-tab grouping + labeling.
 *
 * Used by both the split-view AssetWorkspace and the fullscreen modal so they
 * group and label assets identically.
 */
import {
  Workflow,
  MessageSquare,
  BookOpen,
  FileCode,
  FileJson,
  Boxes,
  ShieldCheck,
  type LucideIcon,
} from 'lucide-react';
import type { AssetPreview } from '../types';

export type AssetTabGroup = 'classic' | 'acxd';

export interface TabMeta {
  icon: LucideIcon;
  label: string;
  labelKo: string;
  labelJa: string;
  group: AssetTabGroup;
}

export const TAB_META: Record<string, TabMeta> = {
  contact_flow: { icon: Workflow, label: 'Contact Flow', labelKo: 'Contact Flow', labelJa: 'Contact Flow', group: 'classic' },
  prompt: { icon: MessageSquare, label: 'AI Prompt', labelKo: 'AI 프롬프트', labelJa: 'AI プロンプト', group: 'classic' },
  faq: { icon: BookOpen, label: 'FAQ', labelKo: 'FAQ', labelJa: 'FAQ', group: 'classic' },
  lambda: { icon: FileCode, label: 'Lambda', labelKo: 'Lambda', labelJa: 'Lambda', group: 'classic' },
  openapi: { icon: FileJson, label: 'OpenAPI', labelKo: 'OpenAPI', labelJa: 'OpenAPI', group: 'classic' },
  cdk: { icon: Boxes, label: 'Infrastructure', labelKo: '인프라', labelJa: 'インフラ', group: 'classic' },
  cloudformation: { icon: Boxes, label: 'Infrastructure', labelKo: '인프라', labelJa: 'インフラ', group: 'classic' },
  acxd_flows: { icon: Workflow, label: 'Flows', labelKo: '플로우', labelJa: 'フロー', group: 'acxd' },
  acxd_slot_types: { icon: FileCode, label: 'Slot Types', labelKo: '슬롯 타입', labelJa: 'スロットタイプ', group: 'acxd' },
  acxd_data_requests: { icon: FileJson, label: 'Data Requests', labelKo: '데이터 요청', labelJa: 'データリクエスト', group: 'acxd' },
  acxd_guardrails: { icon: ShieldCheck, label: 'Guardrails', labelKo: '가드레일', labelJa: 'ガードレール', group: 'acxd' },
  acxd_knowledge_base: { icon: BookOpen, label: 'Knowledge Base', labelKo: '지식 베이스', labelJa: 'ナレッジベース', group: 'acxd' },
  acxd_application: { icon: Boxes, label: 'Application', labelKo: '애플리케이션', labelJa: 'アプリケーション', group: 'acxd' },
};

/** Stable, Classic-first tab order. */
export const TAB_ORDER = [
  'contact_flow',
  'prompt',
  'faq',
  'openapi',
  'lambda',
  'cdk',
  'acxd_flows',
  'acxd_slot_types',
  'acxd_data_requests',
  'acxd_guardrails',
  'acxd_knowledge_base',
  'acxd_application',
];

const TAB_ID_BY_ASSET_TYPE: Record<string, string> = {
  package: 'faq',
  cloudformation: 'cdk',
  acxd_flow: 'acxd_flows',
  acxd_slot_type: 'acxd_slot_types',
  acxd_data_request: 'acxd_data_requests',
  acxd_guardrail: 'acxd_guardrails',
  acxd_knowledge_base: 'acxd_knowledge_base',
  acxd_application: 'acxd_application',
  // Context variables are part of the ACXD application configuration.
  acxd_context_variable: 'acxd_application',
};

/** Normalize asset types into their user-facing tab id. */
export function tabIdFor(assetType: string): string | null {
  const mapped = TAB_ID_BY_ASSET_TYPE[assetType] || assetType;
  return TAB_META[mapped] ? mapped : null;
}

export function tabLabel(meta: TabMeta, language: string): string {
  if (language === 'ko-KR') return meta.labelKo;
  if (language === 'ja-JP') return meta.labelJa;
  return meta.label;
}

/**
 * Short label for a per-file chip / list item: prefer operationId, else fileName.
 * Group-level assets use their file name so application.json and
 * context_variables.json remain distinguishable in the Application tab.
 */
export function itemLabel(p: AssetPreview): string {
  if (
    p.assetType === 'faq' ||
    p.assetType === 'package' ||
    p.assetType === 'acxd_flow' ||
    p.assetType === 'acxd_application' ||
    p.assetType === 'acxd_context_variable' ||
    p.operationId === 'knowledge_base'
  ) {
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
    if (key.startsWith('__')) continue;
    const tabId = tabIdFor(preview.assetType);
    if (!tabId) continue;
    const list = byTab.get(tabId) || [];
    list.push({ key, preview });
    byTab.set(tabId, list);
  }

  for (const [tabId, list] of byTab) {
    const seen = new Set<string>();
    const deduped = list
      .sort((a, b) => (b.preview.createdAt || 0) - (a.preview.createdAt || 0))
      .filter((item) => {
        const id = `${item.preview.operationId || ''}|${item.preview.fileName || ''}`;
        if (seen.has(id)) return false;
        seen.add(id);
        return true;
      });
    byTab.set(tabId, deduped);
  }

  return TAB_ORDER.filter((id) => byTab.has(id)).map((tabId) => ({
    tabId,
    items: byTab.get(tabId)!,
    key: byTab.get(tabId)![0].key,
    preview: byTab.get(tabId)![0].preview,
  }));
}
