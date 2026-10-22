// The reader owns navigation; this module owns explicit-consent AI and card drafts.
export function installAssistant({ state, $, node, api, toast, loadDocument, locateBlock, renderPage }) {
  const names = { problem: '研究问题', method: '方法', result: '结果', limitations: '局限' };
  const drafts = new Map(), saves = new Map(), pending = new Set();
  let tab = 'notes', card = null, target = null, conflict = null;
  const keyFor = id => `paper-reader:research-draft:${id}`;
  const matches = (id, version) => state.paper?.id === id && state.loadVersion === version;
  const panel = $('notes-panel'), notes = node('section'); notes.id = 'note-content';
  $('notes-toggle').textContent = '笔记 / AI';
  document.querySelector('.welcome small').textContent = '文件和笔记保存在本机 · 正文翻译与问答需确认发送';
  notes.append(...panel.childNodes); panel.append(notes);
  const tabs = node('div', 'panel-tabs'); tabs.setAttribute('role', 'group'); tabs.setAttribute('aria-label', '阅读侧栏');
  for (const [value, label] of [['notes', '笔记'], ['assistant', '问答'], ['research', '研究卡片']]) {
    const button = node('button', '', label); button.dataset.panel = value; button.onclick = () => show(value); tabs.append(button);
  }
  panel.prepend(tabs);
  const questionPane = node('section'); questionPane.id = 'assistant-content'; questionPane.hidden = true;
  questionPane.innerHTML = '<h3>向这篇论文提问</h3><p class="muted">回答可能有误。出处存在不等于解释正确；只分析预览中的段落。</p><label class="card-label">关注范围<select id="assistant-focus"><option value="general">综合</option><option value="method">方法</option><option value="result">实验结果</option><option value="limitations">局限</option></select></label><label class="card-label" for="assistant-question">你的问题</label><textarea id="assistant-question" rows="3" maxlength="3000" placeholder="这篇论文的方法和实验结果是什么？"></textarea><button id="assistant-preview-button" class="primary">预览发送内容</button><p id="assistant-status" class="muted" role="status"></p><div id="assistant-history"></div>';
  const cardPane = node('section'); cardPane.id = 'research-content'; cardPane.hidden = true;
  cardPane.innerHTML = '<div class="panel-heading"><h3>这篇论文的研究卡片</h3><a id="export-card" class="plain" download>导出 ↗</a></div><p class="muted">AI 提取与手动整理分开保存。“我的想法”不会发送给模型。</p><button id="generate-card" class="secondary">预览并生成研究卡片</button><div id="research-generated"></div><h3>我的整理</h3>';
  for (const [name, label] of [...Object.entries(names), ['thoughts', '我的想法']]) {
    const labelNode = node('label', 'card-label', label); labelNode.htmlFor = `card-${name}`;
    const input = node('textarea'); input.id = `card-${name}`; input.rows = name === 'thoughts' ? 4 : 2;
    input.maxLength = name === 'thoughts' ? 18000 : 16000;
    input.oninput = () => { try { remember(); status('草稿已存本机；点击保存写入文献库'); } catch { status('本机草稿无法保存，请立即复制内容', true); } };
    cardPane.append(labelNode, input);
  }
  cardPane.append(Object.assign(node('p', 'muted'), { id: 'card-save-status' }), Object.assign(node('button', 'primary', '保存研究卡片'), { id: 'save-card' }), Object.assign(node('button', 'secondary', '核对冲突版本'), { id: 'resolve-card', hidden: true }));
  panel.append(questionPane, cardPane);
  const dialog = node('dialog'); dialog.id = 'assistant-confirm';
  dialog.innerHTML = '<div class="dialog-header"><h2>确认发送给模型的内容</h2><button id="assistant-confirm-close" class="icon-button" aria-label="关闭发送预览">×</button></div><p id="assistant-send-target" class="muted"></p><p id="assistant-send-question"></p><p id="assistant-send-scope" class="muted"></p><div id="assistant-context-list"></div><div class="dialog-actions"><button id="assistant-confirm-cancel" class="secondary">取消</button><button id="assistant-confirm-send" class="primary">确认发送</button></div>';
  const conflictDialog = node('dialog'); conflictDialog.id = 'card-conflict-dialog';
  conflictDialog.innerHTML = '<div class="dialog-header"><h2>核对两个研究卡片版本</h2><button id="card-conflict-close" class="icon-button" aria-label="关闭版本核对">×</button></div><p class="muted">不会自动覆盖。选择前请核对；保留本机版本会替换服务器上的手动整理，不替换 AI 内容。</p><h3>文献库中的版本</h3><pre id="conflict-remote"></pre><h3>本机草稿</h3><pre id="conflict-local"></pre><div class="dialog-actions"><button id="conflict-use-remote" class="secondary">采用文献库版本</button><button id="conflict-use-local" class="primary">保留本机版本并保存</button></div>';
  document.body.append(dialog, conflictDialog);
  for (const id of ['assistant-confirm-close', 'assistant-confirm-cancel']) $(id).onclick = () => dialog.close();
  $('card-conflict-close').onclick = () => conflictDialog.close();
  $('assistant-preview-button').onclick = () => preview('question').catch(error => toast(error.message, true));
  $('generate-card').onclick = () => preview('card').catch(error => toast(error.message, true));
  $('assistant-confirm-send').onclick = () => send().catch(error => toast(error.message, true));
  $('save-card').onclick = () => saveCurrent().catch(error => status(error.message, true));
  $('export-card').onclick = event => {
    try {
      remember();
      if (!card || readDraft(state.paper.id)) { event.preventDefault(); toast('请先保存研究卡片，再导出文献库中的版本'); }
    } catch { event.preventDefault(); toast('无法保存本机草稿，请先复制内容', true); }
  };
  $('resolve-card').onclick = () => resolve().catch(error => status(error.message, true));
  $('conflict-use-local').onclick = async () => {
    const value = conflict; if (!value || !matches(value.id, value.version)) return conflictDialog.close();
    const draft = { ...value.local, revision: value.remote.revision }; persist(value.id, draft); card = value.remote;
    conflictDialog.close(); try { await saveCurrent(); } catch (error) { status(error.message, true); }
  };
  $('conflict-use-remote').onclick = () => {
    const value = conflict; if (!value || !matches(value.id, value.version)) return conflictDialog.close();
    // An explicit decision, not automatic conflict resolution.
    drafts.delete(value.id); localStorage.removeItem(keyFor(value.id)); card = value.remote; fillCard(); conflictDialog.close(); status('已采用文献库版本');
  };
  function status(text, error = false) { $('card-save-status').textContent = text; $('card-save-status').classList.toggle('form-error', error); }
  function applyPanel() {
    notes.hidden = tab !== 'notes'; questionPane.hidden = tab !== 'assistant'; cardPane.hidden = tab !== 'research';
    panel.classList.toggle('ai-mode', tab !== 'notes');
    tabs.querySelectorAll('button').forEach(button => { button.classList.toggle('selected', button.dataset.panel === tab); button.setAttribute('aria-pressed', String(button.dataset.panel === tab)); });
  }
  function show(value) { tab = value; state.notesOpen = true; applyPanel(); renderPage().catch(error => toast(error.message, true)); }
  function readDraft(id) {
    if (drafts.has(id)) return drafts.get(id);
    const raw = localStorage.getItem(keyFor(id)); if (!raw) return null;
    const value = JSON.parse(raw); drafts.set(id, value); return value;
  }
  function persist(id, draft) { localStorage.setItem(keyFor(id), JSON.stringify(draft)); drafts.set(id, draft); }
  function formValue() { return { fields: Object.fromEntries(Object.keys(names).map(name => [name, $(`card-${name}`).value])), thoughts: $('card-thoughts').value }; }
  function remember() {
    if (!card || !state.paper || state.restoring) return;
    const id = state.paper.id, old = readDraft(id), value = formValue();
    const server = { fields: Object.fromEntries(Object.keys(names).map(name => [name, card.fields?.[name] || ''])), thoughts: card.thoughts || '' };
    if (!old && JSON.stringify(server) === JSON.stringify(value)) return;
    persist(id, { revision: old?.revision ?? card.revision, ...value });
  }
  function fillCard() {
    const draft = readDraft(state.paper.id), value = draft || card;
    for (const name of Object.keys(names)) $(`card-${name}`).value = value?.fields?.[name] || '';
    $('card-thoughts').value = value?.thoughts || '';
    const stale = !!draft && draft.revision !== card.revision;
    $('resolve-card').hidden = !stale;
    status(stale ? '文献库版本有变化；本机草稿保留，请核对后保存' : draft ? '存在本机未提交草稿' : '内容已保存到文献库', stale);
    renderGenerated();
  }
  function renderGenerated() {
    const container = $('research-generated'); container.replaceChildren();
    const result = card?.generated;
    if (!result) { container.append(node('p', 'muted', '尚未生成 AI 卡片。也可以直接手动整理，不调用模型。')); return; }
    container.append(node('p', 'muted', `${result.model} · ${result.partial ? '局部段落分析' : '提取段落分析'} · 不含完整图表`));
    for (const [name, label] of Object.entries(names)) { container.append(node('h3', '', label)); renderClaims(container, result.fields?.[name] || [], result); }
  }
  function renderClaims(container, claims, result) {
    const id = state.paper.id, version = state.loadVersion;
    for (const claim of claims) {
      const group = node('section', 'assistant-claim');
      group.append(node('small', `claim-kind ${claim.kind}`, { fact: '论文事实 · AI 提取待核对', inference: '模型解释 · 不是作者原话', unknown: '当前上下文未找到' }[claim.kind]), node('p', '', claim.text));
      for (const cite of claim.sources || []) {
        const button = node('button', 'citation-link', `查看来源 · 第 ${cite.page + 1} 页`);
        button.onclick = async () => {
          try {
            const source = await api(`/desktop/papers/${id}/assistant/source/${encodeURIComponent(cite.id)}?source_hash=${result.source_hash}`);
            if (!matches(id, version)) return;
            await loadDocument(); if (!matches(id, version)) return;
            state.notesOpen = false; await locateBlock(source.page, source.block);
          } catch (error) { toast(error.message, true); }
        };
        group.append(button, node('blockquote', 'source-quote', cite.quote));
      }
      container.append(group);
    }
  }
  function renderDiscussion(history) {
    const container = $('assistant-history'); container.replaceChildren();
    for (const result of [...history.items].reverse().filter(item => item.kind !== 'card')) {
      const section = node('section', 'discussion-entry'); section.append(node('h3', '', result.question), node('p', 'muted', `${result.model} · ${result.partial ? '局部上下文' : '提取正文'} · ${result.created_at.slice(0, 16)}`));
      renderClaims(section, result.claims || [], result); container.append(section);
    }
    if (!container.childNodes.length) container.append(node('p', 'muted', '还没有问答。预览不会调用模型，确认发送后才生成。'));
  }
  async function load(id, version) {
    card = null; $('assistant-history').replaceChildren(); $('research-generated').replaceChildren();
    $('export-card').removeAttribute('href'); $('resolve-card').hidden = true; status('正在加载研究卡片…');
    $('assistant-question').value = ''; $('assistant-status').textContent = ''; $('save-card').disabled = true;
    for (const name of [...Object.keys(names), 'thoughts']) $(`card-${name}`).value = '';
    for (const name of [...Object.keys(names), 'thoughts']) $(`card-${name}`).disabled = true;
    const [history, value] = await Promise.all([api(`/desktop/papers/${id}/discussion`), api(`/desktop/papers/${id}/research-card`)]);
    if (!matches(id, version)) return;
    card = value; fillCard(); renderDiscussion(history); $('save-card').disabled = false;
    for (const name of [...Object.keys(names), 'thoughts']) $(`card-${name}`).disabled = false;
    $('export-card').href = `/desktop/papers/${id}/research-card.md`;
  }
  async function saveOne(id) {
    if (saves.has(id)) return saves.get(id);
    const draft = readDraft(id); if (!draft) return;
    const snapshot = JSON.stringify(draft);
    const task = (async () => {
      const result = await api(`/desktop/papers/${id}/research-card`, 'PATCH', draft);
      const latest = readDraft(id);
      if (JSON.stringify(latest) === snapshot) { drafts.delete(id); localStorage.removeItem(keyFor(id)); }
      else if (latest) persist(id, { ...latest, revision: result.revision });
      if (state.paper?.id === id) { card = result; if (!readDraft(id)) status('研究卡片已保存'); else status('旧输入已保存；新输入仍为草稿'); }
      return result;
    })();
    saves.set(id, task); try { return await task; } finally { saves.delete(id); }
  }
  async function saveCurrent() {
    if (state.restoring || !card) return;
    remember(); const id = state.paper.id;
    try { await saveOne(id); $('resolve-card').hidden = true; }
    catch (error) { if (state.paper?.id === id) $('resolve-card').hidden = !error.message.includes('其他窗口'); throw error; }
  }
  async function resolve() {
    const id = state.paper.id, version = state.loadVersion; remember();
    const remote = await api(`/desktop/papers/${id}/research-card`);
    if (!matches(id, version)) return;
    remember(); const local = readDraft(id); if (!local) return;
    conflict = { id, version, local, remote };
    const printable = value => Object.entries(names).map(([name, label]) => `${label}\n${value.fields?.[name] || '（空）'}`).join('\n\n') + '\n\n我的想法\n' + (value.thoughts || '（空）');
    $('conflict-remote').textContent = printable(remote); $('conflict-local').textContent = printable(local); conflictDialog.showModal();
  }
  async function preview(kind, anchor = null) {
    const id = state.paper?.id, version = state.loadVersion;
    if (!state.paper?.local_pdf_path) throw new Error('请先下载或导入 PDF');
    if (pending.has(id)) throw new Error('这篇论文已有请求正在执行');
    const question = kind === 'card' ? '提取这篇论文的研究问题、方法、结果与局限' : kind === 'explain' ? '解释选中段落，区分作者陈述与模型推断' : $('assistant-question').value.trim();
    if (!question) throw new Error('请先填写问题');
    const request = { kind, question, focus: $('assistant-focus').value, ...(anchor ? { block_id: anchor.block_id, quote: anchor.quote, language: anchor.language } : {}) };
    const value = await api(`/desktop/papers/${id}/assistant/preview`, 'POST', request);
    if (!matches(id, version)) return;
    target = { id, version, request: { ...request, preview_id: value.preview_id, request_id: crypto.randomUUID() } };
    $('assistant-send-target').textContent = `服务：${value.base_url}\n模型：${value.model}\n正文 ${value.characters} 字符 / ${value.included_blocks} 段。远程服务可能计费。不发送笔记或“我的想法”。`;
    $('assistant-send-question').textContent = `问题：${question}${anchor ? '\n选段：' + anchor.quote : ''}`;
    $('assistant-send-scope').textContent = `${value.partial ? '仅部分段落，不是全文分析。' : '包含已提取的正文段落。'}涉及第 ${value.pages.join('、')} 页。${value.scope_note}`;
    $('assistant-context-list').replaceChildren();
    for (const source of value.sources) { const details = node('details'); details.append(node('summary', '', `第 ${source.page + 1} 页 · ${source.id}`), node('p', 'source-quote', source.en)); $('assistant-context-list').append(details); }
    dialog.showModal();
  }
  async function send() {
    const value = target; if (!value || !matches(value.id, value.version)) throw new Error('论文已切换，请重新预览');
    if (pending.has(value.id)) return;
    pending.add(value.id); dialog.close(); show(value.request.kind === 'card' ? 'research' : 'assistant');
    $('assistant-status').textContent = '正在生成；请等待当前请求返回';
    if (value.request.kind === 'card') status('正在生成 AI 卡片；手动整理仍可编辑');
    try {
      await api(`/desktop/papers/${value.id}/assistant`, 'POST', value.request);
      if (!matches(value.id, value.version)) return;
      remember(); const [history, updated] = await Promise.all([api(`/desktop/papers/${value.id}/discussion`), api(`/desktop/papers/${value.id}/research-card`)]);
      if (!matches(value.id, value.version)) return;
      // Inputs are left untouched: typing during the request is never replaced.
      card = updated; renderGenerated(); renderDiscussion(history); $('assistant-status').textContent = '回答已保存；请核对原文';
      if (value.request.kind === 'card') status(readDraft(value.id) ? 'AI 卡片已保存；手动整理仍为本机草稿' : 'AI 卡片已保存；个人整理保持不变');
      toast('AI 内容已保存，个人整理保持不变');
    } catch (error) { if (matches(value.id, value.version)) { $('assistant-status').textContent = error.message; if (value.request.kind === 'card') status(error.message, true); } throw error; }
    finally { pending.delete(value.id); }
  }
  async function flush() {
    remember();
    for (const key of Object.keys(localStorage)) { const match = /^paper-reader:research-draft:(\d+)$/.exec(key); if (match) await saveOne(Number(match[1])); }
    if ([...drafts.values()].length) throw new Error('研究卡片仍有未保存输入，请完成保存后再备份');
  }
  function clearAfterRestore() { drafts.clear(); card = null; for (const key of Object.keys(localStorage)) if (/^paper-reader:research-draft:\d+$/.test(key)) localStorage.removeItem(key); }
  const explain = node('button', '', '解释选段'); explain.id = 'explain-selection'; $('translate-selection').after(explain);
  explain.addEventListener('mousedown', event => event.preventDefault());
  applyPanel();
  return { load, remember, flush, clearAfterRestore, explain: anchor => preview('explain', anchor), show };
}
