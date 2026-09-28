/**
 * Display-side clean-up of internal ids in the builder's own chat replies.
 *
 * The interview maps the customer's document to requirement items (R1…),
 * quoted sentences (Q1…), enum exclusions (E:…) and field conflicts (X:…). Those
 * ids are bookkeeping for the gates, not something the customer should read,
 * and the prompt says so — but the reply still carried "(R17)" or "(R6, R7)"
 * in 3 of 3 live runs (2026-09-25, 09-26 twice). Only the parenthesised form is
 * removed: it is unambiguous, and removing it never changes the sentence.
 *
 * The pattern must stay linear on model text. An E:/X: body stops at a
 * separator, so "(E:a, E:b)" has exactly one reading; when the body could hold
 * one, an unclosed "(E:!,E:!,E:…" split 2^n ways (CodeQL js/redos). An id whose
 * value holds a separator or a space is therefore left visible. The space before
 * a group is trimmed from the text instead of being matched by a leading \s*,
 * which rescanned a run of spaces from every position in it.
 */
const PAREN_IDS = /[(（]\s*(?:[RQ]\d{1,3}|[EX]:[^)）\s,，·/]{1,80})(?:\s*[,，·/]\s*(?:[RQ]\d{1,3}|[EX]:[^)）\s,，·/]{1,80}))*\s*[)）]/;

export function stripInternalRequirementIds(text: string): string {
  if (!text || !/[(（]\s*(?:[RQ]\d|[EX]:)/.test(text)) return text;
  // Every piece but the last ended at a removed group: the space before it goes too.
  const pieces = text.split(PAREN_IDS);
  return pieces.map((piece, i) => (i < pieces.length - 1 ? piece.trimEnd() : piece)).join('');
}
