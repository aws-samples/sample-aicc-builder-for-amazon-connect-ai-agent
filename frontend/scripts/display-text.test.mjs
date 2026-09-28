import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { transform } from 'esbuild';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = await readFile(path.join(ROOT, 'src/lib/displayText.ts'), 'utf8');
const { code } = await transform(source, { loader: 'ts', format: 'esm', target: 'es2020' });
const { stripInternalRequirementIds } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('parenthesised requirement ids leave the reply, the sentence stays intact', () => {
  // live, Hanbit ACXD only, 2026-09-26
  assert.equal(
    stripInternalRequirementIds('문서에 응급 상황 안내 문구 "응급 상황이면 119 또는 응급실(24시간)로 연락해 주세요"가 있는데(R17), 이건 안전 관련이라'),
    '문서에 응급 상황 안내 문구 "응급 상황이면 119 또는 응급실(24시간)로 연락해 주세요"가 있는데, 이건 안전 관련이라',
  );
  assert.equal(stripInternalRequirementIds('동의 문구 (Q2, Q3)는 원문 그대로 둡니다.'), '동의 문구는 원문 그대로 둡니다.');
  assert.equal(stripInternalRequirementIds('오타 (E:book.serviceType=종합세추) 확인'), '오타 확인');
  assert.equal(stripInternalRequirementIds('둘 (R6, R7)과 셋 (Q1·Q2/E:op.f=v) 그리고 (X:name)'), '둘과 셋 그리고');
  assert.equal(stripInternalRequirementIds('전각（R4，R5）끝'), '전각끝');
});

test('text built to make the pattern backtrack is handled in linear time', () => {
  // CodeQL js/redos on PR #57: an unclosed "(E:!,E:!,E:…" took about 2^n steps
  // (n=22 66 ms, each extra id x1.9), and a leading \s* rescanned a run of spaces
  // from every position in it (32,000 spaces 467 ms, quadratic).
  for (const text of ['(E:' + '!,E:'.repeat(28), 'x' + ' '.repeat(64000) + 'y (R1']) {
    const started = performance.now();
    assert.equal(stripInternalRequirementIds(text), text);
    const elapsed = performance.now() - started;
    assert.ok(elapsed < 250, `took ${Math.round(elapsed)} ms`);
  }
});

test('ordinary parentheses and codes are untouched', () => {
  for (const text of ['응급실(24시간)', '제품 (R2 버킷 아님)', 'Room (R&D)', '예약번호 (A20260916)', '평일(08:30–17:30)', '']) {
    assert.equal(stripInternalRequirementIds(text), text);
  }
});
