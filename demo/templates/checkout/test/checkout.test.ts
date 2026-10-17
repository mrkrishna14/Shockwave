import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { getOrderSummary } from '../src/orderSummary.ts';
import { describeReward } from '../src/rewards.ts';
import { STRIPE_API_VERSION, stripeApiKey } from '../src/stripeClient.ts';
import { basisPointsToPercent } from '../src/tax.ts';
import { startFixtureServer, type FixtureServer } from './stripeFixtureServer.ts';

const FIXTURES = new URL('./fixtures/payment_intents.json', import.meta.url).pathname;
const ENV_KEYS = ['STRIPE_SECRET_KEY', 'STRIPE_API_BASE'] as const;

let server: FixtureServer;
const savedEnv: Partial<Record<(typeof ENV_KEYS)[number], string>> = {};

before(async () => {
  server = await startFixtureServer(FIXTURES);
  for (const key of ENV_KEYS) {
    savedEnv[key] = process.env[key];
  }
  // getOrderSummary creates its Stripe client from the environment at call time.
  process.env.STRIPE_SECRET_KEY = 'sk_test_offline_fixture';
  process.env.STRIPE_API_BASE = server.baseUrl;
});

after(async () => {
  for (const key of ENV_KEYS) {
    if (savedEnv[key] === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = savedEnv[key];
    }
  }
  await server.close();
});

test('order summary shows the PaymentIntent total in dollars', async () => {
  const summary = await getOrderSummary('pi_demo_10usd');
  assert.equal(summary.displayTotal, '$10.00');
  assert.equal(summary.status, 'succeeded');
  const request = server.requests.at(-1);
  assert.equal(request?.method, 'GET');
  assert.equal(request?.path, '/v1/payment_intents/pi_demo_10usd');
  assert.equal(request?.stripeVersion, STRIPE_API_VERSION);
  assert.equal(request?.authorization, 'Bearer sk_test_offline_fixture');
});

test('tax note converts basis points to percent', () => {
  assert.equal(basisPointsToPercent(825), 8.25);
});

test('loyalty reward amounts are storefront cents', () => {
  assert.equal(describeReward(), 'WELCOME5: $5.00 off your next order');
});

test('live-mode and missing keys are refused', () => {
  assert.throws(() => stripeApiKey({ STRIPE_SECRET_KEY: 'sk_live_not_allowed' }), /live-mode/);
  assert.throws(() => stripeApiKey({ STRIPE_SECRET_KEY: 'rk_live_not_allowed' }), /live-mode/);
  assert.throws(() => stripeApiKey({}), /STRIPE_SECRET_KEY is not set/);
});
