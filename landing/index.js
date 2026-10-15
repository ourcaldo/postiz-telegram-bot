// Bio landing page: numbered product links per channel + /add webhook.
// GET  /{channel}      -> <=200 products: full list directly. >200: batch cards.
// GET  /{channel}/p/N  -> batch N list (N=1 is newest)
// POST /{channel}/add  -> {name, url} + X-Webhook-Secret header -> {ok, num}

const PAGE_SIZE = 200;
const CHANNELS = new Set(["instagram", "threads", "x", "tiktok", "facebook", "youtube"]);
const ICON = {
  instagram: "📸",
  threads: "🧵",
  x: "𝕏",
  tiktok: "🎵",
  facebook: "👍",
  youtube: "▶️",
};

const css = `
:root{--bg1:#fdf2f8;--bg2:#eff6ff;--card:#fff;--ink:#18181b;--muted:#71717a;--line:#e4e4e7;--accent:#ec4899}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',sans-serif;
  min-height:100vh;background:linear-gradient(160deg,var(--bg1) 0%,var(--bg2) 100%);
  color:var(--ink);display:flex;flex-direction:column;align-items:center;padding:32px 16px 48px}
.wrap{width:100%;max-width:560px}
header{text-align:center;margin-bottom:24px}
.avatar{width:76px;height:76px;border-radius:50%;margin:0 auto 14px;display:flex;align-items:center;justify-content:center;
  font-size:2rem;background:linear-gradient(135deg,#f472b6,#818cf8);box-shadow:0 8px 24px rgba(236,72,153,.25)}
h1{font-size:1.25rem;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:.85rem;margin-top:4px}
.count{display:inline-flex;align-items:center;gap:6px;margin-top:12px;padding:4px 12px;border-radius:99px;
  background:var(--card);border:1px solid var(--line);font-size:.78rem;color:var(--muted)}
.dot{width:7px;height:7px;border-radius:50%;background:#22c55e;animation:pulse 2s infinite}
@keyframes pulse{50%{opacity:.4}}
.list{display:flex;flex-direction:column;gap:10px}
a.item{display:flex;align-items:center;gap:14px;padding:14px 16px;background:var(--card);
  border:1px solid var(--line);border-radius:16px;text-decoration:none;color:var(--ink);
  transition:transform .15s ease,box-shadow .15s ease,border-color .15s ease}
a.item:hover{transform:translateY(-2px);border-color:var(--accent);box-shadow:0 10px 24px rgba(236,72,153,.12)}
.num{flex:none;min-width:44px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;
  font-size:.8rem;font-weight:700;color:#fff;background:linear-gradient(135deg,#ec4899,#818cf8)}
.name{flex:1;font-weight:600;font-size:.95rem;line-height:1.35;word-break:break-word}
.arrow{flex:none;color:var(--muted);font-size:1rem}
a.batch{display:flex;justify-content:space-between;align-items:center;padding:16px 18px;background:var(--card);
  border:1px solid var(--line);border-radius:16px;text-decoration:none;color:var(--ink);font-weight:600;
  transition:transform .15s ease,box-shadow .15s ease,border-color .15s ease}
a.batch:hover{transform:translateY(-2px);border-color:var(--accent);box-shadow:0 10px 24px rgba(236,72,153,.12)}
.batch .range{color:var(--muted);font-weight:500;font-size:.85rem}
.empty{text-align:center;padding:60px 0;color:var(--muted)}
.empty .big{font-size:2.6rem;margin-bottom:12px}
footer{margin-top:32px;color:var(--muted);font-size:.75rem;text-align:center}
footer a{color:var(--muted)}
@media (prefers-color-scheme:dark){
  :root{--bg1:#1c1917;--bg2:#0f172a;--card:#27272a;--ink:#fafafa;--muted:#a1a1aa;--line:#3f3f46}
  .avatar{box-shadow:0 8px 24px rgba(236,72,153,.35)}
}`;

const html = (title, body) => `<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>${css}</style>
</head>
<body><div class="wrap">${body}</div></body>
</html>`;

const esc = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

const header = (channel, total) => `
<header>
  <div class="avatar">${ICON[channel] || "🛍"}</div>
  <h1>Rekomendasi Produk</h1>
  <div class="sub">${esc(channel)}</div>
  <div class="count"><span class="dot"></span>${total} produk</div>
</header>`;

