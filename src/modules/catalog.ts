import { createHash } from "node:crypto";
import { lstatSync, readFileSync, readdirSync } from "node:fs";
import type { Dirent } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseModuleManifest, type ModuleManifest } from "./manifest.js";

const MAX_PACKAGE_FILES = 256;
const MAX_PACKAGE_BYTES = 50 * 1024 * 1024;
const SAFE_PACKAGE_PATH = /^[A-Za-z0-9][A-Za-z0-9._/-]*$/;

export interface ModulePackageFile {
  relativePath: string;
  absolutePath: string;
  size: number;
  sha256: string;
}

export interface ModulePackage {
  directory: string;
  manifest: ModuleManifest;
  files: ModulePackageFile[];
  totalBytes: number;
}

export interface ModuleCatalogError {
  directory: string;
  error: string;
}

export interface ModuleCatalog {
  root: string;
  modules: ModulePackage[];
  errors: ModuleCatalogError[];
}

export function defaultModuleRoot(): string {
  return path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..", "modules");
}

/**
 * One line per module naming what it can do. This is the only place the agent learns that the cluster
 * handles, say, YAML or secret scanning: the module's own tools are behind the proxy and never appear
 * in the daemon's tool list.
 */
export function capabilitySummary(catalog: ModuleCatalog): string {
  const lines = catalog.modules
    .filter((modulePackage) => modulePackage.manifest.capabilities.length > 0)
    // Semicolons, because the phrases themselves contain commas.
    .map((modulePackage) => `- ${modulePackage.manifest.id}: ${modulePackage.manifest.capabilities.join("; ")}`);
  return lines.join("\n");
}


function packageFiles(directory: string): ModulePackageFile[] {
  const files: ModulePackageFile[] = [];

  const visit = (current: string): void => {
    for (const entry of readdirSync(current, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const absolutePath = path.join(current, entry.name);
      const stat = lstatSync(absolutePath);
      if (stat.isSymbolicLink()) throw new Error(`symbolic links are not allowed: ${path.relative(directory, absolutePath)}`);
      if (stat.isDirectory()) {
        visit(absolutePath);
        continue;
      }
      if (!stat.isFile()) throw new Error(`unsupported package entry: ${path.relative(directory, absolutePath)}`);

      const relativePath = path.relative(directory, absolutePath).split(path.sep).join("/");
      if (!SAFE_PACKAGE_PATH.test(relativePath)) {
        throw new Error(`package path contains unsupported characters: ${JSON.stringify(relativePath)}`);
      }
      const content = readFileSync(absolutePath);
      files.push({
        relativePath,
        absolutePath,
        size: stat.size,
        sha256: createHash("sha256").update(content).digest("hex"),
      });
      if (files.length > MAX_PACKAGE_FILES) throw new Error(`package contains more than ${MAX_PACKAGE_FILES} files`);
      if (files.reduce((sum, file) => sum + file.size, 0) > MAX_PACKAGE_BYTES) {
        throw new Error(`package exceeds ${MAX_PACKAGE_BYTES} bytes`);
      }
    }
  };

  visit(directory);
  return files;
}

function sharedPackageFiles(root: string, relativePaths: string[]): ModulePackageFile[] {
  return relativePaths.map((relativePath) => {
    const absolutePath = path.join(root, ...relativePath.split("/"));
    const stat = lstatSync(absolutePath);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`shared file must be a regular file: ${relativePath}`);
    const content = readFileSync(absolutePath);
    return {
      relativePath,
      absolutePath,
      size: stat.size,
      sha256: createHash("sha256").update(content).digest("hex"),
    };
  });
}

function loadPackage(directory: string): ModulePackage {
  const manifestPath = path.join(directory, "module.json");
  const stat = lstatSync(manifestPath);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error("module.json must be a regular file");

  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(manifestPath, "utf8"));
  } catch (err) {
    throw new Error(`cannot parse module.json: ${(err as Error).message}`);
  }
  const manifest = parseModuleManifest(raw, manifestPath);
  if (path.basename(directory) !== manifest.id) throw new Error(`directory name must match module ID ${manifest.id}`);

  const files = [...packageFiles(directory), ...sharedPackageFiles(path.dirname(directory), manifest.sharedFiles)];
  const duplicate = files.find((file, index) => files.findIndex((candidate) => candidate.relativePath === file.relativePath) !== index);
  if (duplicate) throw new Error(`shared file collides with package path: ${duplicate.relativePath}`);
  if (files.length > MAX_PACKAGE_FILES) throw new Error(`package contains more than ${MAX_PACKAGE_FILES} files`);
  if (files.reduce((sum, file) => sum + file.size, 0) > MAX_PACKAGE_BYTES) throw new Error(`package exceeds ${MAX_PACKAGE_BYTES} bytes`);
  const names = new Set(files.map((file) => file.relativePath));
  const requiredFiles = [manifest.lifecycle.install, manifest.lifecycle.uninstall];
  if (manifest.runtime.mode === "service") requiredFiles.push(manifest.runtime.systemdUnit);
  for (const required of requiredFiles) {
    if (!names.has(required)) throw new Error(`manifest references missing file: ${required}`);
  }
  const entrypointFile = manifest.entrypoint.find((part) => part.includes("/")) ?? manifest.entrypoint[1];
  if (entrypointFile && !names.has(entrypointFile)) throw new Error(`entrypoint references missing file: ${entrypointFile}`);

  return { directory, manifest, files, totalBytes: files.reduce((sum, file) => sum + file.size, 0) };
}

export function loadModuleCatalog(root = defaultModuleRoot()): ModuleCatalog {
  const modules: ModulePackage[] = [];
  const errors: ModuleCatalogError[] = [];
  const seen = new Set<string>();

  let entries: Dirent<string>[];
  try {
    entries = readdirSync(root, { withFileTypes: true, encoding: "utf8" });
  } catch (err) {
    return { root, modules, errors: [{ directory: root, error: (err as Error).message }] };
  }

  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
    const directory = path.join(root, entry.name);
    try {
      const modulePackage = loadPackage(directory);
      if (seen.has(modulePackage.manifest.id)) throw new Error(`duplicate module ID: ${modulePackage.manifest.id}`);
      seen.add(modulePackage.manifest.id);
      modules.push(modulePackage);
    } catch (err) {
      errors.push({ directory, error: (err as Error).message });
    }
  }

  return { root, modules, errors };
}