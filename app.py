"""IT-bot admin bot (Telegram glue). Logic lives in core.py."""
import asyncio, functools, io, json, logging, os, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)
import core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("admin")

CFG = dict(live_repo=os.environ.get("LIVE_REPO", "nadeembrijawy/IT-bot"), branch=os.environ.get("LIVE_BRANCH", "main"),
           data_repo=os.environ.get("DATA_REPO", ""))
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
CAP = int(os.environ.get("BUNDLE_CAP", "120000"))
PROMPT = open(os.path.join(core.HERE, "PROMPT.txt"), encoding="utf-8").read()
gh = core.GH(os.environ.get("GITHUB_TOKEN", "x"))
store = core.Store(os.path.join(tempfile.gettempdir(), "admin.db"))
live = core.Live(CFG)
SYNC = {"sha": None}
ACT_LOCK = asyncio.Lock()
tt = asyncio.to_thread


def admin_only(fn):
    @functools.wraps(fn)
    async def w(update: Update, ctx):
        u = update.effective_user
        if not u or u.id not in ADMIN_IDS:
            if update.effective_message:
                await update.effective_message.reply_text(f"Not allowed. Your id: {u.id if u else '?'}")
            return
        return await fn(update, ctx)
    return w


# ---------- persistence of admin.db in the private data repo ----------
def sync_load():
    data, sha = gh.get(CFG["data_repo"], "admin.db")
    SYNC["sha"] = sha
    if data: store.restore(data)

def sync_save():
    data = store.snapshot()
    for _ in range(2):
        try:
            SYNC["sha"] = gh.put(CFG["data_repo"], "admin.db", data, "admin.db autosave", sha=SYNC["sha"]); return
        except core.Conflict:
            _, SYNC["sha"] = gh.get(CFG["data_repo"], "admin.db")

async def save_job(ctx):
    try: await tt(sync_save); ctx.bot_data.pop("save_err", None)
    except Exception as e:
        log.error("autosave failed: %s", e)
        if not ctx.bot_data.get("save_err"):
            ctx.bot_data["save_err"] = True
            for i in ADMIN_IDS:
                try: await ctx.bot.send_message(i, f"⚠️ Autosave to the data repo failed: {str(e)[:200]}\nRun /health")
                except Exception: pass

def dirty(ctx):
    jq = ctx.application.job_queue
    for j in jq.get_jobs_by_name("save"): j.schedule_removal()
    jq.run_once(save_job, 8, name="save")

async def flush_save(): await tt(sync_save)


# ---------- helpers ----------
def cur_subject():
    sid = store.kv_get("subject_id")
    return (int(sid), live.subject(sid)) if sid and live.subs else (None, None)

def sname(s): return f"{s['name']} (Y{s['year']} S{s['semester']})"

async def need_subject(m):
    sid, s = cur_subject()
    if not sid: await m.reply_text("Choose a subject first: /subject"); return None, None
    return sid, s

async def live_ready(): await tt(live.ensure, gh)


# ---------- basic commands ----------
async def start(update, ctx):
    u = update.effective_user
    if u.id not in ADMIN_IDS:
        await update.message.reply_text(f"Your Telegram id: {u.id}\nPut it in ADMIN_IDS, then redeploy."); return
    await update.message.reply_text(
        "Admin bot ready.\n/subject choose subject\nForward polls → saved hidden\nSend lecture PDF (caption: L5 [ocr])\n"
        "/lectures  /drop L5\n/bundle [n] [L5 L6] → file for Claude\nSend patch.json → import\n/review  /publish  /undo\n"
        "Send bot.py → update script, /rollback_script\n/stats  /health")

@admin_only
async def subject_cmd(update, ctx):
    await live_ready()
    years = sorted({s[1] for s in live.subs})
    await update.message.reply_text("Year?", reply_markup=M([[B(f"Year {y}", callback_data=f"sy:{y}") for y in years]]))

