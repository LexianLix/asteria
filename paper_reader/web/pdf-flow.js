/* Continuous source PDF: geometry-only shells, bounded nearby raster pages.
 * No PDF text extraction, OCR, or model calls. Each page owns its ink layer.
 */
export function createPdfFlow({ host, scroller, paint, bindInk, onPosition, onError, drawingPage }) {
  let session = null, frame = 0;
  const gutter = 20;
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

  function dispose() {
    if (frame) cancelAnimationFrame(frame);
    frame = 0;
    const old = session; session = null;
    if (old) {
      old.closed = true;
      for (const entry of old.entries) release(entry);
    }
    host.replaceChildren(); host.hidden = true;
  }

  function release(entry) {
    entry.task?.cancel(); entry.task = null;
    entry.canvas.width = entry.canvas.height = 0;
    entry.canvas.hidden = true;
    entry.layer.setAttribute('hidden', ''); entry.layer.replaceChildren();
    entry.placeholder.hidden = false; entry.ready = false;
    entry.element.dataset.rendered = 'false';
  }

  function position() {
    if (!session?.entries.length) return null;
    const marker = scroller.getBoundingClientRect().top + gutter;
    let chosen = session.entries[0];
    for (const entry of session.entries) {
      if (entry.element.getBoundingClientRect().top > marker) break;
      chosen = entry;
    }
    const rect = chosen.stage.getBoundingClientRect();
    return { page: chosen.number, offset: clamp((marker - rect.top) / rect.height, 0, 1) };
  }

  function goTo(number, offset = 0) {
    const entry = session?.entries[clamp(Math.floor(number), 0, session.entries.length - 1)];
    if (!entry) return;
    const delta = entry.stage.getBoundingClientRect().top - scroller.getBoundingClientRect().top
      + clamp(offset, 0, 1) * entry.viewport.height - gutter;
    scroller.scrollTop += delta;
    refresh();
  }

  function refresh() {
    const active = session;
    if (!active || active.closed || host.hidden || !active.entries.length) return;
    const rect = scroller.getBoundingClientRect();
    const visible = active.entries.filter(entry => {
      const box = entry.element.getBoundingClientRect();
      return box.bottom >= rect.top && box.top <= rect.bottom;
    });
    const current = position();
    const first = visible[0]?.number ?? current.page;
    const last = visible.at(-1)?.number ?? current.page;
    active.wanted = new Set();
    for (let n = Math.max(0, first - 1); n <= Math.min(active.entries.length - 1, last + 1); n++) active.wanted.add(n);
    const pinned = drawingPage();
    if (Number.isInteger(pinned)) active.wanted.add(pinned);
    for (const entry of active.entries) {
      if (!active.wanted.has(entry.number)) release(entry);
    }
    // Prioritize visible pages; at most two render tasks for this document.
    active.queue = [...active.wanted].sort((a, b) => Math.abs(a - current.page) - Math.abs(b - current.page));
    pump(active);
    if (!Number.isInteger(pinned)) onPosition(current);
  }

  function pump(active) {
    if (session !== active || active.closed) return;
    while (active.running < 2 && active.queue.length) {
      const entry = active.entries[active.queue.shift()];
      if (entry.ready || entry.busy || entry.failed || !active.wanted.has(entry.number)) continue;
      entry.busy = true; active.running++;
      render(active, entry).finally(() => {
        entry.busy = false; active.running--;
        if (session === active) {
          if (active.wanted.has(entry.number) && !entry.ready && !entry.failed) active.queue.push(entry.number);
          pump(active);
        }
      });
    }
  }

  async function render(active, entry) {
    let task;
    try {
      const page = await active.pdf.getPage(entry.number + 1);
      if (session !== active || !active.wanted.has(entry.number)) return;
      // Limit raster allocation per page at high DPI/zoom; CSS geometry remains
      // exact. Normal page rendering stays at native device resolution.
      const ratio = Math.min(2, window.devicePixelRatio || 1,
        Math.sqrt(4000000 / (entry.viewport.width * entry.viewport.height)));
      entry.canvas.width = Math.max(1, Math.floor(entry.viewport.width * ratio));
      entry.canvas.height = Math.max(1, Math.floor(entry.viewport.height * ratio));
      entry.canvas.style.width = entry.viewport.width + 'px'; entry.canvas.style.height = entry.viewport.height + 'px';
      task = page.render({ canvasContext: entry.canvas.getContext('2d'), viewport: entry.viewport,
        transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0] });
      entry.task = task;
      await task.promise;
      if (session !== active || !active.wanted.has(entry.number) || entry.task !== task) return;
      entry.ready = true; entry.element.dataset.rendered = 'true';
      entry.canvas.hidden = false; entry.layer.removeAttribute('hidden'); entry.placeholder.hidden = true;
      paint(entry.layer, entry.number);
    } catch (error) {
      if (session !== active || !active.wanted.has(entry.number) || error.name === 'RenderingCancelledException') return;
      release(entry); entry.failed = true;
      entry.placeholder.textContent = `第 ${entry.number + 1} 页加载失败 `;
      const retry = document.createElement('button'); retry.className = 'plain'; retry.textContent = '重试此页';
      retry.onclick = () => {
        entry.failed = false; entry.placeholder.textContent = '正在加载原页…'; refresh();
      };
      entry.placeholder.append(retry); onError(error);
    } finally {
      if (entry.task === task) entry.task = null;
    }
  }

  async function show(pdf, zoom, target) {
    const width = Math.max(280, scroller.clientWidth - 48);
    if (session?.pdf === pdf && session.zoom === zoom && Math.abs(session.width - width) < 1) {
      host.hidden = false; goTo(target.page, target.offset); return;
    }
    dispose(); host.hidden = false;
    const active = { pdf, zoom, width, entries: [], wanted: new Set(), queue: [], running: 0, closed: false };
    session = active;
    // Geometry is cheap and stabilizes mixed-size/rotated pages before scrolling.
    // This loads page metadata, NOT all page canvases or PDF text/OCR.
    const viewports = new Array(pdf.numPages);
    let cursor = 0;
    try {
      await Promise.all(Array.from({ length: Math.min(4, pdf.numPages) }, async () => {
        while (cursor < pdf.numPages && session === active) {
          const n = cursor++, page = await pdf.getPage(n + 1);
          const original = page.getViewport({ scale: 1 });
          viewports[n] = page.getViewport({ scale: Math.min(1.5, width / original.width) * zoom });
        }
      }));
    } catch (error) {
      if (session === active) { dispose(); throw error; }
      return;
    }
    if (session !== active) return;
    const fragment = document.createDocumentFragment();
    for (let n = 0; n < pdf.numPages; n++) {
      const viewport = viewports[n], element = document.createElement('section');
      element.className = 'pdf-flow-page'; element.dataset.page = n; element.dataset.rendered = 'false';
      element.setAttribute('aria-label', `原文第 ${n + 1} 页`);
      const caption = document.createElement('div'); caption.className = 'pdf-page-caption'; caption.textContent = `${n + 1} / ${pdf.numPages}`;
      const stage = document.createElement('div'); stage.className = 'page-stage';
      stage.style.width = viewport.width + 'px'; stage.style.height = viewport.height + 'px';
      const canvas = document.createElement('canvas'); canvas.width = canvas.height = 0; canvas.hidden = true;
      const layer = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      layer.setAttribute('viewBox', '0 0 1000 1000'); layer.setAttribute('preserveAspectRatio', 'none');
      layer.setAttribute('aria-label', `第 ${n + 1} 页标注层`); layer.dataset.page = n; layer.setAttribute('hidden', '');
      bindInk(layer);
      const placeholder = document.createElement('div'); placeholder.className = 'pdf-page-placeholder'; placeholder.textContent = '原页按需加载';
      stage.append(canvas, layer, placeholder); element.append(caption, stage); fragment.append(element);
      active.entries.push({ number: n, element, stage, canvas, layer, placeholder, viewport, ready: false, busy: false, failed: false, task: null });
    }
    host.replaceChildren(fragment); goTo(target.page, target.offset);
  }

  function schedule() {
    if (!session || frame) return;
    frame = requestAnimationFrame(() => { frame = 0; refresh(); });
  }
  scroller.addEventListener('scroll', schedule, { passive: true });
  return { show, dispose, goTo, position,
    layers: () => session?.entries.filter(entry => entry.ready).map(entry => entry.layer) || [],
    setTool: tool => host.classList.toggle('drawing', tool !== 'read'),
    get active() { return !!session && !session.closed; } };
}
