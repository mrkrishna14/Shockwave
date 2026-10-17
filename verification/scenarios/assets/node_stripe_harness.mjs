// Shockwave native scenario harness: Stripe SDK cases (Node.js consumers).
//
// Staged by verification.scenarios.staging as
// .shockwave/scenarios/<scenario_id>/shockwave_stripe_cases.test.mjs next to binding.json
// inside the runner's disposable checkout, and executed natively with `node --test <file>`.
// TypeScript consumers are imported directly: Node >= 22.18 strips erasable type syntax
// natively, so no build step or third-party dependency is needed.
//
// Protocol (binding.json, http_injection "stripe_sdk_env"; same as the Python harness): the
// binding names the consumer's real entry point and its cases (arguments, the field read from
// the result, the invariant expected value, and the PaymentIntent ids the call must request).
// SHOCKWAVE_SCENARIO_ID, SHOCKWAVE_SCENARIO_PHASE, SHOCKWAVE_FIXTURE_ID,
// SHOCKWAVE_FIXTURE_PATH (the fixture set whose `responses` are served),
// SHOCKWAVE_REPORT_PATH and SHOCKWAVE_REPO_ROOT come from the runner environment.
//
// The harness starts an in-process loopback fixture server (a simulation of Stripe's
// GET /v1/payment_intents/{intent} responses; not Stripe, not another provider) on
// 127.0.0.1 and an ephemeral port. Known ids get the fixture body; unknown ids get a
// Stripe-style 404 resource_missing error; other paths get 404 and other methods on the
// operation path 405. Every request is recorded: method, path, the Stripe-Version header and
// only the KIND of the Authorization header ("test_mode_bearer" for "Bearer sk_test_...",
// "missing" or "other"); the key itself is never recorded. Before importing the consumer
// module, the harness sets the binding's key variable (e.g. STRIPE_SECRET_KEY) to a dummy
// test-mode key and its API base variable (e.g. STRIPE_API_BASE) to the server URL, so the
// consumer's own code constructs the official Stripe SDK client against the fixture server.
//
// Network guard: best-effort, in-process interception, NOT network isolation. After the
// fixture server listens, and for the rest of the process, net.Socket connects (net, http,
// https, tls and the undici behind fetch) and globalThis.fetch refuse every target except
// 127.0.0.1 on the fixture server's own port; dgram sends and node:dns lookups and resolves
// always throw ECONNREFUSED. Worker threads (a fresh realm with the real modules), child
// processes and native addons are not covered, so the runner must isolate the network for
// every profile; the guard only keeps the consumer's ordinary HTTP calls on the fixture
// server. `node --test <file>` loads no preload or runner configuration from the checkout
// (node.config.json and .env need explicit flags).
//
// Every case is evaluated (one failure does not skip the others). A case is "failed" only
// when its comparison ran and the value differs from the expectation; an exception, a missing
// result field or a non-JSON result is "error". The report is always written (finally), and
// the test fails iff any case did not pass. Shockwave, not this harness, judges the recorded
// requests: a case without a request, a request other than GET /v1/payment_intents/<the
// case's ids>, a missing Stripe-Version or non-test-mode auth makes the run inconclusive.

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import dgram from "node:dgram";
import dns from "node:dns";
import { readFileSync, renameSync, writeFileSync } from "node:fs";
import http from "node:http";
import { syncBuiltinESMExports } from "node:module";
import net from "node:net";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const SCHEMA_VERSION = "shockwave.v1";
const REPORT_KIND = "scenario_report";
const REPORT_FORMAT = "shockwave.cases.v1";
const OFFLINE_TEST_KEY = "sk_test_shockwave_offline"; // dummy test-mode key; never a real credential
const LOOPBACK_HOST = "127.0.0.1";
const MAX_MESSAGE_CHARS = 500;
const MAX_PATH_CHARS = 200;
const MAX_HEADER_CHARS = 64;
const MAX_RECORDED_REQUESTS = 64;
const MAX_RESULT_CHARS = 4000;
const HARNESS_FILE = fileURLToPath(import.meta.url);
const BINDING_FILE = path.join(path.dirname(HARNESS_FILE), "binding.json");

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function bounded(text, limit = MAX_MESSAGE_CHARS) {
  const value = String(text);
  return value.length <= limit ? value : `${value.slice(0, limit - 3)}...`;
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

// Sorted keys at every level, matching the Python harness's sort_keys output.
function sortKeys(value) {
  if (Array.isArray(value)) {
    return value.map(sortKeys);
  }
  if (value !== null && typeof value === "object") {
    const sorted = {};
    for (const key of Object.keys(value).sort()) {
      sorted[key] = sortKeys(value[key]);
    }
    return sorted;
  }
  return value;
}

function canonical(value) {
  return JSON.stringify(sortKeys(value));
}

function authorizationKind(value) {
  if (!value) {
    return "missing";
  }
  const space = value.indexOf(" ");
  const scheme = space === -1 ? value : value.slice(0, space);
  const credential = space === -1 ? "" : value.slice(space + 1);
  return scheme.toLowerCase() === "bearer" && credential.startsWith("sk_test_") ? "test_mode_bearer" : "other";
}

function headerValue(request, name) {
  const value = request.headers[name];
  return Array.isArray(value) ? value[0] : value;
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function operationPattern(pathTemplate) {
  const segments = pathTemplate
    .replace(/^\/+|\/+$/g, "")
    .split("/")
    .map((segment) => (segment.startsWith("{") && segment.endsWith("}") ? "([^/]+)" : escapeRegExp(segment)));
  return new RegExp(`^/${segments.join("/")}$`);
}

function stripeError(message, code, param) {
  const error = { type: "invalid_request_error", message };
  if (code !== undefined) {
    error.code = code;
  }
  if (param !== undefined) {
    error.param = param;
  }
  return { error };
}

// Serve `responses` for GET <pathTemplate> on 127.0.0.1 (ephemeral port).
function startFixtureServer(responses, pathTemplate, log) {
  const pattern = operationPattern(pathTemplate);
  const server = http.createServer((request, response) => {
    const requestPath = (request.url ?? "").split("?")[0];
    const stripeVersion = headerValue(request, "stripe-version");
    const entry = {
      authorization: authorizationKind(headerValue(request, "authorization")),
      method: bounded(request.method ?? "", 16),
      path: bounded(requestPath, MAX_PATH_CHARS),
      stripe_version: stripeVersion === undefined ? null : bounded(stripeVersion, MAX_HEADER_CHARS),
    };
    if (log.entries.length >= MAX_RECORDED_REQUESTS) {
      log.dropped += 1;
    } else {
      log.entries.push(entry);
    }
    request.resume(); // drain any body
    const match = pattern.exec(requestPath);
    let status;
    let body;
    if (match === null) {
      status = 404;
      body = stripeError(`Unrecognized request URL (${request.method}: ${bounded(requestPath, 120)}).`);
    } else if (request.method !== "GET") {
      status = 405;
      body = stripeError(`The fixture server only serves GET ${pathTemplate}.`);
    } else {
      let resourceId;
      try {
        resourceId = decodeURIComponent(match[1]);
      } catch {
        resourceId = match[1];
      }
      if (Object.hasOwn(responses, resourceId)) {
        status = 200;
        body = responses[resourceId];
      } else {
        status = 404;
        body = stripeError(`No such payment_intent: '${bounded(resourceId, 80)}'`, "resource_missing", "intent");
      }
    }
    const data = JSON.stringify(body);
    response.writeHead(status, {
      "Content-Length": Buffer.byteLength(data),
      "Content-Type": "application/json",
      "Request-Id": "req_shockwave_fixture",
    });
    response.end(request.method === "HEAD" ? undefined : data);
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, LOOPBACK_HOST, () => resolve(server));
  });
}

function closeServer(server) {
  server.closeAllConnections();
  return new Promise((resolve) => server.close(() => resolve()));
}

function refuseNetwork() {
  const error = new Error("shockwave harness: real network access is disabled");
  error.code = "ECONNREFUSED";
  throw error;
}

// net.connect/http/undici pass [options, callback] already normalized; Socket#connect also
// takes (options[, cb]), (port[, host][, cb]) and (path[, cb]) for an IPC socket.
function connectTarget(args) {
  let first = args[0];
  if (Array.isArray(first)) {
    first = first[0];
  }
  if (first !== null && typeof first === "object") {
    return { path: first.path, host: first.host, port: first.port };
  }
  if (typeof first === "number" || (typeof first === "string" && /^\d+$/.test(first))) {
    return { path: undefined, host: typeof args[1] === "string" ? args[1] : undefined, port: first };
  }
  return { path: first, host: undefined, port: undefined };
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

const DNS_QUERY = /^(lookup|resolve|reverse)/;

// Best-effort, in-process interception, not network isolation (see the header); never restored.
function installNetworkGuard(port) {
  const originalConnect = net.Socket.prototype.connect;
  net.Socket.prototype.connect = function guardedConnect(...args) {
    const target = connectTarget(args);
    if (target.path == null && target.host === LOOPBACK_HOST && Number(target.port) === port) {
      return originalConnect.apply(this, args);
    }
    return refuseNetwork();
  };
  dgram.Socket.prototype.send = refuseNetwork;
  for (const target of [dns, dns.promises, dns.Resolver.prototype, dns.promises.Resolver.prototype]) {
    for (const name of Object.getOwnPropertyNames(target)) {
      if (DNS_QUERY.test(name) && typeof target[name] === "function") {
        target[name] = refuseNetwork;
      }
    }
  }
  const originalFetch = globalThis.fetch;
  const allowedOrigin = `http://${LOOPBACK_HOST}:${port}`;
  if (typeof originalFetch === "function") {
    globalThis.fetch = async (input, init) => {
      let origin = null;
      try {
        origin = new URL(requestUrl(input)).origin;
      } catch {
        origin = null;
      }
      if (origin !== allowedOrigin) {
        refuseNetwork();
      }
      return originalFetch(input, init);
    };
  }
  syncBuiltinESMExports(); // named imports such as `import { lookup } from "node:dns"` are guarded too
}

function readResult(value, resultPath) {
  let current = value;
  for (const key of resultPath) {
    const container = current !== null && (typeof current === "object" || typeof current === "function");
    if (!container || !(key in current)) {
      throw new TypeError(`the result has no field ${JSON.stringify(key)}`);
    }
    current = current[key];
  }
  return current;
}

// `value` as JSON data; TypeError for anything else (undefined, functions, Maps, class instances, ...).
function jsonValue(value, depth = 0) {
  if (depth > 32) {
    throw new TypeError("the result is nested too deeply");
  }
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError("the result is not a finite number");
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => jsonValue(item, depth + 1));
  }
  if (typeof value === "object") {
    const prototype = Object.getPrototypeOf(value);
    if (prototype === Object.prototype || prototype === null) {
      const plain = {};
      for (const key of Object.keys(value)) {
        plain[key] = jsonValue(value[key], depth + 1);
      }
      return plain;
    }
  }
  throw new TypeError(`the result is not JSON data (${value === undefined ? "undefined" : typeof value})`);
}

