"""Runtime settings for the fictional Demo Payments provider."""

import os

# "v1" serves the current contract (amounts in USD cents); "v2" serves the proposed contract,
# where only GET /v1/charges/{id} amount is in USD dollars (refunds stay in cents).
CONTRACT_VERSION = os.environ.get("DEMO_PAYMENTS_CONTRACT", "v1")

# Configured at deploy time. A literal-only route scanner cannot resolve this prefix.
REFUNDS_PREFIX = os.environ.get("DEMO_PAYMENTS_REFUNDS_PREFIX", "/v1/refunds")
