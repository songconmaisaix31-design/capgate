import { createHash } from 'node:crypto';
import { Agent } from 'node:https';
import path from 'node:path';
import { parseArgs } from 'node:util';
import { autoloop, LARK_EVENT, required, submitEvent, UnroutedEventError, waitEvent } from './common.mjs';
import { larkScope, MESSAGE_EVENT, messageIdPattern, receivedMessage } from './lark-client.mjs';
import { openInbox } from './lark-inbox.mjs';
import { prepareIndex } from './lark-index.mjs';
import { MessageError, saveAttachments } from './lark-attachments.mjs';

const SUPPORTED = new Set(['text', 'post', 'image', 'file', 'audio', 'media']);

class ReconnectTestAgent extends Agent {
  createConnection(...args) {
    this.socket = super.createConnection(...args);
    return this.socket;
  }
}

function digest(...values) {
  return createHash('sha256').update(JSON.stringify(values)).digest('hex');
}

async function reply(client, id, key, stage, text) {
  const response = await client.im.v1.message.reply({
    path: { message_id: id },
    data: { msg_type: 'text', content: JSON.stringify({ text }), uuid: digest(key, stage).slice(0, 32) },
  });
  if (response.code !== 0 || !response.data?.message_id) {
    throw new Error(`飞书回复失败，API 状态码 ${response.code ?? '未知'}`);
  }
}

function runtime({ client, scope, storage, inbox, hub, stopped }) {
  const active = new Map();

  async function dispatch(row, message) {
    const key = row.digest;
    const { text, resources, error, ...identity } = message;
    const signal = AbortSignal.any([stopped.signal, AbortSignal.timeout(345_000)]);
    if (error) return error;
    let event = row.event;
    if (event === null) {
      const attachments = await saveAttachments(client, path.join(storage, key), identity.message_id, resources, signal);
      const detail = JSON.stringify({ ...identity, text, attachments });
      if (detail.length > 4000) throw new MessageError('消息和附件索引合计超过 4000 字限制。');
      const id = `lark:${key}`;
      ({ event } = await submitEvent(hub, { id, type: LARK_EVENT, subject: id, detail }, signal));
      inbox.submitted(key, event);
      console.log(`飞书消息 ${identity.message_id} 已提交，agent-autoloop 事件 ${event}。`);
      await reply(client, identity.message_id, key, 'accepted', `已提交任务，事件编号 ${event}。`)
        .catch(() => console.error(`飞书事件 ${event} 的接收回执未送达，将继续处理并发送结果。`));
    }
    const run = await waitEvent(hub, event, signal);
    if (run.state === 'done' && run.summary) return run.summary;
    const state = { done: '执行结束', nothing: '没有可执行的任务', cancelled: '处理已停止' }[run.state];
    const reason = typeof run.error === 'string' && run.error.trim() ? run.error : run.summary;
    const detail = typeof reason === 'string' && reason.trim() ? `原因 ${reason.trim()}` : '';
    const response = `事件 ${event} ${state}，agent-autoloop 运行编号 ${run.run}。${detail}`;
    return response.length <= 500 ? response : `${response.slice(0, 499).replace(/[\ud800-\udbff]$/u, '')}…`;
  }

  async function deliver(row) {
    const message = JSON.parse(row.payload);
    if (!scope.chats.has(message.chat) || !scope.senders.has(message.sender)) {
      inbox.complete(row.digest);
      return;
    }
    const result = row.result ?? await dispatch(row, message).catch(error => {
      if (error instanceof MessageError || error instanceof UnroutedEventError) return error.message;
      throw error;
    });
    stopped.signal.throwIfAborted();
    if (row.result === null) inbox.result(row.digest, result);
    await reply(client, message.message_id, row.digest, 'result', result);
    inbox.complete(row.digest);
    console.log(`飞书消息 ${message.message_id} 的终态回执已发送。`);
  }

  return {
    pump() {
      if (active.size >= 2 || stopped.signal.aborted) return;
      for (const row of inbox.pending(2)) {
        if (active.has(row.digest)) continue;
        const job = deliver(row).catch(() => {
          if (stopped.signal.aborted) return;
          inbox.retry(row.digest);
          console.error(`飞书收件 ${row.digest} 暂未处理完成，30 秒后重试。`);
        }).finally(() => active.delete(row.digest));
        active.set(row.digest, job);
        if (active.size >= 2) break;
      }
    },
    idle: () => Promise.allSettled(active.values()),
  };
}

