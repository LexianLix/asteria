import test from 'node:test';
import assert from 'node:assert/strict';
import { translationJobState, translationPageMessage } from '../paper_reader/web/translation-status.js';

const partial = { total_pages: 12, complete_pages: 10, missing_text: 94, missing_local: 136, blocked_pages: 0, no_text_pages: 0 };

test('a completed historical job never hides current-layout gaps', () => {
  const result = translationJobState({ status: 'done', done: 12, total: 12 }, partial);
  assert.match(result.label, /仍有缺项/);
  assert.match(result.progress, /10 \/ 12/);
  assert.equal(result.button, '补译缺项');
  assert.doesNotMatch(result.label, /翻译完成|译文已完成/);
});
test('completion without current metadata is not a full-document claim', () => {
  assert.equal(translationJobState({ status: 'done', scope: 'page' }).label, '上次翻译任务已结束');
});
test('an entirely new document still offers its first full translation', () => {
  assert.equal(translationJobState({}, { total_pages: 2, complete_pages: 0, missing_text: 6,
    missing_local: 0, pages: [{ cached: 0 }, { cached: 0 }] }).button, '翻译全文');
});
test('complete cache is explicitly distinguished from translation accuracy', () => {
  const result = translationJobState({ status: 'done' }, { ...partial, complete_pages: 12, missing_text: 0, missing_local: 0 });
  assert.match(result.label, /缓存齐全/);
  assert.doesNotMatch(result.label, /准确|质量通过/);
});
test('blocked extraction and empty scans cannot become a completed translation', () => {
  assert.match(translationJobState({ status: 'done' }, { ...partial, blocked_pages: 1 }).label, /核对/);
  assert.match(translationJobState({ status: 'done' }, { ...partial, missing_text: 0, missing_local: 0, no_text_pages: 1 }).label, /无可提取文字/);
});
test('active and failed tasks retain their actual lifecycle state', () => {
  const running = translationJobState({ status: 'running', done: 2, total: 12 }, partial);
  assert.equal(running.label, '正在翻译');
  assert.match(running.progress, /2 \/ 12/);
  assert.equal(translationJobState({ status: 'failed' }, partial).label, '翻译失败');
});
test('partially cached pages report both kinds of gaps instead of showing silence', () => {
  const message = translationPageMessage({ state: 'partial', missing_text: 3, missing_local: 7 });
  assert.match(message, /3/); assert.match(message, /7/); assert.match(message, /译文检查/);
  assert.equal(translationPageMessage({ state: 'complete' }), '');
  assert.match(translationPageMessage({ state: 'blocked' }), /核对正文/);
});
