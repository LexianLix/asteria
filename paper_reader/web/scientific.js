// Original PDF crops are rendered locally and lazily, never sent to a model.
export function installScientific({ state, $, node, api, toast, loadDocument, locateBlock, renderPage, openTranslation }) {
  const button = node('button', 'plain', '公式 / 表格'); button.id = 'scientific-button'; $('toc-button').before(button);
  const dialog = node('dialog'); dialog.id = 'scientific-dialog';
  dialog.innerHTML = '<div class="dialog-header"><h2>公式与表格</h2><button id="scientific-close" class="icon-button" aria-label="关闭公式表格">×</button></div><p class="muted">本地保守检测，可能漏项或误判。公式保留原式，不是 LaTeX 重建；提取表格的行列和合并单元格需核对原 PDF。扫描件仍需要 OCR。</p><button id="scientific-reanalyse" class="secondary">重新检测复杂内容</button><p id="scientific-summary" class="muted" role="status"></p><div id="scientific-list"></div>';
  document.body.append(dialog); $('scientific-close').onclick = () => dialog.close();
  const zoomDialog = node('dialog'); zoomDialog.id = 'scientific-zoom-dialog';
  zoomDialog.innerHTML = '<div class="dialog-header"><h2 id="scientific-zoom-title">原 PDF 区域</h2><button id="scientific-zoom-close" class="icon-button" aria-label="关闭原式原表放大">×</button></div><p class="muted">仅在本机渲染原 PDF，不发送图像给模型。放大后可横向或纵向滚动。</p><div class="scientific-zoom-controls"><button id="scientific-zoom-less" class="secondary" aria-label="缩小原式原表">−</button><span id="scientific-zoom-level" role="status"></span><button id="scientific-zoom-more" class="secondary" aria-label="放大原式原表">＋</button></div><div id="scientific-zoom-image" class="scientific-zoom-scroll"></div>';
  document.body.append(zoomDialog); $('scientific-zoom-close').onclick = () => zoomDialog.close();
  let zoomTarget = null, zoomScale = 1, zoomRevision = 0;
  let readingSession = null, readingCrops = [];
  function resetReadingCrops() {
    if (readingSession) { readingSession.stopped = true; readingSession.observer.disconnect(); readingSession.pending.clear(); }
    readingSession = null; readingCrops = [];
  }
  function observeReadingCrops() {
    const session = { stopped: false, active: 0, pending: new Set(), observer: null };
    async function pump() {
      if (session.stopped) return;
      while (session.active < 2 && session.pending.size) {
        const entry = session.pending.values().next().value; session.pending.delete(entry);
        if (!entry.details.isConnected || !entry.details.open || entry.rendered) continue;
        session.active++;
        entry.draw().finally(() => { session.active--; pump(); });
      }
    }
    session.observer = new IntersectionObserver(entries => {
      for (const item of entries) {
        const entry = readingCrops.find(candidate => candidate.details === item.target);
        if (!entry) continue;
        if (item.isIntersecting && entry.details.open && !entry.rendered) session.pending.add(entry);
        else session.pending.delete(entry);
      }
      pump();
    }, { root: $('reading-content'), rootMargin: '160px 0px' });
    readingSession = session;
    for (const entry of readingCrops) session.observer.observe(entry.details);
  }
  zoomDialog.addEventListener('close', () => { zoomRevision++; zoomTarget = null; $('scientific-zoom-image').replaceChildren(); });
  async function redrawZoom() {
    if (!zoomTarget || !zoomDialog.open) return;
    const target = zoomTarget, revision = ++zoomRevision;
    if (target.version !== state.loadVersion || target.id !== state.paper?.id) return zoomDialog.close();
    $('scientific-zoom-less').disabled = zoomScale <= 1; $('scientific-zoom-more').disabled = zoomScale >= 3;
    $('scientific-zoom-level').textContent = `${Math.round(zoomScale * 100)}% · 放大倍率，像素有上限`;
    const holder = node('div'); holder.style.minWidth = '100%'; $('scientific-zoom-image').replaceChildren(holder);
    try { await renderCrop(holder, target.region, target.page, zoomScale, true); }
    catch (error) { if (revision === zoomRevision && zoomDialog.open) holder.replaceChildren(node('p', 'quality-warning', error.message + '，请切换原文 PDF 查看')); }
  }
  $('scientific-zoom-less').onclick = () => { zoomScale = Math.max(1, zoomScale - .5); redrawZoom(); };
  $('scientific-zoom-more').onclick = () => { zoomScale = Math.min(3, zoomScale + .5); redrawZoom(); };
  function openZoom(region, page) {
    zoomTarget = { id: state.paper.id, version: state.loadVersion, region, page }; zoomScale = 1;
    $('scientific-zoom-title').textContent = `第 ${page + 1} 页 · ${region.kind === 'formula' ? '原式' : '原表'}`;
    zoomDialog.showModal(); redrawZoom();
  }
  button.onclick = async () => {
    if (!state.paper?.local_pdf_path) return toast('请先下载或导入 PDF');
    $('scientific-summary').textContent = '正在读取本地结构…'; $('scientific-list').replaceChildren(); dialog.showModal();
    try { const doc = await loadDocument(); if (doc && dialog.open) renderIndex(); } catch (error) { toast(error.message, true); }
  };
  $('scientific-reanalyse').onclick = async () => {
    const id = state.paper?.id, version = state.loadVersion; if (!id) return;
    $('scientific-reanalyse').disabled = true; $('scientific-summary').textContent = '正在本地重新检测，不调用模型…';
    try {
      const doc = await api(`/desktop/papers/${id}/scientific/reanalyse`, 'POST');
      if (state.paper?.id !== id || state.loadVersion !== version) return;
      state.doc = doc; await renderPage(); if (dialog.open) renderIndex(); toast('结构信息已更新，已有译文和手动整理保留');
    } catch (error) { toast(error.message, true); }
    finally { $('scientific-reanalyse').disabled = false; }
  };
  function renderIndex() {
    const id = state.paper.id, version = state.loadVersion, regions = state.doc.pages.flatMap(page => (page.scientific_regions || []).map(region => ({ page: page.number, region })));
    const formulas = regions.filter(item => item.region.kind === 'formula').length;
    $('scientific-summary').textContent = `公式候选 ${formulas} 处 · 表格 ${regions.length - formulas} 处。检测数量不代表覆盖率或正确率。`;
    $('scientific-list').replaceChildren();
    for (const { page, region } of regions) {
      const open = node('button', 'toc-entry', `第 ${page + 1} 页 · ${region.kind === 'table' ? '表格' : '公式候选'}${region.rows ? ` · ${region.rows.length} 行` : ''}`);
      open.onclick = async () => {
        if (state.paper?.id !== id || state.loadVersion !== version) return dialog.close();
        dialog.close(); await locateBlock(page, region);
        const details = document.querySelector(`[data-region-id="${region.id}"]`);
        if (details) { details.open = true; details.scrollIntoView({ block: 'nearest' }); }
      }; $('scientific-list').append(open);
    }
    if (!regions.length) $('scientific-list').append(node('p', 'muted', '没有识别到候选，不代表论文没有公式或表格。请在原文模式核对。'));
    for (const page of state.doc.pages) for (const text of page.scientific_diagnostics || []) $('scientific-list').append(node('p', 'quality-warning', `第 ${page.number + 1} 页：${text}`));
  }
  async function renderCrop(container, region, number, zoom = 1, enlarged = false) {
    const pdf = state.pdf, version = state.loadVersion;
    if (!pdf) throw new Error('原 PDF 尚未加载，请重新打开文献');
    const page = await pdf.getPage(number + 1), base = page.getViewport({ scale: 1 }), [x0, y0, x1, y1] = region.box;
    const width = (x1 - x0) * base.width, height = (y1 - y0) * base.height;
    if (width <= 0 || height <= 0) throw new Error('区域范围无效，请查看原页');
    const desired = Math.max(360, container.clientWidth) / width * zoom;
    const scale = Math.min(enlarged ? 8 : 4, desired, 1800 / width, 1200 / height);
    const viewport = page.getViewport({ scale });
    const canvas = node('canvas', 'scientific-crop'); canvas.width = Math.ceil(width * scale); canvas.height = Math.ceil(height * scale);
    if (enlarged) canvas.style.width = Math.ceil(width * desired) + 'px';
    canvas.setAttribute('aria-label', region.kind === 'page' ? '原 PDF 完整页面' : region.kind === 'formula' ? '原 PDF 公式区域' : '原 PDF 表格区域');
    await page.render({ canvasContext: canvas.getContext('2d'), viewport, transform: [1, 0, 0, 1, -x0 * viewport.width, -y0 * viewport.height], background: '#fff' }).promise;
    if (version !== state.loadVersion || !container.isConnected) return false;
    container.replaceChildren(canvas);
    return true;
  }
  function appendRegions(flow, page, regions = page.scientific_regions || [], { inline = false } = {}) {
    for (const region of regions) {
      const details = node('details', 'scientific-region'); details.dataset.regionId = region.id;
      details.classList.toggle('scientific-inline', inline);
      details.append(node('summary', '', region.kind === 'formula' ? '公式候选 · 查看原式' : `表格 · ${region.rows.length} 行 · 查看原表与单元格`));
      details.append(node('p', 'muted', region.note));
      for (const flag of region.quality_flags || []) details.append(node('p', 'quality-warning', flag));
      const crop = node('div', 'scientific-crop-holder'); crop.append(node('p', 'muted', inline ? '滚动到此处后在本机加载原图，不发送给模型' : '展开后在本机渲染原 PDF 区域')); details.append(crop);
      const [x0, y0, x1, y1] = region.box;
      if (inline && x1 > x0 && y1 > y0 && page.width && page.height) crop.style.aspectRatio = `${(x1 - x0) * page.width} / ${(y1 - y0) * page.height}`;
      const entry = { details, rendered: false, rendering: false, draw: null };
      entry.draw = async () => {
        if (!details.open || entry.rendered || entry.rendering || !details.isConnected) return;
        entry.rendering = true;
        try { entry.rendered = await renderCrop(crop, region, page.number); }
        catch (error) {
          if (crop.isConnected) {
            const retry = node('button', 'plain', '重新加载原图'); retry.onclick = entry.draw;
            crop.replaceChildren(node('p', 'quality-warning', error.message + '，也可定位原页核对'), retry);
          }
        }
        finally { entry.rendering = false; }
      };
      details.addEventListener('toggle', () => {
        if (!inline) entry.draw();
        else if (readingSession && details.isConnected) {
          readingSession.observer.unobserve(details); readingSession.observer.observe(details);
        }
      });
      if (inline) { details.open = true; readingCrops.push(entry); }
      const locate = node('button', 'citation-link', '定位原页区域'); locate.onclick = () => locateBlock(page.number, region); details.append(locate);
      const enlarge = node('button', 'plain', '放大查看原式 / 原表');
      const paperId = state.paper.id, paperVersion = state.loadVersion;
      enlarge.onclick = () => {
        if (state.paper?.id !== paperId || state.loadVersion !== paperVersion) return;
        openZoom(region, page.number);
      }; details.append(enlarge);
      if (region.kind === 'table') {
        const tableWrap = node('div', 'scientific-table-scroll'), table = node('table', 'scientific-table');
        table.append(node('caption', '', '提取单元格 · 结构需核对原表'));
        for (const [rowNumber, cells] of region.rows.entries()) {
          const row = node('tr');
          for (const [colNumber, original] of cells.entries()) {
            const cell = node('td');
            const translated = page.translated[`${region.id}-r${rowNumber}c${colNumber}`];
            cell.append(node('span', '', original || '（空白或合并）'));
            if (translated && translated !== original) cell.append(node('small', 'table-cell-zh', translated));
            row.append(cell);
          } table.append(row);
        }
        tableWrap.append(table);
        if (region.cell_translation_allowed === false) {
          const uncertain = node('details'); uncertain.append(node('summary', '', '查看结构不可靠的提取结果（请以原表为准）'), tableWrap); details.append(uncertain);
        } else details.append(tableWrap);
        const translate = node('button', 'plain', '补齐本页未译文字与单元格');
        translate.disabled = region.cell_translation_allowed === false;
        const id = state.paper.id, version = state.loadVersion;
        translate.onclick = () => {
          if (state.paper?.id !== id || state.loadVersion !== version) return;
          state.page = page.number; openTranslation('page', false);
        }; details.append(translate);
        details.append(node('p', 'muted', '只有文字单元格发给模型；纯数值留在本机。旧译文可能没有单元格译文，可确认补译本页；手动修改仍保留。'));
      } else {
        const extracted = node('details'); extracted.append(node('summary', '', '查看提取字符（可能缺上下标）'), node('p', 'scientific-raw', region.text)); details.append(extracted);
      }
      flow.append(details);
    }
  }
  return { appendRegions, renderCrop, openZoom, resetReadingCrops, observeReadingCrops };
}
