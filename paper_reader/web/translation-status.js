/** Current-layout cache coverage is separate from job history and accuracy. */
export function translationJobState(job = {}, status) {
  const active = ['queued', 'running', 'cancelling'].includes(job.status);
  const labels = { queued: '正在准备论文', running: '正在翻译', cancelling: '正在暂停，等待当前请求返回',
    paused: '翻译已暂停', partial: '部分页面尚未完成', failed: '翻译失败' };
  let label = labels[job.status] || '';
  if (job.status === 'done') {
    label = !status ? '上次翻译任务已结束' : status.blocked_pages ? '上次任务已结束 · 有页面需要核对正文'
      : status.missing_text || status.missing_local ? '上次任务已结束 · 当前译文缓存仍有缺项'
      : status.no_text_pages ? '已提取内容缓存齐全 · 部分页面无可提取文字' : '已提取内容的译文缓存齐全';
  }
  if (!active && status?.review_items) label += `${label ? ' · ' : ''}${status.review_items} 项数字、引用或上下标差异待核对`;
  const progress = !active && status ? `缓存齐全 ${status.complete_pages} / ${status.total_pages} 页`
    : job.total ? `${job.done || 0} / ${job.total} 页` : '';
  const hasCache = job.status === 'done' || status?.complete_pages > 0 || status?.pages?.some(page => page.cached > 0);
  const button = active ? '正在翻译…' : hasCache && status && (status.missing_text || status.missing_local) ? '补译缺项'
    : ['partial', 'paused', 'failed'].includes(job.status) ? '继续翻译' : '翻译全文';
  return { label, progress, button };
}

export function translationPageMessage(page) {
  if (!page) return '尚未检查本页译文完整性，可打开“译文检查”。';
  if (page.state === 'blocked') return '本页无法安全检查，请先核对正文；未自动修改译文。';
  const review = page.review_items?.length ? `本页有 ${page.review_items.length} 项数字、引用或上下标差异；打开“译文检查”定位并核对原文。` : '';
  if (['complete', 'no_text'].includes(page.state)) return review;
  return `本页缓存尚有缺项：${page.missing_text} 项文字待翻译，${page.missing_local} 项数字或保护原文可本地补齐。打开“译文检查”查看；未译内容仍显示原文。${review}`;
}

export function installTranslationStatus({ state, $, node, api, toast, refresh, updateJob, gotoPage, locateBlock }) {
  const button = node('button', 'plain', '译文检查'); button.id = 'translation-check-button';
  button.type = 'button'; $('glossary-button').after(button);
  const dialog = node('dialog', 'translation-check-dialog'); dialog.id = 'translation-check-dialog';
  const header = node('div', 'dialog-header'), title = node('h2', '', '译文检查');
  title.id = 'translation-check-title'; dialog.setAttribute('aria-labelledby', title.id);
  const close = node('button', 'icon-button', '×'); close.type = 'button'; close.setAttribute('aria-label', '关闭译文检查');
  close.onclick = () => dialog.close(); header.append(title, close);
  const summary = node('p', 'translation-check-summary'); summary.id = 'translation-check-summary';
  summary.setAttribute('role', 'status'); summary.setAttribute('aria-live', 'polite');
  const notice = node('p', 'muted', '检查当前正文与表格的缓存缺项，并提示数字、引用或上下标差异；采用当前手动译文进行核对，不代表语义准确。补齐本地项不会调用模型，不会覆盖已有非空译文、手动修改或笔记。');
  const list = node('div', 'translation-check-pages'); list.id = 'translation-check-pages';
  const actions = node('div', 'dialog-actions');
  const repair = node('button', 'primary', '补齐数字与保护原文'); repair.id = 'repair-local-translation'; repair.type = 'button';
  const reload = node('button', 'secondary', '重新检查'); reload.type = 'button';
  actions.append(reload, repair); dialog.append(header, summary, notice, list, actions); document.body.append(dialog);
  let working = false, target = null;
  const current = (expected = target) => expected && expected === target && expected.id === state.paper?.id && expected.version === state.loadVersion;
  function draw(status) {
    list.replaceChildren();
    summary.textContent = `缓存齐全 ${status.complete_pages} / ${status.total_pages} 页 · 文字待翻译 ${status.missing_text} 项 · 可本地补齐 ${status.missing_local} 项`;
    if (status.blocked_pages) summary.textContent += ` · ${status.blocked_pages} 页需核对正文`;
    if (status.review_items) summary.textContent += ` · ${status.review_items} 项差异待核对`;
    const gaps = status.pages.filter(page => page.state !== 'complete' || page.review_items?.length);
    for (const page of gaps) {
      const row = node('button', 'translation-check-row'); row.type = 'button';
      row.append(node('span', '', `第 ${page.page_number + 1} 页`), node('span', 'muted',
        page.state === 'no_text' ? '无可提取文字，请查看原 PDF 或核对 OCR'
          : page.state === 'blocked' ? '保护信息需核对，未自动修复'
          : `${page.state === 'complete' ? '缓存齐全' : `待翻译 ${page.missing_text} 项 · 本地可补齐 ${page.missing_local} 项`}${page.review_items?.length ? ` · ${page.review_items.length} 项差异待核对` : ''}`));
      row.title = (page.review_items || []).flatMap(item => item.warnings).join('；');
      row.onclick = () => {
        if (!current()) return;
        dialog.close();
        const block = state.doc?.pages.find(item => item.number === page.page_number)?.blocks.find(item => item.id === page.review_items?.[0]?.id);
        Promise.resolve(block && locateBlock ? locateBlock(page.page_number, block) : gotoPage(page.page_number)).catch(error => toast(error.message, true));
      };
      list.append(row);
    }
    if (!gaps.length) list.append(node('p', 'muted', '当前提取内容的缓存项已齐全。专业术语、公式、数据与译文含义仍请对照原文核对。'));
    repair.disabled = working || !status.missing_local || !!status.blocked_pages
      || ['queued', 'running', 'cancelling'].includes(state.job.status);
  }
  async function check() {
    if (!current() || working) return;
    const expected = target;
    working = true; repair.disabled = reload.disabled = true;
    summary.textContent = '正在检查当前正文与表格…'; list.replaceChildren();
    try {
      const doc = await api(`/desktop/papers/${expected.id}/document`);
      if (!current(expected)) return;
      state.doc = doc; updateJob(); working = false; draw(doc.translation_status);
    } catch (error) { if (current(expected)) { summary.textContent = error.message; toast(error.message, true); } }
    finally { working = false; reload.disabled = false; }
  }
  button.onclick = () => {
    if (!state.paper?.local_pdf_path) { toast('请先打开一篇已下载的 PDF'); return; }
    if (working) { toast('上一项检查或补齐仍在处理，请稍后再打开'); return; }
    target = { id: state.paper.id, version: state.loadVersion }; dialog.showModal(); check();
  };
  reload.onclick = check;
  repair.onclick = async () => {
    if (!current() || working) return;
    const expected = target, id = expected.id; working = true; repair.disabled = reload.disabled = true;
    try {
      const result = await api(`/desktop/papers/${id}/translation/local-repair`, 'POST', {});
      if (!current(expected)) return;
      const doc = await api(`/desktop/papers/${id}/document`);
      if (!current(expected)) return;
      state.doc = doc; working = false; draw(result.translation_status); updateJob(); await refresh();
      if (current(expected)) toast(`已补齐 ${result.added_local} 项本地保护内容，未调用模型；待翻译文字未改动。`);
    } catch (error) { if (current(expected)) { summary.textContent = error.message; toast(error.message, true); } }
    finally { working = false; reload.disabled = false; }
  };
}
