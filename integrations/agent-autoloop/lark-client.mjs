import { required } from './common.mjs';

export const MESSAGE_EVENT = 'im.message.receive_v1';
export const messageIdPattern = /^om_[A-Za-z0-9_-]+$/u;
const chatIdPattern = /^oc_[A-Za-z0-9_-]+$/u;
const userIdPattern = /^ou_[A-Za-z0-9_-]+$/u;

function allowed(name, pattern) {
  const ids = required(name).split(',').map(id => id.trim());
  if (ids.some(id => !pattern.test(id))) throw new Error(`${name} 包含无效的飞书 ID`);
  return new Set(ids);
}

export function larkScope() {
  return {
    chats: allowed('CAPGATE_LARK_CHATS', chatIdPattern),
    senders: allowed('CAPGATE_LARK_SENDERS', userIdPattern),
  };
}

export function receivedMessage(data, appId, scope) {
  const { sender, message, app_id: eventAppId } = data;
  const senderId = sender?.sender_id?.open_id;
  if (eventAppId !== appId || sender?.sender_type !== 'user' || message?.chat_type !== 'p2p' ||
      !userIdPattern.test(senderId ?? '') || !chatIdPattern.test(message?.chat_id ?? '') ||
      (scope && (!scope.senders.has(senderId) || !scope.chats.has(message.chat_id)))) return;
  const created = Number(message.create_time);
  if (!messageIdPattern.test(message.message_id ?? '') || !Number.isSafeInteger(created) ||
      created <= 0 || typeof message.content !== 'string' || typeof message.message_type !== 'string') {
    throw new Error('飞书事件缺少可信的消息编号、发送时间或内容');
  }
  return {
    chat: message.chat_id, sender: senderId, kind: 'single', message_id: message.message_id,
    created_ms: created,
  };
}
