import * as pdfjs from '/assets/vendor/pdfjs/pdf.mjs';
import { installAssistant } from '/assets/assistant.js';
import { installDiscovery } from '/assets/discovery.js';
import { installScientific } from '/assets/scientific.js';
import { installComplex } from '/assets/complex.js';
import { installReviewed } from '/assets/reviewed.js';
import { createPdfFlow } from '/assets/pdf-flow.js';
import { regionPlacement, entirelyScientific } from '/assets/scientific-flow.js';
import { installChatGPT } from '/assets/chatgpt.js';
import { sourceSyncControl } from '/assets/source-status.js';
import { installClassification } from '/assets/classification.js';
import { installTranslationStatus, translationJobState, translationPageMessage } from '/assets/translation-status.js';
import { installReadingQuality } from '/assets/reading-quality.js';
import { installWorkspace } from '/assets/workspace.js';
pdfjs.GlobalWorkerOptions.workerSrc = '/assets/vendor/pdfjs/pdf.worker.mjs';

const $ = (id) => document.getElementById(id);
const state = { token: '', config: {}, items: [], paper: null, pdf: null, doc: null, annotations: [], page: 0,
  total: 0, mode: 'pdf', tool: 'read', zoom: 1, font: 18, filter: 'all', tag: '', listPage: 1, listTotal: 0,
  loadVersion: 0, renderVersion: 0, previewVersion: 0, readerStateReady: false, notesOpen: false, workspaceView: 'library', job: {}, jobDone: -1, syncStatus: '', busy: false,
  editor: {}, continuous: false, pdfContinuous: false, pdfOffset: 0, focus: false, focusBlock: null, translationTarget: null, snippet: null, editTarget: null, restoring: false };
const isChinese = () => ['zh', 'compare'].includes(state.mode);
const outboxKey = 'paper-reader:annotation-outbox:v1';
let outbox = [], flushing = false, inFlight = null, toastTimer, searchTimer, stroke = null;
const readingPositions = new Map();
let positionTimer, positionFlight = null;
const pdfFlowHost = document.createElement('div'); pdfFlowHost.id = 'pdf-flow'; pdfFlowHost.hidden = true;
$('page-stage').after(pdfFlowHost);
const pdfFlowLabel = node('label', 'check-label pdf-flow-toggle');
const pdfFlowInput = node('input'); pdfFlowInput.id = 'pdf-continuous'; pdfFlowInput.type = 'checkbox';
pdfFlowLabel.append(pdfFlowInput, document.createTextNode('连续滚动')); $('pdf-tools').append(pdfFlowLabel);
const pdfFlow = createPdfFlow({ host: pdfFlowHost, scroller: $('reading-content'), paint: drawAnnotations,
  bindInk: bindInkLayer, drawingPage: () => stroke?.page,
  onError: error => toast(error.message, true),
  onPosition: pos => {
    if (state.workspaceView !== 'reading' || state.mode !== 'pdf' || !state.pdfContinuous || !state.paper) return;
    state.page = pos.page; state.pdfOffset = pos.offset; updateControls(); queueReadingPosition();
  } });
try { outbox = JSON.parse(localStorage.getItem(outboxKey) || '[]'); } catch { toast('本地待保存队列无法读取，请先备份浏览器数据', true); }

function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text !== undefined) el.textContent = text;
  return el;
}
function toast(message, error = false) {
  $('toast').textContent = message; $('toast').hidden = false; $('toast').classList.toggle('error', error);
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 6500 : 3500);
}
function messageFrom(payload) {
  const detail = payload?.detail;
  return typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(x => x.msg).join('；') : detail?.message || '请求失败';
}
async function api(url, method = 'GET', body = null) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 180000);
  try {
    const headers = { 'X-Desktop-Token': state.token };
    if (body && !(body instanceof FormData)) headers['Content-Type'] = 'application/json';
    const response = await fetch(url, { method, headers, body: body instanceof FormData ? body : body ? JSON.stringify(body) : null, signal: controller.signal });
    if (!response.ok) {
      let data; try { data = await response.json(); } catch { data = {}; }
      throw new Error(messageFrom(data));
    }
    return response.status === 204 ? null : response.json();
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时；请稍后重试');
    throw error;
  } finally { clearTimeout(timer); }
}
function safeLink(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) ? url.href : '#'; } catch { return '#'; }
}
function date(value) { return value ? value.slice(0, 10).replaceAll('-', '.') : '本地导入'; }
function setStatus(value) { $('reader-status').textContent = value; }
function persistOutbox() { localStorage.setItem(outboxKey, JSON.stringify(outbox)); }
function pendingFor(id) { return outbox.filter(x => x.paper_id === id).map(x => ({ ...x.body, id: 'pending-' + x.body.client_id, pending: true })); }
function allAnnotations() { return state.paper ? [...state.annotations, ...pendingFor(state.paper.id)] : []; }

function queueReadingPosition() {
  if (!state.paper || !state.readerStateReady || state.workspaceView !== 'reading' || state.restoring) return;
  const payload = { last_page: state.page };
  if (state.mode === 'pdf') Object.assign(payload, { pdf_offset: state.pdfOffset, pdf_zoom: state.zoom, pdf_continuous: state.pdfContinuous });
  else { state.pdfOffset = 0; payload.pdf_offset = 0; }
  state.paper.reader_state = { ...state.paper.reader_state, ...payload };
  if (state.paper.reader_state.status !== 'done') state.paper.reader_state.status = 'reading';
  readingPositions.set(state.paper.id, payload);
  clearTimeout(positionTimer); positionTimer = setTimeout(() => flushReadingPositions().catch(() => {}), 180);
}

async function flushReadingPositions() {
  if (positionFlight) return positionFlight;
  clearTimeout(positionTimer);
  positionFlight = (async () => {
    while (readingPositions.size) {
      const [id, payload] = readingPositions.entries().next().value;
      await api(`/desktop/papers/${id}/state`, 'PATCH', payload);
      if (readingPositions.get(id) === payload) readingPositions.delete(id);
    }
  })();
  try { await positionFlight; } finally { positionFlight = null; }
}

