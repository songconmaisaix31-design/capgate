import { readFile } from 'node:fs/promises';
import { LARK_EVENT, RERUN_EVENT, checkTaskId, localUrl, request, required, waitTask } from './common.mjs';

const modeToRoute = {
  rerun: { source: 'capgate-operator', type: RERUN_EVENT },
  lark: { source: 'capgate-lark', type: LARK_EVENT },
};

function plainLine(value) {
  return typeof value === 'string' ? value.replace(/\s+/gu, ' ').trim() : '';
}

function taskSummary(chain, id) {
  const { status } = chain.task;
  const outcome = { ok: '成功', failed: '失败', degraded: '部分完成' }[status] ?? status;
  const summaries = new Set();
  for (const run of chain.runs) {
    const metadata = typeof run.meta === 'string' ? JSON.parse(run.meta) : run.meta;
    const summary = plainLine(metadata?.summary);
    if (summary) summaries.add(summary);
  }
  const parts = [`capgate 任务 ${id} 已结束，状态 ${outcome}`];
  if (status !== 'ok') {
    const reason = plainLine(chain.task.error) || plainLine(chain.runs.findLast(run => run.error)?.error);
    if (reason) parts.push(`原因 ${reason}`);
  }
  if (summaries.size) parts.push(`结论 ${[...summaries].join('；')}`);
  const artifacts = chain.artifacts.map(artifact => artifact.name).join('、');
  if (artifacts) parts.push(`产物 ${artifacts}`);

  const result = parts.join('。');
  if (result.length <= 500) return result;
  return `${result.slice(0, 499).replace(/[\ud800-\udbff]$/u, '')}…`;
}

async function taskBody(capgate, mode, event, env, tags, signal) {
  if (mode === 'rerun') {
    const id = checkTaskId(event.detail);
    const { task } = await request(capgate, `/api/tasks/${id}`, { signal });
    if (!task?.spec) throw new Error(`任务 ${id} 没有原始规格`);
    if (task.status === 'running') throw new Error(`任务 ${id} 仍在运行`);
    const { id: _, ...original } = JSON.parse(task.spec);
    return { spec: original, source: { rerun_of: id } };
  }
  if (!env || !tags.length) throw new Error('飞书规则须指定 capgate 环境和至少一个标签');
  const input = JSON.parse(event.detail);
  if (typeof input.text !== 'string' || typeof input.sender !== 'string' ||
      typeof input.message_id !== 'string' || typeof input.chat !== 'string' ||
      input.kind !== 'single' || !Number.isSafeInteger(input.created_ms) ||
      !Array.isArray(input.attachments)) {
    throw new Error('飞书事件资料不完整');
  }
  return {
    spec: {
      goal: '处理授权的飞书消息。根据 request.text 和附件引用完成任务，保留来源。',
      tags,
      hint_envs: [env],
      inputs: { request: input, question: input.text },
    },
    source: {
      sender: input.sender, message_id: input.message_id, chat: input.chat,
      kind: input.kind, received_at: event.receivedAt, created_ms: input.created_ms,
    },
  };
}

try {
  const [mode, env, ...tags] = process.argv.slice(2);
  const route = modeToRoute[mode];
  if (!route) throw new Error('命令模式必须是 rerun 或 lark');
  const { events } = JSON.parse(await readFile(required('AGENT_AUTOLOOP_CONTEXT'), 'utf8'));
  if (!Array.isArray(events) || events.length !== 1) throw new Error('每轮必须恰好有一个来源事件');
  const [event] = events;
  if (event.source !== route.source || event.type !== route.type || !event.id) {
    throw new Error('事件来源或类型与命令权限不符');
  }
  const capgate = localUrl('CAPGATE_BRIDGE_URL');
  const signal = AbortSignal.timeout(330_000);
  const { spec, source } = await taskBody(capgate, mode, event, env, tags, signal);
  const provenance = {
    ...source,
    source: event.source,
    event_id: event.id,
    run: required('AGENT_AUTOLOOP_RUN'),
  };
  spec.inputs = { ...spec.inputs, _capgate_autoloop: provenance };
  const created = await request(capgate, '/api/tasks', {
    body: spec,
    headers: { 'Idempotency-Key': `autoloop:${mode}:${event.id}` },
    signal,
  });
  const id = checkTaskId(created.id);
  const indexFile = process.env.CAPGATE_LARK_HISTORY_DB;
  if (mode === 'lark' && indexFile) {
    const { indexMessage } = await import('./lark-index.mjs');
    indexMessage(indexFile, provenance, id);
  }
  const chain = await waitTask(capgate, id, signal);
  console.log(taskSummary(chain, id));
} catch (error) {
  console.error(`capgate 命令失败：${error.message}`);
  process.exitCode = 1;
}
