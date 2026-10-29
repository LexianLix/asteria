import assert from 'node:assert/strict';
import test from 'node:test';
import { readingReviewMessage } from '../paper_reader/web/reading-quality.js';
const status = { pages: [{ page_number: 0, issues: [] }, { page_number: 6, issues: [{ code: 'fragmented_text' }] }] };

test('consent warning names affected pages in the selected scope', () => {
  assert.match(readingReviewMessage(status), /第 7 页/);
  assert.match(readingReviewMessage(status, [6]), /提取异常/);
  assert.equal(readingReviewMessage(status, [0]), '');
  assert.equal(readingReviewMessage(status, []), '');
});

test('unavailable diagnostics never claim success', () => {
  assert.match(readingReviewMessage(null), /尚未检查/);
  assert.equal(readingReviewMessage({ pages: [] }), '');
});

test('large documents keep the consent warning bounded', () => {
  const pages = Array.from({ length: 90 }, (_, n) => ({ page_number: n, issues: [{}] }));
  const message = readingReviewMessage({ pages });
  assert.match(message, /等/);
  assert.ok(message.length < 180);
});