async function refreshOverview() {
  const value = await api('/desktop/overview');
  $('count-all').textContent = value.total; $('count-starred').textContent = value.starred; $('count-downloaded').textContent = value.downloaded;
  const selectedFolder = $('folder-filter').value, folderItems = value.folders || [];
  if (selectedFolder && !folderItems.includes(selectedFolder)) folderItems.push(selectedFolder);
  $('folder-filter').replaceChildren(node('option', '', '所有文件夹')); $('folder-filter').firstChild.value = '';
  $('folder-options').replaceChildren();
  for (const folder of folderItems) {
    const option = node('option', '', folder); option.value = folder; $('folder-filter').append(option);
    const suggestion = node('option'); suggestion.value = folder; $('folder-options').append(suggestion);
  }
  $('folder-filter').value = selectedFolder;
  $('tags').replaceChildren();
  for (const [tag, count] of Object.entries(value.tags).sort((a, b) => b[1] - a[1])) {
    const button = node('button', state.tag === tag ? 'active' : '');
    button.append(node('span', '', tag), node('span', 'count', count));
    button.onclick = () => setFilter('all', tag);
    $('tags').append(button);
  }
  if (!Object.keys(value.tags).length) $('tags').append(node('p', 'muted', '采集或导入后显示分类'));
  const job = value.sync_job;
  const syncControl = sourceSyncControl(job, value.sync_cooldown);
  $('sync-button').disabled = syncControl.disabled;
  $('sync-button').textContent = syncControl.label;
  $('sync-button').title = syncControl.title;
  if (state.syncStatus === 'running' && job.status !== 'running') {
    if (job.status === 'failed') toast('采集失败：' + job.error, true);
    else toast('采集完成，文献库已刷新');
    await refreshLibrary();
  }
  state.syncStatus = job.status;
  if (state.classificationStatus === 'running' && value.classification_job?.status !== 'running') await refreshLibrary();
  state.classificationStatus = value.classification_job?.status;
  workspace.overview(value);
  $('connection').lastChild.textContent = job.status === 'running' ? ' 正在采集论文' : ' 本地存储';
  return value;
}
let libraryRequest = 0;
async function refreshLibrary() {
  const request = ++libraryRequest;
  $('paper-list').setAttribute('aria-busy', 'true');
  $('paper-list').querySelectorAll('.paper-card').forEach(button => { button.disabled = true; });
  const params = new URLSearchParams({ page: state.listPage, page_size: 40 });
  if ($('search').value.trim()) params.set('q', $('search').value.trim());
  if (state.tag) params.set('tag', state.tag);
  if (state.filter === 'starred') params.set('starred', 'true');
  if (state.filter === 'downloaded') params.set('downloaded', 'true');
  if ($('folder-filter').value) params.set('folder', $('folder-filter').value);
  if ($('status-filter').value) params.set('status', $('status-filter').value);
  let result;
  try { result = await api('/desktop/papers?' + params); }
  catch (error) {
    if (request === libraryRequest) {
      $('paper-list').setAttribute('aria-busy', 'false'); $('list-count').textContent = '文献清单加载失败';
      $('paper-list').replaceChildren(node('p', 'empty-list', '无法加载文献清单，请点击刷新重试。'));
    }
    throw error;
  }
  if (request !== libraryRequest) return;
  $('paper-list').setAttribute('aria-busy', 'false');
  state.items = result.items; state.listTotal = result.total;
  $('list-count').textContent = `${result.total} 篇文献`;
  $('list-page').textContent = `${state.listPage} / ${Math.max(1, Math.ceil(result.total / 40))}`;
  $('list-prev').disabled = state.listPage <= 1; $('list-next').disabled = state.listPage * 40 >= result.total;
  $('paper-list').replaceChildren();
  for (const paper of result.items) {
    const selected = state.paper?.id === paper.id || paper.normalized_doi && paper.normalized_doi === state.paper?.normalized_doi;
    const button = node('button', 'paper-card' + (selected ? ' selected' : ''));
    button.dataset.doi = paper.normalized_doi || '';
    button.setAttribute('role', 'listitem'); button.setAttribute('aria-label', paper.title); button.dataset.id = paper.id;
    const meta = node('div', 'paper-meta'); meta.append(node('span', 'source-badge', paper.source === 'local' ? '本地 PDF' : paper.source), node('span', '', date(paper.published_at)));
    const title = node('h2', '', paper.title); const authors = node('p', '', (paper.authors || []).join(' · ') || '作者信息未提供');
    const tags = node('div', 'paper-tags');
    if (paper.versions_count > 1) tags.append(node('span', 'paper-tag', `${paper.versions_count} 个来源版本`));
    for (const tag of [...new Set([...(paper.reader_state?.tags || []), ...(paper.ai_tags || [])])].slice(0, 3)) tags.append(node('span', 'paper-tag', tag));
    if (paper.reader_state?.folder) tags.append(node('span', 'paper-tag', '▱ ' + paper.reader_state.folder));
    tags.append(node('span', 'paper-tag', { unread: '未读', reading: '在读', done: '读完' }[paper.reader_state?.status || 'unread']));
    if (paper.local_pdf_path) tags.append(node('span', 'download-indicator', '↓ 已下载'));
    if (paper.reader_state?.starred) tags.append(node('span', 'download-indicator', '★'));
    button.append(meta, title, authors, tags);
    const summary = paper.ai_summary_zh || paper.abstract;
    if (summary) button.append(node('p', 'paper-abstract', `${paper.ai_summary_zh ? 'AI 摘要 · ' : ''}${Array.from(summary).slice(0, 220).join('')}${Array.from(summary).length > 220 ? '…' : ''}`));
    if (paper.search_hit) button.append(node('p', 'search-excerpt', `第 ${paper.search_hit.page + 1} 页 · ${paper.search_hit.excerpt}`));
    button.onclick = async () => {
      await selectPaper(paper);
      if (paper.search_hit && state.workspaceView === 'reading' && state.paper?.id === paper.id && state.pdf) {
        state.mode = paper.search_hit.language === 'zh' ? 'zh' : 'text'; state.page = paper.search_hit.page;
        try {
          await renderPage();
          if (paper.search_hit.region_id) {
            const region = $('text-page').querySelector(`[data-region-id="${paper.search_hit.region_id}"]`);
            if (region) { region.open = true; region.scrollIntoView({ block: 'nearest' }); }
          } else {
            const target = $('text-page').querySelector(`[data-block-id="${paper.search_hit.block_id}"]`);
            if (target?.parentElement.classList.contains('scientific-text-fallback')) target.parentElement.open = true;
            target?.scrollIntoView({ block: 'nearest' });
          }
        }
        catch (error) { toast(error.message, true); }
      }
    };
    $('paper-list').append(button);
  }
  if (!result.items.length) $('paper-list').append(node('p', 'empty-list', $('search').value || state.tag || state.filter !== 'all' ? '没有匹配的文献。\n试试其他关键词或筛选条件。' : '文献库还是空的。\n点击“获取新论文”，或导入本地 PDF。'));
  workspace.updateResume();
}
async function setFilter(filter, tag = '') {
  workspace.library();
  state.filter = filter; state.tag = tag; state.listPage = 1;
  $('library-title').textContent = tag || ({ all: '全部文献', starred: '我的收藏', downloaded: '已下载' })[filter];
  document.querySelectorAll('[data-filter]').forEach(el => el.classList.toggle('active', el.dataset.filter === filter && !tag));
  try { await Promise.all([refreshLibrary(), refreshOverview()]); } catch (error) { toast(error.message, true); }
}
function rememberDraft() {
  if (state.paper && !state.restoring) localStorage.setItem(`paper-reader:draft:${state.paper.id}`, $('note-input').value);
}
async function selectPaper(paper) {
  try { rememberDraft(); assistant.remember(); } catch (error) { toast('无法保存草稿，请先复制内容', true); return; }
  workspace.reading();
  const version = ++state.loadVersion;
  pdfFlow.dispose(); endStroke(true);
  state.renderVersion++;
  const oldPdf = state.pdf;
  state.pdf = null; state.doc = null; state.annotations = []; state.paper = paper; state.editor = {}; state.focusBlock = null; state.previewVersion++;
  state.readerStateReady = false;
  state.page = Math.max(0, paper.reader_state?.last_page || 0); state.total = 0; state.jobDone = -1; state.job = {};
  state.mode = 'pdf'; state.tool = 'read'; state.zoom = 1; state.pdfContinuous = false; state.pdfOffset = 0;
  $('welcome').hidden = true; $('reader-workspace').hidden = false;
  $('reader-title').textContent = paper.title; $('reader-title').title = paper.title;
  $('reader-authors').textContent = (paper.authors || []).join(' · ') || '作者信息未提供';
  $('reader-source').textContent = `${paper.source === 'local' ? 'LOCAL PDF' : paper.source.toUpperCase()} · ${date(paper.published_at)}`;
  $('export-notes').href = `/desktop/papers/${paper.id}/notes.md`;
  $('favorite-button').classList.toggle('starred', !!paper.reader_state?.starred); $('favorite-button').textContent = paper.reader_state?.starred ? '★' : '☆';
  $('note-input').value = localStorage.getItem(`paper-reader:draft:${paper.id}`) || '';
  document.querySelectorAll('.paper-card').forEach(el => el.classList.toggle('selected', Number(el.dataset.id) === paper.id || !!paper.normalized_doi && el.dataset.doi === paper.normalized_doi));
  $('page-stage').hidden = true; $('text-page').hidden = true; $('paper-overview').hidden = false;
  $('job-banner').hidden = true; setStatus('正在打开…'); updateControls(); renderOverview(); renderNotes();
  if (oldPdf) oldPdf.loadingTask.destroy().catch(() => {});
  try {
    const [notes, editor, readerState] = await Promise.all([api(`/papers/${paper.id}/annotations`), api(`/desktop/papers/${paper.id}/editor`), api(`/desktop/papers/${paper.id}/state`), assistant.load(paper.id, version), discovery.loadVersions(paper.id, version)]);
    if (version !== state.loadVersion) return;
    state.annotations = notes; state.editor = editor; paper.reader_state = { ...readerState, ...readingPositions.get(paper.id) };
    state.readerStateReady = true;
    state.page = Math.max(0, paper.reader_state.last_page || 0);
    state.pdfContinuous = paper.reader_state.pdf_continuous === true;
    state.pdfOffset = Number.isFinite(paper.reader_state.pdf_offset) ? Math.max(0, Math.min(1, paper.reader_state.pdf_offset)) : 0;
    state.zoom = Number.isFinite(paper.reader_state.pdf_zoom) ? Math.max(.5, Math.min(2.5, paper.reader_state.pdf_zoom)) : 1; renderNotes();
    document.querySelectorAll('.paper-card').forEach(el => el.classList.toggle('selected', Number(el.dataset.id) === paper.id || !!paper.normalized_doi && el.dataset.doi === paper.normalized_doi));
    $('favorite-button').classList.toggle('starred', !!readerState.starred); $('favorite-button').textContent = readerState.starred ? '★' : '☆';
    if (paper.local_pdf_path) await loadPdf(version);
    else { updateControls(); updateJob(); setStatus(paper.pdf_url ? 'PDF 尚未下载' : '暂无开放 PDF，可导入本地文件'); }
    const job = await api(`/desktop/papers/${paper.id}/translation`);
    if (version === state.loadVersion) { state.job = job; updateJob(); }
  } catch (error) { if (version === state.loadVersion) { setStatus(error.message); toast(error.message, true); } }
}
function renderOverview() {
  const paper = state.paper; const el = $('paper-overview'); el.replaceChildren();
  el.append(node('p', 'eyebrow', 'PAPER OVERVIEW'), node('h2', '', paper.title));
  const actions = node('div', 'overview-actions');
  const open = node('button', 'primary', paper.local_pdf_path ? '打开原文 PDF' : paper.pdf_url ? '下载并阅读 PDF' : '导入本地 PDF');
  open.onclick = () => paper.local_pdf_path ? loadPdf(state.loadVersion).catch(e => toast(e.message, true)) : paper.pdf_url ? downloadPaper() : $('import-file').click();
  actions.append(open);
  if (paper.abs_url) { const link = node('a', 'secondary plain', '查看来源 ↗'); link.href = safeLink(paper.abs_url); link.target = '_blank'; link.rel = 'noopener noreferrer'; actions.append(link); }
  el.append(actions, node('p', 'eyebrow', paper.ai_summary_zh ? 'AI 摘要 · 请核对原文' : 'ABSTRACT'), node('p', 'abstract', paper.ai_summary_zh || paper.abstract || '本地导入的 PDF 尚无摘要。打开原文或文字阅读即可查看正文。'));
  el.append(node('p', 'overview-meta', `分类：${(paper.categories || []).join(' · ') || '未提供'}\n${paper.license ? '许可：' + paper.license : '下载仅使用来源提供的开放 PDF 地址'}`));
}
async function loadPdf(version) {
  const id = state.paper.id;
  setStatus('正在加载 PDF…');
  const loading = pdfjs.getDocument({ url: `/papers/${id}/pdf`, cMapUrl: '/assets/vendor/pdfjs/cmaps/', cMapPacked: true,
    standardFontDataUrl: '/assets/vendor/pdfjs/standard_fonts/', wasmUrl: '/assets/vendor/pdfjs/wasm/' });
  const pdf = await loading.promise;
  if (version !== state.loadVersion) { await loading.destroy(); return; }
  state.pdf = pdf; state.total = pdf.numPages; state.page = Math.min(state.page, state.total - 1);
  $('paper-overview').hidden = true; updateControls();
  await renderPage();
}
async function downloadPaper() {
  const paper = state.paper, version = state.loadVersion;
  const button = $('paper-overview').querySelector('button');
  if (button) { button.disabled = true; button.textContent = '正在下载 PDF…'; }
  setStatus('正在下载开放 PDF…');
  try {
    const result = await api(`/papers/${paper.id}/download`, 'POST'); paper.local_pdf_path = result.local_pdf_path;
    await Promise.all([refreshOverview(), refreshLibrary()]);
    if (version === state.loadVersion) await loadPdf(version);
  } catch (error) { if (version === state.loadVersion) { renderOverview(); setStatus(error.message); } toast(error.message, true); }
}
function updateControls() {
  document.querySelectorAll('[data-mode]').forEach(el => el.classList.toggle('selected', el.dataset.mode === state.mode));
  document.querySelectorAll('[data-tool]').forEach(el => el.classList.toggle('selected', el.dataset.tool === state.tool));
  $('pdf-tools').hidden = state.mode !== 'pdf'; $('text-tools').hidden = state.mode === 'pdf';
  $('bilingual').closest('label').hidden = !isChinese();
  $('source-preview').hidden = state.mode !== 'compare';
  $('reading-content').classList.toggle('compare', state.mode === 'compare');
  $('continuous').checked = state.continuous;
  pdfFlowInput.checked = state.pdfContinuous; pdfFlow.setTool(state.tool);
  pdfFlowInput.disabled = !state.pdf;
  document.querySelector('.app-shell').classList.toggle('focus-mode', state.focus);
  $('focus-button').textContent = state.focus ? '退出专注' : '专注'; $('focus-button').setAttribute('aria-pressed', String(state.focus));
  $('page-input').value = state.page + 1; $('page-input').max = state.total || 1;
  $('page-total').textContent = state.total || '—'; $('page-prev').disabled = !state.readerStateReady || state.page <= 0; $('page-next').disabled = !state.readerStateReady || !state.total || state.page + 1 >= state.total;
  $('page-input').disabled = !state.readerStateReady || !state.total;
  $('save-note').disabled = $('translate-page').disabled = !state.readerStateReady;
  $('note-page').textContent = `关联第 ${state.page + 1} 页`; $('zoom-level').textContent = Math.round(state.zoom * 100) + '%';
  $('page-stage').classList.toggle('drawing', state.tool !== 'read');
  $('notes-panel').hidden = !state.notesOpen; $('notes-toggle').setAttribute('aria-expanded', String(state.notesOpen));
  readingQuality.update();
}
async function loadDocument() {
  if (state.doc) return state.doc;
  const version = state.loadVersion;
  setStatus('正在提取文字，首次打开可能需要一些时间…');
  const doc = await api(`/desktop/papers/${state.paper.id}/document`);
  if (version !== state.loadVersion) return null;
  state.doc = doc; state.total = doc.pages.length; state.page = Math.min(state.page, state.total - 1); updateControls(); updateJob();
  return doc;
}
async function renderPage() {
  if (state.workspaceView !== 'reading') return;
  endStroke(true);
  scientific.resetReadingCrops();
  const version = ++state.renderVersion, paperVersion = state.loadVersion;
  if (!state.paper?.local_pdf_path) { $('paper-overview').hidden = false; return; }
  updateControls(); $('paper-overview').hidden = true;
  if (state.mode === 'pdf') {
    $('text-page').hidden = true;
    if (!state.pdf) return;
    if (state.pdfContinuous) {
      $('page-stage').hidden = true;
      await pdfFlow.show(state.pdf, state.zoom, { page: state.page, offset: state.pdfOffset });
      if (version !== state.renderVersion || paperVersion !== state.loadVersion) return;
      pdfFlow.setTool(state.tool); updateControls();
      setStatus('原文 PDF · 连续滚动 · 原页按需加载，圈画按页保存');
      return;
    }
    pdfFlow.dispose();
    setStatus('正在渲染原文…');
    const page = await state.pdf.getPage(state.page + 1);
    const initial = page.getViewport({ scale: 1 });
    const available = Math.max(280, $('reading-content').clientWidth - 48);
    const viewport = page.getViewport({ scale: Math.min(1.5, available / initial.width) * state.zoom });
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    const buffer = document.createElement('canvas'); buffer.width = Math.floor(viewport.width * ratio); buffer.height = Math.floor(viewport.height * ratio);
    await page.render({ canvasContext: buffer.getContext('2d'), viewport, transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0] }).promise;
    if (version !== state.renderVersion || paperVersion !== state.loadVersion) return;
    const canvas = $('pdf-canvas'); canvas.width = buffer.width; canvas.height = buffer.height;
    canvas.style.width = viewport.width + 'px'; canvas.style.height = viewport.height + 'px';
    canvas.getContext('2d').drawImage(buffer, 0, 0); $('page-stage').hidden = false;
    drawAnnotations(); setStatus('原文 PDF · 圈画和笔迹按页面比例保存');
  } else {
    pdfFlow.dispose();
    $('page-stage').hidden = true;
    const doc = await loadDocument();
    if (!doc || version !== state.renderVersion || paperVersion !== state.loadVersion) return;
    renderText(); $('text-page').hidden = false;
    if (state.mode === 'compare') await renderSourcePreview();
    setStatus(isChinese() ? '中文阅读 · 未译段落显示原文 · 原文定位区域为文字提取估算' : '文字阅读 · 可选择文字并高亮');
  }
  $('reading-content').scrollTop = 0;
  updateControls();
  queueReadingPosition();
}
function applyTextMarks(element, block, language) {
  const marks = [];
  for (const note of allAnnotations()) {
    if (note.annotation_type !== 'text-highlight') continue;
    try { const data = JSON.parse(note.text); if (data.block_id === block.id && data.language === language) marks.push(data); } catch { /* Old notes remain visible in the note list. */ }
  }
  const text = element.textContent;
  const ranges = marks.map(mark => {
    let start = text.indexOf(mark.quote);
    if (mark.prefix || mark.suffix) {
      let pos = 0;
      while ((pos = text.indexOf(mark.quote, pos)) !== -1) {
        if ((!mark.prefix || text.slice(0, pos).endsWith(mark.prefix)) && (!mark.suffix || text.slice(pos + mark.quote.length).startsWith(mark.suffix))) { start = pos; break; }
        pos++;
      }
    }
    return { start, end: start + mark.quote.length };
  }).filter(r => r.start >= 0).sort((a, b) => a.start - b.start);
  let offset = 0; element.replaceChildren();
  for (const range of ranges) {
    if (range.start < offset) continue;
    element.append(document.createTextNode(text.slice(offset, range.start)), node('mark', '', text.slice(range.start, range.end))); offset = range.end;
  }
  element.append(document.createTextNode(text.slice(offset)));
}
function renderText() {
  scientific.resetReadingCrops();
  const el = $('text-page'), scroll = el.scrollTop; el.replaceChildren(); el.style.setProperty('--text-size', state.font + 'px');
  if (!state.doc) return;
  el.append(node('p', 'parse-warning', state.doc.warning));
  const pages = state.continuous ? state.doc.pages : [state.doc.pages[state.page]];
  const highlightedBlocks = new Set();
  for (const note of allAnnotations()) {
    if (note.annotation_type === 'text-highlight') {
      try { highlightedBlocks.add(JSON.parse(note.text).block_id); } catch { /* Keep legacy notes separate. */ }
    }
  }
  for (const page of pages) {
    if (!page) continue;
    const flow = node('section', 'flow-page'); flow.dataset.page = page.number;
    flow.append(node('p', 'page-kicker', `第 ${page.number + 1} 页 / ${state.total} · ${isChinese() ? '中文阅读' : '原文文字'}`));
    const placement = regionPlacement(page);
    if (page.reading_source) flow.append(node('p', 'parse-warning', '本页采用人工核对正文 · 原文与位置仍需核对；可在增强解析中切换旧版本。'));
    readingQuality.appendPageNotice(flow, page.number);
    if (!page.blocks.length && !state.doc.reading_quality) flow.append(node('p', 'page-empty', '这一页没有可提取文字。可查看原 PDF，或通过增强解析与 OCR 核对并采用正文。'));
    if (isChinese()) {
      const status = state.doc.translation_status?.pages.find(item => item.page_number === page.number);
      const message = translationPageMessage(status);
      if (page.error || message) flow.append(node('p', 'parse-warning', [page.error, message].filter(Boolean).join(' · ')));
    }
    for (const block of page.blocks) {
      scientific.appendRegions(flow, page, placement.before.get(block.id) || [], { inline: true });
      const edit = state.editor.edits?.[block.id], value = edit ? edit.zh : page.translated[block.id];
      const translated = typeof value === 'string' && value.trim() ? value : undefined;
      const language = isChinese() && translated !== undefined ? 'zh' : 'en';
      const wrapper = node('section', 'text-block' + (isChinese() && translated === undefined ? ' untranslated' : '')); wrapper.dataset.blockId = block.id;
      wrapper.classList.toggle('heading-block', block.type === 'heading');
      wrapper.classList.toggle('located', state.focusBlock?.id === block.id);
      const tools = node('div', 'block-actions');
      const locate = node('button', 'plain', '查看原文'); locate.onclick = () => locateBlock(page.number, block); tools.append(locate);
      if (isChinese()) {
        const change = node('button', 'plain', edit ? '修改手动译文' : '修改译文');
        change.onclick = () => { state.editTarget = { paperId: state.paper.id, id: block.id }; $('edit-zh').value = translated || ''; $('edit-dialog').showModal(); }; tools.append(change);
      }
      if (edit) tools.append(node('small', 'manual-label', '手动编辑 · 重译不覆盖'));
      if (block.protected_spans?.length) tools.append(node('small', 'protected-label', '含原式 / 表格区域 · 请核对原页'));
      if (edit?.conflict) tools.append(node('small', 'quality-warning', '机器译文已变化，请核对手动修改'));
      wrapper.append(tools);
      const text = node('p', 'block-text', language === 'zh' ? translated : block.en); text.dataset.language = language;
      applyTextMarks(text, block, language); wrapper.append(text);
      if (isChinese() && translated !== undefined && $('bilingual').checked) wrapper.append(node('p', 'block-original', block.en));
      const warnings = edit?.warnings || page.warnings?.[block.id] || [];
      if (isChinese() && warnings.length) wrapper.append(node('p', 'quality-warning', warnings.join('；')));
      if (!edit && !highlightedBlocks.has(block.id) && entirelyScientific(block, page.scientific_regions || [])) {
        const fallback = node('details', 'scientific-text-fallback');
        fallback.append(node('summary', '', '查看提取文字与译文 · 公式表格请以原图为准'), wrapper);
        flow.append(fallback);
      } else flow.append(wrapper);
    }
    if (placement.trailing.length) {
      flow.append(node('p', 'parse-warning', '以下原图尚未关联到正文段落，请核对原页位置。'));
      scientific.appendRegions(flow, page, placement.trailing, { inline: true });
    }
    for (const diagnostic of page.scientific_diagnostics || []) flow.append(node('p', 'quality-warning', diagnostic));
    el.append(flow);
  }
  scientific.observeReadingCrops();
  el.scrollTop = scroll;
}
async function renderSourcePreview() {
  if (state.mode !== 'compare' || !state.pdf) return;
  const version = ++state.previewVersion, paperVersion = state.loadVersion, number = state.focusBlock?.page ?? state.page;
  const page = await state.pdf.getPage(number + 1), initial = page.getViewport({ scale: 1 });
  const available = Math.max(180, $('source-preview').clientWidth - 24);
  const viewport = page.getViewport({ scale: Math.min(2, available / initial.width) });
  const ratio = Math.min(2, window.devicePixelRatio || 1), buffer = document.createElement('canvas');
  buffer.width = Math.floor(viewport.width * ratio); buffer.height = Math.floor(viewport.height * ratio);
  await page.render({ canvasContext: buffer.getContext('2d'), viewport, transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0] }).promise;
  if (version !== state.previewVersion || paperVersion !== state.loadVersion || state.mode !== 'compare') return;
  const canvas = $('source-canvas'); canvas.width = buffer.width; canvas.height = buffer.height;
  canvas.style.width = viewport.width + 'px'; canvas.style.height = viewport.height + 'px'; canvas.getContext('2d').drawImage(buffer, 0, 0);
  $('source-page-number').textContent = `第 ${number + 1} 页 · 区域为估算`; $('source-region').replaceChildren();
  if (state.focusBlock?.box) {
    const [x0, y0, x1, y1] = state.focusBlock.box;
    $('source-region').append(svgNode('rect', { x: x0 * 1000, y: y0 * 1000, width: (x1 - x0) * 1000, height: (y1 - y0) * 1000, fill: '#3468b2', 'fill-opacity': .12, stroke: '#3468b2', 'stroke-width': 2 }));
  }
}
async function locateBlock(page, block) {
  const changedPage = state.page !== page;
  state.focusBlock = { ...block, page }; state.page = page;
  try {
    const switched = state.mode !== 'compare';
    if (switched || changedPage) { state.mode = 'compare'; await renderPage(); }
    else { updateControls(); await renderSourcePreview(); }
    $('text-page').querySelectorAll('.text-block').forEach(el => el.classList.toggle('located', el.dataset.blockId === block.id));
    const target = $('text-page').querySelector(`[data-block-id="${block.id}"]`);
    if (target?.parentElement.classList.contains('scientific-text-fallback')) target.parentElement.open = true;
    if (switched || changedPage) target?.scrollIntoView({ block: 'nearest' });
  } catch (error) { toast(error.message, true); }
}
function svgNode(tag, attributes) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [name, value] of Object.entries(attributes)) el.setAttribute(name, value);
  return el;
}
function graphic(annotation) {
  if (annotation.annotation_type === 'ink') {
    return svgNode('path', { d: annotation.path.map((p, i) => `${i ? 'L' : 'M'}${p.x * 1000},${p.y * 1000}`).join(' '), stroke: annotation.color || '#3468b2', 'stroke-width': 2.2 });
  }
  if (annotation.annotation_type === 'highlight') return svgNode('rect', { x: annotation.x * 1000, y: annotation.y * 1000, width: annotation.width * 1000, height: annotation.height * 1000, fill: annotation.color || '#e4bd45', opacity: .28 });
  return null;
}
function drawAnnotations(layer = null, pageNumber = state.page) {
  const layers = layer ? [layer] : [$('ink-layer'), ...pdfFlow.layers()];
  for (const target of layers) {
    const number = target === $('ink-layer') ? pageNumber : Number(target.dataset.page);
    target.replaceChildren();
    for (const annotation of allAnnotations().filter(n => n.page_number === number)) { const el = graphic(annotation); if (el) target.append(el); }
    if (stroke?.layer === target) target.append(stroke.el);
  }
}
function getSelectionAnchor() {
  const selection = window.getSelection();
  if (!selection?.rangeCount || selection.isCollapsed) return null;
  const range = selection.getRangeAt(0);
  const start = range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer : range.startContainer.parentElement;
  const end = range.endContainer.nodeType === Node.ELEMENT_NODE ? range.endContainer : range.endContainer.parentElement;
  const block = start.closest('.text-block'), text = start.closest('.block-text');
  if (!block || !text || end.closest('.block-text') !== text) return null;
  const before = range.cloneRange(); before.selectNodeContents(text); before.setEnd(range.startContainer, range.startOffset);
  const offset = before.toString().length, quote = selection.toString();
  return { block_id: block.dataset.blockId, page_number: Number(block.closest('.flow-page').dataset.page), language: text.dataset.language, quote, prefix: text.textContent.slice(Math.max(0, offset - 30), offset), suffix: text.textContent.slice(offset + quote.length, offset + quote.length + 30) };
}
function queueAnnotation(body) {
  body.client_id = crypto.randomUUID();
  outbox.push({ paper_id: state.paper.id, body });
  try { persistOutbox(); } catch (error) { outbox.pop(); throw new Error('本地存储空间不足，未保存；请先复制笔记内容'); }
  $('save-status').textContent = '已存本机，正在提交…'; renderNotes(); drawAnnotations();
  flushOutbox();
}
async function flushOutbox() {
  if (flushing || !state.token || !outbox.length) return;
  flushing = true;
  try {
    while (outbox.length) {
      const entry = outbox[0];
      inFlight = entry.body.client_id;
      const saved = await api(`/desktop/papers/${entry.paper_id}/annotations`, 'POST', entry.body);
      outbox = outbox.filter(x => x.body.client_id !== entry.body.client_id); persistOutbox();
      if (state.paper?.id === entry.paper_id && !saved.deleted) { state.annotations = state.annotations.filter(x => x.id !== saved.id); state.annotations.push(saved); }
      renderNotes(); drawAnnotations();
    }
    $('save-status').textContent = '笔记已保存';
  } catch (error) { $('save-status').textContent = '已存本机，等待补存'; }
  finally { flushing = false; inFlight = null; }
}
function renderNotes() {
  const notes = allAnnotations().sort((a, b) => a.page_number - b.page_number);
  $('notes-count').textContent = notes.length; $('notes-list').replaceChildren();
  for (const note of notes) {
    const card = node('section', 'note-card' + (note.pending ? ' pending' : ''));
    const header = node('div', 'note-card-header'); const jump = node('button', 'plain', `第 ${note.page_number + 1} 页 · ${{ note: '笔记', ink: '笔迹', highlight: '区域高亮', 'text-highlight': '文字高亮' }[note.annotation_type] || '标注'}`);
    jump.onclick = () => gotoPage(note.page_number); header.append(jump);
    const remove = node('button', 'plain', '×'); remove.setAttribute('aria-label', '删除这条标注'); remove.onclick = () => deleteNote(note); header.append(remove); card.append(header);
    if (note.annotation_type === 'text-highlight') {
      try {
        const data = JSON.parse(note.text); card.append(node('blockquote', '', data.quote));
        if (state.doc && !state.doc.pages.some(page => page.blocks.some(block => block.id === data.block_id)))
          card.append(node('small', 'muted', '此高亮属于其他正文版本，已保留引文；可切换旧版本查看。'));
      } catch { card.append(node('p', '', note.text)); }
    } else if (note.text) card.append(node('p', '', note.text));
    else if (note.annotation_type === 'ink') card.append(node('p', 'muted', '原文页面上的自由笔迹'));
    else card.append(node('p', 'muted', '原文页面上的框选高亮'));
    if (note.pending) card.append(node('small', 'muted', '已存本机，等待提交'));
    $('notes-list').append(card);
  }
  if (!notes.length) $('notes-list').append(node('p', 'muted', '还没有笔记。写下一个问题，或在原文上圈画。'));
}
async function deleteNote(note) {
  if (note.pending && 'pending-' + inFlight === note.id) { toast('这条标注正在提交，请完成后再删除'); return; }
  if (!confirm('删除这条笔记或标注？此操作不能撤回。')) return;
  try {
    if (note.pending) { outbox = outbox.filter(x => 'pending-' + x.body.client_id !== note.id); persistOutbox(); }
    else { await api(`/desktop/papers/${state.paper.id}/annotations/${note.id}`, 'DELETE'); state.annotations = state.annotations.filter(x => x.id !== note.id); }
    renderNotes(); drawAnnotations(); if (state.mode !== 'pdf' && state.doc) renderText();
  } catch (error) { toast(error.message, true); }
}
async function gotoPage(page) {
  if (state.workspaceView !== 'reading' || !state.readerStateReady || !state.total || state.busy || !Number.isFinite(page)) return;
  endStroke(true);
  state.page = Math.max(0, Math.min(state.total - 1, Math.floor(page)));
  state.focusBlock = null;
  if (state.mode === 'pdf' && state.pdfContinuous && pdfFlow.active) {
    state.pdfOffset = 0; pdfFlow.goTo(state.page); updateControls(); return;
  }
  if (state.continuous && state.mode !== 'pdf' && state.doc) {
    $('text-page').querySelector(`[data-page="${state.page}"]`)?.scrollIntoView({ block: 'start' });
    updateControls(); if (state.mode === 'compare') await renderSourcePreview(); return;
  }
  try { await renderPage(); } catch (error) { toast(error.message, true); }
}
function updateJob() {
  const job = state.job;
  const active = ['queued', 'running', 'cancelling'].includes(job.status);
  const presentation = translationJobState(job, state.doc?.translation_status);
  $('translate-button').disabled = active || !state.readerStateReady; $('translate-button').textContent = presentation.button;
  $('job-banner').hidden = job.status === 'idle' || !job.status;
  $('job-banner').classList.toggle('error', !!job.error);
  $('cancel-translation').hidden = !active; $('cancel-translation').disabled = job.status === 'cancelling';
  $('job-text').textContent = `${presentation.label}${active && job.scope === 'page' ? ' · 当前页' : active && job.scope === 'failed' ? ' · 失败页补译' : ''}${presentation.progress ? ` · ${presentation.progress}` : ''}${job.error ? ' · ' + job.error : ''}`;
  if (job.error) $('job-text').title = job.error;
}
async function pollJob() {
  if (!state.paper) return;
  const id = state.paper.id, version = state.loadVersion;
  const job = await api(`/desktop/papers/${id}/translation`);
  if (version !== state.loadVersion) return;
  state.job = job; updateJob();
  if (job.done !== undefined && job.done !== state.jobDone) {
    const needsRefresh = state.jobDone >= 0 || job.done > 0;
    state.jobDone = job.done;
    if (needsRefresh && isChinese()) {
      state.doc = null; const doc = await loadDocument(), editor = await api(`/desktop/papers/${id}/editor`);
      if (doc && version === state.loadVersion) { state.editor = editor; renderText(); }
    }
  }
}
async function importFile(file) {
  if (!file) return;
  $('import-button').disabled = true; toast('正在导入 PDF…');
  try {
    const form = new FormData(); form.append('file', file);
    const paper = await api('/desktop/import', 'POST', form);
    state.filter = 'all'; state.tag = ''; state.listPage = 1; $('search').value = '';
    await setFilter('all'); await selectPaper(paper); toast('PDF 已导入本机');
  } catch (error) { toast(error.message, true); }
  finally { $('import-button').disabled = false; $('import-file').value = ''; }
}
function pointerPosition(event, layer) {
  const rect = layer.getBoundingClientRect();
  return { x: Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)), y: Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)) };
}
function beginStroke(event) {
  if (state.tool === 'read' || !state.paper || event.button !== 0) return;
  const layer = event.currentTarget, page = layer.dataset.page === undefined ? state.page : Number(layer.dataset.page);
  layer.setPointerCapture(event.pointerId);
  state.page = page; updateControls();
  const point = pointerPosition(event, layer); stroke = { tool: state.tool, start: point, points: [point], version: state.loadVersion, page, layer };
  const body = { annotation_type: state.tool, color: state.tool === 'highlight' ? '#e4bd45' : $('ink-color').value, path: [point], x: point.x, y: point.y, width: 0, height: 0 };
  stroke.body = body; stroke.el = graphic(body); layer.append(stroke.el);
}
function moveStroke(event) {
  if (!stroke || stroke.layer !== event.currentTarget) return; const point = pointerPosition(event, stroke.layer);
  if (stroke.tool === 'ink') {
    stroke.points.push(point); stroke.body.path = stroke.points;
    stroke.el.setAttribute('d', stroke.points.map((p, i) => `${i ? 'L' : 'M'}${p.x * 1000},${p.y * 1000}`).join(' '));
  } else {
    Object.assign(stroke.body, { x: Math.min(point.x, stroke.start.x), y: Math.min(point.y, stroke.start.y), width: Math.abs(point.x - stroke.start.x), height: Math.abs(point.y - stroke.start.y) });
    for (const key of ['x', 'y', 'width', 'height']) stroke.el.setAttribute(key, stroke.body[key] * 1000);
  }
}
function endStroke(cancelled = false) {
  if (!stroke) return; const saved = stroke; stroke = null;
  if (cancelled || saved.version !== state.loadVersion || (saved.tool === 'ink' ? saved.points.length < 2 : saved.body.width < .002 || saved.body.height < .002)) { drawAnnotations(); return; }
  try { queueAnnotation({ ...saved.body, page_number: saved.page }); } catch (error) { drawAnnotations(); toast(error.message, true); }
}
function bindInkLayer(layer) {
  layer.addEventListener('pointerdown', beginStroke);
  layer.addEventListener('pointermove', moveStroke);
  layer.addEventListener('pointerup', () => endStroke());
  layer.addEventListener('pointercancel', () => endStroke(true));
  layer.addEventListener('lostpointercapture', () => endStroke(true));
}
bindInkLayer($('ink-layer'));
pdfFlowInput.onchange = async () => {
  endStroke(true); state.pdfContinuous = pdfFlowInput.checked; state.pdfOffset = 0;
  try { await renderPage(); queueReadingPosition(); } catch (error) { toast(error.message, true); }
};

