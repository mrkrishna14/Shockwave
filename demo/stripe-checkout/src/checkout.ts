import Stripe from 'stripe';

// A deliberately fake credential; the demo harness intercepts the HTTP response.
const stripe = new Stripe('sk_test_shockwave_demo_not_a_real_key', {
  httpClient: Stripe.createFetchHttpClient(),
  maxNetworkRetries: 0,
});

export async function displayPaymentIntent(id: string): Promise<string> {
  const payment = await stripe.paymentIntents.retrieve(id);
  const dollars = payment.amount / 100;
  return `$${dollars.toFixed(2)}`;
}

// Independent local data remains denominated in cents after the provider change.
export function displayLocalAmount(amount: number): string {
  return `$${(amount / 100).toFixed(2)}`;
}
