// Bundles the bgutil PO-token server into one file (run by ytop.bgserver
// with the stick's own Deno):
//
//   deno run --allow-read --allow-write=<server> --allow-env \
//     --allow-net=registry.npmjs.org bgbundle.mjs <server folder>
//
// Writes <server>/bundle/generate_once.js (the whole script and its npm
// packages), <server>/bundle/assets.json (the node_modules files the bundle
// still reads at run time) and <server>/bundle/THIRD_PARTY_LICENSES.txt.
//
// A stick pays per file far more than per byte: node_modules is ~6,000
// files, the bundle is a handful.  Packages that find files next to their
// own code (__dirname, import.meta.url) keep working: each such module's
// paths are rewritten to point into ../node_modules/<its folder>, where the
// files it reads (not code) are kept.

import * as esbuild from "npm:esbuild-wasm@0.25.5";
import { builtinModules } from "node:module";
import * as fs from "node:fs";
import * as path from "node:path";

const server = path.resolve(Deno.args[0] ?? ".");
const nodeModules = path.join(server, "node_modules");
const out = path.join(server, "bundle");
const builtins = new Set(builtinModules);
const CODE = /\.(c|m)?js$/;
// Files beside a package's code that it never reads at run time: code and
// metadata already inside the bundle, type declarations, source maps, docs,
// dotfiles.
const BUNDLED = /\.(c|m)?(j|t)s$|\.json$|\.map$|\.d\.(c|m)?ts$|\.(md|markdown)$|^\./;

// Module folders (relative to node_modules) that find files beside them.
const locating = new Set();
// Every package folder the bundle took code from.
const packages = new Set();

function packageRoot(file) {
  const rel = path.relative(nodeModules, file).split(path.sep);
  const take = rel[0].startsWith("@") ? 2 : 1;
  // Nested node_modules: the innermost package owns the file.
  let end = take;
  for (let i = 0; i < rel.length; i++) {
    if (rel[i] === "node_modules" && i + 1 < rel.length) {
      end = i + 1 + (rel[i + 1].startsWith("@") ? 2 : 1);
    }
  }
  return rel.slice(0, end).join("/");
}