const itemList = (rows) =>
  `<div class="list">${rows
    .map(
      (r) =>
        `<a class="item" href="${esc(r.url)}" target="_blank" rel="noopener">
           <span class="num">${r.num}</span>
           <span class="name">${esc(r.name)}</span>
           <span class="arrow">›</span>
         </a>`
    )
    .join("")}</div>`;

const batchList = (channel, total) => {
  const batches = Math.ceil(total / PAGE_SIZE);
  let links = "";
  for (let b = 1; b <= batches; b++) {
    const end = total - (b - 1) * PAGE_SIZE;
    const start = Math.max(1, end - PAGE_SIZE + 1);
    links += `<a class="batch" href="/${channel}/p/${b}">
                <span>Produk No. ${start} - ${end}</span>
                <span class="range">Lihat ›</span>
              </a>`;
  }
  return `<div class="list">${links}</div>`;
};

async function count(env, channel) {
  const r = await env.DB.prepare("SELECT COUNT(*) AS c FROM products WHERE channel = ?")
    .bind(channel)
    .first();
  return r.c;
}

async function fetchRange(env, channel, start, end) {
  const rows = await env.DB.prepare(
    "SELECT num, name, url FROM products WHERE channel = ? AND num BETWEEN ? AND ? ORDER BY num DESC"
  )
    .bind(channel, start, end)
    .all();
  return rows.results;
}

async function pageIndex(env, channel) {
  const total = await count(env, channel);
  if (total === 0) {
    return html(
      `Rekomendasi Produk — ${channel}`,
      `${header(channel, 0)}<div class="empty"><div class="big">🛍</div>Belum ada produk.<br>Produk akan muncul di sini otomatis.</div>`
    );
  }
  // <=200: show the full list directly, no batch step
  if (total <= PAGE_SIZE) {
    const rows = await fetchRange(env, channel, 1, total);
    return html(`Rekomendasi Produk — ${channel}`, header(channel, total) + itemList(rows));
  }
  return html(`Rekomendasi Produk — ${channel}`, header(channel, total) + batchList(channel, total));
}

async function pageList(env, channel, b) {
  const total = await count(env, channel);
  if (total === 0 || b < 1 || b > Math.ceil(total / PAGE_SIZE)) return null;
  const end = total - (b - 1) * PAGE_SIZE;
  const start = Math.max(1, end - PAGE_SIZE + 1);
  const rows = await fetchRange(env, channel, start, end);
  return html(
    `Produk No. ${start} - ${end}`,
    header(channel, total) + itemList(rows) +
      `<footer><a href="/${channel}">« Kembali ke daftar batch</a></footer>`
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);

    // POST /{channel}/add — webhook from bot
    if (
      request.method === "POST" &&
      parts.length === 2 &&
      parts[1] === "add" &&
      CHANNELS.has(parts[0])
    ) {
      if (request.headers.get("x-webhook-secret") !== env.WEBHOOK_SECRET) {
        return Response.json({ ok: false, error: "unauthorized" }, { status: 401 });
      }
      let body;
      try {
        body = await request.json();
      } catch {
        return Response.json({ ok: false, error: "bad json" }, { status: 400 });
      }
      const { name, url: purl } = body || {};
      if (!name || !purl || !/^https?:\/\//.test(purl)) {
        return Response.json({ ok: false, error: "name and http(s) url required" }, { status: 400 });
      }
      const channel = parts[0];
      // per-channel counter: max+1 (D1 serializes writes)
      const next = await env.DB.prepare(
        "SELECT COALESCE(MAX(num), 0) + 1 AS n FROM products WHERE channel = ?"
      )
        .bind(channel)
        .first();
      await env.DB.prepare(
        "INSERT INTO products (channel, num, name, url) VALUES (?, ?, ?, ?)"
      )
        .bind(channel, next.n, name, purl)
        .run();
      return Response.json({ ok: true, num: next.n });
    }

    // GET /{channel} or /{channel}/p/N — public pages
    if (request.method === "GET" && parts.length >= 1 && CHANNELS.has(parts[0])) {
      const channel = parts[0];
      if (parts.length === 1) {
        return new Response(await pageIndex(env, channel), {
          headers: { "content-type": "text/html; charset=utf-8" },
        });
      }
      if (parts.length === 3 && parts[1] === "p" && /^\d+$/.test(parts[2])) {
        const page = await pageList(env, channel, parseInt(parts[2], 10));
        if (page) {
          return new Response(page, {
            headers: { "content-type": "text/html; charset=utf-8" },
          });
        }
      }
    }

    return new Response("Not found", { status: 404 });
  },
};
