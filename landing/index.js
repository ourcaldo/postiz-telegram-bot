// Bio landing page: numbered product links per channel + /add webhook.
// GET  /{channel}          -> batch index for channel (200 per batch, newest batch first)
// GET  /{channel}/p/N      -> batch N list (N=1 is newest)
// POST /{channel}/add       -> {name, url} + X-Webhook-Secret header -> {ok, num}

const PAGE_SIZE = 200;
// channels we accept; first request with a new slug below creates its own page.
// locked set keeps typos/abuse out of the webhook
const CHANNELS = new Set(["instagram", "threads", "x", "tiktok", "facebook", "youtube"]);

const html = (title, body) => `<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>
  body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;max-width:640px;margin:0 auto;padding:24px 16px;background:#fafafa;color:#111}
  h1{font-size:1.3rem;text-align:center}
  a.item{display:block;padding:12px 14px;margin:8px 0;background:#fff;border:1px solid #e5e5e5;border-radius:10px;text-decoration:none;color:#111}
  a.item:hover{border-color:#111}
  .num{color:#767676;font-size:.85rem}
  .back{display:block;text-align:center;margin-top:20px;color:#767676;text-decoration:none}
</style>
</head>
<body>${body}</body>
</html>`;

const esc = (s) =>
  String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

async function count(env, channel) {
  const r = await env.DB.prepare("SELECT COUNT(*) AS c FROM products WHERE channel = ?")
    .bind(channel)
    .first();
  return r.c;
}

async function pageIndex(env, channel) {
  const total = await count(env, channel);
  if (total === 0) {
    return html(
      `Produk ${channel}`,
      `<h1>Belum ada produk 🛍</h1>`
    );
  }
  const batches = Math.ceil(total / PAGE_SIZE);
  let links = "";
  for (let b = 1; b <= batches; b++) {
    const end = total - (b - 1) * PAGE_SIZE;
    const start = Math.max(1, end - PAGE_SIZE + 1);
    links += `<a class="item" href="/${channel}/p/${b}">Produk No. ${start} - ${end}</a>`;
  }
  return html(`Produk ${channel}`, `<h1>Produk ${channel}</h1>${links}`);
}

async function pageList(env, channel, b) {
  const total = await count(env, channel);
  if (total === 0 || b < 1 || b > Math.ceil(total / PAGE_SIZE)) return null;
  const end = total - (b - 1) * PAGE_SIZE;
  const start = Math.max(1, end - PAGE_SIZE + 1);
  const rows = await env.DB.prepare(
    "SELECT num, name, url FROM products WHERE channel = ? AND num BETWEEN ? AND ? ORDER BY num DESC"
  )
    .bind(channel, start, end)
    .all();
  const items = rows.results
    .map(
      (r) =>
        `<a class="item" href="${esc(r.url)}" target="_blank" rel="noopener"><span class="num">No. ${r.num}</span><br>${esc(r.name)}</a>`
    )
    .join("");
  return html(
    `Produk No. ${start} - ${end}`,
    `<h1>Produk No. ${start} - ${end}</h1>${items}<a class="back" href="/${channel}">« Kembali</a>`
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean); // e.g. ["instagram","add"] | ["instagram","p","2"]

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
      // per-channel counter: max+1 inside a transaction (D1 serializes writes)
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
