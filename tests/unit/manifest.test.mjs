/**
 * Unit tests for the package/plugin manifest deliverables.
 *
 * These guard the install contract: bundle patch wiring, exports the harness
 * plugin inventory reads, locale/icon assets, TypeScript config, and the
 * zero-dependency rule. No Python or network is involved.
 */

import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

import { repoRoot } from "./_helpers.mjs";

function read(relative) {
  return readFileSync(join(repoRoot, relative), "utf8");
}

function readJson(relative) {
  return JSON.parse(read(relative));
}

const pkg = readJson("package.json");

test("package.json declares the host-plugin manifest", () => {
  assert.equal(pkg.name, "@genzhelu/dsh-lifesci");
  assert.equal(pkg.version, "0.2.0");
  assert.equal(pkg.private, true);
  assert.equal(pkg.type, "module");
  assert.equal(pkg.main, "dist/index.js");
  assert.equal(pkg.icon, "icon.svg", "plugin inventory reads a manifest icon");
  assert.equal(pkg.dsh.bundle.patch, "./cordis.patch.yml");
  assert.deepEqual(pkg.exports, {
    ".": "./dist/index.js",
    "./locale/*.json": "./locale/*.json",
    "./icon": "./icon.svg",
    "./package.json": "./package.json",
  });
  assert.deepEqual(Object.keys(pkg.devDependencies).sort(), ["@types/node", "typescript"]);
  assert.equal(pkg.dependencies, undefined, "the plugin must stay zero-runtime-dependency");
});

test("package, Python package, and pyproject versions stay in sync", () => {
  const init = read("python/lifesci/__init__.py");
  const initVersion = /__version__\s*=\s*"([^"]+)"/.exec(init)?.[1];
  const pyprojectVersion = /^version\s*=\s*"([^"]+)"/m.exec(read("python/pyproject.toml"))?.[1];
  assert.equal(initVersion, pkg.version, "python/lifesci/__init__.py version must match package.json");
  assert.equal(pyprojectVersion, pkg.version, "python/pyproject.toml version must match package.json");
});

test("package.json files list ships everything the Python layer and harness need", () => {
  const required = [
    "dist",
    "python",
    "schemas",
    "cordis.patch.yml",
    "locale",
    "icon.svg",
    "docs",
    "scripts",
    "README.md",
    "install.md",
    "requirements.txt",
    "requirements-dev.txt",
  ];
  for (const entry of required) assert.ok(pkg.files.includes(entry), `files must include ${entry}`);
});

test("package.json scripts are the documented build/test commands", () => {
  assert.equal(pkg.scripts.build, "node scripts/generate-tool-specs.mjs && tsc -p tsconfig.json");
  assert.equal(pkg.scripts.typecheck, "tsc -p tsconfig.json --noEmit");
  assert.equal(pkg.scripts.test, "node scripts/run-tests.mjs");
  assert.equal(pkg.scripts["test:node"], "node --test tests/unit/*.test.mjs tests/golden/*.test.mjs");
  assert.equal(pkg.scripts["test:network"], "node --test tests/network/*.test.mjs && node scripts/python-tests.mjs --network");
});

test("package exports point at files that exist after a build", () => {
  assert.ok(existsSync(join(repoRoot, "dist", "index.js")), "dist/index.js must exist (run npm run build)");
  assert.ok(existsSync(join(repoRoot, "icon.svg")));
  assert.ok(existsSync(join(repoRoot, "locale", "en.json")));
  assert.ok(existsSync(join(repoRoot, "locale", "zh.json")));
});

test("cordis.patch.yml inserts exactly one lifesci host row", () => {
  const patch = read("cordis.patch.yml");
  assert.equal((patch.match(/^- insert:/gm) ?? []).length, 1, "one insert list");
  assert.match(patch, /^ {4}- id: lifesci$/m);
  assert.match(patch, /^ {6}name: '@genzhelu\/dsh-lifesci'$/m);
  assert.match(patch, /^ {6}config: \{\}$/m);
});

