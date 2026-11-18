import { setTimeout } from 'node:timers/promises';

export const RERUN_EVENT = 'capgate.rerun';
export const LARK_EVENT = 'capgate.lark.message';

export function required(name) {
  const value = process.env[name];
  if (!value?.trim()) throw new Error(`请设置 ${name}`);
  return value;
}

export function localUrl(name) {
  const base = new URL(required(name));
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password ||
      !['127.0.0.1', '[::1]', 'localhost'].includes(base.hostname)) {
    throw new Error(`${name} 必须是本机 HTTP 服务地址`);
  }
  return base;
}

export async function request(base, path, { body, headers, signal } = {}) {
  const timeout = AbortSignal.timeout(10_000);
  const response = await fetch(new URL(path, base), {
    method: body === undefined ? 'GET' : 'POST',
    redirect: 'error',
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    headers: { ...(body === undefined ? {} : { 'content-type': 'application/json' }), ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`${path} 返回 HTTP ${response.status}`);
  return response.json();
}

export function autoloop(tokenName) {
  const base = localUrl('CAPGATE_AUTOLOOP_URL');
  const headers = { Authorization: `Bearer ${required(tokenName)}` };
  return (path, options = {}) => request(base, path, { ...options, headers });
}

export async function submitEvent(hub, body, signal) {
  const receipt = await hub('/v1/events', { body, signal });
  if (!Number.isInteger(receipt.event)) throw new Error('agent-autoloop 未返回事件编号');
  if (!receipt.routed?.length) throw new Error(`事件 ${receipt.event} 没有匹配的 agent-autoloop 规则`);
  return receipt;
}

export async function waitEvent(hub, event, signal) {
  while (!signal.aborted) {
    const state = await hub(`/v1/events/${event}`, { signal });
    if (state.runs.length > 1) throw new Error(`事件 ${event} 匹配了多轮，请检查规则`);
    const run = state.runs[0];
    if (run && ['done', 'nothing', 'cancelled'].includes(run.state)) return run;
    await setTimeout(1000, undefined, { signal });
  }
  signal.throwIfAborted();
}

export function checkTaskId(id) {
  if (typeof id !== 'string' || !/^task_[a-z0-9]+$/.test(id)) {
    throw new Error('任务编号格式无效');
  }
  return id;
}

export async function waitTask(capgate, id, signal) {
  while (!signal.aborted) {
    const chain = await request(capgate, `/api/tasks/${id}`, { signal });
    if (chain.task?.status && chain.task.status !== 'running') return chain;
    await setTimeout(500, undefined, { signal });
  }
  signal.throwIfAborted();
}
