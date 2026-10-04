# SIG Predictions adapter rules

- Use only the documented v1 API for automation; never scrape WebUI endpoints.
- Keep API keys in environment variables and never log them.
- Copy exact route/auth/payload/rate-limit details from the public API Reference; do not infer them from browser traffic.
- Keep all SIG request/response classes in this package.
- Convert numeric wire values to/from the repository's Decimal model only in market_handler/sig_predictions_adapter.py.
- Preserve YES/NO outcome and explicit BUY/SELL intent; do not silently rewrite complementary positions.
- Preserve REST asOf and realtime trade IDs in raw logs when wiring feeds.
- Retry documented HTTP 503 cancellation failures safely; do not mark orders cancelled locally until the server confirms it.
