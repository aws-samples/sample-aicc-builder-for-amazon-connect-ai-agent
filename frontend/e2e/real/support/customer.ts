/**
 * The simulated "customer" that talks to the real AICC Builder agent.
 *
 * Two modes:
 *  - LLM (default when AWS creds + Bedrock are reachable): a Claude Sonnet 4.6+
 *    call role-plays a concrete persona (ABC 호텔) and answers the agent's ACTUAL
 *    latest message — so the dialogue stays coherent instead of firing canned
 *    nudges blind to the question. A hard rule keeps it decisive: once the agent
 *    has the info it needs, confirm and tell it to proceed (don't reopen specs).
 *  - Heuristic fallback (no creds / Bedrock error): keyword-aware replies —
 *    answer obvious questions from a persona fact-sheet, else affirm + proceed.
 *
 * Either way the reply is grounded in what the agent said, and every exchange is
 * recorded by the Transcript for review.
 */
// NOTE: We do NOT static-import @aws-sdk/client-bedrock-runtime here. Playwright's
// TS transform tries to load the SDK's transitive .ts sources (@aws-crypto/crc32)
// and crashes ("Unexpected module status 3"). Instead we lazy-require the CJS
// build at runtime via createRequire, which loads the prebuilt dist-cjs cleanly.
import { createRequire } from 'node:module';
const requireCjs = createRequire(import.meta.url);
// eslint-disable-next-line @typescript-eslint/no-explicit-any
let _sdk: any = null;
function loadSdk() {
  if (!_sdk) _sdk = requireCjs('@aws-sdk/client-bedrock-runtime');
  return _sdk;
}

/** Concrete, self-consistent persona + facts the agent might ask about. */
export const PERSONA = {
  company: 'ABC 호텔',
  industry: '호텔/숙박',
  language: '한국어',
  kickoff: [
    '호텔 예약 AI 음성 상담원을 만들어 주세요. 회사명은 "ABC 호텔", 산업은 호텔/숙박입니다.',
    '필요한 작업은 3가지입니다: (1) 예약 조회 — 예약번호(reservationId)로 조회,',
    '(2) 예약 취소 — 예약번호(reservationId)로 취소,',
    '(3) 객실 공실 조회 — 체크인/체크아웃 날짜(checkInDate, checkOutDate)로 조회.',
    '발신자 전화번호 기반 개인화 인사는 필요 없습니다. 한국어만 지원하면 됩니다.',
    '새 데이터베이스를 생성해 주세요(기존 DB 없음). 데모용 테스트 전화번호는 +821012345678 입니다.',
    '이 내용으로 충분합니다. 바로 진행해 주세요.',
  ].join(' '),
  facts: [
    '회사명: ABC 호텔, 산업: 호텔/숙박, 지원 언어: 한국어만.',
    '작업 3개: 예약 조회(reservationId), 예약 취소(reservationId), 객실 공실 조회(checkInDate/checkOutDate).',
    '발신자 전화번호 개인화 인사: 불필요.',
    '데이터베이스: 신규 생성 (기존 DB 없음).',
    '데모 테스트 전화번호: +821012345678.',
    '인증/본인확인: 예약번호로 충분, 별도 인증 불필요.',
    '외부 연동(SMS/카카오/이메일/Redmine 등): 불필요.',
    '확정되지 않은 세부사항은 일반적인 호텔 기준의 합리적 기본값으로 진행해도 좋음.',
  ],
};

// The customer brain. Sonnet 4.6+ for a coherent, decisive role-play (the agent
// it talks to runs on Opus, so a capable customer keeps the dialogue on-track).
// Override with CUSTOMER_LLM_MODEL.
const CUSTOMER_MODEL = process.env.CUSTOMER_LLM_MODEL || 'global.anthropic.claude-sonnet-4-6';

const AFFIRM = /진행|좋(아|습니다)|네|확인|맞아|그래|ok|okay|proceed|go ahead|sounds good|yes/i;

export interface CustomerReply {
  text: string;
  via: 'llm' | 'heuristic';
}

export class Customer {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private client: any = null;
  private llmDisabled = false;

