import { createHash, randomUUID } from 'node:crypto';
import { RERUN_EVENT, autoloop, checkTaskId, submitEvent, waitEvent } from './common.mjs';

try {
  const [id, requestId = randomUUID()] = process.argv.slice(2);
  const task = checkTaskId(id);
  if (!/^[a-zA-Z0-9_-]{1,80}$/.test(requestId)) throw new Error('重跑请求编号格式无效');
  const hub = autoloop('CAPGATE_AUTOLOOP_OPERATOR_KEY');
  const signal = AbortSignal.timeout(360_000);
  const idempotency = createHash('sha256').update(JSON.stringify([task, requestId])).digest('hex');
  const key = `rerun:${idempotency}`;
  const { event } = await submitEvent(hub, {
    id: key,
    type: RERUN_EVENT,
    subject: key,
    detail: task,
  }, signal);
  console.error(`agent-autoloop 事件 ${event}，重跑请求 ${requestId}`);
  const run = await waitEvent(hub, event, signal);
  if (run.state !== 'done' || !run.summary) {
    throw new Error(`事件 ${event} 未完成，请查看 agent-autoloop 运行记录 ${run.run}`);
  }
  console.log(run.summary);
} catch (error) {
  console.error(`重跑失败：${error.message}`);
  process.exitCode = 1;
}
