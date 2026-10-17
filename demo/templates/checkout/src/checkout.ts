import type Stripe from 'stripe';

import { createStripeClient } from './stripeClient.ts';
import { basisPointsToPercent, SALES_TAX_BASIS_POINTS } from './tax.ts';

export interface OrderSummary {
  paymentIntentId: string;
  status: string;
  displayTotal: string;
  taxNote: string;
}

/**
 * Builds the order confirmation shown after payment. This storefront sells in USD only.
 */
export async function getOrderSummary(
  paymentIntentId: string,
  stripe: Stripe = createStripeClient(),
): Promise<OrderSummary> {
  const paymentIntent = await stripe.paymentIntents.retrieve(paymentIntentId);
  // Stripe reports USD amounts in cents.
  const totalDollars = paymentIntent.amount / 100;
  return {
    paymentIntentId: paymentIntent.id,
    status: paymentIntent.status,
    displayTotal: `$${totalDollars.toFixed(2)}`,
    taxNote: `Includes ${basisPointsToPercent(SALES_TAX_BASIS_POINTS).toFixed(2)}% sales tax`,
  };
}