  constructor(opts: { region?: string; disableLlm?: boolean } = {}) {
    if (opts.disableLlm || process.env.CUSTOMER_LLM === '0') {
      this.llmDisabled = true;
    } else {
      try {
        const { BedrockRuntimeClient } = loadSdk();
        this.client = new BedrockRuntimeClient({
          region: opts.region || process.env.CUSTOMER_LLM_REGION || 'us-east-1',
        });
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn(`[customer] could not init Bedrock client; heuristic only: ${(e as Error).message}`);
        this.llmDisabled = true;
      }
    }
  }

  /** Produce the customer's reply to the agent's latest message. */
  async reply(agentMessage: string, ctx: { phase?: string; turn: number }): Promise<CustomerReply> {
    const trimmed = (agentMessage || '').trim();
    if (!this.llmDisabled && this.client) {
      try {
        const text = await this.askLlm(trimmed, ctx);
        if (text) return { text, via: 'llm' };
      } catch (e) {
        // Disable after first failure so we don't pay repeated timeouts.
        this.llmDisabled = true;
        // eslint-disable-next-line no-console
        console.warn(`[customer] LLM unavailable, falling back to heuristic: ${(e as Error).message}`);
      }
    }
    return { text: this.heuristic(trimmed, ctx), via: 'heuristic' };
  }

  private async askLlm(agentMessage: string, ctx: { phase?: string; turn: number }): Promise<string> {
    const system = [
      'You are role-playing a CUSTOMER talking to an AI assistant that builds an Amazon Connect contact-center.',
      'Reply ONLY as the customer, in Korean, in 1–2 short sentences. No preamble, no quotes, no markdown.',
      'You are a busy, decisive customer. Answer the assistant\'s latest question directly using the FACTS below.',
      'If the assistant asks something not in the FACTS, give a reasonable, concrete default for a typical hotel and move on — never say "I don\'t know".',
      'If the assistant is summarizing/confirming or asking whether to proceed/generate, CONFIRM and tell it to proceed. Do NOT reopen or add new requirements.',
      `If it appears generation already started (phase=${ctx.phase ?? 'interview'}), just approve continuing to the next step.`,
      '',
      'FACTS about your project:',
      ...PERSONA.facts.map((f) => '- ' + f),
    ].join('\n');

    const { ConverseCommand } = loadSdk();
    const res = await this.client!.send(
      new ConverseCommand({
        modelId: CUSTOMER_MODEL,
        system: [{ text: system }],
        messages: [
          {
            role: 'user',
            content: [
              {
                text:
                  `The assistant just said:\n"""\n${agentMessage || '(no text; it performed tool/asset actions)'}\n"""\n\n` +
                  'Your reply as the customer:',
              },
            ],
          },
        ],
        inferenceConfig: { maxTokens: 200, temperature: 0.3 },
      }),
    );
    const out = res.output?.message?.content?.map((c) => ('text' in c ? c.text : '')).join('').trim();
    return out || '';
  }

  /** Keyword-aware fallback grounded in the persona facts. */
  private heuristic(agentMessage: string, ctx: { phase?: string; turn: number }): string {
    const m = agentMessage.toLowerCase();
    // If the agent is mid/late and clearly working/confirming, just approve.
    if (!agentMessage || ctx.phase === 'generation' || AFFIRM.test(agentMessage)) {
      return '네, 좋습니다. 그대로 진행해 주세요.';
    }
    if (/언어|language/.test(m)) return '한국어만 지원하면 됩니다.';
    if (/전화|phone|번호|개인화|greeting/.test(m)) return '발신자 전화번호 기반 개인화 인사는 필요 없습니다. 데모 번호는 +821012345678 입니다.';
    if (/데이터|database|db|스키마|schema/.test(m)) return '기존 DB는 없습니다. 새 데이터베이스를 생성해 주세요.';
    if (/인증|auth|본인|확인/.test(m)) return '예약번호로 확인하면 충분합니다. 별도 인증은 필요 없습니다.';
    if (/작업|operation|업무|기능|할 수 있|무엇/.test(m))
      return '예약 조회, 예약 취소, 객실 공실 조회 3가지면 됩니다.';
    if (/회사|업종|industry|company|이름/.test(m)) return 'ABC 호텔이고, 호텔/숙박업입니다.';
    if (/\?|할까요|드릴까요|진행|생성|시작|확인/.test(agentMessage))
      return '네, 그 내용대로 진행해 주세요.';
    // Default: keep it moving with the canonical use case once, then affirm.
    return ctx.turn === 0 ? PERSONA.kickoff : '네, 좋습니다. 그대로 진행해 주세요.';
  }
}
