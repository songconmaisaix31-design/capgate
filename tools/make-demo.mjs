#!/usr/bin/env node
// 用真实 CLI 输出渲染 README 演示动效（SVG）。
//
// 三条硬要求：
//   1. 确定性——task id、时延、绝对路径全部归一化，否则 CI 每次都会"更新"一次；
//   2. 只画真跑过的行——命令在临时账本里真执行，不做静态示意；
//   3. 跨平台——分隔符与路径在渲染前统一。
//
// 用法：
//   node tools/make-demo.mjs            # 生成 assets/demo/demo.svg
//   node tools/make-demo.mjs --check    # 只检查是否最新（CI 用），过期退出 1

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = join(ROOT, "assets", "demo", "demo.svg");
const PYTHON = process.env.PYTHON ?? "python";

const CARDS = [
  {
    title: "capgate · 一次任务的路由与执行",
    // static 是刻意的：默认 thompson 每次都随机采样，动画会天天变
    cmd: "python -m capgate demo -s pipeline --strategy static --brief",
  },
  { title: "selfhost · 用 capgate 跑自己的质量门", cmd: "python -m selfhost.loop --only T03" },
];

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);

function run(command) {
  const env = {
    ...process.env,
    PYTHONIOENCODING: "utf-8",
    PYTHONUTF8: "1",
    NO_COLOR: "1",
    TERM: "dumb",
    COLUMNS: "120",
  };
  const out = execFileSync(PYTHON, command.split(" ").slice(1), {
    cwd: ROOT,
    env,
    encoding: "utf8",
    timeout: 180_000,
  });
  return out;
}

function normalize(text) {
  const rootPattern = new RegExp(ROOT.replace(/\\/g, "\\\\").replace(/\//g, "[/\\\\]"), "g");
  return text
    .replace(/\x1b\[[0-9;]*m/g, "") // ANSI
    .replace(rootPattern, "<repo>")
    .replace(/task_[0-9a-f]{6,}/g, "task_<id>")
    .replace(/\b\d+(?:\.\d+)?ms\b/g, "<ms>")
    .replace(/\\/g, "/") // 分隔符统一，否则 Windows/Linux 会互相"更新"
    .split("\n")
    .map((line) => line.replace(/\s+$/, ""))
    .filter((line) => line.trim() !== "")
    .join("\n")
    .trimEnd();
}

function buildSvg(cards, lines) {
  const PAD = 26;
  const LINE_H = 21;
  const CARD_HEAD = 46;
  const CARD_GAP = 18;
  const WIDTH = 900;

  let y = PAD;
  const parts = [];
  const meta = [];

  for (const card of cards) {
    const body = card.lines;
    const height = CARD_HEAD + card.lines.length * LINE_H + 16;
    meta.push({ y, height, title: card.title, cmd: card.cmd, body });
    y += height + CARD_GAP;
  }

  const height = y - CARD_GAP + PAD;
  let delay = 0.15;

  for (const card of meta) {
    parts.push(
      `  <g class="card">`,
      `    <rect x="${PAD}" y="${card.y}" width="${WIDTH - PAD * 2}" height="${card.height}" rx="10" ` +
        `fill="#161b22" stroke="#30363d"/>`,
      `    <circle cx="${PAD + 18}" cy="${card.y + 18}" r="4" fill="#f85149"/>`,
      `    <circle cx="${PAD + 32}" cy="${card.y + 18}" r="4" fill="#d29922"/>`,
      `    <circle cx="${PAD + 46}" cy="${card.y + 18}" r="4" fill="#3fb950"/>`,
      `    <text x="${PAD + 62}" y="${card.y + 22}" class="title">${esc(card.title)}</text>`,
      `    <text x="${PAD + 18}" y="${card.y + 40}" class="cmd">$ ${esc(card.cmd)}</text>`,
    );
    card.body.forEach((line, i) => {
      const ly = card.y + CARD_HEAD + i * LINE_H + 6;
      const cls = line.includes("→") ? "line accent" : line.startsWith("✓") ? "line ok" : "line";
      parts.push(
        `    <text x="${PAD + 18}" y="${ly}" class="${cls}" style="animation-delay:${delay.toFixed(2)}s">${esc(line)}</text>`,
      );
      delay += 0.09;
    });
    parts.push(`  </g>`);
  }

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${WIDTH}" height="${height}" viewBox="0 0 ${WIDTH} ${height}" role="img" aria-label="capgate 终端演示">
  <style>
    .bg { fill: #0d1117; }
    text { font-family: "JetBrains Mono", "Cascadia Mono", Consolas, "Noto Sans Mono CJK SC", monospace; font-size: 13px; fill: #c9d1d9; }
    .title { fill: #8b949e; font-size: 12px; }
    .cmd { fill: #58a6ff; }
    .line { opacity: 0; animation: fade .26s ease-out forwards; }
    .accent { fill: #d2a8ff; }
    .ok { fill: #3fb950; }
    .cursor { animation: blink 1.06s steps(2, start) infinite; }
    @keyframes fade { from { opacity: 0; transform: translateY(2px); } to { opacity: 1; transform: none; } }
    @keyframes blink { 50% { opacity: 0; } }
  </style>
  <rect class="bg" width="100%" height="100%" rx="0"/>
${parts.join("\n")}
  <text x="${WIDTH - PAD - 8}" y="${height - 12}" class="cursor" fill="#3fb950">▍</text>
</svg>
`;
}

function render() {
  const cards = CARDS.map((card) => ({ ...card, lines: normalize(run(card.cmd)).split("\n") }));
  return buildSvg(cards, null);
}

const svg = render();
const check = process.argv.includes("--check");

if (check) {
  // Windows 下 core.autocrlf 会把盘上的 demo.svg 换成 CRLF，比对前先归一成 LF，
  // 否则 --check 会因为行尾差异误报"过期"。
  const current = existsSync(OUT) ? readFileSync(OUT, "utf8").replace(/\r\n?/g, "\n") : "";
  if (current !== svg) {
    console.error("assets/demo/demo.svg 已过期，请跑：node tools/make-demo.mjs");
    process.exit(1);
  }
  console.log("assets/demo/demo.svg 是最新的");
} else {
  mkdirSync(dirname(OUT), { recursive: true });
  writeFileSync(OUT, svg, "utf8");
  console.log(`已生成 ${OUT}（${svg.split("\n").length} 行，${Buffer.byteLength(svg)} 字节）`);
}
