from flask import (Flask, request, redirect, session, render_template_string,
                   url_for, flash, abort, g, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta
from urllib.parse import quote, urlencode
from functools import wraps
import os, sqlite3, uuid, hmac, secrets, json, re, csv, io, time, math

# NEXVORA STORE PRO V4 — PHOTO + VIDEO
# Required environment: ADMIN_PASSWORD (or ADMIN_PASSWORD_HASH), WHATSAPP_NUMBER
# Optional: SECRET_KEY, COOKIE_SECURE=1 (HTTPS), TRUST_PROXY=1, DATA_DIR

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", BASE)
os.makedirs(DATA, exist_ok=True)
DB = os.path.join(DATA, "nexvora.db")
UPLOADS = os.path.join(BASE, "static", "uploads")
os.makedirs(UPLOADS, exist_ok=True)

WHATSAPP_NUMBER = "".join(c for c in os.environ.get("WHATSAPP_NUMBER", "992919186336") if c.isdigit())
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
_pw = os.environ.get("Jurabekjon123.")
ADMIN_HASH = os.environ.get("ADMIN_PASSWORD_HASH") or (generate_password_hash(_pw) if _pw else None)
STATUSES = ["Нав", "Тасдиқ шуд", "Омода мешавад", "Фиристода шуд", "Анҷом ёфт", "Бекор"]
PER_PAGE = 12
ADMIN_TTL = 8 * 3600


def load_secret():
    k = os.environ.get("SECRET_KEY")
    if k:
        return k
    p = os.path.join(DATA, ".secret_key")
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    for _ in range(50):
        v = open(p).read().strip()
        if v:
            return v
        time.sleep(0.1)
    raise RuntimeError("SECRET_KEY хонда нашуд")


app = Flask(__name__)
app.secret_key = load_secret()
app.config.update(MAX_CONTENT_LENGTH=100 * 1024 * 1024,
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=COOKIE_SECURE,
                  PERMANENT_SESSION_LIFETIME=timedelta(days=7),
                  SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=30))
