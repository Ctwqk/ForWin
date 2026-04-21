import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const SOURCE_DIR = __dirname;
const DIST_ROOT = path.resolve(SOURCE_DIR, '..', 'dist');

export const EXTENSION_TARGETS = ['chromium', 'firefox'];

function cloneManifest(sourceManifest) {
  return JSON.parse(JSON.stringify(sourceManifest));
}

export function buildManifestForTarget(target, sourceManifest) {
  const manifest = cloneManifest(sourceManifest);

  if (target === 'chromium') {
    return manifest;
  }

  if (target !== 'firefox') {
    throw new Error(`Unsupported extension target: ${target}`);
  }

  manifest.permissions = (manifest.permissions || []).filter((permission) => permission !== 'debugger');
  manifest.background = {
    scripts: ['background.js'],
    type: 'module',
  };
  delete manifest.options_page;
  manifest.options_ui = {
    page: 'options.html',
    open_in_tab: true,
  };
  manifest.browser_specific_settings = {
    gecko: {
      id: 'forwin-publisher@example.com',
    },
  };

  return manifest;
}

async function readSourceManifest() {
  const raw = await fs.readFile(path.join(SOURCE_DIR, 'manifest.json'), 'utf8');
  return JSON.parse(raw);
}

async function copySourceTree(destinationDir) {
  await fs.rm(destinationDir, { recursive: true, force: true });
  await fs.mkdir(destinationDir, { recursive: true });

  const entries = await fs.readdir(SOURCE_DIR, { withFileTypes: true });
  for (const entry of entries) {
    if (entry.name === 'tests' || entry.name === 'node_modules') {
      continue;
    }
    await fs.cp(
      path.join(SOURCE_DIR, entry.name),
      path.join(destinationDir, entry.name),
      { recursive: true },
    );
  }
}

export async function buildTarget(target) {
  const sourceManifest = await readSourceManifest();
  const manifest = buildManifestForTarget(target, sourceManifest);
  const destinationDir = path.join(DIST_ROOT, `forwin-publisher-${target}`);

  await copySourceTree(destinationDir);
  await fs.writeFile(
    path.join(destinationDir, 'manifest.json'),
    `${JSON.stringify(manifest, null, 2)}\n`,
    'utf8',
  );

  return {
    target,
    destinationDir,
    manifest,
  };
}

export async function buildTargets(targets = EXTENSION_TARGETS) {
  const selectedTargets = Array.isArray(targets) ? targets : [targets];
  return Promise.all(selectedTargets.map((target) => buildTarget(target)));
}

async function main() {
  const target = process.argv[2] || 'all';
  const selectedTargets = target === 'all' ? EXTENSION_TARGETS : [target];
  await buildTargets(selectedTargets);
}

if (process.argv[1] && path.resolve(process.argv[1]) === __filename) {
  await main();
}
