import http from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.join(path.dirname(fileURLToPath(import.meta.url)), 'dist');
const port = Number(process.env.PORT || 8791);
const csp = (await readFile(path.join(root, '_headers'), 'utf8')).split(/\r?\n/).find(line => line.trim().startsWith('Content-Security-Policy:'))?.trim().slice('Content-Security-Policy:'.length).trim();
const types = {'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.svg':'image/svg+xml','.webm':'video/webm','.webp':'image/webp','.png':'image/png','.mp4':'video/mp4'};
http.createServer(async (req,res) => {
  const requested = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
  const filename = path.resolve(root, '.' + (requested === '/' ? '/index.html' : requested));
  if (!filename.startsWith(root + path.sep) || !['GET','HEAD'].includes(req.method)) { res.writeHead(403);res.end();return; }
  try { const body=await readFile(filename);res.writeHead(200,{'Content-Type':types[path.extname(filename)] || 'application/octet-stream','Cache-Control':'no-store',...(csp ? {'Content-Security-Policy':csp} : {})});res.end(req.method === 'HEAD' ? undefined : body); }
  catch { res.writeHead(404);res.end('Not found'); }
}).listen(port,'127.0.0.1',()=>console.log(`PAI local preview: http://127.0.0.1:${port}`));
