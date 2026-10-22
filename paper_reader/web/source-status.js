// Pure display state; expiry only re-enables a manual action, never starts one.
export function sourceSyncControl(job = {}, cooldown = null, now = Date.now()) {
  const deadline = cooldown ? Date.parse(cooldown.retry_at) : NaN;
  const waiting = !!cooldown && (!Number.isFinite(deadline) || deadline > now);
  const running = job.status === 'running';
  return {
    disabled: running || waiting,
    label: running ? '正在获取…' : waiting ? '来源限流，等待重试' : '↻ 获取新论文',
    title: waiting ? (Number.isFinite(deadline)
      ? `来源请求受限，最早可在 ${new Date(deadline).toLocaleString('zh-CN')}（本地时间）再试；到期不会自动重发本次请求。`
      : '来源冷却记录异常，请刷新后检查；不会发送请求。') : '',
  };
}