@admin_only
async def stats_cmd(update, ctx):
    rows = store.q("SELECT subject_id,status,count(*) c FROM inbox GROUP BY 1,2 ORDER BY 1")
    if not rows: await update.message.reply_text("Inbox empty."); return
    await live_ready(); by = {}
    for r in rows: by.setdefault(r["subject_id"], []).append(f"{r['status']} {r['c']}")
    out = [f"{(live.subject(k) or {'name': k})['name']}: " + ", ".join(v) for k, v in by.items()]
    nsrc = store.one("SELECT count(*) c FROM sources")["c"]
    await update.message.reply_text("\n".join(out) + f"\nSources stored: {nsrc}")


# ---------- polls ----------
@admin_only
async def on_poll(update, ctx):
    m = update.effective_message; p = m.poll
    await live_ready()
    sid, s = await need_subject(m)
    if not sid: return
    res, key, info = core.add_poll(store, live, sid, p.question, [o.text for o in p.options],
                                   getattr(p, "correct_option_id", None), getattr(p, "explanation", None))
    pend = ctx.bot_data.setdefault("pend", {"new": [], "matched": [], "dup": 0, "pc": 0})
    if res == "dup": pend["dup"] += 1
    else: pend[res].append(key)
    if getattr(p, "correct_option_id", None) is not None: pend["pc"] += 1
    jq = ctx.application.job_queue
    for j in jq.get_jobs_by_name("pend"): j.schedule_removal()
    jq.run_once(flush_pend, 3, name="pend", chat_id=m.chat_id)
    dirty(ctx)

async def flush_pend(ctx):
    pend = ctx.bot_data.pop("pend", None)
    if not pend: return
    _, s = cur_subject()
    txt = (f"Saved hidden: {len(pend['new'])} new + {len(pend['matched'])} matched live | duplicates skipped: {pend['dup']}"
           f"\nSubject: {sname(s) if s else '?'}\npolls carrying an answer: {pend['pc']}")
    await ctx.bot.send_message(ctx.job.chat_id, txt)


# ---------- documents ----------
@admin_only
async def on_doc(update, ctx):
    m = update.effective_message; d = m.document; name = (d.file_name or "").lower()
    if name.endswith(".pdf"): return await lecture_in(update, ctx)
    if name.endswith(".json") or name.endswith(".txt"):
        f = await d.get_file(); data = await f.download_as_bytearray()
        return await patch_in(update, ctx, bytes(data).decode("utf-8", "ignore"))
    if name.endswith(".py"): return await script_in(update, ctx)
    await m.reply_text("Send PDF (lecture), .json (patch) or .py (bot script). PowerPoint: export to PDF first.")

async def lecture_in(update, ctx):
    m = update.effective_message; d = m.document
    sid, s = await need_subject(m)
    if not sid: return
    cap = (m.caption or "").split(); ocr = "ocr" in [c.lower() for c in cap]
    lid = next((c for c in cap if c.lower() != "ocr"), None)
    if not lid:
        n = len(store.q("SELECT 1 FROM sources WHERE subject_id=?", (sid,))) + 1; lid = f"L{n}"
    lid = "".join(ch for ch in lid if ch.isalnum() or ch in "_-")[:12]
    await m.reply_text(f"Converting {lid}{' with OCR' if ocr else ''}…")
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "in.pdf")
        f = await d.get_file(); await f.download_to_drive(path)
        try: text, info = await tt(core.convert_pdf, path, lid, ocr)
        except Exception as e: await m.reply_text(f"Conversion failed: {e}"); return
    core.save_source(store, sid, lid, text, info, (d.file_name or "")[:60])
    msg = [f"✅ {lid} saved for {sname(s)}: {info['pages']} pages, ≈{core.est_tokens(text)} tokens"]
    if info["image_pages"]: msg.append(f"Picture/low-text pages (content may be missing): {info['image_pages']}")
    if info["arabic_pages"]: msg.append(f"Arabic pages: check text is not scrambled: {info['arabic_pages']}")
    await m.reply_text("\n".join(msg)); dirty(ctx)

@admin_only
async def lectures_cmd(update, ctx):
    sid, s = await need_subject(update.message)
    if not sid: return
    rows = store.q("SELECT sid,tokens,pages,flags FROM sources WHERE subject_id=? ORDER BY sid", (sid,))
    tot = sum(r["tokens"] for r in rows)
    await update.message.reply_text(f"{sname(s)}\n" + ("\n".join(f"{r['sid']}: {r['pages']}p ≈{r['tokens']}t" for r in rows) or "no sources")
                                    + f"\nTotal ≈{tot} tokens (cap {CAP})")

