/** Local rule alerts describe extraction anomalies, never translation accuracy. */
export function readingReviewMessage(status, pageNumbers = null) {
  if (!status) return '正文尚未检查，请先核对提取内容。';
  const pages = status.pages.filter(page => page.issues.length && (!pageNumbers || pageNumbers.includes(page.page_number)));
  if (!pages.length) return '';
  const numbers = pages.slice(0, 8).map(page => page.page_number + 1).join('、');
  return `第 ${numbers}${pages.length > 8 ? ' 等' : ''} 页存在提取异常或无可提取文字。请先通过“正文检查”核对原页；异常文字可能影响翻译，无文字页需先提取或 OCR。`;
}

export function installReadingQuality({ state, $, node, api, toast, loadDocument, locateBlock, gotoPage }) {
  const button = node('button', 'plain', '正文检查'); button.id = 'reading-quality-button'; button.type = 'button';
  $('reader-status').after(button);
  const dialog = node('dialog', 'reading-quality-dialog'); dialog.id = 'reading-quality-dialog';
  const header = node('div', 'dialog-header'), title = node('h2', '', '正文检查'); title.id = 'reading-quality-title';
  dialog.setAttribute('aria-labelledby', title.id);
  const close = node('button', 'icon-button', '×'); close.type = 'button'; close.setAttribute('aria-label', '关闭正文检查');
  close.onclick = () => dialog.close(); header.append(title, close);
  const summary = node('p', 'reading-quality-summary'); summary.id = 'reading-quality-summary'; summary.setAttribute('role', 'status');
  const notice = node('p', 'muted', '在本机检查无文字、英文逐字拆分和无法解码的字形。不调用 AI，不修改正文、已有译文或批注。未发现这些异常，也不代表提取完整或译文准确。');
  const list = node('div', 'reading-quality-pages'); list.id = 'reading-quality-pages';
  dialog.append(header, summary, notice, list); document.body.append(dialog);
  const warning = node('p', 'quality-warning'); warning.id = 'translation-reading-warning'; warning.hidden = true;
  $('confirm-description').after(warning);
  let target = null;
  function current(expected) { return expected.id === state.paper?.id && expected.version === state.loadVersion; }
  function update() {
    const count = state.doc?.reading_quality?.review_pages;
    button.textContent = count ? `正文检查 · ${count} 页待核对` : '正文检查';
  }
  function translationWarning() {
    const scope = $('translation-scope').value, target = state.translationTarget;
    const pages = scope === 'page' ? [target?.page] : scope === 'failed' ? state.doc?.pages.filter(page => page.error).map(page => page.number) || [] : null;
    warning.textContent = readingReviewMessage(state.doc?.reading_quality, pages); warning.hidden = !warning.textContent;
  }
  $('translation-scope').addEventListener('change', translationWarning);
  async function viewPage(number, blockId) {
    dialog.close();
    const block = state.doc.pages.find(page => page.number === number)?.blocks.find(block => block.id === blockId);
    if (block) await locateBlock(number, block);
    else { state.mode = 'pdf'; await gotoPage(number); }
  }
  button.onclick = async () => {
    if (!state.paper?.local_pdf_path) return toast('请先下载或导入 PDF');
    target = { id: state.paper.id, version: state.loadVersion }; const expected = target;
    button.disabled = true;
    try {
      // Refresh derived diagnostics when a reviewed layout has been adopted.
      await loadDocument();
      if (!current(expected) || target !== expected) return;
      const doc = await api(`/desktop/papers/${expected.id}/document`);
      if (!current(expected) || target !== expected) return;
      state.doc.reading_quality = doc.reading_quality; update(); list.replaceChildren();
      summary.textContent = doc.reading_quality.review_pages ? `${doc.reading_quality.review_pages} / ${doc.reading_quality.total_pages} 页需要核对原文` : '未发现规则内异常；仍需结合原文判断阅读质量。';
      for (const page of doc.reading_quality.pages) for (const issue of page.issues) {
        const row = node('button', 'reading-quality-row'); row.type = 'button';
        row.append(node('strong', '', `第 ${page.page_number + 1} 页`), node('span', '', issue.message), node('small', '', '查看原文 ↗'));
        row.onclick = () => { if (current(expected)) viewPage(page.page_number, issue.block_ids[0]).catch(error => toast(error.message, true)); else dialog.close(); };
        list.append(row);
      }
      dialog.showModal();
    } catch (error) { if (current(expected)) toast(error.message, true); }
    finally { button.disabled = false; }
  };
  function appendPageNotice(flow, number) {
    const issues = state.doc?.reading_quality?.pages.find(page => page.page_number === number)?.issues || [];
    if (!issues.length) return;
    const notice = node('aside', 'reading-quality-notice');
    notice.append(node('p', '', issues.map(issue => issue.message).join(' ')));
    const view = node('button', 'plain', '核对原页'); view.type = 'button';
    view.onclick = () => { state.mode = 'pdf'; gotoPage(number).catch(error => toast(error.message, true)); };
    notice.append(view); flow.append(notice);
  }
  return { update, translationWarning, appendPageNotice };
}
