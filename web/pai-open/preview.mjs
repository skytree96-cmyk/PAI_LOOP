import http from 'node:http';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.dirname(fileURLToPath(import.meta.url));
const types = {'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.svg':'image/svg+xml','.webm':'video/webm','.webp':'image/webp','.png':'image/png','.mp4':'video/mp4'};
http.createServer(async (req,res) => {
  const requested = decodeURIComponent(new URL(req.url, 'http://127.0.0.1').pathname);
  const filename = path.resolve(root, '.' + (requested === '/' ? '/index.html' : requested));
  if (!filename.startsWith(root + path.sep) || !['GET','HEAD'].includes(req.method)) { res.writeHead(403);res.end();return; }
  try { const body=await readFile(filename);res.writeHead(200,{'Content-Type':types[path.extname(filename)] || 'application/octet-stream','Cache-Control':'no-store'});res.end(req.method === 'HEAD' ? undefined : body); }
  catch { res.writeHead(404);res.end('Not found'); }
}).listen(8788,'127.0.0.1',()=>console.log('PAI local preview: http://127.0.0.1:8788'));
