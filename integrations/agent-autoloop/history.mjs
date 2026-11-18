import { parseArgs } from 'node:util';
import { checkTaskId, localUrl, request, required } from './common.mjs';
import { larkScope, messageIdPattern } from './lark-client.mjs';
import { messagesIn } from './lark-index.mjs';

const MAX_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_PAGES = 5;
const PAGE_SIZE = 50;
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
      remote: { type: 'boolean' },
    },
  });
  const { kind, chat, remote } = values;
  if (kind !== 'single' || !chat) {
    throw new Error('必须主动指定 --kind single 与 --chat 单聊 ID');
  }
  const since = boundary(values.since, '--since');
  const until = boundary(values.until, '--until');
  if (since >= until) throw new Error('--until 必须晚于 --since');
  if (until - since > MAX_WINDOW_MS) throw new Error('时间窗最多为 7 天');
  const { chats, senders } = larkScope();
  if (!chats.has(chat)) throw new Error('当前白名单未授权读取该会话');
  if (remote && process.env.CAPGATE_LARK_REMOTE_HISTORY !== '1') throw new Error('飞书远端历史未启用');
  return { kind, chat, since, until, senders: [...senders], remote };
}

async function readOriginal(capgate, reference) {
  const id = checkTaskId(reference.task_id);
  const chain = await request(capgate, `/api/tasks/${id}`);
  const task = JSON.parse(chain.task.spec);
  const source = task.inputs?._capgate_autoloop;
  const message = task.inputs?.request;
  const identity = ['kind', 'chat', 'sender', 'message_id'];
  if (task.id !== id || source?.source !== 'capgate-lark' || source.event_id !== reference.event_id ||
      Date.parse(source.received_at) !== reference.received_ms ||
      identity.some(field => source[field] !== reference[field] || message?.[field] !== reference[field]) ||
      !Number.isSafeInteger(source.created_ms) || message.created_ms !== source.created_ms ||
      typeof message.text !== 'string' || !Array.isArray(message.attachments)) {
    throw new Error(`任务 ${id} 的消息来源与索引不一致`);
  }
  const conclusions = chain.runs.flatMap(run => {
    const metadata = typeof run.meta === 'string' ? JSON.parse(run.meta) : run.meta;
    return typeof metadata?.summary === 'string' && metadata.summary.trim()
      ? [metadata.summary.trim()]
      : [];
  });
  return {
    ...reference, created_ms: source.created_ms, text: message.text,
    attachment_count: message.attachments.length, conclusion: [...new Set(conclusions)].join('；'),
  };
}

async function readRemote({ chat, since, until, senders }) {
  const { larkClient } = await import('./lark-api.mjs');
  const { client, sdk } = larkClient();
  const authorized = new Set(senders);
  const messages = new Map();
  let pageToken;
  for (let page = 0; page < MAX_PAGES; page++) {
    const response = await client.im.v1.message.list({ params: {
      container_id_type: 'chat', container_id: chat, sort_type: 'ByCreateTimeAsc', page_size: PAGE_SIZE,
      start_time: String(Math.floor(since / 1000)), end_time: String(Math.ceil(until / 1000)),
      ...(pageToken && { page_token: pageToken }),
    } });
    if (response.code !== 0 || !response.data) {
      throw new Error(`飞书历史 API 查询失败，状态码 ${response.code ?? '未知'}`);
    }
    for (const item of response.data.items ?? []) {
      const created = Number(item.create_time);
      const sender = item.sender;
      if (item.chat_id !== chat || !messageIdPattern.test(item.message_id ?? '') || item.deleted ||
          !Number.isSafeInteger(created) || created < since || created >= until ||
          sender?.sender_type !== 'user' || sender.id_type !== 'open_id' || !authorized.has(sender.id)) continue;
      let text = '';
      if (typeof item.body?.content === 'string' && ['text', 'post'].includes(item.msg_type)) {
        const message = await sdk.normalize({
          sender: { sender_id: { open_id: sender.id }, sender_type: sender.sender_type },
          message: {
            message_id: item.message_id, chat_id: chat, chat_type: 'p2p',
            message_type: item.msg_type, create_time: item.create_time, content: item.body.content,
          },
        }, { stripBotMentions: false });
        text = message.content;
      }
      messages.set(item.message_id, {
        sender: sender.id, message_id: item.message_id, created_ms: created, type: item.msg_type, text,
      });
    }
    if (!response.data.has_more) return [...messages.values()];
    const next = response.data.page_token;
    if (!next || next === pageToken) throw new Error('飞书历史 API 未返回可用的下一页游标');
    pageToken = next;
  }
  throw new Error('时间窗内历史超过 250 条，请缩小查询范围');
}

function referenceOf(message) {
  const source = { message_id: message.message_id, sender: message.sender };
  if (message.created_ms !== undefined) source.created_at = new Date(message.created_ms).toISOString();
  if (message.received_ms !== undefined) source.received_at = new Date(message.received_ms).toISOString();
  if (message.type !== undefined) source.type = message.type;
  if (message.task_id !== undefined) source.task_id = message.task_id;
  if (message.attachment_count !== undefined) source.attachment_count = message.attachment_count;
  return source;
}

function digest(messages) {
  const summary = [];
  const todos = [];
  const recorded = new Set();
  for (const message of messages) {
    const source = referenceOf(message);
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
  let messages;
  if (selection.remote) messages = await readRemote(selection);
  else {
    const capgate = localUrl('CAPGATE_BRIDGE_URL');
    const references = messagesIn(required('CAPGATE_LARK_HISTORY_DB'), selection);
    messages = [];
    for (const reference of references) messages.push(await readOriginal(capgate, reference));
  }
  const { kind, chat, since, until } = selection;
  console.log(JSON.stringify({
    kind, chat, since: new Date(since).toISOString(), until: new Date(until).toISOString(),
    source: selection.remote ? 'feishu' : 'capgate',
    count: messages.length, messages: messages.map(referenceOf), ...digest(messages),
  }, null, 2));
} catch (error) {
  console.error(`飞书历史整理失败：${error.message}`);
  process.exitCode = 1;
}