test("locale files expose real plugin metadata in English and Chinese", () => {
  const en = readJson("locale/en.json");
  const zh = readJson("locale/zh.json");
  for (const [label, locale] of [
    ["en", en],
    ["zh", zh],
  ]) {
    assert.equal(typeof locale.meta?.title, "string", `${label}.meta.title`);
    assert.equal(typeof locale.meta?.description, "string", `${label}.meta.description`);
    assert.ok(locale.meta.title.trim().length > 0, `${label} title is not empty`);
    assert.ok(locale.meta.description.trim().length >= 30, `${label} description is a real description`);
  }
  assert.notEqual(en.meta.title, zh.meta.title, "the locales are actually translated");
  assert.match(zh.meta.title, /[\u4e00-\u9fff]/, "zh title contains Chinese characters");
  assert.match(zh.meta.description, /[\u4e00-\u9fff]/, "zh description contains Chinese characters");
});

test("icon.svg is original helix + data-point artwork below 256 KiB", () => {
  const path = join(repoRoot, "icon.svg");
  const bytes = statSync(path).size;
  assert.ok(bytes > 0);
  assert.ok(bytes <= 256 * 1024, `icon.svg is ${bytes} bytes; limit is 262144`);
  const svg = read("icon.svg").trim();
  assert.ok(svg.startsWith("<svg"), "icon.svg is an SVG document");
  assert.ok(svg.endsWith("</svg>"), "icon.svg is well-closed");
  assert.match(svg, /xmlns="http:\/\/www\.w3\.org\/2000\/svg"/);
  assert.match(svg, /viewBox="0 0 64 64"/);
  assert.match(svg, /<path[\s\S]*<circle/, "helix strands (paths) and data points (circles) are both present");
  assert.match(svg, /<title[^>]*>LifeSci Research<\/title>/, "icon has an accessible title");
});

test("tsconfig.json compiles ESM NodeNext sources into dist/", () => {
  const tsconfig = readJson("tsconfig.json");
  const options = tsconfig.compilerOptions;
  assert.equal(options.module, "NodeNext");
  assert.equal(options.moduleResolution, "NodeNext");
  assert.equal(options.strict, true);
  assert.equal(options.outDir, "dist");
  assert.equal(options.rootDir, "src");
  assert.ok(options.noEmitOnError, "a type error must fail the build");
  assert.ok(tsconfig.include.includes("src/**/*.ts"));
});

/** Collect module specifiers imported by one JS/TS source file. */
function importSpecifiers(source) {
  const specifiers = [];
  const pattern = /(?:from\s+|import\s*\(\s*|require\s*\(\s*)["']([^"']+)["']/g;
  for (const match of source.matchAll(pattern)) specifiers.push(match[1]);
  return specifiers;
}

test("the plugin imports only Node builtins and relative modules (zero dependencies)", () => {
  const sourceFiles = readdirSync(join(repoRoot, "src"))
    .filter((name) => name.endsWith(".ts"))
    .map((name) => join("src", name));
  const distFiles = readdirSync(join(repoRoot, "dist"))
    .filter((name) => name.endsWith(".js"))
    .map((name) => join("dist", name));
  const scriptFiles = readdirSync(join(repoRoot, "scripts"))
    .filter((name) => name.endsWith(".mjs"))
    .map((name) => join("scripts", name));
  for (const relative of [...sourceFiles, ...distFiles, ...scriptFiles]) {
    for (const specifier of importSpecifiers(read(relative))) {
      assert.ok(
        specifier.startsWith("node:") || specifier.startsWith("."),
        `${relative} imports "${specifier}"; only node: builtins and relative modules are allowed`,
      );
    }
  }
});

test("no file in the plugin imports @deepseek-ai packages", () => {
  const directories = ["src", "dist", "scripts"];
  for (const directory of directories) {
    for (const name of readdirSync(join(repoRoot, directory))) {
      if (!/\.(ts|js|mjs)$/.test(name)) continue;
      const relative = join(directory, name);
      for (const specifier of importSpecifiers(read(relative))) {
        assert.ok(
          !specifier.startsWith("@deepseek-ai/"),
          `${relative} imports "${specifier}"; the plugin must stay dependency-free`,
        );
      }
    }
  }
});
