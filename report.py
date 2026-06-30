#!/usr/bin/env python3
"""
Burrow Retail — Monday Morning Report
Runs every Monday 8am CT via GitHub Actions.
Data: Looker explore API (burrow model) + HubSpot Private App.
Plan: Google Sheet CSV export (shared "Anyone with the link can view").
Delivery: HTML report → GitHub Pages; Slack link → #salesoperations.
"""

import os, sys, datetime, csv, io, base64, calendar
import requests
from datetime import timezone

# ── Config ────────────────────────────────────────────────────────────────────

LOOKER_URL    = os.environ["LOOKER_BASE_URL"]
LOOKER_ID     = os.environ["LOOKER_CLIENT_ID"]
LOOKER_SECRET = os.environ["LOOKER_CLIENT_SECRET"]
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
BW_HS_TOKEN   = os.environ.get("BW_HUBSPOT_TOKEN", "")
GITHUB_REPO   = "maryspreck-star/burrow-retail-report"
PAGE_URL      = "https://maryspreck-star.github.io/burrow-retail-report/"

FORECAST_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1nCTsLse2FOsWuP5j6Xnw59_wbMDBIlOQiLVi7sN-4ps"
    "/export?format=csv&gid=1740540747"
)

RETAIL_FILTER = {"bw_orders.order_channel": "Retail"}

STORE_MAP = {
    "Soho":        "soho",
    "Boston":      "boston",
    "Chicago":     "chicago",
    "Los Angeles": "la",
}
STORE_LABELS = {"soho": "Soho", "boston": "Boston", "chicago": "Chicago", "la": "Los Angeles"}
STORES = ["soho", "boston", "chicago", "la"]

# HubSpot — team IDs → store slugs
HS_TEAM_STORE = {
    "66253418": "soho",    "66253436": "chicago",
    "66253405": "boston",  "66253431": "la",
}
# Owner ID → store slug (fallback when hubspot_team_id is null on a deal)
HS_OWNER_TEAM = {
    "83155723": "soho",    "83155721": "soho",    "91710921": "soho",
    "83155725": "soho",    "94567021": "soho",
    "83155716": "chicago", "83155715": "chicago",  "83155714": "chicago", "83774658": "chicago",
    "91476773": "la",      "86186641": "la",        "89460158": "la",
    "88147731": "boston",  "87493927": "boston",    "83225829": "boston",
}
HS_OWNER_NAME = {
    "83155723": "Aaron Bullard",       "83155721": "Richard St. Germain",
    "91710921": "Ashanti King-Wynn",   "83155725": "William Hill",
    "94567021": "Erik Oftedal",
    "83155716": "Eleanor White",       "83155715": "Emily Arrowsmith",
    "83155714": "Nina Angell",         "83774658": "Dana Trombetta",
    "91476773": "Savannah Archinal",   "86186641": "Kinsey Austin",
    "89460158": "Demi Adeyinka",
    "88147731": "Casie Eifrig",        "87493927": "Delsie Lopez",
    "83225829": "Keyla Valerio Piriz",
}
HS_CLOSED_WON = [
    "264c3b2f-856c-4973-b659-95b5f775dc8b",  # Salesforce Default Pipeline
    "957899065",                               # 2025 Sales Pipeline
]

# ── Dates ─────────────────────────────────────────────────────────────────────

def compute_dates():
    today     = datetime.date.today()
    yd        = today - datetime.timedelta(days=1)
    yd_minus1 = yd - datetime.timedelta(days=1)
    lw_end    = yd
    lw_start  = lw_end - datetime.timedelta(days=6)
    mtd_start = yd.replace(day=1)
    def ly(dt): return dt.replace(year=dt.year - 1)
    return dict(
        today=today, yd=yd, yd_minus1=yd_minus1,
        lw_start=lw_start, lw_end=lw_end, mtd_start=mtd_start,
        ly_yd=ly(yd), ly_yd_minus1=ly(yd_minus1),
        ly_lw_start=ly(lw_start), ly_lw_end=ly(lw_end),
        ly_mtd_start=ly(mtd_start),
        week_label=f"Week of {lw_start.strftime('%b %-d')}–{lw_end.strftime('%-d, %Y')}",
    )

# ── Looker ────────────────────────────────────────────────────────────────────

class Looker:
    REV   = "bw_order_items.total_discount_order_item_revenue"
    ORDS  = "bw_orders.num_orders"
    UNITS = "bw_order_items.number_of_items"
    STORE = "bw_orders.store_name"
    DATE  = "bw_orders.created_date"
    MONTH = "bw_orders.created_month"
    COLL  = "bw_products.collection"
    ASSOC = "bw_orders.Sale_associate"

    def __init__(self):
        r = requests.post(f"{LOOKER_URL}/api/4.0/login",
                          data={"client_id": LOOKER_ID, "client_secret": LOOKER_SECRET})
        r.raise_for_status()
        self.h = {"Authorization": f"token {r.json()['access_token']}",
                  "Content-Type": "application/json"}

    def query(self, fields, filters, sorts=None, limit=500):
        body = {"model": "burrow", "view": "bw_orders",
                "fields": fields, "filters": filters,
                "sorts": sorts or [], "limit": str(limit)}
        r = requests.post(f"{LOOKER_URL}/api/4.0/queries/run/json",
                          headers=self.h, json=body)
        if not r.ok:
            print(f"Looker error {r.status_code}: {r.text[:300]}", file=sys.stderr)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else [data]

    def _df(self, start, end):
        return str(start) if start == end else f"{start} to {end}"

    def totals(self, start, end):
        rows = self.query(
            fields=[self.REV, self.ORDS, self.UNITS],
            filters={**RETAIL_FILTER, self.DATE: self._df(start, end)},
        )
        row = rows[0] if rows else {}
        return dict(
            revenue=float(row.get(self.REV,   0) or 0),
            orders =int(  row.get(self.ORDS,  0) or 0),
            units  =int(  row.get(self.UNITS, 0) or 0),
        )

    def stores(self, start, end):
        rows = self.query(
            fields=[self.STORE, self.REV, self.ORDS],
            filters={**RETAIL_FILTER, self.DATE: self._df(start, end)},
            sorts=[f"{self.REV} desc"],
        )
        result = {s: 0.0 for s in STORES}
        for row in rows:
            k = STORE_MAP.get(row.get(self.STORE, ""))
            if k:
                result[k] = float(row.get(self.REV, 0) or 0)
        return result

    def daily(self, start, end):
        rows = self.query(
            fields=[self.DATE, self.REV],
            filters={**RETAIL_FILTER, self.DATE: self._df(start, end)},
            sorts=[self.DATE],
        )
        return {
            int(str(r.get(self.DATE, "1900-01-01")).split("-")[2]): float(r.get(self.REV, 0) or 0)
            for r in rows if r.get(self.DATE)
        }

    def collections(self, start, end):
        try:
            rows = self.query(
                fields=[self.COLL, self.REV, self.UNITS],
                filters={**RETAIL_FILTER, self.DATE: self._df(start, end)},
                sorts=[f"{self.REV} desc"], limit=25,
            )
            if rows and not rows[0].get(self.COLL):
                return []
            return rows
        except Exception as e:
            print(f"  ⚠  Collections query failed: {e}")
            return []

    def associates(self, start, end):
        try:
            rows = self.query(
                fields=[self.STORE, self.ASSOC, self.REV, self.ORDS, self.UNITS],
                filters={**RETAIL_FILTER, self.DATE: self._df(start, end)},
                sorts=[self.STORE, f"{self.REV} desc"], limit=300,
            )
            result = {}
            for row in rows:
                store = STORE_MAP.get(row.get(self.STORE, "") or "")
                assoc = (row.get(self.ASSOC) or "").strip()
                if not store or not assoc:
                    continue
                rev   = float(row.get(self.REV,   0) or 0)
                ords  = int(  row.get(self.ORDS,  0) or 0)
                units = int(  row.get(self.UNITS, 0) or 0)
                result.setdefault(store, {}).setdefault(assoc, {"revenue": 0.0, "orders": 0, "units": 0})
                result[store][assoc]["revenue"] += rev
                result[store][assoc]["orders"]  += ords
                result[store][assoc]["units"]   += units
            return result
        except Exception as e:
            print(f"  ⚠  Associates query failed: {e}")
            return {}

    def monthly_trend(self, start, end):
        try:
            rows = self.query(
                fields=[self.MONTH, self.REV, self.ORDS, self.UNITS],
                filters={**RETAIL_FILTER, self.DATE: self._df(start, end)},
                sorts=[f"{self.MONTH} desc"], limit=48,
            )
            return rows
        except Exception as e:
            print(f"  ⚠  Monthly trend query failed: {e}")
            return []

