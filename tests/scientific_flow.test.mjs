import test from 'node:test';
import assert from 'node:assert/strict';
import { regionPlacement, entirelyScientific } from '../paper_reader/web/scientific-flow.js';

test('regions follow first linked block, deduplicate and keep orphans separate', () => {
  const page = { blocks: [{ id: 'a' }, { id: 'b', protected_spans: [{ region_id: 'r2' }] }],
    scientific_regions: [{ id: 'r1', block_ids: ['b', 'a'] }, { id: 'r2' }, { id: 'orphan', block_ids: ['missing'] }, { id: 'r1' }] };
  const original = structuredClone(page), plan = regionPlacement(page);
  assert.deepEqual(plan.before.get('a').map(r => r.id), ['r1']);
  assert.deepEqual(plan.before.get('b').map(r => r.id), ['r2']);
  assert.deepEqual(plan.trailing.map(r => r.id), ['orphan']);
  assert.deepEqual(page, original);
});
test('empty and reviewed pages do not invent position links', () => {
  assert.deepEqual(regionPlacement({}).trailing, []);
  assert.equal(regionPlacement({ blocks: [{ id: 'new' }], scientific_regions: [{ id: 'r', block_ids: ['old'] }] }).before.size, 0);
});
test('only complete exact protected coverage folds extraction', () => {
  const regions = [{ id: 'r' }];
  assert.equal(entirelyScientific({ en: ' E = mc² ', protected_spans: [{ start: 1, end: 8, region_id: 'r' }] }, regions), true);
  assert.equal(entirelyScientific({ en: 'E = mc². Body prose', protected_spans: [{ start: 0, end: 7, region_id: 'r' }] }, regions), false);
  assert.equal(entirelyScientific({ en: '', protected_spans: [] }, regions), false);
});
test('Python code-point offsets handle non-BMP symbols', () => {
  assert.equal(entirelyScientific({ en: '𝑥²', protected_spans: [{ start: 0, end: 2, region_id: 'r' }] }, [{ id: 'r' }]), true);
});
test('untrusted, malformed and stale coverage remains readable', () => {
  for (const span of [{ start: -1, end: 2, region_id: 'r' }, { start: 0, end: 4, region_id: 'r' },
    { start: 0, end: 2.5, region_id: 'r' }, { start: 0, end: 2, region_id: 'missing' }]) {
    assert.equal(entirelyScientific({ en: 'x²', protected_spans: [span] }, [{ id: 'r' }]), false);
  }
  assert.equal(entirelyScientific({ en: 'x²', protected_spans: [] }, [{ id: 'r' }]), false);
});
