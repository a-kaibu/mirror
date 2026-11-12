import {parameters, search} from "./search.mjs";
const output = document.querySelector("#result");
try {
  const options = parameters(new URL(location.href));
  const response = await fetch(new URL("manifest.json", import.meta.url), {cache: "no-store"});
  if (!response.ok) throw new Error("カタログを取得できませんでした");
  output.textContent = JSON.stringify(search(await response.json(), options), null, 2);
  document.title = "JSON search";
} catch (error) {
  output.textContent = JSON.stringify({error: error.message}, null, 2);
  document.title = "JSON search error";
}
