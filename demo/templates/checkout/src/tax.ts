// Sales tax rate configured for the storefront, in basis points (825 = 8.25%).
export const SALES_TAX_BASIS_POINTS = 825;

// Basis points to percent. Unrelated to Stripe amounts.
export function basisPointsToPercent(basisPoints: number): number {
  return basisPoints / 100;
}