# ── Google Sheets plan ────────────────────────────────────────────────────────

def get_plan(d):
    try:
        resp = requests.get(FORECAST_CSV_URL, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"  ⚠  Forecast sheet unavailable: {e}")
        return {}

    rows = list(csv.reader(io.StringIO(resp.text)))

    def pv(s):
        try:    return float(str(s).replace("$", "").replace(",", "").strip() or 0)
        except: return 0.0

    col, hdr_idx = {}, None
    for i, row in enumerate(rows):
        row_lc = [c.strip().lower() for c in row]
        if any("soho" in c or "boston" in c for c in row_lc):
            hdr_idx = i
            for j, c in enumerate(row_lc):
                if   "soho"    in c: col["soho"]    = j
                elif "boston"  in c: col["boston"]  = j
                elif "chicago" in c: col["chicago"] = j
                elif "angeles" in c: col["la"]      = j
            break

    if hdr_idx is None:
        print("  ⚠  Could not find header row in plan sheet")
        return {}

    cm, cy = d["mtd_start"].month, d["mtd_start"].year
    plan   = {}
    for row in rows[hdr_idx + 1:]:
        if not row or not row[0].strip():
            continue
        try:
            m, day, y = (int(x) for x in row[0].strip().split("/"))
            if m != cm or y != cy:
                continue
        except:
            continue
        s  = pv(row[col.get("soho",    1)] if len(row) > col.get("soho",    1) else 0)
        b  = pv(row[col.get("boston",  2)] if len(row) > col.get("boston",  2) else 0)
        ch = pv(row[col.get("chicago", 3)] if len(row) > col.get("chicago", 3) else 0)
        la = pv(row[col.get("la",      4)] if len(row) > col.get("la",      4) else 0)
        plan[day] = {"soho": s, "boston": b, "chicago": ch, "la": la, "total": s + b + ch + la}
    return plan

# ── HubSpot ───────────────────────────────────────────────────────────────────

def _hs_headers():
    return {"Authorization": f"Bearer {BW_HS_TOKEN}", "Content-Type": "application/json"}

def _hs_search_deals(filters, properties):
    results, after = [], None
    while True:
        body = {"filterGroups": filters, "properties": properties, "limit": 100}
        if after:
            body["after"] = after
        r = requests.post("https://api.hubapi.com/crm/v3/objects/deals/search",
                          headers=_hs_headers(), json=body, timeout=30)
        if not r.ok:
            print(f"  ⚠  HubSpot deals {r.status_code}: {r.text[:200]}")
            break
        data = r.json()
        results.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
    return results

def hs_mtd_actuals(mtd_start_ms, yd_end_ms):
    all_deals = []
    for stage in HS_CLOSED_WON:
        all_deals.extend(_hs_search_deals(
            filters=[{"filters": [
                {"propertyName": "dealstage",           "operator": "EQ",  "value": stage},
                {"propertyName": "meaningful_contact_", "operator": "EQ",  "value": "true"},
                {"propertyName": "closedate",           "operator": "GTE", "value": str(mtd_start_ms)},
                {"propertyName": "closedate",           "operator": "LTE", "value": str(yd_end_ms)},
            ]}],
            properties=["amount", "hubspot_owner_id", "hubspot_team_id"],
        ))

    by_store, by_owner = {s: 0.0 for s in STORES}, {}
    for deal in all_deals:
        p     = deal.get("properties", {})
        oid   = str(p.get("hubspot_owner_id") or "")
        tid   = str(p.get("hubspot_team_id")  or "")
        amt   = float(p.get("amount") or 0)
        store = HS_TEAM_STORE.get(tid) or HS_OWNER_TEAM.get(oid)
        if store:
            by_store[store] = by_store.get(store, 0) + amt
        if oid in HS_OWNER_NAME:
            by_owner[oid] = by_owner.get(oid, 0) + amt
    return by_store, by_owner

