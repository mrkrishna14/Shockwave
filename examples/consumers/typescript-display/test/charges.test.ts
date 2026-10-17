// Native tests (node:test). SHOCKWAVE_RESPONSE_FIXTURE selects the
// demo-payments charge response; default is the local old (cents) fixture.
// Both old and proposed fixtures represent $10, so expect "$10.00".
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { displayAmount, fetchChargeDisplay } from "../src/charges.ts";
import { formatOrderFee } from "../src/orders.ts";

const fixturePath =
  process.env.SHOCKWAVE_RESPONSE_FIXTURE ??
  fileURLToPath(new URL("../fixtures/charge_old.json", import.meta.url));
const loadFixture = () => JSON.parse(readFileSync(fixturePath, "utf8"));

test("displayAmount shows ten dollars", () => {
  assert.equal(displayAmount(loadFixture()), "$10.00");
});

test("order fee negative control", () => {
  assert.equal(formatOrderFee({ amount: 250 }), "$2.50");
});

test("fetchChargeDisplay calls the demo endpoint", async () => {
  const calls: string[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (url: string) => {
    calls.push(String(url));
    return { ok: true, status: 200, json: async () => loadFixture() };
  }) as unknown as typeof fetch; // no network
  try {
    assert.equal(await fetchChargeDisplay("ch_demo"), "$10.00");
    assert.deepEqual(calls, ["http://demo-payments.test/v1/charges/ch_demo"]);
  } finally {
    globalThis.fetch = original;
  }
});
