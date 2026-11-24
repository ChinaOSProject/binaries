const media = "https://media.githubusercontent.com/media/ChinaOSProject/binaries";
const keyring = "chinaos-archive-keyring.gpg";
const releasePath = /^dists\/stable\/(?:InRelease|Release(?:\.gpg)?)$/;
const packagesPath = /^dists\/stable\/main\/binary-[a-z0-9-]+\/Packages(?:\.gz)?$/;
const byHashPath = /^dists\/stable\/main\/binary-[a-z0-9-]+\/by-hash\/SHA256\/[a-f0-9]{64}$/;
const packagePath = /^pool\/[a-f0-9]{40}\/[A-Za-z0-9][A-Za-z0-9.+_~-]*\/[a-z0-9-]+\/[A-Za-z0-9][A-Za-z0-9.+_~-]*\.deb$/;

function unavailable(head) {
  return new Response(head ? null : "APT snapshot unavailable\n", {
    status: 503,
    headers: { "Cache-Control": "no-store", "Retry-After": "60" },
  });
}

async function metadata(request, bucket, path, immutable) {
  const head = request.method === "HEAD";
  let key = `by-hash/${path}`;
  if (!immutable) {
    const current = await bucket.get("current");
    const snapshot = current && (await current.text()).trim();
    if (!snapshot || !/^[a-f0-9]{64}$/.test(snapshot)) {
      return unavailable(head);
    }
    key = `snapshots/${snapshot}/${path}`;
  }
  const object = head ? await bucket.head(key) : await bucket.get(key);
  if (!object) {
    return new Response(null, { status: 404, headers: { "Cache-Control": "no-store" } });
  }
  let type = "text/plain; charset=utf-8";
  if (path === keyring || immutable) type = "application/octet-stream";
  if (path.endsWith(".gz")) type = "application/gzip";
  return new Response(head ? null : object.body, {
    headers: {
      "Cache-Control": immutable ? "public, max-age=31536000, immutable, no-transform"
        : "no-cache, no-transform",
      "Content-Length": String(object.size),
      "Content-Type": type,
      "ETag": object.httpEtag,
    },
  });
}

export default {
  async fetch(request, env) {
    const head = request.method === "HEAD";
    if (request.method !== "GET" && !head) {
      return new Response(null, { status: 405, headers: { Allow: "GET, HEAD" } });
    }
    const url = new URL(request.url);
    let parts;
    try {
      parts = url.pathname.slice(1).split("/").map(decodeURIComponent);
    } catch {
      return new Response(null, { status: 400 });
    }
    if (parts.some((part) => part.includes("/"))) {
      return new Response(null, { status: 400 });
    }
    const path = parts.join("/");

    if (packagePath.test(path)) {
      const target = `${media}/${parts.slice(1).map(encodeURIComponent).join("/")}`;
      return new Response(null, {
        status: 307,
        headers: { Location: target, "Cache-Control": "public, max-age=86400, immutable" },
      });
    }
    if (path === "chinaos.sources") {
      const source = `Types: deb\nURIs: ${url.origin}\nSuites: stable\nComponents: main\n`
        + `Signed-By: /usr/share/keyrings/${keyring}\n`;
      return new Response(head ? null : source, {
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }

    const immutable = byHashPath.test(path);
    if (immutable || path === keyring || releasePath.test(path) || packagesPath.test(path)) {
      try {
        return await metadata(request, env.APT_INDEX, path, immutable);
      } catch (error) {
        console.error("APT metadata read failed", error);
        return unavailable(head);
      }
    }
    return new Response(null, { status: 404 });
  },
};
