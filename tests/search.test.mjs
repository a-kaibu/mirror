import test from "node:test";
import assert from "node:assert/strict";
import {parameters, search} from "../web/search.mjs";
const handle = async (request, loader) => {
  let options;
  try {options = parameters(new URL(request.url));}
  catch (error) {return Response.json({error: error.message}, {status: 400});}
  try {return Response.json(search(await loader(), options));}
  catch {return Response.json({error: "Catalog temporarily unavailable"}, {status: 503});}
};
const catalog = {schema_version: 1, generated_at: "2026-11-12", manufacturers: [{id: "m", name: "Ｂｒａｎｄ"}],
  products: [{id: "p", manufacturer_id: "m", title: "作品名"}], files: [
    {id: "a", product_id: "p", kind: "trial", filename: "日本語.zip", sources: [], download_url: "https://example.test/a"},
    {id: "b", product_id: "p", kind: "patch", filename: "update.zip", sources: [], download_url: null},
    {id: "c", product_id: "p", kind: "trial", filename: "movie.zip", sources: [{filename: "alias.zip"}], download_url: null},
  ]};
const request = (query, loader = async () => catalog, method = "GET") => handle(new Request("https://search.test/search" + query, {method}), loader);
test("trial and availability filters with manufacturer normalization", async () => {
  const response = await request("?kind=trial&available=true&manufacturer=brand");
  const result = await response.json();
  assert.equal(result.total, 1); assert.equal(result.results[0].id, "a");
  assert.equal(result.results[0].product.title, "作品名");
});
test("Japanese multiword search and aliases", async () => {
  assert.equal((await (await request("?q=" + encodeURIComponent("作品名 日本語"))).json()).total, 1);
  assert.equal((await (await request("?q=ALIAS")).json()).results[0].id, "c");
});
test("pagination and unavailable only", async () => {
  const result = await (await request("?available=false&limit=1&offset=1")).json();
  assert.equal(result.total, 2); assert.equal(result.results.length, 1); assert.equal(result.results[0].id, "c");
});
test("invalid queries rejected before reading upstream", async () => {
  for (const query of ["?kind=bad", "?limit=0", "?limit=201", "?limit=2.5", "?offset=-1", "?available=yes", "?kind=trial&kind=patch", "?unknown=x"]) {
    let called = false;
    const response = await request(query, async () => {called = true; return catalog;});
    assert.equal(response.status, 400, query); assert.equal(called, false);
  }
});
test("upstream errors and schema failures never report empty success", async () => {
  for (const loader of [async () => {throw new Error("secret upstream failure");}, async () => ({}), async () => ({...catalog, products: []})]) {
    const response = await request("", loader); assert.equal(response.status, 503);
    assert.equal((await response.json()).error, "Catalog temporarily unavailable");
  }
});
test("full freeware distributions have their own filter", async () => {
  const loader = async () => ({...catalog, files: [...catalog.files, {id: "full", product_id: "p", kind: "full", filename: "game.zip", sources: [], download_url: "https://example.test/full"}]});
  const result = await (await request("?kind=full", loader)).json();
  assert.equal(result.total, 1);
  assert.equal(result.results[0].id, "full");
});
