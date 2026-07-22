#!/usr/bin/env python3
# =============================================================================
#  blanks_bot.py  —  Pure Choice Apparel BLANK INVENTORY KEEPER
#  (this bot does NOT order — Shane's bot orders. This one keeps the counts.)
# =============================================================================
#
#  TWO QUANTITIES per (style, color, size)
#  ---------------------------------------
#     ON HAND    physically on the shelf
#     ALLOCATED  reserved to a job (committed, not yet physically pulled)
#     AVAILABLE  = ON HAND - ALLOCATED   (what's free — the number Shane nets against)
#
#  A blank stays in ON HAND until it physically leaves, but drops out of
#  AVAILABLE the moment it's allocated — so two jobs can never claim the same
#  blank and the shelf count stays honest.
#
#  EVENTS (each appended to an idempotent ledger.csv, keyed by event+ref)
#  ---------------------------------------------------------------------
#     restock   +ON HAND               SanMar order received INTO stock
#     allocate  +ALLOCATED             job commits to stock ("Order Blanks" checked)
#     pull      -ON HAND  -ALLOCATED   blank physically leaves ("Received Garments")
#     adjust    +/-ON HAND             manual correction / recount
#
#  So the flow we agreed: order in -> ALLOCATE (available drops, Shane only
#  orders the shortfall) -> restock when SanMar arrives -> PULL confirms it left.
#
#  FILES (in the project dir, or output_dir from config)
#     on_hand.csv    physical shelf     : sanmar_style,sanmar_color,description,<sizes>,total
#     allocated.csv  reserved to jobs   : same shape
#     ledger.csv     append-only log    : timestamp,event,ref,style,color,size,qty,note
#     tracker.html   generated view (On Hand / Allocated / Available)
#
#  SETUP
#     pip install requests pyyaml            # (tracker + file commands need no deps)
#     python blanks_bot.py --init            # writes config.example.yaml + sku_map.csv
#     cp config.example.yaml config.yaml     # fill in Printavo token + status ids
#     python blanks_bot.py --list-statuses   # find the status ids
#
#  RUN MODES
#     python blanks_bot.py --tracker                              # (re)build tracker.html
#     python blanks_bot.py --restock-file  in.csv  --ref PO-4821  # +on hand
#     python blanks_bot.py --allocate-file job.csv --ref JOB-771  # +allocated
#     python blanks_bot.py --pull-file     job.csv --ref JOB-771  # -on hand, -allocated
#     python blanks_bot.py --receive                              # Printavo "Received Garments" -> pull
#     python blanks_bot.py --to-order                             # demand net of AVAILABLE
#     python blanks_bot.py --once                                 # --receive then rebuild tracker
# =============================================================================

import argparse
import csv
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

SIZE_ORDER = ["S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL", "6XL"]

SIZE_ALIASES = {
    "xs": "XS", "size_xs": "XS", "s": "S", "size_s": "S", "small": "S",
    "m": "M", "size_m": "M", "med": "M", "medium": "M", "l": "L", "size_l": "L", "large": "L",
    "xl": "XL", "size_xl": "XL", "2xl": "2XL", "xxl": "2XL", "size_2xl": "2XL", "2x": "2XL",
    "3xl": "3XL", "xxxl": "3XL", "size_3xl": "3XL", "3x": "3XL", "4xl": "4XL", "size_4xl": "4XL", "4x": "4XL",
    "5xl": "5XL", "size_5xl": "5XL", "5x": "5XL", "6xl": "6XL", "size_6xl": "6XL", "6x": "6XL",
}

PRINTAVO_ENDPOINT = "https://www.printavo.com/api/v2"
STATE_HEADER = ["sanmar_style", "sanmar_color", "description"] + SIZE_ORDER + ["total"]
LEDGER_HEADER = ["timestamp", "event", "ref", "sanmar_style", "sanmar_color", "size", "qty", "note"]
SECTION_ORDER = ["T-Shirts", "Sweatshirts (Crewneck)", "Hoodies"]


def norm_size(raw):
    return SIZE_ALIASES.get(str(raw).lower().strip())


def now_iso():
    return dt.datetime.now().replace(microsecond=0).isoformat()


# ----------------------------------------------------------------------------
# Config + init
# ----------------------------------------------------------------------------

