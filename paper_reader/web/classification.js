export function installClassification({ $, api, toast }) {
  const section = document.createElement('section');
  section.innerHTML = '<h3>AI 摘要与分类</h3><p id="classification-state" class="muted">默认关闭；规则分类不受影响。</p><button type="button" id="classification-settings" class="secondary">配置自动分类</button>';
  $('daily-time').closest('label').nextElementSibling.after(section);
  const dialog = document.createElement('dialog');
  dialog.id = 'classification-dialog';
  dialog.innerHTML = '<form id="classification-form"><div class="dialog-header"><div><p class="eyebrow">METADATA ONLY</p><h2>摘要与分类授权</h2></div><button type="button" id="classification-close" class="icon-button" aria-label="关闭分类授权">×</button></div><p id="classification-target" class="muted"></p><h3>发送范围</h3><p id="classification-scope"></p><p id="classification-automatic" class="muted"></p><p class="muted">使用已保存的模型设置；尚未保存的设置不生效。远程请求可能消耗套餐额度或产生接口费用，不自动更换服务或计费方式。</p><button type="button" id="classification-usage" class="plain" hidden>管理 ChatGPT 用量</button><label class="check-label"><input id="classification-enabled" type="checkbox"> 允许采集后自动生成中文摘要与标签</label><p id="classification-pending" class="muted"></p><p id="classification-error" class="form-error"></p><div class="model-tests"><button type="button" id="classification-run" class="secondary">补齐摘要与分类</button></div><div class="dialog-actions"><button type="button" id="classification-cancel" class="secondary">取消</button><button type="submit" class="primary">确认授权设置</button></div></form>';
  document.body.append(dialog);
  let preview = null;
  async function refresh() {
    const status = await api('/desktop/classification');
    $('classification-state').textContent = status.enabled ? '已授权使用保存的模型；每次最多 30 篇。'
      : status.paused ? '分类已暂停，不会继续自动发送；请检查用量或连接后重新授权。'
      : status.needs_confirmation ? '模型连接已变化，需要重新核对发送范围。' : '默认关闭；仅使用规则分类，不发送摘要。';
    return status;
  }
  $('classification-settings').onclick = async () => {
    try {
      preview = await api('/desktop/classification/preview', 'POST', {});
      $('classification-target').textContent = `${preview.provider === 'chatgpt' ? 'Using ChatGPT plan · ' : ''}${preview.connection_label} · ${preview.model}\n${preview.base_url}`;
      $('classification-scope').textContent = preview.scope;
      $('classification-automatic').textContent = preview.automatic_scope;
      $('classification-enabled').checked = preview.enabled;
      $('classification-usage').hidden = preview.provider !== 'chatgpt';
      $('classification-run').disabled = !preview.enabled;
      $('classification-pending').textContent = `待处理 ${preview.pending} 篇；已完成摘要不会重复请求。`;
      $('classification-error').textContent = preview.job?.error || '';
      dialog.showModal();
    } catch (error) { toast(error.message, true); }
  };
  $('classification-form').onsubmit = async event => {
    event.preventDefault();
    try {
      await api('/desktop/classification/consent', 'POST', { enabled: $('classification-enabled').checked,
        confirmed: true, preview_id: preview?.preview_id });
      await refresh(); dialog.close(); toast('分类授权设置已保存');
    } catch (error) { $('classification-error').textContent = error.message; }
  };
  $('classification-run').onclick = async () => {
    try {
      const job = await api('/desktop/classification/run', 'POST', {});
      if (job.status === 'disabled') throw new Error('请先重新授权分类');
      $('classification-pending').textContent = '分类任务已启动，可在任务中心查看；不会发送笔记或 PDF。';
    } catch (error) { $('classification-error').textContent = error.message; }
  };
  for (const id of ['classification-close', 'classification-cancel']) $(id).onclick = () => dialog.close();
  $('classification-usage').onclick = async () => {
    try { await api('/desktop/chatgpt/usage', 'POST', {}); } catch (error) { $('classification-error').textContent = error.message; }
  };
  return { refresh };
}
