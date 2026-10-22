// Source review is an explicit page-level transaction, not a silent OCR replacement.
export function installReviewed({ state, $, node, api, toast, renderCrop, renderPage, renderNotes }) {
  const launch = node('button', 'secondary', '核对正文 / 切换版本'); launch.id = 'reading-review-button';
  $('complex-status').before(launch);
  const dialog = node('dialog'); dialog.id = 'reading-review-dialog';
  dialog.innerHTML = `<div class="dialog-header"><h2>核对正文 · 保留阅读版本</h2><button id="reading-review-close" class="icon-button" aria-label="关闭正文核对">×</button></div>
    <p class="muted">对照原页，调整顺序、改字并排除误识别。采用只影响所选页，不删除旧正文、译文、手动修改或笔记；有变化的段落不沿用旧译文。</p>
    <div class="reading-review-controls"><label>核对页码<input id="reading-review-page" type="number" min="1"></label><button id="reading-review-load" class="secondary">读取所选页</button></div>
    <p id="reading-review-status" class="muted" role="status" aria-live="polite"></p>
    <div class="reading-review-grid"><section class="reading-review-original"><h3 id="reading-review-source-title">原 PDF</h3><div id="reading-review-crop"></div></section>
    <section class="reading-review-editor"><h3>正文顺序与内容</h3><div class="reading-review-controls"><button id="reading-review-current" class="plain">载入当前正文</button><button id="reading-review-extracted" class="plain">载入增强候选</button><button id="reading-review-upgrade" class="plain" hidden>载入新版文字提取</button><button id="reading-review-add" class="plain">补充遗漏段落</button></div><div id="reading-review-blocks"></div></section></div>
    <div class="reading-review-footer"><label class="check-label"><input id="reading-review-confirm" type="checkbox"> 我已核对原页、顺序、遗漏和科学符号，并确认采用所保留的段落</label>
    <button id="reading-review-adopt" class="primary" disabled>采用核对正文</button><p class="muted">表格和公式仍以原 PDF 为准；OCR 不保证数学语义正确。省略的段落不会进入当前正文，但旧版本仍保留。</p>
    <label>保留的正文版本<select id="reading-review-versions"></select></label><button id="reading-review-switch" class="secondary" disabled>切换所选版本</button></div>`;
  document.body.append(dialog);
  let target = null, review = null, rows = [], sequence = 0, saving = false, edited = false, candidateSource = 'enhanced', candidateRevision = null;
  const live = () => dialog.open && target?.id === state.paper?.id && target?.version === state.loadVersion;
  const dirty = () => { edited = true; $('reading-review-confirm').checked = false; updateActions(); };
  const discardAllowed = () => !edited || confirm('核对修改尚未采用。放弃这些修改？已保存的正文和笔记不会受影响。');
  function updateActions() {
    const retained = rows.filter(row => row.included);
    const correctPage = review && Number($('reading-review-page').value) === review.page + 1;
    $('reading-review-adopt').disabled = saving || !correctPage || !candidateRevision || !retained.length || retained.length > 500
      || retained.some(row => !row.text.trim() || row.text.length > 10000) || retained.reduce((n, row) => n + row.text.length, 0) > 100000 || !$('reading-review-confirm').checked;
    $('reading-review-switch').disabled = saving || !correctPage || !$('reading-review-versions').value || $('reading-review-versions').value === review.active_version;
    for (const id of ['reading-review-load', 'reading-review-current', 'reading-review-extracted', 'reading-review-upgrade', 'reading-review-add', 'reading-review-page', 'reading-review-versions']) $(id).disabled = saving;
    $('reading-review-confirm').disabled = saving;
    $('reading-review-blocks').querySelectorAll('input,textarea,button').forEach(element => {
      if (saving) { element.dataset.wasDisabled = String(element.disabled); element.disabled = true; }
      else if (element.dataset.wasDisabled !== undefined) { element.disabled = element.dataset.wasDisabled === 'true'; delete element.dataset.wasDisabled; }
    });
  }
  function loadRows(candidates) {
    rows = candidates.map(c => ({ candidate_id: c.id, text: c.text, included: true, label: c.label, box: c.box, estimated: c.estimated_box, scriptRestoration: c.numeric_script_restoration }));
    dirty(); renderRows();
  }
  function renderRows(focus = null) {
    const container = $('reading-review-blocks'); container.replaceChildren();
    rows.forEach((row, index) => {
      const item = node('section', 'reading-review-block'); item.dataset.index = index;
      const controls = node('div', 'reading-review-row');
      const label = node('label', 'check-label'), include = node('input'); include.type = 'checkbox'; include.checked = row.included;
      include.onchange = () => { row.included = include.checked; dirty(); }; label.append(include, document.createTextNode(`段落 ${index + 1} · ${row.label || '手动补充'}`)); controls.append(label);
      for (const [offset, title] of [[-1, '上移'], [1, '下移']]) {
        const button = node('button', 'plain', title); button.disabled = index + offset < 0 || index + offset >= rows.length;
        button.setAttribute('aria-label', `段落 ${index+1}${title}`);
        button.onclick = () => { [rows[index], rows[index+offset]] = [rows[index+offset], rows[index]]; dirty(); renderRows(index + offset); }; controls.append(button);
      }
      const input = node('textarea'); input.value = row.text; input.rows = Math.min(6, Math.max(2, Math.ceil(row.text.length / 70))); input.maxLength = 10000;
      input.setAttribute('aria-label', `段落 ${index+1}正文`); input.oninput = () => { row.text = input.value; dirty(); };
      input.onfocus = () => highlight(row.box);
      item.append(controls, input);
      if (row.scriptRestoration) {
        const evidence = node('details', 'scientific-raw');
        evidence.append(node('summary', '', '数字上下标核对记录 · 仍需对照原页'),
          node('p', '', `原识别：${row.scriptRestoration.original_text}`),
          node('p', '', `按原 PDF 字形位置恢复的候选：${row.scriptRestoration.restored_text}`));
        item.append(evidence);
      }
      if (row.estimated) item.append(node('small', 'muted', '来源区域为估算；手动补充段落定位整页。'));
      container.append(item);
    });
    if (!rows.length) container.append(node('p', 'muted', '此页没有可采用的文字。先启用英文 OCR 解析，或对照原页补充段落。'));
    if (focus !== null) container.children[focus]?.querySelector('textarea')?.focus();
  }
  function highlight(box) {
    const holder = $('reading-review-crop'); holder.querySelector('.reading-review-box')?.remove();
    if (!box || !live()) return;
    const overlay = node('div', 'reading-review-box'); overlay.style.left = box[0]*100 + '%'; overlay.style.top = box[1]*100 + '%';
    overlay.style.width = (box[2]-box[0])*100 + '%'; overlay.style.height = (box[3]-box[1])*100 + '%'; holder.append(overlay);
  }
  async function load() {
    if (!live() || saving) return;
    if (!discardAllowed()) return;
    edited = false;
    const current = target, request = ++sequence, page = Number($('reading-review-page').value) - 1;
    if (!Number.isInteger(page) || page < 0 || page >= state.total) return toast('请选择 PDF 内的页码', true);
    review = null; candidateRevision = null; rows = []; updateActions(); $('reading-review-confirm').checked = false;
    $('reading-review-status').textContent = '正在读取正文版本与候选…'; $('reading-review-blocks').replaceChildren(); $('reading-review-crop').replaceChildren();
    try {
      const data = await api(`/desktop/papers/${current.id}/reading-source/${page}`);
      if (!live() || target !== current || request !== sequence) return;
      review = data; $('reading-review-status').textContent = data.warning + (data.preview_revision ? '' : ' 此页没有可用增强预览；可切换已保存版本。');
      candidateSource = data.candidate_source || 'enhanced'; candidateRevision = data.preview_revision;
      $('reading-review-extracted').textContent = data.candidate_source === 'extraction' ? '载入新版文字提取' : '载入增强候选';
      $('reading-review-upgrade').hidden = data.candidate_source === 'extraction' || !data.extraction_candidates?.length;
      $('reading-review-source-title').textContent = `原 PDF · 第 ${page+1} 页`;
      const versions = $('reading-review-versions'); versions.replaceChildren();
      for (const version of data.versions) { const option = node('option', '', version.label + (version.active ? '（当前）' : '')); option.value = version.id; versions.append(option); }
      if (data.versions.length) versions.value = data.active_version;
      loadRows(data.active_version !== 'original' ? data.current_blocks : data.candidates);
      edited = false;
      const crop = node('div'); $('reading-review-crop').replaceChildren(crop);
      await renderCrop(crop, { box: [0, 0, 1, 1], kind: 'page' }, page);
    } catch (error) { if (live() && request === sequence) $('reading-review-status').textContent = error.message + '。未改变当前正文，请重新读取。'; }
  }
  launch.onclick = () => {
    $('complex-dialog').close(); target = { id: state.paper.id, version: state.loadVersion };
    $('reading-review-page').value = state.page + 1; $('reading-review-page').max = state.total; dialog.showModal(); load();
  };
  $('reading-review-close').onclick = () => { if (!saving && discardAllowed()) dialog.close(); };
  dialog.addEventListener('cancel', event => { if (saving || !discardAllowed()) event.preventDefault(); });
  dialog.addEventListener('close', () => { sequence++; target = null; review = null; rows = []; edited = false; });
  $('reading-review-load').onclick = load;
  $('reading-review-page').oninput = updateActions;
  $('reading-review-current').onclick = () => { if (review && discardAllowed()) loadRows(review.current_blocks); };
  $('reading-review-extracted').onclick = () => { if (review && discardAllowed()) {
    candidateSource = review.candidate_source || 'enhanced'; candidateRevision = review.preview_revision; loadRows(review.candidates);
  } };
  $('reading-review-upgrade').onclick = () => { if (review && discardAllowed()) {
    candidateSource = 'extraction'; candidateRevision = review.extraction_revision; loadRows(review.extraction_candidates);
  } };
  $('reading-review-add').onclick = () => { if (!review) return; rows.push({ candidate_id: null, text: '', included: true, estimated: true }); dirty(); renderRows(rows.length - 1); };
  $('reading-review-confirm').onchange = updateActions; $('reading-review-versions').onchange = updateActions;
  async function commit(action, payload) {
    if (!live() || !review || saving) return;
    const current = target, page = review.page; saving = true; updateActions(); $('reading-review-close').disabled = true;
    try {
      const result = await api(`/desktop/papers/${current.id}/reading-source/${page}/${action}`, 'POST', {
        source_hash: review.source_hash, document_token: review.document_token, ...payload });
      if (!live() || target !== current) return;
      state.doc = result.document; state.editor = await api(`/desktop/papers/${current.id}/editor`);
      if (!live() || target !== current) return;
      state.page = page; state.focusBlock = null; state.mode = 'text'; renderNotes(); await renderPage();
      toast(action === 'adopt' ? '核对正文已采用；旧版本和笔记已保留' : '正文版本已切换；笔记仍保留');
      saving = false; dialog.close();
    } catch (error) { if (live()) $('reading-review-status').textContent = error.message + '。请重新读取当前版本；本地修改仍留在此页。'; }
    finally { saving = false; $('reading-review-close').disabled = false; updateActions(); }
  }
  $('reading-review-adopt').onclick = () => {
    if ($('reading-review-adopt').disabled) return;
    commit('adopt', { preview_revision: candidateRevision, candidate_source: candidateSource, confirmed: $('reading-review-confirm').checked,
      blocks: rows.filter(row => row.included).map(row => ({ candidate_id: row.candidate_id, text: row.text })) });
  };
  $('reading-review-switch').onclick = () => {
    if ($('reading-review-switch').disabled || !confirm('切换所选页的正文版本？当前译文会随版本保留，笔记和手动修改不会删除。')) return;
    commit('switch', { version: $('reading-review-versions').value });
  };
  return { open: () => launch.click() };
}