if os.environ.get("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


# ---------- Database ----------
def init_db():
    d = sqlite3.connect(DB, timeout=30)
    d.execute("PRAGMA journal_mode=WAL")
    d.executescript("""
    CREATE TABLE IF NOT EXISTS products(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, price REAL NOT NULL DEFAULT 0,
      image TEXT DEFAULT '', category TEXT DEFAULT 'Умумӣ', description TEXT DEFAULT '',
      stock INTEGER NOT NULL DEFAULT 0, sale_price REAL, featured INTEGER DEFAULT 0,
      active INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT NOT NULL, phone TEXT NOT NULL,
      address TEXT DEFAULT '', note TEXT DEFAULT '', items_json TEXT NOT NULL, total REAL NOT NULL,
      status TEXT DEFAULT 'Нав', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS promos(
      code TEXT PRIMARY KEY, percent INTEGER NOT NULL, max_uses INTEGER NOT NULL DEFAULT 0,
      uses INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS hits(key TEXT NOT NULL, ts INTEGER NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_hits ON hits(key, ts);
    CREATE INDEX IF NOT EXISTS ix_prod ON products(active, category);
    """)
    cols = {r[1] for r in d.execute("PRAGMA table_info(orders)")}
    for c, t in (("discount", "REAL DEFAULT 0"), ("promo", "TEXT DEFAULT ''")):
        if c not in cols:
            try:
                d.execute(f"ALTER TABLE orders ADD COLUMN {c} {t}")
            except sqlite3.OperationalError:
                pass
    pcols = {r[1] for r in d.execute("PRAGMA table_info(products)")}
    if "video" not in pcols:
        d.execute("ALTER TABLE products ADD COLUMN video TEXT DEFAULT ''")
    d.commit()
    d.close()


init_db()


def db():
    if "db" not in g:
        con = sqlite3.connect(DB, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA synchronous=NORMAL")
        g.db = con
    return g.db


@app.teardown_appcontext
def close_db(_):
    con = g.pop("db", None)
    if con:
        con.close()


# ---------- Protection ----------
def ip():
    return request.remote_addr or "?"


def too_many(key, limit, window):
    now = int(time.time())
    db().execute("DELETE FROM hits WHERE ts<?", (now - 3600,))
    n = db().execute("SELECT COUNT(*) FROM hits WHERE key=? AND ts>?", (key, now - window)).fetchone()[0]
    return n >= limit


def hit(key):
    db().execute("INSERT INTO hits(key,ts) VALUES(?,?)", (key, int(time.time())))


def limited(key, limit, window):
    if too_many(key, limit, window):
        return True
    hit(key)
    return False


def csrf():
    if not session.get("_csrf"):
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def guard():
    session.permanent = True
    if request.method == "POST":
        a, b = request.form.get("_csrf", ""), session.get("_csrf", "")
        if not a or not b or not hmac.compare_digest(a, b):
            abort(400, description="Санҷиши амниятӣ нодуруст аст. Саҳифаро нав кунед ва дубора кӯшиш намоед.")


@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    r.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    r.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self' 'unsafe-inline'; "
                                            "script-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
    if COOKIE_SECURE:
        r.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith(("/admin", "/login", "/track", "/done", "/checkout", "/cart")):
        r.headers["Cache-Control"] = "no-store"
    return r


def is_admin():
    a = session.get("admin")
    return isinstance(a, int) and a is not True and time.time() - a < ADMIN_TTL


def admin_required(fn):
    @wraps(fn)
    def inner(*a, **kw):
        if not is_admin():
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return inner


# ---------- Helpers ----------
def money(x):
    return f"{float(x or 0):,.2f}"


def eff(p):
    s = p["sale_price"]
    return float(s) if s and float(s) > 0 else float(p["price"])


def items_of(o):
    try:
        return json.loads(o["items_json"])
    except Exception:
        return []


def safe_next(default="/cart"):
    n = request.form.get("next", "")
    return n if n.startswith("/") and not n.startswith("//") and "\\" not in n else default


def get_cart():
    raw, clean = session.get("cart", {}), {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                i, q = int(k), int(v)
                if i > 0 and 1 <= q <= 99:
                    clean[str(i)] = q
            except (ValueError, TypeError):
                pass
    return clean


def cart_info():
    cart, items, sub = get_cart(), [], 0.0
    if cart:
        ids = [int(k) for k in cart]
        rows = db().execute(f"SELECT * FROM products WHERE active=1 AND id IN ({','.join('?' * len(ids))})", ids).fetchall()
        for p in rows:
            q, pr = cart[str(p["id"])], eff(p)
            items.append({"product": p, "quantity": q, "price": pr, "subtotal": pr * q})
            sub += pr * q
    promo, discount = None, 0.0
    code = session.get("promo")
    if code:
        promo = db().execute("SELECT * FROM promos WHERE code=? AND active=1 AND (max_uses=0 OR uses<max_uses)", (code,)).fetchone()
        if promo:
            discount = round(sub * promo["percent"] / 100, 2)
        else:
            session.pop("promo", None)
    return dict(items=items, subtotal=sub, discount=discount, total=round(sub - discount, 2), promo=promo)


@app.context_processor
def inject():
    return dict(cart_count=sum(get_cart().values()), money=money, eff=eff,
                csrf_token=csrf, order_statuses=STATUSES, admin=is_admin())


def sniff(head):
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def save_image(f):
    ext = sniff(f.stream.read(16))
    f.stream.seek(0)
    if not ext:
        return None
    name = uuid.uuid4().hex + "." + ext
    f.save(os.path.join(UPLOADS, name))
    return name


def sniff_video(head):
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "mp4"
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    return None


def save_video(f):
    ext = sniff_video(f.stream.read(16))
    f.stream.seek(0)
    if not ext:
        return None
    name = uuid.uuid4().hex + "." + ext
    f.save(os.path.join(UPLOADS, name))
    return name


def drop_image(name):
    if name:
        p = os.path.join(UPLOADS, os.path.basename(name))
        if os.path.isfile(p):
            os.remove(p)


def drop_video(name):
    if name:
        p = os.path.join(UPLOADS, os.path.basename(name))
        if os.path.isfile(p):
            os.remove(p)


def read_product_form():
    f = request.form
    try:
        name = f.get("name", "").strip()[:120]
        cat = f.get("category", "").strip()[:80] or "Умумӣ"
        desc = f.get("description", "").strip()[:1000]
        pr = float(f.get("price", ""))
        stock = int(f.get("stock", "0") or 0)
        sr = f.get("sale", "").strip()
        sale = float(sr) if sr else None
    except ValueError:
        return None
    if not name or not math.isfinite(pr) or not 0 < pr <= 1e7 or not 0 <= stock <= 1_000_000:
        return None
    if sale is not None and (not math.isfinite(sale) or sale <= 0 or sale >= pr):
        return None
    return dict(name=name, cat=cat, desc=desc, price=pr, stock=stock, sale=sale,
                featured=1 if f.get("featured") else 0)


def wa_link(o, its):
    msg = f"Салом! Фармоиши №{o['id']} аз NEXVORA STORE\nХаридор: {o['customer_name']}\nТелефон: {o['phone']}\n"
    if o["address"]:
        msg += f"Суроға: {o['address']}\n"
    msg += "\nМаҳсулотҳо:\n" + "".join(f"{x['name']} × {x['quantity']} = {x['subtotal']:.2f} сомонӣ\n" for x in its)
    if o["discount"]:
        msg += f"\nТахфиф ({o['promo']}): -{o['discount']:.2f} сомонӣ"
    msg += f"\nҲамагӣ: {o['total']:.2f} сомонӣ"
    if o["note"]:
        msg += f"\nШарҳ: {o['note']}"
    return f"https://wa.me/{WHATSAPP_NUMBER}?text={quote(msg)}"


# ---------- Design ----------
STYLE = """
:root{--bg:#08090c;--panel:#11141b;--panel2:#171b25;--line:#272c38;--gold:#e6c46b;--gold2:#b08c3e;--txt:#f4f5f7;--muted:#9aa1ad;--ok:#1fa971;--bad:#d64550;--r:16px;--serif:Georgia,"Times New Roman",serif}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;color:var(--txt);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:radial-gradient(900px 480px at 88% -6%,#2b2212 0,transparent 60%),var(--bg)}
main{flex:1}a{color:inherit;text-decoration:none}button,input,textarea,select{font:inherit}
h1,h2,h3{font-family:var(--serif);line-height:1.2;margin:.4em 0;font-weight:700}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px}
header{position:sticky;top:0;z-index:30;background:rgba(9,11,16,.84);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
.head{max-width:1180px;margin:auto;padding:12px 16px;display:flex;justify-content:space-between;align-items:center;gap:12px}
.logo{font:900 24px var(--serif);letter-spacing:6px;color:var(--gold)}.logo small{display:block;font:600 8px system-ui;letter-spacing:3px;color:var(--muted)}
.nav{display:flex;gap:4px;align-items:center;font-size:14px}.nav a{padding:8px 12px;border-radius:10px;color:#d8dbe2}.nav a:hover{background:#1b202b}
.cartlink b{background:var(--gold);color:#111;border-radius:99px;padding:1px 8px;margin-left:6px;font-size:12px}
.wrap{max-width:1180px;margin:auto;padding:18px 16px}
.gold,.price{color:var(--gold)}.muted{color:var(--muted);font-size:13px}.center{text-align:center}
.hero{position:relative;overflow:hidden;border:1px solid #3a3020;border-radius:24px;padding:54px 32px;margin:6px 0 20px;background:linear-gradient(135deg,#1d1810,#0f1219 62%)}
.hero:after{content:"";position:absolute;right:-90px;top:-90px;width:300px;height:300px;border-radius:50%;background:radial-gradient(circle,#e6c46c40,transparent 70%)}
.hero h1{font-size:clamp(30px,6.5vw,54px);max-width:680px;margin-top:0}.hero p{max-width:520px;margin:12px 0 22px;font-size:16px}
.btn{display:inline-block;border:0;border-radius:12px;padding:12px 18px;background:linear-gradient(90deg,var(--gold2),#f3dc90);color:#14110a;font-weight:700;cursor:pointer;text-align:center;transition:box-shadow .15s}
.btn:hover{box-shadow:0 8px 22px #e6c46c30}.btn:disabled{opacity:.45;cursor:not-allowed;box-shadow:none}
.dark{background:#242a36;color:#fff}.ghost{background:transparent;color:var(--gold);border:1px solid #5a4a26}.green{background:var(--ok);color:#fff}.red{background:var(--bad);color:#fff}
.sm{padding:8px 12px;font-size:13px}.block{display:block;width:100%}
.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:var(--r);padding:18px;margin:12px 0}
.field{width:100%;padding:12px 14px;background:#0b0e14;color:#fff;border:1px solid #333a48;border-radius:12px;outline:0}.field:focus{border-color:var(--gold)}
label{display:block;margin:12px 0 6px;color:#c9ccd4;font-size:13px}
.chips{display:flex;gap:8px;overflow-x:auto;padding:4px 0 12px;scrollbar-width:none}.chip{white-space:nowrap;padding:8px 14px;border:1px solid var(--line);border-radius:99px;background:#12161e;font-size:14px}.chip.on,.chip:hover{border-color:var(--gold);color:var(--gold)}
.search{display:flex;gap:8px;margin:4px 0 18px}.search select{max-width:150px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;display:flex;flex-direction:column;transition:border-color .2s}
.card:hover{border-color:#6b5628}
.photo{position:relative;height:190px;background:#0b0e13;display:flex;align-items:center;justify-content:center;overflow:hidden;color:#303440;letter-spacing:5px;font-weight:800}
.photo img{width:100%;height:100%;object-fit:contain}
.badge{position:absolute;top:10px;left:10px;background:var(--bad);color:#fff;font-style:normal;font-size:12px;font-weight:700;padding:3px 9px;border-radius:99px}.badge.out{left:auto;right:10px;background:#3b3f4a}
.pbody{padding:12px 14px 14px;display:flex;flex-direction:column;gap:4px;flex:1}.pbody h3{font-size:17px;margin:2px 0}
.price{font-weight:800;font-size:19px}.old{text-decoration:line-through;color:#7d8491;font-size:13px;margin-left:8px;font-weight:400}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:auto;padding-top:10px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.line{display:grid;grid-template-columns:72px 1fr auto;gap:6px 14px;align-items:center}
.thumb{width:72px;height:72px;background:#0b0e13;border-radius:12px;overflow:hidden;display:flex;align-items:center;justify-content:center}.thumb img{width:100%;height:100%;object-fit:contain}
.qty{grid-column:1/-1;display:flex;gap:8px}.qty form{margin:0}
.inline{display:flex;gap:8px}.sum{display:flex;justify-content:space-between;padding:4px 0}.sum.big{font:700 22px var(--serif);color:var(--gold);border-top:1px solid var(--line);margin-top:8px;padding-top:12px}
.steps{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0}.steps div{flex:1 1 90px;text-align:center;font-size:12px;padding:10px 4px;border-radius:10px;background:#12161e;border:1px solid var(--line);color:var(--muted)}
.steps .done{border-color:var(--gold2);color:var(--gold)}.steps .now{background:var(--gold);color:#111;font-weight:700;border-color:var(--gold)}
.pill{display:inline-block;padding:3px 11px;border-radius:99px;font-size:12px;background:#242a36}.pill.bad{background:#4a1b21;color:#ffb3ba}.pill.warn{background:#4a3a14;color:var(--gold)}
.pager{display:flex;gap:6px;justify-content:center;margin:22px 0;flex-wrap:wrap}.pager a{padding:8px 13px;border:1px solid var(--line);border-radius:10px}.pager .on{background:var(--gold);color:#111;border-color:var(--gold)}
.statgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.stat{background:#171b25;border:1px solid #3a3020;border-radius:14px;padding:14px}.stat b{display:block;color:var(--gold);font:700 24px var(--serif);margin-top:4px}
table{width:100%;border-collapse:collapse;min-width:560px}td,th{text-align:left;padding:10px;border-bottom:1px solid #2a2f3a;font-size:13px;vertical-align:top}th{color:var(--gold)}
.tablewrap{overflow-x:auto}.flash{background:#13291f;border:1px solid #1f6f4d;padding:12px 14px;border-radius:12px;margin:8px 0}.flash.err{background:#2b1519;border-color:#7a2a33}
.hp{position:absolute;left:-9999px;opacity:0;height:0}
footer{text-align:center;color:#6f7582;padding:26px 16px;border-top:1px solid var(--line);margin-top:26px;font-size:13px}
@media(max-width:900px){.grid{grid-template-columns:repeat(3,1fr)}.statgrid{grid-template-columns:1fr 1fr}}
@media(max-width:600px){.grid{grid-template-columns:repeat(2,1fr);gap:10px}.photo{height:140px}.cols{grid-template-columns:1fr}.hero{padding:32px 20px}.search{flex-wrap:wrap}.search select{max-width:none;flex:1}.nav a{padding:8px 9px}.logo{font-size:20px;letter-spacing:4px}}
@media(max-width:360px){.grid{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){*{transition:none!important;scroll-behavior:auto!important}}
"""
BASE_HTML = """<!doctype html><html lang="tg"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#08090c"><title>{{title}}</title><style>""" + STYLE + """</style></head><body>
<header><div class="head"><a href="/" class="logo">NEXVORA<small>PREMIUM ONLINE STORE</small></a>
<nav class="nav"><a href="/">Мағоза</a><a href="/track">Пайгирӣ</a><a class="cartlink" href="/cart">Сабад{% if cart_count %}<b>{{cart_count}}</b>{% endif %}</a>{% if admin %}<a href="/admin">Admin</a>{% endif %}</nav></div></header>
<div class="wrap" style="padding-bottom:0">{% for c,m in get_flashed_messages(with_categories=true) %}<div class="flash {{c}}">{{m}}</div>{% endfor %}</div>
{{body|safe}}
<footer>NEXVORA STORE © 2026 · Сифат ва эътимод{% if not admin %} · <a href="/login">Вуруд</a>{% endif %}</footer></body></html>"""


def page(body, title="NEXVORA STORE", status=200, **ctx):
    ctx["title"] = title
    ctx["body"] = render_template_string(body, **ctx)
    return render_template_string(BASE_HTML, **ctx), status


def err_page(code, msg):
    return page('<main class="wrap"><div class="panel center"><h1 class="gold">{{code}}</h1><p class="muted">{{msg}}</p><a class="btn" href="/">Ба мағоза</a></div></main>',
                title=str(code), status=code, code=code, msg=msg)


CARD = """{% macro card(p) %}<article class="card"><a class="photo" href="/product/{{p.id}}">{% if p.image %}<img loading="lazy" src="{{url_for('static',filename='uploads/'+p.image)}}" alt="{{p.name}}">{% else %}NEXVORA{% endif %}{% if p.sale_price and p.sale_price>0 and p.price>0 %}<i class="badge">-{{((1-p.sale_price/p.price)*100)|round|int}}%</i>{% endif %}{% if p.stock<1 %}<i class="badge out">Тамом шуд</i>{% endif %}</a>
<div class="pbody"><div class="muted">{{p.category}}{% if p.featured %} · <span class="gold">Машҳур</span>{% endif %}</div><h3>{{p.name}}</h3><div><span class="price">{{money(eff(p))}} сомонӣ</span>{% if p.sale_price and p.sale_price>0 %}<span class="old">{{money(p.price)}}</span>{% endif %}</div>
<div class="actions"><form method="post" action="/cart/add/{{p.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input type="hidden" name="next" value="{{request.full_path.rstrip('?')}}"><button class="btn sm block" {% if p.stock<1 %}disabled{% endif %}>+ Ба сабад</button></form><a class="btn dark sm" href="/product/{{p.id}}">Дидан</a></div></div></article>{% endmacro %}"""

HOME = CARD + """<main class="wrap"><section class="hero"><h1>Интихоби беҳтарин — дар як ҷо.</h1><p class="muted">Маҳсулотро интихоб кунед, ба сабад илова намоед ва фармоишро тавассути WhatsApp фиристед.</p><a class="btn" href="#products">Харид кардан</a> <a class="btn ghost" href="/track">Фармоишро пайгирӣ кардан</a></section>
<div class="chips" id="categories"><a class="chip {{'on' if not cat}}" href="/">Ҳама</a>{% for c in cats %}<a class="chip {{'on' if c==cat}}" href="/?category={{c|urlencode}}">{{c}}</a>{% endfor %}</div>
<form class="search" method="get" id="products">{% if cat %}<input type="hidden" name="category" value="{{cat}}">{% endif %}<input class="field" name="q" value="{{q}}" placeholder="Ҷустуҷӯи маҳсулот" maxlength="80"><select class="field" name="sort"><option value="new">Навтарин</option><option value="low" {{'selected' if sort=='low'}}>Арзонтарин</option><option value="high" {{'selected' if sort=='high'}}>Қиматтарин</option></select><button class="btn">Ёфтан</button></form>
<div class="grid">{% for p in products %}{{card(p)}}{% else %}<div class="panel" style="grid-column:1/-1">Маҳсулот ёфт нашуд. Ҷустуҷӯро тағйир диҳед.</div>{% endfor %}</div>
{% if pages>1 %}<div class="pager">{% for n in range(1,pages+1) %}<a class="{{'on' if n==pg}}" href="/?{{qs}}{{'&' if qs}}page={{n}}">{{n}}</a>{% endfor %}</div>{% endif %}</main>"""


@app.route("/")
def home():
    q = request.args.get("q", "").strip()[:80]
    cat = request.args.get("category", "").strip()[:80]
    sort = request.args.get("sort", "new")
    if sort not in ("new", "low", "high"):
        sort = "new"
    try:
        pg = max(1, int(request.args.get("page", "1")))
    except ValueError:
        pg = 1
    e = "CASE WHEN sale_price>0 THEN sale_price ELSE price END"
    order = {"low": e + " ASC, id DESC", "high": e + " DESC, id DESC", "new": "featured DESC, id DESC"}[sort]
    where, args = "active=1", []
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where += " AND (name LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
        args += [like] * 3
    if cat:
        where += " AND category=?"
        args.append(cat)
    d = db()
    total = d.execute(f"SELECT COUNT(*) FROM products WHERE {where}", args).fetchone()[0]
    pages = max(1, -(-total // PER_PAGE))
    pg = min(pg, pages)
    products = d.execute(f"SELECT * FROM products WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                         args + [PER_PAGE, (pg - 1) * PER_PAGE]).fetchall()
    cats = [r[0] for r in d.execute("SELECT DISTINCT category FROM products WHERE active=1 ORDER BY category")]
    qs = urlencode({k: v for k, v in (("q", q), ("category", cat), ("sort", sort if sort != "new" else "")) if v})
    return page(HOME, products=products, cats=cats, q=q, cat=cat, sort=sort, pg=pg, pages=pages, qs=qs)


@app.route("/product/<int:pid>")
def product_detail(pid):
    p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        abort(404)
    rel = db().execute("SELECT * FROM products WHERE active=1 AND category=? AND id!=? ORDER BY id DESC LIMIT 4",
                       (p["category"], pid)).fetchall()
    detail = CARD + """<main class="wrap"><div class="cols"><div><div class="panel"><div class="photo" style="height:340px;border-radius:12px">{% if p.image %}<img src="{{url_for('static',filename='uploads/'+p.image)}}" alt="{{p.name}}">{% else %}NEXVORA{% endif %}</div></div>
{% if p.video %}<div class="panel"><h2 class="gold">Видеои маҳсулот 🎥</h2><video controls playsinline preload="metadata" style="width:100%;max-height:450px;border-radius:14px;background:#000"><source src="{{url_for('static',filename='uploads/'+p.video)}}" type="{{'video/webm' if p.video.endswith('.webm') else 'video/mp4'}}">Браузери шумо видео намоиш дода наметавонад.</video></div>{% endif %}</div>
<div class="panel"><p class="muted">{{p.category}}</p><h1>{{p.name}}</h1><p><span class="price" style="font-size:28px">{{money(eff(p))}} сомонӣ</span>{% if p.sale_price and p.sale_price>0 %}<span class="old">{{money(p.price)}}</span>{% endif %}</p><p class="muted" style="font-size:15px">{{p.description}}</p>
<p>{% if p.stock<1 %}<span class="pill bad">Тамом шуд</span>{% elif p.stock<=3 %}<span class="pill warn">Танҳо {{p.stock}} дона монд</span>{% else %}<span class="pill">Дар анбор: {{p.stock}}</span>{% endif %}</p>
<form method="post" action="/cart/add/{{p.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input type="hidden" name="next" value="{{request.path}}"><button class="btn block" {% if p.stock<1 %}disabled{% endif %}>Ба сабад илова кардан</button></form>
<a class="btn green block" style="margin-top:10px" href="https://wa.me/{{phone}}?text={{('Салом! Дар бораи '+p.name+' маълумот мехоҳам.')|urlencode}}">Савол дар WhatsApp</a></div></div>
{% if rel %}<h2>Маҳсулоти монанд</h2><div class="grid">{% for r in rel %}{{card(r)}}{% endfor %}</div>{% endif %}</main>"""
    return page(detail, title=p["name"], p=p, rel=rel, phone=WHATSAPP_NUMBER)


CART = """<main class="wrap" style="max-width:820px"><h1>Сабади харид</h1>
{% for x in items %}<div class="panel line"><div class="thumb">{% if x.product.image %}<img src="{{url_for('static',filename='uploads/'+x.product.image)}}" alt="">{% endif %}</div><div><b>{{x.product.name}}</b><div class="muted">{{money(x.price)}} × {{x.quantity}}</div></div><b class="gold">{{money(x.subtotal)}}</b>
<div class="qty">{% for action,label in [('minus','−'),('add','+'),('remove','Нест кардан')] %}<form method="post" action="/cart/{{action}}/{{x.product.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">{{label}}</button></form>{% endfor %}</div></div>
{% else %}<div class="panel center">Сабад холӣ аст. <a class="gold" href="/">Ба мағоза равед</a></div>{% endfor %}
{% if items %}<div class="panel"><form class="inline" method="post" action="/cart/promo"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input class="field" name="code" placeholder="Промокод" maxlength="30" value="{{promo.code if promo}}"><button class="btn dark">Татбиқ</button></form>
<div class="sum" style="margin-top:12px"><span>Маблағи маҳсулот</span><span>{{money(subtotal)}}</span></div>{% if promo %}<div class="sum"><span>Тахфиф ({{promo.code}} −{{promo.percent}}%)</span><span>−{{money(discount)}}</span></div>{% endif %}<div class="sum big"><span>Ҳамагӣ</span><span>{{money(total)}} сомонӣ</span></div>
<a class="btn block" style="margin-top:14px" href="/checkout">Расмикунонии фармоиш</a></div>{% endif %}</main>"""


@app.route("/cart")
def cart():
    return page(CART, title="Сабад", **cart_info())


@app.route("/cart/promo", methods=["POST"])
def cart_promo():
    if limited("promo:" + ip(), 20, 600):
        abort(429)
    code = request.form.get("code", "").strip().upper()[:30]
    if not code:
        session.pop("promo", None)
        flash("Промокод хориҷ шуд.", "ok")
    elif db().execute("SELECT 1 FROM promos WHERE code=? AND active=1 AND (max_uses=0 OR uses<max_uses)", (code,)).fetchone():
        session["promo"] = code
        flash("Промокод татбиқ шуд.", "ok")
    else:
        flash("Промокод нодуруст аст ё муддаташ гузаштааст.", "err")
    return redirect("/cart")


@app.route("/cart/<action>/<int:pid>", methods=["POST"])
def cart_change(action, pid):
    if action not in ("add", "minus", "remove"):
        abort(404)
    c, key = get_cart(), str(pid)
    if action == "add":
        p = db().execute("SELECT stock FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if p and c.get(key, 0) < min(99, p["stock"]):
            c[key] = c.get(key, 0) + 1
            flash("Ба сабад илова шуд.", "ok")
        else:
            flash("Миқдори маҳсулот дар анбор кофӣ нест.", "err")
    elif action == "minus" and key in c:
        c[key] -= 1
        if c[key] <= 0:
            c.pop(key)
    elif action == "remove":
        c.pop(key, None)
    session["cart"] = c
    return redirect(safe_next())


CHECKOUT = """<main class="wrap" style="max-width:700px"><h1>Расмикунонии фармоиш</h1><div class="panel"><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input class="hp" name="website" tabindex="-1" autocomplete="off">
<label>Ному насаб</label><input class="field" name="name" required minlength="2" maxlength="100" autocomplete="name"><label>Телефон</label><input class="field" name="phone" required maxlength="30" placeholder="+992..." inputmode="tel" autocomplete="tel"><label>Суроға (ихтиёрӣ)</label><input class="field" name="address" maxlength="250"><label>Шарҳ (ихтиёрӣ)</label><textarea class="field" name="note" rows="3" maxlength="500"></textarea>
{% if promo %}<div class="sum" style="margin-top:12px"><span>Тахфиф ({{promo.code}})</span><span>−{{money(discount)}}</span></div>{% endif %}<div class="sum big"><span>Ҳамагӣ</span><span>{{money(total)}} сомонӣ</span></div><button class="btn block" style="margin-top:14px">Сабти фармоиш</button></form></div></main>"""


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    ci = cart_info()
    if not ci["items"]:
        return redirect("/cart")
    if request.method == "GET":
        return page(CHECKOUT, title="Фармоиш", **ci)
    if request.form.get("website"):
        return redirect("/")
    if limited("order:" + ip(), 5, 600):
        abort(429, description="Фармоишҳо хеле зуд-зуд мераванд. Якчанд дақиқа интизор шавед.")
    name = request.form.get("name", "").strip()[:100]
    phone = request.form.get("phone", "").strip()[:30]
    address = request.form.get("address", "").strip()[:250]
    note = request.form.get("note", "").strip()[:500]
    if len(name) < 2 or not re.fullmatch(r"\+?[0-9() -]{7,30}", phone) or len(re.sub(r"\D", "", phone)) < 7:
        flash("Ном ё рақами телефон нодуруст аст.", "err")
        return redirect("/checkout")
    d = db()
    d.execute("BEGIN IMMEDIATE")
    try:
        order_items = []
        for x in ci["items"]:
            q, pid = x["quantity"], x["product"]["id"]
            cur = d.execute("UPDATE products SET stock=stock-? WHERE id=? AND active=1 AND stock>=?", (q, pid, q))
            if cur.rowcount != 1:
                raise ValueError("stock")
            p = d.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            pr = eff(p)
            order_items.append({"id": pid, "name": p["name"], "quantity": q, "price": pr, "subtotal": pr * q})
        sub = sum(i["subtotal"] for i in order_items)
        discount, code = 0.0, ""
        pcode = session.get("promo")
        if pcode:
            cur = d.execute("UPDATE promos SET uses=uses+1 WHERE code=? AND active=1 AND (max_uses=0 OR uses<max_uses)", (pcode,))
            if cur.rowcount == 1:
                pc = d.execute("SELECT percent FROM promos WHERE code=?", (pcode,)).fetchone()
                discount, code = round(sub * pc["percent"] / 100, 2), pcode
        total = round(sub - discount, 2)
        oid = d.execute("INSERT INTO orders(customer_name,phone,address,note,items_json,total,discount,promo) VALUES(?,?,?,?,?,?,?,?)",
                        (name, phone, address, note, json.dumps(order_items, ensure_ascii=False), total, discount, code)).lastrowid
        d.execute("COMMIT")
    except ValueError:
        d.execute("ROLLBACK")
        flash("Баъзе маҳсулот дигар дар анбор нест ё миқдораш кам шудааст. Сабадро санҷед.", "err")
        return redirect("/cart")
    except Exception:
        d.execute("ROLLBACK")
        raise
    session["cart"] = {}
    session.pop("promo", None)
    session["done"] = oid
    return redirect("/done")


DONE = """<main class="wrap" style="max-width:640px"><div class="panel center"><h1 class="gold">Фармоиш сабт шуд</h1><p style="font-size:20px">Рақами фармоиш: <b>№{{o.id}}</b></p><p class="muted">Матни тайёрро тавассути WhatsApp фиристед, то мо фармоишро зудтар тасдиқ кунем. Барои пайгирӣ рақами фармоиш ва телефони худро нигоҳ доред.</p>
<a class="btn green block" href="{{wa}}">Кушодани WhatsApp</a><p><a class="gold" href="/track">Пайгирии фармоиш</a> · <a href="/">Ба мағоза</a></p></div></main>"""


@app.route("/done")
def done():
    oid = session.get("done")
    o = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone() if oid else None
    if not o:
        return redirect("/")
    return page(DONE, title="Фармоиш сабт шуд", o=o, wa=wa_link(o, items_of(o)))


TRACK = """<main class="wrap" style="max-width:680px"><h1>Пайгирии фармоиш</h1><div class="panel"><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Рақами фармоиш</label><input class="field" name="order" inputmode="numeric" required maxlength="10" placeholder="Масалан: 15"><label>Телефон</label><input class="field" name="phone" required maxlength="30" placeholder="+992..." inputmode="tel"><button class="btn block" style="margin-top:14px">Санҷидан</button></form></div>
{% if o %}<div class="panel"><h2>Фармоиши №{{o.id}}</h2><p class="muted">{{o.created_at}}</p>
{% if o.status=='Бекор' %}<p><span class="pill bad">Бекор шуд</span></p>{% else %}{% set steps=order_statuses[:5] %}{% set idx=steps.index(o.status) if o.status in steps else 0 %}<div class="steps">{% for s in steps %}<div class="{{'now' if loop.index0==idx else ('done' if loop.index0<idx else '')}}">{{s}}</div>{% endfor %}</div>{% endif %}
{% for x in its %}<div class="sum"><span>{{x.name}} × {{x.quantity}}</span><span>{{money(x.subtotal)}}</span></div>{% endfor %}<div class="sum big"><span>Ҳамагӣ</span><span>{{money(o.total)}} сомонӣ</span></div></div>{% endif %}</main>"""


@app.route("/track", methods=["GET", "POST"])
def track():
    o = None
    if request.method == "POST":
        if limited("track:" + ip(), 15, 600):
            abort(429)
        try:
            oid = int(request.form.get("order", "").strip().lstrip("#"))
        except ValueError:
            oid = 0
        digits = re.sub(r"\D", "", request.form.get("phone", ""))
        row = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if row and len(digits) >= 7 and re.sub(r"\D", "", row["phone"])[-9:] == digits[-9:]:
            o = row
        else:
            flash("Фармоиш бо ин маълумот ёфт нашуд. Рақам ва телефонро санҷед.", "err")
    return page(TRACK, title="Пайгирӣ", o=o, its=items_of(o) if o else [])


# ---------- Admin ----------
LOGIN = """<main class="wrap" style="max-width:440px"><div class="panel"><h1 class="gold">Вуруд ба панели admin</h1><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Парол</label><input class="field" type="password" name="password" required autocomplete="current-password"><button class="btn block" style="margin-top:14px">Ворид шудан</button></form></div></main>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        key = "login:" + ip()
        if too_many(key, 5, 900):
            abort(429, description="Кӯшишҳои зиёд. 15 дақиқа интизор шавед.")
        if ADMIN_HASH and check_password_hash(ADMIN_HASH, request.form.get("password", "")):
            session.clear()
            session["admin"] = int(time.time())
            csrf()
            return redirect("/admin")
        hit(key)
        flash("Парол нодуруст аст.", "err")
    if not ADMIN_HASH:
        flash("ADMIN_PASSWORD дар муҳит танзим нашудааст, вуруд ғайрифаъол аст.", "err")
    return page(LOGIN, title="Вуруд")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/")


PFIELDS = """{% macro pfields(p=None) %}<div class="cols"><div><label>Ном *</label><input class="field" name="name" required maxlength="120" value="{{p.name if p else ''}}"></div><div><label>Категория</label><input class="field" name="category" maxlength="80" value="{{p.category if p else ''}}"></div>
<div><label>Нарх *</label><input class="field" type="number" step=".01" min="0.01" name="price" required value="{{p.price if p else ''}}"></div><div><label>Нархи тахфифӣ</label><input class="field" type="number" step=".01" min="0" name="sale" value="{{p.sale_price if p and p.sale_price else ''}}"></div>
<div><label>Миқдори анбор</label><input class="field" type="number" min="0" name="stock" value="{{p.stock if p else 0}}"></div>
<div><label>Акс (jpg, png, webp)</label><input class="field" type="file" name="image" accept=".jpg,.jpeg,.png,.webp"></div>
<div><label>Видео (MP4/WebM, то 100 MB)</label><input class="field" type="file" name="video" accept="video/mp4,video/webm"></div></div>
<label>Тавсиф</label><textarea class="field" rows="3" name="description" maxlength="1000">{{p.description if p else ''}}</textarea><label><input type="checkbox" name="featured" {{'checked' if p and p.featured}}> Маҳсулоти машҳур</label>{% if p %}<label><input type="checkbox" name="active" {{'checked' if p.active}}> Дар мағоза намоиш дода шавад</label>{% endif %}{% endmacro %}"""

ADMIN = PFIELDS + """<main class="wrap"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><h1 class="gold">Панели admin</h1><form method="post" action="/logout"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">Баромадан</button></form></div>
<div class="statgrid"><div class="stat">Маҳсулот<b>{{s.pc}}</b></div><div class="stat">Фармоишҳо (имрӯз {{s.today}})<b>{{s.oc}}</b></div><div class="stat">Даромад<b>{{money(s.rev)}}</b></div><div class="stat">Анбори кам (≤3)<b>{{s.low}}</b></div></div>
<div class="panel"><h2>Иловаи маҳсулот</h2><form method="post" action="/admin/add" enctype="multipart/form-data"><input type="hidden" name="_csrf" value="{{csrf_token()}}">{{pfields()}}<button class="btn" style="margin-top:12px">Илова кардан</button></form></div>
<div class="panel"><h2>Промокодҳо</h2><form class="inline" method="post" action="/admin/promo/add" style="flex-wrap:wrap"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input class="field" style="flex:2;min-width:120px" name="code" placeholder="КОД (масалан NEW10)" maxlength="30" required><input class="field" style="flex:1;min-width:90px" type="number" name="percent" min="1" max="90" placeholder="% тахфиф" required><input class="field" style="flex:1;min-width:90px" type="number" name="max_uses" min="0" placeholder="Ҳад (0 = бешумор)"><button class="btn">Сохтан</button></form>
<div class="tablewrap"><table><tr><th>Код</th><th>Тахфиф</th><th>Истифода</th><th>Ҳолат</th><th></th></tr>{% for c in promos %}<tr><td><b>{{c.code}}</b></td><td>{{c.percent}}%</td><td>{{c.uses}}{% if c.max_uses %} / {{c.max_uses}}{% endif %}</td><td>{{'Фаъол' if c.active else 'Хомӯш'}}</td><td style="display:flex;gap:6px"><form method="post" action="/admin/promo/toggle/{{c.code}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">{{'Хомӯш' if c.active else 'Фаъол'}}</button></form><form method="post" action="/admin/promo/delete/{{c.code}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red sm">Нест</button></form></td></tr>{% endfor %}</table></div></div>
<div class="panel"><h2>Маҳсулотҳо</h2><div class="tablewrap"><table><tr><th>Ном</th><th>Нарх</th><th>Анбор</th><th></th></tr>{% for p in products %}<tr><td>{{p.name}}<br><span class="muted">{{p.category}}{% if not p.active %} · пинҳон{% endif %}</span></td><td>{{money(eff(p))}}</td><td>{% if p.stock<=3 %}<span class="pill warn">{{p.stock}}</span>{% else %}{{p.stock}}{% endif %}</td><td style="display:flex;gap:6px"><a class="btn dark sm" href="/admin/edit/{{p.id}}">Таҳрир</a><form method="post" action="/admin/delete/{{p.id}}" onsubmit="return confirm('Нест карда шавад?')"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red sm">Нест</button></form></td></tr>{% endfor %}</table></div></div>
<div class="panel"><div style="display:flex;justify-content:space-between;align-items:center"><h2>Фармоишҳо</h2><a class="btn ghost sm" href="/admin/export.csv">Боргирии CSV</a></div><div class="tablewrap"><table><tr><th>№</th><th>Харидор</th><th>Ҳамагӣ</th><th>Вазъият</th><th>Сана</th><th></th></tr>{% for o in orders %}<tr><td>#{{o.id}}</td><td>{{o.customer_name}}<br><span class="muted">{{o.phone}}</span></td><td>{{money(o.total)}}</td><td>{{o.status}}</td><td>{{o.created_at}}</td><td><a class="btn dark sm" href="/admin/order/{{o.id}}">Кушодан</a></td></tr>{% endfor %}</table></div></div></main>"""


@app.route("/admin")
@admin_required
def admin():
    d = db()
    s = dict(pc=d.execute("SELECT COUNT(*) FROM products").fetchone()[0],
             oc=d.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
             today=d.execute("SELECT COUNT(*) FROM orders WHERE date(created_at)=date('now')").fetchone()[0],
             rev=d.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='Бекор'").fetchone()[0],
             low=d.execute("SELECT COUNT(*) FROM products WHERE active=1 AND stock<=3").fetchone()[0])
    return page(ADMIN, title="Admin",
                products=d.execute("SELECT * FROM products ORDER BY id DESC LIMIT 300").fetchall(),
                orders=d.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 100").fetchall(),
                promos=d.execute("SELECT * FROM promos ORDER BY code").fetchall(), s=s)


@app.route("/admin/add", methods=["POST"])
@admin_required
def admin_add():
    v = read_product_form()
    if not v:
        flash("Маълумоти маҳсулот ё нарх нодуруст аст.", "err")
        return redirect("/admin")
    img = ""
    vid = ""
    f = request.files.get("image")
    if f and f.filename:
        img = save_image(f)
        if not img:
            flash("Акс бояд jpg, png ё webp бошад.", "err")
            return redirect("/admin")
    vf = request.files.get("video")
    if vf and vf.filename:
        vid = save_video(vf)
        if not vid:
            drop_image(img)
            flash("Видео бояд MP4 ё WebM бошад.", "err")
            return redirect("/admin")
    db().execute(
        """INSERT INTO products(name,price,image,video,category,description,stock,sale_price,featured)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        (v["name"], v["price"], img, vid, v["cat"], v["desc"], v["stock"], v["sale"], v["featured"])
    )
    flash("Маҳсулот илова шуд.", "ok")
    return redirect("/admin")


EDIT = PFIELDS + """<main class="wrap" style="max-width:760px"><div class="panel"><h1 class="gold">Таҳрири маҳсулот</h1>
{% if p.image %}<img style="max-width:140px;border-radius:10px" src="{{url_for('static',filename='uploads/'+p.image)}}" alt="">{% endif %}
{% if p.video %}<p class="gold">Видеои ҳозира:</p><video controls playsinline preload="metadata" style="width:100%;max-width:400px;border-radius:12px;background:#000"><source src="{{url_for('static',filename='uploads/'+p.video)}}" type="{{'video/webm' if p.video.endswith('.webm') else 'video/mp4'}}"></video>{% endif %}
<form method="post" enctype="multipart/form-data"><input type="hidden" name="_csrf" value="{{csrf_token()}}">{{pfields(p)}}<button class="btn" style="margin-top:12px">Нигоҳ доштан</button> <a class="btn dark" href="/admin">Бозгашт</a></form></div></main>"""


@app.route("/admin/edit/<int:pid>", methods=["GET", "POST"])
@admin_required
def edit_product(pid):
    d = db()
    p = d.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        abort(404)
    if request.method == "POST":
        v = read_product_form()
        if not v:
            flash("Маълумоти воридшуда нодуруст аст.", "err")
            return redirect(url_for("edit_product", pid=pid))
        img = p["image"]
        vid = p["video"] or ""
        f = request.files.get("image")
        if f and f.filename:
            new = save_image(f)
            if not new:
                flash("Акс бояд jpg, png ё webp бошад.", "err")
                return redirect(url_for("edit_product", pid=pid))
            drop_image(img)
            img = new
        vf = request.files.get("video")
        if vf and vf.filename:
            new_video = save_video(vf)
            if not new_video:
                flash("Видео бояд MP4 ё WebM бошад.", "err")
                return redirect(url_for("edit_product", pid=pid))
            drop_video(vid)
            vid = new_video
        d.execute(
            """UPDATE products SET name=?,category=?,description=?,price=?,sale_price=?,
            stock=?,image=?,video=?,featured=?,active=? WHERE id=?""",
            (v["name"], v["cat"], v["desc"], v["price"], v["sale"], v["stock"],
             img, vid, v["featured"], 1 if request.form.get("active") else 0, pid)
        )
        flash("Маҳсулот нав шуд.", "ok")
        return redirect("/admin")
    return page(EDIT, title="Таҳрир", p=p)


@app.route("/admin/delete/<int:pid>", methods=["POST"])
@admin_required
def delete_product(pid):
    p = db().execute("SELECT image,video FROM products WHERE id=?", (pid,)).fetchone()
    if p:
        db().execute("DELETE FROM products WHERE id=?", (pid,))
        drop_image(p["image"])
        drop_video(p["video"])
    flash("Маҳсулот нест карда шуд.", "ok")
    return redirect("/admin")


@app.route("/admin/promo/add", methods=["POST"])
@admin_required
def promo_add():
    code = re.sub(r"[^A-Z0-9_-]", "", request.form.get("code", "").upper())[:30]
    try:
        pc, mx = int(request.form.get("percent", "")), int(request.form.get("max_uses", "0") or 0)
    except ValueError:
        pc, mx = 0, -1
    if not code or not 1 <= pc <= 90 or mx < 0:
        flash("Промокод ё фоизи тахфиф нодуруст аст (1–90).", "err")
    else:
        try:
            db().execute("INSERT INTO promos(code,percent,max_uses) VALUES(?,?,?)", (code, pc, mx))
            flash("Промокод сохта шуд.", "ok")
        except sqlite3.IntegrityError:
            flash("Чунин промокод аллакай ҳаст.", "err")
    return redirect("/admin")


@app.route("/admin/promo/<action>/<code>", methods=["POST"])
@admin_required
def promo_change(action, code):
    if action == "toggle":
        db().execute("UPDATE promos SET active=1-active WHERE code=?", (code,))
    elif action == "delete":
        db().execute("DELETE FROM promos WHERE code=?", (code,))
    else:
        abort(404)
    return redirect("/admin")


ORDER = """<main class="wrap" style="max-width:720px"><div class="panel"><h1 class="gold">Фармоиши №{{o.id}}</h1><p class="muted">{{o.created_at}}</p><p>Харидор: <b>{{o.customer_name}}</b></p><p>Телефон: <a class="gold" href="tel:{{o.phone}}">{{o.phone}}</a> · <a class="gold" href="https://wa.me/{{o.phone|replace('+','')|replace(' ','')|replace('-','')|replace('(','')|replace(')','')}}">WhatsApp</a></p><p>Суроға: {{o.address or '—'}}</p><p>Шарҳ: {{o.note or '—'}}</p>
<h3>Маҳсулотҳо</h3>{% for x in its %}<div class="sum"><span>{{x.name}} × {{x.quantity}}</span><span>{{money(x.subtotal)}}</span></div>{% endfor %}{% if o.discount %}<div class="sum"><span>Тахфиф ({{o.promo}})</span><span>−{{money(o.discount)}}</span></div>{% endif %}<div class="sum big"><span>Ҳамагӣ</span><span>{{money(o.total)}} сомонӣ</span></div>
<form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Вазъият</label><select class="field" name="status">{% for s in order_statuses %}<option {{'selected' if o.status==s}}>{{s}}</option>{% endfor %}</select><button class="btn" style="margin-top:12px">Нав кардан</button> <a class="btn dark" href="/admin">Бозгашт</a></form></div></main>"""


@app.route("/admin/order/<int:oid>", methods=["GET", "POST"])
@admin_required
def order_detail(oid):
    d = db()
    o = d.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        abort(404)
    if request.method == "POST":
        s = request.form.get("status", "")
        if s not in STATUSES:
            abort(400)
        d.execute("UPDATE orders SET status=? WHERE id=?", (s, oid))
        flash("Вазъи фармоиш нав шуд.", "ok")
        return redirect(url_for("order_detail", oid=oid))
    return page(ORDER, title=f"Фармоиш №{oid}", o=o, its=items_of(o))


@app.route("/admin/export.csv")
@admin_required
def export_csv():
    def cell(v):
        v = "" if v is None else str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@") else v
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["№", "Сана", "Харидор", "Телефон", "Суроға", "Шарҳ", "Маҳсулотҳо", "Тахфиф", "Ҳамагӣ", "Вазъият"])
    for o in db().execute("SELECT * FROM orders ORDER BY id DESC").fetchall():
        its = "; ".join(f"{x['name']} x{x['quantity']}" for x in items_of(o))
        w.writerow([o["id"], o["created_at"], cell(o["customer_name"]), cell(o["phone"]), cell(o["address"]),
                    cell(o["note"]), cell(its), o["discount"] or 0, o["total"], o["status"]])
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=orders.csv"})


# ---------- Errors ----------
@app.errorhandler(400)
def e400(e):
    return err_page(400, getattr(e, "description", "Дархост нодуруст аст."))


@app.errorhandler(404)
def e404(e):
    return err_page(404, "Саҳифа ёфт нашуд.")


@app.errorhandler(413)
def e413(e):
    return err_page(413, "Файл хеле калон аст. Ҳадди аксар 100 MB.")


@app.errorhandler(429)
def e429(e):
    d = getattr(e, "description", "")
    return err_page(429, d if d and "Too Many" not in d else "Дархостҳо хеле зиёданд. Каме интизор шавед.")


@app.errorhandler(500)
def e500(e):
    return err_page(500, "Хатои дохилӣ. Лутфан баъдтар дубора кӯшиш кунед.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
