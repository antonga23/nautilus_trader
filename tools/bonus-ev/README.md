# Bonus EV Dashboard — SA Sportsbooks

Interactive tool for deciding **where it pays to claim a deposit bonus** across South-African-facing
sportsbooks, assuming each bonus bet is hedged on a betting exchange at a fixed cost per unit of turnover
(default −2%). Ranks books by the rand kept after clearing the wagering requirement.

## Use

Open [`index.html`](index.html) directly in a browser — it is fully self-contained (inline CSS/JS, no build,
no network calls). Every input cell is editable; the ranking, KPI cards and capital-over-time chart recompute
live. Filter by **kind** to hide casino-only and out-of-region offers.

## Model

```
turnover   = wr× × base ÷ contribution%      (base = deposit+bonus, or bonus-only)
hedge cost = hedge% × turnover                (the exchange back/lay drag)
net kept   = bonus − hedge cost               ← primary ranking
bets       = ⌈turnover ÷ stake-per-bet⌉       (stake capped at the book's max; blank = no cap → 1 bet)
```

Net kept assumes the deposit is recovered (it washes out of the hedged grind); profit is the bonus minus the
cost of turning it over. The chart shows the "withdraw-now" position bleeding negative as you grind, then
jumping to net-positive the moment the rollover clears.

## Data — `venues.json`

Each record: `name, region, currency, kind, bonusType, deposit, bonus, wr, base, minOdds, maxStake, expiry,
confidence, notes, source`.

- **kind**: `sports` / `freebet` (usable) · `casino` (can't hedge on a sports exchange) · `out` (non-ZAR) ·
  `unverified`.
- **confidence**: `high` (from official T&Cs) / `med` (reputable review) / `low` (unconfirmed). Each row links its source.

> The data is web-sourced and bonus terms change often — **verify a book's current terms on its own site
> before depositing.** The `base` field (deposit+bonus vs bonus-only) is the single biggest EV lever; confirm it first.

## Caveats

- This is matched betting against the books' interests. Most SA books void bonuses for "irregular play"
  (hedging, covering both sides, risk-free profit-seeking). The clean EV here ignores void risk, account
  limits, and exchange liquidity at each book's odds floor.
- **Free-bet / no-deposit** rows show `net = full bonus`, an upper bound — stake-not-returned free bets
  realistically retain ~70%.

To refresh the data, edit `venues.json` (or edit inline in the UI and copy the values back).
