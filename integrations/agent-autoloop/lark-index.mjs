import { closeSync, existsSync, mkdirSync, openSync } from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

const MAX_MESSAGES = 250;

function openIndex(file, readOnly) {
  if (!path.isAbsolute(file)) throw new Error('飞书历史索引必须使用绝对路径');
  if (readOnly) {
    if (!existsSync(file)) throw new Error('飞书历史索引尚未建立，请检查入口和工作器的索引配置');
  } else {
    mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
    closeSync(openSync(file, 'a', 0o600));
  }
  const database = new DatabaseSync(file, { readOnly });
  database.exec('PRAGMA busy_timeout = 5000');
  return database;
}

function ensureSchema(database) {
  database.exec(`
    CREATE TABLE IF NOT EXISTS lark_messages (
      event_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK (kind = 'single'),
      chat TEXT NOT NULL,
      sender TEXT NOT NULL,
      message_id TEXT NOT NULL,
      received_ms INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS lark_messages_window ON lark_messages(kind, chat, received_ms);
  `);
}

export function prepareIndex(file) {
  const database = openIndex(file, false);
  try {
    ensureSchema(database);
  } finally {
    database.close();
  }
}

export function indexMessage(file, source, taskId) {
  const received = Date.parse(source.received_at);
  if (!Number.isFinite(received)) throw new Error('agent-autoloop 未提供有效的收件时间');
  const database = openIndex(file, false);
  try {
    ensureSchema(database);
    const row = database.prepare(`
      INSERT INTO lark_messages (event_id, task_id, kind, chat, sender, message_id, received_ms)
      VALUES (?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(event_id) DO UPDATE SET event_id = excluded.event_id
        WHERE task_id = excluded.task_id AND kind = excluded.kind AND chat = excluded.chat
          AND sender = excluded.sender AND message_id = excluded.message_id
          AND received_ms = excluded.received_ms
      RETURNING event_id
    `).get(source.event_id, taskId, source.kind, source.chat, source.sender, source.message_id, received);
    if (!row) throw new Error('飞书消息引用与同一事件的已有索引不一致');
  } finally {
    database.close();
  }
}

export function messagesIn(file, { kind, chat, since, until, senders }) {
  const database = openIndex(file, true);
  try {
    const messages = database.prepare(`
      SELECT event_id, task_id, kind, chat, sender, message_id, received_ms
      FROM lark_messages
      WHERE kind = ? AND chat = ? AND received_ms >= ? AND received_ms < ?
        AND sender IN (SELECT value FROM json_each(?))
      ORDER BY received_ms, event_id
      LIMIT ?
    `).all(kind, chat, since, until, JSON.stringify(senders), MAX_MESSAGES + 1);
    if (messages.length > MAX_MESSAGES) throw new Error('时间窗内超过 250 条消息，请缩小查询范围');
    return messages;
  } finally {
    database.close();
  }
}