@admin_only
async def drop_cmd(update, ctx):
    sid, s = await need_subject(update.message)
    if not sid or not ctx.args: return
    n = store.x("DELETE FROM sources WHERE subject_id=? AND sid=?", (sid, ctx.args[0])).rowcount
    await update.message.reply_text("Deleted." if n else "Not found."); dirty(ctx)


# ---------- bundle ----------
@admin_only
async def bundle_cmd(update, ctx):
    m = update.message; await live_ready()
    sid, s = await need_subject(m)
    if not sid: return
    limit = next((int(a) for a in ctx.args if a.isdigit()), 40)
    ids = tuple(a for a in ctx.args if not a.isdigit() and a.lower() != "all")
    res = core.build_bundle(store, live, sid, PROMPT, limit, ids, include_bundled="all" in [a.lower() for a in ctx.args])
    if not res: await m.reply_text("No unbundled hidden questions for this subject (use /bundle all to resend)."); return
    text, rows, tok, ns = res
    if tok > CAP:
        await m.reply_text(f"Too big: ≈{tok} tokens > cap {CAP}. Use fewer questions (/bundle 20) or pick lectures (/bundle L2 L4)."); return
    b = store.seq("batch")
    for r in rows: store.x("UPDATE inbox SET batch=? WHERE key=?", (b, r["key"]))
    fn = f"bundle_{s['name'].replace(' ', '_')}_{b}.txt"
    await m.reply_document(io.BytesIO(text.encode("utf-8")), filename=fn,
                           caption=f"{len(rows)} questions, {ns} sources, ≈{tok} tokens.\nUpload to Claude with: solve the attached")
    dirty(ctx)


# ---------- patch import + review ----------
async def patch_in(update, ctx, text):
    m = update.effective_message
    try: rep = core.import_patch(store, text)
    except Exception as e: await m.reply_text(f"Could not read patch: {e}"); return
    out = [f"Imported for review: {len(rep['ok'])} | TODO: {len(rep['todo'])} | rejected: {len(rep['bad'])}"]
    out += [f"✗ {k}: {w[:110]}" for k, w in rep["bad"][:12]]
    if len(rep["bad"]) > 12: out.append(f"… +{len(rep['bad']) - 12} more")
    if rep["ok"]: out.append("Next: /review")
    await m.reply_text("\n".join(out)); dirty(ctx)

@admin_only
async def on_text(update, ctx):
    t = (update.message.text or "").lstrip()
    if t[:1] in "[{": await patch_in(update, ctx, t)

def review_msg(r):
    opts = json.loads(r["options"]); fl = json.loads(r["flags"] or "[]")
    lv = core.approval(r["source"], r["confidence"])
    left = store.one("SELECT count(*) c FROM inbox WHERE status='imported'")["c"]
    lines = [f"{left} left | {r['key']}", f"Q: {r['question'][:700]}"]
    lines += [("✅ " if i == r["correct_index"] else "▫️ ") + f"{i}) {o[:120]}" for i, o in enumerate(opts)]
    lines += [f"Why: {r['explanation']}", f"Src: {r['source']}/{r['confidence']} — {r['citation'] or '-'}"[:400]]
    if fl: lines.append("⚠️ CONFLICT: " + ", ".join(fl))
    if r["note"]: lines.append(f"Note: {r['note'][:150]}")
    if lv == "no": lines.append("Cannot approve (low evidence). Hold or Exclude.")
    row = []
    if lv != "no": row.append(B("⚠️ Overwrite" if fl and any('live' in f for f in fl) else ("✅ Approve" if lv == "ok" else "✅ Approve (check)"), callback_data=f"rv:a:{r['key']}"))
    row += [B("⏸ Hold", callback_data=f"rv:h:{r['key']}"), B("🚫 Exclude", callback_data=f"rv:x:{r['key']}")]
    return "\n".join(lines)[:3900], M([row, [B("⏭ Skip", callback_data=f"rv:s:{r['key']}")]])