CONFIG_EXAMPLE = """\
printavo:
  email: you@purechoiceapparel.com      # email tied to your Printavo API token
  token: PRINTAVO_API_TOKEN             # My Account > API (paste yours; this file is gitignored)
  needs_blanks_status_ids:              # status(es) holding orders needing blanks
    - "0000"
  received_status_ids:                  # status(es) meaning "Received Garments" (the PULL trigger)
    - "0000"

sku_map_path: sku_map.csv
output_dir: .
"""

SKU_MAP_EXAMPLE = """\
printavo_style,printavo_color,sanmar_style,sanmar_color,confirmed
NL1810,Black,NL1810,Black,yes
NL1810,Gray (HeavyWeight),NL1810,Heather Gray,no
"""


def write_init_files():
    for name, content in [("config.example.yaml", CONFIG_EXAMPLE), ("sku_map.csv", SKU_MAP_EXAMPLE)]:
        if Path(name).exists():
            print(f"[init] {name} already exists -- left untouched")
        else:
            Path(name).write_text(content)
            print(f"[init] wrote {name}")


def load_config(path="config.yaml"):
    import yaml
    if not Path(path).exists():
        sys.exit(f"Missing {path}. Run --init, copy config.example.yaml to config.yaml, fill it in.")
    with open(path) as f:
        return yaml.safe_load(f)


def out_dir(cfg):
    d = Path(cfg.get("output_dir", ".")) if cfg else Path(".")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ----------------------------------------------------------------------------
# State  (on_hand.csv / allocated.csv share this shape)
# ----------------------------------------------------------------------------

def load_state(path):
    """{(style,color): {'desc': str, 'sizes': {SIZE: int}}}"""
    state = {}
    if not Path(path).exists():
        return state
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            key = (r["sanmar_style"].strip(), r["sanmar_color"].strip())
            sizes = {s: int(r[s]) for s in SIZE_ORDER if (r.get(s) or "").strip() and int(r[s]) != 0}
            state[key] = {"desc": (r.get("description") or "").strip(), "sizes": sizes}
    return state


def save_state(path, state):
    rows = sorted(state.items(), key=lambda kv: (kv[0][0].lower(), kv[0][1].lower()))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(STATE_HEADER)
        for (style, color), rec in rows:
            total = sum(rec["sizes"].values())
            if not total and not rec.get("desc"):
                continue
            w.writerow([style, color, rec.get("desc", "")]
                       + [rec["sizes"].get(s, "") for s in SIZE_ORDER] + [total])


def available(on_hand, allocated):
    """AVAILABLE = ON HAND - ALLOCATED, per (style,color,size), floored at 0."""
    avail = {}
    for key, rec in on_hand.items():
        al = allocated.get(key, {}).get("sizes", {})
        sizes = {s: max(0, q - al.get(s, 0)) for s, q in rec["sizes"].items()}
        avail[key] = {"desc": rec.get("desc", ""), "sizes": {s: q for s, q in sizes.items() if q}}
    return avail


# ----------------------------------------------------------------------------
# Ledger + event application
# ----------------------------------------------------------------------------

def load_ledger(path):
    if not Path(path).exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def processed_refs(ledger, event):
    return {row["ref"] for row in ledger if row["event"] == event and row["ref"]}


def append_ledger(path, rows):
    new = not Path(path).exists()
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(LEDGER_HEADER)
        for r in rows:
            w.writerow([r[k] for k in LEDGER_HEADER])


# each event -> which states it moves and in which direction
EVENT_DELTAS = {
    "restock":  {"on_hand": +1},
    "allocate": {"allocated": +1},
    "pull":     {"on_hand": -1, "allocated": -1},
    "adjust":   {"on_hand": +1},   # qty may be negative for a downward correction
}


def apply_event(states, event, ref, lines, note, ts):
    """Mutate the affected states by `lines` [(style,color,size,qty)]; return ledger rows."""
    deltas = EVENT_DELTAS[event]
    led = []
    for style, color, size, qty in lines:
        qty = int(qty)
        if not qty:
            continue
        for name, sign in deltas.items():
            rec = states[name].setdefault((style, color), {"desc": "", "sizes": {}})
            rec["sizes"][size] = rec["sizes"].get(size, 0) + sign * qty
            if rec["sizes"][size] <= 0:
                rec["sizes"].pop(size, None)
        led.append({"timestamp": ts, "event": event, "ref": ref, "sanmar_style": style,
                    "sanmar_color": color, "size": size, "qty": qty, "note": note})
    return led


