import { randomUUID } from 'node:crypto';
import { createWriteStream } from 'node:fs';
import { lstat, mkdir, rename, rm } from 'node:fs/promises';
import path from 'node:path';
import { Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';

const MAX_BYTES = 25 * 1024 * 1024;
const TYPES = new Set(['image', 'file', 'audio', 'video']);

export class MessageError extends Error {}

export async function saveAttachments(client, directory, messageId, resources, signal) {
  if (!resources.length) return [];
  await mkdir(directory, { recursive: true, mode: 0o700 });
  if ((await lstat(directory)).isSymbolicLink()) throw new MessageError('飞书附件目录不能使用符号链接。');
  const attachments = [];
  let bytes = 0;
  for (const { type, fileKey, fileName } of resources) {
    signal.throwIfAborted();
    if (!TYPES.has(type) || typeof fileKey !== 'string' || !/^[a-z0-9_-]+$/iu.test(fileKey)) {
      throw new MessageError('飞书消息包含无法下载的附件。');
    }
    const response = await client.im.v1.messageResource.get({
      path: { message_id: messageId, file_key: fileKey },
      params: { type: type === 'image' ? 'image' : 'file' },
      headers: { 'Content-Type': 'application/json' },
    });
    const stream = response.getReadableStream();
    if (response.headers?.['content-type']?.includes('application/json') && !response.headers['content-disposition']) {
      stream.destroy();
      throw new MessageError('飞书未返回附件，请检查机器人权限和消息。');
    }
    const limit = new Transform({
      transform(chunk, _encoding, next) {
        bytes += chunk.length;
        if (bytes > MAX_BYTES) next(new MessageError('该条消息的附件累计超过 25 MiB。'));
        else next(null, chunk);
      },
    });
    const filename = `${attachments.length + 1}.bin`;
    const target = path.join(directory, filename);
    const partial = `${target}.${randomUUID()}.part`;
    try {
      await pipeline(stream, limit, createWriteStream(partial, { flags: 'wx', mode: 0o600 }), { signal });
      await rename(partial, target);
    } finally {
      await rm(partial, { force: true });
    }
    attachments.push({ type, name: fileName ?? filename, path: target });
  }
  return attachments;
}
