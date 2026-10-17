// Loyalty rewards are issued by the storefront itself, never by Stripe. Their amount is
// stored in cents in the storefront's own configuration.
export interface LoyaltyReward {
  code: string;
  amount: number;
}

export const WELCOME_REWARD: LoyaltyReward = { code: 'WELCOME5', amount: 500 };

export function describeReward(reward: LoyaltyReward = WELCOME_REWARD): string {
  return `${reward.code}: $${(reward.amount / 100).toFixed(2)} off your next order`;
}
