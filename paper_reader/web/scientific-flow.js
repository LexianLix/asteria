// Presentation only: do not rewrite cached text, translation IDs or note anchors.
export function regionPlacement(page) {
  const before = new Map(), trailing = [], seen = new Set();
  for (const region of page.scientific_regions || []) {
    if (!region.id || seen.has(region.id)) continue;
    seen.add(region.id);
    const linked = new Set(region.block_ids || []);
    const anchor = (page.blocks || []).find(block => linked.has(block.id) ||
      (block.protected_spans || []).some(span => span.region_id === region.id));
    if (!anchor) trailing.push(region);
    else {
      if (!before.has(anchor.id)) before.set(anchor.id, []);
      before.get(anchor.id).push(region);
    }
  }
  return { before, trailing };
}

export function entirelyScientific(block, regions) {
  const text = Array.from(block.en || ''), valid = new Set(regions.map(region => region.id));
  if (!text.some(char => !/\s/u.test(char))) return false;
  const covered = new Set();
  for (const span of block.protected_spans || []) {
    if (!valid.has(span.region_id) || !Number.isInteger(span.start) || !Number.isInteger(span.end) ||
        span.start < 0 || span.end <= span.start || span.end > text.length) return false;
    for (let i = span.start; i < span.end; i++) covered.add(i);
  }
  return text.every((char, index) => /\s/u.test(char) || covered.has(index));
}
