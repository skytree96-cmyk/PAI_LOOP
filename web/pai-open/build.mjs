import { readFile, writeFile, mkdir, copyFile, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.dirname(fileURLToPath(import.meta.url));
const output = path.join(root, 'dist');
await mkdir(output, { recursive: true });
const files = ['index.html', 'styles.css', 'app.js', 'favicon.svg', '_headers'];
const html = await readFile(path.join(root, 'index.html'), 'utf8');
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
if (new Set(ids).size !== ids.length) throw new Error('Duplicate HTML id');
for (const [, target] of html.matchAll(/href="#([^"]+)"/g)) if (!ids.includes(target)) throw new Error(`Missing anchor ${target}`);
for (const [, target] of html.matchAll(/aria-(?:controls|labelledby)="([^"]+)"/g)) for (const id of target.split(' ')) if (!ids.includes(id)) throw new Error(`Missing ARIA target ${id}`);
if (/사업자등록번호|운영 PIN\s*[:=]\s*\d/.test(html)) throw new Error('Unexpected private data in public page');
for (const file of files) await copyFile(path.join(root, file), path.join(output, file));
if (html.includes('/assets/')) {
  await mkdir(path.join(output, 'assets'), { recursive: true });
  for (const [, file] of html.matchAll(/(?:src|poster)="\/(assets\/[^"?]+)"/g)) { await stat(path.join(root, file)); await copyFile(path.join(root, file), path.join(output, file)); }
}
await writeFile(path.join(output, '404.html'), '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PAI · 페이지를 찾을 수 없습니다</title><body><main><h1>페이지를 찾을 수 없습니다.</h1><p><a href="/">PAI 소개로 돌아가기</a></p></main></body></html>');
console.log(`Build verified. Static output: ${output}`);
