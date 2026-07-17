#!/usr/bin/env python3
"""Web console + scheduler for the sealed price collector.

Runs the pipeline every day at 03:00 UTC (both sources refresh overnight) and
serves a small local web UI to keep an eye on things and curate the catalog:

  /            run history and status
  /triage      products never seen before - approve, drop, or pick a match
  /catalog     browse what's published, filter by set/type/name
  /divergence  products where the $ and EUR prices disagree suspiciously
  /decisions   everything decided so far, with undo and a yaml backup export

New products are NOT published until approved in triage. If you never open
the console, already-approved products just keep updating.

Env vars: PORT (8811), OUT_DIR (out), STATE_DB (collector_state.db),
SCHEDULE (1), PUBLISH (0 - upload step, off until hosting is set up).
"""

import datetime as dt
import html
import json
import os
import sqlite3
import subprocess
import sys
import threading

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

import build_sealed_db as pipeline

PORT = int(os.environ.get("PORT", "8811"))
OUT_DIR = os.environ.get("OUT_DIR", "out")
STATE_DB = os.environ.get("STATE_DB", "collector_state.db")
SCHEDULE = os.environ.get("SCHEDULE", "1") == "1"
CATCHUP_AFTER_HOURS = 20  # on startup, run if the last success is older than this
PAGE_SIZE = 50
CM_URL = "https://www.cardmarket.com/en/Pokemon/Products?idProduct={}"

app = FastAPI(title="sealed-collector")
run_lock = threading.Lock()


def open_state():
    db = sqlite3.connect(STATE_DB)
    db.executescript(pipeline.STATE_SCHEMA)
    for migration in ("ALTER TABLE runs ADD COLUMN pending INTEGER",
                      "ALTER TABLE product_decisions ADD COLUMN name TEXT",
                      "ALTER TABLE product_decisions ADD COLUMN group_name TEXT"):
        try:
            db.execute(migration)
        except sqlite3.OperationalError:
            pass
    return db


def open_catalog():
    return sqlite3.connect(os.path.join(OUT_DIR, "sealed_prices.db"))


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def product_info(state_db, product_id):
    """Name and set for a product, wherever it currently lives."""
    row = state_db.execute("SELECT name, group_name FROM pending_products "
                           "WHERE product_id=?", (product_id,)).fetchone()
    if row:
        return row
    try:
        catalog = open_catalog()
        row = catalog.execute(
            "SELECT p.name, s.name FROM sealed_products p "
            "JOIN sealed_sets s ON s.group_id=p.group_id "
            "WHERE p.product_id=?", (product_id,)).fetchone()
        catalog.close()
        if row:
            return row
    except sqlite3.Error:
        pass
    return (None, None)


def save_decision(state_db, product_id, decision, cm_id):
    name, group_name = product_info(state_db, product_id)
    with state_db:
        # a product that's no longer visible anywhere keeps its old stored name
        old = state_db.execute("SELECT name, group_name FROM product_decisions "
                               "WHERE product_id=?", (product_id,)).fetchone()
        if old and not name:
            name, group_name = old
        state_db.execute(
            "INSERT OR REPLACE INTO product_decisions "
            "(product_id, decision, cm_id, decided_at, name, group_name) "
            "VALUES (?,?,?,?,?,?)",
            (product_id, decision, cm_id, now_iso(), name, group_name))
        state_db.execute("DELETE FROM pending_products WHERE product_id=?", (product_id,))


# ------------------------------------------------------------ running

