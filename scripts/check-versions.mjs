#!/usr/bin/env node
/**
 * check-versions.mjs — version & release-config consistency gate (no deps).
 *
 * Single source of truth for the app version is the root package.json.
 * tauri.conf.json must either point at it ("version": "../package.json",
 * which Tauri 2 resolves at build time) or carry the identical literal
 * version, and src-tauri/Cargo.toml's [package] version must match too —
 * a drift between the three produces installers whose NSIS version,
 * updater manifest and sidecar-reported version disagree.
 *
 * Usage:
 *   node scripts/check-versions.mjs            dev mode (empty updater
 *                                              pubkey is a WARN)
 *   node scripts/check-versions.mjs --release  release mode (empty updater
 *                                              pubkey is a hard FAIL: an
 *                                              unsigned/unverifiable update
 *                                              channel must never ship)
 *
 * Exit code 0 = all hard checks pass, 1 = at least one hard check failed.
 * Every failure prints a FAIL line naming the file and both values.
 */
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const releaseMode = process.argv.includes("--release");

// The bundle identifier is part of the installed app's identity (registry
// keys, update channel, data dirs). It must never change after first ship.
const EXPECTED_IDENTIFIER = "com.youtubeagent.app";

let failures = 0;
let warnings = 0;
const fail = (msg) => { failures++; console.log(`FAIL  ${msg}`); };
const warn = (msg) => { warnings++; console.log(`WARN  ${msg}`); };
const pass = (msg) => console.log(`PASS  ${msg}`);

function readJson(rel) {
  return JSON.parse(readFileSync(resolve(root, rel), "utf8"));
}

// --- package.json: the truth -------------------------------------------------
const pkg = readJson("package.json");
const pkgVersion = pkg.version;
if (!pkgVersion || !/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(pkgVersion)) {
  fail(`package.json: version "${pkgVersion}" is missing or not semver`);
} else {
  pass(`package.json version = ${pkgVersion} (source of truth)`);
}

// --- tauri.conf.json ---------------------------------------------------------
const tauri = readJson("src-tauri/tauri.conf.json");
let tauriVersion = tauri.version;
if (typeof tauriVersion === "string" && tauriVersion.endsWith(".json")) {
  // Pointer form: resolve relative to src-tauri/ and read the target.
  const target = resolve(root, "src-tauri", tauriVersion);
  const targetVersion = JSON.parse(readFileSync(target, "utf8")).version;
  pass(`tauri.conf.json version is pointer "${tauriVersion}" -> ${targetVersion}`);
  tauriVersion = targetVersion;
}
if (tauriVersion !== pkgVersion) {
  fail(`version mismatch: tauri.conf.json resolves to "${tauriVersion}" but package.json is "${pkgVersion}"`);
} else {
  pass(`tauri.conf.json version matches package.json (${tauriVersion})`);
}

// Identifier must be unchanged.
if (tauri.identifier !== EXPECTED_IDENTIFIER) {
  fail(`identifier changed: expected "${EXPECTED_IDENTIFIER}", found "${tauri.identifier}" (never change the identifier after first release)`);
} else {
  pass(`identifier unchanged (${EXPECTED_IDENTIFIER})`);
}

// Sidecar must be bundled, or the installed shell has no backend to spawn.
const externalBin = tauri.bundle?.externalBin;
if (!Array.isArray(externalBin) || externalBin.length === 0) {
  fail(`bundle.externalBin is empty/missing in tauri.conf.json — the "agent-backend" sidecar would not be bundled`);
} else if (!externalBin.some((e) => /(^|\/)agent-backend$/.test(e))) {
  fail(`bundle.externalBin ${JSON.stringify(externalBin)} does not include the "agent-backend" sidecar`);
} else {
  pass(`bundle.externalBin includes sidecar: ${JSON.stringify(externalBin)}`);
}

// --- Cargo.toml [package] version --------------------------------------------
const cargoToml = readFileSync(resolve(root, "src-tauri/Cargo.toml"), "utf8");
let cargoVersion = null;
{
  // Only the version inside the [package] table counts — dependency tables
  // below it carry their own version keys that must not be picked up.
  let inPackage = false;
  for (const line of cargoToml.split(/\r?\n/)) {
    const header = line.match(/^\s*\[([^\]]+)\]\s*$/);
    if (header) { inPackage = header[1].trim() === "package"; continue; }
    if (inPackage) {
      const m = line.match(/^\s*version\s*=\s*"([^"]+)"/);
      if (m) { cargoVersion = m[1]; break; }
    }
  }
}
if (cargoVersion === null) {
  fail(`src-tauri/Cargo.toml: no version found in the [package] table`);
} else if (cargoVersion !== pkgVersion) {
  fail(`version mismatch: Cargo.toml [package] version is "${cargoVersion}" but package.json is "${pkgVersion}"`);
} else {
  pass(`Cargo.toml [package] version matches package.json (${cargoVersion})`);
}

// --- Updater pubkey ------------------------------------------------------------
// Empty pubkey = the updater can never verify a signed update manifest.
// Tolerable while developing; a hard failure for a release build.
const pubkey = (tauri.plugins?.updater?.pubkey ?? "").trim();
if (pubkey.length === 0) {
  const msg = `plugins.updater.pubkey is EMPTY in tauri.conf.json — no update can be signature-verified until the minisign public key from \`tauri signer generate\` is pasted in (see src-tauri/keys/)`;
  if (releaseMode) fail(`${msg} [release mode: blocking]`);
  else warn(`${msg} [dev mode: warning only; run with --release to enforce]`);
} else {
  pass(`plugins.updater.pubkey is set (${pubkey.length} chars)`);
}

// --- Summary -------------------------------------------------------------------
if (failures > 0) {
  console.log(`\ncheck-versions: ${failures} failure(s), ${warnings} warning(s) — NOT release-ready`);
  process.exit(1);
}
console.log(`\ncheck-versions: all versions agree on ${pkgVersion}${warnings ? ` (${warnings} warning(s))` : ""}`);
