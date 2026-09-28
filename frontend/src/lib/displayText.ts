/**
 * Display-side clean-up of internal ids in the builder's own chat replies.
 *
 * The interview maps the customer's document to requirement items (R1…),
 * quoted sentences (Q1…), enum exclusions (E:…) and field conflicts (X:…). Those
 * ids are bookkeeping for the gates, not something the customer should read,
 * and the prompt says so — but the reply still carried "(R17)" or "(R6, R7)"
 * in 3 of 3 live runs (2026-09-25, 09-26 twice). Only the parenthesised form is
 * removed: it is unambiguous, and removing it never changes the sentence.
 */
const PAREN_IDS = /\s*[(（]\s*(?:[RQ]\d{1,3}|[EX]:[^)）\s]{1,80})(?:\s*[,，·/]\s*(?:[RQ]\d{1,3}|[EX]:[^)）\s]{1,80}))*\s*[)）]/g;

export function stripInternalRequirementIds(text: string): string {
  if (!text || !/[(（]\s*(?:[RQ]\d|[EX]:)/.test(text)) return text;
  return text.replace(PAREN_IDS, '');
}
