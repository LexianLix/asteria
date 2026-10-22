import test from 'node:test';
import assert from 'node:assert/strict';
import { sourceSyncControl } from '../paper_reader/web/source-status.js';

test('cooldown disables manual action and names the earliest retry time', () => {
  const result = sourceSyncControl({ status: 'failed' }, { retry_at: '2026-10-17T08:00:00Z' }, Date.parse('2026-10-17T07:00:00Z'));
  assert.equal(result.disabled, true); assert.match(result.label, /限流/); assert.match(result.title, /到期不会自动重发/);
});
test('expiry restores manual action without scheduling anything', () => {
  const result = sourceSyncControl({ status: 'failed' }, { retry_at: '2026-10-17T08:00:00Z' }, Date.parse('2026-10-17T09:00:00Z'));
  assert.equal(result.disabled, false); assert.equal(result.label, '↻ 获取新论文');
});
test('running stays disabled and another source without cooldown is available', () => {
  assert.equal(sourceSyncControl({ status: 'running' }).disabled, true);
  assert.equal(sourceSyncControl({ status: 'failed' }, null).disabled, false);
});
test('invalid timestamps fail closed and far-future server timestamps stay blocked', () => {
  assert.equal(sourceSyncControl({}, { retry_at: 'bad' }).disabled, true);
  assert.equal(sourceSyncControl({}, { retry_at: '9999-12-31T23:59:59.999999+00:00' }).disabled, true);
});