function valueText(value) {
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "boolean") {
    return value ? "True" : "False";
  }
  return canonical(value);
}

function newReport() {
  let harnessSha256 = null;
  try {
    harnessSha256 = sha256(readFileSync(HARNESS_FILE));
  } catch {
    harnessSha256 = null;
  }
  return {
    binding_sha256: null,
    cases: [],
    error_type: null,
    fixture_id: process.env.SHOCKWAVE_FIXTURE_ID ?? null,
    fixture_sha256: null,
    harness_sha256: harnessSha256,
    kind: REPORT_KIND,
    message: "the scenario did not complete",
    phase: process.env.SHOCKWAVE_SCENARIO_PHASE ?? null,
    report_format: REPORT_FORMAT,
    requests_dropped: 0,
    scenario_id: process.env.SHOCKWAVE_SCENARIO_ID ?? null,
    scenario_kind: null,
    schema_version: SCHEMA_VERSION,
    status: "error",
    unattributed_requests: [],
  };
}

function writeReport(report) {
  const target = process.env.SHOCKWAVE_REPORT_PATH;
  if (!target) {
    process.stderr.write("shockwave harness: no report path was provided; the report was not written\n");
    return;
  }
  const temporary = `${target}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(sortKeys(report), null, 2)}\n`);
  renameSync(temporary, target);
}

