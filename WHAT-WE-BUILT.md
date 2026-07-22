# What we built — Pure Choice Apparel blank inventory side

_For the bridge collaboration. This is the PCA (Sampson) side. The goal: keep an
accurate on-hand blank count and hand it to Shane's ordering bot. **This bot does
not order** — Shane's does. Nothing gets locked in without Sampson + Shane signing off._

## The bot's job (inventory keeper)
`blanks_bot.py` keeps the count; it never buys. It tracks **three numbers** per
(style, color, size):

| | meaning |
|---|---|
| **On Hand** | physically on the shelf |
| **Allocated** | reserved to a job (committed, not yet pulled) |
| **Available** | On Hand − Allocated → what's free (the number to net orders against) |

**Events** (all from Printavo's API, each idempotent + logged in `ledger.csv`):
- **allocate** (+Allocated) — "Order Blanks" checked. Available drops, On Hand doesn't, so Shane only orders the shortfall and no two jobs claim the same blank.
- **pull** (−On Hand, −Allocated) — "Received Garments". Confirms the reserved blank physically left.
- **restock** (+On Hand) — a SanMar order arrives into stock.

It regenerates **`tracker.html`** (On Hand / Allocated / Available, grouped by
garment, with a freshness stamp) and exports **`on_hand.csv`** — the file Shane's
side reads.

## Current snapshot — 2,135 units, SanMar-exact style codes
Style codes are verified against sanmar.com so they map cleanly on the ordering side:

| Code | Garment |
|---|---|
| **NL1810** | Next Level cotton tee **(S–3XL only)** |
| **5000** | Gildan Heavy Cotton tee |
| **1717** | Comfort Colors garment-dyed tee |
| **18000** | Gildan Heavy Blend crew |
| **DT6600** | District V.I.T. Super Heavyweight Fleece Hoodie |
| **DT6104** | District V.I.T. Fleece Crew |
| **PC54** | Port & Company Core Cotton Tee (the 4XL/5XL extended tees) |
| **Lane 7** | non-SanMar supplier tee — **Shane does NOT order this** (blank # TBD) |

**Two rules baked into the data:**
1. **NL1810 tops out at 3XL** — any 4XL/5XL of those tees are **PC54**, not NL1810.
2. **Lane 7 is not SanMar** — flagged; it stays in inventory but the ordering bot skips it.

Sizes tracked: **S → 6XL** (no XS).

## What the ordering side needs from us
Per (style, color, size): current **Available** + a **freshness timestamp** by ~2:15 PM.
Both come straight out of `on_hand.csv` / the tracker.

## Open (Sampson to provide)
- Lane 7 blank style #.
- **How tracking works on the floor** — who counts, how often, in what order, and
  how a count reaches the sheet. This is the piece to design the shared system around.
- Printavo API token wired in (local `config.yaml`) to turn on the live receive/pull.
