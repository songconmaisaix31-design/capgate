import { readFile } from 'node:fs/promises';
import { RERUN_EVENT, WECOM_EVENT, checkTaskId, localUrl, request, required, waitTask } from './common.mjs';

const modeToRoute = {
  rerun: { source: 'capgate-operator', type: RERUN_EVENT },
  wecom: { source: 'capgate-wecom', type: WECOM_EVENT },
};

async function taskBody(capgate, mode, event, env, tags, signal) {
  if (mode === 'rerun') {
    const id = checkTaskId(event.detail);
    const { task } = await request(capgate, `/api/tasks/${id}`, { signal });
    if (!task?.spec) throw new Error(`任务 ${id} 没有原始规格`);
    if (task.status === 'running') throw new Error(`任务 ${id} 仍在运行`);
    const { id: _, ...original } = JSON.parse(task.spec);
    return { spec: original, source: { rerun_of: id } };
  }
  if (!env || !tags.length) throw new Error('企微规则须指定 capgate 环境和至少一个标签');
  const input = JSON.parse(event.detail);
  if (typeof input.text !== 'string' || typeof input.sender !== 'string' ||
      typeof input.message_id !== 'string' || typeof input.chat !== 'string' ||
      !Array.isArray(input.attachments)) {
    throw new Error('企微事件资料不完整');
  }
  return {
    spec: {
      goal: '处理授权的企业微信消息。根据 request.text 和附件引用完成任务，保留来源。',
      tags,
      hint_envs: [env],
      inputs: { request: input },
    },
    source: { sender: input.sender, message_id: input.message_id, chat: input.chat },
  };
}

try {
  const [mode, env, ...tags] = process.argv.slice(2);
  const route = modeToRoute[mode];
  if (!route) throw new Error('命令模式必须是 rerun 或 wecom');
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
  const chain = await waitTask(capgate, id, signal);
  const { status } = chain.task;
  const outcome = { ok: '成功', failed: '失败', degraded: '部分完成' }[status] ?? status;
  const artifactNames = chain.artifacts.map(artifact => artifact.name).join('、');
  const detail = artifactNames ? `，产物 ${artifactNames}` : '';
  console.log(`capgate 任务 ${id} 已结束，状态 ${outcome}${detail}`);
} catch (error) {
  console.error(`capgate 命令失败：${error.message}`);
  process.exitCode = 1;
}
