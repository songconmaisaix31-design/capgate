import { closeSync, lstatSync, mkdirSync, openSync } from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';

export function openInbox(directory) {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  if (lstatSync(directory).isSymbolicLink()) throw new Error('飞书附件目录不能使用符号链接');
  const file = path.join(directory, 'inbox.sqlite3');
  closeSync(openSync(file, 'a', 0o600));
  const database = new DatabaseSync(file);
  database.exec(`
    PRAGMA busy_timeout = 500;
    CREATE TABLE IF NOT EXISTS lark_inbox (
      digest TEXT PRIMARY KEY,
      payload TEXT,
      result TEXT,
      done INTEGER NOT NULL DEFAULT 0,
      retry_at INTEGER NOT NULL DEFAULT 0
    );
  `);
  const add = database.prepare('INSERT OR IGNORE INTO lark_inbox(digest, payload) VALUES (?, ?)');
  const get = database.prepare(`
    SELECT digest, payload, result FROM lark_inbox WHERE done = 0 AND retry_at <= ? ORDER BY rowid LIMIT ?
  `);
  const result = database.prepare('UPDATE lark_inbox SET result = ? WHERE digest = ? AND done = 0');
  const retry = database.prepare('UPDATE lark_inbox SET retry_at = ? WHERE digest = ? AND done = 0');
  const complete = database.prepare('UPDATE lark_inbox SET done = 1, payload = NULL, result = NULL WHERE digest = ?');
  return {
    add: (digest, payload) => add.run(digest, JSON.stringify(payload)),
    pending: limit => get.all(Date.now(), limit),
    result: (digest, text) => result.run(text, digest),
    retry: digest => retry.run(Date.now() + 30_000, digest),
    complete: digest => complete.run(digest),
    close: () => database.close(),
  };
}
