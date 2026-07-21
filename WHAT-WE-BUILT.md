# What we built — Pure Choice Apparel blanks system

_For the bridge collaboration. This is the PCA side's starting point — the goal is to
design the shared system **around this**, not replace it. Nothing gets locked in without
Brandon + the other dev signing off._

## 1. On-hand inventory sheet (the thing the floor uses)
- **File:** `on_hand_inventory.html` — a single-file, offline, browser-based count sheet. Autosaves to the browser; **Export CSV** drops `on_hand.csv`.
- **Grouped by garment type** into three sections: **T-Shirts**, **Sweatshirts (Crewneck)**, **Hoodies**.
- **Columns:** Style, Color, Description, then sizes `XS S M L XL 2XL 3XL 4XL 5XL 6XL`, plus per-row Total. Footer shows per-size totals + grand total.
- **Current snapshot:** 28 items, **2,135 units** on hand.

## 2. The export the bot consumes
- **File:** `on_hand.csv` — same data, wide format.
- **Header:** `sanmar_style,sanmar_color,description,XS,S,M,L,XL,2XL,3XL,4XL,5XL,6XL,total`
- Intended use: net on-hand against Printavo demand **before** ordering blanks from SanMar (bot TODO [E]).

## 3. The ordering bot
- **File:** `blanks_bot.py` (single file; full design + API notes in its header).
- Pulls Printavo orders in a "needs blanks" status → aggregates demand by (style,color,size) → maps to SanMar via `sku_map.csv` → builds an XLSX → emails on a 3 PM cron. On triple-gated approval, submits the SanMar PO and writes back to Printavo.
- Open items: `TODO(next-builder)` [A] live SanMar sendPO, [B] stock check, [C] confirm Printavo size shape, [D] real sku_map, [E] net on_hand.csv.

## 4. Items flagged for a human to confirm
Style/Color are transcribed from handwritten shelf counts; brand→SanMar style-number mapping is still open (bot TODO [D]). Specific reads to verify:
- **Pink Gildan Tee, 2XL = 7** (digit could be a 2).
- **Black Poly/Cotton Tee, 4XL = 24** (label was abbreviated "Poly/Co").
- **"Heather Grey" Next Level** (M:13, 3XL:1) — the grid row's color label was cut off; best guess.
- **Gildan Crew 18000 (Navy & Tan)** — dedicated crew sheets were used over the grid's sparser cells; numbers differ.
- **DT6600 Red Hoodie 4XL = 28** and **CC1717 Black 4XL = 45** — both were crossed-out corrections.

## 5. How tracking actually works on the floor
> **TODO (Brandon to fill):** how the shelves get counted — how often, by whom, in what
> order, and how a count gets from paper into the sheet. This is the part the shared
> system should be designed around.
