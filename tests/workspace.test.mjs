import assert from 'node:assert/strict';
import test from 'node:test';
import { dailyCollectionMessage } from '../paper_reader/web/workspace.js';

const today = '2026-10-29';
const settings = { daily_enabled: true, daily_time: '09:00' };
test('an attempted or interrupted collection never becomes a success label', () => {
  for (const status of ['failed', 'paused', 'running', undefined]) {
    const text = dailyCollectionMessage({ ...settings, daily_sync: { date: today, status } }, today);
    assert.doesNotMatch(text, /已完成/);
  }
  assert.match(dailyCollectionMessage({ ...settings, daily_sync: { date: today, status: 'failed' } }, today), /手动重试/);
  assert.match(dailyCollectionMessage({ ...settings, daily_sync: { date: today, status: 'done' } }, today), /已完成/);
});
test('old successes do not describe today and disabled collection is explicit', () => {
  assert.match(dailyCollectionMessage(settings, today), /启动时会补跑/);
  assert.doesNotMatch(dailyCollectionMessage({ ...settings, daily_sync: { date: '2026-10-28', status: 'done' } }, today), /今天.*已完成/);
  assert.match(dailyCollectionMessage({ daily_enabled: false }, today), /未开启/);
});