async function evaluateCase(fn, kase) {
  const entry = {
    actual: null,
    case_id: kase.case_id,
    error_type: null,
    expected: kase.expected,
    message: "the case did not complete",
    relevant: kase.relevant,
    requests: [], // filled from the request log once the server has stopped
    status: "error",
  };
  try {
    const result = await fn(...kase.args);
    const actual = jsonValue(readResult(result, kase.result_path));
    if (canonical(actual).length > MAX_RESULT_CHARS) {
      throw new RangeError(`the result is larger than ${MAX_RESULT_CHARS} characters of JSON`);
    }
    entry.actual = actual;
    entry.status = canonical(actual) === canonical(kase.expected) ? "passed" : "failed";
    const field = kase.result_path.join(".");
    const label = field && typeof kase.expected !== "string" ? `${field}=` : "";
    entry.message = bounded(`expected ${label}${valueText(kase.expected)}, actual ${valueText(actual)}`);
  } catch (error) {
    entry.status = "error";
    entry.error_type = errorType(error);
    entry.message = bounded(`${errorType(error)}: ${errorMessage(error)}`);
  }
  return entry;
}

async function runCases(report) {
  const bindingRaw = readFileSync(BINDING_FILE);
  report.binding_sha256 = sha256(bindingRaw);
  const binding = JSON.parse(bindingRaw.toString("utf8"));
  report.scenario_id = binding.scenario_id;
  report.scenario_kind = binding.scenario_kind;
  if (process.env.SHOCKWAVE_SCENARIO_ID !== binding.scenario_id) {
    throw new Error("SHOCKWAVE_SCENARIO_ID does not match binding.json");
  }
  const target = binding.binding;
  if (target.http_injection !== "stripe_sdk_env") {
    throw new Error(`unsupported http_injection ${JSON.stringify(target.http_injection)}`);
  }

  const fixtureRaw = readFileSync(requiredEnv("SHOCKWAVE_FIXTURE_PATH"));
  report.fixture_sha256 = sha256(fixtureRaw);
  const fixture = JSON.parse(fixtureRaw.toString("utf8"));
  if (fixture.fixture_set_id !== process.env.SHOCKWAVE_FIXTURE_ID) {
    throw new Error("the fixture set does not match SHOCKWAVE_FIXTURE_ID");
  }
  if (binding.operation.method !== "GET") {
    throw new Error("the fixture server only simulates GET operations");
  }

  const log = { entries: [], dropped: 0 };
  const server = await startFixtureServer(fixture.responses, binding.operation.path, log); // before the guard
  const windows = []; // [case entry, first request index, end index]: requests received during its call
  try {
    const { port } = server.address();
    process.env[target.api_key_env] = OFFLINE_TEST_KEY;
    process.env[target.api_base_env] = `http://${LOOPBACK_HOST}:${port}`;
    installNetworkGuard(port);
    const moduleUrl = pathToFileURL(path.join(requiredEnv("SHOCKWAVE_REPO_ROOT"), target.module)).href;
    const consumer = await import(moduleUrl);
    const fn = consumer[target.function];
    if (typeof fn !== "function") {
      throw new TypeError(`${target.module} does not export a function named ${target.function}`);
    }
    for (const kase of binding.cases) {
      const start = log.entries.length;
      const entry = await evaluateCase(fn, kase);
      windows.push([entry, start, log.entries.length]);
      report.cases.push(entry);
    }
  } finally {
    await closeServer(server);
    const covered = new Set();
    for (const [entry, start, end] of windows) {
      entry.requests = log.entries.slice(start, end);
      for (let index = start; index < end; index += 1) {
        covered.add(index);
      }
    }
    report.unattributed_requests = log.entries.filter((_, index) => !covered.has(index));
    report.requests_dropped = log.dropped;
  }
}

test("stripe scenario cases", async () => {
  const report = newReport();
  try {
    await runCases(report);
    const failing = report.cases.filter((entry) => entry.status !== "passed").map((entry) => entry.case_id);
    report.status = failing.length > 0 || report.cases.length === 0 ? "failed" : "passed";
    report.message = bounded(
      `${report.cases.length - failing.length} of ${report.cases.length} cases passed` +
        (failing.length > 0 ? `; not passed: ${failing.join(", ")}` : ""),
    );
  } catch (error) {
    report.status = "error";
    report.error_type = errorType(error);
    report.message = bounded(`${errorType(error)}: ${errorMessage(error)}`);
    throw error;
  } finally {
    writeReport(report);
  }
  assert.strictEqual(report.status, "passed", report.message);
});
