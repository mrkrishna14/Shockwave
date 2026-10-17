// Storefront price display (contract double of Noirit's TypeScript consumer fixture).
const DEMO_PAYMENTS_BASE_URL = process.env.DEMO_PAYMENTS_BASE_URL ?? "http://demo-payments.test";

export interface Charge {
  id: string;
  amount: number;
  currency: string;
}

export async function fetchCharge(chargeId: string): Promise<Charge> {
  const response = await fetch(`${DEMO_PAYMENTS_BASE_URL}/v1/charges/${chargeId}`);
  if (!response.ok) {
    throw new Error(`demo-payments returned HTTP ${response.status}`);
  }
  return (await response.json()) as Charge;
}

export async function displayChargePrice(chargeId: string): Promise<string> {
  const charge = await fetchCharge(chargeId);
  return `$${(charge.amount / 100).toFixed(2)}`;
}