const plugin = {
  name: "ytop",
  setup(build) {
    // Node built-ins stay imports, always with the node: prefix Deno wants.
    build.onResolve({ filter: /^[^./]/ }, (args) => {
      const name = args.path.replace(/^node:/, "");
      // Exact names only: "punycode/" is the npm package, not the built-in.
      if (builtins.has(name)) {
        return { path: `node:${name}`, external: true };
      }
      return undefined;
    });
    build.onLoad({ filter: CODE }, async (args) => {
      if (!args.path.startsWith(nodeModules + path.sep)) return undefined;
      packages.add(packageRoot(args.path));
      let text = await fs.promises.readFile(args.path, "utf8");
      // ``const require = createRequire(import.meta.url)`` then
      // ``require("x.json")``: drop the local require so esbuild bundles
      // what it loads instead of leaving a run-time read.
      text = text.replace(
        /\b(const|let|var)\s+require\s*=\s*createRequire\(\s*import\.meta\.url\s*\)\s*;?/g,
        "",
      );
      // ``require.resolve("./x.js")`` only names a file (jsdom's sync-XHR
      // worker, never run here): point it into node_modules, unread.
      const modDir = path.relative(nodeModules, path.dirname(args.path))
        .split(path.sep).join("/");
      text = text.replace(
        /\brequire\.resolve\(\s*(["'])(\.{1,2}\/[^"']+)\1\s*\)/g,
        (_, _q, rel) => `__ytop_path(new URL(${JSON.stringify(
          "../node_modules/" + path.posix.join(modDir, rel))}, __ytop_base))`,
      );
      const usesDir = /\b__(dirname|filename)\b/.test(text);
      const usesUrl = /\bimport\.meta\.(url|dirname|filename)\b/.test(text);
      if (!usesDir && !usesUrl) return { contents: text, loader: "js" };
      const dir = path.relative(nodeModules, path.dirname(args.path))
        .split(path.sep).join("/");
      const file = path.relative(nodeModules, args.path)
        .split(path.sep).join("/");
      locating.add(dir);
      // Never ``import.meta`` inside a module: that makes esbuild treat a
      // CommonJS module as ESM.  The banner defines ``__ytop_base``.
      const at = (rel) => `new URL(${JSON.stringify("../node_modules/" + rel)}, __ytop_base)`;
      if (usesUrl) {
        text = text
          .replace(/\bimport\.meta\.url\b/g, `${at(file)}.href`)
          .replace(/\bimport\.meta\.dirname\b/g, `__ytop_path(${at(dir + "/")})`)
          .replace(/\bimport\.meta\.filename\b/g, `__ytop_path(${at(file)})`);
      }
      if (usesDir) {
        // A module wrapper (CommonJS) or the module's own scope (ESM).
        text = `var __dirname = __ytop_path(${at(dir + "/")});\n`
          + `var __filename = __ytop_path(${at(file)});\n` + text;
      }
      return { contents: text, loader: "js" };
    });
  },
};

fs.rmSync(out, { recursive: true, force: true });
fs.mkdirSync(out, { recursive: true });
await esbuild.build({
  entryPoints: [path.join(server, "src", "generate_once.ts")],
  absWorkingDir: server,
  bundle: true,
  format: "esm",
  platform: "node",
  target: "es2022",
  outfile: path.join(out, "generate_once.js"),
  // jsdom's optional native canvas: never installed, never needed.
  external: ["canvas"],
  legalComments: "none",
  logLevel: "warning",
  banner: {
    js: [
      'import { createRequire as __ytop_cr } from "node:module";',
      'import { fileURLToPath as __ytop_f2p } from "node:url";',
      "const __ytop_base = import.meta.url;",
      "const require = __ytop_cr(__ytop_base);",
      "const __ytop_path = (u) => __ytop_f2p(u).replace(/[\\\\/]$/, '');",
    ].join("\n"),
  },
  plugins: [plugin],
});
esbuild.stop();

// Files the locating modules may read: everything that is not code in their
// folders and below (stylesheets, WebAssembly, ...).
const assets = new Set();
for (const dir of locating) {
  const root = path.join(nodeModules, dir);
  const pkg = packageRoot(path.join(root, "x"));
  const walk = (folder) => {
    for (const entry of fs.readdirSync(folder, { withFileTypes: true })) {
      const full = path.join(folder, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== "node_modules") walk(full);
      } else if (entry.isFile() && !BUNDLED.test(entry.name)
                 && !/^(license|licence|copying|notice|readme|changelog)/i.test(entry.name)) {
        assets.add(path.relative(nodeModules, full).split(path.sep).join("/"));
      }
    }
  };
  // Assets often sit beside the package root, not the module (../x.wasm).
  walk(path.join(nodeModules, pkg));
}
fs.writeFileSync(path.join(out, "assets.json"),
  JSON.stringify([...assets].sort(), null, 2) + "\n");

// One licence file instead of one per package.
const licences = [];
for (const pkg of [...packages].sort()) {
  const root = path.join(nodeModules, pkg);
  let meta = {};
  try {
    meta = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  } catch { /* no package.json */ }
  const files = fs.readdirSync(root)
    .filter((n) => /^(license|licence|copying|notice)/i.test(n)).sort();
  const header = `${meta.name ?? pkg} ${meta.version ?? ""} (${meta.license ?? "see below"})`;
  const body = files.map((n) => fs.readFileSync(path.join(root, n), "utf8").trim());
  licences.push(`${"=".repeat(72)}\n${header}\n${"=".repeat(72)}\n${body.join("\n\n") || "(no licence file)"}\n`);
}
fs.writeFileSync(path.join(out, "THIRD_PARTY_LICENSES.txt"), licences.join("\n"));
console.log(JSON.stringify({ packages: packages.size, assets: assets.size, locating: [...locating].sort() }));
