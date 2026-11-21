export function installLibraryTools({ state, $, node, api, toast, workspace, openNote }) {
  const notesButton = node('button', 'nav-item', '我的笔记');
  notesButton.id = 'my-notes-button';
  $('brief-button').after(notesButton);

  const notes = node('section', 'notes-workspace');
  notes.setAttribute('aria-label', '我的笔记');
  notes.innerHTML = `<header><h1>我的笔记</h1><button id="notes-batch-export" class="secondary" disabled>导出 Markdown</button></header>
    <form id="notes-filters" class="notes-filters">
      <label>论文标题<input id="notes-paper-filter" type="search" maxlength="200"></label>
      <label>笔记或摘录关键词<input id="notes-keyword" type="search" maxlength="200"></label>
      <label>类型<select id="notes-kind"><option value="">全部</option><option value="note">笔记</option><option value="text-highlight">文字高亮</option><option value="highlight">区域高亮</option><option value="ink">笔迹</option></select></label>
      <button type="submit" class="secondary">筛选</button>
    </form>
    <p id="notes-summary" class="muted" role="status"></p>
    <div id="library-notes-list"></div>
    <footer class="today-pagination"><button id="notes-prev" class="plain">上一页</button><span id="notes-page"></span><button id="notes-next" class="plain">下一页</button></footer>`;
  document.querySelector('.library').before(notes);

  const types = { note: '笔记', 'text-highlight': '文字高亮', highlight: '区域高亮', ink: '笔迹' };
  const active = () => state.workspaceView === 'notes';
  let notesPage = 1, request = 0, applied = new URLSearchParams();
  async function loadNotes() {
    const sequence = ++request;
    const query = new URLSearchParams(applied);
    query.set('page', notesPage);
    $('notes-prev').disabled = $('notes-next').disabled = $('notes-batch-export').disabled = true;
    $('library-notes-list').replaceChildren();
    $('notes-summary').textContent = '正在读取已保存的笔记…';
    try {
      const result = await api('/desktop/notes?' + query);
      if (!active() || sequence !== request) return;
      const pageCount = Math.max(1, Math.ceil(result.total / 30));
      if (notesPage > pageCount) { notesPage = pageCount; return loadNotes(); }
      $('notes-summary').textContent = result.total ? `找到 ${result.total} 条已保存的笔记与标注` : '没有符合条件的笔记。';
      $('notes-page').textContent = `${notesPage} / ${pageCount}`;
      $('notes-prev').disabled = notesPage === 1;
      $('notes-next').disabled = notesPage >= pageCount;
      $('notes-batch-export').disabled = !result.total;
      for (const note of result.items) {
        const row = node('article', 'library-note');
        const jump = node('button', 'plain', `${note.paper_title} · 第 ${note.page_number + 1} 页 ↗`);
        jump.onclick = async () => {
          jump.disabled = true;
          try {
            const paper = await api(`/papers/${note.paper_id}`);
            if (active() && sequence === request) await openNote(paper, note);
          } catch (error) { toast(error.message, true); }
          finally { jump.disabled = false; }
        };
        row.append(jump, node('small', 'muted', types[note.annotation_type] || '标注'),
          node('p', '', note.content || '原文页面上的图形标注'));
        $('library-notes-list').append(row);
      }
    } catch (error) {
      if (active() && sequence === request) $('notes-summary').textContent = error.message;
    }
  }
  notesButton.onclick = () => { if (workspace.notes()) loadNotes(); };
  $('notes-filters').onsubmit = event => {
    event.preventDefault();
    applied = new URLSearchParams();
    for (const [key, id] of [['paper', 'notes-paper-filter'], ['q', 'notes-keyword'], ['kind', 'notes-kind']]) {
      const value = $(id).value.trim();
      if (value) applied.set(key, value);
    }
    notesPage = 1;
    loadNotes();
  };
  $('notes-prev').onclick = () => { notesPage--; loadNotes(); };
  $('notes-next').onclick = () => { notesPage++; loadNotes(); };

  async function download(path, filename, payload) {
    const response = await fetch(path, payload ? {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Desktop-Token': state.token },
      body: JSON.stringify(payload),
    } : undefined);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(typeof error.detail === 'string' ? error.detail : '导出失败，请重试。');
    }
    const url = URL.createObjectURL(await response.blob());
    const link = node('a');
    link.href = url;
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  $('notes-batch-export').onclick = () => {
    download('/desktop/notes.md?' + applied, 'asteria-notes.md').catch(error => toast(error.message, true));
  };

  const exportPanel = node('div', 'citation-selection');
  exportPanel.innerHTML = `<div><button id="select-citations" class="secondary" aria-pressed="false">选择文献导出</button>
    <span id="citation-count" class="muted" role="status"></span></div>
    <div id="citation-batch-actions" hidden><button id="citation-select-page" class="plain">选择本页</button>
    <button id="citation-clear" class="plain">清空选择</button><button id="citation-batch-export" class="primary">导出所选</button></div>`;
  document.querySelector('.list-heading').before(exportPanel);
  const readerExport = node('button', 'plain', '导出引用');
  readerExport.id = 'citation-paper-export';
  $('favorite-button').before(readerExport);
  const dialog = node('dialog', 'citation-dialog');
  dialog.innerHTML = `<div class="dialog-header"><h2 id="citation-export-title">导出引用</h2>
    <button id="citation-close" class="icon-button" aria-label="关闭引用导出">×</button></div>
    <p id="citation-description" class="muted"></p><div class="dialog-actions">
    <button id="citation-bibtex" class="secondary">下载 BibTeX</button>
    <button id="citation-ris" class="primary">下载 RIS</button></div>`;
  dialog.setAttribute('aria-labelledby', 'citation-export-title');
  document.body.append(dialog);
  const selected = new Set();
  let selecting = false, exportIds = [];
  function updateSelection() {
    $('select-citations').setAttribute('aria-pressed', String(selecting));
    $('select-citations').textContent = selecting ? '结束选择' : '选择文献导出';
    $('citation-batch-actions').hidden = !selecting;
    $('citation-count').textContent = selecting ? `已选 ${selected.size} 篇` : '';
    $('citation-batch-export').disabled = !selected.size;
    $('citation-select-page').disabled = !$('paper-list').querySelector('.paper-card:not(:disabled)');
    for (const control of $('paper-list').querySelectorAll('.citation-check')) {
      control.hidden = !selecting;
      control.checked = selected.has(Number(control.value));
    }
  }
  function addPaper(paper, card) {
    const entry = node('div', 'paper-list-entry');
    const control = node('input', 'citation-check');
    control.type = 'checkbox';
    control.value = paper.id;
    control.setAttribute('aria-label', '选择导出 ' + paper.title);
    control.hidden = !selecting;
    control.checked = selected.has(paper.id);
    control.onchange = () => {
      if (card.disabled) { control.checked = selected.has(paper.id); return; }
      if (control.checked && selected.size === 200) toast('每次最多导出 200 篇。', true);
      else if (control.checked) selected.add(paper.id);
      else selected.delete(paper.id);
      updateSelection();
    };
    entry.append(control, card);
    return entry;
  }
  function showExport(ids) {
    exportIds = ids;
    $('citation-description').textContent = `导出 ${ids.length} 篇文献的来源信息。缺失字段留空。`;
    dialog.showModal();
  }
  $('select-citations').onclick = () => {
    selecting = !selecting;
    if (!selecting) selected.clear();
    updateSelection();
  };
  $('citation-select-page').onclick = () => {
    for (const card of $('paper-list').querySelectorAll('.paper-card:not(:disabled)')) {
      if (selected.size >= 200) { toast('已达到 200 篇的导出上限。'); break; }
      selected.add(Number(card.dataset.id));
    }
    updateSelection();
  };
  $('citation-clear').onclick = () => { selected.clear(); updateSelection(); };
  $('citation-batch-export').onclick = () => { if (selected.size) showExport([...selected]); };
  readerExport.onclick = () => { if (state.paper) showExport([state.paper.id]); };
  $('citation-close').onclick = () => dialog.close();
  for (const [fmt, extension] of [['bibtex', 'bib'], ['ris', 'ris']]) {
    $(`citation-${fmt}`).onclick = async () => {
      $('citation-bibtex').disabled = $('citation-ris').disabled = true;
      try {
        await download('/desktop/citations/export', 'asteria-citations.' + extension, { paper_ids: exportIds, format: fmt });
        dialog.close();
      } catch (error) { toast(error.message, true); }
      finally { $('citation-bibtex').disabled = $('citation-ris').disabled = false; }
    };
  }
  return { addPaper, updateSelection };
}
