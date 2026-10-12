"""Admin-bot core logic (no Telegram code): storage, patch validation, bundle, safe publish."""
import base64, difflib, hashlib, json, os, re, sqlite3, subprocess, sys, tempfile, threading, time, unicodedata, uuid
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
SOURCES_OK = ("lecture-text", "exam-bank")
ALL_SOURCES = SOURCES_OK + ("inferred-from-concept",)


class Conflict(Exception): ...      # GitHub says the file changed under us
class Stale(Exception): ...         # live file changed after we prepared


# ---------- GitHub contents API ----------
class GH:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28"})

    def get(self, repo, path, branch=None):
        url = f"{API}/repos/{repo}/contents/{path}"
        prm = {"ref": branch} if branch else None
        r = self.s.get(url, params=prm, timeout=60)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        j = r.json()
        if isinstance(j, list):
            raise ValueError(f"{path} is a folder")
        if j.get("encoding") == "base64" and j.get("content"):
            data = base64.b64decode(j["content"])
        else:
            rr = self.s.get(url, params=prm, headers={"Accept": "application/vnd.github.raw"}, timeout=120)
            rr.raise_for_status()
            data = rr.content
        return data, j["sha"]

    def put(self, repo, path, data, message, sha=None, branch=None):
        body = {"message": message, "content": base64.b64encode(data).decode()}
        if sha: body["sha"] = sha
        if branch: body["branch"] = branch
        r = self.s.put(f"{API}/repos/{repo}/contents/{path}", json=body, timeout=180)
        if r.status_code in (409, 422):
            raise Conflict(r.text[:200])
        r.raise_for_status()
        return r.json()["content"]["sha"]


# ---------- text helpers ----------
_AR = str.maketrans({'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ى': 'ي', 'ة': 'ه', 'ی': 'ي', 'ک': 'ك', 'ھ': 'ه'})

def norm(s, strip_num=True):
    s = unicodedata.normalize('NFKC', s or '')
    s = re.sub('[\u064b-\u065f\u0670\u0640\u200e\u200f\u202a-\u202e]', '', s).translate(_AR).lower()
    if strip_num:
        s = re.sub(r'^\s*\d+\s*[.)\-:]\s*', '', s)
    return re.sub(r'\s+', ' ', s).strip()

def fingerprint(q, opts):
    return hashlib.sha1((norm(q) + '|' + '|'.join(norm(o, False) for o in opts)).encode()).hexdigest()

def est_tokens(text):
    ar = sum('\u0600' <= c <= '\u06ff' for c in text)
    return int(ar / 2.5 + (len(text) - ar) / 3.6)