document.querySelectorAll('[data-filter]').forEach(el => el.onclick = () => setFilter(el.dataset.filter));
document.querySelectorAll('[data-mode]').forEach(el => el.onclick = async () => {
  if (!state.paper?.local_pdf_path) { toast('请先下载或导入 PDF'); return; }
  state.mode = el.dataset.mode; state.tool = 'read';
  try { await renderPage(); } catch (error) { toast(error.message, true); }
});
document.querySelectorAll('[data-tool]').forEach(el => el.onclick = () => { state.tool = el.dataset.tool; updateControls(); });
document.querySelectorAll('[data-close]').forEach(el => el.onclick = () => $(el.dataset.close).close());
$('search').oninput = () => {
  libraryRequest++; state.listPage = 1; clearTimeout(searchTimer);
  $('paper-list').setAttribute('aria-busy', 'true'); $('list-count').textContent = '正在搜索文献…';
  $('paper-list').querySelectorAll('.paper-card').forEach(button => { button.disabled = true; });
  searchTimer = setTimeout(() => refreshLibrary().catch(e => toast(e.message, true)), 250);
};
for (const id of ['folder-filter', 'status-filter']) $(id).onchange = () => { state.listPage = 1; refreshLibrary().catch(e => toast(e.message, true)); };
$('list-prev').onclick = () => { state.listPage--; refreshLibrary().catch(e => toast(e.message, true)); };
$('list-next').onclick = () => { state.listPage++; refreshLibrary().catch(e => toast(e.message, true)); };
$('refresh-button').onclick = () => Promise.all([refreshOverview(), refreshLibrary()]).catch(e => toast(e.message, true));
$('import-button').onclick = $('welcome-import').onclick = () => $('import-file').click();
$('import-file').onchange = () => importFile($('import-file').files[0]);
$('page-prev').onclick = () => gotoPage(state.page - 1); $('page-next').onclick = () => gotoPage(state.page + 1);
$('page-input').onchange = () => gotoPage(Number($('page-input').value) - 1);
$('zoom-in').onclick = () => { state.zoom = Math.min(2.5, state.zoom + .15); renderPage().catch(e => toast(e.message, true)); };
$('zoom-out').onclick = () => { state.zoom = Math.max(.5, state.zoom - .15); renderPage().catch(e => toast(e.message, true)); };
$('font-up').onclick = () => { state.font = Math.min(30, state.font + 2); renderText(); };
$('font-down').onclick = () => { state.font = Math.max(14, state.font - 2); renderText(); };
$('bilingual').onchange = renderText;
$('notes-toggle').onclick = () => { state.notesOpen = !state.notesOpen; updateControls(); if (state.pdf) renderPage().catch(e => toast(e.message, true)); };
$('favorite-button').onclick = async () => {
  if (!state.paper) return; const paper = state.paper;
  try {
    paper.reader_state = await api(`/desktop/papers/${paper.id}/state`, 'PATCH', { starred: !paper.reader_state?.starred });
    if (state.paper.id === paper.id) { $('favorite-button').textContent = paper.reader_state.starred ? '★' : '☆'; $('favorite-button').classList.toggle('starred', paper.reader_state.starred); }
    await Promise.all([refreshOverview(), refreshLibrary()]);
  } catch (error) { toast(error.message, true); }
};
$('note-input').oninput = () => { try { rememberDraft(); } catch { $('save-status').textContent = '草稿保存失败，请复制备份'; } };
$('save-note').onclick = () => {
  const text = $('note-input').value.trim(); if (!state.paper || !text) { toast('先写一点笔记再保存'); return; }
  try {
    queueAnnotation({ page_number: state.page, annotation_type: 'note', text, color: '#3468b2', path: [] });
    $('note-input').value = ''; rememberDraft(); toast('笔记已存到本机');
  } catch (error) { toast(error.message, true); }
};
$('text-highlight').addEventListener('mousedown', event => event.preventDefault());
$('text-highlight').onclick = () => {
  const anchor = getSelectionAnchor();
  if (!anchor) { toast('请在文字阅读区选中同一段落内的文字'); return; }
  try { queueAnnotation({ page_number: anchor.page_number, annotation_type: 'text-highlight', text: JSON.stringify(anchor), color: '#e4bd45', path: [] }); window.getSelection().removeAllRanges(); renderText(); } catch (error) { toast(error.message, true); }
};
$('undo-button').onclick = async () => {
  const last = allAnnotations().filter(n => n.page_number === state.page && ['ink', 'highlight'].includes(n.annotation_type)).at(-1);
  if (!last) { toast('当前页没有可撤销的图形标注'); return; }
  await deleteNote(last);
};
$('sync-button').onclick = async () => {
  try { await api('/desktop/sync', 'POST', { source: state.config.sync_source || 'arxiv', max_results: 30 }); await refreshOverview(); } catch (error) { toast(error.message, true); }
};
const chatgpt = installChatGPT({ state, $, node, api, toast });
const classification = installClassification({ $, api, toast });
$('settings-button').onclick = async () => {
  try {
    state.config = await api('/desktop/config');
    $('provider').value = state.config.provider; $('base-url').value = state.config.base_url; $('model').value = state.config.model;
    $('api-key').value = ''; $('api-key').placeholder = state.config.has_api_key ? '已配置；留空保持原有 Key' : '可选，按服务要求填写';
    $('key-label').hidden = state.config.provider === 'ollama'; $('sync-source').value = state.config.sync_source;
    $('daily-enabled').checked = state.config.daily_enabled; $('daily-time').value = state.config.daily_time; $('settings-error').textContent = '';
    $('auto-backup').checked = state.config.auto_backup; $('close-to-tray').checked = !!state.config.close_to_tray; $('clear-api-key').checked = false; $('model-test-result').textContent = '';
    $('settings-dialog').showModal();
    await classification.refresh();
    chatgpt.syncProvider();
    if (state.config.provider === 'chatgpt') await chatgpt.refresh();
  } catch (error) { toast(error.message, true); }
};
$('provider').onchange = () => { $('api-key').value = ''; $('key-label').hidden = $('provider').value === 'ollama'; $('base-url').value = $('provider').value === 'ollama' ? 'http://127.0.0.1:11434' : 'https://api.openai.com/v1'; chatgpt.syncProvider(); if ($('provider').value === 'chatgpt') { $('model').value = ''; $('model-options').replaceChildren(); chatgpt.refresh().catch(error => { $('chatgpt-status').textContent = error.message; }); } };
function settingsPayload() {
  const payload = { provider: $('provider').value, base_url: $('base-url').value.trim(), model: $('model').value.trim(), daily_enabled: $('daily-enabled').checked, daily_time: $('daily-time').value, sync_source: $('sync-source').value, auto_backup: $('auto-backup').checked, clear_api_key: $('clear-api-key').checked };
  payload.close_to_tray = $('close-to-tray').checked;
  if ($('api-key').value.trim()) payload.api_key = $('api-key').value.trim();
  return payload;
}
$('settings-form').onsubmit = async event => {
  event.preventDefault(); const payload = settingsPayload();
  try { state.config = await api('/desktop/config', 'PUT', payload); $('settings-dialog').close(); toast('设置已保存'); } catch (error) { $('settings-error').textContent = error.message; }
};
$('topics-button').onclick = async () => {
  try { const topics = await api('/desktop/topics'); $('topic-categories').value = (topics.categories || []).join('\n'); $('topic-keywords').value = (topics.keywords || []).join('\n'); $('topics-error').textContent = ''; $('topics-dialog').showModal(); } catch (error) { toast(error.message, true); }
};
$('topics-form').onsubmit = async event => {
  event.preventDefault(); const split = id => $(id).value.split('\n').map(s => s.trim()).filter(Boolean);
  try { await api('/desktop/topics', 'PUT', { categories: split('topic-categories'), keywords: split('topic-keywords') }); $('topics-dialog').close(); toast('采集方向已保存'); } catch (error) { $('topics-error').textContent = error.message; }
};
async function openTranslation(scope = 'all', force = false) {
  if (!state.paper?.local_pdf_path) { toast('请先下载 PDF，再翻译全文'); return; }
  try {
    const target = { id: state.paper.id, version: state.loadVersion, page: state.page };
    await loadDocument();
    if (target.id !== state.paper?.id || target.version !== state.loadVersion) return;
    state.config = await api('/desktop/config');
    if (target.id !== state.paper?.id || target.version !== state.loadVersion) return;
    state.translationTarget = target;
    target.chatgptBinding = state.config.provider === 'chatgpt' ? { chatgpt_account_id: state.config.chatgpt_account_id, chatgpt_generation: state.config.chatgpt_generation, chatgpt_model: state.config.model } : {};
    $('translation-scope').value = scope; $('force-translate').checked = force;
    $('confirm-description').textContent = state.config.provider === 'ollama'
      ? `使用本地模型 ${state.config.model}，正文发送到 ${state.config.base_url}。`
      : state.config.provider === 'chatgpt' ? `Using ChatGPT plan · ${state.config.chatgpt_label}。使用 ${state.config.model}，将所选论文正文发送到 OpenAI 官方服务；使用你的套餐或已授权额度。`
      : `使用 ${state.config.model}，将论文正文发送到 ${state.config.base_url}。远程服务可能收取费用。`;
    $('confirm-description').textContent += ' 默认只补齐未译项，保留已有译文；勾选强制重译才重新发送整页。公式候选保留原式，识别表格的文字单元格单独翻译，纯数值不发送；检测可能漏项，请核对原文。';
    readingQuality.translationWarning();
    $('confirm-dialog').showModal();
  } catch (error) { toast(error.message, true); }
}
$('translate-button').onclick = () => openTranslation('all');
$('translate-page').onclick = () => openTranslation('page');
$('confirm-cancel').onclick = $('confirm-close').onclick = () => $('confirm-dialog').close();
$('confirm-translate').onclick = async () => {
  $('confirm-dialog').close();
  const target = state.translationTarget; if (!target) return;
  const { id, version } = target;
  try {
    const job = await api(`/desktop/papers/${id}/translation`, 'POST', { scope: $('translation-scope').value, page_number: target.page, force: $('force-translate').checked, ...target.chatgptBinding });
    if (version !== state.loadVersion) return;
    state.job = job; updateJob(); state.mode = 'zh'; await renderPage();
  } catch (error) { toast(error.message, true); }
};
$('cancel-translation').onclick = async () => {
  try { state.job = await api(`/desktop/papers/${state.paper.id}/translation/cancel`, 'POST'); updateJob(); } catch (error) { toast(error.message, true); }
};
$('export-notes').onclick = event => { if (pendingFor(state.paper?.id).length) { event.preventDefault(); toast('还有未提交的笔记，请补存完成后再导出'); } };

