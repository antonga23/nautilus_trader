# WSB and BlackBetInAsia Adapters - Placeholder Note

These adapters follow the same structure as the 10bet adapter with Playwright-based web scraping.

## Status

The foundational structure has been created by copying the 10bet adapter. These are **placeholder implementations** that provide:

- Configuration classes with anti-bot settings
- Browser client with rate limiting and stealth mode
- Instrument provider skeletons
- Data client with polling framework
- Execution client with risk engine integration
- Factory functions

## Next Steps

When ready to implement full functionality:

1. **Update constants.py** with venue-specific URLs and selectors
2. **Update selectors.py** with actual CSS selectors from DOM inspection
3. **Implement market scraping** in browser_client.py and providers.py
4. **Add authentication flows** for login/session management
5. **Test with live sites** and adjust selectors as needed

## Key Differences

### WSB (WorldSportsBetting)

- South African audience
- ZAR currency
- Promotional rollover terms (varies by promotion)
- Similar UI to 10bet

### BlackBetInAsia

- Asian market focus
- May require VPN/proxy for geo-restrictions
- Advanced Asian handicap markets
- Potentially different session management

Both adapters use the same anti-bot infrastructure as 10bet:

- RateLimiter (random delays, req/min limits)
- Fingerprint randomization
- Stealth scripts
- Placeholder authentication

The current implementations are sufficient for integration testing with the arbitrage strategy, even though actual market scraping requires DOM inspection of live sites.
