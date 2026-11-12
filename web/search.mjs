const KINDS = new Set(["trial", "patch", "full", "movie", "other"]);
const fold = value => String(value ?? "").normalize("NFKC").toLocaleLowerCase();

export function parameters(url) {
  const p = url.searchParams;
  for (const key of p.keys()) {
    if (!["q", "kind", "manufacturer", "available", "limit", "offset"].includes(key) || p.getAll(key).length !== 1)
      throw new Error("Unknown or repeated parameter: " + key);
  }
  const kind = p.get("kind") || "";
  if (kind && !KINDS.has(kind)) throw new Error("kind must be trial, patch, full, movie or other");
  const available = p.get("available");
  if (available !== null && !["true", "false"].includes(available)) throw new Error("available must be true or false");
  const integer = (key, fallback, min, max) => {
    if (!p.has(key)) return fallback;
    const raw = p.get(key);
    if (!/^\d+$/.test(raw)) throw new Error(key + " must be an integer");
    const n = Number(raw);
    if (!Number.isSafeInteger(n) || n < min || n > max) throw new Error(key + " is out of range");
    return n;
  };
  const q = p.get("q") || "";
  if (q.length > 256) throw new Error("q exceeds 256 characters");
  return {q: fold(q).trim(), kind, manufacturer: p.get("manufacturer") || "", available,
    limit: integer("limit", 50, 1, 200), offset: integer("offset", 0, 0, 1000000)};
}

export function search(manifest, options) {
  if (manifest.schema_version !== 1 || !Array.isArray(manifest.files) || !Array.isArray(manifest.products) || !Array.isArray(manifest.manufacturers))
    throw new Error("Unsupported catalog schema");
  const products = new Map(manifest.products.map(p => [p.id, p]));
  const makers = new Map(manifest.manufacturers.map(m => [m.id, m]));
  const results = [];
  let total = 0;
  for (const file of manifest.files) {
    if (options.kind && file.kind !== options.kind) continue;
    const product = products.get(file.product_id);
    const maker = makers.get(product?.manufacturer_id);
    if (!product || !maker) throw new Error("Broken catalog relation");
    if (options.manufacturer && options.manufacturer !== maker.id && fold(options.manufacturer) !== fold(maker.name)) continue;
    if (options.available !== null && Boolean(file.download_url) !== (options.available === "true")) continue;
    const text = [file.name, file.filename, product.title, maker.name, ...(file.sources || []).map(s => s.filename)].map(fold).join(" ");
    if (!options.q.split(/\s+/).every(word => text.includes(word))) continue;
    if (total >= options.offset && results.length < options.limit)
      results.push({...file, product: {id: product.id, title: product.title}, manufacturer: maker});
    total++;
  }
  return {schema_version: 1, generated_at: manifest.generated_at, total, limit: options.limit, offset: options.offset, results};
}