for (const [id, route] of [['model-list-button', 'models'], ['model-test-button', 'test']]) {
  $(id).onclick = async () => {
    $(id).disabled = true; $('model-test-result').textContent = route === 'test' ? '正在试译固定测试句…' : '正在获取模型列表…';
    try {
      const payload = settingsPayload();
      if (route === 'models' && payload.provider === 'chatgpt' && !payload.model) payload.model = '__catalog__';
      const result = await api(`/desktop/model/${route}`, 'POST', payload);
      if (route === 'models') {
        $('model-options').replaceChildren(...result.models.map(name => { const el = node('option'); el.value = name; el.label = result.model_catalog?.find(entry => entry.id === name)?.name || name; return el; }));
        $('model-test-result').textContent = result.models.length ? `连接成功，发现 ${result.models.length} 个模型：${result.models.join('、')}。仍需试译确认模型可生成。` : '连接成功，但没有可用模型；请先安装或启用模型。';
      } else $('model-test-result').textContent = `试译成功 · ${result.model}\n${result.translated_text}`;
    } catch (error) { $('model-test-result').textContent = error.message; }
    finally { $(id).disabled = false; }
  };
}
$('continuous').onchange = async () => { state.continuous = $('continuous').checked; await renderPage(); if (state.continuous) await gotoPage(state.page); };
$('focus-button').onclick = () => { state.focus = !state.focus; updateControls(); renderPage().catch(e => toast(e.message, true)); };
$('toc-button').onclick = async () => {
  if (!state.pdf) { toast('先打开 PDF，再查看目录'); return; }
  const id = state.paper.id, list = $('toc-list'); list.replaceChildren(node('p', 'muted', '正在读取目录…')); $('toc-dialog').showModal();
  try {
    const outline = await state.pdf.getOutline(); list.replaceChildren();
    if (id !== state.paper?.id) return;
    if (outline?.length) {
      const append = (entries, depth = 0) => { for (const entry of entries) {
        const button = node('button', 'toc-entry', entry.title); button.style.paddingLeft = `${12 + depth * 14}px`;
        button.onclick = async () => {
          try {
            const dest = typeof entry.dest === 'string' ? await state.pdf.getDestination(entry.dest) : entry.dest;
            if (!dest) { toast('该目录项没有页码'); return; }
            const page = typeof dest[0] === 'number' ? dest[0] : await state.pdf.getPageIndex(dest[0]);
            if (id !== state.paper?.id) return; $('toc-dialog').close(); await gotoPage(page);
          } catch (error) { toast(error.message, true); }
        }; list.append(button); append(entry.items || [], depth + 1);
      } }; append(outline);
    } else {
      const doc = await loadDocument(); if (!doc || id !== state.paper?.id) return;
      list.append(node('p', 'muted', 'PDF 没有自带目录，以下为标题推断，可能不完整。'));
      for (const entry of doc.outline || []) {
        const button = node('button', 'toc-entry', `${entry.title} · 第 ${entry.page + 1} 页`);
        button.onclick = async () => { $('toc-dialog').close(); await gotoPage(entry.page); };
        list.append(button);
      }
      if (!doc.outline?.length) list.append(node('p', 'muted', '未识别到章节标题，可使用页码导航。'));
    }
  } catch (error) { list.replaceChildren(node('p', 'form-error', error.message)); }
};
$('glossary-button').onclick = () => {
  if (!state.paper) return;
  $('glossary-dialog').dataset.paperId = state.paper.id;
  $('glossary-input').value = (state.editor.glossary || []).map(t => `${t.en} = ${t.zh}`).join('\n'); $('glossary-dialog').showModal();
};
$('save-glossary').onclick = async () => {
  try {
    const glossary = $('glossary-input').value.split('\n').map(s => s.trim()).filter(Boolean).map(line => {
      const pos = line.indexOf('='); if (pos <= 0 || !line.slice(pos + 1).trim()) throw new Error('每行请填写：英文 = 中文');
      return { en: line.slice(0, pos).trim(), zh: line.slice(pos + 1).trim() };
    });
    const id = Number($('glossary-dialog').dataset.paperId), editor = await api(`/desktop/papers/${id}/editor`, 'PATCH', { glossary });
    if (id === state.paper?.id) state.editor = editor; $('glossary-dialog').close(); toast('术语表已保存，仅用于新翻译');
  } catch (error) { toast(error.message, true); }
};
$('save-edit').onclick = async () => {
  const target = state.editTarget; if (!target) return;
  try {
    await api(`/desktop/papers/${target.paperId}/editor`, 'PATCH', { block_id: target.id, zh: $('edit-zh').value });
    if (target.paperId === state.paper?.id) {
      const version = state.loadVersion;
      const [editor, doc] = await Promise.all([api(`/desktop/papers/${target.paperId}/editor`), api(`/desktop/papers/${target.paperId}/document`)]);
      if (target.paperId === state.paper?.id && version === state.loadVersion) {
        state.editor = editor; state.doc = doc; updateJob(); renderText();
      }
    }
    $('edit-dialog').close(); toast('手动译文已保存；重译不会覆盖');
  } catch (error) { toast(error.message, true); }
};
$('translate-selection').addEventListener('mousedown', event => event.preventDefault());
$('translate-selection').onclick = async () => {
  const anchor = getSelectionAnchor(); if (!anchor) { toast('请在同一段落内选择文字'); return; }
  const paperId = state.paper.id; state.snippet = { paperId, anchor, result: null };
  $('snippet-source').textContent = anchor.quote; $('snippet-result').textContent = ''; $('snippet-to-note').hidden = true;
  try {
    const cfg = await api('/desktop/config');
    if (paperId !== state.paper?.id) return;
    state.snippet.chatgptBinding = cfg.provider === 'chatgpt' ? { chatgpt_account_id: cfg.chatgpt_account_id, chatgpt_generation: cfg.chatgpt_generation, chatgpt_model: cfg.model } : {};
    $('snippet-target').textContent = cfg.provider === 'chatgpt' ? `Using ChatGPT plan · ${cfg.chatgpt_label} · ${cfg.model}。只发送上述选段，使用你的套餐或已授权额度。` : `只发送上述选段到 ${cfg.base_url} · ${cfg.model}。远程服务可能计费。`;
    $('snippet-dialog').showModal();
  } catch (error) { toast(error.message, true); }
};
$('snippet-start').onclick = async () => {
  const target = state.snippet; if (!target) return;
  $('snippet-start').disabled = true; $('snippet-result').textContent = '正在翻译选段…';
  try {
    const result = await api(`/desktop/papers/${target.paperId}/translate-selection`, 'POST', { text: target.anchor.quote, ...target.chatgptBinding });
    if (state.snippet !== target) return; target.result = result;
    $('snippet-result').textContent = result.translated_text + (result.warnings?.length ? '\n核对提示：' + result.warnings.join('；') : '') + (result.cached ? '\n使用已有选段译文，未再次调用模型。' : '');
    $('snippet-to-note').hidden = false;
  } catch (error) { $('snippet-result').textContent = error.message; }
  finally { $('snippet-start').disabled = false; }
};
$('snippet-to-note').onclick = () => {
  const target = state.snippet;
  if (!target?.result || target.paperId !== state.paper?.id) { toast('论文已切换，请返回对应论文再保存'); return; }
  try {
    queueAnnotation({ page_number: target.anchor.page_number, annotation_type: 'note', text: `选段翻译 · ${target.result.model}\n原文：${target.anchor.quote}\n译文：${target.result.translated_text}`, color: '#3468b2', path: [] });
    $('snippet-dialog').close(); toast('选段译文已保存为笔记');
  } catch (error) { toast(error.message, true); }
};