async def send_next(chat, ctx, skip=()):
    ph = ",".join("?" * len(skip)) if skip else "''"
    r = store.one(f"SELECT * FROM inbox WHERE status='imported' AND key NOT IN ({ph}) ORDER BY subject_id,key", tuple(skip))
    if not r:
        c = {s: n for s, n in [(x["status"], x["c"]) for x in store.q("SELECT status,count(*) c FROM inbox GROUP BY 1")]}
        await ctx.bot.send_message(chat, f"Review done. approved {c.get('approved', 0)}, held {c.get('held', 0)}, excluded {c.get('excluded', 0)}. /publish when ready.")
        return
    t, kb = review_msg(r); await ctx.bot.send_message(chat, t, reply_markup=kb)

@admin_only
async def review_cmd(update, ctx):
    ctx.bot_data["skipped"] = []; await send_next(update.effective_chat.id, ctx)


# ---------- publish / undo / script ----------
def act_summary(a):
    if a["type"] == "publish":
        r = a["rep"]; s = [f"Publish → live bot.db\nnew {len(r['new'])} | updated {len(r['upd'])} | excluded {len(r['excl'])} | dropped {len(r['drop'])} | skipped {len(r['skip'])}"]
        s += [f"skip {k}: {w}" for k, w in r["skip"][:8]]
        s.append("Each commit restarts the live bot (users lose position). Confirm?"); return "\n".join(s)
    s = [f"{a['label']}: {a['target']}"]
    if a.get("undo"): s.append("Restores the WHOLE backup file (any later changes to it are lost).")
    if a["target"].endswith(".py"): s.append(f"lines +{a['add']} / -{a['rem']}")
    s += [f"⚠️ {w}" for w in a.get("warns", [])] + [f"✗ {e}" for e in a.get("errs", [])]
    s.append("Confirm?" if not a.get("errs") else "Blocked."); return "\n".join(s)

async def propose(m, ctx, act):
    ctx.bot_data["act"] = act
    kb = M([[B("✅ Confirm", callback_data="ac:ok"), B("✖ Cancel", callback_data="ac:no")]]) if not act.get("errs") else None
    await m.reply_text(act_summary(act), reply_markup=kb)

@admin_only
async def publish_cmd(update, ctx):
    m = update.message
    if ACT_LOCK.locked(): await m.reply_text("Another action is running."); return
    try: await flush_save()
    except Exception as e: await m.reply_text(f"⚠️ Could not save admin.db to the data repo ({str(e)[:150]}). Run /health. Continuing with publish.")
    try: act = await tt(core.prepare_publish, gh, store, CFG)
    except Exception as e: await m.reply_text(f"Prepare failed: {e}"); return
    if not act: await m.reply_text("Nothing approved yet. /review first."); return
    await propose(m, ctx, act)

@admin_only
async def undo_cmd(update, ctx):
    b = store.kv_get("last_db_backup")
    if not b: await update.message.reply_text("No backup recorded."); return
    old, _ = await tt(gh.get, CFG["data_repo"], b)
    cur, sha = await tt(gh.get, CFG["live_repo"], "bot.db", CFG["branch"])
    act = dict(type="file", target="bot.db", sha=sha, old=cur, new=old, errs=[], warns=[], add=0, rem=0, label=f"restore {b}", undo=True)
    await propose(update.message, ctx, act)

async def script_in(update, ctx):
    m = update.effective_message; f = await m.document.get_file(); data = bytes(await f.download_as_bytearray())
    act = await tt(core.prepare_file_update, gh, CFG, "bot.py", data, "update bot.py")
    await propose(m, ctx, act)

@admin_only
async def rollback_script_cmd(update, ctx):
    b = store.kv_get("last_script_backup")
    if not b: await update.message.reply_text("No script backup recorded."); return
    old, _ = await tt(gh.get, CFG["data_repo"], b)
    act = await tt(core.prepare_file_update, gh, CFG, "bot.py", old, f"rollback to {b}")
    await propose(update.message, ctx, act)

