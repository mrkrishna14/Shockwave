import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { getOrderSummary } from '../src/orderSummary.ts';
import { describeReward } from '../src/rewards.ts';
import { createStripeClient, STRIPE_API_VERSION } from '../src/stripeClient.ts';
import { basisPointsToPercent } from '../src/tax.ts';
import { startFixtureServer, type FixtureServer } from './stripeFixtureServer.ts';

const FIXTURES = new URL('./fixtures/payment_intents.json', import.meta.url).pathname;

let server: FixtureServer;

before(async () => {
  server = await startFixtureServer(FIXTURES);
});

after(async () => {
  await server.close();
});

function offlineClient() {
  return createStripeClient({ STRIPE_SECRET_KEY: 'sk_test_offline_fixture', STRIPE_API_BASE: server.baseUrl });
}

test('order summary shows the PaymentIntent total in dollars', async () => {
  const summary = await getOrderSummary('pi_demo_10usd', offlineClient());
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

test('live-mode keys are refused', () => {
  assert.throws(() => createStripeClient({ STRIPE_SECRET_KEY: 'sk_live_not_allowed' }), /live-mode/);
});
