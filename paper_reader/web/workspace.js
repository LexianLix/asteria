/** Separate finding papers from reading one; keep reader state and notes alive. */
export function dailyCollectionMessage(overview, today) {
  if (!overview?.daily_enabled) return '自动采集未开启，可在设置中选择研究方向与时间。';
  const schedule = `每天 ${overview.daily_time} · 应用运行时执行`;
  const daily = overview.daily_sync || {};
  if (daily.date !== today) return `${schedule} · 当天到点后，启动时会补跑尚未执行的采集。`;
  const labels = { running: '今天的自动采集正在进行', done: '今天的自动采集已完成',
    failed: '今天的自动采集失败，可查看任务中心并手动重试', paused: '上次自动采集已中断，可手动重新采集' };
  return `${schedule} · ${labels[daily.status] || '今天已尝试自动采集，请在任务中心查看结果'}`;
}

export function installWorkspace({ state, $, node, api, toast, selectPaper, renderPage, leaveReading }) {
  const shell = document.querySelector('.app-shell'); shell.dataset.view = 'library';
  const todayButton = node('button', 'nav-item', '今日论文'); todayButton.id = 'today-button'; todayButton.type = 'button';
  $('brief-button').before(todayButton);
  const back = node('button', 'plain', '文献列表 ↗'); back.id = 'back-library'; back.type = 'button';
  $('notes-toggle').after(back);
  const resume = node('button', 'workspace-resume'); resume.id = 'resume-reading'; resume.type = 'button'; resume.hidden = true;
  document.querySelector('.library-header').after(resume);
  const today = node('section', 'today-workspace'); today.id = 'today-workspace'; today.setAttribute('aria-label', '今日论文');
  today.innerHTML = '<header class="today-header"><div><p class="eyebrow">YOUR RESEARCH, AT YOUR PACE</p><h1>今日论文</h1><p class="muted">按进入本机文献库的日期，回看值得阅读的论文。</p></div><div class="today-date"><label>收录日期<input id="today-date" type="date" aria-label="今日论文收录日期"></label><button id="today-refresh" class="plain">刷新</button></div></header><div class="collection-status"><p id="daily-collection-status" role="status"></p><button id="today-settings" class="plain">采集设置 ↗</button></div><div class="today-list-heading"><h2>阅读清单</h2><span id="today-count" role="status"></span></div><div id="today-paper-list"></div><footer class="today-pagination"><button id="today-prev" class="plain">上一页</button><span id="today-page"></span><button id="today-next" class="plain">下一页</button></footer>';
  document.querySelector('.library').before(today);
  let generation = 0, page = 1;
  const localDate = () => { const now = new Date(); return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`; };
  $('today-date').value = localDate();
  function show(view) {
    if (state.workspaceView === 'reading' && view !== 'reading') {
      try { leaveReading(); } catch { toast('草稿无法保存，请先复制内容。', true); return false; }
    }
    state.workspaceView = view; state.focus = false; shell.classList.remove('focus-mode');
    $('focus-button').textContent = '专注'; $('focus-button').setAttribute('aria-pressed', 'false');
    shell.dataset.view = view; todayButton.classList.toggle('active', view === 'today');
    todayButton.setAttribute('aria-current', view === 'today' ? 'page' : 'false');
    return true;
  }
  function library() {
    generation++;
    if (show('library')) {
      document.querySelectorAll('[data-filter]').forEach(el => el.classList.toggle('active', el.dataset.filter === state.filter && !state.tag));
      updateResume();
    }
  }
  function reading() { show('reading'); }
  function updateResume() {
    const paper = state.paper || state.items.find(item => item.reader_state?.status === 'reading' && item.local_pdf_path);
    resume.hidden = !paper;
    if (!paper) return;
    resume.replaceChildren(node('span', '', '继续阅读'), node('strong', '', paper.title),
      node('small', '', `第 ${(paper.reader_state?.last_page || 0) + 1} 页 ↗`));
    resume.onclick = () => {
      if (paper.id === state.paper?.id) { reading(); renderPage().catch(error => toast(error.message, true)); }
      else selectPaper(paper);
    };
  }
  async function refreshToday() {
    if (state.workspaceView !== 'today') return;
    if (!$('today-date').value) {
      generation++; $('today-count').textContent = '请选择有效的收录日期。'; $('today-paper-list').replaceChildren();
      $('today-prev').disabled = $('today-next').disabled = true; $('today-page').textContent = ''; return;
    }
    const version = ++generation;
    $('today-count').textContent = '正在整理本机文献…'; $('today-prev').disabled = $('today-next').disabled = true;
    const params = new URLSearchParams({ date: $('today-date').value, utc_offset_minutes: -new Date().getTimezoneOffset(), page, page_size: 20 });
    try {
      const result = await api('/desktop/discovery/brief?' + params);
      if (version !== generation || state.workspaceView !== 'today') return;
      const lastPage = Math.max(1, Math.ceil(result.total / 20));
      if (page > lastPage) { page = lastPage; return refreshToday(); }
      $('today-count').textContent = `${result.total} 篇文献 · 本地导入 ${result.local_imports} 篇`;
      $('today-page').textContent = `${result.page} / ${Math.max(1, Math.ceil(result.total / 20))}`;
      $('today-prev').disabled = page <= 1; $('today-next').disabled = page * 20 >= result.total;
      const list = $('today-paper-list'); list.replaceChildren();
      for (const paper of result.items) {
        const card = node('article', 'today-paper');
        const title = node('button', 'today-paper-title', paper.title); title.type = 'button'; title.onclick = () => selectPaper(paper);
        card.append(node('p', 'today-paper-meta', `${paper.sources.join(' · ')} · ${paper.versions_count} 个来源版本`), title,
          node('p', 'today-paper-summary', `${paper.brief_is_ai ? 'AI 摘要 · ' : ''}${paper.brief_text}`),
          node('p', 'today-paper-reasons', paper.match_reasons.join(' · ')));
        const open = node('button', 'plain', paper.local_pdf_path ? '打开阅读 ↗' : '查看文献 ↗'); open.type = 'button'; open.onclick = () => selectPaper(paper); card.append(open);
        list.append(card);
      }
      if (!result.items.length) {
        const empty = node('div', 'today-empty'); empty.append(node('h2', '', '这一天还没有收录文献'),
          node('p', '', '可以选择其他日期，采集论文，或导入一份本地 PDF。'));
        const importButton = node('button', 'secondary', '导入 PDF'); importButton.onclick = () => $('import-file').click();
        const viewLibrary = node('button', 'plain', '查看文献库 ↗'); viewLibrary.onclick = library;
        empty.append(importButton, viewLibrary); list.append(empty);
      }
    } catch (error) {
      if (version === generation && state.workspaceView === 'today') { $('today-count').textContent = '文献清单暂时无法加载，请刷新重试。'; toast(error.message, true); }
    }
  }
  todayButton.onclick = () => { if (!show('today')) return; document.querySelectorAll('[data-filter]').forEach(el => el.classList.remove('active')); refreshToday(); };
  back.onclick = library;
  $('today-settings').onclick = () => $('settings-button').click();
  $('today-refresh').onclick = refreshToday;
  $('today-date').onchange = () => { page = 1; refreshToday(); };
  $('today-prev').onclick = () => { page--; refreshToday(); };
  $('today-next').onclick = () => { page++; refreshToday(); };
  function overview(value) { $('daily-collection-status').textContent = dailyCollectionMessage(value, localDate()); updateResume(); }
  return { library, reading, refreshToday, overview, updateResume };
}
