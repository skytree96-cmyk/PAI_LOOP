import { readFile, writeFile, mkdir, copyFile, stat, readdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.dirname(fileURLToPath(import.meta.url));
const output = path.join(root, 'dist');
await mkdir(output, { recursive: true });
const files = ['styles.css', 'open-refinement.css', 'story.css', 'app.js', 'story.js', 'favicon.svg', '_headers'];
const mediaFiles = ['assets/pai-intro-3d.mp4', 'assets/pai-intro-3d-poster.webp'];
const template = await readFile(path.join(root, 'index.html'), 'utf8');
const story = await readFile(path.join(root, 'story.html'), 'utf8');
if (template.split('<!-- PAI_STORY_COMPONENT -->').length !== 2) throw new Error('Expected one story slot');
const html = template.replace('<!-- PAI_STORY_COMPONENT -->', story);
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
if (new Set(ids).size !== ids.length) throw new Error('Duplicate HTML id');
for (const [, target] of html.matchAll(/href="#([^"]+)"/g)) if (!ids.includes(target)) throw new Error(`Missing anchor ${target}`);
for (const [, target] of html.matchAll(/aria-(?:controls|labelledby)="([^"]+)"/g)) for (const id of target.split(' ')) if (!ids.includes(id)) throw new Error(`Missing ARIA target ${id}`);
if (/사업자등록번호|운영 PIN\s*[:=]\s*\d/.test(html)) throw new Error('Unexpected private data in public page');
const referencedMedia = new Set([...html.matchAll(/(?:src|poster)="\/(assets\/[^"?]+)"/g)].map(([, file]) => file));
for (const file of referencedMedia) if (!mediaFiles.includes(file)) throw new Error(`Unexpected media reference ${file}`);
for (const file of mediaFiles) {
  if (!referencedMedia.has(file)) throw new Error(`Missing product-tour reference ${file}`);
  if (!(await stat(path.join(root, file))).isFile()) throw new Error(`Media is not a file: ${file}`);
}
for (const [, reference] of html.matchAll(/(?:src|href)="\/(?!assets\/)([^"?#]+)(?:\?[^"#]*)?"/g)) {
  if (!files.includes(reference)) throw new Error(`Unexpected static reference ${reference}`);
}
for (const [, href] of html.matchAll(/href="(https:[^"]+)"/g)) {
  const url = new URL(href);
  if (!['pai-loop-demo.onrender.com', 'cdn.jsdelivr.net'].includes(url.hostname)) throw new Error(`Unexpected external link ${url.hostname}`);
}
await writeFile(path.join(output, 'index.html'), html);
for (const file of files) await copyFile(path.join(root, file), path.join(output, file));
const mediaOutput = path.join(output, 'assets');
await mkdir(mediaOutput, { recursive: true });
for (const obsolete of ['pai-loop-background.webm', 'pai-loop-poster.webp', 'pai-product-tour.webm', 'pai-product-poster.webp']) {
  await rm(path.join(mediaOutput, obsolete), { force: true });
}
for (const entry of await readdir(mediaOutput, { withFileTypes: true })) {
  if (!entry.isFile() || !mediaFiles.includes(`assets/${entry.name}`)) throw new Error(`Unexpected output asset ${entry.name}`);
}
for (const file of mediaFiles) await copyFile(path.join(root, file), path.join(output, file));
await writeFile(path.join(output, '404.html'), '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>PAI · 페이지를 찾을 수 없습니다</title><body><main><h1>페이지를 찾을 수 없습니다.</h1><p><a href="/">PAI 소개로 돌아가기</a></p></main></body></html>');
console.log(`Build verified. Static output: ${output}`);
