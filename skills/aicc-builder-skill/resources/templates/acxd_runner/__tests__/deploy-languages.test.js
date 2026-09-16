'use strict';
// Live (2026-09-10): UpdateApplicationDeployment refused "A deployment requires at least one language code".
const test = require('node:test');
const assert = require('node:assert');
const { applicationLanguageCodes } = require('../lib/steps');

test('language codes come from settings.languageCodes / languageSettings / languageCode, de-duplicated', () => {
  const doc = { settings: { languageCode: 'en-US', languageCodes: ['en-US'], languageSettings: [{ languageCode: 'ko-KR' }] } };
  assert.deepStrictEqual(applicationLanguageCodes(doc), ['en-US', 'ko-KR']);
  assert.deepStrictEqual(applicationLanguageCodes({ mainLanguageCode: 'ja-JP' }), ['ja-JP']);
  assert.deepStrictEqual(applicationLanguageCodes({}), []);
});
