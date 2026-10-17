// Displays charges from the fictional demo-payments service (not Stripe).
// Assumes the current contract: `amount` is in minor units (cents).

export const DEMO_BASE_URL = "http://demo-payments.test";

export interface ChargeResponse {
  id: string;
  amount: number;
  currency: string;
}

export function displayAmount(response: ChargeResponse): string {
  const dollars = response.amount / 100;
  return `$${dollars.toFixed(2)}`;
}

// Thin HTTP integration: GET /v1/charges/{id} and format the amount.
export async function fetchChargeDisplay(chargeId: string): Promise<string> {
  const res = await fetch(`${DEMO_BASE_URL}/v1/charges/${chargeId}`);
  if (!res.ok) throw new Error(`demo-payments returned ${res.status}`);
  const charge = await res.json();
  return displayAmount(charge);
}
