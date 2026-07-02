# Bonus EV Dashboard — SA Sportsbooks

Interactive tool for deciding **where it pays to claim a deposit bonus** across South-African-facing
sportsbooks, assuming each bonus bet is hedged on a betting exchange at a fixed cost per unit of turnover
(default −2%). Ranks books by the rand kept after clearing the wagering requirement.

## Use

Open [`index.html`](index.html) directly in a browser — it is fully self-contained (inline CSS/JS, no build,
no network calls). Every input cell is editable; the ranking, KPI cards and capital-over-time chart recompute
live. Filter by **kind** to hide casino-only and out-of-region offers, search by name, page through results.

## Model

```
turnover     = wr× × base ÷ contribution%      (base = deposit+bonus, or bonus-only)
hedge cost   = hedge% × turnover                (the exchange back/lay drag)
realizable   = min(bonus, max-payout cap)       (winnings caps can bind)
net kept     = realizable − hedge cost          ← primary ranking
bets         = ⌈turnover ÷ stake-per-bet⌉       (stake capped at max stake; blank = no cap → 1 bet)
```

Net kept assumes the deposit is recovered (it washes out of the hedged grind); profit is the bonus minus the
cost of turning it over, capped by any max-payout limit on bonus winnings. The chart shows the "withdraw-now"
position bleeding negative as you grind, then jumping to net-positive the moment the rollover clears.

## Data — `venues.json`

91 SA-facing venues (47 from official T&C pages, the rest from reputable reviews). Each record:
`name, region, currency, kind, bonusType, deposit, bonus, wr, base, minOdds, maxStake, maxPayout, minDeposit,
expiry, confidence, sourceType, notes, source`.

- **kind**: `sports` / `freebet` (usable) · `casino` (can't hedge on a sports exchange) · `out` (non-ZAR) ·
  `unverified`.
- **confidence**: `high` (official T&Cs) / `med` (reputable review) / `low` (unconfirmed). **sourceType**
  marks `official` vs `review`. Each row links its source.
- **maxStake** = per-bet cap on bonus funds (drives bet count). **maxPayout** = cap on withdrawable bonus
  winnings (can cap net).

> Bonus terms are web-sourced and change often — **verify a book's current terms on its own site before
> depositing.** The `base` field (deposit+bonus vs bonus-only) is the single biggest EV lever; confirm it first.
> A handful of low/med-confidence records may conflate sibling brands or offers — treat with caution.

## Caveats

- This is matched betting against the books' interests. Most SA books void bonuses for "irregular play"
  (hedging, covering both sides, risk-free profit-seeking). The clean EV here ignores void risk, account
  limits, and exchange liquidity at each book's odds floor.
- **Free-bet / no-deposit** rows show `net = full bonus`, an upper bound — stake-not-returned free bets
  realistically retain ~70%.
- **Offshore / crypto** books (ZAR-facing but foreign-licensed) top the ranking on raw cap size but carry the
  most counterparty and payout risk — check the region tag and notes.

To refresh the data, edit `venues.json` (or edit inline in the UI and copy the values back).