def hs_month_goals(year, month):
    end_day  = calendar.monthrange(year, month)[1]
    start_ms = int(datetime.datetime(year, month, 1,          tzinfo=timezone.utc).timestamp() * 1000)
    end_ms   = int(datetime.datetime(year, month, end_day, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)

    r = requests.post(
        "https://api.hubapi.com/crm/v3/objects/goal_targets/search",
        headers=_hs_headers(),
        json={
            "filterGroups": [{"filters": [
                {"propertyName": "hs_start_datetime", "operator": "GTE", "value": str(start_ms)},
                {"propertyName": "hs_start_datetime", "operator": "LTE", "value": str(end_ms)},
            ]}],
            "properties": ["hs_goal_name", "hs_target_amount"],
            "limit": 100,
        },
        timeout=30,
    )
    if not r.ok:
        print(f"  ⚠  Goals API {r.status_code}: {r.text[:200]}")
        return {s: 0.0 for s in STORES}, {}

    STORE_KW = {
        "soho":    ["new york", "soho"],
        "chicago": ["chicago"],
        "boston":  ["boston"],
        "la":      ["los angeles"],
    }

    by_store, by_name = {s: 0.0 for s in STORES}, {}
    for g in r.json().get("results", []):
        p        = g.get("properties", {})
        name_raw = (p.get("hs_goal_name") or "").strip()
        name_lc  = name_raw.lower()
        amt      = float(p.get("hs_target_amount") or 0)
        if not amt:
            continue
        # Try rep match first (first + last name both appear in goal title)
        matched = False
        for oid, rep_name in HS_OWNER_NAME.items():
            parts = rep_name.split()
            fn    = parts[0].lower()
            ln    = parts[-1].lower() if len(parts) > 1 else fn
            if fn in name_lc and ln in name_lc:
                by_name[rep_name] = amt
                matched = True
                break
        if matched:
            continue
        # Store match
        for store, kws in STORE_KW.items():
            if any(k in name_lc for k in kws):
                by_store[store] = amt
                break
    return by_store, by_name

# ── Helpers ───────────────────────────────────────────────────────────────────

def pct(a, b):   return round((a / b - 1) * 100, 1) if b else None
def fmtd(v):     return f"${v:,.0f}" if v else "$0"
def arrow(v):    return "▲" if v is not None and v >= 0 else "▼"
def pct_s(v):    return f"{abs(v):.0f}%" if v is not None else "n/a"
def sign(v):     return f"{arrow(v)} {pct_s(v)}" if v is not None else "–"
def fmt_pct(v):  return (("+" if v >= 0 else "−") + pct_s(v)) if v is not None else "–"
def css_cls(v):  return "pos" if v is not None and v >= 0 else "neg"
def aov_fn(r):   return round(r["revenue"] / r["orders"]) if r["orders"] else 0

# ── CSS ───────────────────────────────────────────────────────────────────────

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  background: #EAECF2; color: #1A1E2E; font-size: 13px; line-height: 1.5;
}
.header {
  background: #1C2646; color: #EEF0F7; padding: 20px 28px;
  display: flex; justify-content: space-between; align-items: flex-end;
}
.header-title { font-size: 22px; font-weight: 600; letter-spacing: -0.3px; }
.header-sub { font-size: 12px; color: #8C93B0; margin-top: 3px; }
.header-right { text-align: right; font-size: 12px; color: #8C93B0; line-height: 1.7; }
.content { padding: 20px 28px; max-width: 1080px; }
.perf-summary {
  background: white; border: 1px solid #D5D9E8; border-radius: 6px;
  padding: 16px 20px; font-size: 13px; line-height: 1.7; color: #2C3252;
}
.perf-summary p { margin-bottom: 10px; }
.perf-summary p:last-child { margin-bottom: 0; }
.section-label {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 1.2px; color: #6B718F; margin: 22px 0 10px;
  border-bottom: 1px solid #D5D9E8; padding-bottom: 6px;
}
.period-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }
.card { background: white; border: 1px solid #D5D9E8; border-radius: 6px; padding: 14px 16px; }
.card-period {
  font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 1px; color: #6B718F; margin-bottom: 12px;
}
.kpi {
  display: flex; justify-content: space-between; align-items: baseline;
  padding: 7px 0; border-bottom: 1px solid #EBEDF5;
}
.kpi:last-of-type { border-bottom: none; }
.kpi-name { font-size: 12px; color: #6B718F; }
.kpi-right { text-align: right; }
.kpi-val { font-size: 14px; font-weight: 600; }
.kpi-delta { font-size: 11px; color: #6B718F; margin-top: 1px; }
.pos { color: #1A7F5A; } .neg { color: #B84040; } .neutral { color: #6B718F; }
.plan-section { margin-top: 12px; padding-top: 12px; border-top: 1px solid #EBEDF5; }
.plan-header {
  display: flex; justify-content: space-between; font-size: 11px;
  color: #6B718F; margin-bottom: 6px;
}
.plan-bar-track { background: #EBEDF5; border-radius: 3px; height: 7px; overflow: hidden; }
.plan-bar-fill  { background: #2B52CC; height: 100%; border-radius: 3px; }
.plan-bar-fill.over { background: #1A7F5A; }
.plan-detail { font-size: 11px; color: #6B718F; margin-top: 5px; text-align: right; }
.tbl {
  width: 100%; border-collapse: collapse; background: white;
  border: 1px solid #D5D9E8; border-radius: 6px; overflow: hidden;
}
.tbl th {
  background: #F0F2F8; font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.8px; color: #6B718F; padding: 8px 12px; text-align: right; white-space: nowrap;
}
.tbl th:first-child { text-align: left; }
.tbl td {
  padding: 9px 12px; text-align: right; border-bottom: 1px solid #EBEDF5;
  font-size: 12px; white-space: nowrap;
}
.tbl td:first-child { text-align: left; }
.tbl tr:last-child td { border-bottom: none; }
.tbl .total-row td { font-weight: 700; background: #F5F6FB; }
.col-divider { border-left: 2px solid #D5D9E8 !important; }
.store-name { font-weight: 600; }
.chart-wrap { background: white; border: 1px solid #D5D9E8; border-radius: 6px; padding: 16px; }
.chart-legend { display: flex; gap: 18px; margin-bottom: 14px; font-size: 11px; color: #6B718F; }
.dot { width: 10px; height: 10px; border-radius: 2px; display: inline-block;
       margin-right: 5px; vertical-align: middle; }
.pacing-grid { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 12px; }
.pacing-card { background: white; border: 1px solid #D5D9E8; border-radius: 6px; padding: 14px 16px; }
.pacing-store { font-size: 12px; font-weight: 700; color: #1A1E2E; margin-bottom: 4px; }
.pacing-actual { font-size: 18px; font-weight: 700; color: #1A1E2E; margin-bottom: 4px; }
.pacing-badge {
  display: inline-block; font-size: 10px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.6px; padding: 2px 7px; border-radius: 10px; margin-bottom: 10px;
}
.badge-ahead { background: #E4F5ED; color: #1A7F5A; }
.badge-on    { background: #EBF0FB; color: #2B52CC; }
.badge-risk  { background: #FDF3E7; color: #C47A15; }
.pacing-bar-track { background: #EBEDF5; border-radius: 4px; height: 8px; overflow: hidden; margin: 6px 0; }
.pacing-bar-fill  { height: 100%; border-radius: 4px; }
.fill-ahead { background: #1A7F5A; } .fill-on { background: #2B52CC; } .fill-risk { background: #C47A15; }
.pacing-nums { font-size: 11px; color: #6B718F; }
.hs-grid { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 12px; }
.hs-card { background: white; border: 1px solid #D5D9E8; border-radius: 6px; padding: 14px 16px; }
.hs-store { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px; color: #6B718F; margin-bottom: 4px; }
.hs-pct { font-size: 26px; font-weight: 700; color: #1A1E2E; }
.hs-bar-track { background: #EBEDF5; border-radius: 4px; height: 6px; overflow: hidden; margin: 8px 0 6px; }
.hs-bar-fill { height: 100%; border-radius: 4px; background: #2B52CC; }
.hs-bar-fill.over { background: #1A7F5A; }
.hs-nums { font-size: 11px; color: #6B718F; }
.footer {
  padding: 16px 28px; font-size: 11px; color: #8C93B0;
  border-top: 1px solid #D5D9E8; margin-top: 8px;
}
"""

# ── HTML ──────────────────────────────────────────────────────────────────────

def make_html(d, ty_yd, ty_lw, ty_mtd, ly_yd, ly_lw, ly_mtd,
              stores_yd, stores_lw, stores_mtd,
              stores_ly_yd, stores_ly_lw, stores_ly_mtd,
              plan, daily_actuals, ty_colls, ly_colls, associates_mtd,
              store_plan_mtd, mtd_plan, lw_plan, yd_plan,
              hs_by_store=None, hs_by_owner=None,
              hs_goals_store=None, hs_goals_rep=None):

    yd_day = d["yd"].day

    def _s(v): return (f"+{abs(v):.0f}%" if v >= 0 else f"−{abs(v):.0f}%") if v is not None else "n/a"

    # ── Performance summary ───────────────────────────────────────────────────
    mtd_vly_v = pct(ty_mtd["revenue"], ly_mtd["revenue"])
    mtd_vp_v  = pct(ty_mtd["revenue"], mtd_plan)
    lw_vly_v  = pct(ty_lw["revenue"],  ly_lw["revenue"])
    lw_vp_v   = pct(ty_lw["revenue"],  lw_plan)
    yd_vly_v  = pct(ty_yd["revenue"],  ly_yd["revenue"])
    yd_vp_v   = pct(ty_yd["revenue"],  yd_plan)

    p1 = (f"MTD retail revenue of {fmtd(ty_mtd['revenue'])} is tracking "
          f"{_s(mtd_vp_v)} vs plan and {sign(mtd_vly_v)} vs last year "
          f"through {d['yd'].strftime('%B %-d')}.")
    if ty_lw["revenue"] > 0:
        lw_note = "ahead of plan" if (lw_vp_v or 0) >= 0 else f"{_s(lw_vp_v)} vs plan"
        p1 += (f" Last week ({d['lw_start'].strftime('%b %-d')}–{d['lw_end'].strftime('%-d')}) "
               f"came in at {fmtd(ty_lw['revenue'])}, {lw_note} and {sign(lw_vly_v)} vs LY "
               f"({ty_lw['orders']} orders, AOV {fmtd(aov_fn(ty_lw))}).")
    if ty_yd["revenue"] == 0 and yd_plan > 0:
        p1 += (f" No orders recorded {d['yd'].strftime('%A, %B %-d')}, "
               f"driving the full gap against a {fmtd(yd_plan)} daily plan.")
    elif ty_yd["revenue"] > 0:
        p1 += (f" Yesterday ({d['yd'].strftime('%a %b %-d')}): {fmtd(ty_yd['revenue'])} — "
               f"{sign(yd_vly_v)} vs LY, {_s(yd_vp_v)} vs plan, {ty_yd['orders']} orders.")

    # Store summary paragraph
    def _store_line(key):
        rev  = stores_mtd.get(key, 0)
        ly_r = stores_ly_mtd.get(key, 0)
        vly  = pct(rev, ly_r)
        vp   = pct(rev, store_plan_mtd.get(key, 0))
        bits = [fmtd(rev)]
        if vp  is not None: bits.append(f"{_s(vp)} vs plan")
        if vly is not None: bits.append(f"{sign(vly)} vs LY")
        return f"({', '.join(bits)})"

    sorted_stores = sorted(STORES, key=lambda s: stores_mtd.get(s, 0), reverse=True)
    p2_parts = []
    for s in sorted_stores:
        p2_parts.append(f"{STORE_LABELS[s]} {_store_line(s)}")
    p2 = "Store MTD breakdown: " + "; ".join(p2_parts) + "."

    # Collections highlight
    p3 = ""
    if ty_colls and ty_mtd["revenue"] > 0:
        ly_map = {r.get(Looker.COLL, ""): float(r.get(Looker.REV, 0) or 0) for r in ly_colls if r.get(Looker.COLL)}
        total  = sum(float(r.get(Looker.REV, 0) or 0) for r in ty_colls)
        top3   = []
        for r in ty_colls[:3]:
            coll = r.get(Looker.COLL, "") or ""
            rev  = float(r.get(Looker.REV, 0) or 0)
            mix  = round(rev / total * 100) if total else 0
            vly  = pct(rev, ly_map[coll]) if coll in ly_map else None
            part = f"{coll} {fmtd(rev)} ({mix}%"
            if vly is not None: part += f", {sign(vly)} vs LY"
            part += ")"
            top3.append(part)
        if top3:
            p3 = "Top collections MTD: " + ", ".join(top3) + "."

    summary_html = (
        '<div class="section-label">Performance Summary</div>'
        '<div class="perf-summary">'
        f'<p>{p1}</p>'
        f'<p>{p2}</p>'
        + (f'<p>{p3}</p>' if p3 else '')
        + '</div>'
    )

    # ── KPI cards ─────────────────────────────────────────────────────────────
    def kpi_card(period_label, r, ly_r, plan_total, plan_label):
        rev_vly = pct(r["revenue"], ly_r["revenue"])
        ord_vly = pct(r["orders"],  ly_r["orders"])
        aov_vly = pct(aov_fn(r),    aov_fn(ly_r))
        vp      = pct(r["revenue"], plan_total)
        bar_w   = min(100, round(r["revenue"] / plan_total * 100)) if plan_total else 0
        over    = " over" if vp is not None and vp >= 0 else ""
        return f"""
    <div class="card">
      <div class="card-period">{period_label}</div>
      <div class="kpi">
        <span class="kpi-name">Revenue</span>
        <div class="kpi-right">
          <div class="kpi-val">{fmtd(r["revenue"])}</div>
          <div class="kpi-delta"><span class="{css_cls(rev_vly)}">{sign(rev_vly)} vs LY</span></div>
        </div>
      </div>
      <div class="kpi">
        <span class="kpi-name">Orders</span>
        <div class="kpi-right">
          <div class="kpi-val">{r["orders"]}</div>
          <div class="kpi-delta"><span class="{css_cls(ord_vly)}">{sign(ord_vly)} vs LY</span></div>
        </div>
      </div>
      <div class="kpi">
        <span class="kpi-name">AOV</span>
        <div class="kpi-right">
          <div class="kpi-val">{"n/a" if not r["orders"] else fmtd(aov_fn(r))}</div>
          <div class="kpi-delta"><span class="{css_cls(aov_vly)}">{sign(aov_vly)} vs LY</span></div>
        </div>
      </div>
      <div class="plan-section">
        <div class="plan-header">
          <span>vs Plan ({plan_label})</span>
          <strong class="{css_cls(vp)}">{fmt_pct(vp)}</strong>
        </div>
        <div class="plan-bar-track">
          <div class="plan-bar-fill{over}" style="width:{bar_w}%;"></div>
        </div>
        <div class="plan-detail">{fmtd(r["revenue"])} / {fmtd(plan_total)} plan</div>
      </div>
    </div>"""

    # ── Store performance table ───────────────────────────────────────────────
    def store_tbl_row(key, label):
        yd_vly  = pct(stores_yd.get(key, 0),  stores_ly_yd.get(key, 0))
        lw_vly  = pct(stores_lw.get(key, 0),  stores_ly_lw.get(key, 0))
        mtd_vly = pct(stores_mtd.get(key, 0), stores_ly_mtd.get(key, 0))
        mtd_vp  = pct(stores_mtd.get(key, 0), store_plan_mtd.get(key, 0))
        return (f'<tr><td><span class="store-name">{label}</span></td>'
                f'<td>{fmtd(stores_yd.get(key, 0))}</td>'
                f'<td><span class="{css_cls(yd_vly)}">{sign(yd_vly)}</span></td>'
                f'<td class="col-divider">{fmtd(stores_lw.get(key, 0))}</td>'
                f'<td><span class="{css_cls(lw_vly)}">{sign(lw_vly)}</span></td>'
                f'<td class="col-divider">{fmtd(stores_mtd.get(key, 0))}</td>'
                f'<td><span class="{css_cls(mtd_vp)}">{fmt_pct(mtd_vp)}</span></td>'
                f'<td><span class="{css_cls(mtd_vly)}">{sign(mtd_vly)}</span></td></tr>')

    total_yd_vly  = pct(ty_yd["revenue"],  ly_yd["revenue"])
    total_lw_vly  = pct(ty_lw["revenue"],  ly_lw["revenue"])
    total_mtd_vly = pct(ty_mtd["revenue"], ly_mtd["revenue"])
    total_mtd_vp  = pct(ty_mtd["revenue"], mtd_plan)

    store_table = f"""
  <table class="tbl">
    <thead>
      <tr>
        <th>Store</th>
        <th>Yesterday</th><th>vs LY</th>
        <th class="col-divider">Last Week ({d['lw_start'].strftime('%b %-d')}–{d['lw_end'].strftime('%-d')})</th><th>vs LY</th>
        <th class="col-divider">MTD</th><th>vs Plan</th><th>vs LY</th>
      </tr>
    </thead>
    <tbody>
      <tr class="total-row">
        <td>Total Retail</td>
        <td>{fmtd(ty_yd["revenue"])}</td>
        <td><span class="{css_cls(total_yd_vly)}">{sign(total_yd_vly)}</span></td>
        <td class="col-divider">{fmtd(ty_lw["revenue"])}</td>
        <td><span class="{css_cls(total_lw_vly)}">{sign(total_lw_vly)}</span></td>
        <td class="col-divider">{fmtd(ty_mtd["revenue"])}</td>
        <td><span class="{css_cls(total_mtd_vp)}">{fmt_pct(total_mtd_vp)}</span></td>
        <td><span class="{css_cls(total_mtd_vly)}">{sign(total_mtd_vly)}</span></td>
      </tr>
      {store_tbl_row("soho",    "Soho")}
      {store_tbl_row("boston",  "Boston")}
      {store_tbl_row("chicago", "Chicago")}
      {store_tbl_row("la",      "Los Angeles")}
    </tbody>
  </table>"""

    # ── MTD Pacing to Goal cards ──────────────────────────────────────────────
    def pacing_card(key, label):
        actual = stores_mtd.get(key, 0)
        goal   = store_plan_mtd.get(key, 0)
        p_val  = round(actual / goal * 100) if goal else None
        bar_w  = min(100, p_val or 0)
        if p_val is None:
            badge_cls, badge_txt, fill_cls = "badge-on", "No Data", "fill-on"
        elif p_val >= 110:
            badge_cls, badge_txt, fill_cls = "badge-ahead", "Pacing Ahead", "fill-ahead"
        elif p_val >= 90:
            badge_cls, badge_txt, fill_cls = "badge-on",    "On Track",     "fill-on"
        else:
            badge_cls, badge_txt, fill_cls = "badge-risk",  "At Risk",      "fill-risk"
        pct_text = f"{p_val}% of plan" if p_val is not None else "–"
        return f"""
      <div class="pacing-card">
        <div class="pacing-store">{label}</div>
        <div class="pacing-actual">{fmtd(actual)}</div>
        <span class="pacing-badge {badge_cls}">{badge_txt}</span>
        <div class="pacing-bar-track">
          <div class="pacing-bar-fill {fill_cls}" style="width:{bar_w}%;"></div>
        </div>
        <div class="pacing-nums">{pct_text} &nbsp;·&nbsp; Plan: {fmtd(goal)}</div>
      </div>"""

    pacing_section = f"""
  <div class="section-label">MTD Pacing to Plan &mdash; {d['mtd_start'].strftime('%b %Y')}</div>
  <div class="pacing-grid">
    {pacing_card("soho",    "Soho")}
    {pacing_card("boston",  "Boston")}
    {pacing_card("chicago", "Chicago")}
    {pacing_card("la",      "Los Angeles")}
  </div>"""

    # ── Daily chart ───────────────────────────────────────────────────────────
    days_list  = list(range(1, yd_day + 1))
    actuals_js = ", ".join(str(daily_actuals.get(n, 0)) for n in days_list)
    plans_js   = ", ".join(str(plan.get(n, {}).get("total", 0)) for n in days_list)
    labels_js  = ", ".join(f"'{n}'" for n in days_list)
    max_val    = max([daily_actuals.get(n, 0) for n in days_list] +
                     [plan.get(n, {}).get("total", 0) for n in days_list] + [1])
    chart_max  = max(8000, (int(max_val / 2000) + 1) * 2000)

    # ── Collections table ─────────────────────────────────────────────────────
    coll_html = ""
    if ty_colls:
        ly_map = {r.get(Looker.COLL, ""): (float(r.get(Looker.REV, 0) or 0),
                                            int(r.get(Looker.UNITS, 0) or 0))
                  for r in ly_colls if r.get(Looker.COLL)}
        total = sum(float(r.get(Looker.REV, 0) or 0) for r in ty_colls) or 1
        rows_html = ""
        for r in ty_colls:
            coll  = r.get(Looker.COLL, "Other") or "Other"
            rev   = float(r.get(Looker.REV,   0) or 0)
            units = int(  r.get(Looker.UNITS, 0) or 0)
            mix   = round(rev / total * 100)
            bar_w = max(1, round(mix * 2.5))
            ly_rev, _  = ly_map.get(coll, (0, 0))
            coll_vly   = pct(rev, ly_rev)
            vly_cell   = (f'<span class="{css_cls(coll_vly)}">{sign(coll_vly)}</span>'
                          if coll_vly is not None else '<span class="neutral">n/a</span>')
            rows_html += (f'<tr><td>{coll}</td><td>{fmtd(rev)}</td>'
                          f'<td style="text-align:left;padding-left:8px;">'
                          f'<span style="display:inline-block;height:6px;background:#D4C5B0;border-radius:2px;vertical-align:middle;margin-right:6px;width:{bar_w}px;"></span>{mix}%</td>'
                          f'<td>{units}</td><td>{vly_cell}</td></tr>')
        coll_html = f"""
  <div class="section-label">Collection Mix &mdash; MTD {d['mtd_start'].strftime('%b %-d')}–{d['yd'].strftime('%-d')}</div>
  <table class="tbl">
    <thead>
      <tr>
        <th>Collection</th><th>Revenue</th>
        <th style="text-align:left;padding-left:8px;">Mix</th>
        <th>Units</th><th>vs LY</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>"""

    # ── Associates table ──────────────────────────────────────────────────────
    assoc_html = ""
    if associates_mtd:
        rows_html = ""
        for store in STORES:
            store_data = associates_mtd.get(store, {})
            if not store_data:
                continue
            sorted_assocs = sorted(store_data.items(), key=lambda x: x[1]["revenue"], reverse=True)
            for i, (name, data) in enumerate(sorted_assocs):
                aov_v = round(data["revenue"] / data["orders"]) if data["orders"] else 0
                store_cell = (f'<td rowspan="{len(sorted_assocs)}" style="font-weight:700;vertical-align:top;padding-top:11px;">'
                              f'{STORE_LABELS[store]}</td>'
                              if i == 0 else '')
                rows_html += (f'<tr>{store_cell}'
                              f'<td style="text-align:left;">{name}</td>'
                              f'<td>{fmtd(data["revenue"])}</td>'
                              f'<td>{data["orders"]}</td>'
                              f'<td>{fmtd(aov_v)}</td>'
                              f'<td>{data["units"]}</td></tr>')
        assoc_html = f"""
  <div class="section-label">Sales Associates &mdash; MTD {d['mtd_start'].strftime('%b %-d')}–{d['yd'].strftime('%-d')}</div>
  <table class="tbl">
    <thead>
      <tr><th>Store</th><th>Associate</th><th>Revenue</th><th>Orders</th><th>AOV</th><th>Units</th></tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>"""

    # ── HubSpot % to Goal ─────────────────────────────────────────────────────
    hs_html = ""
    if hs_by_store and hs_goals_store and any(hs_goals_store.values()):
        def hs_card(key, label):
            actual = (hs_by_store or {}).get(key, 0)
            goal   = (hs_goals_store or {}).get(key, 0)
            p_val  = round(actual / goal * 100) if goal else None
            bar_w  = min(100, p_val or 0)
            over   = " over" if p_val and p_val >= 100 else ""
            p_text = f"{p_val}%" if p_val is not None else "–"
            return f"""
        <div class="hs-card">
          <div class="hs-store">{label}</div>
          <div class="hs-pct">{p_text}</div>
          <div class="hs-bar-track"><div class="hs-bar-fill{over}" style="width:{bar_w}%;"></div></div>
          <div class="hs-nums">{fmtd(actual)} / {fmtd(goal)} goal</div>
        </div>"""

        store_cards = "".join(hs_card(s, STORE_LABELS[s]) for s in STORES)

        # Rep table
        rep_rows = ""
        if hs_by_owner and hs_goals_rep:
            # Group reps by store
            for store in STORES:
                store_reps = [(oid, name) for oid, name in HS_OWNER_NAME.items()
                              if HS_OWNER_TEAM.get(oid) == store]
                store_reps_with_data = [
                    (name, hs_by_owner.get(oid, 0), hs_goals_rep.get(name, 0))
                    for oid, name in store_reps
                    if hs_by_owner.get(oid, 0) > 0 or hs_goals_rep.get(name, 0) > 0
                ]
                store_reps_with_data.sort(key=lambda x: x[1], reverse=True)
                for i, (name, actual, goal) in enumerate(store_reps_with_data):
                    p_val   = round(actual / goal * 100) if goal else None
                    p_text  = f"{p_val}%" if p_val is not None else "–"
                    p_cls   = "pos" if p_val and p_val >= 100 else ("neg" if p_val and p_val < 90 else "neutral")
                    store_cell = (f'<td rowspan="{len(store_reps_with_data)}" style="font-weight:700;vertical-align:top;padding-top:11px;">'
                                  f'{STORE_LABELS[store]}</td>'
                                  if i == 0 else '')
                    rep_rows += (f'<tr>{store_cell}'
                                 f'<td style="text-align:left;">{name}</td>'
                                 f'<td>{fmtd(actual)}</td>'
                                 f'<td>{fmtd(goal)}</td>'
                                 f'<td><span class="{p_cls}">{p_text}</span></td></tr>')

        rep_table = (f"""
  <table class="tbl" style="margin-top:12px;">
    <thead>
      <tr><th>Store</th><th>Associate</th><th>MTD Actual</th><th>Monthly Goal</th><th>% to Goal</th></tr>
    </thead>
    <tbody>{rep_rows}</tbody>
  </table>""" if rep_rows else "")

        hs_html = f"""
  <div class="section-label">HubSpot % to Goal &mdash; MTD (Meaningful Contact + Closed Won)</div>
  <div class="hs-grid">{store_cards}</div>
  {rep_table}"""

    # ── Assemble ──────────────────────────────────────────────────────────────
    lw_label  = f"{d['lw_start'].strftime('%b %-d')}–{d['lw_end'].strftime('%-d, %Y')}"
    yd_label  = d["yd"].strftime("%a %b %-d")
    mtd_label = f"{d['mtd_start'].strftime('%b %-d')}–{d['yd'].strftime('%-d')}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Burrow Retail &mdash; {d['week_label']}</title>
<style>{CSS}</style>
</head>
<body>

<div class="header">
  <div>
    <div class="header-title">Burrow Retail</div>
    <div class="header-sub">Weekly Report &mdash; {d['week_label']}</div>
  </div>
  <div class="header-right">
    Data through {d['yd'].strftime('%a %b %-d, %Y')}<br>
    Soho &middot; Boston &middot; Chicago &middot; Los Angeles
  </div>
</div>

<div class="content">

  {summary_html}

  <div class="section-label">Performance Overview</div>
  <div class="period-grid">
    {kpi_card(f"Yesterday &mdash; {yd_label}", ty_yd, ly_yd, yd_plan, d['yd'].strftime('%b %-d'))}
    {kpi_card(f"Last Week &mdash; {d['lw_start'].strftime('%b %-d')}–{d['lw_end'].strftime('%-d')}", ty_lw, ly_lw, lw_plan, lw_label)}
    {kpi_card(f"MTD &mdash; {mtd_label}", ty_mtd, ly_mtd, mtd_plan, mtd_label)}
  </div>

  <div class="section-label">Store Performance</div>
  {store_table}

  {pacing_section}

  <div class="section-label">MTD Daily Revenue vs Plan &mdash; {d['mtd_start'].strftime('%b %Y')}</div>
  <div class="chart-wrap">
    <div class="chart-legend">
      <span><span class="dot" style="background:#2B52CC;"></span>Actual Revenue</span>
      <span><span class="dot" style="background:#C8CEDE;"></span>Daily Plan</span>
    </div>
    <canvas id="dailyChart" style="width:100%;height:auto;display:block;"></canvas>
  </div>

  {coll_html}
  {assoc_html}
  {hs_html}

</div>

<div class="footer">
  Generated {d['today'].strftime('%a %b %-d, %Y')} &nbsp;&middot;&nbsp;
  Data through {d['yd'].strftime('%b %-d, %Y')} &nbsp;&middot;&nbsp;
  Source: Looker / Burrow model + HubSpot &nbsp;&middot;&nbsp;
  Filter: Retail channel
</div>

<script>
(function() {{
  var canvas = document.getElementById('dailyChart');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');
  var days    = [{labels_js}];
  var actuals = [{actuals_js}];
  var plans   = [{plans_js}];
  var W=900, H=200;
  canvas.width=W; canvas.height=H;
  var padL=56, padR=12, padT=8, padB=28;
  var cW=W-padL-padR, cH=H-padT-padB;
  var MAX={chart_max}, n=days.length;
  var groupW=cW/n, bW=Math.max(groupW*0.3,5);
  var step=MAX/4;
  [0,step,step*2,step*3,MAX].forEach(function(v) {{
    var y=padT+cH-(v/MAX)*cH;
    ctx.save(); ctx.strokeStyle='#EBEDF5'; ctx.lineWidth=1; ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.moveTo(padL,y); ctx.lineTo(padL+cW,y); ctx.stroke(); ctx.restore();
    ctx.fillStyle='#8C93B0'; ctx.font='10px -apple-system,sans-serif'; ctx.textAlign='right';
    ctx.fillText(v===0?'$0':'$'+(v/1000)+'K', padL-5, y+3.5);
  }});
  days.forEach(function(day,i) {{
    var gx=padL+i*groupW, cx=gx+groupW/2;
    var pH=(plans[i]/MAX)*cH;
    if (plans[i]>0) {{ ctx.fillStyle='#C8CEDE'; ctx.fillRect(cx-bW,padT+cH-pH,bW,Math.max(pH,1)); }}
    var aH=(actuals[i]/MAX)*cH;
    ctx.fillStyle=actuals[i]>0?'#2B52CC':'#F0F2F8';
    ctx.fillRect(cx,padT+cH-aH,bW,Math.max(aH,1));
    ctx.fillStyle='#8C93B0'; ctx.font='9px -apple-system,sans-serif';
    ctx.textAlign='center'; ctx.fillText(day,cx,H-8);
  }});
  ctx.strokeStyle='#D5D9E8'; ctx.lineWidth=1; ctx.setLineDash([]);
  ctx.beginPath(); ctx.moveTo(padL,padT+cH); ctx.lineTo(padL+cW,padT+cH); ctx.stroke();
}})();
</script>
</body>
</html>"""

# ── GitHub Pages ──────────────────────────────────────────────────────────────

def push_report_page(html, d):
    if not GITHUB_TOKEN:
        print("  ⚠  No GITHUB_TOKEN — skipping page publish")
        return None
    encoded = base64.b64encode(html.encode()).decode()
    headers = {"Authorization": f"token {GITHUB_TOKEN}",
               "Accept": "application/vnd.github+json",
               "Content-Type": "application/json"}
    r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/index.html",
                     headers=headers, params={"ref": "gh-pages"})
    body = {"message": f"Report {d['yd']}", "content": encoded, "branch": "gh-pages"}
    if r.ok:
        body["sha"] = r.json()["sha"]
    r2 = requests.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/index.html",
                      headers=headers, json=body)
    if r2.ok:
        print(f"✅  Published to {PAGE_URL}")
        return PAGE_URL
    print(f"  ⚠  Page publish failed: {r2.status_code} {r2.text[:300]}")
    return None

# ── Slack deduplication ───────────────────────────────────────────────────────

_POSTED_FLAG = "last_slack_post.txt"

def _gh_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}

def check_already_posted(date_str):
    if not GITHUB_TOKEN:
        return False
    r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_POSTED_FLAG}",
                     headers=_gh_headers())
    if not r.ok:
        return False
    try:
        return base64.b64decode(r.json()["content"]).decode().strip() == date_str
    except:
        return False

def mark_as_posted(date_str):
    if not GITHUB_TOKEN:
        return
    headers = _gh_headers()
    encoded = base64.b64encode(date_str.encode()).decode()
    body    = {"message": f"Mark Slack post {date_str}", "content": encoded}
    r       = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_POSTED_FLAG}",
                            headers=headers)
    if r.ok:
        body["sha"] = r.json()["sha"]
    r2 = requests.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{_POSTED_FLAG}",
                      headers=headers, json=body)
    if r2.ok:
        print(f"✅  Marked as posted ({date_str})")
    else:
        print(f"  ⚠  Could not update flag: {r2.status_code} {r2.text[:100]}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    d = compute_dates()
    print(f"Week: {d['week_label']}  yd={d['yd']}  mtd_start={d['mtd_start']}")

    today_str = str(d["today"])
    if check_already_posted(today_str):
        print(f"ℹ️  Already posted today ({today_str}) — skipping")
        return

    print("Reading forecast sheet...")
    plan = get_plan(d)

    print("Connecting to Looker...")
    lk = Looker()

    print("Querying totals (6 windows)...")
    ty_yd  = lk.totals(d["yd"],           d["yd"])
    ly_yd  = lk.totals(d["ly_yd"],        d["ly_yd"])
    ty_lw  = lk.totals(d["lw_start"],     d["lw_end"])
    ly_lw  = lk.totals(d["ly_lw_start"],  d["ly_lw_end"])
    # MTD range ends at yd_minus1 to avoid Looker data lag; yesterday added below.
    ty_mtd = lk.totals(d["mtd_start"],    d["yd_minus1"])
    ly_mtd = lk.totals(d["ly_mtd_start"], d["ly_yd_minus1"])

    print("Querying store breakdown...")
    stores_yd     = lk.stores(d["yd"],           d["yd"])
    stores_ly_yd  = lk.stores(d["ly_yd"],        d["ly_yd"])
    stores_lw     = lk.stores(d["lw_start"],     d["lw_end"])
    stores_ly_lw  = lk.stores(d["ly_lw_start"],  d["ly_lw_end"])
    stores_mtd    = lk.stores(d["mtd_start"],    d["yd_minus1"])
    stores_ly_mtd = lk.stores(d["ly_mtd_start"], d["ly_yd_minus1"])

    print("Querying daily actuals...")
    daily_actuals = lk.daily(d["mtd_start"], d["yd_minus1"])
    daily_actuals[d["yd"].day] = ty_yd["revenue"]

    print("Querying collections (TY + LY)...")
    ty_colls = lk.collections(d["mtd_start"],    d["yd_minus1"])
    ly_colls = lk.collections(d["ly_mtd_start"], d["ly_yd_minus1"])

    print("Querying associates...")
    associates_mtd = lk.associates(d["mtd_start"], d["yd_minus1"])
    associates_yd  = lk.associates(d["yd"],        d["yd"])
    # Merge yesterday into MTD associates
    for store, store_data in associates_yd.items():
        for assoc, data in store_data.items():
            associates_mtd.setdefault(store, {}).setdefault(assoc, {"revenue": 0.0, "orders": 0, "units": 0})
            associates_mtd[store][assoc]["revenue"] += data["revenue"]
            associates_mtd[store][assoc]["orders"]  += data["orders"]
            associates_mtd[store][assoc]["units"]   += data["units"]

    # Merge yesterday into MTD totals and stores
    for key in ("revenue", "orders", "units"):
        ty_mtd[key] += ty_yd[key]
        ly_mtd[key] += ly_yd[key]
    for store in STORES:
        stores_mtd[store]    += stores_yd.get(store, 0)
        stores_ly_mtd[store] += stores_ly_yd.get(store, 0)

    # Plan sums
    yd_day   = d["yd"].day
    yd_plan  = plan.get(yd_day, {}).get("total", 0)
    lw_days  = [d["lw_start"] + datetime.timedelta(days=i) for i in range(7)]
    lw_plan  = sum(plan.get(day.day, {}).get("total", 0)
                   for day in lw_days if day.month == d["mtd_start"].month)
    mtd_plan = sum(plan.get(day, {}).get("total", 0) for day in range(1, yd_day + 1))
    store_plan_mtd = {s: sum(plan.get(day, {}).get(s, 0) for day in range(1, yd_day + 1))
                      for s in STORES}

    # HubSpot
    hs_by_store, hs_by_owner   = {s: 0.0 for s in STORES}, {}
    hs_goals_store, hs_goals_rep = {s: 0.0 for s in STORES}, {}
    if BW_HS_TOKEN:
        print("Querying HubSpot...")
        try:
            mtd_start_ms = int(datetime.datetime(d["mtd_start"].year, d["mtd_start"].month, 1,
                                                  tzinfo=timezone.utc).timestamp() * 1000)
            yd_end_ms    = int(datetime.datetime(d["yd"].year, d["yd"].month, d["yd"].day,
                                                  23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
            hs_by_store, hs_by_owner     = hs_mtd_actuals(mtd_start_ms, yd_end_ms)
            hs_goals_store, hs_goals_rep = hs_month_goals(d["mtd_start"].year, d["mtd_start"].month)
        except Exception as e:
            print(f"  ⚠  HubSpot error: {e}")

    print("Generating HTML report...")
    html = make_html(
        d, ty_yd, ty_lw, ty_mtd, ly_yd, ly_lw, ly_mtd,
        stores_yd, stores_lw, stores_mtd, stores_ly_yd, stores_ly_lw, stores_ly_mtd,
        plan, daily_actuals, ty_colls, ly_colls, associates_mtd,
        store_plan_mtd, mtd_plan, lw_plan, yd_plan,
        hs_by_store=hs_by_store, hs_by_owner=hs_by_owner,
        hs_goals_store=hs_goals_store, hs_goals_rep=hs_goals_rep,
    )
    report_url = push_report_page(html, d)

    # ── Slack message ─────────────────────────────────────────────────────────
    mtd_vly   = pct(ty_mtd["revenue"], ly_mtd["revenue"])
    lw_vly    = pct(ty_lw["revenue"],  ly_lw["revenue"])
    mtd_vp    = pct(ty_mtd["revenue"], mtd_plan)
    lw_vp     = pct(ty_lw["revenue"],  lw_plan)
    mtd_aov   = aov_fn(ty_mtd)
    mtd_aov_vly = pct(mtd_aov, aov_fn(ly_mtd))

    lw_label  = f"{d['lw_start'].strftime('%b %-d')}–{d['lw_end'].strftime('%-d')}"
    link_line = f"\n<{report_url}|View full report →>" if report_url else ""

    text = (
        f"📊 *Burrow Retail — {d['week_label']}*{link_line}\n\n"
        f"*MTD (thru {d['yd'].strftime('%b %-d')})*\n"
        f"Revenue: *{fmtd(ty_mtd['revenue'])}*  {sign(mtd_vly)} vs LY  |  {fmt_pct(mtd_vp)} vs plan\n"
        f"Orders: *{ty_mtd['orders']}*  |  AOV: *{fmtd(mtd_aov)}*  {sign(mtd_aov_vly)} vs LY\n\n"
        f"*Store Breakdown (MTD)*\n"
    )
    for s in STORES:
        vp = pct(stores_mtd[s], store_plan_mtd.get(s, 0))
        text += f"• {STORE_LABELS[s]}: {fmtd(stores_mtd[s])}  ({fmt_pct(vp)} vs plan)\n"
    text += (
        f"\n*Last Week ({lw_label})*\n"
        f"Revenue: {fmtd(ty_lw['revenue'])}  {sign(lw_vly)} vs LY  |  {fmt_pct(lw_vp)} vs plan\n"
        f"Orders: {ty_lw['orders']}  |  AOV: {fmtd(aov_fn(ty_lw))}\n"
    )
    if hs_goals_store and any(hs_goals_store.values()):
        text += "\n*HubSpot % to Goal (meaningful contact)*\n"
        for s in STORES:
            act  = hs_by_store.get(s, 0)
            goal = hs_goals_store.get(s, 0)
            pv   = round(act / goal * 100) if goal else None
            text += f"• {STORE_LABELS[s]}: {pv}%\n" if pv else f"• {STORE_LABELS[s]}: –\n"

    print("Posting to Slack...")
    resp = requests.post(SLACK_WEBHOOK, json={"text": text, "mrkdwn": True})
    if resp.status_code == 200 and resp.text == "ok":
        print("✅  Posted to Slack")
        mark_as_posted(today_str)
    else:
        print(f"❌  Slack error: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
