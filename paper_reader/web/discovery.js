export function installDiscovery({ state, $, node, api, toast, selectPaper }) {
  let briefPage = 1, requestVersion = 0, versionsRequest = 0;
  const offset = () => -new Date().getTimezoneOffset();
  const localDate = () => { const value = new Date(); return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`; };
  const safeLink = value => { try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : '#'; } catch { return '#'; } };
  const briefButton = node('button', 'nav-item', '每日论文简报'); briefButton.id = 'brief-button'; $('sync-button').after(briefButton);
  const versionsButton = node('button', 'plain', '来源 / 版本'); versionsButton.id = 'versions-button'; $('favorite-button').before(versionsButton);
  const briefDialog = node('dialog'); briefDialog.id = 'brief-dialog';
  briefDialog.innerHTML = '<div class="dialog-header"><div><p class="eyebrow">DAILY READING INBOX</p><h2>每日论文简报</h2></div><button id="brief-close" class="icon-button" aria-label="关闭每日简报">×</button></div><p class="muted">按首次进入文献库的日期整理，不是发表日期。重复采集不算新论文；不额外调用 AI。</p><div class="brief-controls"><label>入库日期<input id="brief-date" type="date" required></label><button id="brief-today" class="secondary">今天</button><a id="export-brief" class="plain" download>导出简报 ↗</a></div><p id="brief-summary" class="muted" role="status"></p><div id="brief-days" class="brief-days"></div><div id="brief-list"></div><div class="brief-controls"><button id="brief-prev" class="secondary">上一页</button><span id="brief-page" class="muted"></span><button id="brief-next" class="secondary">下一页</button></div>';
  const versionsDialog = node('dialog'); versionsDialog.id = 'versions-dialog';
  versionsDialog.innerHTML = '<div class="dialog-header"><h2>来源与阅读版本</h2><button id="versions-close" class="icon-button" aria-label="关闭来源版本">×</button></div><p id="versions-description" class="muted"></p><div id="versions-list"></div>';
  document.body.append(briefDialog, versionsDialog);
  $('brief-close').onclick = () => briefDialog.close(); $('versions-close').onclick = () => versionsDialog.close();
  briefButton.onclick = () => { $('brief-date').value = localDate(); briefPage = 1; briefDialog.showModal(); refreshBrief().catch(error => toast(error.message, true)); };
  $('brief-date').onchange = () => { briefPage = 1; refreshBrief().catch(error => toast(error.message, true)); };
  $('brief-today').onclick = () => { $('brief-date').value = localDate(); briefPage = 1; refreshBrief().catch(error => toast(error.message, true)); };
  $('brief-prev').onclick = () => { briefPage--; refreshBrief().catch(error => toast(error.message, true)); };
  $('brief-next').onclick = () => { briefPage++; refreshBrief().catch(error => toast(error.message, true)); };
  versionsButton.onclick = () => openVersions(state.paper.id).catch(error => toast(error.message, true));
  function params() { return new URLSearchParams({ date: $('brief-date').value, utc_offset_minutes: offset(), page: briefPage, page_size: 20 }); }
  async function refreshBrief() {
    if (!$('brief-date').value) throw new Error('请选择有效日期');
    const version = ++requestVersion, query = params();
    $('export-brief').removeAttribute('href'); $('brief-list').replaceChildren();
    $('brief-summary').textContent = '正在整理本机文献…'; $('brief-prev').disabled = true; $('brief-next').disabled = true;
    const data = await api('/desktop/discovery/brief?' + query);
    if (version !== requestVersion || !briefDialog.open) return;
    $('brief-summary').textContent = `新增 ${data.total} 篇文献 · 本地导入 ${data.local_imports} 篇 · 另有 ${data.new_source_versions} 条新增来源版本。`;
    $('brief-page').textContent = `${briefPage} / ${Math.max(1, Math.ceil(data.total / 20))}`;
    $('brief-prev').disabled = briefPage <= 1; $('brief-next').disabled = briefPage * 20 >= data.total;
    query.delete('page'); query.delete('page_size'); $('export-brief').href = '/desktop/discovery/brief.md?' + query;
    $('brief-days').replaceChildren();
    for (const day of data.days.slice(0, 12)) {
      const button = node('button', day.date === data.date ? 'selected' : '', `${day.date} · ${day.count}`);
      button.onclick = () => { $('brief-date').value = day.date; briefPage = 1; refreshBrief().catch(error => toast(error.message, true)); }; $('brief-days').append(button);
    }
    $('brief-list').replaceChildren();
    for (const paper of data.items) {
      const section = node('section', 'brief-paper'); section.dataset.id = paper.id;
      section.append(node('p', 'eyebrow', `${paper.sources.join(' / ')} · 入库 ${paper.collected_at.slice(0, 16).replace('T', ' ')}`), node('h3', '', paper.title));
      const reasons = node('p', 'match-reasons', paper.match_reasons.join('；')); section.append(reasons);
      const details = node('details'); details.append(node('summary', '', paper.brief_is_ai ? '已有 AI 摘要 · 请核对原文' : '来源摘要 · 非 AI 生成'), node('p', 'brief-abstract', paper.brief_text));
      const actions = node('div', 'brief-controls'), open = node('button', 'primary', '打开阅读');
      open.onclick = async () => { briefDialog.close(); await selectPaper(paper); }; actions.append(open);
      const versions = node('button', 'plain', `${paper.versions_count} 个来源版本`);
      versions.onclick = () => { briefDialog.close(); openVersions(paper.id).catch(error => toast(error.message, true)); }; actions.append(versions);
      if (paper.abs_url) { const source = node('a', 'plain', '查看来源 ↗'); source.href = safeLink(paper.abs_url); source.target = '_blank'; source.rel = 'noopener noreferrer'; actions.append(source); }
      section.append(details, actions); $('brief-list').append(section);
    }
    if (!data.total) $('brief-list').append(node('p', 'muted', '这一天没有首次入库的论文。可选择有记录的日期，或先获取新论文。'));
  }
  async function openVersions(id) {
    const version = ++versionsRequest;
    versionsDialog.showModal(); $('versions-list').replaceChildren(node('p', 'muted', '正在读取来源记录…'));
    $('versions-description').textContent = '';
    const data = await api(`/desktop/papers/${id}/versions`);
    if (!versionsDialog.open || version !== versionsRequest) return;
    $('versions-description').textContent = (data.normalized_doi ? 'DOI：' + data.normalized_doi + '\n' : '没有可归组的 DOI，不按相似标题猜测合并。\n') + data.note;
    $('versions-list').replaceChildren();
    for (const paper of data.items) {
      const section = node('section', 'version-card'); section.dataset.id = paper.id;
      section.append(node('p', 'eyebrow', `${paper.source} · ${paper.source_id}${state.paper?.id === paper.id ? ' · 正在阅读' : ''}`), node('h3', '', paper.title), node('p', 'muted', `${paper.local_pdf_path ? '已下载 PDF' : 'PDF 未下载'} · ${paper.annotations_count} 条笔记/标注 · ${paper.has_translation ? '已有译文' : '尚无译文'}${paper.has_research_card ? ' · 有研究卡片' : ''}`));
      const open = node('button', 'primary', '打开这个版本');
      open.onclick = async () => { versionsDialog.close(); await selectPaper(paper); }; section.append(open);
      if (paper.abs_url) { const link = node('a', 'plain', '查看来源 ↗'); link.href = safeLink(paper.abs_url); link.target = '_blank'; link.rel = 'noopener noreferrer'; section.append(link); }
      $('versions-list').append(section);
    }
  }
  async function loadVersions(id, version) {
    versionsButton.textContent = '来源 / 版本'; versionsButton.disabled = true;
    const data = await api(`/desktop/papers/${id}/versions`);
    if (state.paper?.id !== id || state.loadVersion !== version) return;
    state.paper.normalized_doi = data.normalized_doi;
    versionsButton.disabled = false; versionsButton.textContent = `来源 / 版本${data.items.length > 1 ? ' · ' + data.items.length : ''}`;
  }
  return { loadVersions };
}