def run_pipeline(reason):
    if not run_lock.acquire(blocking=False):
        return {"ok": False, "error": "a run is already in progress"}
    try:
        print(f"[console] run start ({reason})")
        version, report = pipeline.run(OUT_DIR, STATE_DB)
        check = subprocess.run(
            [sys.executable, "validate_db.py", "--db", os.path.join(OUT_DIR, "sealed_prices.db")],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
        summary = check.stdout.strip().splitlines()[-1] if check.stdout else ""
        db = open_state()
        with db:
            db.execute("UPDATE runs SET message=? WHERE id=(SELECT MAX(id) FROM runs)",
                       (f"validate: {summary} (exit {check.returncode})",))
        db.close()
        if os.environ.get("PUBLISH", "0") == "1" and check.returncode == 0:
            print("[console] upload step would go here - not set up yet")
        return {"ok": True, "version": version, "validate_exit": check.returncode}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        run_lock.release()


def start_run(reason):
    threading.Thread(target=run_pipeline, args=(reason,), daemon=True).start()


# ------------------------------------------------------------ page chrome

STYLE = """<style>
 body{background:#0d0d14;color:#f0f0fa;font:14px/1.45 -apple-system,Inter,sans-serif;margin:0}
 header{position:sticky;top:0;background:#17172a;padding:10px 16px;display:flex;gap:10px;align-items:center;z-index:2;flex-wrap:wrap}
 a.nav{color:#f0f0fa;text-decoration:none;padding:6px 12px;border-radius:8px;background:#21213a}
 a.nav.on{background:#6c63ff} a{color:#5ba4f5;text-decoration:none}
 button{background:#6c63ff;color:#fff;border:0;border-radius:8px;padding:6px 14px;cursor:pointer}
 button.ghost{background:#21213a;border:1px solid #333355}
 table{border-collapse:collapse;width:100%} td,th{padding:6px 10px;border-bottom:1px solid #21213a;text-align:left;vertical-align:middle}
 .imgbox{width:56px;height:74px;background:#fff;border-radius:5px;display:flex;align-items:center;justify-content:center}
 .imgbox img{max-width:52px;max-height:70px}
 .ok{color:#22d47a}.bad{color:#f5453d}.mid{color:#e8b73a}.dim{color:#7878a8}
 select,input{background:#21213a;color:#f0f0fa;border:1px solid #333355;border-radius:6px;padding:5px 8px}
 .badge{background:#21213a;border-radius:99px;padding:2px 10px;font-size:12px}
 .pill{border-radius:99px;padding:2px 8px;font-size:11px;background:#21213a}
 .cand{padding:5px 8px;margin:2px 0;border-radius:6px;background:#21213a;cursor:pointer;border:1px solid transparent;display:block}
 .cand:hover{border-color:#6c63ff}
</style>"""

REMATCH_DIALOG = """
<div id="rmdlg" style="display:none;position:fixed;inset:0;background:#000a;z-index:9;align-items:center;justify-content:center">
 <div style="background:#17172a;border-radius:12px;padding:18px;width:640px;max-height:80vh;overflow:auto">
  <p><b>Rematch:</b> <span id="rmname"></span>
     <button class="ghost" style="float:right" onclick="rmClose()">✕</button></p>
  <p>Paste a Cardmarket idProduct or product URL:<br>
     <input id="rmid" size="50" placeholder="e.g. 784949 or https://www.cardmarket.com/…?idProduct=784949"
            onkeydown="if(event.key==='Enter')rmLookup()">
     <button onclick="rmLookup()">Resolve</button></p>
  <p id="rmprev"></p>
  <p>…or search the Cardmarket catalog by name:<br>
     <input id="rmq" size="40" placeholder="type at least 2 chars" oninput="rmSearch()"></p>
  <div id="rmres"></div>
  <p style="margin-top:14px">
     <button class="ghost" onclick="rmSave(-1)">Never match (block the matcher)</button>
     <button class="ghost" onclick="rmSave(null)">Clear (let the matcher decide)</button></p>
 </div>
</div>
<script>
let rmPid=null, rmTimer=null;
function openRematch(pid,name){rmPid=pid;document.getElementById('rmname').textContent=name;
 document.getElementById('rmdlg').style.display='flex';
 document.getElementById('rmid').value='';document.getElementById('rmq').value='';
 document.getElementById('rmprev').innerHTML='';document.getElementById('rmres').innerHTML=''}
function rmClose(){document.getElementById('rmdlg').style.display='none'}
function rmExtract(v){const m=v.match(/idProduct=(\\d+)/);if(m)return m[1];
 return /^\\d+$/.test(v)?v:null}
function rmLookup(){const id=rmExtract(document.getElementById('rmid').value.trim());
 const prev=document.getElementById('rmprev');
 if(!id){prev.innerHTML='<span class="bad">no id found in that</span>';return}
 fetch('/api/cm_lookup?id='+id).then(r=>r.json()).then(j=>{
  if(!j.found){prev.innerHTML='<span class="bad">id '+id+' is not in the Cardmarket catalog</span>';return}
  const warn=j.used_by.length?'<br><span class="mid">already used by: '+j.used_by.join(' · ')+'</span>':'';
  prev.innerHTML='<b>'+j.name+'</b> <span class="dim">['+j.category+'] '+id+'</span> '+
   '<a href="https://www.cardmarket.com/en/Pokemon/Products?idProduct='+id+'" target="_blank">CM↗</a>'+warn+
   '<br><button onclick="rmSave('+id+')">Confirm this match</button>'})}
function rmSearch(){clearTimeout(rmTimer);rmTimer=setTimeout(()=>{
 const q=document.getElementById('rmq').value;if(q.length<2)return;
 fetch('/api/cm_search?q='+encodeURIComponent(q)).then(r=>r.json()).then(rows=>{
  document.getElementById('rmres').innerHTML=rows.map(r=>
   '<div class="cand" onclick="rmSave('+r.id+')"><b>'+r.name+'</b> '+
   '<span class="dim">['+r.category+'] '+r.id+'</span> '+
   '<a href="https://www.cardmarket.com/en/Pokemon/Products?idProduct='+r.id+'" target="_blank" '+
   'onclick="event.stopPropagation()">CM↗</a></div>').join('')||'<span class="dim">nothing found</span>'})},250)}
function rmSave(cm){fetch('/api/rematch',{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify({pid:rmPid,cm:cm})})
 .then(r=>r.json()).then(j=>{rmClose();if(j.ok)setTimeout(()=>location.reload(),400);else alert(JSON.stringify(j))})}
</script>"""


def page(active, body, pending):
    tabs = [("/", "Status"), ("/triage", f"Triage ({pending})"),
            ("/catalog", "Catalog"), ("/divergence", "Divergence"),
            ("/decisions", "Decisions")]
    nav = "".join(f'<a class="nav {"on" if path == active else ""}" href="{path}">{label}</a>'
                  for path, label in tabs)
    return HTMLResponse(f"<!doctype html><meta charset='utf-8'>{STYLE}"
                        f"<header>{nav}<span style='flex:1'></span>"
                        f"<button onclick=\"fetch('/api/run',{{method:'POST'}}).then(r=>r.json())"
                        f".then(j=>alert(JSON.stringify(j)))\">Run now</button></header>"
                        f"{body}{REMATCH_DIALOG}")


def count_pending(state_db):
    return state_db.execute("SELECT COUNT(*) FROM pending_products").fetchone()[0]


# ------------------------------------------------------------ status

@app.get("/", response_class=HTMLResponse)
def status_page():
    db = open_state()
    runs = db.execute("SELECT started_at, finished_at, status, stage, message, products, "
                      "priced, cm_matched, published_version, pending FROM runs "
                      "ORDER BY id DESC LIMIT 30").fetchall()
    pending = count_pending(db)
    last_ok = db.execute("SELECT started_at, published_version FROM runs WHERE status='ok' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    missed = ""
    if last_ok:
        hours_ago = (dt.datetime.now(dt.timezone.utc)
                     - dt.datetime.fromisoformat(last_ok[0])).total_seconds() / 3600
        if hours_ago > 26:
            missed = f"<p class='bad'>MISSED: last successful run was {hours_ago:.0f}h ago</p>"
    rows = "".join(
        f"<tr><td>{r[0][:16]}</td><td class='{'ok' if r[2] == 'ok' else 'bad'}'>{r[2]}</td>"
        f"<td>{r[3] or ''}</td><td>{r[5] or ''}</td><td>{r[6] or ''}</td><td>{r[7] or ''}</td>"
        f"<td>{r[9] if r[9] is not None else ''}</td><td>{r[8] or ''}</td>"
        f"<td class='dim'>{html.escape((r[4] or '')[:110])}</td></tr>" for r in runs)
    body = (f"{missed}<div style='padding:12px 16px'>"
            f"<p>last publish: <b>{last_ok[1] if last_ok else '—'}</b>"
            f" · waiting for review: <b>{pending}</b>"
            f" · schedule: {'daily 03:00 UTC' if SCHEDULE else 'off'}</p>"
            f"<table><tr><th>started</th><th>status</th><th>stage</th><th>products</th>"
            f"<th>priced</th><th>cm</th><th>pending</th><th>version</th><th>message</th></tr>"
            f"{rows}</table></div>")
    return page("/", body, pending)


@app.get("/status.json")
def status_json():
    db = open_state()
    last = db.execute("SELECT started_at, status, published_version, pending FROM runs "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    pending = count_pending(db)
    db.close()
    return JSONResponse({"last_run": last, "pending": pending})


# ------------------------------------------------------------ triage

@app.get("/triage", response_class=HTMLResponse)
def triage_page():
    db = open_state()
    rows = db.execute("SELECT product_id, name, group_name, product_type, image_url, url, "
                      "us_exclusive, heuristic_cm, heuristic_score, candidates "
                      "FROM pending_products ORDER BY group_name, name").fetchall()
    pending = len(rows)
    db.close()
    items = []
    for pid, name, group, ptype, img, url, us_excl, best_cm, best_score, candidates in rows:
        candidates = json.loads(candidates or "[]")
        options = ['<option value="">— no CM match —</option>']
        for score, cm_id, cm_name in candidates:
            selected = " selected" if best_cm and cm_id == best_cm else ""
            options.append(f'<option value="{cm_id}"{selected}>{score:.2f} · {html.escape(cm_name)}</option>')
        cm_links = " ".join(f'<a href="{CM_URL.format(c[1])}" target="_blank">CM{i + 1}↗</a>'
                            for i, c in enumerate(candidates[:5]))
        proposal = (f"<span class='pill ok'>match {best_score:.2f}</span>" if best_cm
                    else "<span class='pill dim'>no match found</span>")
        items.append(f"""<tr data-pid="{pid}">
 <td><div class="imgbox"><img loading="lazy" src="{img or ''}"></div></td>
 <td><b>{html.escape(name)}</b><br><span class="dim">{html.escape(group or '')} · {ptype}
   {'· <span class=mid>US-exclusive</span>' if us_excl else ''}</span><br>
   <a href="{url or '#'}" target="_blank">TCGplayer↗</a> {cm_links}</td>
 <td>{proposal}</td>
 <td><select class="cmsel">{''.join(options)}</select></td>
 <td><select class="action"><option value="keep">Keep</option>
     <option value="drop">Drop</option><option value="skip" selected>— decide later —</option></select>
     <br><button class="ghost" style="font-size:11px;padding:3px 8px"
       onclick="openRematch({pid},{json.dumps(name)})">Rematch…</button></td>
</tr>""")
    body = f"""<div style='padding:12px 16px'>
<p>{pending} products never seen before. Rows you leave alone stay unpublished;
everything already approved keeps updating either way.
<button class="ghost" onclick="approveAll()">Approve all (with proposed matches)</button>
<button onclick="save()">Save decisions</button> <span id="msg"></span></p>
<table><tr><th></th><th>product</th><th>proposal</th><th>CM match</th><th>decision</th></tr>
{''.join(items)}</table>
<p><button onclick="save()">Save decisions</button></p></div>
<script>
function approveAll(){{document.querySelectorAll('.action').forEach(s=>s.value='keep')}}
function save(){{
 const decisions={{}};
 document.querySelectorAll('tr[data-pid]').forEach(tr=>{{
  const action=tr.querySelector('.action').value;
  if(action==='skip')return;
  const cm=tr.querySelector('.cmsel').value;
  decisions[tr.dataset.pid]={{action:action, cm: cm?parseInt(cm):null}};
 }});
 fetch('/api/triage',{{method:'POST',headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify(decisions)}}).then(r=>r.json()).then(j=>{{
   document.getElementById('msg').textContent=JSON.stringify(j);
   if(j.ok) setTimeout(()=>location.reload(), 800);
 }});
}}
</script>"""
    return page("/triage", body, pending)


@app.post("/api/triage")
async def api_triage(request: Request):
    decisions = await request.json()
    if not decisions:
        return {"ok": True, "saved": 0, "note": "nothing selected"}
    db = open_state()
    for pid, choice in decisions.items():
        action = choice.get("action")
        if action not in ("keep", "drop"):
            continue
        cm = choice.get("cm")
        save_decision(db, int(pid), action, cm if action == "keep" else None)
    db.close()
    start_run("triage save")
    return {"ok": True, "saved": len(decisions), "note": "run started to apply the decisions"}


# ------------------------------------------------------------ catalog

@app.get("/catalog", response_class=HTMLResponse)
def catalog_page(set_id: int = 0, ptype: str = "", q: str = "", page_num: int = 1):
    catalog = open_catalog()
    sets = catalog.execute("SELECT group_id, name FROM sealed_sets "
                           "ORDER BY published_on DESC").fetchall()
    types = [r[0] for r in catalog.execute(
        "SELECT DISTINCT product_type FROM sealed_products ORDER BY 1")]
    where, params = [], []
    if set_id:
        where.append("p.group_id=?"); params.append(set_id)
    if ptype:
        where.append("p.product_type=?"); params.append(ptype)
    if q:
        where.append("p.name_lower LIKE ?"); params.append(f"%{q.lower()}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    total = catalog.execute(f"SELECT COUNT(*) FROM sealed_products p {where_sql}",
                            params).fetchone()[0]
    rows = catalog.execute(
        f"""SELECT p.product_id, p.name, s.name, p.product_type, p.image_url, p.url,
            p.cardmarket_id, l.tcgplayer_market, l.cardmarket_trend, p.is_presale, p.us_exclusive
            FROM sealed_products p JOIN sealed_sets s ON s.group_id=p.group_id
            LEFT JOIN sealed_latest_prices l USING(product_id) {where_sql}
            ORDER BY s.published_on DESC, p.name LIMIT ? OFFSET ?""",
        params + [PAGE_SIZE, (page_num - 1) * PAGE_SIZE]).fetchall()
    catalog.close()
    db = open_state()
    pending = count_pending(db)
    db.close()

    set_options = '<option value="0">all sets</option>' + "".join(
        f'<option value="{gid}"{" selected" if gid == set_id else ""}>{html.escape(name)}</option>'
        for gid, name in sets)
    type_options = '<option value="">all types</option>' + "".join(
        f'<option{" selected" if t == ptype else ""}>{t}</option>' for t in types)
    items = "".join(f"""<tr>
 <td><div class="imgbox"><img loading="lazy" src="{img or ''}"></div></td>
 <td><b>{html.escape(name)}</b><br><span class="dim">{html.escape(set_name)} · {ptype_}</span>
   {'<span class="pill mid">presale</span>' if presale else ''}{'<span class="pill mid">US</span>' if us_excl else ''}</td>
 <td>{f'${tp:,.2f}' if tp else '<span class=dim>—</span>'}</td>
 <td>{f'€{cm:,.2f}' if cm else '<span class=dim>—</span>'}</td>
 <td><a href="{url or '#'}" target="_blank">TP↗</a>
     {f'<a href="{CM_URL.format(cm_id)}" target="_blank">CM↗</a>' if cm_id else ''}
     <br><button class="ghost" style="font-size:11px;padding:3px 8px"
       onclick="openRematch({pid},{json.dumps(name)})">Rematch</button></td></tr>"""
                    for pid, name, set_name, ptype_, img, url, cm_id, tp, cm, presale, us_excl in rows)
    total_pages = max(1, -(-total // PAGE_SIZE))
    query = f"set_id={set_id}&ptype={ptype}&q={q}"
    pager = " ".join(
        f'<a href="/catalog?{query}&page_num={i}">{"<b>%d</b>" % i if i == page_num else i}</a>'
        for i in range(max(1, page_num - 5), min(total_pages, page_num + 5) + 1))
    body = f"""<div style='padding:12px 16px'>
<form method="get" action="/catalog" style="display:flex;gap:8px;align-items:center">
 <select name="set_id">{set_options}</select><select name="ptype">{type_options}</select>
 <input name="q" value="{html.escape(q)}" placeholder="search name…">
 <button>Filter</button><span class="badge">{total} products</span></form>
<table><tr><th></th><th>product</th><th>$ market</th><th>€ trend</th><th>links</th></tr>{items}</table>
<p>page {page_num}/{total_pages} · {pager}</p></div>"""
    return page("/catalog", body, pending)


# ------------------------------------------------------------ divergence

@app.get("/divergence", response_class=HTMLResponse)
def divergence_page(min_ratio: float = 3.5):
    catalog = open_catalog()
    rows = catalog.execute(
        """SELECT p.product_id, p.name, s.name, p.image_url, p.url, p.cardmarket_id,
           l.tcgplayer_market, l.cardmarket_trend
           FROM sealed_latest_prices l JOIN sealed_products p USING(product_id)
           JOIN sealed_sets s ON s.group_id=p.group_id
           WHERE l.tcgplayer_market IS NOT NULL AND l.cardmarket_trend IS NOT NULL""").fetchall()
    catalog.close()
    db = open_state()
    acked = {r[0] for r in db.execute("SELECT product_id FROM divergence_ack")}
    pending = count_pending(db)
    db.close()

    flagged = []
    for pid, name, set_name, img, url, cm_id, tp, cm in rows:
        if pid in acked or min(tp, cm) < 5:  # ratios on tiny prices are noise
            continue
        ratio = max(tp / cm, cm / tp)
        if ratio > min_ratio:
            flagged.append((ratio, pid, name, set_name, img, url, cm_id, tp, cm))
    flagged.sort(reverse=True)
    items = "".join(f"""<tr data-pid="{pid}">
 <td><div class="imgbox"><img loading="lazy" src="{img or ''}"></div></td>
 <td><b>{html.escape(name)}</b><br><span class="dim">{html.escape(set_name)}</span><br>
   <a href="{url or '#'}" target="_blank">TP↗</a> <a href="{CM_URL.format(cm_id)}" target="_blank">CM↗</a></td>
 <td class="bad">×{ratio:.1f}</td><td>${tp:,.2f}</td><td>€{cm:,.2f}</td>
 <td><button class="ghost" onclick="act({pid},'ack')">Ack (real gap)</button>
     <button class="ghost" onclick="act({pid},'unmatch')">Unmatch</button>
     <button class="ghost" onclick="openRematch({pid},{json.dumps(name)})">Rematch…</button></td></tr>"""
                    for ratio, pid, name, set_name, img, url, cm_id, tp, cm in flagged)
    body = f"""<div style='padding:12px 16px'>
<form method="get" action="/divergence">flag ratios above ×
 <input name="min_ratio" value="{min_ratio}" size="4"> <button>Apply</button>
 <span class="badge">{len(flagged)} flagged</span>
 <span class="dim">Ack = markets really disagree, stop flagging · Unmatch = the match was wrong</span></form>
<table><tr><th></th><th>product</th><th>ratio</th><th>$</th><th>€</th><th>action</th></tr>{items}</table></div>
<script>
function act(pid, what){{
 fetch('/api/divergence',{{method:'POST',headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify({{pid:pid, what:what}})}}).then(r=>r.json())
  .then(j=>{{ if(j.ok) document.querySelector(`tr[data-pid='${{pid}}']`).remove() }});
}}
</script>"""
    return page("/divergence", body, pending)


@app.post("/api/divergence")
async def api_divergence(request: Request):
    body = await request.json()
    pid, what = int(body["pid"]), body["what"]
    db = open_state()
    if what == "ack":
        with db:
            db.execute("INSERT OR REPLACE INTO divergence_ack VALUES (?,?)", (pid, now_iso()))
    elif what == "unmatch":
        save_decision(db, pid, "keep", -1)
    db.close()
    if what == "unmatch":
        start_run("divergence unmatch")
    return {"ok": True}


# ------------------------------------------------------------ decisions

@app.get("/decisions", response_class=HTMLResponse)
def decisions_page(kind: str = "curated", q: str = ""):
    db = open_state()
    where, params = [], []
    if kind == "drop":
        where.append("d.decision='drop'")
    elif kind == "forced":
        where.append("d.cm_id > 0")
    elif kind == "never":
        where.append("d.cm_id = -1")
    elif kind == "curated":
        # everything a human actually decided, hiding the bootstrap noise
        where.append("(d.decision='drop' OR d.cm_id IS NOT NULL)")
    if q:
        where.append("d.name LIKE ?"); params.append(f"%{q}%")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db.execute(
        f"""SELECT d.product_id, d.decision, d.cm_id, d.decided_at, d.name, d.group_name,
            c.name FROM product_decisions d
            LEFT JOIN cm_catalog c ON c.id_product = d.cm_id
            {where_sql} ORDER BY d.decided_at DESC LIMIT 500""", params).fetchall()
    total = db.execute(f"SELECT COUNT(*) FROM product_decisions d {where_sql}",
                       params).fetchone()[0]
    pending = count_pending(db)
    db.close()

    def filter_link(k, label):
        style = ' style="background:#6c63ff"' if k == kind else ""
        return f'<a class="nav"{style} href="/decisions?kind={k}&q={q}">{label}</a>'

    items = []
    for pid, decision, cm_id, when, name, group, cm_name in rows:
        if decision == "drop":
            what = '<span class="bad">DROPPED</span>'
        elif cm_id == -1:
            what = '<span class="mid">never match</span>'
        elif cm_id and cm_id > 0:
            what = (f'<span class="ok">→ {html.escape(cm_name or "?")}</span> '
                    f'<a href="{CM_URL.format(cm_id)}" target="_blank">CM↗</a>')
        else:
            what = '<span class="dim">keep (matcher decides)</span>'
        items.append(f"""<tr>
 <td><b>{html.escape(name or f'#{pid}')}</b><br>
     <span class="dim">{html.escape(group or '')} · {pid} · {when[:16]}</span></td>
 <td>{what}</td>
 <td><button class="ghost" onclick="undo({pid},'{decision}')">Undo → triage</button>
     <button class="ghost" onclick="openRematch({pid},{json.dumps(name or str(pid))})">Rematch…</button></td>
</tr>""")
    body = f"""<div style='padding:12px 16px'>
<p>{filter_link('curated','Curated')} {filter_link('drop','Drops')} {filter_link('forced','Forced matches')}
   {filter_link('never','Never-match')} {filter_link('all','All')}
   <form method="get" action="/decisions" style="display:inline">
    <input type="hidden" name="kind" value="{kind}">
    <input name="q" value="{html.escape(q)}" placeholder="search name…"><button>Search</button></form>
   <span class="badge">{total} decisions</span>
   <a href="/api/decisions.yaml" download="decisions_backup.yaml"><button class="ghost">Export YAML backup</button></a></p>
<p class="dim">Undo removes the decision - the product goes back to Triage on the next run
(and stops being published, if it was a keep).</p>
<table><tr><th>product</th><th>decision</th><th>actions</th></tr>{''.join(items)}</table>
{'<p class="dim">showing the first 500 - use search to narrow down</p>' if total > 500 else ''}</div>
<script>
function undo(pid, decision){{
 const warning = decision==='keep' ? 'This product will be unpublished and go back to Triage. Continue?'
                                   : 'This product will go back to Triage on the next run. Continue?';
 if(!confirm(warning)) return;
 fetch('/api/undo_decision',{{method:'POST',headers:{{'Content-Type':'application/json'}},
  body:JSON.stringify({{pid:pid}})}}).then(r=>r.json()).then(j=>{{if(j.ok)location.reload()}});
}}
</script>"""
    return page("/decisions", body, pending)


@app.post("/api/undo_decision")
async def api_undo(request: Request):
    body = await request.json()
    pid = int(body["pid"])
    db = open_state()
    with db:
        db.execute("DELETE FROM product_decisions WHERE product_id=?", (pid,))
    db.close()
    start_run("undo decision")
    return {"ok": True}


@app.get("/api/decisions.yaml")
def api_decisions_yaml():
    """Everything decided in the console, as yaml. The decisions only live in
    collector_state.db, so keep a copy of this somewhere safe."""
    db = open_state()
    rows = db.execute("SELECT product_id, decision, cm_id, name FROM product_decisions "
                      "ORDER BY product_id").fetchall()
    db.close()
    lines = ["# decisions backup, generated by the collector console",
             f"# {now_iso()}",
             "# to restore: copy collector_state.db back, or merge matches/never",
             "# into cm_overrides.yaml (drops have to be re-dropped in triage)", "",
             "matches:"]
    lines += [f"  {pid}: {cm_id}  # {name}" for pid, d, cm_id, name in rows if cm_id and cm_id > 0]
    lines.append("\nnever:")
    lines += [f"  - {pid}  # {name}" for pid, d, cm_id, name in rows if cm_id == -1]
    lines.append("\ndrops:")
    lines += [f"  - {pid}  # {name}" for pid, d, cm_id, name in rows if d == "drop"]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/yaml")


# ------------------------------------------------------------ rematcher api

@app.get("/api/cm_lookup")
def api_cm_lookup(id: int):
    db = open_state()
    row = db.execute("SELECT name, category FROM cm_catalog WHERE id_product=?", (id,)).fetchone()
    db.close()
    if not row:
        return {"found": False}
    used_by = []
    try:
        catalog = open_catalog()
        used_by = [r[0] for r in catalog.execute(
            "SELECT name FROM sealed_products WHERE cardmarket_id=?", (id,))]
        catalog.close()
    except sqlite3.Error:
        pass
    return {"found": True, "name": row[0], "category": row[1], "used_by": used_by}


@app.get("/api/cm_search")
def api_cm_search(q: str):
    db = open_state()
    rows = db.execute("SELECT id_product, name, category FROM cm_catalog "
                      "WHERE name LIKE ? ORDER BY name LIMIT 20", (f"%{q}%",)).fetchall()
    db.close()
    return [{"id": r[0], "name": r[1], "category": r[2]} for r in rows]


@app.post("/api/rematch")
async def api_rematch(request: Request):
    body = await request.json()
    pid, cm = int(body["pid"]), body.get("cm")  # cm: id, -1 = never, null = matcher decides
    if cm is not None:
        cm = int(cm)
        if cm > 0:
            db = open_state()
            known = db.execute("SELECT 1 FROM cm_catalog WHERE id_product=?", (cm,)).fetchone()
            db.close()
            if not known:
                return {"ok": False, "error": f"id {cm} is not in the Cardmarket catalog"}
    db = open_state()
    # rematching a pending product approves it at the same time
    save_decision(db, pid, "keep", cm)
    db.close()
    start_run("rematch")
    return {"ok": True, "note": "run started to apply the rematch"}


# ------------------------------------------------------------ scheduler

@app.post("/api/run")
def api_run():
    if run_lock.locked():
        return {"ok": False, "error": "a run is already in progress"}
    start_run("manual")
    return {"ok": True, "note": "run started"}


def catch_up_if_needed():
    """After downtime (power loss etc.) run right away instead of waiting
    for the next 03:00."""
    db = open_state()
    last = db.execute("SELECT started_at FROM runs WHERE status='ok' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    if not last:
        start_run("first ever run")
        return
    hours_ago = (dt.datetime.now(dt.timezone.utc)
                 - dt.datetime.fromisoformat(last[0])).total_seconds() / 3600
    if hours_ago > CATCHUP_AFTER_HOURS:
        start_run(f"catch-up, last success {hours_ago:.0f}h ago")


if __name__ == "__main__":
    if SCHEDULE:
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(lambda: run_pipeline("scheduled"),
                          CronTrigger(hour=3, minute=0, timezone="UTC"),
                          misfire_grace_time=3600, coalesce=True)
        scheduler.start()
        catch_up_if_needed()
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