def lines_from_wide_csv(path):
    """Wide sheet -> [(style,color,size,qty)] + {key: desc}."""
    lines, descs = [], {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            style = (r.get("sanmar_style") or "").strip()
            color = (r.get("sanmar_color") or "").strip()
            if not style:
                continue
            if r.get("description"):
                descs[(style, color)] = r["description"].strip()
            for col, val in r.items():
                lbl = norm_size(col)
                if lbl and str(val).strip().lstrip("-").isdigit() and int(val):
                    lines.append((style, color, lbl, int(val)))
    return lines, descs


# ----------------------------------------------------------------------------
# Printavo reads
# ----------------------------------------------------------------------------

PRINTAVO_STATUSES_QUERY = "query { statuses { nodes { id name } } }"
PRINTAVO_ORDERS_QUERY = """
query Orders($after: String, $statusIds: [ID!]) {
  orders(first: 25, after: $after, statusIds: $statusIds, sortOn: CREATED_AT_DESC) {
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Invoice { id visualId lineItemGroups { nodes { lineItems { nodes {
        styleNumber styleDescription color sizes { size quantity } } } } } }
      ... on Quote   { id visualId lineItemGroups { nodes { lineItems { nodes {
        styleNumber styleDescription color sizes { size quantity } } } } } }
    }
  }
}
"""


def _post(cfg, query, variables=None):
    import requests
    h = {"Content-Type": "application/json",
         "email": cfg["printavo"]["email"], "token": cfg["printavo"]["token"]}
    resp = requests.post(PRINTAVO_ENDPOINT, headers=h,
                         json={"query": query, "variables": variables or {}}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"Printavo error: {data['errors']}")
    return data["data"]


def list_statuses(cfg):
    nodes = _post(cfg, PRINTAVO_STATUSES_QUERY)["statuses"]
    nodes = nodes.get("nodes", nodes) if isinstance(nodes, dict) else nodes
    print("Printavo statuses  (id  name):")
    for s in nodes:
        print(f"  {s['id']:>8}  {s['name']}")
    return nodes


def fetch_orders(cfg, status_ids):
    if not status_ids:
        raise SystemExit("No status ids configured. Run --list-statuses and set them in config.yaml.")
    status_ids = [str(s) for s in status_ids]
    after, nodes = None, []
    while True:
        conn = _post(cfg, PRINTAVO_ORDERS_QUERY, {"after": after, "statusIds": status_ids})["orders"]
        nodes.extend(conn["nodes"])
        if conn["pageInfo"]["hasNextPage"]:
            after = conn["pageInfo"]["endCursor"]
        else:
            return nodes


def order_lines(order, sku_map):
    lines, skipped = [], []
    for g in (order.get("lineItemGroups", {}) or {}).get("nodes", []) or []:
        for li in (g.get("lineItems", {}) or {}).get("nodes", []) or []:
            pstyle = (li.get("styleNumber") or "").strip()
            pcolor = (li.get("color") or "").strip()
            if not pstyle:
                continue
            mp = sku_map.get((pstyle, pcolor))
            if not mp or not mp["confirmed"]:
                skipped.append((pstyle, pcolor))
                continue
            for s in (li.get("sizes") or []):
                lbl = norm_size(s.get("size"))
                if lbl and s.get("quantity"):
                    lines.append((mp["sanmar_style"], mp["sanmar_color"], lbl, int(s["quantity"])))
    return lines, skipped


def load_sku_map(path="sku_map.csv"):
    m = {}
    if not Path(path).exists():
        return m
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            m[(r["printavo_style"].strip(), r["printavo_color"].strip())] = {
                "sanmar_style": r["sanmar_style"].strip(), "sanmar_color": r["sanmar_color"].strip(),
                "confirmed": r.get("confirmed", "no").strip().lower() == "yes"}
    return m


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def _paths(cfg):
    d = out_dir(cfg)
    return d / "on_hand.csv", d / "allocated.csv", d / "ledger.csv"


def _file_event(cfg, event, path, ref, note):
    on_p, al_p, led_p = _paths(cfg)
    states = {"on_hand": load_state(on_p), "allocated": load_state(al_p)}
    ledger = load_ledger(led_p)
    if ref in processed_refs(ledger, event):
        print(f"[{event}] ref {ref} already applied -- skipping (idempotent).")
        return
    lines, descs = lines_from_wide_csv(path)
    for key, desc in descs.items():
        states["on_hand"].setdefault(key, {"desc": "", "sizes": {}})
        if desc:
            states["on_hand"][key]["desc"] = desc
            states["allocated"].get(key, {}).setdefault("desc", desc) if key in states["allocated"] else None
    led = apply_event(states, event, ref, lines, note or f"{event} {ref}", now_iso())
    save_state(on_p, states["on_hand"])
    save_state(al_p, states["allocated"])
    append_ledger(led_p, led)
    print(f"[{event}] ref {ref}: {sum(l['qty'] for l in led)} unit(s) across {len(led)} line(s).")


def cmd_receive(cfg):
    """Printavo 'Received Garments' orders -> PULL (-on hand, -allocated), idempotent."""
    on_p, al_p, led_p = _paths(cfg)
    sku_map = load_sku_map(cfg.get("sku_map_path", "sku_map.csv"))
    states = {"on_hand": load_state(on_p), "allocated": load_state(al_p)}
    ledger = load_ledger(led_p)
    done = processed_refs(ledger, "pull")
    all_led, applied, flagged = [], 0, set()
    for o in fetch_orders(cfg, cfg["printavo"].get("received_status_ids", [])):
        ref = str(o.get("id"))
        if ref in done:
            continue
        lines, skipped = order_lines(o, sku_map)
        flagged |= set(skipped)
        all_led += apply_event(states, "pull", ref, lines, f"received #{o.get('visualId')}", now_iso())
        applied += 1
    if all_led:
        save_state(on_p, states["on_hand"])
        save_state(al_p, states["allocated"])
        append_ledger(led_p, all_led)
    print(f"[receive] pulled {applied} received order(s); {len(all_led)} line(s).")
    if flagged:
        print(f"[receive] WARN unmapped/unconfirmed skipped: "
              + ", ".join(f"{s}/{c}" for s, c in sorted(flagged)))


def cmd_to_order(cfg):
    """Demand (Printavo needs-blanks) net of AVAILABLE -> what to order. Read-only."""
    on_p, al_p, _ = _paths(cfg)
    sku_map = load_sku_map(cfg.get("sku_map_path", "sku_map.csv"))
    avail = available(load_state(on_p), load_state(al_p))
    demand = defaultdict(lambda: defaultdict(int))
    for o in fetch_orders(cfg, cfg["printavo"].get("needs_blanks_status_ids", [])):
        lines, _ = order_lines(o, sku_map)
        for style, color, size, qty in lines:
            demand[(style, color)][size] += qty
    print(f"{'STYLE':<26} {'COLOR':<16} {'SIZE':<5} {'DEMAND':>6} {'AVAIL':>6} {'ORDER':>6}")
    for (style, color), sizes in sorted(demand.items()):
        av = avail.get((style, color), {}).get("sizes", {})
        for size in SIZE_ORDER:
            need = sizes.get(size, 0)
            if need:
                print(f"{style:<26} {color:<16} {size:<5} {need:>6} {av.get(size,0):>6} {max(0,need-av.get(size,0)):>6}")


# ----------------------------------------------------------------------------
# Tracker page  (On Hand / Allocated / Available)
# ----------------------------------------------------------------------------

def classify(style, desc):
    t = f"{style} {desc}".lower()
    if "hood" in t or "dt6600" in t or "dt6100" in t:
        return "Hoodies"
    if "crew" in t or "18000" in t or "sweat" in t or "6154" in t:
        return "Sweatshirts (Crewneck)"
    return "T-Shirts"


def build_tracker(on_hand, allocated, ledger, out_path, freshness):
    from html import escape
    keys = set(on_hand) | set(allocated)
    groups = defaultdict(list)
    for key in keys:
        style, color = key
        desc = on_hand.get(key, {}).get("desc") or allocated.get(key, {}).get("desc", "")
        oh = on_hand.get(key, {}).get("sizes", {})
        al = allocated.get(key, {}).get("sizes", {})
        if sum(oh.values()) == 0 and sum(al.values()) == 0:
            continue
        groups[classify(style, desc)].append((style, color, desc, oh, al))

    g_oh = g_al = 0
    for g in groups.values():
        for _, _, _, oh, al in g:
            g_oh += sum(oh.values())
            g_al += sum(al.values())
    g_av = g_oh - g_al

    def cell(v):
        return v if v else ""

    rows = []
    for sec in SECTION_ORDER:
        items = sorted(groups.get(sec, []), key=lambda x: (x[0].lower(), x[1].lower()))
        if not items:
            continue
        rows.append(f'<tr class="section-row"><td class="section" colspan="{len(SIZE_ORDER)+6}">{escape(sec)}</td></tr>')
        for style, color, desc, oh, al in items:
            oht, alt = sum(oh.values()), sum(al.values())
            avt = oht - alt
            size_cells = "".join(f"<td>{cell(oh.get(s,0))}</td>" for s in SIZE_ORDER)
            rows.append(
                f'<tr><td class="text">{escape(style)}</td><td class="text">{escape(color)}</td>'
                f'<td class="text">{escape(desc)}</td>{size_cells}'
                f'<td class="oh">{oht}</td><td class="al">{cell(alt)}</td><td class="av">{avt}</td></tr>')

    # allocated detail (only rows with allocations)
    al_rows = []
    for key in sorted(allocated, key=lambda k: (k[0].lower(), k[1].lower())):
        al = allocated[key]["sizes"]
        if not sum(al.values()):
            continue
        cells = "".join(f"<td>{cell(al.get(s,0))}</td>" for s in SIZE_ORDER)
        al_rows.append(f'<tr><td class="text">{escape(key[0])}</td><td class="text">{escape(key[1])}</td>{cells}'
                       f'<td class="al">{sum(al.values())}</td></tr>')
    al_table = "".join(al_rows) or f'<tr><td colspan="{len(SIZE_ORDER)+3}" class="muted">Nothing allocated right now.</td></tr>'

    recent = list(reversed(ledger))[:15]
    ev_sign = {"restock": "+", "allocate": "~", "pull": "−", "adjust": "±"}
    act = "".join(
        f'<tr><td>{escape(r["timestamp"])}</td><td class="ev {r["event"]}">{r["event"]}</td>'
        f'<td>{ev_sign.get(r["event"],"")}{r["qty"]}</td>'
        f'<td>{escape(r["sanmar_style"])} / {escape(r["sanmar_color"])} {escape(r["size"])}</td>'
        f'<td>{escape(r["note"])}</td></tr>' for r in recent) \
        or '<tr><td colspan="5" class="muted">No activity yet.</td></tr>'

    size_hdr = "".join(f"<th>{s}</th>" for s in SIZE_ORDER)

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Blanks Inventory — Pure Choice Apparel</title>
<style>
 :root{{--bg:#0f172a;--line:#1f2937;--line2:#374151;--ink:#e5e7eb;--muted:#9ca3af;--accent:#38bdf8;--good:#22c55e;--warn:#fbbf24}}
 *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
 header{{padding:18px 22px;border-bottom:1px solid var(--line);display:flex;flex-wrap:wrap;gap:16px;align-items:center;justify-content:space-between}}
 h1{{margin:0;font-size:17px;font-weight:650}} h1 small{{display:block;color:var(--muted);font-weight:400;font-size:12px;margin-top:3px}}
 .kpis{{display:flex;gap:20px}} .kpi{{text-align:right}} .kpi .n{{font-size:20px;font-weight:700}} .kpi .l{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}}
 .kpi.oh .n{{color:var(--accent)}} .kpi.al .n{{color:var(--warn)}} .kpi.av .n{{color:var(--good)}}
 .stamp{{font-size:11px;color:var(--muted);width:100%;text-align:right}}
 .wrap{{padding:16px 22px 80px}} h2{{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:1px;margin:28px 0 8px}}
 .tablewrap{{overflow-x:auto;border:1px solid var(--line);border-radius:10px}}
 table{{border-collapse:collapse;width:100%;min-width:1000px}} th,td{{border-bottom:1px solid var(--line);padding:8px 6px;text-align:center;white-space:nowrap}}
 thead th{{background:#0c1424;color:#cbd5e1;font-size:11px;letter-spacing:.4px;text-transform:uppercase}}
 thead th.text,td.text{{text-align:left;padding-left:12px}}
 td.oh{{font-weight:700;color:var(--accent)}} td.al{{font-weight:700;color:var(--warn)}} td.av{{font-weight:700;color:var(--good)}}
 tr.section-row td.section{{background:#0b2a3a;color:#7dd3fc;font-weight:700;letter-spacing:1.5px;text-align:left;padding:9px 12px;font-size:12px;text-transform:uppercase}}
 .muted{{color:var(--muted)}} .log table{{min-width:640px}}
 td.ev{{font-size:11px;text-transform:uppercase;letter-spacing:.5px;font-weight:700}}
 td.ev.restock{{color:var(--accent)}} td.ev.allocate{{color:var(--warn)}} td.ev.pull{{color:#fca5a5}} td.ev.adjust{{color:var(--muted)}}
</style></head><body>
<header>
  <h1>Blanks Inventory<small>Pure Choice Apparel — maintained by the blanks bot</small></h1>
  <div class="kpis">
    <div class="kpi oh"><div class="n">{g_oh}</div><div class="l">On Hand</div></div>
    <div class="kpi al"><div class="n">{g_al}</div><div class="l">Allocated</div></div>
    <div class="kpi av"><div class="n">{g_av}</div><div class="l">Available</div></div>
  </div>
  <div class="stamp">Last updated by bot: {escape(freshness)}</div>
</header>
<div class="wrap">
  <h2>On Hand — physical shelf (per size) · with Allocated / Available totals</h2>
  <div class="tablewrap"><table>
    <thead><tr><th class="text">Style</th><th class="text">Color</th><th class="text">Description</th>{size_hdr}
      <th>On&nbsp;Hand</th><th>Alloc</th><th>Avail</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>

  <h2>Allocated — reserved to jobs (per size)</h2>
  <div class="tablewrap"><table>
    <thead><tr><th class="text">Style</th><th class="text">Color</th>{size_hdr}<th>Total</th></tr></thead>
    <tbody>{al_table}</tbody>
  </table></div>

  <h2>Recent activity (ledger)</h2>
  <div class="tablewrap log"><table>
    <thead><tr><th>When</th><th>Event</th><th>Qty</th><th>Item</th><th>Source</th></tr></thead>
    <tbody>{act}</tbody>
  </table></div>
</div></body></html>"""
    Path(out_path).write_text(html)
    return g_oh, g_al, g_av


def cmd_tracker(cfg):
    on_p, al_p, led_p = _paths(cfg)
    oh, al, ledger = load_state(on_p), load_state(al_p), load_ledger(led_p)
    g_oh, g_al, g_av = build_tracker(oh, al, ledger, out_dir(cfg) / "tracker.html", now_iso())
    print(f"[tracker] on hand {g_oh}, allocated {g_al}, available {g_av}.")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="PCA blank inventory keeper (does not order)")
    p.add_argument("--init", action="store_true")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--list-statuses", action="store_true")
    p.add_argument("--restock-file", metavar="CSV", help="+ON HAND (SanMar received into stock)")
    p.add_argument("--allocate-file", metavar="CSV", help="+ALLOCATED (Order Blanks checked)")
    p.add_argument("--pull-file", metavar="CSV", help="-ON HAND & -ALLOCATED (physical pull)")
    p.add_argument("--ref", help="reference id for a --*-file command (PO / job number)")
    p.add_argument("--note", default="")
    p.add_argument("--receive", action="store_true", help="Printavo 'Received Garments' -> pull")
    p.add_argument("--to-order", action="store_true", help="demand net of AVAILABLE (read-only)")
    p.add_argument("--tracker", action="store_true")
    p.add_argument("--once", action="store_true", help="--receive then rebuild tracker")
    args = p.parse_args()

    if args.init:
        return write_init_files()

    file_cmd = args.restock_file or args.allocate_file or args.pull_file
    needs_cfg = args.list_statuses or args.receive or args.to_order or args.once
    cfg = load_config(args.config) if (needs_cfg or Path(args.config).exists()) else {"output_dir": "."}

    if args.list_statuses:
        list_statuses(cfg)
    elif file_cmd:
        if not args.ref:
            sys.exit("--*-file requires --ref (e.g. --ref PO-4821 or --ref JOB-771)")
        event = "restock" if args.restock_file else "allocate" if args.allocate_file else "pull"
        _file_event(cfg, event, file_cmd, args.ref, args.note)
        cmd_tracker(cfg)
    elif args.receive:
        cmd_receive(cfg)
        cmd_tracker(cfg)
    elif args.to_order:
        cmd_to_order(cfg)
    elif args.once:
        cmd_receive(cfg)
        cmd_tracker(cfg)
    elif args.tracker:
        cmd_tracker(cfg)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
