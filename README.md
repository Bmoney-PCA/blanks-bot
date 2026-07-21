# Blanks Bot — Printavo → SanMar blanks ordering (Pure Choice Apparel)

Automation for a screen-printing shop: pull orders that need blanks from Printavo,
sum demand across all of them, net out what's already on the shelf, map to SanMar
SKUs, and produce a "Blanks To Order" sheet (emailed on a 3 PM cron). On explicit
approval it submits the SanMar PO and writes back to Printavo.

## Files
| File | What it is |
|------|-----------|
| `blanks_bot.py` | The bot. Full design + API notes are in the file header. |
| `config.example.yaml` | Copy to `config.yaml` and fill in credentials/IDs. |
| `sku_map.csv` | Printavo (style,color) → SanMar (style,color). `confirmed=yes` required to order. |
| `on_hand_inventory.html` | **On-shelf inventory entry sheet.** Open in a browser, enter what's in stock, Export CSV → `on_hand.csv`. |
| `reports/` | Generated XLSX reports land here. |

## Setup
```bash
pip install requests pyyaml openpyxl zeep
cp config.example.yaml config.yaml     # then fill it in
python blanks_bot.py --list-statuses   # find pull status + AWAITING GARMENTS IDs
python blanks_bot.py --once            # pull -> sheet -> email (buys nothing)
```

## On-hand inventory
Open `on_hand_inventory.html`, enter counts per style/color/size, click **Export CSV**.
Save the file as `on_hand.csv` in this folder. Style/Color must match the `sanmar_style`
/ `sanmar_color` values in `sku_map.csv` so on-hand can be subtracted from demand.
(Bot-side netting is the next wiring step — see TODO below.)

## Still open (see `TODO(next-builder)` in blanks_bot.py)
- **[A]** Live SanMar `sendPO` via zeep type factories (currently DRY-RUN placeholder).
- **[B]** `getPreSubmitInfo` stock check → "Available" column.
- **[C]** Confirm Printavo v2 `lineItems.sizes` shape on the real account.
- **[D]** Fill `sku_map.csv` with real, confirmed mappings.
- **[E]** Net `on_hand.csv` against aggregated demand before building the order sheet.

## Safety
Live PO requires **all three**: `--submit`, `--i-approve`, and `sanmar.po_enabled: true`.
Everything else is dry-run. Money is real — eyeball the sheet for days before going live.