# ---------- admin.db ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox(key TEXT PRIMARY KEY, fp TEXT UNIQUE, subject_id INT, target TEXT, live_key TEXT,
  question TEXT, options TEXT, tg_correct INT, tg_expl TEXT, db_correct INT, db_excluded INT,
  status TEXT DEFAULT 'hidden', correct_index INT, explanation TEXT, source TEXT, confidence TEXT,
  citation TEXT, note TEXT, flags TEXT, batch INT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS sources(subject_id INT, sid TEXT, title TEXT, text TEXT, tokens INT, pages INT,
  flags TEXT, created REAL, PRIMARY KEY(subject_id, sid));
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS log(ts REAL, action TEXT, detail TEXT);
"""

class Store:
    def __init__(self, path):
        self.path, self.lock = path, threading.RLock()
        self._open()

    def _open(self):
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA); self.con.commit()

    def q(self, sql, a=()):
        with self.lock: return self.con.execute(sql, a).fetchall()

    def one(self, sql, a=()):
        with self.lock: return self.con.execute(sql, a).fetchone()

    def x(self, sql, a=()):
        with self.lock:
            cur = self.con.execute(sql, a); self.con.commit(); return cur

    def kv_get(self, k, d=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,)); return r['v'] if r else d

    def kv_set(self, k, v):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def seq(self, name):
        n = int(self.kv_get(name, 0)) + 1; self.kv_set(name, n); return n

    def log(self, a, d=''):
        self.x("INSERT INTO log VALUES(?,?,?)", (time.time(), a, str(d)[:500]))

    def snapshot(self):
        with self.lock:
            tmp = tempfile.mktemp(suffix='.db'); dst = sqlite3.connect(tmp)
            self.con.backup(dst); dst.close()
            b = open(tmp, 'rb').read(); os.remove(tmp); return b

    def restore(self, data):
        with self.lock:
            self.con.close(); open(self.path, 'wb').write(data); self._open()


# ---------- live bot.db cache ----------
class Live:
    def __init__(self, cfg):
        self.cfg = cfg; self.t = 0; self.sha = None
        self.path = os.path.join(tempfile.gettempdir(), 'live_cache.db')

    def ensure(self, gh, max_age=300, force=False):
        if force or not os.path.exists(self.path) or time.time() - self.t > max_age:
            data, sha = gh.get(self.cfg['live_repo'], 'bot.db', self.cfg['branch'])
            if data is None: raise RuntimeError('bot.db not found in live repo')
            open(self.path, 'wb').write(data); self.sha, self.t = sha, time.time()
            self._index()

    def _index(self):
        con = sqlite3.connect(self.path)
        rows = con.execute("SELECT key,subject_id,question,options,correct_index,excluded FROM questions").fetchall()
        self.subs = con.execute("SELECT id,year,semester,name,track FROM subjects ORDER BY year,semester,id").fetchall()
        con.close()
        self.keys, self.fp, self.byq = set(), {}, {}
        for key, sid, q, o, ci, ex in rows:
            self.keys.add(key)
            try: opts = json.loads(o)
            except Exception: continue
            self.fp[fingerprint(q, opts)] = dict(key=key, ci=ci, ex=ex, sid=sid)
            self.byq.setdefault(norm(q), []).append(key)

    def subject(self, sid):
        for s in self.subs:
            if s[0] == int(sid): return dict(id=s[0], year=s[1], semester=s[2], name=s[3], track=s[4])
        return None

    def match(self, q, opts): return self.fp.get(fingerprint(q, opts))
    def similar(self, q): return self.byq.get(norm(q), [])


# ---------- add polls ----------
def add_poll(store, live, sid, q, opts, tg_correct=None, tg_expl=None):
    f = fingerprint(q, opts)
    ex = store.one("SELECT key FROM inbox WHERE fp=?", (f,))
    if ex: return 'dup', ex['key'], None
    m = live.match(q, opts); now = time.time()
    if m:
        key, target, sid = m['key'], 'update', m['sid']
        if store.one("SELECT 1 FROM inbox WHERE key=?", (key,)): return 'dup', key, None
        extra = None
    else:
        sim = live.similar(q)
        year = live.subject(sid)['year']
        while True:
            key = f"y{year}:a{store.seq('seq_a')}"
            if key not in live.keys and not store.one("SELECT 1 FROM inbox WHERE key=?", (key,)): break
        target, extra = 'insert', (sim[0] if sim else None)
    store.x("INSERT INTO inbox(key,fp,subject_id,target,live_key,question,options,tg_correct,tg_expl,db_correct,"
            "db_excluded,note,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, f, int(sid), target, m['key'] if m else None, q, json.dumps(opts, ensure_ascii=False),
             tg_correct, tg_expl, m['ci'] if m else None, m['ex'] if m else None,
             f"similar live question {extra} (options differ)" if extra else None, now, now))
    return ('matched' if m else 'new'), key, m


# ---------- sources (lectures / exams) ----------
def convert_pdf(pdf, lid, ocr=False):
    cmd = [sys.executable, os.path.join(HERE, 'convert_lectures.py'), pdf, lid] + (['--ocr'] if ocr else [])
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if p.returncode != 0: raise RuntimeError(p.stderr[-300:])
    stats = next((l for l in p.stderr.splitlines() if 'pages=' in l), '')
    def lst(name):
        m = re.search(name + r'=\[(.*?)\]', stats)
        return [int(x) for x in re.findall(r'\d+', m.group(1))] if m else []
    pg = re.search(r'pages=(\d+)', stats)
    return p.stdout, dict(pages=int(pg.group(1)) if pg else 0,
                          image_pages=lst('image_or_low_text_pages'), arabic_pages=lst('pages_with_arabic_check_text_layer'))

def save_source(store, subject_id, sid, text, info, title=''):
    store.x("INSERT INTO sources VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(subject_id,sid) DO UPDATE SET "
            "title=excluded.title,text=excluded.text,tokens=excluded.tokens,pages=excluded.pages,"
            "flags=excluded.flags,created=excluded.created",
            (subject_id, sid, title, text, est_tokens(text), info.get('pages', 0), json.dumps(info), time.time()))

def get_slide(store, subject_id, sid, n):
    r = store.one("SELECT text FROM sources WHERE subject_id=? AND sid=?", (subject_id, sid))
    if not r: return None
    m = re.search(rf'(?ms)^§{re.escape(sid)}\.{int(n)}\n(.*?)(?=^§|\Z)', r['text'])
    return m.group(1) if m else None


# ---------- patch validation ----------
CIT_RE = re.compile(r'^\s*§?([A-Za-z][A-Za-z0-9_-]*?)\.(\d+)\s*[:|]\s*(.+)$', re.S)

def check_citation(cit, subject_id, store):
    m = CIT_RE.match(cit or '')
    if not m: return False, 'format must be ID.n: "quote"'
    sid, n, quote = m.group(1), m.group(2), m.group(3).strip().strip('"“”\'')
    slide = get_slide(store, subject_id, sid, n)
    if slide is None: return False, f'{sid}.{n} not found in sources'
    hay = norm(slide, False)
    parts = [norm(p, False) for p in re.split(r'…|\.\.\.', quote) if len(norm(p, False)) >= 6]
    if not parts: return False, 'quote too short'
    for p in parts:
        if p not in hay: return False, f'quote not found on {sid}.{n}'
    return True, ''

def approval(src, conf):
    if src in SOURCES_OK and conf == 'high': return 'ok'
    if (src in SOURCES_OK and conf == 'medium') or (src == 'inferred-from-concept' and conf == 'high'): return 'review'
    return 'no'

def parse_patch(text):
    t = text.strip()
    a = min([i for i in (t.find('['), t.find('{')) if i >= 0], default=-1)
    b = max(t.rfind(']'), t.rfind('}'))
    if a < 0 or b < a: raise ValueError('no JSON found')
    j = json.loads(t[a:b + 1])
    if isinstance(j, dict): j = j.get('items') or j.get('patch') or [j]
    if not isinstance(j, list): raise ValueError('patch must be a list')
    return j

def import_patch(store, text):
    rep = dict(ok=[], bad=[], todo=[])
    for it in parse_patch(text):
        if not isinstance(it, dict): rep['bad'].append(('?', 'not an object')); continue
        key = str(it.get('key', '')).strip()
        row = store.one("SELECT * FROM inbox WHERE key=?", (key,))
        if not row: rep['bad'].append((key, 'unknown key')); continue
        if row['status'] == 'published': rep['bad'].append((key, 'already published')); continue
        opts = json.loads(row['options'])
        ci, src = it.get('correct_index'), str(it.get('source') or '').strip()
        conf, cit = str(it.get('confidence') or '').strip().lower(), str(it.get('citation') or '').strip()
        expl = str(it.get('explanation') or '').strip()
        if src == 'TODO' or ci is None:
            store.x("UPDATE inbox SET note=?,updated=? WHERE key=?", ('TODO ' + str(it.get('note', ''))[:150], time.time(), key))
            rep['todo'].append(key); continue
        errs = []
        if isinstance(ci, bool) or not isinstance(ci, int) or not 0 <= ci < len(opts): errs.append('correct_index out of range')
        if src not in ALL_SOURCES: errs.append(f'bad source {src!r}')
        if conf not in ('high', 'medium', 'low'): errs.append('bad confidence')
        if not expl: errs.append('empty explanation')
        if len(expl) > 200: errs.append(f'explanation {len(expl)}>200')
        if 'options' in it and it['options'] != opts: errs.append('options changed')
        if src in SOURCES_OK and not errs:
            ok, why = check_citation(cit, row['subject_id'], store)
            if not ok: errs.append('citation: ' + why)
        if errs:
            store.x("UPDATE inbox SET note=?,updated=? WHERE key=?", ('; '.join(errs)[:300], time.time(), key))
            rep['bad'].append((key, '; '.join(errs))); continue
        flags = []
        if row['tg_correct'] is not None and row['tg_correct'] != ci: flags.append(f"poll says {row['tg_correct']}")
        if row['db_correct'] is not None and row['db_correct'] != ci: flags.append(f"live has {row['db_correct']}")
        store.x("UPDATE inbox SET status='imported',correct_index=?,explanation=?,source=?,confidence=?,citation=?,"
                "note=?,flags=?,updated=? WHERE key=?",
                (ci, expl, src, conf, cit, str(it.get('note', ''))[:200] or None, json.dumps(flags), time.time(), key))
        rep['ok'].append(key)
    return rep


# ---------- bundle ----------
def build_bundle(store, live, sid, prompt, limit=40, only_ids=(), include_bundled=False):
    rows = store.q("SELECT * FROM inbox WHERE subject_id=? AND status='hidden' "
                   + ("" if include_bundled else "AND batch IS NULL ") + "ORDER BY created LIMIT ?", (int(sid), limit))
    if not rows: return None
    srcs = [s for s in store.q("SELECT * FROM sources WHERE subject_id=? ORDER BY sid", (int(sid),))
            if not only_ids or s['sid'] in only_ids]
    sub = live.subject(sid)
    out = [prompt.strip(), '', f"=== QUESTIONS ({len(rows)}) | {sub['name']} ==="]
    for r in rows:
        opts = json.loads(r['options'])
        out.append(f"{r['key']}|Q: {r['question']}")
        out.append('  '.join(f"{i}) {o}" for i, o in enumerate(opts)))
        h = []
        if r['tg_correct'] is not None: h.append(f"poll={r['tg_correct']}")
        if r['db_correct'] is not None: h.append(f"live={r['db_correct']}")
        if h: out.append('hint(unverified): ' + ' '.join(h))
    out += ['', '=== SOURCES ===' if srcs else '=== SOURCES: none provided ===']
    for s in srcs:
        out += [f"## {s['sid']}" + (f" {s['title']}" if s['title'] else ''), s['text'].rstrip()]
    text = '\n'.join(out) + '\n'
    return text, rows, est_tokens(text), len(srcs)


# ---------- safe publish to the live repo ----------
def apply_items(db_path, items):
    con = sqlite3.connect(db_path); cur = con.cursor()
    rep = dict(new=[], upd=[], excl=[], drop=[], skip=[])
    before = cur.execute("SELECT count(*) FROM questions").fetchone()[0]
    for it in items:
        k = it['key']
        if it['status'] == 'excluded':
            if it['target'] == 'update':
                n = cur.execute("UPDATE questions SET excluded=1 WHERE key=?", (it['live_key'],)).rowcount
                (rep['excl'] if n else rep['skip']).append(k if n else (k, 'live key missing'))
            else: rep['drop'].append(k)
            continue
        opts = json.loads(it['options']); q = it['question']; expl = it['explanation'] or ''
        problem = None
        if len(expl) > 200: problem = 'explanation>200'
        elif not 2 <= len(opts) <= 10 or any(len(o) > 100 for o in opts): problem = 'options violate Telegram limits'
        elif len(q) > 300: problem = 'question>300 chars (Telegram limit)'
        elif not 0 <= it['correct_index'] < len(opts): problem = 'bad correct_index'
        if problem: rep['skip'].append((k, problem)); continue
        review = 0 if approval(it['source'], it['confidence']) == 'ok' else 1
        if it['target'] == 'update':
            row = cur.execute("SELECT options FROM questions WHERE key=?", (it['live_key'],)).fetchone()
            if not row: rep['skip'].append((k, 'live key missing')); continue
            if [norm(o, False) for o in json.loads(row[0])] != [norm(o, False) for o in opts]:
                rep['skip'].append((k, 'options differ from live row')); continue
            cur.execute("UPDATE questions SET correct_index=?,explanation=?,needs_review=?,excluded=0 WHERE key=?",
                        (it['correct_index'], expl, review, it['live_key']))
            rep['upd'].append(k)
        else:
            if cur.execute("SELECT 1 FROM questions WHERE key=?", (k,)).fetchone():
                rep['skip'].append((k, 'key already in live')); continue
            cur.execute("INSERT INTO questions(key,subject_id,question,options,correct_index,explanation,needs_review,"
                        "answer_source,image,long_question,excluded) VALUES(?,?,?,?,?,?,?,0,NULL,0,0)",
                        (k, it['subject_id'], q, json.dumps(opts, ensure_ascii=False), it['correct_index'], expl, review))
            rep['new'].append(k)
    con.commit()
    ok = cur.execute("PRAGMA integrity_check").fetchone()[0] == 'ok'
    after = cur.execute("SELECT count(*) FROM questions").fetchone()[0]
    con.close()
    if not ok or after != before + len(rep['new']): raise RuntimeError('integrity check failed on the prepared copy')
    return rep

def _put_live(gh, cfg, target, new, old, sha, msg):
    cur, cur_sha = gh.get(cfg['live_repo'], target, cfg['branch'])
    if cur_sha != sha: raise Stale(f'{target} changed on GitHub')
    bpath = f"backups/{target}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    if old is not None: gh.put(cfg['data_repo'], bpath, old, f"backup before: {msg}")
    gh.put(cfg['live_repo'], target, new, msg, sha=sha, branch=cfg['branch'])
    back, _ = gh.get(cfg['live_repo'], target, cfg['branch'])
    if back != new: raise RuntimeError('read-back mismatch after commit')
    return bpath

def prepare_publish(gh, store, cfg):
    rows = [dict(r) for r in store.q("SELECT * FROM inbox WHERE status IN ('approved','excluded') ORDER BY key")]
    if not rows: return None
    old, sha = gh.get(cfg['live_repo'], 'bot.db', cfg['branch'])
    if old is None: raise RuntimeError('bot.db missing in live repo')
    tmp = tempfile.mktemp(suffix='.db'); open(tmp, 'wb').write(old)
    try:
        rep = apply_items(tmp, rows); new = open(tmp, 'rb').read()
    finally:
        if os.path.exists(tmp): os.remove(tmp)
    return dict(type='publish', sha=sha, old=old, new=new, rep=rep, rows=rows)

def execute_publish(gh, store, cfg, act):
    n = len(act['rep']['new']) + len(act['rep']['upd']) + len(act['rep']['excl'])
    changed = n > 0
    bpath = None
    if changed:
        bpath = _put_live(gh, cfg, 'bot.db', act['new'], act['old'], act['sha'], f"Admin bot: publish {n} question(s)")
        tmp = tempfile.mktemp(suffix='.db')
        back, _ = gh.get(cfg['live_repo'], 'bot.db', cfg['branch']); open(tmp, 'wb').write(back)
        con = sqlite3.connect(tmp); bad = []
        for it in act['rows']:
            if it['key'] in act['rep']['new'] + act['rep']['upd']:
                lk = it['live_key'] or it['key']
                r = con.execute("SELECT correct_index,explanation,excluded FROM questions WHERE key=?", (lk,)).fetchone()
                if not r or r[0] != it['correct_index'] or r[1] != it['explanation'] or r[2] != 0: bad.append(it['key'])
        con.close(); os.remove(tmp)
        if bad: raise RuntimeError(f'verification mismatch for {bad}')
    done = set(act['rep']['new'] + act['rep']['upd'] + act['rep']['excl'] + act['rep']['drop'])
    for k in done:
        store.x("UPDATE inbox SET status='published',updated=? WHERE key=?", (time.time(), k))
    if bpath: store.kv_set('last_db_backup', bpath)
    store.log('publish', json.dumps({k: len(v) for k, v in act['rep'].items()}))
    return bpath

def script_check(new, old):
    errs, warns = [], []
    try: compile(new.decode('utf-8'), 'bot.py', 'exec')
    except Exception as e: errs.append(f'syntax error: {e}')
    t = new.decode('utf-8', 'ignore')
    for needle in ('run_polling', 'bot.db'):
        if needle not in t: warns.append(f'"{needle}" not found in new file')
    if old and len(new) < 0.5 * len(old): warns.append('new file is less than half the old size')
    add = rem = 0
    if old:
        for l in difflib.unified_diff(old.decode('utf-8', 'ignore').splitlines(), t.splitlines(), lineterm='', n=0):
            if l.startswith('+') and not l.startswith('+++'): add += 1
            elif l.startswith('-') and not l.startswith('---'): rem += 1
    return errs, warns, add, rem

def prepare_file_update(gh, cfg, target, new, label):
    old, sha = gh.get(cfg['live_repo'], target, cfg['branch'])
    errs, warns, add, rem = script_check(new, old) if target.endswith('.py') else ([], [], 0, 0)
    return dict(type='file', target=target, sha=sha, old=old, new=new, errs=errs, warns=warns, add=add, rem=rem, label=label)

def execute_file_update(gh, store, cfg, act):
    bpath = _put_live(gh, cfg, act['target'], act['new'], act['old'], act['sha'], f"Admin bot: {act['label']}")
    store.kv_set('last_' + ('script' if act['target'].endswith('.py') else 'db') + '_backup', bpath)
    store.log('file', f"{act['target']} {act['label']}")
    return bpath