async function saveDraftsToServer() {
  if (state.restoring) return;
  rememberDraft(); const drafts = {};
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i), match = /^paper-reader:draft:(\d+)$/.exec(key);
    if (match) drafts[match[1]] = localStorage.getItem(key);
  }
  await api('/desktop/drafts', 'POST', { drafts });
}
async function prepareBackup() {
  await flushReadingPositions();
  await flushOutbox(); if (outbox.length) throw new Error('还有未提交标注，请等待补存完成后再备份或恢复');
  await saveDraftsToServer();
  await assistant.flush();
}
window.paperReaderBeforeExit = async () => {
  try {
    if (state.restoring || stroke || document.querySelector('dialog[open]')) throw new Error('请先完成保存、关闭对话框或等待恢复结束，再退出程序');
    await prepareBackup();
    return { ok: true };
  } catch (error) { toast('尚未退出：' + error.message, true); return { ok: false }; }
};
async function refreshBackups() {
  const data = await api('/desktop/backups'); $('backup-list').replaceChildren();
  for (const item of data.items) {
    const card = node('section', 'backup-item'); card.append(node('p', '', item.name), node('small', 'muted', `${(item.size / 1024 / 1024).toFixed(2)} MB · ${item.modified_at}`));
    const actions = node('div', 'backup-actions'), download = node('a', 'plain', '下载备份');
    download.href = `/desktop/backups/${encodeURIComponent(item.name)}/download`; download.download = item.name; actions.append(download);
    const verify = node('button', 'plain', '验证'), restore = node('button', 'plain', '恢复此备份');
    verify.onclick = async () => {
      verify.disabled = true;
      try { const result = await api(`/desktop/backups/${item.name}/verify`, 'POST'); $('backup-result').textContent = `验证通过：${result.papers} 篇文献、${result.annotations} 条标注、${result.pdfs} 个 PDF。`; }
      catch (error) { $('backup-result').textContent = error.message; } finally { verify.disabled = false; }
    };
    restore.onclick = async () => {
      if (!confirm('恢复此备份将替换当前文献库、译文、笔记和草稿。程序会先保存当前库的回滚备份；原 PDF 不会删除。是否继续？')) return;
      restore.disabled = true;
      try {
        await prepareBackup(); state.restoring = true; $('backup-result').textContent = '正在验证并恢复，请勿关闭程序…';
        const result = await api('/desktop/backups/restore', 'POST', { name: item.name, confirmation: 'RESTORE' });
        const drafts = await api('/desktop/drafts');
        assistant.clearAfterRestore();
        const keys = Object.keys(localStorage).filter(k => /^paper-reader:draft:\d+$/.test(k)); keys.forEach(k => localStorage.removeItem(k));
        for (const [id, text] of Object.entries(drafts)) localStorage.setItem(`paper-reader:draft:${id}`, text);
        alert(`恢复完成。恢复前快照：${result.recovery_backup}。页面将刷新。`); location.reload();
      } catch (error) { state.restoring = false; $('backup-result').textContent = error.message; restore.disabled = false; }
    };
    actions.append(verify, restore); card.append(actions); $('backup-list').append(card);
  }
  if (!data.items.length) $('backup-list').append(node('p', 'muted', '还没有备份。先创建一份经过验证的快照。'));
}
$('backup-center-button').onclick = async () => { $('settings-dialog').close(); $('backup-result').textContent = ''; $('backup-dialog').showModal(); try { await refreshBackups(); } catch (error) { $('backup-result').textContent = error.message; } };
$('create-backup').onclick = async () => {
  $('create-backup').disabled = true; $('backup-result').textContent = '正在保存草稿、备份并验证…';
  try { await prepareBackup(); const result = await api('/desktop/backups', 'POST'); $('backup-result').textContent = `备份已验证：${result.papers} 篇文献、${result.annotations} 条标注、${result.pdfs} 个 PDF。`; await refreshBackups(); }
  catch (error) { $('backup-result').textContent = error.message; } finally { $('create-backup').disabled = false; }
};
const importBackup = node('button', 'secondary', '导入备份 ZIP'), backupInput = node('input');
backupInput.type = 'file'; backupInput.accept = '.zip,application/zip'; backupInput.hidden = true;
$('create-backup').after(importBackup, backupInput);
importBackup.onclick = () => backupInput.click();
backupInput.onchange = async () => {
  const file = backupInput.files[0]; if (!file) return;
  importBackup.disabled = true; $('backup-result').textContent = '正在导入并验证，不会立即恢复…';
  try { const form = new FormData(); form.append('file', file); const result = await api('/desktop/backups/import', 'POST', form); $('backup-result').textContent = `导入并验证通过：${result.papers} 篇文献。选择“恢复此备份”才会替换当前库。`; await refreshBackups(); }
  catch (error) { $('backup-result').textContent = error.message; }
  finally { importBackup.disabled = false; backupInput.value = ''; }
};
const tasksButton = node('button', 'plain', '任务中心'); tasksButton.id = 'tasks-button';
$('settings-button').before(tasksButton);
const indexButton = node('button', 'secondary', '建立 / 更新全文索引'); indexButton.type = 'button'; indexButton.id = 'index-button';
const indexHelp = node('p', 'muted', '全文搜索覆盖已提取 PDF 正文、译文、手动改译和笔记。未打开的 PDF 可在此建立索引；暂停后可在任务中心继续，文件变化会重新核对。扫描件无 OCR 时不能检索正文。');
$('backup-center-button').after(indexButton, indexHelp);
indexButton.onclick = async () => {
  try { await api('/desktop/index', 'POST'); toast('已开始建立全文索引，可在任务中心查看进度'); } catch (error) { toast(error.message, true); }
};
const organizeButton = node('button', 'plain', '整理'); organizeButton.id = 'organize-button'; $('favorite-button').before(organizeButton);
organizeButton.onclick = async () => {
  if (!state.paper) return;
  const id = state.paper.id, meta = await api(`/desktop/papers/${id}/state`);
  if (state.paper?.id !== id) return;
  $('organize-dialog').dataset.paperId = id; $('paper-folder').value = meta.folder || ''; $('paper-tags').value = (meta.tags || []).join('，');
  $('paper-status').value = meta.status || 'unread'; $('organize-dialog').showModal();
};
$('save-organize').onclick = async () => {
  const id = Number($('organize-dialog').dataset.paperId);
  try {
    const meta = await api(`/desktop/papers/${id}/state`, 'PATCH', { folder: $('paper-folder').value.trim(), tags: $('paper-tags').value.split(/[,，]/).map(t => t.trim()).filter(Boolean), status: $('paper-status').value });
    if (state.paper?.id === id) state.paper.reader_state = meta; $('organize-dialog').close();
    await Promise.all([refreshLibrary(), refreshOverview()]); toast('文献整理已保存');
  } catch (error) { toast(error.message, true); }
};
async function refreshTasks() {
  const data = await api('/desktop/tasks'), list = $('tasks-list'); list.replaceChildren();
  for (const task of data.items) {
    const card = node('section', 'backup-item'); card.append(node('p', '', task.title));
    card.dataset.taskKey = task.key;
    const status = { queued: '准备中', running: '执行中', cancelling: '正在停止任务', paused: '已暂停', cancelled: '已取消', done: '完成', partial: '部分失败', failed: '失败' }[task.status] || '记录';
    card.append(node('small', 'muted', `${status}${task.total ? ` · ${task.done || 0}/${task.total} ${['index-fulltext', 'classification'].includes(task.key) ? '篇' : '页'}` : ''}${task.updated_at ? ' · ' + task.updated_at : ''}`));
    if (task.error) card.append(node('p', 'quality-warning', task.error));
    if (task.failed_pages?.length) card.append(node('p', 'muted', '失败页：' + task.failed_pages.map(n => n + 1).join('、')));
    const actions = node('div', 'backup-actions');
    if (task.key === 'index-fulltext' && ['queued', 'running', 'cancelling'].includes(task.status)) {
      const pause = node('button', 'plain', '暂停索引'); pause.disabled = task.status === 'cancelling';
      pause.onclick = async () => { try { await api('/desktop/index/cancel', 'POST'); await refreshTasks(); } catch (error) { toast(error.message, true); } }; actions.append(pause);
    }
    if (task.key === 'index-fulltext' && ['paused', 'partial', 'failed'].includes(task.status)) {
      const resume = node('button', 'plain', '继续索引');
      resume.onclick = async () => { try { await api('/desktop/index', 'POST'); await refreshTasks(); } catch (error) { toast(error.message, true); } }; actions.append(resume);
    }
    if (task.paper_id) {
      const open = node('button', 'plain', '打开论文'); open.onclick = async () => { const paper = await api(`/papers/${task.paper_id}`); $('tasks-dialog').close(); await selectPaper(paper); }; actions.append(open);
      if (task.key.startsWith('translate:') && ['queued', 'running', 'cancelling'].includes(task.status)) {
        const pause = node('button', 'plain', '暂停翻译'); pause.disabled = task.status === 'cancelling';
        pause.onclick = async () => { try { await api(`/desktop/papers/${task.paper_id}/translation/cancel`, 'POST'); await refreshTasks(); } catch (error) { toast(error.message, true); } }; actions.append(pause);
      }
      if (task.key.startsWith('complex:')) {
        const preview = node('button', 'plain', '查看增强解析'); preview.onclick = async () => { const paper = await api(`/papers/${task.paper_id}`); $('tasks-dialog').close(); await selectPaper(paper); await complex.open(); }; actions.append(preview);
        if (['queued', 'running', 'cancelling'].includes(task.status)) {
          const cancel = node('button', 'plain', '取消解析'); cancel.disabled = task.status === 'cancelling';
          cancel.onclick = async () => { try { await api(`/desktop/papers/${task.paper_id}/complex/cancel`, 'POST'); await refreshTasks(); } catch (error) { toast(error.message, true); } }; actions.append(cancel);
        }
      }
    }
    card.append(actions); list.append(card);
  }
  if (!data.items.length) list.append(node('p', 'muted', '尚无任务记录。'));
}
tasksButton.onclick = async () => { $('tasks-dialog').showModal(); try { await refreshTasks(); } catch (error) { $('tasks-list').replaceChildren(node('p', 'form-error', error.message)); } };
$('refresh-tasks').onclick = () => refreshTasks().catch(error => toast(error.message, true));
let flowTimer;
function trackReadingPosition() {
  if (state.workspaceView !== 'reading' || !state.continuous || state.mode === 'pdf' || !state.doc) return;
  clearTimeout(flowTimer); flowTimer = setTimeout(async () => {
    if (state.workspaceView !== 'reading') return;
    const scroller = state.mode === 'compare' ? $('text-page') : $('reading-content'), top = scroller.getBoundingClientRect().top;
    const pages = [...$('text-page').querySelectorAll('.flow-page')];
    const closest = pages.reduce((best, el) => Math.abs(el.getBoundingClientRect().top - top) < Math.abs(best.getBoundingClientRect().top - top) ? el : best, pages[0]);
    if (!closest) return;
    const number = Number(closest.dataset.page); if (number === state.page) return;
    state.page = number; state.focusBlock = null; updateControls();
    try { if (state.mode === 'compare') await renderSourcePreview(); await api(`/desktop/papers/${state.paper.id}/state`, 'PATCH', { last_page: number }); } catch { /* Reading remains available. */ }
  }, 160);
}
$('reading-content').addEventListener('scroll', trackReadingPosition, { passive: true });
$('text-page').addEventListener('scroll', trackReadingPosition, { passive: true });
document.addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); workspace.library(); $('search').focus(); }
  if (!['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target.tagName) && !document.querySelector('dialog[open]')) {
    if (event.key === 'ArrowRight') gotoPage(state.page + 1);
    if (event.key === 'ArrowLeft') gotoPage(state.page - 1);
  }
});
window.addEventListener('beforeunload', () => { try { rememberDraft(); assistant.remember(); persistOutbox(); } catch { /* Existing persisted data remains intact. */ } });
let resizeTimer;
window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (state.workspaceView !== 'reading') return; if (state.pdf && state.mode === 'pdf') renderPage().catch(() => {}); else if (state.mode === 'compare') renderSourcePreview().catch(() => {}); }, 250); });

