// Review-only local parsing. Never replace the saved reader document silently.
export function installComplex({ state, $, node, api, toast, locateBlock, renderPage, renderCrop, openZoom }) {
  const launch = node('button', 'secondary', '增强解析与 OCR…'); launch.id = 'complex-button';
  $('scientific-reanalyse').after(launch);
  const dialog = node('dialog'); dialog.id = 'complex-dialog';
  dialog.innerHTML = `<div class="dialog-header"><h2>增强解析 · 原页核对</h2><div class="complex-header-actions"><button id="complex-fallback" class="secondary">继续原文阅读</button><button id="complex-close" class="icon-button" aria-label="关闭增强解析">×</button></div></div>
    <p class="muted">本地 CPU 模型，独立预览，不覆盖已保存正文、译文和笔记。合并表头、阅读顺序与科学符号仍需核对原 PDF。成功重跑会更新本预览，失败保留上次结果。</p>
    <p id="complex-runtime" class="muted"></p>
    <div class="complex-controls"><label>起始页<input id="complex-start-page" type="number" min="1" value="1"></label><label>结束页<input id="complex-end-page" type="number" min="1" value="1"></label>
      <label class="check-label"><input id="complex-ocr" type="checkbox"> 英文扫描 OCR</label><button id="complex-start" class="primary" disabled>解析所选页</button><button id="complex-cancel" class="secondary" hidden>取消解析</button></div>
    <p id="complex-status" role="status" aria-live="polite"></p><div id="complex-result"></div>`;
  document.body.append(dialog);
  const consent = node('dialog'); consent.id = 'complex-confirm';
  consent.innerHTML = '<div class="dialog-header"><h2>启动本地增强解析</h2><button id="complex-confirm-close" class="icon-button" aria-label="关闭解析确认">×</button></div><p id="complex-confirm-description"></p><div class="dialog-actions"><button id="complex-confirm-start" class="primary">开始本地解析</button></div>';
  document.body.append(consent);
  let target = null, runtime = null, timer = null, preview = null, selectedTable = null, renderedRevision = null, runtimeSequence = 0, pollSequence = 0;
  const live = () => target && target.id === state.paper?.id && target.version === state.loadVersion;
  const busy = job => ['queued', 'running', 'cancelling'].includes(job.status);
  const validRange = () => Number.isInteger(Number($('complex-start-page').value)) && Number.isInteger(Number($('complex-end-page').value))
    && Number($('complex-start-page').value) >= 1 && Number($('complex-end-page').value) >= Number($('complex-start-page').value)
    && Number($('complex-end-page').value) <= state.total && Number($('complex-end-page').value) - Number($('complex-start-page').value) < 100;
  function stopPoll() { clearTimeout(timer); timer = null; }
  dialog.addEventListener('close', () => { stopPoll(); target = null; preview = null; renderedRevision = null; if (consent.open) consent.close(); });
  $('complex-close').onclick = () => dialog.close(); $('complex-confirm-close').onclick = () => consent.close();
  launch.onclick = () => { $('scientific-dialog').close(); open(); };
  $('complex-fallback').onclick = async () => { dialog.close(); state.mode = 'pdf'; await renderPage(); };
  $('complex-ocr').onchange = async () => {
    if (!live()) return;
    try { await readRuntime(); } catch (error) { if (live()) { runtime = null; $('complex-start').disabled = true; $('complex-status').textContent = error.message + '，请重新打开解析窗口。'; } }
  };
  for (const id of ['complex-start-page', 'complex-end-page']) $(id).oninput = () => { $('complex-start').disabled = !runtime?.available || !validRange() || !$('complex-cancel').hidden; };
  async function readRuntime() {
    const current = target, sequence = ++runtimeSequence;
    const result = await api('/desktop/complex/runtime?ocr=' + $('complex-ocr').checked);
    if (!live() || target !== current || sequence !== runtimeSequence) return;
    runtime = result; $('complex-runtime').textContent = result.reason || `${result.warning} · 后端 ${result.backend} · 依赖是否完整以实际启动为准`;
    $('complex-start').disabled = !result.available || !validRange() || !$('complex-cancel').hidden;
  }
  async function open() {
    if (dialog.open) return;
    if (!state.paper?.local_pdf_path) return toast('请先下载或导入 PDF');
    target = { id: state.paper.id, version: state.loadVersion }; runtime = null; preview = null; selectedTable = null; renderedRevision = null;
    $('complex-start-page').value = state.page + 1; $('complex-end-page').value = state.page + 1;
    for (const id of ['complex-start-page', 'complex-end-page']) $(id).max = state.total;
    $('complex-ocr').checked = false; $('complex-cancel').hidden = true; $('complex-start').disabled = true;
    $('complex-status').textContent = '正在读取本地解析状态…'; $('complex-result').replaceChildren();
    dialog.showModal();
    try { await readRuntime(); await poll(); } catch (error) { if (live()) $('complex-status').textContent = error.message + '。可以重新解析，或继续原文阅读。'; }
  }
  $('complex-start').onclick = () => {
    if (!live() || !runtime?.available || !validRange()) return;
    $('complex-confirm-description').textContent = `解析第 ${$('complex-start-page').value}–${$('complex-end-page').value} 页${$('complex-ocr').checked ? '，启用英文 OCR（PDFium 渲染，仍需核对原页）' : ''}。仅使用本机独立环境和已有模型，不上传 PDF、不调用翻译接口、不下载模型。可能占用约 1.5 GB 内存。成功后更新增强预览；现有正文、译文与笔记不变。`;
    consent.showModal();
  };
  $('complex-confirm-start').onclick = async () => {
    if (!live() || !validRange()) return consent.close();
    const current = target; $('complex-confirm-start').disabled = true;
    try {
      await api(`/desktop/papers/${current.id}/complex`, 'POST', { start_page: Number($('complex-start-page').value), end_page: Number($('complex-end-page').value), ocr: $('complex-ocr').checked });
      consent.close(); if (live() && target === current) await poll();
    } catch (error) { toast(error.message, true); }
    finally { $('complex-confirm-start').disabled = false; }
  };
  $('complex-cancel').onclick = async () => {
    if (!live()) return;
    const current = target; $('complex-cancel').disabled = true;
    try { await api(`/desktop/papers/${current.id}/complex/cancel`, 'POST'); if (live() && target === current) await poll(); }
    catch (error) { toast(error.message, true); }
    finally { $('complex-cancel').disabled = false; }
  };
  async function poll() {
    stopPoll(); if (!live() || !dialog.open) return;
    const current = target, sequence = ++pollSequence; const data = await api(`/desktop/papers/${current.id}/complex`);
    if (!live() || current !== target || !dialog.open || sequence !== pollSequence) return;
    const job = data.job, running = busy(job);
    $('complex-cancel').hidden = !running; $('complex-cancel').disabled = job.status === 'cancelling';
    $('complex-start').disabled = running || !runtime?.available || !validRange();
    const labels = { idle: '尚未解析', queued: '准备本地模型', running: '本地解析中（暂无页级进度）', cancelling: '正在停止独立解析进程', cancelled: '已取消，原内容保留', failed: '解析失败，原内容保留', paused: '上次解析中断，可重新解析', done: '增强预览已就绪' };
    $('complex-status').textContent = `${labels[job.status] || job.status}${job.error ? ' · ' + job.error : ''}`;
    if (data.preview?.source_hash) {
      preview = data.preview;
      const revision = preview.source_hash + ':' + preview.revision;
      if (revision !== renderedRevision) { renderedRevision = revision; showPreview(); }
    } else if (!running) $('complex-result').replaceChildren(node('p', 'muted', '还没有增强结果。未配置环境时仍可正常阅读原 PDF。'));
    if (running) timer = setTimeout(() => poll().catch(error => { if (live()) $('complex-status').textContent = error.message; }), 800);
  }
  function showPreview() {
    const result = $('complex-result'); result.replaceChildren();
    result.append(node('p', 'muted', `独立预览 · 第 ${preview.requested_pages.join('–')} 页 · ${preview.parser.ocr ? '英文 OCR' : '文字 PDF'} · 后端 ${preview.parser.backend || '未记录'} · 不是已确认正文或译文`));
    if (preview.tables.length) {
      const label = node('label', 'complex-table-label', '核对区域'); const select = node('select'); select.id = 'complex-table-select';
      for (const table of preview.tables) {
        const option = node('option', '', `第 ${table.provenance[0].page + 1} 页 · ${table.caption?.text.slice(0, 70) || '无表注候选'} · ${table.row_count} 行`); option.value = table.id; select.append(option);
      }
      if (!preview.tables.some(table => table.id === selectedTable)) selectedTable = preview.tables[0].id;
      select.value = selectedTable; select.onchange = () => { selectedTable = select.value; showTable(); };
      label.append(select); result.append(label, node('div', 'complex-review')); result.lastChild.id = 'complex-review'; showTable();
    }
    const textDetails = node('details', 'complex-texts'); textDetails.append(node('summary', '', '提取文字片段（顺序与科学符号未确认）'));
    for (const text of preview.texts) {
      const p = node('p', 'scientific-raw', text.text);
      if (text.provenance.length) p.prepend(node('small', 'muted', `第 ${text.provenance[0].page + 1} 页 · `));
      textDetails.append(p);
    } result.append(textDetails);
    const links = node('div', 'complex-links'); result.append(links);
    for (const link of preview.table_links) {
      const section = node('section', 'complex-link'); section.append(node('p', '', `第 ${link.from_page} 页 → 第 ${link.to_page} 页 · ${link.confirmed ? '已人工确认续表关系' : '待核对续表关系'}`), node('p', 'muted', link.evidence));
      for (const [id, title] of [[link.from_id, '查看前页表格'], [link.to_id, '查看后页表格']]) {
        const jump = node('button', 'plain', title); jump.onclick = () => { selectedTable = id; $('complex-table-select').value = id; showTable(); }; section.append(jump);
      }
      const confirm = node('button', 'secondary', link.confirmed ? '撤销续表确认' : '已核对两页，确认续表'); confirm.className += ' complex-link-confirm';
      confirm.onclick = async () => {
        if (!live()) return; const current = target; confirm.disabled = true;
        try {
          const updated = await api(`/desktop/papers/${current.id}/complex/table-link`, 'POST', { source_hash: preview.source_hash, revision: preview.revision, from_id: link.from_id, to_id: link.to_id, confirmed: !link.confirmed });
          if (!live() || current !== target) return;
          preview = updated; renderedRevision = updated.source_hash + ':' + updated.revision; showPreview();
        } catch (error) { toast(error.message, true); }
        finally { confirm.disabled = false; }
      }; section.append(confirm, node('p', 'muted', '只确认续表关系，不保证每个单元格正确；原始两页分片和坐标仍保留。')); links.append(section);
    }
  }
  function showTable() {
    const table = preview.tables.find(table => table.id === selectedTable), holder = $('complex-review'); if (!table || !holder) return;
    holder.replaceChildren(); const source = node('section', 'complex-source'), structure = node('section', 'complex-structure'); holder.append(source, structure);
    const prov = table.provenance[0]; source.append(node('h3', '', `原 PDF · 第 ${prov.page + 1} 页`));
    const crop = node('div', 'scientific-crop-holder'); source.append(crop);
    renderCrop(crop, { box: prov.box, kind: 'table' }, prov.page).catch(error => { if (crop.isConnected) crop.replaceChildren(node('p', 'quality-warning', error.message)); });
    const locate = node('button', 'citation-link', '定位原页区域'); locate.onclick = async () => { dialog.close(); await locateBlock(prov.page, { id: table.id, box: prov.box }); }; source.append(locate);
    const enlarge = node('button', 'plain', '放大核对原表'); enlarge.onclick = () => { if (!live()) return; dialog.close(); openZoom({ box: prov.box, kind: 'table' }, prov.page); }; source.append(enlarge);
    structure.append(node('h3', '', '提取结构 · 需要核对'), node('p', 'quality-warning', table.math_warning));
    for (const flag of table.quality_flags) structure.append(node('p', 'quality-warning', flag));
    const scroll = node('div', 'scientific-table-scroll'), grid = node('table', 'scientific-table'); grid.append(node('caption', '', '保留行列跨度的独立结构预览'));
    const origins = new Map(), covered = new Set();
    for (const cell of table.cells) {
      origins.set(cell.row * table.columns + cell.column, cell);
      for (let r = cell.row; r < cell.row + cell.row_span; r++) for (let c = cell.column; c < cell.column + cell.column_span; c++) {
        if (r !== cell.row || c !== cell.column) covered.add(r * table.columns + c);
      }
    }
    let missing = 0;
    for (let rowIndex = 0; rowIndex < Math.min(table.row_count, 200); rowIndex++) {
      const row = node('tr');
      for (let column = 0; column < table.columns; column++) {
        const position = rowIndex * table.columns + column;
        if (covered.has(position)) continue;
        const cell = origins.get(position);
        const td = node(cell?.column_header || cell?.row_header ? 'th' : 'td', '', cell ? cell.text || '（空白）' : '');
        td.dataset.row = rowIndex; td.dataset.column = column;
        if (cell) {
          td.colSpan = cell.column_span; td.rowSpan = cell.row_span;
          if (cell.script_restoration) {
            td.dataset.sourceScripts = 'true';
            td.title = `原始识别：${cell.original_text}\n数字上下标按原 PDF 字符位置恢复，仍需核对原页。`;
          }
        }
        else { missing++; td.dataset.missing = 'true'; td.title = '解析未提供这个位置的单元格，请核对原表'; }
        row.append(td);
      } grid.append(row);
    } scroll.append(grid); structure.append(scroll);
    const restored = table.cells.filter(cell => cell.script_restoration);
    if (restored.length) {
      const evidence = node('details', 'complex-script-evidence');
      evidence.append(node('summary', '', `查看 ${restored.length} 个单元格的恢复依据与原始识别`));
      for (const cell of restored) {
        evidence.append(node('p', 'scientific-raw', `第 ${cell.row + 1} 行 / 第 ${cell.column + 1} 列：${cell.original_text} → ${cell.text}\n原 PDF 字符：${cell.script_restoration.source_scripted_text}`));
      }
      structure.append(evidence);
    }
    if (missing) structure.append(node('p', 'quality-warning', '部分位置没有解析结果，已保留空位，不移动邻列；请核对原表的空白或合并关系。'));
    if (table.row_count > 200) structure.append(node('p', 'quality-warning', '仅预览前 200 行，请缩小页码范围或核对原 PDF。'));
    if (table.prose_column_candidates) {
      const alternative = node('details'); alternative.append(node('summary', '', '按列排列的待核对正文（不是已确认阅读顺序）'));
      table.prose_column_candidates.forEach((cells, column) => { alternative.append(node('h4', '', `第 ${column + 1} 列候选`), node('p', 'scientific-raw', cells.map(cell => cell.text).join('\n'))); }); structure.append(alternative);
    }
  }
  return { open };
}
