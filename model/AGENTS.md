# Internal model invariants

- This package is venue-independent.
- Use `Decimal` for prices, quantities, monetary values, notionals, rates, and
  other values that participate in trading decisions or accounting.
- Internal instrument names are logical symbols, not venue-qualified symbols.
- Do not import exchange SDKs or venue-specific request/response classes here.
- Market adapters own all conversion to/from venue-specific numeric and symbol
  representations.
- Do not round or quantize for a venue inside strategy, risk, portfolio, or
  execution logic.
