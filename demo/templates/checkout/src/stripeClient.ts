import type Stripe from 'stripe';

// Stripe API version this checkout was built and tested against (stripe-node 23.0.0 default).
export const STRIPE_API_VERSION = '2026-09-30.endive';

/**
 * The Stripe secret key the checkout uses: STRIPE_SECRET_KEY, which must be a test-mode key
 * (sk_test_...). Missing keys and live-mode keys (sk_live_..., rk_live_...) are refused.
 */
export function stripeApiKey(env: NodeJS.ProcessEnv = process.env): string {
  const apiKey = env.STRIPE_SECRET_KEY;
  if (!apiKey) {
    throw new Error('STRIPE_SECRET_KEY is not set');
  }
  if (apiKey.startsWith('sk_live_') || apiKey.startsWith('rk_live_')) {
    throw new Error('refusing a live-mode Stripe key: this checkout runs with test-mode keys only');
  }
  return apiKey;
}

/**
 * Options for the official Stripe SDK client. STRIPE_API_BASE is optional and points the SDK at
 * another Stripe-compatible base URL, such as a local response fixture server in tests; when it
 * is unset, the SDK talks to https://api.stripe.com.
 */
export function stripeConfig(env: NodeJS.ProcessEnv = process.env): Stripe.StripeConfig {
  const config: Stripe.StripeConfig = {
    apiVersion: STRIPE_API_VERSION,
    maxNetworkRetries: 0,
  };
  const apiBase = env.STRIPE_API_BASE;
  if (apiBase) {
    const url = new URL(apiBase);
    config.protocol = url.protocol === 'http:' ? 'http' : 'https';
    config.host = url.hostname;
    if (url.port) {
      config.port = url.port;
    }
  }
  return config;
}
