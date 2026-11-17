import { createHash, randomUUID } from 'node:crypto';
import { lstat, mkdir, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { WECOM_EVENT, autoloop, required, submitEvent, waitEvent } from './common.mjs';

const MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024;

class MessageError extends Error {}

function allowed(name) {
  return new Set(required(name).split(',').map(id => id.trim()).filter(Boolean));
}

function parts(body) {
  if (body.msgtype === 'mixed') return body.mixed.msg_item;
  return [body];
}

async function collect(client, directory, body, signal) {
  const text = [];
  const attachments = [];
  await mkdir(directory, { recursive: true, mode: 0o700 });
  if ((await lstat(directory)).isSymbolicLink()) throw new MessageError('附件目录不能使用符号链接');
  let bytes = 0;
  for (const item of parts(body)) {
    signal.throwIfAborted();
    if (item.msgtype === 'text' || item.msgtype === 'voice') {
      text.push(item[item.msgtype].content);
      continue;
    }
    if (!['image', 'file', 'video'].includes(item.msgtype)) {
      throw new MessageError(`暂不支持的企微消息类型 ${item.msgtype}`);
    }
    const { url, aeskey } = item[item.msgtype];
    const media = new URL(url);
    if (media.protocol !== 'https:' || media.username || media.password || !aeskey) {
      throw new MessageError('附件没有有效的加密下载地址');
    }
    const { buffer, filename } = await client.downloadFile(url, aeskey);
    bytes += buffer.length;
    if (bytes > MAX_ATTACHMENT_BYTES) throw new MessageError('单条消息附件累计超过 25 MiB');
    signal.throwIfAborted();
    const target = path.join(directory, `${attachments.length + 1}.bin`);
    const partial = `${target}.${randomUUID()}.part`;
    try {
      await writeFile(partial, buffer, { flag: 'wx', mode: 0o600 });
      await rename(partial, target);
    } finally {
      await rm(partial, { force: true });
    }
    attachments.push({ type: item.msgtype, name: filename ?? path.basename(target), path: target });
  }
  return { text: text.join('\n'), attachments };
}

async function listen() {
  const botId = required('CAPGATE_WECOM_BOT_ID');
  const secret = required('CAPGATE_WECOM_BOT_SECRET');
  const senders = allowed('CAPGATE_WECOM_SENDERS');
  if (!senders.size) throw new Error('企业微信发送者白名单不能为空');
  const groups = new Set((process.env.CAPGATE_WECOM_GROUPS ?? '').split(',').map(id => id.trim()));
  const storage = required('CAPGATE_WECOM_ATTACHMENT_DIR');
  if (!path.isAbsolute(storage)) throw new Error('企微附件目录必须是绝对路径');
  const hub = autoloop('CAPGATE_AUTOLOOP_WECOM_KEY');
  const { WSClient, WSAuthFailureError, generateReqId } = await import('@wecom/aibot-node-sdk');
  const client = new WSClient({ botId, secret, maxReconnectAttempts: -1 });
  const stopped = new AbortController();
  const active = new Map();

  async function receive(frame, digest, chat, sender) {
    const stream = generateReqId('stream');
    const signal = AbortSignal.any([stopped.signal, AbortSignal.timeout(345_000)]);
    let result;
    try {
      await client.replyStream(frame, stream, '已收到请求，正在等待执行。', false);
      const body = frame.body;
      const message = await collect(client, path.join(storage, digest), body, signal);
      const detail = JSON.stringify({ ...message, sender, chat, message_id: body.msgid });
      if (detail.length > 4000) {
        throw new MessageError('消息和附件索引合计超过 agent-autoloop 的 4000 字限制');
      }
      const id = `wecom:${digest}`;
      const { event } = await submitEvent(hub, {
        id, type: WECOM_EVENT, subject: id, detail,
      }, signal);
      const run = await waitEvent(hub, event, signal);
      result = run.state === 'done' && run.summary
        ? run.summary
        : `事件 ${event} 未完成，请查看 agent-autoloop 运行记录 ${run.run}。`;
    } catch (error) {
      if (signal.aborted) result = '等待任务超时，请在本机查看执行状态。';
      else if (error instanceof MessageError) result = error.message;
      else result = '任务提交或状态查询失败，请在本机查看运行记录。';
    }
    if (!stopped.signal.aborted) await client.replyStream(frame, stream, result, true);
  }

  client.on('message', frame => {
    if (stopped.signal.aborted) return;
    const body = frame.body;
    const sender = body?.from?.userid;
    const chat = body?.chattype === 'single' ? sender : body?.chatid;
    const permittedChat = body?.chattype === 'single' ||
      (body?.chattype === 'group' && chat && groups.has(chat));
    if (!sender || !chat || !body.msgid || body.aibotid !== botId || !permittedChat || !senders.has(sender)) {
      client.replyStream(frame, generateReqId('stream'), '此账号或会话没有任务提交权限。', true)
        .catch(error => console.error(`企微拒绝回执未送达：${error.message}`));
      return;
    }
    const digest = createHash('sha256').update(JSON.stringify([botId, chat, sender, body.msgid])).digest('hex');
    if (active.has(digest)) return;
    const pending = receive(frame, digest, chat, sender)
      .catch(error => console.error(`企微任务回执未送达：${error.message}`))
      .finally(() => active.delete(digest));
    active.set(digest, pending);
  });

  client.on('authenticated', () => console.log('企业微信智能机器人已连接。'));
  client.on('disconnected', () => {
    console.error('企业微信连接已断开。断线期间未收到确认的消息可能未派发或未收到回复，请先核查运行记录。');
  });
  client.on('reconnecting', attempt => console.error(`企业微信正在第 ${attempt} 次重连。`));
  const shutdown = new Promise((resolve, reject) => {
    process.once('SIGINT', resolve);
    process.once('SIGTERM', resolve);
    client.on('error', error => {
      if (error instanceof WSAuthFailureError) {
        reject(new Error('企业微信机器人认证失败，请检查机器人 ID 和密钥。'));
      } else {
        console.error('企业微信连接报错，请检查机器人配置和网络。');
      }
    });
  });
  client.connect();
  try {
    await shutdown;
  } finally {
    stopped.abort();
    client.disconnect();
    await Promise.allSettled(active.values());
  }
}

try {
  if (process.env.CAPGATE_WECOM_ENABLED === '1') await listen();
  else console.log('企业微信入口未启用。');
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
}