async function boot() {
  try {
    const value = await api('/desktop/bootstrap'); state.token = value.token; state.config = value.config;
    const drafts = await api('/desktop/drafts');
    for (const [id, text] of Object.entries(drafts)) if (localStorage.getItem(`paper-reader:draft:${id}`) === null) localStorage.setItem(`paper-reader:draft:${id}`, text);
    await Promise.all([refreshLibrary(), refreshOverview()]); flushOutbox();
  } catch (error) { $('list-count').textContent = '服务连接失败'; toast(error.message, true); }
}
let polling = false;
setInterval(async () => {
  if (polling || !state.token) return; polling = true;
  try { await pollJob(); await refreshOverview(); await flushOutbox(); if ($('tasks-dialog').open) await refreshTasks(); } catch { /* Local drafts and pending notes are preserved. */ }
  finally { polling = false; }
}, 3000);
setInterval(() => { if (state.token) saveDraftsToServer().catch(() => {}); }, 15000);
const assistant = installAssistant({ state, $, node, api, toast, loadDocument, locateBlock, renderPage });
const discovery = installDiscovery({ state, $, node, api, toast, selectPaper });
const scientific = installScientific({ state, $, node, api, toast, loadDocument, locateBlock, renderPage, openTranslation });
const complex = installComplex({ state, $, node, api, toast, locateBlock, renderPage, renderCrop: scientific.renderCrop, openZoom: scientific.openZoom });
installReviewed({ state, $, node, api, toast, renderCrop: scientific.renderCrop, renderPage, renderNotes });
installTranslationStatus({ state, $, node, api, toast, updateJob, gotoPage, locateBlock, refresh: renderPage });
const readingQuality = installReadingQuality({ state, $, node, api, toast, loadDocument, locateBlock, gotoPage });
const workspace = installWorkspace({ state, $, node, api, toast, selectPaper, renderPage,
  leaveReading: () => { rememberDraft(); assistant.remember(); queueReadingPosition(); endStroke(true); pdfFlow.dispose(); } });
const trayLabel = node('label', 'check-label'), trayInput = node('input');
trayInput.id = 'close-to-tray'; trayInput.type = 'checkbox';
trayLabel.append(trayInput, document.createTextNode('关闭窗口时驻留托盘，继续后台任务'));
$('daily-time').closest('label').after(trayLabel, node('p', 'muted', '仅适用于原生 Windows 窗口，默认关闭。托盘菜单可打开阅读器、隐藏窗口或退出；退出才会停止程序。托盘不可用时不会隐藏窗口。'));
$('explain-selection').onclick = () => {
  const anchor = getSelectionAnchor();
  if (!anchor) return toast('请先在文字阅读区选择同一段落内的文字');
  assistant.explain(anchor).catch(error => toast(error.message, true));
};
boot();
