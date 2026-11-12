"use strict";

self.addEventListener("install", event => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

async function download(id) {
  try {
    // Read the deployed catalog on demand; do not keep a stale persistent copy.
    const catalogResponse = await fetch(new URL("manifest.json", self.registration.scope), {cache: "no-store"});
    if (!catalogResponse.ok) throw new Error("目録を読み込めませんでした。");
    const catalog = await catalogResponse.json();
    const file = catalog.files.find(entry => entry.id === id && entry.download_url);
    if (!file) return new Response("公開ファイルが見つかりません。", {status: 404});
    const url = new URL(file.download_url);
    if (url.origin !== "https://huggingface.co" || !url.pathname.startsWith("/buckets/")) {
      throw new Error("ダウンロード先を確認できませんでした。");
    }
    const upstream = await fetch(url, {credentials: "omit"});
    if (!upstream.ok || !upstream.body) throw new Error("ファイルを取得できませんでした。");
    const name = file.filename.replace(/[\\/\x00-\x1f\x7f]/g, "_");
    const encoded = encodeURIComponent(name).replace(/['()*]/g, char => `%${char.charCodeAt(0).toString(16).toUpperCase()}`);
    const headers = new Headers({
      "Content-Type": "application/octet-stream",
      "Content-Disposition": `attachment; filename="${id}"; filename*=UTF-8''${encoded}`,
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "no-store",
    });
    if (file.size !== null) headers.set("Content-Length", String(file.size));
    return new Response(upstream.body, {headers});
  } catch (error) {
    return new Response(`${error.message}\n元のページに戻り、時間をおいて再試行してください。`, {
      status: 502, headers: {"Content-Type": "text/plain; charset=utf-8"},
    });
  }
}

self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  const prefix = new URL("download/", self.registration.scope);
  if (event.request.method === "GET" && url.origin === prefix.origin && url.pathname.startsWith(prefix.pathname)) {
    const id = url.pathname.slice(prefix.pathname.length);
    if (/^[a-f0-9]{64}$/.test(id)) event.respondWith(download(id));
  }
});
