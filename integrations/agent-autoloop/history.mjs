import { parseArgs } from 'node:util';
import { checkTaskId, localUrl, request, required } from './common.mjs';
import { messagesIn } from './wecom-index.mjs';

const timePattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})$/u;
const todoPattern = /^(?:[-*]\s*|\d+[.、]\s*)?(?:待办|行动项|下一步|请\s*\S|需要\s*\S|必须\s*\S|由\S+负责)/u;

function boundary(text, name) {
  const ms = Date.parse(text);
  if (!timePattern.test(text ?? '') || !Number.isFinite(ms)) {
    throw new Error(`${name} 必须是含时区的日期时间，如 2026-11-18T09:00:00+08:00`);
  }
  return ms;
}

function scope() {
  const { values } = parseArgs({
    options: {
      kind: { type: 'string' }, chat: { type: 'string' },
      since: { type: 'string' }, until: { type: 'string' },
    },
  });
  const { kind, chat } = values;
  if (!['single', 'group'].includes(kind) || !chat) {
    throw new Error('必须主动指定 --kind single|group 与 --chat 会话 ID');
  }
  const since = boundary(values.since, '--since');
  const until = boundary(values.until, '--until');
  if (since >= until) throw new Error('--until 必须晚于 --since');
  const senders = required('CAPGATE_WECOM_SENDERS').split(',').map(id => id.trim()).filter(Boolean);
  if (!senders.length) throw new Error('企业微信发送者白名单不能为空');
  const groups = (process.env.CAPGATE_WECOM_GROUPS ?? '').split(',').map(id => id.trim());
  if ((kind === 'single' && !senders.includes(chat)) || (kind === 'group' && !groups.includes(chat))) {
    throw new Error('当前白名单未授权读取该会话');
  }
  return { kind, chat, since, until, senders: kind === 'single' ? [chat] : senders };
}

async function readOriginal(capgate, reference) {
  const id = checkTaskId(reference.task_id);
  const chain = await request(capgate, `/api/tasks/${id}`);
  const task = JSON.parse(chain.task.spec);
  const source = task.inputs?._capgate_autoloop;
  const message = task.inputs?.request;
  const identity = ['kind', 'chat', 'sender', 'message_id'];
  if (task.id !== id || source?.source !== 'capgate-wecom' || source.event_id !== reference.event_id ||
      Date.parse(source.received_at) !== reference.received_ms ||
      identity.some(field => source[field] !== reference[field] || message?.[field] !== reference[field]) ||
      typeof message?.text !== 'string') {
    throw new Error(`任务 ${id} 的消息来源与索引不一致`);
  }
  const conclusions = chain.runs.flatMap(run => {
    const metadata = typeof run.meta === 'string' ? JSON.parse(run.meta) : run.meta;
    return typeof metadata?.summary === 'string' && metadata.summary.trim()
      ? [metadata.summary.trim()]
      : [];
  });
  return { ...reference, text: message.text, conclusion: [...new Set(conclusions)].join('；') };
}

function digest(messages) {
  const summary = [];
  const todos = [];
  const recorded = new Set();
  for (const message of messages) {
    const source = {
      message_id: message.message_id, sender: message.sender,
      received_at: new Date(message.received_ms).toISOString(),
    };
    const sentences = message.text.split(/[。！？；\r\n]+/u).map(text => text.trim()).filter(Boolean);
    const excerpt = message.conclusion || sentences[0];
    if (excerpt) {
      const text = excerpt.replace(/\s+/gu, ' ');
      const characters = [...text];
      summary.push({ ...source, text: characters.length > 200 ? `${characters.slice(0, 199).join('')}…` : text });
    }
    for (const text of sentences) {
      const key = JSON.stringify([message.sender, text]);
      if (!todoPattern.test(text) || recorded.has(key)) continue;
      recorded.add(key);
      todos.push({ ...source, text });
    }
  }
  return { summary, todos };
}

try {
  const selection = scope();
  const capgate = localUrl('CAPGATE_BRIDGE_URL');
  const references = messagesIn(required('CAPGATE_WECOM_HISTORY_DB'), selection);
  const messages = [];
  for (const reference of references) messages.push(await readOriginal(capgate, reference));
  const { kind, chat, since, until } = selection;
  console.log(JSON.stringify({
    kind, chat, since: new Date(since).toISOString(), until: new Date(until).toISOString(),
    count: messages.length, ...digest(messages),
  }, null, 2));
} catch (error) {
  console.error(`企微历史整理失败：${error.message}`);
  process.exitCode = 1;
}