async function connect(values, options, sdk, handler, onConnected) {
  const { bootstrap, 'verify-reconnect': verify } = values;
  const agent = verify ? new ReconnectTestAgent() : undefined;
  const { promise, resolve, reject } = Promise.withResolvers();
  const startup = setTimeout(() => reject(new Error('飞书长连接验收超时')), verify ? 240_000 : 45_000);
  const stop = () => resolve();
  const dispatcher = new sdk.EventDispatcher({ logger: options.logger }).register({ [MESSAGE_EVENT]: handler });
  const socket = new sdk.WSClient({
    ...options, agent, autoReconnect: true, handshakeTimeoutMs: 10_000, wsConfig: { pingTimeout: 10 },
    onReady() {
      console.log('飞书机器人长连接已建立。');
      if (!verify) clearTimeout(startup);
      onConnected?.();
      if (agent) {
        if (!agent.socket) return reject(new Error('未找到测试进程建立的 TLS 连接'));
        console.log('正在终止本测试进程的 TLS 连接，等待 SDK 自动重连。');
        agent.socket.destroy();
      }
    },
    onReconnecting() {
      console.error('飞书长连接已断开，SDK 正在重连。未落盘的消息可能未派发，可从白名单单聊历史核对。');
    },
    onReconnected() {
      console.log('飞书机器人已自动重连。');
      onConnected?.();
      if (bootstrap && verify) resolve();
    },
    onError() { reject(new Error('飞书长连接无法继续，请检查应用配置或网络。')); },
  });
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);
  try {
    await Promise.race([socket.start({ eventDispatcher: dispatcher }).then(() => promise), promise]);
  } finally {
    clearTimeout(startup);
    process.removeListener('SIGINT', stop);
    process.removeListener('SIGTERM', stop);
    socket.close({ force: true });
    agent?.destroy();
  }
}

async function main() {
  const { values } = parseArgs({ options: {
    bootstrap: { type: 'boolean' }, 'verify-reconnect': { type: 'boolean' }, identify: { type: 'boolean' },
  } });
  if (values.identify && (!values.bootstrap || values['verify-reconnect'])) {
    throw new Error('--identify 仅可与 --bootstrap 单独使用');
  }
  if (values['verify-reconnect'] && !values.bootstrap) throw new Error('--verify-reconnect 需要 --bootstrap');
  if (!values.bootstrap && process.env.CAPGATE_LARK_ENABLED !== '1') {
    console.log('飞书入口未启用。');
    return;
  }
  const { larkClient } = await import('./lark-api.mjs');
  if (values.bootstrap) {
    const challenge = values.identify ? required('CAPGATE_LARK_IDENTIFY_TEXT') : null;
    if (challenge && challenge.length < 32) throw new Error('领取短语至少需要 32 个字符');
    const { sdk, options } = larkClient();
    let identified = false;
    await connect(values, options, sdk, data => {
      if (!challenge || identified || data.message?.message_type !== 'text') return;
      const identity = receivedMessage(data, options.appId);
      if (!identity || JSON.parse(data.message.content).text !== challenge) return;
      identified = true;
      console.log(JSON.stringify({ sender: identity.sender, chat: identity.chat }));
    });
    return;
  }
  const scope = larkScope();
  const attachmentDir = required('CAPGATE_LARK_ATTACHMENT_DIR');
  if (!path.isAbsolute(attachmentDir)) throw new Error('飞书附件目录必须是绝对路径');
  const hub = autoloop('CAPGATE_AUTOLOOP_LARK_KEY');
  const { client, options, sdk } = larkClient();
  const stopped = new AbortController();
  const indexFile = process.env.CAPGATE_LARK_HISTORY_DB;
  if (indexFile) prepareIndex(indexFile);
  const storage = path.join(attachmentDir, digest(options.appId).slice(0, 24));
  const inbox = openInbox(storage);
  const queue = runtime({ client, scope, storage, inbox, hub, stopped });
  const tick = setInterval(queue.pump, 3000);
  async function accept(data) {
    try {
      if (stopped.signal.aborted) return;
      const identity = receivedMessage(data, options.appId, scope);
      if (!identity) return;
      const type = data.message.message_type;
      const key = digest(options.appId, identity.message_id);
      if (!SUPPORTED.has(type)) {
        inbox.add(key, { ...identity, error: '此入口支持文本、富文本、图片、文件、音频和视频。' });
      } else {
        const message = await sdk.normalize(data, { stripBotMentions: false });
        const missingResource = !['text', 'post'].includes(type) && !message.resources.length;
        const error = missingResource ? '飞书消息缺少可下载的附件。' : undefined;
        inbox.add(key, { ...identity, text: message.content, resources: message.resources, error });
      }
      queue.pump();
    } catch (error) {
      const id = data?.message?.message_id;
      const hint = typeof id === 'string' && messageIdPattern.test(id) ? ` ${id}` : '';
      console.error(`飞书接收事件${hint} 处理失败，请检查本机收件队列。`);
      throw error;
    }
  }
  try {
    await connect(values, options, sdk, accept, queue.pump);
  } finally {
    clearInterval(tick);
    stopped.abort();
    await queue.idle();
    inbox.close();
  }
}

try {
  await main();
} catch (error) {
  console.error(`飞书入口失败：${error.message}`);
  process.exitCode = 1;
}
