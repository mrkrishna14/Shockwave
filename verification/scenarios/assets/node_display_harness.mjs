// Shockwave native regression scenario: the displayed-price invariant (Node.js consumers).
//
// Staged by verification.scenarios.staging as
// .shockwave/scenarios/<scenario_id>/shockwave_display_invariant.test.mjs next to
// binding.json inside the runner's disposable checkout, and executed natively with
// `node --test <file>`. TypeScript consumers are imported directly: Node >= 22.18 strips
// erasable type syntax natively, so no build step or third-party dependency is needed.
//
// Protocol (same as the Python harness): binding.json names the consumer entry point and
// the invariant expectation (the displayed price is $10.00 for equivalent current/proposed
// payment responses). SHOCKWAVE_SCENARIO_ID, SHOCKWAVE_SCENARIO_PHASE,
// SHOCKWAVE_FIXTURE_ID, SHOCKWAVE_FIXTURE_PATH (fixture whose response.body is served),
// SHOCKWAVE_REPORT_PATH and SHOCKWAVE_REPO_ROOT come from the runner environment.
// globalThis.fetch is replaced for the rest of the process by a fake that serves the
// fixture body and records the URL (the real fetch is never called).
//
// Network guard: best-effort, in-process interception, NOT network isolation. Before the
// bound function runs, and for the rest of the process, net.Socket connects (net, http,
// https, tls), dgram sends and node:dns lookups and resolves throw ECONNREFUSED. Worker
// threads (a fresh realm with the real fetch and modules), child processes and native
// addons are not covered, so the runner must isolate the network for every profile; the
// guard only keeps the consumer's ordinary HTTP calls on the fake. `node --test <file>`
// loads no preload or runner configuration from the checkout (node.config.json and .env
// need explicit flags).
//
// The report is always written (finally). "failed" means only that the relevant
// comparison ran and the displayed value differed; anything that prevented it (missing
// module, syntax error, missing export, any exception) is "error", which Shockwave
// interprets as inconclusive.

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import dgram from "node:dgram";
import dns from "node:dns";
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import net from "node:net";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCHEMA_VERSION = "shockwave.v1";
const REPORT_KIND = "scenario_report";
const MAX_MESSAGE_CHARS = 500;
const HARNESS_FILE = fileURLToPath(import.meta.url);
const BINDING_FILE = path.join(path.dirname(HARNESS_FILE), "binding.json");

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function bounded(text) {
  const value = String(text);
  return value.length <= MAX_MESSAGE_CHARS ? value : `${value.slice(0, MAX_MESSAGE_CHARS - 3)}...`;
}