async def run_act(q, ctx):
    act = ctx.bot_data.pop("act", None)
    if not act: await q.edit_message_text("Nothing pending."); return
    async with ACT_LOCK:
        try:
            if act["type"] == "publish":
                cur_sha = (await tt(gh.get, CFG["live_repo"], "bot.db", CFG["branch"]))[1]
                if cur_sha != act["sha"]:
                    act = await tt(core.prepare_publish, gh, store, CFG)
                    await q.edit_message_text("bot.db changed on GitHub meanwhile. Re-prepared:"); await propose(q.message, ctx, act); return
                b = await tt(core.execute_publish, gh, store, CFG, act)
                await q.edit_message_text(f"✅ Published and verified. Backup: {b}\nLive bot will redeploy.")
            else:
                b = await tt(core.execute_file_update, gh, store, CFG, act)
                if act.get("undo"):
                    store.x("UPDATE inbox SET status='approved' WHERE status='published'")
                await q.edit_message_text(f"✅ Done and verified. Backup: {b}")
            dirty(ctx)
            live.t = 0
        except core.Stale: await q.edit_message_text("File changed on GitHub; run the command again.")
        except core.Conflict: await q.edit_message_text("GitHub rejected the commit (file changed). Run the command again.")
        except Exception as e: await q.edit_message_text(f"Failed: {e}")


# ---------- callbacks ----------
async def on_cb(update, ctx):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS: await q.answer("Not allowed"); return
    await q.answer(); d = q.data
    if d.startswith("sy:"):
        y = int(d[3:]); subs = [s for s in live.subs if s[1] == y]
        kb = [[B(f"S{s[2]} {s[3]}" + (f" [{s[4]}]" if s[4] and s[4] != "shared" else ""), callback_data=f"ss:{s[0]}")] for s in subs]
        await q.edit_message_text(f"Year {y}: subject?", reply_markup=M(kb))
    elif d.startswith("ss:"):
        store.kv_set("subject_id", d[3:]); s = live.subject(d[3:])
        await q.edit_message_text(f"Subject set: {sname(s)}\nNow forward polls / lecture PDFs."); dirty(ctx)
    elif d.startswith("rv:"):
        _, a, key = d.split(":", 2)
        st = {"a": "approved", "h": "held", "x": "excluded"}.get(a)
        r = store.one("SELECT * FROM inbox WHERE key=?", (key,))
        if st and r and r["status"] == "imported":
            if st == "approved" and core.approval(r["source"], r["confidence"]) == "no": await q.answer("Not approvable"); return
            store.x("UPDATE inbox SET status=?,updated=? WHERE key=?", (st, __import__("time").time(), key))
        if a == "s": ctx.bot_data.setdefault("skipped", []).append(key)
        await q.edit_message_reply_markup(None)
        await send_next(q.message.chat_id, ctx, ctx.bot_data.get("skipped", [])); dirty(ctx)
    elif d == "ac:ok": await run_act(q, ctx)
    elif d == "ac:no": ctx.bot_data.pop("act", None); await q.edit_message_text("Cancelled.")


# ---------- errors must never be silent ----------
async def on_error(update, ctx):
    log.error("handler error", exc_info=ctx.error)
    try:
        if update is not None and getattr(update, "effective_message", None):
            await update.effective_message.reply_text(f"⚠️ Error: {type(ctx.error).__name__}: {str(ctx.error)[:300]}\nTry /health")
    except Exception: pass

@admin_only
async def health_cmd(update, ctx):
    await update.message.reply_text("\n".join(await tt(core.health, gh, CFG)))


# ---------- web server (Render needs an open port) ----------
class H(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
    def log_message(self, *a): pass

async def post_init(app):
    if not CFG["data_repo"]: raise SystemExit("DATA_REPO not set")
    await tt(sync_load); log.info("admin.db loaded")

def main():
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "10000"))), H).serve_forever(), daemon=True).start()
    app = Application.builder().token(os.environ["BOT_TOKEN"]).post_init(post_init).build()
    for n, f in [("start", start), ("subject", subject_cmd), ("stats", stats_cmd), ("lectures", lectures_cmd), ("drop", drop_cmd),
                 ("bundle", bundle_cmd), ("review", review_cmd), ("publish", publish_cmd), ("undo", undo_cmd),
                 ("rollback_script", rollback_script_cmd), ("health", health_cmd)]:
        app.add_handler(CommandHandler(n, f))
    app.add_handler(MessageHandler(filters.POLL, on_poll))
    app.add_handler(MessageHandler(filters.Document.ALL, on_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_error_handler(on_error)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
