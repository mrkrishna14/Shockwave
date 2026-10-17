// Negative control: an unrelated `amount` that is NOT a demo-payments field.
// Order amounts are internal cents values; a migration must leave this alone.

export interface Order {
  amount: number;
}

export function formatOrderFee(order: Order): string {
  const fee = order.amount / 100;
  return `$${fee.toFixed(2)}`;
}

export function sampleOrderFee(): string {
  const charge = { id: "local", amount: 250 }; // a local object named charge is not origin evidence
  return formatOrderFee(charge);
}
