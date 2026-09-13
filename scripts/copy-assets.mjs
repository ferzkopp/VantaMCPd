#!/usr/bin/env node
// tsc only emits .js, so the dashboard's static files are copied into dist/ alongside it.
import { cpSync, existsSync, mkdirSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const from = path.join(root, "src", "web");
const to = path.join(root, "dist", "web");

if (!existsSync(from)) {
  console.error(`copy-assets: ${from} does not exist`);
  process.exit(1);
}

cpSync(from, to, { recursive: true });
const jobsFrom = path.join(root, "src", "jobs", "remote-runner.py");
const jobsTo = path.join(root, "dist", "jobs", "remote-runner.py");
mkdirSync(path.dirname(jobsTo), { recursive: true });
cpSync(jobsFrom, jobsTo);
console.log(`copied ${readdirSync(to).join(", ")} -> dist/web`);