function requiredEnv(name) {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set by the runner`);
  }
  return value;
}

function errorType(error) {
  if (error && typeof error.code === "string" && error.code.startsWith("ERR_")) {
    return error.code; // e.g. ERR_MODULE_NOT_FOUND, ERR_UNSUPPORTED_TYPESCRIPT_SYNTAX
  }
  if (error && typeof error.name === "string" && error.name) {
    return error.name;
  }
  return typeof error;
}

function errorMessage(error) {
  return error && typeof error.message === "string" ? error.message : String(error);
}

function requestUrl(input) {
  if (typeof input === "string") {
    return input;
  }
  if (input instanceof URL) {
    return input.href;
  }
  if (input && typeof input.url === "string") {
    return input.url;
  }
  return String(input);
}

function refuseNetwork() {
  const error = new Error("shockwave harness: real network access is disabled");
  error.code = "ECONNREFUSED";
  throw error;
}

function installFakeFetch(body, requestedUrls) {
  // Never restored: a late call must not reach the real fetch either.
  globalThis.fetch = async (input) => {
    requestedUrls.push(requestUrl(input));
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };
}

const DNS_QUERY = /^(lookup|resolve|reverse)/;

// Best-effort, in-process interception, not network isolation (see the header); never restored.
function installNetworkGuard() {
  net.Socket.prototype.connect = refuseNetwork;
  dgram.Socket.prototype.send = refuseNetwork;
  for (const target of [dns, dns.promises, dns.Resolver.prototype, dns.promises.Resolver.prototype]) {
    for (const name of Object.getOwnPropertyNames(target)) {
      if (DNS_QUERY.test(name) && typeof target[name] === "function") {
        target[name] = refuseNetwork;
      }
    }
  }
  syncBuiltinESMExports(); // named imports such as `import { lookup } from "node:dns"` are guarded too
}

const HTTP_INJECTIONS = new Map([["js_global_fetch", installFakeFetch]]);

function newReport() {
  let harnessSha256 = null;
  try {
    harnessSha256 = sha256(readFileSync(HARNESS_FILE));
  } catch {
    harnessSha256 = null;
  }
  // Keys in sorted order, matching the Python harness's sort_keys output.
  return {
    actual: null,
    assertion_id: null,
    binding_sha256: null,
    error_type: null,
    expected: null,
    fixture_id: process.env.SHOCKWAVE_FIXTURE_ID ?? null,
    harness_sha256: harnessSha256,
    kind: REPORT_KIND,
    message: "the scenario did not complete",
    phase: process.env.SHOCKWAVE_SCENARIO_PHASE ?? null,
    requested_urls: [],
    scenario_id: process.env.SHOCKWAVE_SCENARIO_ID ?? null,
    schema_version: SCHEMA_VERSION,
    status: "error",
  };
}

function writeReport(report) {
  const target = process.env.SHOCKWAVE_REPORT_PATH;
  if (!target) {
    process.stderr.write("shockwave harness: no report path was provided; the report was not written\n");
    return;
  }
  const temporary = `${target}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(report, null, 2)}\n`);
  renameSync(temporary, target);
}

async function runScenario(report) {
  const bindingRaw = readFileSync(BINDING_FILE);
  report.binding_sha256 = sha256(bindingRaw);
  const binding = JSON.parse(bindingRaw.toString("utf8"));
  report.scenario_id = binding.scenario_id;
  report.assertion_id = binding.assertion_id;
  report.expected = binding.expected_display;
  if (process.env.SHOCKWAVE_SCENARIO_ID !== binding.scenario_id) {
    throw new Error("SHOCKWAVE_SCENARIO_ID does not match binding.json");
  }

  const fixture = JSON.parse(readFileSync(requiredEnv("SHOCKWAVE_FIXTURE_PATH"), "utf8"));
  if (fixture.fixture_id !== process.env.SHOCKWAVE_FIXTURE_ID) {
    throw new Error("the fixture document does not match SHOCKWAVE_FIXTURE_ID");
  }

  const target = binding.binding;
  const install = HTTP_INJECTIONS.get(target.http_injection);
  if (install === undefined) {
    throw new Error(`unsupported http_injection ${JSON.stringify(target.http_injection)}`);
  }
  install(fixture.response.body, report.requested_urls);
  installNetworkGuard();
  const moduleUrl = pathToFileURL(path.join(requiredEnv("SHOCKWAVE_REPO_ROOT"), target.module)).href;
  const consumer = await import(moduleUrl);
  const render = consumer[target.function];
  if (typeof render !== "function") {
    throw new TypeError(`${target.module} does not export a function named ${target.function}`);
  }
  const actual = await render(...target.args);
  if (typeof actual !== "string") {
    throw new TypeError(`${target.module}#${target.function} returned ${typeof actual}, not a display string`);
  }
  return { actual, expected: binding.expected_display };
}

test("displayed price invariant", async () => {
  const report = newReport();
  let outcome;
  try {
    outcome = await runScenario(report);
    report.actual = outcome.actual;
    report.status = outcome.actual === outcome.expected ? "passed" : "failed";
    report.message = bounded(`expected ${outcome.expected}, actual ${outcome.actual}`);
  } catch (error) {
    report.status = "error";
    report.error_type = errorType(error);
    report.message = bounded(`${errorType(error)}: ${errorMessage(error)}`);
    throw error;
  } finally {
    writeReport(report);
  }
  assert.strictEqual(outcome.actual, outcome.expected, report.message);
});
