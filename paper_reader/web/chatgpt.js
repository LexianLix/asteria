// Model connection only; no application accounts or credentials in browser storage.
export function installChatGPT({ state, $, node, api, toast }) {
  const option = node('option', '', 'ChatGPT · 账号套餐授权'); option.value = 'chatgpt'; $('provider').append(option);
  const panel = node('section'); panel.id = 'chatgpt-panel'; panel.hidden = true;
  panel.innerHTML = `<h3>使用你的 ChatGPT 套餐</h3><p class="muted">在系统浏览器登录并授权此应用。登录不发送论文；翻译、问答仍需确认发送。用量受你的套餐与本应用权限限制。</p>
    <label>ChatGPT 连接<select id="chatgpt-account"><option value="">添加 ChatGPT 连接</option></select></label>
    <div class="chatgpt-actions"><button type="button" id="chatgpt-signin" class="chatgpt-signin">Continue with ChatGPT</button><button type="button" id="chatgpt-reconsent" class="secondary" hidden>授权套餐使用</button><button type="button" id="chatgpt-cancel" class="secondary" hidden>取消登录</button><button type="button" id="chatgpt-logout" class="plain" hidden>退出所选连接</button></div>
    <p id="chatgpt-status" class="muted" role="status" aria-live="polite"></p><div id="chatgpt-plan-row" hidden><span>Using ChatGPT plan · 正在使用你的套餐</span><button type="button" id="chatgpt-usage" class="plain">管理 ChatGPT 用量</button></div>`;
  $('key-label').before(panel);
  const welcome = node('dialog'); welcome.id = 'chatgpt-welcome';
  welcome.innerHTML = '<h2>正在使用你的 ChatGPT 套餐</h2><p class="muted">此应用的合资格 AI 请求将使用你的套餐或已授权额度。你可以在 ChatGPT 设置中管理应用权限与用量；论文仍只在你明确确认后发送。</p><button type="button" id="chatgpt-welcome-dismiss" class="primary">Got it · 知道了</button>';
  document.body.append(welcome);
  let status = null, timer = null, sequence = 0, busy = false, welcomeId = null;
  const active = () => status?.connection?.accounts.find(account => account.id === status.connection.active);
  const current = () => $('settings-dialog').open && $('provider').value === 'chatgpt';
  function render() {
    const account = active(), waiting = ['waiting', 'exchanging'].includes(status?.status);
    const select = $('chatgpt-account'); select.replaceChildren();
    for (const entry of status?.connection?.accounts || []) {
      const option = node('option', '', `${entry.label} · ${entry.client_suffix}${entry.signed_in ? '' : ' · 需登录'}`); option.value = entry.id; select.append(option);
    }
    const add = node('option', '', '添加其他 ChatGPT 连接'); add.value = ''; select.append(add);
    select.value = status?.connection?.active || '';
    $('chatgpt-cancel').hidden = !waiting;
    $('chatgpt-signin').disabled = waiting || busy;
    $('chatgpt-account').disabled = waiting || busy;
    $('chatgpt-logout').hidden = !account?.signed_in;
    $('chatgpt-reconsent').hidden = !account?.signed_in || account.plan_authorized;
    $('chatgpt-plan-row').hidden = !account?.plan_authorized;
    $('chatgpt-status').textContent = waiting ? status.message : account?.plan_authorized ? '已连接并授权套餐。获取模型列表后选择模型，再试译确认。' : account?.signed_in ? '已登录，但未授权使用套餐。请明确授权后再调用模型。' : status?.message || '尚未连接 ChatGPT。请在系统浏览器登录。';
    if (status?.status === 'failed') $('chatgpt-status').textContent = status.message;
    if (account?.needs_welcome && !welcome.open && welcomeId !== account.id) {
      welcomeId = account.id; welcome.showModal();
    }
  }
  async function refresh() {
    clearTimeout(timer); const request = ++sequence;
    const value = await api('/desktop/chatgpt/status');
    if (request !== sequence || !current()) return;
    status = value; render();
    if (['waiting', 'exchanging'].includes(value.status)) timer = setTimeout(() => refresh().catch(error => { $('chatgpt-status').textContent = error.message; }), 750);
  }
  function syncProvider() {
    const selected = $('provider').value === 'chatgpt'; panel.hidden = !selected;
    $('base-url').readOnly = selected;
    $('key-label').hidden = $('provider').value !== 'openai';
    if (selected) { $('base-url').value = 'https://api.openai.com/v1'; $('api-key').value = ''; }
    else { clearTimeout(timer); sequence++; }
  }
  async function signIn(reconsent = false) {
    busy = true; render();
    try {
      await api('/desktop/chatgpt/login', 'POST', { registration_id: $('chatgpt-account').value || null, reconsent });
      await refresh();
    } catch (error) { status = { ...(status || {}), status: 'failed', message: error.message }; $('chatgpt-status').textContent = error.message; }
    finally { busy = false; if (status) render(); }
  }
  $('chatgpt-signin').onclick = () => signIn();
  $('chatgpt-reconsent').onclick = () => signIn(true);
  $('chatgpt-cancel').onclick = async () => { try { await api('/desktop/chatgpt/cancel', 'POST'); await refresh(); } catch (error) { toast(error.message, true); } };
  $('chatgpt-logout').onclick = async () => {
    if (!confirm('退出所选 ChatGPT 连接？论文与笔记保留，新 AI 请求将停止。')) return;
    try { const result = await api('/desktop/chatgpt/logout', 'POST'); toast(result.message); $('model').value = ''; $('model-options').replaceChildren(); await refresh(); }
    catch (error) { toast(error.message, true); }
  };
  $('chatgpt-account').onchange = async () => {
    if (!$('chatgpt-account').value) return;
    try { await api('/desktop/chatgpt/select', 'POST', { registration_id: $('chatgpt-account').value }); $('model').value = ''; $('model-options').replaceChildren(); await refresh(); }
    catch (error) { toast(error.message, true); }
  };
  $('chatgpt-usage').onclick = async () => { try { await api('/desktop/chatgpt/usage', 'POST'); } catch (error) { toast(error.message, true); } };
  $('chatgpt-welcome-dismiss').onclick = async () => {
    try { await api('/desktop/chatgpt/welcome', 'POST', { registration_id: welcomeId }); welcome.close(); await refresh(); }
    catch (error) { toast(error.message, true); }
  };
  welcome.addEventListener('cancel', event => { event.preventDefault(); $('chatgpt-welcome-dismiss').click(); });
  $('settings-dialog').addEventListener('close', () => { clearTimeout(timer); sequence++; });
  return { syncProvider, refresh };
}
