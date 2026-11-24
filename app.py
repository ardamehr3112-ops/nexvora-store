from flask import (Flask, request, redirect, session, render_template_string,
                   url_for, flash, abort, g, Response)
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import timedelta
from urllib.parse import quote, urlencode
from functools import wraps
import os, sqlite3, uuid, hmac, secrets, json, re, csv, io, time, math, logging
from logging.handlers import RotatingFileHandler

# NEXVORA STORE PRO V4 — V75 REVIEW MODERATION — BASE V74
# V75: review approval queue, public-only approved reviews, and per-session submission cooldown.
# V72: customer address book with add, edit, delete, default address, and checkout suggestions.
# V73: comparison improvements and CSV export for selected products.
# V74: customer account wallet for phone-bound coupons and gift-card balances.
# Required environment: ADMIN_PASSWORD (or ADMIN_PASSWORD_HASH), WHATSAPP_NUMBER
# Optional: SECRET_KEY, COOKIE_SECURE=1 (HTTPS), TRUST_PROXY=1, DATA_DIR

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", BASE)
os.makedirs(DATA, exist_ok=True)
DB = os.path.join(DATA, "nexvora.db")
# Keep database, secrets, logs, and uploaded product media on Render's persistent disk.
STATIC_DIR = os.path.join(BASE, "static")
os.makedirs(STATIC_DIR, exist_ok=True)
if os.path.abspath(DATA) != os.path.abspath(BASE):
    UPLOADS = os.path.join(DATA, "uploads")
    os.makedirs(UPLOADS, exist_ok=True)
    STATIC_UPLOADS = os.path.join(STATIC_DIR, "uploads")
    if not os.path.lexists(STATIC_UPLOADS):
        os.symlink(UPLOADS, STATIC_UPLOADS)
else:
    UPLOADS = os.path.join(STATIC_DIR, "uploads")
    os.makedirs(UPLOADS, exist_ok=True)

# V67: log application errors without exposing private request data.
LOG_FILE = os.path.join(DATA, "error.log")
logger = logging.getLogger("nexvora")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _log_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    _log_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(_log_handler)

WHATSAPP_NUMBER = "".join(c for c in os.environ.get("WHATSAPP_NUMBER", "992919186336") if c.isdigit())
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
_pw = os.environ.get("ADMIN_PASSWORD", "")
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
      status TEXT DEFAULT 'Нав', created_at TEXT DEFAULT CURRENT_TIMESTAMP, points_redeemed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS banners(
      id INTEGER PRIMARY KEY CHECK(id=1), title TEXT NOT NULL DEFAULT '', subtitle TEXT DEFAULT '',
      image TEXT DEFAULT '', button_text TEXT DEFAULT '', button_url TEXT DEFAULT '',
      start_date TEXT DEFAULT '', end_date TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS promos(
      code TEXT PRIMARY KEY, percent INTEGER NOT NULL, max_uses INTEGER NOT NULL DEFAULT 0,
      uses INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, expires_on TEXT DEFAULT '');
    CREATE TABLE IF NOT EXISTS personal_coupons(
      code TEXT PRIMARY KEY, phone TEXT NOT NULL, percent INTEGER NOT NULL CHECK(percent BETWEEN 1 AND 90),
      expires_on TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1, used_order_id INTEGER DEFAULT NULL,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS product_questions(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      product_id INTEGER NOT NULL,
      customer_name TEXT NOT NULL DEFAULT 'Муштарӣ',
      question TEXT NOT NULL,
      answer TEXT DEFAULT '',
      is_public INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      answered_at TEXT DEFAULT '',
      FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS ix_product_questions ON product_questions(product_id, is_public, id DESC);
    CREATE TABLE IF NOT EXISTS gift_cards(
      code TEXT PRIMARY KEY, initial_amount REAL NOT NULL CHECK(initial_amount>0),
      balance REAL NOT NULL CHECK(balance>=0), recipient_phone TEXT DEFAULT '',
      expires_on TEXT DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS gift_card_transactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, order_id INTEGER,
      amount_change REAL NOT NULL, reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS auto_discount(
      id INTEGER PRIMARY KEY CHECK(id=1), min_amount REAL NOT NULL DEFAULT 0,
      percent INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS delivery_zones(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, fee REAL NOT NULL DEFAULT 0,
      free_from REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS admin_notifications(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL,
      title TEXT NOT NULL,
      body TEXT NOT NULL DEFAULT '',
      target_url TEXT NOT NULL DEFAULT '/admin',
      is_read INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS ix_admin_notifications_read_created
      ON admin_notifications(is_read, created_at DESC);
    CREATE TABLE IF NOT EXISTS hits(key TEXT NOT NULL, ts INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS stock_movements(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      product_id INTEGER,
      product_name TEXT NOT NULL,
      change_qty INTEGER NOT NULL,
      before_qty INTEGER NOT NULL,
      after_qty INTEGER NOT NULL,
      reason TEXT NOT NULL,
      reference TEXT DEFAULT '',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS ix_stock_movements_created ON stock_movements(created_at DESC);
    CREATE TABLE IF NOT EXISTS expenses(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      category TEXT NOT NULL DEFAULT 'Дигар',
      amount REAL NOT NULL CHECK(amount > 0),
      note TEXT DEFAULT '',
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS ix_expenses_created ON expenses(created_at DESC);
    CREATE INDEX IF NOT EXISTS ix_hits ON hits(key, ts);
    CREATE INDEX IF NOT EXISTS ix_prod ON products(active, category);
    """)
    d.executescript("""
    CREATE TABLE IF NOT EXISTS order_status_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
      old_status TEXT DEFAULT '', new_status TEXT NOT NULL,
      changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS ix_order_status_history_order ON order_status_history(order_id, changed_at DESC);
    CREATE TABLE IF NOT EXISTS order_notifications(
      id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
      status TEXT NOT NULL, message TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS faqs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, answer TEXT NOT NULL,
      active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS contact_messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT NOT NULL,
      phone TEXT NOT NULL, subject TEXT NOT NULL, message TEXT NOT NULL,
      is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE INDEX IF NOT EXISTS ix_contact_messages_created ON contact_messages(created_at DESC);
    CREATE INDEX IF NOT EXISTS ix_order_notifications_order ON order_notifications(order_id, id DESC);
    CREATE TABLE IF NOT EXISTS product_reviews(
      id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
      customer_name TEXT NOT NULL DEFAULT 'Муштарӣ', rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
      review_text TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      is_approved INTEGER NOT NULL DEFAULT 1,
      FOREIGN KEY(product_id) REFERENCES products(id) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS ix_reviews_product ON product_reviews(product_id, created_at DESC);
    CREATE TABLE IF NOT EXISTS loyalty_points(
      phone TEXT PRIMARY KEY, customer_name TEXT NOT NULL DEFAULT '', points INTEGER NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS loyalty_transactions(
      id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, order_id INTEGER, points_change INTEGER NOT NULL,
      reason TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(order_id, reason));
    """)
    cols = {r[1] for r in d.execute("PRAGMA table_info(orders)")}
    if "points_redeemed" not in cols:
        d.execute("ALTER TABLE orders ADD COLUMN points_redeemed INTEGER NOT NULL DEFAULT 0")
    for c, t in (("gift_card_code", "TEXT DEFAULT ''"), ("gift_card_amount", "REAL DEFAULT 0")):
        if c not in cols:
            d.execute(f"ALTER TABLE orders ADD COLUMN {c} {t}")
    for c, t in (("discount", "REAL DEFAULT 0"), ("promo", "TEXT DEFAULT ''"), ("shipping_fee", "REAL DEFAULT 0"), ("delivery_zone", "TEXT DEFAULT ''")):
        if c not in cols:
            try:
                d.execute(f"ALTER TABLE orders ADD COLUMN {c} {t}")
            except sqlite3.OperationalError:
                pass
    d.execute("INSERT INTO order_status_history(order_id,old_status,new_status,changed_at) SELECT o.id,'',o.status,o.created_at FROM orders o WHERE NOT EXISTS (SELECT 1 FROM order_status_history h WHERE h.order_id=o.id)")
    pcols = {r[1] for r in d.execute("PRAGMA table_info(products)")}
    if "video" not in pcols:
        d.execute("ALTER TABLE products ADD COLUMN video TEXT DEFAULT ''")
    if "cost_price" not in pcols:
        d.execute("ALTER TABLE products ADD COLUMN cost_price REAL NOT NULL DEFAULT 0")
    # Compatibility migration for databases created by earlier versions.
    review_cols = {r[1] for r in d.execute("PRAGMA table_info(product_reviews)").fetchall()}
    if "is_approved" not in review_cols:
        # Existing reviews remain public for backward compatibility.
        d.execute("ALTER TABLE product_reviews ADD COLUMN is_approved INTEGER NOT NULL DEFAULT 1")
    d.execute("CREATE INDEX IF NOT EXISTS ix_reviews_approval ON product_reviews(is_approved, id DESC)")
    promo_cols = {r[1] for r in d.execute("PRAGMA table_info(promos)").fetchall()}
    if "expires_on" not in promo_cols:
        d.execute("ALTER TABLE promos ADD COLUMN expires_on TEXT DEFAULT ''")
    d.execute("INSERT OR IGNORE INTO auto_discount(id,min_amount,percent,active) VALUES(1,0,0,0)")
    d.execute("INSERT OR IGNORE INTO delivery_zones(name,fee,free_from,active) VALUES('Дар шаҳр',15,500,1)")
    d.execute("INSERT OR IGNORE INTO delivery_zones(name,fee,free_from,active) VALUES('Берун аз шаҳр',30,800,1)")
    if d.execute('SELECT COUNT(*) FROM faqs').fetchone()[0] == 0:
        d.executemany('INSERT INTO faqs(question,answer,sort_order) VALUES(?,?,?)', [
            ('Чӣ тавр фармоиш диҳам?', 'Маҳсулотро интихоб карда, ба сабад илова кунед ва дар саҳифаи расмикунонӣ маълумоти худро нависед.', 1),
            ('Чӣ тавр фармоишро пайгирӣ кунам?', 'Ба саҳифаи «Пайгирӣ» даромада, рақами фармоиш ва телефони истифодашударо ворид кунед.', 2),
            ('Доставка чанд пул аст?', 'Нархи доставка вобаста ба минтақа аст. Нарх дар расмикунонии фармоиш нишон дода мешавад.', 3),
            ('Оё маҳсулотро баргардондан мумкин аст?', 'Барои масъалаи баргардонидан бо мағоза тавассути алоқа муроҷиат кунед.', 4),
            ('Чӣ тавр тахфиф истифода кунам?', 'Агар промокод дошта бошед, онро дар сабад ворид кунед. Тахфифҳои фаъол низ метавонанд худкор ҳисоб шаванд.', 5)
        ])
    d.commit()
    d.close()


def loyalty_balance(d, phone):
    row = d.execute("SELECT points FROM loyalty_points WHERE phone=?", (phone,)).fetchone()
    return int(row["points"] if row else 0)

def apply_loyalty_change(d, phone, name, order_id, delta, reason):
    if not phone or not delta:
        return
    # Avoid duplicate accrual/reversal for the same order and reason.
    cur = d.execute("INSERT OR IGNORE INTO loyalty_transactions(phone,order_id,points_change,reason) VALUES(?,?,?,?)",
                    (phone, order_id, int(delta), reason))
    if cur.rowcount != 1:
        return
    d.execute("""INSERT INTO loyalty_points(phone,customer_name,points) VALUES(?,?,?) ON CONFLICT(phone) DO UPDATE SET
              customer_name=excluded.customer_name, points=MAX(0, loyalty_points.points+excluded.points),
              updated_at=CURRENT_TIMESTAMP""", (phone, name, int(delta)))



try:
    init_db()
    logger.info("NEXVORA database initialized successfully")
except Exception:
    logger.exception("NEXVORA database initialization failed")
    raise RuntimeError("Базаи маълумот оғоз нашуд. Файли error.log-ро санҷед.")

# V72: saved customer delivery addresses. Phone number is the ownership key.
with sqlite3.connect(DB, timeout=30) as _address_db:
    _address_db.execute("PRAGMA busy_timeout=30000")
    _address_db.execute("""CREATE TABLE IF NOT EXISTS customer_addresses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL, is_default INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    _address_db.execute("CREATE INDEX IF NOT EXISTS idx_customer_addresses_phone ON customer_addresses(phone)")

def db():
    if "db" not in g:
        con = sqlite3.connect(DB, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA synchronous=NORMAL")
        g.db = con
    return g.db


def log_stock_movement(conn, product_id, product_name, before_qty, after_qty, reason, reference=""):
    """Record a stock quantity change. Call inside the same transaction when possible."""
    before_qty, after_qty = int(before_qty), int(after_qty)
    delta = after_qty - before_qty
    if delta:
        conn.execute(
            """INSERT INTO stock_movements
            (product_id,product_name,change_qty,before_qty,after_qty,reason,reference)
            VALUES(?,?,?,?,?,?,?)""",
            (product_id, str(product_name)[:120], delta, before_qty, after_qty,
             str(reason)[:80], str(reference)[:80])
        )


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
            session.pop("admin", None)
            return redirect(url_for("login"))
        # V68: sliding inactivity timeout; active admins stay signed in,
        # but an idle session expires after ADMIN_TTL seconds.
        session["admin"] = int(time.time())
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
        promo = db().execute("SELECT * FROM promos WHERE code=? AND active=1 AND (max_uses=0 OR uses<max_uses) AND (expires_on='' OR expires_on>=date('now'))", (code,)).fetchone()
        if promo:
            discount = round(sub * promo["percent"] / 100, 2)
        else:
            session.pop("promo", None)
    auto = db().execute("SELECT * FROM auto_discount WHERE id=1").fetchone()
    auto_used = False
    if not promo and auto and auto["active"] and auto["percent"] > 0 and sub >= auto["min_amount"]:
        discount = round(sub * auto["percent"] / 100, 2)
        auto_used = True
    return dict(items=items, subtotal=sub, discount=discount, total=round(sub - discount, 2), promo=promo,
                auto_discount=auto, auto_used=auto_used)


def get_favorites():
    raw = session.get("favorites", [])
    clean = []
    if isinstance(raw, list):
        for v in raw[:100]:
            try:
                pid = int(v)
                if pid > 0 and pid not in clean:
                    clean.append(pid)
            except (TypeError, ValueError):
                pass
    session["favorites"] = clean
    return clean


@app.context_processor
def inject():
    return dict(cart_count=sum(get_cart().values()), favorites_count=len(get_favorites()), compare_count=len(session.get("compare", [])), money=money, eff=eff,
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
        cost = float(f.get("cost_price", "0") or 0)
        stock = int(f.get("stock", "0") or 0)
        sr = f.get("sale", "").strip()
        sale = float(sr) if sr else None
    except ValueError:
        return None
    if (not name or not math.isfinite(pr) or not 0 < pr <= 1e7
            or not math.isfinite(cost) or not 0 <= cost <= pr
            or not 0 <= stock <= 1_000_000):
        return None
    if sale is not None and (not math.isfinite(sale) or sale <= 0 or sale >= pr):
        return None
    return dict(name=name, cat=cat, desc=desc, price=pr, cost_price=cost,
                stock=stock, sale=sale, featured=1 if f.get("featured") else 0)


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
<nav class="nav"><a href="/">Мағоза</a><a href="/faq">Саволҳо</a><a href="/contact">Тамос</a><a href="/track">Пайгирӣ</a><a href="/account">👤 Кабинет</a><a href="/favorites">❤️ Дӯстдошта{% if favorites_count %}<b>{{favorites_count}}</b>{% endif %}</a><a href="/compare">⚖️ Муқоиса{% if compare_count %}<b>{{compare_count}}</b>{% endif %}</a><a class="cartlink" href="/cart">Сабад{% if cart_count %}<b>{{cart_count}}</b>{% endif %}</a>{% if admin %}<a href="/admin">Admin</a>{% endif %}</nav></div></header>
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
<div class="actions"><form method="post" action="/favorites/toggle/{{p.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input type="hidden" name="next" value="{{request.full_path.rstrip('?')}}"><button class="btn dark sm" aria-label="Ба дӯстдоштаҳо">❤️</button></form><form method="post" action="/cart/add/{{p.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input type="hidden" name="next" value="{{request.full_path.rstrip('?')}}"><button class="btn sm block" {% if p.stock<1 %}disabled{% endif %}>+ Ба сабад</button></form><form method="post" action="/compare/add/{{p.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input type="hidden" name="next" value="{{request.full_path.rstrip('?')}}"><button class="btn dark sm" title="Муқоиса">⚖️</button></form><a class="btn dark sm" href="/product/{{p.id}}">Дидан</a></div></div></article>{% endmacro %}"""

HOME = CARD + """{% if banner %}<section class="wrap" style="padding-top:12px"><div class="panel" style="overflow:hidden;padding:0;position:relative;min-height:150px;background:linear-gradient(110deg,#17191f,#292018);display:flex;align-items:center;gap:16px;flex-wrap:wrap">{% if banner.image %}<img src="{{url_for('static',filename='uploads/'+banner.image)}}" alt="{{banner.title}}" style="width:100%;max-height:330px;object-fit:cover">{% endif %}<div style="padding:18px;flex:1;min-width:220px"><h2 class="gold" style="margin-top:0">{{banner.title}}</h2>{% if banner.subtitle %}<p class="muted">{{banner.subtitle}}</p>{% endif %}{% if banner.button_text and banner.button_url %}<a class="btn" href="{{banner.button_url}}">{{banner.button_text}}</a>{% endif %}</div></div></section>{% endif %}<main class="wrap"><section class="hero"><h1>Интихоби беҳтарин — дар як ҷо.</h1><p class="muted">Маҳсулотро интихоб кунед, ба сабад илова намоед ва фармоишро тавассути WhatsApp фиристед.</p><a class="btn" href="#products">Харид кардан</a> <a class="btn ghost" href="/track">Фармоишро пайгирӣ кардан</a></section>
<div class="chips" id="categories"><a class="chip {{'on' if not cat}}" href="/">Ҳама</a>{% for c in cats %}<a class="chip {{'on' if c==cat}}" href="/?category={{c|urlencode}}">{{c}}</a>{% endfor %}</div>
<form class="search" method="get" id="products" style="display:grid;gap:8px">{% if cat %}<input type="hidden" name="category" value="{{cat}}">{% endif %}<input class="field" name="q" value="{{q}}" placeholder="Ҷустуҷӯи маҳсулот" maxlength="80"><div style="display:flex;gap:8px;flex-wrap:wrap"><input class="field" name="min_price" type="number" min="0" step="0.01" value="{{min_price}}" placeholder="Нархи аз" style="max-width:140px"><input class="field" name="max_price" type="number" min="0" step="0.01" value="{{max_price}}" placeholder="Нархи то" style="max-width:140px"><select class="field" name="sort"><option value="new">Навтарин</option><option value="low" {{'selected' if sort=='low'}}>Арзонтарин</option><option value="high" {{'selected' if sort=='high'}}>Қиматтарин</option></select></div><div style="display:flex;gap:12px;flex-wrap:wrap"><label><input type="checkbox" name="sale" value="1" {{'checked' if sale}}> Танҳо тахфифдор</label><label><input type="checkbox" name="new_only" value="1" {{'checked' if new_only}}> Нав (30 рӯзи охир)</label></div><button class="btn">Филтр кардан</button><a class="btn ghost" href="/">Тоза кардани филтрҳо</a></form>
<div class="grid">{% for p in products %}{{card(p)}}{% else %}<div class="panel" style="grid-column:1/-1">Маҳсулот ёфт нашуд. Ҷустуҷӯро тағйир диҳед.</div>{% endfor %}</div>
{% if pages>1 %}<div class="pager">{% for n in range(1,pages+1) %}<a class="{{'on' if n==pg}}" href="/?{{qs}}{{'&' if qs}}page={{n}}">{{n}}</a>{% endfor %}</div>{% endif %}</main>"""


@app.route("/healthz")
def healthz():
    """Minimal service check for hosting platforms; never returns secrets or paths."""
    try:
        check = sqlite3.connect(DB, timeout=5)
        try:
            result = check.execute("PRAGMA quick_check").fetchone()
        finally:
            check.close()
        healthy = bool(result and result[0] == "ok")
        payload = {"status": "ok" if healthy else "degraded", "database": "ok" if healthy else "error", "version": "V75"}
        return Response(json.dumps(payload, ensure_ascii=False), status=200 if healthy else 503,
                        mimetype="application/json")
    except Exception:
        logger.exception("Health check failed")
        payload = {"status": "degraded", "database": "error", "version": "V75"}
        return Response(json.dumps(payload, ensure_ascii=False), status=503,
                        mimetype="application/json")


@app.route("/")
def home():
    q = request.args.get("q", "").strip()[:80]
    cat = request.args.get("category", "").strip()[:80]
    sort = request.args.get("sort", "new")
    if sort not in ("new", "low", "high"):
        sort = "new"
    sale = request.args.get("sale") == "1"
    new_only = request.args.get("new_only") == "1"
    def price_arg(key):
        raw = request.args.get(key, "").strip()
        try:
            val = float(raw)
            return val if 0 <= val <= 100000000 else None
        except (ValueError, TypeError):
            return None
    min_price, max_price = price_arg("min_price"), price_arg("max_price")
    if min_price is not None and max_price is not None and min_price > max_price:
        min_price, max_price = max_price, min_price
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
    if sale:
        where += " AND sale_price IS NOT NULL AND sale_price>0 AND sale_price<price"
    if new_only:
        where += " AND created_at >= datetime('now','-30 days')"
    effective = "CASE WHEN sale_price>0 AND sale_price<price THEN sale_price ELSE price END"
    if min_price is not None:
        where += f" AND {effective}>=?"
        args.append(min_price)
    if max_price is not None:
        where += f" AND {effective}<=?"
        args.append(max_price)
    d = db()
    total = d.execute(f"SELECT COUNT(*) FROM products WHERE {where}", args).fetchone()[0]
    pages = max(1, -(-total // PER_PAGE))
    pg = min(pg, pages)
    products = d.execute(f"SELECT * FROM products WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                         args + [PER_PAGE, (pg - 1) * PER_PAGE]).fetchall()
    cats = [r[0] for r in d.execute("SELECT DISTINCT category FROM products WHERE active=1 ORDER BY category")]
    now = __import__("datetime").date.today().isoformat()
    banner = d.execute("SELECT * FROM banners WHERE id=1 AND active=1 AND (start_date='' OR start_date<=?) AND (end_date='' OR end_date>=?)", (now, now)).fetchone()
    qs = urlencode({k: v for k, v in (("q", q), ("category", cat), ("sort", sort if sort != "new" else ""), ("min_price", min_price if min_price is not None else ""), ("max_price", max_price if max_price is not None else ""), ("sale", "1" if sale else ""), ("new_only", "1" if new_only else "")) if v})
    return page(HOME, products=products, cats=cats, q=q, cat=cat, sort=sort, pg=pg, pages=pages, qs=qs, min_price=min_price if min_price is not None else "", max_price=max_price if max_price is not None else "", sale=sale, new_only=new_only, banner=banner)


# V65: lightweight session-based product comparison (maximum 4 items).
@app.route("/compare")
def compare_page():
    ids = session.get("compare", [])
    if not isinstance(ids, list):
        ids = []
    ids = list(dict.fromkeys(int(x) for x in ids if str(x).isdigit()))[:4]
    session["compare"] = ids
    products = []
    if ids:
        rows = db().execute(f"SELECT * FROM products WHERE active=1 AND id IN ({','.join('?' * len(ids))})", ids).fetchall()
        by_id = {p["id"]: p for p in rows}
        products = [by_id[i] for i in ids if i in by_id]
    body = r'''<main class="wrap"><h1>⚖️ Муқоисаи маҳсулот</h1><p class="muted">Нарх, категория, тавсиф ва дастрасиро якҷоя муқоиса кунед. То 4 маҳсулот.</p>
{% if products %}<div style="overflow-x:auto"><table style="width:100%;min-width:600px;border-collapse:collapse"><thead><tr><th style="padding:12px;text-align:left">Хусусият</th>{% for p in products %}<th style="padding:12px;text-align:left;min-width:180px"><a href="/product/{{p.id}}">{{p.name}}</a><div class="photo" style="height:120px;margin-top:8px">{% if p.image %}<img src="{{url_for('static',filename='uploads/'+p.image)}}" alt="{{p.name}}">{% else %}NEXVORA{% endif %}</div><form method="post" action="/compare/remove/{{p.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm" style="margin-top:6px">Хориҷ кардан</button></form></th>{% endfor %}</tr></thead><tbody>
<tr><td style="padding:12px;border-bottom:1px solid var(--line)"><b>Нарх</b></td>{% for p in products %}<td style="padding:12px;border-bottom:1px solid var(--line)"><b class="gold">{{money(eff(p))}} сомонӣ</b>{% if p.sale_price and p.sale_price>0 and p.sale_price<p.price %}<div class="old">{{money(p.price)}} сомонӣ</div>{% endif %}</td>{% endfor %}</tr>
<tr><td style="padding:12px;border-bottom:1px solid var(--line)"><b>Категория</b></td>{% for p in products %}<td style="padding:12px;border-bottom:1px solid var(--line)">{{p.category or '—'}}</td>{% endfor %}</tr>
<tr><td style="padding:12px;border-bottom:1px solid var(--line)"><b>Тавсиф</b></td>{% for p in products %}<td style="padding:12px;border-bottom:1px solid var(--line)">{{p.description or '—'}}</td>{% endfor %}</tr>
<tr><td style="padding:12px;border-bottom:1px solid var(--line)"><b>Мавҷудият</b></td>{% for p in products %}<td style="padding:12px;border-bottom:1px solid var(--line)">{% if p.stock>0 %}Дар анбор: {{p.stock}}{% else %}<span class="pill bad">Тамом шуд</span>{% endif %}</td>{% endfor %}</tr>
<tr><td style="padding:12px"><b>Фарқ аз нархи пасттарин</b></td>{% for p in products %}<td style="padding:12px">{% if p.id == cheapest_id %}<span class="pill">Арзонтарин</span>{% else %}{{money(eff(p)-lowest)}} сомонӣ{% endif %}</td>{% endfor %}</tr>
</tbody></table></div><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px"><a class="btn" href="/compare/export.csv">⬇️ Боргирии муқоиса (CSV)</a><form method="post" action="/compare/clear"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark">Тоза кардани рӯйхат</button></form></div>{% else %}<div class="panel">Ҳоло маҳсулот барои муқоиса интихоб нашудааст. Ба мағоза равед ва тугмаи ⚖️-ро зер кунед.</div>{% endif %}<a class="btn" href="/">Ба мағоза</a></main>'''
    lowest = min((eff(p) for p in products), default=0)
    cheapest_id = next((p["id"] for p in products if eff(p)==lowest), None)
    return page(body, title="Муқоисаи маҳсулот", products=products, lowest=lowest, cheapest_id=cheapest_id)

@app.route("/compare/export.csv")
def compare_export_csv():
    """Export only the current visitor's selected, active products."""
    ids = session.get("compare", [])
    if not isinstance(ids, list):
        ids = []
    ids = list(dict.fromkeys(int(x) for x in ids if str(x).isdigit()))[:4]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["ID", "Маҳсулот", "Категория", "Нархи ҷорӣ (сомонӣ)", "Нархи пешина (сомонӣ)", "Захира", "Тавсиф"])
    if ids:
        marks = ",".join("?" for _ in ids)
        rows = db().execute(f"SELECT * FROM products WHERE active=1 AND id IN ({marks})", ids).fetchall()
        by_id = {p["id"]: p for p in rows}
        for pid in ids:
            p = by_id.get(pid)
            if not p:
                continue
            writer.writerow([p["id"], p["name"], p["category"] or "", money(eff(p)), money(p["price"]), p["stock"], p["description"] or ""])
    response = Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8")
    response.headers["Content-Disposition"] = "attachment; filename=nexvora_comparison.csv"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/compare/add/<int:pid>", methods=["POST"])
def compare_add(pid):
    product = db().execute("SELECT id,active FROM products WHERE id=?", (pid,)).fetchone()
    if not product or not product["active"]:
        abort(404)
    ids = session.get("compare", [])
    if not isinstance(ids, list): ids = []
    ids = [int(x) for x in ids if str(x).isdigit() and int(x)!=pid]
    if len(ids) >= 4:
        flash("Муқоиса то 4 маҳсулот маҳдуд аст. Ягон маҳсулотро хориҷ кунед.", "warn")
    else:
        ids.insert(0, pid)
        session["compare"] = ids
        flash("Маҳсулот ба муқоиса илова шуд.", "ok")
    return redirect(safe_next("/compare"))

@app.route("/compare/remove/<int:pid>", methods=["POST"])
def compare_remove(pid):
    ids = session.get("compare", [])
    if isinstance(ids, list):
        session["compare"] = [int(x) for x in ids if str(x).isdigit() and int(x)!=pid]
    flash("Маҳсулот аз муқоиса хориҷ шуд.", "ok")
    return redirect("/compare")

@app.route("/compare/clear", methods=["POST"])
def compare_clear():
    session["compare"] = []
    flash("Рӯйхати муқоиса тоза шуд.", "ok")
    return redirect("/compare")


@app.route("/favorites")
def favorites_page():
    ids = get_favorites()
    products = []
    if ids:
        rows = db().execute(f"SELECT * FROM products WHERE active=1 AND id IN ({','.join('?' * len(ids))})", ids).fetchall()
        by_id = {p["id"]: p for p in rows}
        products = [by_id[i] for i in ids if i in by_id]
    body = CARD + """<main class="wrap"><h1>❤️ Дӯстдоштаҳо</h1><p class="muted">Маҳсулотҳои интихобкардаатон.</p><div class="grid">{% for p in products %}{{card(p)}}{% else %}<div class="panel">Ҳоло маҳсулот илова нашудааст. <a href="/">Ба мағоза</a></div>{% endfor %}</div></main>"""
    return page(body, title="Дӯстдоштаҳо", products=products)


@app.route("/favorites/toggle/<int:pid>", methods=["POST"])
def favorites_toggle(pid):
    product = db().execute("SELECT id,active FROM products WHERE id=?", (pid,)).fetchone()
    if not product or not product["active"]:
        abort(404)
    ids = get_favorites()
    if pid in ids:
        ids.remove(pid)
        flash("Маҳсулот аз дӯстдоштаҳо хориҷ шуд.", "ok")
    else:
        if len(ids) >= 100:
            flash("Рӯйхати дӯстдоштаҳо то 100 маҳсулот маҳдуд аст.", "warn")
        else:
            ids.insert(0, pid)
            flash("Маҳсулот ба дӯстдоштаҳо илова шуд.", "ok")
    session["favorites"] = ids
    return redirect(safe_next("/favorites"))


@app.route("/product/<int:pid>")
def product_detail(pid):
    p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        abort(404)
    # V50: recommendations rank same-category items first, then featured/new items.
    rel = db().execute("""SELECT * FROM products
        WHERE active=1 AND id!=? AND stock>=0
        ORDER BY CASE WHEN category=? THEN 0 ELSE 1 END,
                 CASE WHEN featured=1 THEN 0 ELSE 1 END,
                 created_at DESC, id DESC
        LIMIT 4""", (pid, p["category"])).fetchall()
    reviews = db().execute("SELECT * FROM product_reviews WHERE product_id=? AND is_approved=1 ORDER BY id DESC LIMIT 100", (pid,)).fetchall()
    questions = db().execute("SELECT * FROM product_questions WHERE product_id=? AND is_public=1 ORDER BY id DESC LIMIT 50", (pid,)).fetchall()
    rating_row = db().execute("SELECT COUNT(*) AS n, AVG(rating) AS avg FROM product_reviews WHERE product_id=? AND is_approved=1", (pid,)).fetchone()
    detail = CARD + """<main class="wrap"><div class="cols"><div><div class="panel"><div class="photo" style="height:340px;border-radius:12px">{% if p.image %}<img src="{{url_for('static',filename='uploads/'+p.image)}}" alt="{{p.name}}">{% else %}NEXVORA{% endif %}</div></div>
{% if p.video %}<div class="panel"><h2 class="gold">Видеои маҳсулот 🎥</h2><video controls playsinline preload="metadata" style="width:100%;max-height:450px;border-radius:14px;background:#000"><source src="{{url_for('static',filename='uploads/'+p.video)}}" type="{{'video/webm' if p.video.endswith('.webm') else 'video/mp4'}}">Браузери шумо видео намоиш дода наметавонад.</video></div>{% endif %}</div>
<div class="panel"><p class="muted">{{p.category}}</p><h1>{{p.name}}</h1><p><span class="price" style="font-size:28px">{{money(eff(p))}} сомонӣ</span>{% if p.sale_price and p.sale_price>0 %}<span class="old">{{money(p.price)}}</span>{% endif %}</p><p class="muted" style="font-size:15px">{{p.description}}</p>
<p>{% if p.stock<1 %}<span class="pill bad">Тамом шуд</span>{% elif p.stock<=5 %}<span class="pill warn">Танҳо {{p.stock}} дона монд</span>{% else %}<span class="pill">Дар анбор: {{p.stock}}</span>{% endif %}</p>
<form method="post" action="/cart/add/{{p.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input type="hidden" name="next" value="{{request.path}}"><button class="btn block" {% if p.stock<1 %}disabled{% endif %}>Ба сабад илова кардан</button></form><form method="post" action="/compare/add/{{p.id}}" style="margin-top:8px"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input type="hidden" name="next" value="{{request.path}}"><button class="btn dark block">⚖️ Ба муқоиса илова кардан</button></form>
<a class="btn green block" style="margin-top:10px" href="https://wa.me/{{phone}}?text={{('Салом! Дар бораи '+p.name+' маълумот мехоҳам.')|urlencode}}">Савол дар WhatsApp</a></div></div>
<section class="panel" id="reviews" style="margin-top:18px"><h2>⭐ Рейтинг ва фикру мулоҳиза</h2><p class="gold"><b>{{'%.1f'|format(rating_avg or 0)}} / 5</b> · {{review_count}} фикр</p><form method="post" action="/product/{{p.id}}/review"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Номи шумо (ихтиёрӣ)</label><input class="field" name="customer_name" maxlength="60" placeholder="Муштарӣ"><label>Рейтинг</label><select class="field" name="rating" required><option value="5">★★★★★ — Аъло</option><option value="4">★★★★ — Хуб</option><option value="3">★★★ — Миёна</option><option value="2">★★ — Бад</option><option value="1">★ — Хеле бад</option></select><label>Фикри шумо</label><textarea class="field" name="review_text" rows="3" maxlength="500" placeholder="Фикратонро нависед (ихтиёрӣ)"></textarea><button class="btn" style="margin-top:10px">Сабти рейтинг</button><p class="muted">Фикри нав баъди санҷиши маъмур нашр мешавад.</p></form><h3>Фикрҳои муштариён</h3>{% for rv in reviews %}<div class="panel" style="margin:10px 0"><b>{{rv.customer_name}}</b> <span class="gold">{{'★' * rv.rating}}{{'☆' * (5-rv.rating)}}</span><div class="muted">{{rv.created_at}}</div>{% if rv.review_text %}<p>{{rv.review_text}}</p>{% endif %}</div>{% else %}<p class="muted">Ҳоло фикре нест.</p>{% endfor %}</section>
<section class="panel" id="questions" style="margin-top:18px"><h2>❓ Саволҳо дар бораи маҳсулот</h2><form method="post" action="/product/{{p.id}}/question"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Номи шумо</label><input class="field" name="customer_name" maxlength="60" placeholder="Муштарӣ"><label>Саволи шумо</label><textarea class="field" name="question" rows="3" maxlength="500" required placeholder="Дар бораи маҳсулот пурсед..."></textarea><button class="btn" style="margin-top:8px">Фиристодани савол</button></form><h3>Ҷавобҳои мағоза</h3>{% for q in questions %}<div class="panel" style="margin:10px 0"><b>{{q.customer_name}}</b><p>{{q.question}}</p>{% if q.answer %}<p class="gold"><b>Ҷавоби мағоза:</b> {{q.answer}}</p>{% endif %}<small class="muted">{{q.created_at}}</small></div>{% else %}<p class="muted">Ҳоло саволи нашршуда нест.</p>{% endfor %}</section>
{% if rel %}<h2>✨ Шояд ин ҳам писандатон ояд</h2><p class="muted">Маҳсулоти монанд, нав ва интихобшуда</p><div class="grid">{% for r in rel %}{{card(r)}}{% endfor %}</div>{% endif %}</main>"""
    return page(detail, title=p["name"], p=p, rel=rel, phone=WHATSAPP_NUMBER, reviews=reviews, rating_avg=rating_row["avg"], review_count=rating_row["n"], questions=questions)


CART = """<main class="wrap" style="max-width:820px"><h1>Сабади харид</h1>
{% for x in items %}<div class="panel line"><div class="thumb">{% if x.product.image %}<img src="{{url_for('static',filename='uploads/'+x.product.image)}}" alt="">{% endif %}</div><div><b>{{x.product.name}}</b><div class="muted">{{money(x.price)}} × {{x.quantity}}</div></div><b class="gold">{{money(x.subtotal)}}</b>
<div class="qty">{% for action,label in [('minus','−'),('add','+'),('remove','Нест кардан')] %}<form method="post" action="/cart/{{action}}/{{x.product.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">{{label}}</button></form>{% endfor %}</div></div>
{% else %}<div class="panel center">Сабад холӣ аст. <a class="gold" href="/">Ба мағоза равед</a></div>{% endfor %}
{% if items %}<div class="panel"><form class="inline" method="post" action="/cart/promo"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input class="field" name="code" placeholder="Промокод" maxlength="30" value="{{promo.code if promo}}"><button class="btn dark">Татбиқ</button></form>
<div class="sum" style="margin-top:12px"><span>Маблағи маҳсулот</span><span>{{money(subtotal)}}</span></div>{% if discount %}<div class="sum"><span>Тахфиф {% if promo %}({{promo.code}} −{{promo.percent}}%){% else %}(тахфифи автоматӣ){% endif %}</span><span>−{{money(discount)}}</span></div>{% endif %}<div class="sum big"><span>Ҳамагӣ</span><span>{{money(total)}} сомонӣ</span></div>
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
    elif db().execute("SELECT 1 FROM promos WHERE code=? AND active=1 AND (max_uses=0 OR uses<max_uses) AND (expires_on='' OR expires_on>=date('now'))", (code,)).fetchone():
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
<label>Ному насаб</label><input class="field" name="name" required minlength="2" maxlength="100" autocomplete="name"><label>Телефон</label><input class="field" name="phone" required maxlength="30" placeholder="+992..." inputmode="tel" autocomplete="tel"><label>Купони шахсӣ (ихтиёрӣ)</label><input class="field" name="personal_code" maxlength="30" placeholder="Коде, ки ба телефони шумо дода шудааст"><small class="muted">Купон танҳо ба рақами телефон вобаста аст ва як маротиба истифода мешавад.</small><label>Сертификати тӯҳфа (ихтиёрӣ)</label><input class="field" name="gift_card_code" maxlength="32" placeholder="Коди Gift Card"><small class="muted">Маблағи сертификат аз ҳисоб кам карда мешавад.</small><label>Бонус барои тахфиф (хол)</label><input class="field" name="points_to_use" type="number" min="0" step="1" value="0" inputmode="numeric"><small class="muted">10 хол = 1 сомонӣ тахфиф. То 20% маблағи маҳсулот истифода мешавад; бонус ба рақами телефони шумо вобаста аст.</small><label>Минтақаи доставка</label><select class="field" name="zone_id" required>{% for z in delivery_zones %}<option value="{{z.id}}">{{z.name}} — {{money(z.fee)}} сомонӣ{% if z.free_from>0 %} (ройгон аз {{money(z.free_from)}}){% endif %}</option>{% endfor %}</select><label>Суроға (ихтиёрӣ)</label><input class="field" name="address" maxlength="250" list="saved-address-list"><datalist id="saved-address-list">{% for a in saved_addresses %}<option value="{{a.address}}">{{a.label}}</option>{% endfor %}</datalist><label>Шарҳ (ихтиёрӣ)</label><textarea class="field" name="note" rows="3" maxlength="500"></textarea>
<div class="sum"><span>Доставка</span><span>{{money(shipping_fee)}} сомонӣ</span></div><div class="muted">Бонус: 10 хол = 1 сомонӣ тахфиф, ҳадди истифода 20% аз маблағи маҳсулот.</div>{% if discount %}<div class="sum" style="margin-top:12px"><span>Тахфиф {% if promo %}({{promo.code}}){% else %}(автоматӣ){% endif %}</span><span>−{{money(discount)}}</span></div>{% endif %}<div class="sum big"><span>Ҳамагӣ</span><span>{{money(total)}} сомонӣ</span></div><button class="btn block" style="margin-top:14px">Сабти фармоиш</button></form></div></main>"""


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    ci = cart_info()
    if not ci["items"]:
        return redirect("/cart")
    if request.method == "GET":
        zones = db().execute("SELECT * FROM delivery_zones WHERE active=1 ORDER BY id").fetchall()
        first_fee = float(zones[0]["fee"] or 0) if zones else 0.0
        if zones and zones[0]["free_from"] and ci["subtotal"] >= zones[0]["free_from"]: first_fee = 0.0
        ci.update(delivery_zones=zones, shipping_fee=first_fee, total=round(ci["total"]+first_fee,2))
        saved_addresses = []
        account_phone = session.get("customer_phone")
        if account_phone:
            saved_addresses = db().execute("SELECT label,address FROM customer_addresses WHERE phone=? ORDER BY is_default DESC,id DESC", (account_phone,)).fetchall()
        return page(CHECKOUT, title="Фармоиш", saved_addresses=saved_addresses, **ci)
    if request.form.get("website"):
        return redirect("/")
    if limited("order:" + ip(), 5, 600):
        abort(429, description="Фармоишҳо хеле зуд-зуд мераванд. Якчанд дақиқа интизор шавед.")
    name = request.form.get("name", "").strip()[:100]
    phone = request.form.get("phone", "").strip()[:30]
    address = request.form.get("address", "").strip()[:250]
    note = request.form.get("note", "").strip()[:500]
    try:
        zone_id = int(request.form.get("zone_id", "0"))
    except (TypeError, ValueError):
        zone_id = 0
    zone = db().execute("SELECT * FROM delivery_zones WHERE id=? AND active=1", (zone_id,)).fetchone()
    if not zone:
        flash("Минтақаи доставка интихоб нашудааст.", "err")
        return redirect("/checkout")
    if len(name) < 2 or not re.fullmatch(r"\+?[0-9() -]{7,30}", phone) or len(re.sub(r"\D", "", phone)) < 7:
        flash("Ном ё рақами телефон нодуруст аст.", "err")
        return redirect("/checkout")
    d = db()
    d.execute("BEGIN IMMEDIATE")
    try:
        order_items = []
        for x in ci["items"]:
            q, pid = x["quantity"], x["product"]["id"]
            p_before = d.execute("SELECT name,stock FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            cur = d.execute("UPDATE products SET stock=stock-? WHERE id=? AND active=1 AND stock>=?", (q, pid, q))
            if cur.rowcount != 1:
                raise ValueError("stock")
            log_stock_movement(d, pid, p_before["name"], p_before["stock"], p_before["stock"] - q,
                               "Фурӯш", "Сабади харид")
            p = d.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
            pr = eff(p)
            order_items.append({"id": pid, "name": p["name"], "quantity": q, "price": pr,
                                "cost_price": float(p["cost_price"] or 0), "subtotal": pr * q})
        sub = sum(i["subtotal"] for i in order_items)
        discount, code = 0.0, ""
        pcode = session.get("promo")
        if pcode:
            cur = d.execute("UPDATE promos SET uses=uses+1 WHERE code=? AND active=1 AND (max_uses=0 OR uses<max_uses) AND (expires_on='' OR expires_on>=date('now'))", (pcode,))
            if cur.rowcount == 1:
                pc = d.execute("SELECT percent FROM promos WHERE code=?", (pcode,)).fetchone()
                discount, code = round(sub * pc["percent"] / 100, 2), pcode
        personal_code = re.sub(r"[^A-Z0-9_-]", "", request.form.get("personal_code", "").strip().upper())[:30]
        normalized_phone = re.sub(r"\D", "", phone)
        personal_coupon = None
        if personal_code and discount == 0:
            personal_coupon = d.execute("SELECT * FROM personal_coupons WHERE code=? AND active=1 AND used_order_id IS NULL AND (expires_on='' OR expires_on>=date('now'))", (personal_code,)).fetchone()
            if personal_coupon and re.sub(r"\D", "", personal_coupon["phone"]) == normalized_phone:
                discount, code = round(sub * personal_coupon["percent"] / 100, 2), personal_code
            elif personal_coupon:
                flash("Купони шахсӣ ба ин рақами телефон тааллуқ надорад.", "err")
            elif personal_code:
                flash("Купони шахсӣ нодуруст аст, истифода шудааст ё мӯҳлаташ гузаштааст.", "err")
        if discount == 0:
            ad = d.execute("SELECT min_amount,percent,active FROM auto_discount WHERE id=1").fetchone()
            if ad and ad["active"] and ad["percent"] > 0 and sub >= ad["min_amount"]:
                discount, code = round(sub * ad["percent"] / 100, 2), "AUTO" + str(ad["percent"])
        shipping_fee = float(zone["fee"] or 0)
        if zone["free_from"] and sub >= float(zone["free_from"]):
            shipping_fee = 0.0
        try:
            requested_points = max(0, min(1000000, int(request.form.get("points_to_use", "0") or 0)))
        except (TypeError, ValueError):
            requested_points = 0
        available_points = loyalty_balance(d, phone)
        # 10 points = 1 somoni; redemption capped at 20% of product subtotal and remaining payable product amount.
        max_bonus_discount = min(round(sub * 0.20, 2), max(0.0, sub - discount))
        max_points_by_cap = int(max_bonus_discount * 10)
        points_used = min(requested_points, available_points, max_points_by_cap)
        bonus_discount = round(points_used / 10.0, 2)
        total_before_gift = round(sub - discount - bonus_discount + shipping_fee, 2)
        gift_code = re.sub(r"[^A-Z0-9_-]", "", request.form.get("gift_card_code", "").strip().upper())[:32]
        gift_card = None
        gift_amount = 0.0
        if gift_code:
            gift_card = d.execute("SELECT * FROM gift_cards WHERE code=? AND active=1 AND balance>0 AND (expires_on='' OR expires_on>=date('now'))", (gift_code,)).fetchone()
            if gift_card and gift_card["recipient_phone"] and re.sub(r"\D", "", gift_card["recipient_phone"]) != normalized_phone:
                gift_card = None
                flash("Ин сертификат ба рақами телефони дигар дода шудааст.", "err")
            elif not gift_card:
                flash("Сертификат нодуруст, ғайрифаъол ё мӯҳлаташ гузаштааст.", "err")
            if gift_card:
                gift_amount = min(float(gift_card["balance"]), max(0.0, total_before_gift))
        total = round(max(0.0, total_before_gift - gift_amount), 2)
        note_snapshot = ("Доставка: " + zone["name"] + " (" + str(round(shipping_fee,2)) + " сомонӣ). Бонус: " + str(points_used) + " хол (" + str(round(bonus_discount,2)) + " сомонӣ). Сертификат: " + (gift_code + " (" + str(round(gift_amount,2)) + " сомонӣ). " if gift_amount else "нест. ") + note)[:500]
        oid = d.execute("INSERT INTO orders(customer_name,phone,address,note,items_json,total,discount,promo,shipping_fee,delivery_zone,points_redeemed,gift_card_code,gift_card_amount) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (name, phone, address, note_snapshot, json.dumps(order_items, ensure_ascii=False), total, round(discount+bonus_discount,2), code, shipping_fee, zone["name"], points_used, gift_code if gift_amount else "", gift_amount)).lastrowid
        d.execute("INSERT INTO admin_notifications(kind,title,body,target_url) VALUES(?,?,?,?)",
                  ("order", "Фармоиши нав №" + str(oid), name + " · " + phone + " · " + str(round(total, 2)) + " сомонӣ", "/admin/order/" + str(oid)))
        if gift_card and gift_amount:
            cur_gift = d.execute("UPDATE gift_cards SET balance=balance-? WHERE code=? AND active=1 AND balance>=?", (gift_amount, gift_code, gift_amount))
            if cur_gift.rowcount != 1:
                raise ValueError("giftcard")
            d.execute("INSERT INTO gift_card_transactions(code,order_id,amount_change,reason) VALUES(?,?,?,?)", (gift_code, oid, -gift_amount, "Истифода дар фармоиш"))
        if personal_coupon and code == personal_code:
            cur_coupon = d.execute("UPDATE personal_coupons SET used_order_id=?,active=0 WHERE code=? AND active=1 AND used_order_id IS NULL", (oid, personal_code))
            if cur_coupon.rowcount != 1:
                raise ValueError("coupon")
        if points_used:
            apply_loyalty_change(d, phone, name, oid, -points_used, "redeem")
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
{% if o.status=='Бекор' %}<p><span class="pill bad">Бекор шуд</span></p>{% else %}<p><b>Вазъияти фармоиш:</b> {{o.status}}</p>{% set steps=order_statuses[:5] %}{% set idx=steps.index(o.status) if o.status in steps else 0 %}<div class="steps">{% for s in steps %}<div class="{{'now' if loop.index0==idx else ('done' if loop.index0<idx else '')}}">{{s}}</div>{% endfor %}</div>{% endif %}
{% if o.delivery_zone %}<div class="sum"><span>Минтақаи доставка</span><span>{{o.delivery_zone}}</span></div>{% endif %}{% if o.address %}<p><b>Суроғаи расонидан:</b> {{o.address}}</p>{% endif %}
{% for x in its %}<div class="sum"><span>{{x.name}} × {{x.quantity}}</span><span>{{money(x.subtotal)}}</span></div>{% endfor %}{% if o.shipping_fee and o.shipping_fee>0 %}<div class="sum"><span>Доставка</span><span>{{money(o.shipping_fee)}} сомонӣ</span></div>{% endif %}{% if o.discount and o.discount>0 %}<div class="sum"><span>Тахфиф</span><span>−{{money(o.discount)}} сомонӣ</span></div>{% endif %}{% if o.points_redeemed %}<div class="sum"><span>Бонус истифода шуд</span><span>{{o.points_redeemed}} хол (−{{money(o.points_redeemed/10)}} сомонӣ)</span></div>{% endif %}<div class="sum big"><span>Ҳамагӣ</span><span>{{money(o.total)}} сомонӣ</span></div><p class="muted">Вазъиятро мағоза нав мекунад. Барои савол метавонед бо мағоза тамос гиред.</p></div>{% endif %}</main>"""


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


NOTIFICATIONS = """<main class="wrap" style="max-width:680px"><h1>Огоҳиномаҳои фармоиш</h1><div class="panel"><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Рақами фармоиш</label><input class="field" name="order" required inputmode="numeric" maxlength="10" placeholder="Масалан: 15"><label>Телефон</label><input class="field" name="phone" required maxlength="30" inputmode="tel" placeholder="+992..."><button class="btn block" style="margin-top:12px">Намоиш додан</button></form></div>{% if o %}<div class="panel"><h2>Фармоиши №{{o.id}}</h2><p>Вазъияти ҳозира: <b>{{o.status}}</b></p>{% if notices %}<h3>Таърихи огоҳиномаҳо</h3>{% for n in notices %}<div class="panel" style="margin:8px 0"><b>{{n.status}}</b><p>{{n.message}}</p><small class="muted">{{n.created_at}}</small></div>{% endfor %}{% else %}<p class="muted">Ҳоло огоҳиномаи нав нест. Ҳангоми тағйир ёфтани вазъият ин ҷо нишон дода мешавад.</p>{% endif %}<a class="btn dark" href="/track">Пайгирии фармоиш</a></div>{% endif %}</main>"""

@app.route("/notifications", methods=["GET", "POST"])
def customer_notifications():
    o = None
    notices = []
    if request.method == "POST":
        if limited("notifications:" + ip(), 15, 600):
            abort(429)
        try:
            oid = int(request.form.get("order", "").strip().lstrip("#"))
        except ValueError:
            oid = 0
        digits = re.sub(r"\D", "", request.form.get("phone", ""))
        row = db().execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if row and len(digits) >= 7 and re.sub(r"\D", "", row["phone"])[-9:] == digits[-9:]:
            o = row
            notices = db().execute("SELECT status,message,created_at FROM order_notifications WHERE order_id=? ORDER BY id DESC", (oid,)).fetchall()
        else:
            flash("Фармоиш бо ин маълумот ёфт нашуд. Рақам ва телефонро санҷед.", "err")
    return page(NOTIFICATIONS, title="Огоҳиномаҳо", o=o, notices=notices)


# ---------- Customer panel (order + phone verification) ----------
ACCOUNT_LOGIN = """<main class=\"wrap\" style=\"max-width:680px\"><h1>👤 Кабинети муштарӣ</h1><div class=\"panel\"><p class=\"muted\">Барои дидани фармоишҳо ва бонусҳо, рақами фармоиш ва телефони ҳамон фармоишро ворид кунед.</p><form method=\"post\"><input type=\"hidden\" name=\"_csrf\" value=\"{{csrf_token()}}\"><label>Рақами фармоиш</label><input class=\"field\" name=\"order\" inputmode=\"numeric\" maxlength=\"10\" required placeholder=\"Масалан: 24\"><label>Телефон</label><input class=\"field\" name=\"phone\" inputmode=\"tel\" maxlength=\"30\" required placeholder=\"+992...\"><button class=\"btn block\" style=\"margin-top:12px\">Ворид шудан</button></form><p class=\"muted\">Ин усул барои тасдиқ танҳо маълумоти фармоишро истифода мекунад. Телефон ва рақами фармоишро ба дигарон надиҳед.</p></div><a class=\"btn dark\" href=\"/track\">Пайгирии фармоиш</a></main>"""

ACCOUNT_PANEL = """<main class=\"wrap\"><div style=\"display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap\"><h1>👤 Кабинети муштарӣ</h1><form method=\"post\" action=\"/account/logout\"><input type=\"hidden\" name=\"_csrf\" value=\"{{csrf_token()}}\"><button class=\"btn dark\">Баромадан</button></form></div><p class=\"muted\">Салом, {{customer_name}} · Телефон: {{phone_mask}}</p><div class="panel"><h2>✏️ Маълумоти профил</h2><form method="post" action="/account/profile"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Номи намоишӣ</label><input class="field" name="display_name" maxlength="60" required value="{{profile_name}}" placeholder="Номи шумо"><button class="btn" style="margin-top:10px">Сабти профил</button></form><p class="muted">Рақами телефон барои амният аз ин ҷо иваз намешавад.</p></div><div class=\"statgrid\" style=\"grid-template-columns:repeat(2,1fr)\"><div class=\"stat\">Бонусҳои дастрас<b>{{points}} хол</b><small>10 хол = 1 сомонӣ тахфиф</small></div><div class=\"stat\">Фармоишҳои шумо<b>{{orders|length}}</b></div></div><div class=\"panel\"><h2>📦 Таърихи фармоишҳо</h2>{% for o in orders %}<details style=\"border-bottom:1px solid var(--line);padding:12px 0\"><summary style=\"cursor:pointer\"><b>№{{o.id}}</b> · {{o.created_at}} · {{o.status}}<br><span class=\"gold\">{{money(o.total)}} сомонӣ</span></summary><div style=\"padding-top:10px\">{% for it in o['items'] %}<div class=\"sum\"><span>{{it.name}} × {{it.quantity}}</span><span>{{money(it.subtotal)}} сомонӣ</span></div>{% endfor %}{% if o.points_redeemed %}<p class=\"muted\">Бонуси истифодашуда: {{o.points_redeemed}} хол</p>{% endif %}<a class=\"btn dark sm\" href=\"/track\">Пайгирӣ</a></div></details>{% else %}<p class=\"muted\">Фармоишҳо пайдо нашуданд.</p>{% endfor %}</div><div class=\"panel\"><h2>🎁 Таърихи бонусҳо</h2>{% for t in transactions %}<div class=\"sum\"><span>{{t.reason}} · {{t.created_at}}</span><b class=\"{{'gold' if t.points_change>0 else ''}}\">{{'+' if t.points_change>0 else ''}}{{t.points_change}} хол</b></div>{% else %}<p class=\"muted\">Ҳоло ҳаракати бонусҳо нест.</p>{% endfor %}</div><div class=\"panel\"><h2>🎟 Купонҳои шахсии шумо</h2><p class=\"muted\">Купонҳо танҳо барои рақами телефони тасдиқшуда нишон дода мешаванд. Ҳангоми фармоиш кодро ворид кунед.</p>{% for c in personal_coupons %}<div class=\"sum\"><span><b>{{c.code}}</b> · {{c.percent}}% тахфиф<br><small class=\"muted\">{{('Мӯҳлат: '+c.expires_on) if c.expires_on else 'Бемӯҳлат'}}</small></span><b class=\"gold\">ФАЪОЛ</b></div>{% else %}<p class=\"muted\">Ҳоло купони дастрас надоред.</p>{% endfor %}</div><div class=\"panel\"><h2>🎁 Сертификатҳои тӯҳфаи шумо</h2>{% for c in gift_cards %}<div class=\"sum\"><span><b>{{c.code}}</b><br><small class=\"muted\">{{('Мӯҳлат: '+c.expires_on) if c.expires_on else 'Бемӯҳлат'}}</small></span><b class=\"gold\">{{money(c.balance)}} сомонӣ</b></div>{% else %}<p class=\"muted\">Сертификати дастрас надоред.</p>{% endfor %}</div><div class=\"panel\"><h2>❤️ Дӯстдоштаҳо дар ин телефон</h2><p>Шумора: {{favorites_count}} маҳсулот. Рӯйхати дӯстдоштаҳо ба ҳамин браузер вобаста аст.</p><a class=\"btn dark\" href=\"/favorites\">Кушодани рӯйхат</a></div><a class=\"btn ghost\" href=\"/\">Ба мағоза</a></main>"""

@app.route("/account", methods=["GET", "POST"])
def customer_account():
    if request.method == "POST":
        # Rate-limit by IP first, then by order ID to slow down guessing.
        if limited("account-login:" + ip(), 8, 900):
            abort(429, description="Кӯшишҳои зиёд. Баъдтар такрор кунед.")
        try:
            oid = int(request.form.get("order", "").strip().lstrip("#"))
        except (TypeError, ValueError):
            oid = 0
        if oid <= 0 or limited("account-order:" + str(oid), 5, 900):
            abort(429, description="Кӯшишҳои зиёд барои ин фармоиш. Баъдтар такрор кунед.")
        digits = re.sub(r"\D", "", request.form.get("phone", ""))
        row = db().execute("SELECT id,phone,customer_name FROM orders WHERE id=?", (oid,)).fetchone()
        stored_digits = re.sub(r"\D", "", row["phone"] if row else "")
        # Require at least 9 digits; compare only the last 9 for Tajik numbers.
        if row and len(digits) >= 9 and len(stored_digits) >= 9 and stored_digits[-9:] == digits[-9:]:
            # Remove previous customer identity before establishing the new session.
            session.pop("customer_phone", None)
            session.pop("customer_name", None)
            session["customer_phone"] = row["phone"]
            session["customer_name"] = row["customer_name"]
            session["customer_login_at"] = int(time.time())
            session["customer_session_nonce"] = secrets.token_hex(16)
            return redirect("/account")
        flash("Маълумот дуруст нест. Рақами фармоиш ва телефонро санҷед.", "err")
        return page(ACCOUNT_LOGIN, title="Кабинети муштарӣ")
    phone = session.get("customer_phone")
    if not phone:
        return page(ACCOUNT_LOGIN, title="Кабинети муштарӣ")
    orders_raw = db().execute("SELECT * FROM orders WHERE phone=? ORDER BY id DESC LIMIT 100", (phone,)).fetchall()
    orders = []
    for o in orders_raw:
        item = dict(o)
        item["items"] = items_of(o)
        orders.append(item)
    transactions = db().execute("SELECT points_change,reason,created_at FROM loyalty_transactions WHERE phone=? ORDER BY id DESC LIMIT 100", (phone,)).fetchall()
    point_count = loyalty_balance(db(), phone)
    addresses = db().execute("SELECT id,label,address,is_default FROM customer_addresses WHERE phone=? ORDER BY is_default DESC,id DESC", (phone,)).fetchall()
    db().execute("CREATE TABLE IF NOT EXISTS customer_profiles(phone TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    profile = db().execute("SELECT display_name FROM customer_profiles WHERE phone=?", (phone,)).fetchone()
    profile_name = (profile["display_name"] if profile and profile["display_name"] else session.get("customer_name", "Муштарӣ"))
    digits = re.sub(r"\D", "", phone)
    phone_mask = ("+" + digits[:3] + " ••• •• " + digits[-2:]) if len(digits) > 7 else "••••"
    # Normalize phone formatting so +992, spaces and dashes match safely.
    personal_coupons = []
    for coupon in db().execute("SELECT code,phone,percent,expires_on FROM personal_coupons WHERE active=1 AND used_order_id IS NULL AND (expires_on='' OR expires_on>=date('now')) ORDER BY created_at DESC").fetchall():
        coupon_digits = re.sub(r"\D", "", coupon["phone"] or "")
        if len(coupon_digits) >= 9 and len(digits) >= 9 and coupon_digits[-9:] == digits[-9:]:
            personal_coupons.append(coupon)
    gift_cards = []
    for gift in db().execute("SELECT code,balance,expires_on,recipient_phone FROM gift_cards WHERE active=1 AND balance>0 AND (expires_on='' OR expires_on>=date('now')) ORDER BY created_at DESC").fetchall():
        recipient_digits = re.sub(r"\D", "", gift["recipient_phone"] or "")
        if not recipient_digits or (len(recipient_digits) >= 9 and len(digits) >= 9 and recipient_digits[-9:] == digits[-9:]):
            gift_cards.append(gift)
    return page(ACCOUNT_PANEL, title="Кабинети муштарӣ", orders=orders, transactions=transactions,
               points=point_count, customer_name=session.get("customer_name", "Муштарӣ"),
               phone_mask=phone_mask, favorites_count=len(get_favorites()), profile_name=profile_name, addresses=addresses,
               personal_coupons=personal_coupons, gift_cards=gift_cards)

@app.route("/account/addresses/add", methods=["POST"])
def customer_address_add():
    phone = session.get("customer_phone")
    if not phone:
        return redirect("/account")
    if limited("account-address-add:" + ip(), 10, 900):
        abort(429)
    label = re.sub(r"[\x00-\x1f]", "", (request.form.get("label") or "").strip())[:40]
    address = re.sub(r"[\x00-\x1f]", " ", (request.form.get("address") or "").strip())[:250]
    if len(address) < 5:
        flash("Суроғаро пурратар нависед.", "err")
        return redirect("/account")
    d = db()
    count = d.execute("SELECT COUNT(*) FROM customer_addresses WHERE phone=?", (phone,)).fetchone()[0]
    if count >= 5:
        flash("Шумо метавонед то 5 суроғаро нигоҳ доред.", "err")
        return redirect("/account")
    make_default = bool(request.form.get("is_default")) or count == 0
    if make_default:
        d.execute("UPDATE customer_addresses SET is_default=0 WHERE phone=?", (phone,))
    d.execute("INSERT INTO customer_addresses(phone,label,address,is_default) VALUES(?,?,?,?)", (phone,label,address,1 if make_default else 0))
    flash("Суроға сабт шуд.", "ok")
    return redirect("/account")

@app.route("/account/addresses/default/<int:aid>", methods=["POST"])
def customer_address_default(aid):
    phone = session.get("customer_phone")
    if not phone:
        return redirect("/account")
    d = db()
    row = d.execute("SELECT id FROM customer_addresses WHERE id=? AND phone=?", (aid,phone)).fetchone()
    if row:
        d.execute("UPDATE customer_addresses SET is_default=0 WHERE phone=?", (phone,))
        d.execute("UPDATE customer_addresses SET is_default=1,updated_at=CURRENT_TIMESTAMP WHERE id=? AND phone=?", (aid,phone))
        flash("Суроғаи асосӣ иваз шуд.", "ok")
    return redirect("/account")

@app.route("/account/addresses/delete/<int:aid>", methods=["POST"])
def customer_address_delete(aid):
    phone = session.get("customer_phone")
    if not phone:
        return redirect("/account")
    d = db()
    row = d.execute("SELECT is_default FROM customer_addresses WHERE id=? AND phone=?", (aid,phone)).fetchone()
    if row:
        d.execute("DELETE FROM customer_addresses WHERE id=? AND phone=?", (aid,phone))
        if row["is_default"]:
            next_row = d.execute("SELECT id FROM customer_addresses WHERE phone=? ORDER BY id DESC LIMIT 1", (phone,)).fetchone()
            if next_row:
                d.execute("UPDATE customer_addresses SET is_default=1 WHERE id=? AND phone=?", (next_row["id"],phone))
        flash("Суроға нест карда шуд.", "ok")
    return redirect("/account")

@app.route("/account/profile", methods=["POST"])
def customer_profile_update():
    phone = session.get("customer_phone")
    if not phone:
        return redirect("/account")
    if limited("account-profile:" + ip(), 10, 900):
        abort(429, description="Кӯшишҳои зиёд. Баъдтар такрор кунед.")
    display_name = (request.form.get("display_name") or "").strip()
    if not display_name or len(display_name) > 60:
        flash("Номро дуруст ворид кунед (1 то 60 аломат).", "err")
        return redirect("/account")
    db().execute("CREATE TABLE IF NOT EXISTS customer_profiles(phone TEXT PRIMARY KEY, display_name TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    db().execute("INSERT INTO customer_profiles(phone,display_name,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(phone) DO UPDATE SET display_name=excluded.display_name,updated_at=CURRENT_TIMESTAMP", (phone, display_name))
    session["customer_name"] = display_name
    flash("Маълумоти профил сабт шуд.", "ok")
    return redirect("/account")

@app.route("/account/logout", methods=["POST"])
def customer_account_logout():
    session.pop("customer_phone", None)
    session.pop("customer_name", None)
    session.pop("customer_login_at", None)
    session.pop("customer_session_nonce", None)
    flash("Шумо аз кабинети муштарӣ баромадед.", "ok")
    return redirect("/account")


# ---------- Admin ----------
LOGIN = """<main class="wrap" style="max-width:440px"><div class="panel"><h1 class="gold">Вуруд ба панели admin</h1><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Парол</label><input class="field" type="password" name="password" required autocomplete="current-password"><button class="btn block" style="margin-top:14px">Ворид шудан</button></form></div></main>"""


FAQ_PAGE = """<main class="wrap" style="max-width:850px"><h1>❓ Саволҳои маъмул</h1><p class="muted">Ҷавобҳои кӯтоҳ ба саволҳои харид.</p>{% for f in faqs %}<details class="panel" style="margin:10px 0"><summary style="cursor:pointer;color:var(--gold);font-weight:700;padding:4px">{{f.question}}</summary><div style="padding-top:10px;line-height:1.7">{{f.answer}}</div></details>{% else %}<div class="panel">Ҳоло саволҳо илова нашудаанд.</div>{% endfor %}<a class="btn dark" href="/">Ба мағоза</a></main>"""

@app.route("/faq")
def faq_public():
    faqs = db().execute("SELECT * FROM faqs WHERE active=1 ORDER BY sort_order,id").fetchall()
    return page(FAQ_PAGE, title="Саволҳои маъмул", faqs=faqs)

CONTACT_PAGE = """<main class="wrap" style="max-width:720px"><h1>📩 Тамос бо NEXVORA</h1><p class="muted">Савол ё пешниҳод доред? Паём фиристед.</p><div class="panel"><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Ном</label><input class="field" name="customer_name" maxlength="80" required placeholder="Номи шумо"><label>Телефон</label><input class="field" name="phone" maxlength="30" required inputmode="tel" placeholder="+992..."><label>Мавзӯъ</label><input class="field" name="subject" maxlength="100" required placeholder="Мавзӯи паём"><label>Паём</label><textarea class="field" name="message" rows="5" maxlength="1500" required placeholder="Паёми худро нависед"></textarea><button class="btn block" style="margin-top:12px">Фиристодани паём</button></form></div><a class="btn dark" href="/">Ба мағоза</a></main>"""

@app.route("/contact", methods=["GET", "POST"])
def contact_public():
    if request.method == "POST":
        key = "contact:" + (request.remote_addr or "unknown")
        if limited(key, 5, 3600):
            flash("Паёмҳо зиёд фиристода шуданд. Баъдтар кӯшиш кунед.", "err")
            return redirect("/contact")
        name = request.form.get("customer_name", "").strip()[:80]
        phone = request.form.get("phone", "").strip()[:30]
        subject = request.form.get("subject", "").strip()[:100]
        message = request.form.get("message", "").strip()[:1500]
        if not (name and phone and subject and message):
            flash("Ҳамаи майдонҳоро пур кунед.", "err")
        elif len(message) < 5:
            flash("Паём хеле кӯтоҳ аст.", "err")
        else:
            cur = db().execute("INSERT INTO contact_messages(customer_name,phone,subject,message) VALUES(?,?,?,?)", (name,phone,subject,message))
            mid = cur.lastrowid
            db().execute("INSERT INTO admin_notifications(kind,title,body,target_url) VALUES(?,?,?,?)",
                         ("message", "Паёми нав аз " + name, subject + " · " + phone, "/admin/messages"))
            flash("Паём фиристода шуд. Мағоза онро мебинад.", "ok")
            return redirect("/contact")
    return page(CONTACT_PAGE, title="Тамос")

CONTACT_ADMIN = """<main class="wrap"><h1 class="gold">📩 Паёмҳои муштариён</h1><p><a class="btn dark" href="/admin">← Панели Admin</a></p><div class="panel"><p>Ҳама: {{total}} · Нав: {{unread}}</p>{% for m in messages %}<article class="panel" style="margin:10px 0;border:1px solid {{'var(--gold)' if not m.is_read else 'var(--line)'}}"><div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><h3>{{m.subject}}</h3><span class="pill">{{'Нав' if not m.is_read else 'Хонда шуд'}}</span></div><p><b>{{m.customer_name}}</b> · <a href="tel:{{m.phone}}">{{m.phone}}</a> · <a href="https://wa.me/{{m.phone|replace('+','')|replace(' ','')|replace('-','')|replace('(','')|replace(')','')}}">WhatsApp</a></p><p class="muted">{{m.created_at}}</p><p style="white-space:pre-wrap">{{m.message}}</p><div style="display:flex;gap:8px;flex-wrap:wrap"><form method="post" action="/admin/messages/read/{{m.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">{{'Хонда шуд' if not m.is_read else 'Нав гардон'}}</button></form><form method="post" action="/admin/messages/delete/{{m.id}}" onsubmit="return confirm('Паём нест карда шавад?')"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red sm">Нест кардан</button></form></div></article>{% else %}<p>Паём нест.</p>{% endfor %}</div></main>"""

@app.route("/admin/messages")
@admin_required
def admin_messages():
    messages = db().execute("SELECT * FROM contact_messages ORDER BY id DESC LIMIT 300").fetchall()
    total = db().execute("SELECT COUNT(*) FROM contact_messages").fetchone()[0]
    unread = db().execute("SELECT COUNT(*) FROM contact_messages WHERE is_read=0").fetchone()[0]
    return page(CONTACT_ADMIN, title="Паёмҳо", messages=messages, total=total, unread=unread, admin=True)

@app.route("/admin/messages/read/<int:mid>", methods=["POST"])
@admin_required
def admin_message_read(mid):
    db().execute("UPDATE contact_messages SET is_read=CASE WHEN is_read=1 THEN 0 ELSE 1 END WHERE id=?", (mid,))
    return redirect("/admin/messages")

@app.route("/admin/messages/delete/<int:mid>", methods=["POST"])
@admin_required
def admin_message_delete(mid):
    db().execute("DELETE FROM contact_messages WHERE id=?", (mid,))
    flash("Паём нест карда шуд.", "ok")
    return redirect("/admin/messages")

FAQ_ADMIN = """<main class="wrap"><h1 class="gold">Идоракунии FAQ</h1><p><a class="btn dark" href="/admin">← Ба панели Admin</a> <a class="btn" href="/faq" target="_blank">Намоиши FAQ</a></p><div class="panel"><h2>Иловаи савол</h2><form method="post" action="/admin/faq/add"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Савол</label><input class="field" name="question" maxlength="220" required><label>Ҷавоб</label><textarea class="field" name="answer" rows="4" maxlength="2000" required></textarea><label>Тартиб (рақам)</label><input class="field" type="number" name="sort_order" value="0"><button class="btn" style="margin-top:10px">Сабт кардан</button></form></div><div class="panel"><h2>Саволҳои мавҷуда</h2>{% for f in faqs %}<article class="panel" style="margin:10px 0"><form method="post" action="/admin/faq/edit/{{f.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Савол</label><input class="field" name="question" maxlength="220" value="{{f.question}}" required><label>Ҷавоб</label><textarea class="field" name="answer" rows="3" maxlength="2000" required>{{f.answer}}</textarea><label>Тартиб</label><input class="field" type="number" name="sort_order" value="{{f.sort_order}}"><button class="btn sm" style="margin-top:8px">Навсозӣ</button></form><div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap"><form method="post" action="/admin/faq/toggle/{{f.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">{{'Хомӯш кардан' if f.active else 'Фаъол кардан'}}</button></form><form method="post" action="/admin/faq/delete/{{f.id}}" onsubmit="return confirm('Ин саволро нест мекунед?')"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red sm">Нест кардан</button></form><span class="pill">{{'Фаъол' if f.active else 'Хомӯш'}}</span></div></article>{% else %}<p>Савол нест.</p>{% endfor %}</div></main>"""

@app.route("/admin/faq")
@admin_required
def admin_faq():
    return page(FAQ_ADMIN, title="FAQ Admin", faqs=db().execute("SELECT * FROM faqs ORDER BY sort_order,id").fetchall(), admin=True)

@app.route("/admin/faq/add", methods=["POST"])
@admin_required
def admin_faq_add():
    question=request.form.get("question", "").strip()[:220]
    answer=request.form.get("answer", "").strip()[:2000]
    try: sort_order=int(request.form.get("sort_order", "0"))
    except ValueError: sort_order=0
    if question and answer:
        db().execute("INSERT INTO faqs(question,answer,sort_order) VALUES(?,?,?)", (question,answer,sort_order))
        flash("Савол илова шуд.", "ok")
    else: flash("Савол ва ҷавобро пур кунед.", "err")
    return redirect("/admin/faq")

@app.route("/admin/faq/edit/<int:fid>", methods=["POST"])
@admin_required
def admin_faq_edit(fid):
    question=request.form.get("question", "").strip()[:220]
    answer=request.form.get("answer", "").strip()[:2000]
    try: sort_order=int(request.form.get("sort_order", "0"))
    except ValueError: sort_order=0
    if question and answer:
        db().execute("UPDATE faqs SET question=?,answer=?,sort_order=? WHERE id=?", (question,answer,sort_order,fid))
        flash("Савол нав шуд.", "ok")
    else: flash("Савол ва ҷавоб холӣ набошанд.", "err")
    return redirect("/admin/faq")

@app.route("/admin/faq/toggle/<int:fid>", methods=["POST"])
@admin_required
def admin_faq_toggle(fid):
    db().execute("UPDATE faqs SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (fid,))
    return redirect("/admin/faq")

@app.route("/admin/faq/delete/<int:fid>", methods=["POST"])
@admin_required
def admin_faq_delete(fid):
    db().execute("DELETE FROM faqs WHERE id=?", (fid,))
    flash("Савол нест карда шуд.", "ok")
    return redirect("/admin/faq")

@app.route("/product/<int:pid>/question", methods=["POST"])
def product_question_add(pid):
    product = db().execute("SELECT id FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not product:
        abort(404)
    if limited("product-question:" + ip(), 5, 3600):
        flash("Шумо дар як соат танҳо 5 савол фиристода метавонед.", "warn")
        return redirect(url_for("product_detail", pid=pid) + "#questions")
    name = (request.form.get("customer_name") or "Муштарӣ").strip()[:60] or "Муштарӣ"
    question = (request.form.get("question") or "").strip()
    if len(question) < 5:
        flash("Савол бояд камаш 5 ҳарф дошта бошад.", "warn")
    else:
        db().execute("INSERT INTO product_questions(product_id,customer_name,question) VALUES(?,?,?)", (pid, name, question[:500]))
        flash("Саволи шумо қабул шуд. Пас аз ҷавоби мағоза нашр мешавад.", "ok")
    return redirect(url_for("product_detail", pid=pid) + "#questions")


PRODUCT_QUESTIONS_ADMIN = """<main class="wrap"><h1 class="gold">❓ Саволҳои маҳсулот</h1><p><a class="btn dark" href="/admin">← Панели Admin</a></p>{% for q in questions %}<div class="panel"><b>{{q.product_name}}</b><p><b>{{q.customer_name}}:</b> {{q.question}}</p><small class="muted">{{q.created_at}}</small><form method="post" action="/admin/questions/answer/{{q.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Ҷавоби мағоза</label><textarea class="field" name="answer" rows="3" maxlength="1000">{{q.answer}}</textarea><label style="display:block;margin:8px 0"><input type="checkbox" name="publish" value="1" {{'checked' if q.is_public}}> Нашр кардан дар саҳифаи маҳсулот</label><button class="btn">Сабт кардани ҷавоб</button></form><form method="post" action="/admin/questions/delete/{{q.id}}" style="margin-top:8px"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark">Нест кардан</button></form></div>{% else %}<div class="panel">Ҳоло саволе нест.</div>{% endfor %}</main>"""

@app.route("/admin/questions")
@admin_required
def admin_product_questions():
    rows = db().execute("SELECT q.*,p.name AS product_name FROM product_questions q LEFT JOIN products p ON p.id=q.product_id ORDER BY q.id DESC LIMIT 300").fetchall()
    return page(PRODUCT_QUESTIONS_ADMIN, title="Саволҳои маҳсулот", questions=rows)


@app.route("/admin/questions/answer/<int:qid>", methods=["POST"])
@admin_required
def admin_product_question_answer(qid):
    answer = (request.form.get("answer") or "").strip()[:1000]
    publish = 1 if request.form.get("publish") == "1" and answer else 0
    db().execute("UPDATE product_questions SET answer=?,is_public=?,answered_at=CURRENT_TIMESTAMP WHERE id=?", (answer, publish, qid))
    flash("Ҷавоб сабт шуд.", "ok")
    return redirect("/admin/questions")


@app.route("/admin/questions/delete/<int:qid>", methods=["POST"])
@admin_required
def admin_product_question_delete(qid):
    db().execute("DELETE FROM product_questions WHERE id=?", (qid,))
    flash("Савол нест карда шуд.", "ok")
    return redirect("/admin/questions")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        key = "login:" + ip()
        if too_many(key, 5, 900):
            abort(429, description="Кӯшишҳои зиёд. 15 дақиқа интизор шавед.")
        if ADMIN_HASH and check_password_hash(ADMIN_HASH, request.form.get("password", "")):
            session.clear()
            session["admin"] = int(time.time())
            session["admin_login_at"] = int(time.time())
            csrf()
            logger.info("Admin login successful from %s", ip())
            return redirect("/admin")
        hit(key)
        flash("Парол нодуруст аст.", "err")
    if not ADMIN_HASH:
        flash("ADMIN_PASSWORD дар муҳит танзим нашудааст, вуруд ғайрифаъол аст.", "err")
    return page(LOGIN, title="Вуруд")


@app.route("/logout", methods=["POST"])
def logout():
    if is_admin():
        logger.info("Admin logout from %s", ip())
    session.clear()
    return redirect("/")


PFIELDS = """{% macro pfields(p=None) %}<div class="cols"><div><label>Ном *</label><input class="field" name="name" required maxlength="120" value="{{p.name if p else ''}}"></div><div><label>Категория</label><input class="field" name="category" maxlength="80" value="{{p.category if p else ''}}"></div>
<div><label>Нарх барои харидор *</label><input class="field" type="number" step=".01" min="0.01" name="price" required value="{{p.price if p else ''}}"></div><div><label>Арзиши хариди 1 дона</label><input class="field" type="number" step=".01" min="0" name="cost_price" value="{{p.cost_price if p and p.cost_price else '0'}}"></div><div><label>Нархи тахфифӣ</label><input class="field" type="number" step=".01" min="0" name="sale" value="{{p.sale_price if p and p.sale_price else ''}}"></div>
<div><label>Миқдори анбор</label><input class="field" type="number" min="0" name="stock" value="{{p.stock if p else 0}}"></div>
<div><label>Акс (jpg, png, webp)</label><input class="field" type="file" name="image" accept=".jpg,.jpeg,.png,.webp"></div>
<div><label>Видео (MP4/WebM, то 100 MB)</label><input class="field" type="file" name="video" accept="video/mp4,video/webm"></div></div>
<label>Тавсиф</label><textarea class="field" rows="3" name="description" maxlength="1000">{{p.description if p else ''}}</textarea><label><input type="checkbox" name="featured" {{'checked' if p and p.featured}}> Маҳсулоти машҳур</label>{% if p %}<label><input type="checkbox" name="active" {{'checked' if p.active}}> Дар мағоза намоиш дода шавад</label>{% endif %}{% endmacro %}"""

ADMIN = PFIELDS + """<main class="wrap"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px"><h1 class="gold">{{ "Панель администратора" if admin_lang=="ru" else "Панели admin" }}</h1><a class="btn gold sm" href="/admin/dashboard">📊 Dashboard</a><form method="get" action="/admin/lang" style="display:flex;gap:6px;align-items:center"><button class="btn ghost sm" name="lang" value="tg">Тоҷикӣ</button><button class="btn ghost sm" name="lang" value="ru">Русский</button></form><a class="btn gold sm" href="/admin/banner">Баннери реклама</a><a class="btn dark sm" href="/admin/faq">FAQ</a><a class="btn dark sm" href="/admin/messages">Паёмҳо</a><a class="btn gold sm" href="/admin/notifications">🔔 Огоҳиномаҳо</a><a class="btn dark sm" href="/admin/questions">Саволҳои маҳсулот</a><a class="btn gold sm" href="/admin/gift-cards">Gift Card</a><form method="post" action="/logout"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">Баромадан</button></form></div>
<div class="statgrid"><div class="stat">Маҳсулот<b>{{s.pc}}</b></div><div class="stat">{{ "Заказы" if admin_lang=="ru" else "Фармоишҳо" }} (имрӯз {{s.today}})<b>{{s.oc}}</b></div><div class="stat">Даромад<b>{{money(s.rev)}}</b></div><div class="stat">Анбори кам (≤5)<b>{{s.low}}</b></div></div>
<div class="panel"><h2>{{ "Отчёт продаж" if admin_lang=="ru" else "Ҳисоботи фурӯш" }}</h2><form method="get" action="/admin" class="inline" style="gap:8px;flex-wrap:wrap;margin-bottom:12px"><input type="hidden" name="q" value="{{filters.q}}"><input type="hidden" name="category" value="{{filters.category}}"><input type="hidden" name="state" value="{{filters.state}}"><label>Давра</label><select class="field" name="report" style="max-width:180px"><option value="7" {{'selected' if report_period=='7'}}>7 рӯзи охир</option><option value="30" {{'selected' if report_period=='30'}}>30 рӯзи охир</option><option value="90" {{'selected' if report_period=='90'}}>90 рӯзи охир</option><option value="all" {{'selected' if report_period=='all'}}>Ҳама вақт</option></select><button class="btn">{{ "Показать" if admin_lang=="ru" else "Намоиш" }}</button></form><div class="statgrid"><div class="stat">Фармоишҳо<b>{{sales.count}}</b></div><div class="stat">Даромад<b>{{money(sales.revenue)}}</b></div><div class="stat">Миёнаи фармоиш<b>{{money(sales.average)}}</b></div><div class="stat">Анҷомёфта<b>{{sales.done}}</b></div></div><div class="tablewrap"><table><tr><th>Вазъият</th><th>Шумора</th></tr>{% for row in sales.statuses %}<tr><td>{{row.status}}</td><td>{{row.n}}</td></tr>{% endfor %}</table></div>
<h3>Маҳсулоти серхаридор</h3><div class="tablewrap"><table><tr><th>Маҳсулот</th><th>Дона</th><th>Даромад</th></tr>{% for row in sales.top_products %}<tr><td>{{row.name}}</td><td>{{row.quantity}}</td><td>{{money(row.revenue)}}</td></tr>{% else %}<tr><td colspan="3">Ҳоло фурӯше нест.</td></tr>{% endfor %}</table></div>
<p class="muted" style="margin-bottom:0">Даромад фармоишҳои бекоршударо ҳисоб намекунад. Рӯйхат аз рӯйи миқдори фурӯш тартиб дода шудааст.</p></div>
<div class="panel"><h2>{{ "Уведомления склада" if admin_lang=="ru" else "Огоҳии анбор" }}</h2>
<div class="statgrid"><div class="stat">Тамом шудааст<b>{{stock_alerts.out_count}}</b></div><div class="stat">Кам мондааст (1–5)<b>{{stock_alerts.low_count}}</b></div></div>
{% if stock_alerts.items %}<p class="muted">Маҳсулоте, ки захираашон 5 дона ё камтар аст:</p><div class="tablewrap"><table><tr><th>Маҳсулот</th><th>Категория</th><th>Боқимонда</th><th>Амалиёт</th></tr>{% for p in stock_alerts.items %}<tr><td>{{p.name}}</td><td>{{p.category}}</td><td>{% if p.stock==0 %}<span class="pill bad">Тамом шуд</span>{% else %}<span class="pill warn">{{p.stock}} дона</span>{% endif %}</td><td><a class="btn dark sm" href="/admin/edit/{{p.id}}">Пур кардани анбор</a></td></tr>{% endfor %}</table></div>{% else %}<p>Ҳамаи маҳсулоти фаъол захираи кофӣ доранд.</p>{% endif %}</div>
<div class="panel"><h2>Иловаи маҳсулот</h2><form method="post" action="/admin/add" enctype="multipart/form-data"><input type="hidden" name="_csrf" value="{{csrf_token()}}">{{pfields()}}<button class="btn" style="margin-top:12px">Илова кардан</button></form></div>
<div class="panel"><h2>🚚 Минтақаҳо ва доставка</h2><p class="muted">Нархи доставка ва маблағи расонидани ройгонро идора кунед.</p><form class="inline" style="flex-wrap:wrap" method="post" action="/admin/delivery/add"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input class="field" name="name" maxlength="80" placeholder="Номи минтақа" required><input class="field" type="number" name="fee" min="0" step="0.01" placeholder="Нарх (сомонӣ)" required><input class="field" type="number" name="free_from" min="0" step="0.01" placeholder="Доставка ройгон аз (0=нест)" required><button class="btn">Илова</button></form><div class="tablewrap"><table><tr><th>Минтақа</th><th>Нарх</th><th>Ройгон аз</th><th>Ҳолат</th><th></th></tr>{% for z in delivery_zones_admin %}<tr><td>{{z.name}}</td><td>{{money(z.fee)}}</td><td>{{money(z.free_from) if z.free_from else '—'}}</td><td>{{'Фаъол' if z.active else 'Хомӯш'}}</td><td><form method="post" action="/admin/delivery/toggle/{{z.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">{{'Хомӯш' if z.active else 'Фаъол'}}</button></form><form method="post" action="/admin/delivery/delete/{{z.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red sm">Нест</button></form></td></tr>{% endfor %}</table></div></div>
<div class="panel"><h2>🎁 Тахфифи автоматӣ</h2><p class="muted">Агар сабад ба маблағи муайян расад, тахфиф бе промокод ҳисоб мешавад. Промокоди шахсӣ афзалият дорад.</p><form method="post" action="/admin/auto-discount/save" class="inline" style="flex-wrap:wrap"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Аз маблағ (сомонӣ)</label><input class="field" name="min_amount" type="number" min="0" step="0.01" value="{{auto_discount.min_amount}}" required><label>Тахфиф (%)</label><input class="field" name="percent" type="number" min="0" max="90" value="{{auto_discount.percent}}" required><label><input type="checkbox" name="active" {{'checked' if auto_discount.active}}> Фаъол</label><button class="btn">Сабт</button></form></div>
<div class="panel"><h2>Промокодҳо</h2><form class="inline" method="post" action="/admin/promo/add" style="flex-wrap:wrap"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input class="field" style="flex:2;min-width:120px" name="code" placeholder="КОД (масалан NEW10)" maxlength="30" required><input class="field" style="flex:1;min-width:90px" type="number" name="percent" min="1" max="90" placeholder="% тахфиф" required><input class="field" style="flex:1;min-width:90px" type="number" name="max_uses" min="0" placeholder="Ҳад (0 = бешумор)"><input class="field" style="flex:1;min-width:140px" type="date" name="expires_on" title="Мӯҳлати анҷом (ихтиёрӣ)"><button class="btn">Сохтан</button></form>
<div class="tablewrap"><table><tr><th>Код</th><th>Тахфиф</th><th>Истифода</th><th>Мӯҳлат</th><th>Ҳолат</th><th></th></tr>{% for c in promos %}<tr><td><b>{{c.code}}</b></td><td>{{c.percent}}%</td><td>{{c.uses}}{% if c.max_uses %} / {{c.max_uses}}{% endif %}</td><td>{{c.expires_on or 'Бемӯҳлат'}}</td><td>{{'Фаъол' if c.active and (not c.expires_on or c.expires_on>=today) else 'Хомӯш/Мӯҳлат гузашта'}}</td><td style="display:flex;gap:6px"><form method="post" action="/admin/promo/toggle/{{c.code}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">{{'Хомӯш' if c.active else 'Фаъол'}}</button></form><form method="post" action="/admin/promo/delete/{{c.code}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red sm">Нест</button></form></td></tr>{% endfor %}</table></div></div>
<div class="panel"><h2>Таърихи тағйироти анбор</h2>
<p class="muted">100 ҳаракати охирин: фурӯш, иловаи маҳсулот ва тағйири дастии миқдор.</p>
<div class="tablewrap"><table><tr><th>Сана</th><th>Маҳсулот</th><th>Тағйир</th><th>Пеш</th><th>Баъд</th><th>Сабаб</th></tr>
{% for m in stock_history %}<tr><td>{{m.created_at}}</td><td>{{m.product_name}}</td><td>{% if m.change_qty>0 %}<span class="pill">+{{m.change_qty}}</span>{% else %}<span class="pill warn">{{m.change_qty}}</span>{% endif %}</td><td>{{m.before_qty}}</td><td>{{m.after_qty}}</td><td>{{m.reason}}{% if m.reference %}<br><span class="muted">{{m.reference}}</span>{% endif %}</td></tr>{% else %}<tr><td colspan="6">Ҳоло таърихи тағйирот нест.</td></tr>{% endfor %}</table></div></div>
<div class="panel"><h2>Ҳисоботи молиявӣ</h2>
<p class="muted">Арзиши харид аз рӯйи нархи сабтшуда дар вақти фармоиш ҳисоб мешавад. Фармоишҳои кӯҳнае, ки пеш аз V37 сохта шудаанд, арзиши харид надоранд ва ба фоида дохил намешаванд.</p>
<div class="statgrid"><div class="stat">Даромади умумӣ<b>{{money(finance.revenue)}}</b></div><div class="stat">Арзиши моли фурӯхташуда<b>{{money(finance.cogs)}}</b></div><div class="stat">Фоидаи умумии маълум<b>{{money(finance.gross_profit)}}</b></div><div class="stat">Хароҷот<b>{{money(finance.expenses)}}</b></div><div class="stat">Фоидаи ҳисобшудаи маълум<b>{{money(finance.estimated_net)}}</b></div><div class="stat">Молҳои фармоишҳои кӯҳна<b>{{finance.cost_unknown_items}} дона</b></div></div>
<p class="muted">Фоидаи ҳисобшуда танҳо барои фурӯшҳое дуруст аст, ки арзиши хариди онҳо сабт шудааст. Агар арзиши харид 0 гузошта шавад, фоида аз ҳад зиёд нишон дода мешавад.</p>
<h3>Фоидаи ҳар маҳсулот</h3>
<p class="muted">Даромад бо назардошти тахфифи фармоиш тақсим шудааст. Маҳсулоте, ки арзиши хариди кӯҳнааш маълум нест, бо аломати «Нопурра» нишон дода мешавад.</p>
<div class="tablewrap"><table><tr><th>Маҳсулот</th><th>Дона</th><th>Даромад</th><th>Арзиши харид</th><th>Фоида</th><th>Ҳолат</th></tr>
{% for p in finance.products %}<tr><td>{{p.name}}</td><td>{{p.quantity}}</td><td>{{money(p.revenue)}}</td><td>{{money(p.cost)}}</td><td><b>{{money(p.profit)}}</b></td><td>{% if p.unknown %}<span class="pill warn">Нопурра</span>{% elif p.profit<0 %}<span class="pill warn">Зарар</span>{% else %}<span class="pill">Фоида</span>{% endif %}</td></tr>{% else %}<tr><td colspan="6">Ҳоло маълумоти фурӯш нест.</td></tr>{% endfor %}</table></div>
<h3>Маҳсулоти бо зарар фурӯхташуда</h3>
<div class="tablewrap"><table><tr><th>Маҳсулот</th><th>Дона</th><th>Фоида / зарар</th></tr>
{% for p in finance.loss_products %}<tr><td>{{p.name}}</td><td>{{p.quantity}}</td><td><b>{{money(p.profit)}}</b></td></tr>{% else %}<tr><td colspan="3">Маҳсулоти бо зарар фурӯхташуда ёфт нашуд.</td></tr>{% endfor %}</table></div>
<h3>Иловаи хароҷот</h3><form method="post" action="/admin/expense/add" class="cols">
<input type="hidden" name="_csrf" value="{{csrf_token()}}">
<div><label>Номи хароҷот *</label><input class="field" name="title" maxlength="120" placeholder="Масалан: Расонидан" required></div>
<div><label>Категория</label><select class="field" name="category"><option>Расонидан</option><option>Бастабандӣ</option><option>Реклама</option><option>Иҷора</option><option>Коммуналӣ</option><option>Дигар</option></select></div>
<div><label>Маблағ (сомонӣ) *</label><input class="field" type="number" name="amount" min="0.01" max="10000000" step="0.01" required></div>
<div><label>Шарҳ</label><input class="field" name="note" maxlength="300" placeholder="Ихтиёрӣ"></div>
<div style="display:flex;align-items:end"><button class="btn">Сабти хароҷот</button></div></form>
<h3>Хароҷотҳои охирин</h3><div class="tablewrap"><table><tr><th>Сана</th><th>Ном</th><th>Категория</th><th>Маблағ</th><th>Шарҳ</th><th></th></tr>
{% for e in finance.recent %}<tr><td>{{e.created_at}}</td><td>{{e.title}}</td><td>{{e.category}}</td><td>{{money(e.amount)}}</td><td>{{e.note or '—'}}</td><td><form method="post" action="/admin/expense/delete/{{e.id}}" onsubmit="return confirm('Ин хароҷот нест карда шавад?')"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red sm">Нест</button></form></td></tr>{% else %}<tr><td colspan="6">Ҳоло хароҷот сабт нашудааст.</td></tr>{% endfor %}</table></div></div>
<div class="panel"><h2>Маҳсулотҳо ({{products|length}})</h2>
<form method="get" action="/admin" class="cols" style="margin-bottom:14px">
<div><label>Ҷустуҷӯ аз рӯйи ном</label><input class="field" name="q" value="{{filters.q}}" placeholder="Номи маҳсулот"></div>
<div><label>Категория</label><select class="field" name="category"><option value="">Ҳамаи категорияҳо</option>{% for c in categories %}<option value="{{c}}" {{'selected' if filters.category==c}}>{{c}}</option>{% endfor %}</select></div>
<div><label>Ҳолати маҳсулот</label><select class="field" name="state"><option value="all" {{'selected' if filters.state=='all'}}>Ҳама</option><option value="active" {{'selected' if filters.state=='active'}}>Фаъол</option><option value="hidden" {{'selected' if filters.state=='hidden'}}>Пинҳон</option><option value="low" {{'selected' if filters.state=='low'}}>Анбори кам (≤5)</option></select></div>
<div style="display:flex;align-items:end;gap:8px"><button class="btn">Ҷустуҷӯ</button><a class="btn dark" href="/admin">Тоза кардан</a></div>
</form>
<div class="tablewrap"><table><tr><th>Ном</th><th>Нарх</th><th>Анбор</th><th></th></tr>{% for p in products %}<tr><td>{{p.name}}<br><span class="muted">{{p.category}}{% if not p.active %} · пинҳон{% endif %}</span></td><td>{{money(eff(p))}}</td><td>{% if p.stock<=5 %}<span class="pill warn">{{p.stock}}</span>{% else %}{{p.stock}}{% endif %}</td><td style="display:flex;gap:6px"><a class="btn dark sm" href="/admin/edit/{{p.id}}">Таҳрир</a><form method="post" action="/admin/delete/{{p.id}}" onsubmit="return confirm('Нест карда шавад?')"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red sm">Нест</button></form></td></tr>{% else %}<tr><td colspan="4">Маҳсулот ёфт нашуд.</td></tr>{% endfor %}</table></div></div>
<div class="panel"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap"><h2>{{ "Заказы" if admin_lang=="ru" else "Фармоишҳо" }}</h2><a class="btn ghost sm" href="/admin/customers">{{ "Клиенты" if admin_lang=="ru" else "Муштариён" }}</a><a class="btn ghost sm" href="/admin/reviews">{{ "Отзывы" if admin_lang=="ru" else "Рейтингҳо" }}</a><a class="btn ghost sm" href="/admin/report/print?report={{report_period}}" target="_blank">{{ "Отчёт PDF / Печать" if admin_lang=="ru" else "Ҳисоботи PDF / Чоп" }}</a> <a class="btn ghost sm" href="/admin/export.csv">{{ "Скачать CSV" if admin_lang=="ru" else "Боргирии CSV" }}</a></div><form method="get" action="/admin" style="display:flex;gap:8px;margin:10px 0;flex-wrap:wrap"><input name="order_q" value="{{order_q}}" placeholder="{{ 'ID / имя / телефон / адрес' if admin_lang=='ru' else 'ID / ном / телефон / суроға' }}" style="flex:1;min-width:180px"><button class="btn dark sm">{{ "Найти" if admin_lang=="ru" else "Ҷустуҷӯ" }}</button><a class="btn ghost sm" href="/admin">{{ "Сбросить" if admin_lang=="ru" else "Тоза" }}</a></form><div class="tablewrap"><table><tr><th>№</th><th>{{ "Покупатель" if admin_lang=="ru" else "Харидор" }}</th><th>{{ "Итого" if admin_lang=="ru" else "Ҳамагӣ" }}</th><th>{{ "Статус" if admin_lang=="ru" else "Вазъият" }}</th><th>{{ "Дата" if admin_lang=="ru" else "Сана" }}</th><th></th></tr>{% for o in orders %}<tr><td>#{{o.id}}</td><td>{{o.customer_name}}<br><span class="muted">{{o.phone}}</span></td><td>{{money(o.total)}}</td><td>{{o.status}}</td><td>{{o.created_at}}</td><td><a class="btn dark sm" href="/admin/order/{{o.id}}">{{ "Открыть" if admin_lang=="ru" else "Кушодан" }}</a></td></tr>{% endfor %}</table></div></div></main>"""


@app.route("/admin/banner", methods=["GET", "POST"])
@admin_required
def admin_banner():
    d = db()
    current = d.execute("SELECT * FROM banners WHERE id=1").fetchone()
    if request.method == "POST":
        title = request.form.get("title", "").strip()[:100]
        subtitle = request.form.get("subtitle", "").strip()[:240]
        button_text = request.form.get("button_text", "").strip()[:40]
        button_url = request.form.get("button_url", "").strip()[:300]
        start_date = request.form.get("start_date", "").strip()[:10]
        end_date = request.form.get("end_date", "").strip()[:10]
        active = 1 if request.form.get("active") == "1" else 0
        image = current["image"] if current else ""
        upload = request.files.get("image")
        if upload and upload.filename:
            saved = save_image(upload)
            if not saved:
                flash("Файл тасвир эътироф нашуд. JPG, PNG ё WEBP истифода кунед.", "err")
                return redirect(url_for("admin_banner"))
            image = saved
        if button_url and not (button_url.startswith("/") and not button_url.startswith("//") or button_url.startswith("https://")):
            flash("Суроғаи тугма бояд / ё https:// бошад.", "err")
            return redirect(url_for("admin_banner"))
        try:
            if start_date: __import__("datetime").date.fromisoformat(start_date)
            if end_date: __import__("datetime").date.fromisoformat(end_date)
        except ValueError:
            flash("Сана нодуруст аст.", "err")
            return redirect(url_for("admin_banner"))
        if start_date and end_date and start_date > end_date:
            flash("Санаи оғоз баъд аз санаи анҷом буда наметавонад.", "err")
            return redirect(url_for("admin_banner"))
        if not title:
            flash("Сарлавҳаи баннерро нависед.", "err")
            return redirect(url_for("admin_banner"))
        d.execute("INSERT INTO banners(id,title,subtitle,image,button_text,button_url,start_date,end_date,active,updated_at) VALUES(1,?,?,?,?,?,?,?, ?, ?,CURRENT_TIMESTAMP) ON CONFLICT(id) DO UPDATE SET title=excluded.title,subtitle=excluded.subtitle,image=excluded.image,button_text=excluded.button_text,button_url=excluded.button_url,start_date=excluded.start_date,end_date=excluded.end_date,active=excluded.active,updated_at=CURRENT_TIMESTAMP", (title,subtitle,image,button_text,button_url,start_date,end_date,active))
        flash("Баннери реклама сабт шуд.", "ok")
        return redirect(url_for("admin_banner"))
    current = d.execute("SELECT * FROM banners WHERE id=1").fetchone()
    body = """<main class="wrap" style="max-width:800px"><div class="panel"><h1 class="gold">Баннери реклама</h1><p class="muted">Баннер дар саҳифаи аввали мағоза намоиш мешавад. Мӯҳлати санаҳо ихтиёрӣ аст.</p><form method="post" enctype="multipart/form-data"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Сарлавҳа *</label><input class="field" name="title" maxlength="100" required value="{{b.title if b else ''}}" placeholder="Масалан: Тахфифи махсус"><label>Матни иловагӣ</label><textarea class="field" name="subtitle" maxlength="240" rows="3">{{b.subtitle if b else ''}}</textarea><label>Расм (JPG, PNG ё WEBP)</label><input class="field" type="file" name="image" accept="image/jpeg,image/png,image/webp">{% if b and b.image %}<p><img src="{{url_for('static',filename='uploads/'+b.image)}}" style="width:100%;max-height:260px;object-fit:contain;border-radius:12px"></p>{% endif %}<label>Матни тугма</label><input class="field" name="button_text" maxlength="40" value="{{b.button_text if b else ''}}" placeholder="Ҳозир харид кардан"><label>Суроғаи тугма (/product/1 ё https://...)</label><input class="field" name="button_url" maxlength="300" value="{{b.button_url if b else ''}}" placeholder="/"><div class="cols"><div><label>Санаи оғоз</label><input class="field" type="date" name="start_date" value="{{b.start_date if b else ''}}"></div><div><label>Санаи анҷом</label><input class="field" type="date" name="end_date" value="{{b.end_date if b else ''}}"></div></div><label><input type="checkbox" name="active" value="1" {{'checked' if b and b.active}}> Баннер фаъол бошад</label><button class="btn block" style="margin-top:12px">Сабт кардан</button></form><a class="btn dark block" style="margin-top:10px" href="/admin">Бозгашт ба Admin</a></div></main>"""
    return page(body, title="Баннери реклама", admin=True, b=current)


@app.route("/admin/lang")
@admin_required
def admin_language():
    lang = request.args.get("lang", "tg")
    session["admin_lang"] = "ru" if lang == "ru" else "tg"
    return redirect("/admin")


@app.route("/admin")
@admin_required
def admin():
    d = db()
    q = request.args.get("q", "").strip()[:100]
    category = request.args.get("category", "").strip()[:80]
    state = request.args.get("state", "all")
    if state not in {"all", "active", "hidden", "low"}:
        state = "all"
    where, args = [], []
    if q:
        where.append("name LIKE ?")
        args.append("%" + q + "%")
    if category:
        where.append("category=?")
        args.append(category)
    if state == "active":
        where.append("active=1")
    elif state == "hidden":
        where.append("active=0")
    elif state == "low":
        where.extend(["active=1", "stock<=5"])
    sql = "SELECT * FROM products" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT 300"
    products = d.execute(sql, args).fetchall()
    categories = [r[0] for r in d.execute("SELECT DISTINCT category FROM products WHERE category IS NOT NULL AND category!='' ORDER BY category").fetchall()]
    s = dict(pc=d.execute("SELECT COUNT(*) FROM products").fetchone()[0],
             oc=d.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
             today=d.execute("SELECT COUNT(*) FROM orders WHERE date(created_at)=date('now')").fetchone()[0],
             rev=d.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='Бекор'").fetchone()[0],
             low=d.execute("SELECT COUNT(*) FROM products WHERE active=1 AND stock<=5").fetchone()[0])
    report_period = request.args.get("report", "30")
    if report_period not in {"7", "30", "90", "all"}:
        report_period = "30"
    date_filter = "" if report_period == "all" else " AND datetime(created_at)>=datetime('now', ?)"
    date_args = [] if report_period == "all" else [f"-{int(report_period)} days"]
    sales_row = d.execute("SELECT COUNT(*) AS n, COALESCE(SUM(CASE WHEN status!='Бекор' THEN total ELSE 0 END),0) AS revenue, SUM(CASE WHEN status='Анҷом ёфт' THEN 1 ELSE 0 END) AS done FROM orders WHERE 1=1" + date_filter, date_args).fetchone()
    # Parse order snapshots in Python so this works with SQLite builds without JSON1.
    best = {}
    completed_orders = d.execute("SELECT items_json FROM orders WHERE status!='Бекор'" + date_filter, date_args).fetchall()
    for order in completed_orders:
        try:
            items = json.loads(order["items_json"] or "[]")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                pid = item.get("id")
                try:
                    qty = max(0, int(item.get("quantity", 0)))
                    price = max(0.0, float(item.get("price", 0)))
                    pid = int(pid)
                except (TypeError, ValueError, OverflowError):
                    continue
                if qty == 0:
                    continue
                row = best.setdefault(pid, {"name": str(item.get("name", "Маҳсулоти нестшуда"))[:120], "quantity": 0, "revenue": 0.0})
                row["quantity"] += qty
                row["revenue"] += price * qty
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    top_products = sorted(best.values(), key=lambda x: (x["quantity"], x["revenue"]), reverse=True)[:10]
    sales = dict(count=sales_row["n"], revenue=sales_row["revenue"],
                 average=(sales_row["revenue"] / max(1, sales_row["n"])),
                 done=sales_row["done"] or 0,
                 statuses=d.execute("SELECT status, COUNT(*) AS n FROM orders WHERE 1=1" + date_filter + " GROUP BY status ORDER BY n DESC", date_args).fetchall(),
                 top_products=top_products)
    stock_alert_rows = d.execute(
        "SELECT id,name,category,stock FROM products WHERE active=1 AND stock<=5 ORDER BY stock ASC,name COLLATE NOCASE LIMIT 100"
    ).fetchall()
    stock_alerts = {
        "items": stock_alert_rows,
        "out_count": sum(1 for row in stock_alert_rows if row["stock"] <= 0),
        "low_count": sum(1 for row in stock_alert_rows if 1 <= row["stock"] <= 5),
    }
    stock_history = d.execute("SELECT * FROM stock_movements ORDER BY id DESC LIMIT 100").fetchall()
    expense_filter = "" if report_period == "all" else " AND datetime(created_at)>=datetime('now', ?)"
    expense_args = [] if report_period == "all" else [f"-{int(report_period)} days"]
    expense_row = d.execute("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS total FROM expenses WHERE 1=1" + expense_filter, expense_args).fetchone()
    # Cost is taken from each order snapshot, so later product edits do not rewrite old costs.
    cost_rows = d.execute("SELECT items_json,total FROM orders WHERE status!='Бекор'" + date_filter, date_args).fetchall()
    cogs = 0.0
    cost_known_revenue = 0.0
    cost_unknown_items = 0
    product_profit = {}
    for order in cost_rows:
        try:
            entries = json.loads(order["items_json"] or "[]")
            if not isinstance(entries, list):
                continue
            clean = [x for x in entries if isinstance(x, dict)]
            gross_subtotal = sum(max(0, int(x.get("quantity", 0))) * max(0.0, float(x.get("price", 0))) for x in clean)
            order_total = max(0.0, float(order["total"] or 0))
            discount_factor = min(1.0, order_total / gross_subtotal) if gross_subtotal > 0 else 1.0
            for item in clean:
                qty = max(0, int(item.get("quantity", 0)))
                price = max(0.0, float(item.get("price", 0)))
                if qty <= 0:
                    continue
                pid = str(item.get("id", "unknown"))
                pname = str(item.get("name", "Маҳсулоти номаълум"))[:120]
                row = product_profit.setdefault(pid, {"id": pid, "name": pname, "quantity": 0, "revenue": 0.0, "cost": 0.0, "profit": 0.0, "unknown": False})
                row["quantity"] += qty
                if "cost_price" not in item:
                    cost_unknown_items += qty
                    row["unknown"] = True
                    continue
                unit_cost = max(0.0, float(item.get("cost_price", 0)))
                net_revenue = price * qty * discount_factor
                item_cost = unit_cost * qty
                item_profit = net_revenue - item_cost
                cogs += item_cost
                cost_known_revenue += net_revenue
                row["revenue"] += net_revenue
                row["cost"] += item_cost
                row["profit"] += item_profit
        except (TypeError, ValueError, json.JSONDecodeError, OverflowError):
            continue
    expenses_total = float(expense_row["total"] or 0)
    known_gross = cost_known_revenue - cogs
    finance = dict(revenue=float(sales_row["revenue"] or 0),
                   expenses=expenses_total,
                   balance=float(sales_row["revenue"] or 0) - expenses_total,
                   cogs=cogs, cost_known_revenue=cost_known_revenue,
                   gross_profit=known_gross, estimated_net=known_gross - expenses_total,
                   cost_unknown_items=cost_unknown_items,
                   products=sorted(product_profit.values(), key=lambda x: (x["profit"], x["quantity"]), reverse=True),
                   loss_products=[x for x in product_profit.values() if not x["unknown"] and x["profit"] < 0],
                   expense_count=expense_row["n"],
                   recent=d.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT 100").fetchall())
    order_q = request.args.get("order_q", "").strip()[:100]
    order_where, order_args = [], []
    if order_q:
        if order_q.isdigit():
            order_where.append("id=?")
            order_args.append(int(order_q))
        order_where.extend(["customer_name LIKE ?", "phone LIKE ?", "address LIKE ?"])
        pattern = "%" + order_q + "%"
        order_args.extend([pattern, pattern, pattern])
    order_sql = "SELECT * FROM orders" + (" WHERE " + " OR ".join(order_where) if order_where else "") + " ORDER BY id DESC LIMIT 100"
    return page(ADMIN, title="Admin", admin_lang=session.get("admin_lang", "tg"), products=products, categories=categories,
                filters={"q": q, "category": category, "state": state}, report_period=report_period, sales=sales, finance=finance, stock_alerts=stock_alerts,
                stock_history=stock_history, order_q=order_q,
                orders=d.execute(order_sql, order_args).fetchall(),
                promos=d.execute("SELECT * FROM promos ORDER BY code").fetchall(), auto_discount=d.execute("SELECT * FROM auto_discount WHERE id=1").fetchone(), today=__import__("datetime").date.today().isoformat(), s=s, delivery_zones_admin=d.execute("SELECT * FROM delivery_zones ORDER BY id").fetchall())



# ---------- V69: Admin analytics dashboard ----------
ADMIN_DASHBOARD = """<main class="wrap"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap"><h1 class="gold">📊 Dashboard — омори мағоза</h1><a class="btn dark" href="/admin">← Ба панели Admin</a></div>
<div class="statgrid"><div class="stat">Ҳамаи фармоишҳо<b>{{kpi.orders}}</b></div><div class="stat">Фурӯши 7 рӯз<b>{{money(kpi.revenue)}} сом.</b></div><div class="stat">Имрӯз<b>{{kpi.today}}</b></div><div class="stat">Маҳсулоти фаъол<b>{{kpi.products}}</b></div><div class="stat">Муштариёни нодир<b>{{kpi.customers}}</b></div><div class="stat">Анбори кам (≤5)<b>{{kpi.low}}</b></div></div>
<div class="panel"><h2>Фурӯш дар 7 рӯзи охир</h2><p class="muted">Танҳо фармоишҳои бекорнашуда ҳисоб шудаанд. Баландии сутунҳо аз рӯйи маблағи рӯзона аст.</p>
<div style="display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px;align-items:end;min-height:190px">{% for day in days %}<div style="text-align:center;min-width:0"><div class="muted" style="font-size:11px;overflow-wrap:anywhere">{{money(day.revenue)}}</div><div style="height:130px;display:flex;align-items:end;justify-content:center;background:#17191f;border-radius:9px;padding:4px"><div title="{{day.label}}: {{money(day.revenue)}} сомонӣ" style="width:75%;height:{{day.height}}%;min-height:3px;background:linear-gradient(180deg,#e5c36a,#94702b);border-radius:6px 6px 3px 3px"></div></div><b style="font-size:11px">{{day.short}}</b></div>{% endfor %}</div></div>
<div class="panel"><h2>Фармоишҳо аз рӯйи вазъият</h2><div class="tablewrap"><table><tr><th>Вазъият</th><th>Шумора</th></tr>{% for s in statuses %}<tr><td>{{s.status}}</td><td><b>{{s.n}}</b></td></tr>{% else %}<tr><td colspan="2">Ҳоло фармоиш нест.</td></tr>{% endfor %}</table></div></div>
<div class="panel"><h2>Огоҳии анбор</h2><div class="tablewrap"><table><tr><th>Маҳсулот</th><th>Боқимонда</th></tr>{% for p in low_items %}<tr><td>{{p.name}}</td><td>{{p.stock}}</td></tr>{% else %}<tr><td colspan="2">Маҳсулоти каммонда нест.</td></tr>{% endfor %}</table></div></div></main>"""

@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    d = db()
    kpi = {
        "orders": d.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
        "revenue": float(d.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='Бекор' AND date(created_at)>=date('now','-6 days')").fetchone()[0] or 0),
        "today": d.execute("SELECT COUNT(*) FROM orders WHERE date(created_at)=date('now')").fetchone()[0],
        "products": d.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0],
        "customers": d.execute("SELECT COUNT(DISTINCT phone) FROM orders WHERE phone IS NOT NULL AND phone!=''").fetchone()[0],
        "low": d.execute("SELECT COUNT(*) FROM products WHERE active=1 AND stock<=5").fetchone()[0],
    }
    rows = d.execute("SELECT date(created_at) AS day, COALESCE(SUM(CASE WHEN status!='Бекор' THEN total ELSE 0 END),0) AS revenue FROM orders WHERE date(created_at)>=date('now','-6 days') GROUP BY date(created_at)").fetchall()
    by_day = {r["day"]: float(r["revenue"] or 0) for r in rows}
    from datetime import date, timedelta
    today = date.today()
    values = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        key = day.isoformat()
        values.append({"label": key, "short": day.strftime("%d.%m"), "revenue": by_day.get(key, 0.0)})
    peak = max((x["revenue"] for x in values), default=0.0)
    for item in values:
        item["height"] = max(3, round(item["revenue"] / peak * 100)) if peak > 0 else 3
    statuses = d.execute("SELECT status,COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC").fetchall()
    low_items = d.execute("SELECT name,stock FROM products WHERE active=1 AND stock<=5 ORDER BY stock ASC,name COLLATE NOCASE LIMIT 30").fetchall()
    return page(ADMIN_DASHBOARD, title="NEXVORA — Dashboard", admin=True, admin_lang=session.get("admin_lang", "tg"),
                kpi=kpi, days=values, statuses=statuses, low_items=low_items)


# ---------- V70: Persistent admin notification center ----------
ADMIN_NOTIFICATIONS = """<main class="wrap"><div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap"><h1 class="gold">🔔 Огоҳиномаҳои Admin</h1><a class="btn dark" href="/admin">← Панели Admin</a></div>
<div class="panel"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap"><p>Ҳама: <b>{{total}}</b> · Хонда нашуда: <b>{{unread}}</b></p><form method="post" action="/admin/notifications/read-all"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn gold sm" {% if unread==0 %}disabled{% endif %}>Ҳамаро хондашуда кун</button></form></div>
{% for n in notices %}<article class="panel" style="margin:10px 0;border:1px solid {{'var(--gold)' if not n.is_read else 'var(--line)'}}"><div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><b>{{'🛒' if n.kind=='order' else '📩'}} {{n.title}}</b><span class="pill">{{'Нав' if not n.is_read else 'Хонда шуд'}}</span></div><p>{{n.body}}</p><small class="muted">{{n.created_at}}</small><div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px"><a class="btn dark sm" href="{{n.target_url}}">Кушодан</a>{% if not n.is_read %}<form method="post" action="/admin/notifications/read/{{n.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn gold sm">Хонда шуд</button></form>{% endif %}</div></article>{% else %}<p class="muted">Ҳоло огоҳинома нест. Ҳангоми фармоиши нав ё паёми муштарӣ огоҳинома пайдо мешавад.</p>{% endfor %}</div></main>"""

@app.route("/admin/notifications")
@admin_required
def admin_notifications():
    d = db()
    notices = d.execute("SELECT * FROM admin_notifications ORDER BY id DESC LIMIT 200").fetchall()
    total = d.execute("SELECT COUNT(*) FROM admin_notifications").fetchone()[0]
    unread = d.execute("SELECT COUNT(*) FROM admin_notifications WHERE is_read=0").fetchone()[0]
    return page(ADMIN_NOTIFICATIONS, title="Огоҳиномаҳо", admin=True,
                notices=notices, total=total, unread=unread)

@app.route("/admin/notifications/read/<int:nid>", methods=["POST"])
@admin_required
def admin_notification_read(nid):
    db().execute("UPDATE admin_notifications SET is_read=1 WHERE id=?", (nid,))
    return redirect("/admin/notifications")

@app.route("/admin/notifications/read-all", methods=["POST"])
@admin_required
def admin_notifications_read_all():
    db().execute("UPDATE admin_notifications SET is_read=1 WHERE is_read=0")
    return redirect("/admin/notifications")


GIFT_CARDS_ADMIN = """<main class="wrap"><h1 class="gold">🎁 Сертификатҳои тӯҳфа</h1><p><a class="btn dark" href="/admin">← Панели Admin</a></p><div class="panel"><h2>Сохтани сертификат</h2><form method="post" action="/admin/gift-cards/create"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Маблағ (сомонӣ)</label><input class="field" type="number" name="amount" min="1" max="100000" step="0.01" required><label>Телефони қабулкунанда (ихтиёрӣ; барои маҳдуд кардани истифода)</label><input class="field" name="phone" maxlength="30" placeholder="+992..."><label>Мӯҳлат (ихтиёрӣ)</label><input class="field" type="date" name="expires_on"><button class="btn" style="margin-top:10px">Сохтан</button></form></div><div class="panel"><h2>Сертификатҳои сохташуда</h2><div class="tablewrap"><table><tr><th>Код</th><th>Маблағи аввал</th><th>Бақия</th><th>Телефон</th><th>Мӯҳлат</th><th>Ҳолат</th><th>Амалиёт</th></tr>{% for c in cards %}<tr><td><b>{{c.code}}</b></td><td>{{money(c.initial_amount)}}</td><td>{{money(c.balance)}}</td><td>{{c.recipient_phone or 'Ҳама'}}</td><td>{{c.expires_on or 'Бемӯҳлат'}}</td><td>{{'Фаъол' if c.active else 'Хомӯш'}}</td><td><form method="post" action="/admin/gift-cards/toggle/{{c.code}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">{{'Хомӯш' if c.active else 'Фаъол'}}</button></form></td></tr>{% else %}<tr><td colspan="7">Ҳоло сертификат нест.</td></tr>{% endfor %}</table></div></div></main>"""

@app.route("/admin/gift-cards")
@admin_required
def admin_gift_cards():
    cards = db().execute("SELECT * FROM gift_cards ORDER BY created_at DESC").fetchall()
    return page(GIFT_CARDS_ADMIN, title="Gift Cards", cards=cards)

@app.route("/admin/gift-cards/create", methods=["POST"])
@admin_required
def admin_gift_cards_create():
    try:
        amount = float(request.form.get("amount", "0"))
    except (TypeError, ValueError):
        amount = 0
    phone = request.form.get("phone", "").strip()[:30]
    expires = request.form.get("expires_on", "").strip()[:10]
    if amount <= 0 or amount > 100000:
        flash("Маблағ нодуруст аст.", "err")
        return redirect("/admin/gift-cards")
    if phone and len(re.sub(r"\D", "", phone)) < 7:
        flash("Рақами телефон нодуруст аст.", "err")
        return redirect("/admin/gift-cards")
    code = "GIFT-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(10))
    db().execute("INSERT INTO gift_cards(code,initial_amount,balance,recipient_phone,expires_on) VALUES(?,?,?,?,?)", (code, amount, amount, phone, expires))
    flash("Сертификат сохта шуд: " + code, "ok")
    return redirect("/admin/gift-cards")

@app.route("/admin/gift-cards/toggle/<code>", methods=["POST"])
@admin_required
def admin_gift_cards_toggle(code):
    db().execute("UPDATE gift_cards SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE code=?", (code,))
    flash("Ҳолати сертификат тағйир ёфт.", "ok")
    return redirect("/admin/gift-cards")

@app.route("/admin/delivery/add", methods=["POST"])
@admin_required
def admin_delivery_add():
    name = request.form.get("name", "").strip()[:80]
    try:
        fee = float(request.form.get("fee", "-1"))
        free_from = float(request.form.get("free_from", "-1"))
    except (TypeError, ValueError):
        flash("Нарх нодуруст аст.", "err")
        return redirect("/admin")
    if not name or fee < 0 or free_from < 0 or fee > 100000 or free_from > 1000000:
        flash("Маълумоти минтақа нодуруст аст.", "err")
        return redirect("/admin")
    try:
        db().execute("INSERT INTO delivery_zones(name,fee,free_from,active) VALUES(?,?,?,1)", (name, fee, free_from))
        flash("Минтақа илова шуд.", "ok")
    except sqlite3.IntegrityError:
        flash("Ин номи минтақа аллакай вуҷуд дорад.", "err")
    return redirect("/admin")

@app.route("/admin/delivery/toggle/<int:zone_id>", methods=["POST"])
@admin_required
def admin_delivery_toggle(zone_id):
    db().execute("UPDATE delivery_zones SET active=CASE WHEN active=1 THEN 0 ELSE 1 END WHERE id=?", (zone_id,))
    flash("Ҳолати минтақа тағйир ёфт.", "ok")
    return redirect("/admin")

@app.route("/admin/delivery/delete/<int:zone_id>", methods=["POST"])
@admin_required
def admin_delivery_delete(zone_id):
    db().execute("DELETE FROM delivery_zones WHERE id=?", (zone_id,))
    flash("Минтақа нест карда шуд.", "ok")
    return redirect("/admin")

@app.route("/admin/customers")
@admin_required
def admin_customers():
    d = db()
    q = request.args.get("q", "").strip()[:100]
    where, args = "", []
    if q:
        where = " WHERE customer_name LIKE ? OR phone LIKE ?"
        pat = "%" + q + "%"
        args = [pat, pat]
    customers = d.execute(
        "SELECT phone, MAX(customer_name) AS customer_name, "
        "SUM(CASE WHEN status!='Бекор' THEN 1 ELSE 0 END) AS valid_orders, "
        "SUM(CASE WHEN status!='Бекор' THEN total ELSE 0 END) AS total_spent, "
        "MAX(created_at) AS last_order FROM orders" + where +
        " GROUP BY phone ORDER BY last_order DESC LIMIT 300", args
    ).fetchall()
    lang = session.get("admin_lang", "tg")
    body = '''<main class="wrap"><div class="panel"><div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px"><h1>{{ 'Клиенты' if lang=='ru' else 'Муштариён' }}</h1><a class="btn dark sm" href="/admin">{{ 'Назад' if lang=='ru' else 'Бозгашт' }}</a></div>
    <form method="get" style="display:flex;gap:8px;flex-wrap:wrap;margin:12px 0"><input class="field" name="q" value="{{q}}" placeholder="{{ 'Имя или телефон' if lang=='ru' else 'Ном ё рақами телефон' }}" style="flex:1;min-width:180px"><button class="btn">{{ 'Поиск' if lang=='ru' else 'Ҷустуҷӯ' }}</button><a class="btn ghost" href="/admin/customers">{{ 'Сброс' if lang=='ru' else 'Тоза' }}</a></form>
    <div class="tablewrap"><table><tr><th>{{ 'Имя' if lang=='ru' else 'Ном' }}</th><th>Телефон</th><th>{{ 'Заказы' if lang=='ru' else 'Фармоишҳо' }}</th><th>{{ 'Покупки' if lang=='ru' else 'Маблағи харид' }}</th><th>{{ 'Последний заказ' if lang=='ru' else 'Фармоиши охирин' }}</th><th></th></tr>{% for c in customers %}<tr><td>{{c.customer_name}}</td><td><a href="tel:{{c.phone}}">{{c.phone}}</a></td><td>{{c.valid_orders}}</td><td>{{money(c.total_spent)}}</td><td>{{c.last_order}}</td><td><a class="btn dark sm" href="/admin/customer?phone={{c.phone|urlencode}}">{{ 'Открыть' if lang=='ru' else 'Кушодан' }}</a></td></tr>{% else %}<tr><td colspan="6">{{ 'Не найдено' if lang=='ru' else 'Муштарӣ ёфт нашуд' }}</td></tr>{% endfor %}</table></div></div></main>'''
    return page(body, title="Customers", customers=customers, q=q, lang=lang, admin=True, admin_lang=lang)


@app.route("/admin/customer")
@admin_required
def admin_customer_detail():
    phone = request.args.get("phone", "").strip()[:100]
    if not phone:
        return redirect(url_for("admin_customers"))
    orders = db().execute("SELECT * FROM orders WHERE phone=? ORDER BY id DESC LIMIT 200", (phone,)).fetchall()
    if not orders:
        return redirect(url_for("admin_customers"))
    name = orders[0]["customer_name"]
    valid = [o for o in orders if o["status"] != "Бекор"]
    spent = sum(float(o["total"] or 0) for o in valid)
    points = loyalty_balance(db(), phone)
    lang = session.get("admin_lang", "tg")
    body = '''<main class="wrap"><div class="panel"><div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px"><h1>{{ 'Клиент' if lang=='ru' else 'Муштарӣ' }}</h1><a class="btn dark sm" href="/admin/customers">{{ 'Назад' if lang=='ru' else 'Бозгашт' }}</a></div><h2>{{name}}</h2><p>Телефон: <a href="tel:{{phone}}">{{phone}}</a> · <a href="https://wa.me/{{phone|replace('+','')|replace(' ','')|replace('-','')|replace('(','')|replace(')','')}}">WhatsApp</a></p><div class="statgrid"><div class="stat">{{ 'Заказы' if lang=='ru' else 'Фармоишҳо' }}<b>{{valid_count}}</b></div><div class="stat">{{ 'Сумма покупок' if lang=='ru' else 'Маблағи харид' }}<b>{{money(spent)}}</b></div><div class="stat">{{ 'Бонусы' if lang=='ru' else 'Бонусҳо' }}<b>{{points}} хол</b></div></div><p class="muted">1 хол барои ҳар 100 сомонӣ аз маҳсулот (танҳо фармоиши анҷомёфта).</p><h3>{{ 'История заказов' if lang=='ru' else 'Таърихи фармоишҳо' }}</h3><div class="tablewrap"><table><tr><th>{{ 'Дата' if lang=='ru' else 'Сана' }}</th><th>{{ 'Статус' if lang=='ru' else 'Вазъият' }}</th><th>{{ 'Сумма' if lang=='ru' else 'Маблағ' }}</th><th></th></tr>{% for o in orders %}<tr><td>{{o.created_at}}</td><td>{{o.status}}</td><td>{{money(o.total)}}</td><td><a class="btn dark sm" href="/admin/order/{{o.id}}">{{ 'Открыть' if lang=='ru' else 'Кушодан' }}</a></td></tr>{% endfor %}</table></div></div></main>'''
    return page(body, title="Customer", name=name, phone=phone, orders=orders, valid_count=len(valid), spent=spent, points=points, lang=lang, admin=True, admin_lang=lang)


@app.route("/product/<int:pid>/review", methods=["POST"])
def submit_review(pid):
    product = db().execute("SELECT id FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not product:
        abort(404)
    try:
        rating = int(request.form.get("rating", "5"))
    except (ValueError, TypeError):
        rating = 0
    name = re.sub(r"[\x00-\x1f]", "", request.form.get("customer_name", "").strip())[:60] or "Муштарӣ"
    review_text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", request.form.get("review_text", "").strip())[:500]
    if rating not in range(1, 6):
        flash("Рейтинг аз 1 то 5 интихоб шавад.")
        return redirect(url_for("product_detail", pid=pid) + "#reviews")
    # Simple session cooldown: prevents accidental repeated submissions for this product.
    now = int(time.time())
    recent = session.get("review_cooldowns", {})
    last = int(recent.get(str(pid), 0) or 0)
    if now - last < 60:
        flash("Лутфан, пеш аз фиристодани рейтинги нав 1 дақиқа интизор шавед.")
        return redirect(url_for("product_detail", pid=pid) + "#reviews")
    db().execute("INSERT INTO product_reviews(product_id,customer_name,rating,review_text,is_approved) VALUES(?,?,?,?,0)", (pid, name, rating, review_text))
    recent = {k: v for k, v in recent.items() if now - int(v or 0) < 3600}
    recent[str(pid)] = now
    session["review_cooldowns"] = recent
    flash("Рейтинг қабул шуд. Пас аз санҷиши маъмур дар саҳифа пайдо мешавад. Раҳмат!")
    return redirect(url_for("product_detail", pid=pid) + "#reviews")

@app.route("/admin/reviews")
@admin_required
def admin_reviews():
    lang = session.get("admin_lang", "tg")
    reviews = db().execute("SELECT r.*,p.name AS product_name FROM product_reviews r JOIN products p ON p.id=r.product_id ORDER BY r.id DESC LIMIT 300").fetchall()
    body = """<main class="wrap"><div class="panel"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px"><h1>{{ 'Отзывы' if lang=='ru' else 'Рейтингҳо ва фикрҳо' }}</h1><a class="btn dark sm" href="/admin">{{ 'Назад' if lang=='ru' else 'Бозгашт' }}</a></div><div class="tablewrap"><table><tr><th>{{ 'Товар' if lang=='ru' else 'Маҳсулот' }}</th><th>{{ 'Имя' if lang=='ru' else 'Ном' }}</th><th>{{ 'Оценка' if lang=='ru' else 'Рейтинг' }}</th><th>{{ 'Отзыв' if lang=='ru' else 'Фикр' }}</th><th>{{ 'Дата' if lang=='ru' else 'Сана' }}</th><th>{{ 'Статус' if lang=='ru' else 'Вазъият' }}</th><th></th></tr>{% for r in reviews %}<tr><td>{{r.product_name}}</td><td>{{r.customer_name}}</td><td class="gold">{{'★' * r.rating}}</td><td>{{r.review_text}}</td><td>{{r.created_at}}</td><td>{% if r.is_approved %}<span class="pill">{{ 'Опубликован' if lang=='ru' else 'Нашршуда' }}</span>{% else %}<span class="pill warn">{{ 'На проверке' if lang=='ru' else 'Интизор' }}</span>{% endif %}</td><td>{% if not r.is_approved %}<form method="post" action="/admin/reviews/approve/{{r.id}}" style="display:inline"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn sm">{{ 'Одобрить' if lang=='ru' else 'Тасдиқ' }}</button></form>{% endif %}<form method="post" action="/admin/reviews/delete/{{r.id}}" onsubmit="return confirm({{ ('Удалить отзыв?' if lang=='ru' else 'Фикр нест карда шавад?')|tojson }})"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red sm">{{ 'Удалить' if lang=='ru' else 'Нест' }}</button></form></td></tr>{% else %}<tr><td colspan="7">{{ 'Отзывов пока нет' if lang=='ru' else 'Ҳоло фикре нест' }}</td></tr>{% endfor %}</table></div></div></main>"""
    return page(body, title="Reviews", reviews=reviews, lang=lang, admin=True, admin_lang=lang)


@app.route("/admin/reviews/approve/<int:rid>", methods=["POST"])
@admin_required
def admin_review_approve(rid):
    db().execute("UPDATE product_reviews SET is_approved=1 WHERE id=?", (rid,))
    flash("Фикр тасдиқ ва нашр карда шуд.")
    return redirect(url_for("admin_reviews"))


@app.route("/admin/reviews/delete/<int:rid>", methods=["POST"])
@admin_required
def admin_review_delete(rid):
    db().execute("DELETE FROM product_reviews WHERE id=?", (rid,))
    flash("Фикр нест карда шуд.")
    return redirect(url_for("admin_reviews"))


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
    d = db()
    cur = d.execute(
        """INSERT INTO products(name,price,cost_price,image,video,category,description,stock,sale_price,featured)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (v["name"], v["price"], v["cost_price"], img, vid, v["cat"], v["desc"], v["stock"], v["sale"], v["featured"])
    )
    if v["stock"] > 0:
        log_stock_movement(d, cur.lastrowid, v["name"], 0, v["stock"], "Иловаи маҳсулот", "Маҳсулоти нав")
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
            """UPDATE products SET name=?,category=?,description=?,price=?,cost_price=?,sale_price=?,
            stock=?,image=?,video=?,featured=?,active=? WHERE id=?""",
            (v["name"], v["cat"], v["desc"], v["price"], v["cost_price"], v["sale"], v["stock"],
             img, vid, v["featured"], 1 if request.form.get("active") else 0, pid)
        )
        log_stock_movement(d, pid, v["name"], p["stock"], v["stock"], "Тағйири дастӣ", "Таҳрири маҳсулот")
        flash("Маҳсулот нав шуд.", "ok")
        return redirect("/admin")
    return page(EDIT, title="Таҳрир", p=p)


@app.route("/admin/delete/<int:pid>", methods=["POST"])
@admin_required
def delete_product(pid):
    p = db().execute("SELECT image,video FROM products WHERE id=?", (pid,)).fetchone()
    if p:
        db().execute("DELETE FROM product_reviews WHERE product_id=?", (pid,))
        db().execute("DELETE FROM products WHERE id=?", (pid,))
        drop_image(p["image"])
        drop_video(p["video"])
    flash("Маҳсулот нест карда шуд.", "ok")
    return redirect("/admin")


@app.route("/admin/auto-discount/save", methods=["POST"])
@admin_required
def auto_discount_save():
    try:
        minimum = float(request.form.get("min_amount", "0"))
        percent = int(request.form.get("percent", "0"))
    except (TypeError, ValueError):
        minimum, percent = -1, -1
    active = 1 if request.form.get("active") == "on" else 0
    if minimum < 0 or not 0 <= percent <= 90:
        flash("Маблағ ё фоизи тахфиф нодуруст аст.", "err")
    else:
        db().execute("UPDATE auto_discount SET min_amount=?,percent=?,active=? WHERE id=1", (minimum, percent, active))
        flash("Тахфифи автоматӣ сабт шуд.", "ok")
    return redirect("/admin")


@app.route("/admin/promo/add", methods=["POST"])
@admin_required
def promo_add():
    code = re.sub(r"[^A-Z0-9_-]", "", request.form.get("code", "").upper())[:30]
    try:
        pc, mx = int(request.form.get("percent", "")), int(request.form.get("max_uses", "0") or 0)
    except ValueError:
        pc, mx = 0, -1
    expires = request.form.get("expires_on", "").strip()
    if expires:
        try:
            from datetime import date
            date.fromisoformat(expires)
        except ValueError:
            expires = "INVALID"
    if not code or not 1 <= pc <= 90 or mx < 0 or expires == "INVALID":
        flash("Промокод, фоиз, лимит ё сана нодуруст аст.", "err")
    else:
        try:
            db().execute("INSERT INTO promos(code,percent,max_uses,expires_on) VALUES(?,?,?,?)", (code, pc, mx, expires))
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


# ---------- V63: personal coupons for loyal customers ----------
@app.route("/admin/coupons", methods=["GET", "POST"])
@admin_required
def admin_personal_coupons():
    if request.method == "POST":
        code = re.sub(r"[^A-Z0-9_-]", "", request.form.get("code", "").strip().upper())[:30]
        phone = request.form.get("phone", "").strip()[:30]
        try:
            percent = int(request.form.get("percent", "0"))
        except ValueError:
            percent = 0
        expires = request.form.get("expires_on", "").strip()[:10]
        if expires:
            try:
                from datetime import date
                date.fromisoformat(expires)
            except ValueError:
                expires = "INVALID"
        if not code or len(re.sub(r"\D", "", phone)) < 7 or not 1 <= percent <= 90 or expires == "INVALID":
            flash("Код, телефон, фоиз ё сана нодуруст аст.", "err")
        else:
            try:
                db().execute("INSERT INTO personal_coupons(code,phone,percent,expires_on) VALUES(?,?,?,?)", (code, phone, percent, expires))
                flash("Купони шахсӣ сохта шуд.", "ok")
            except sqlite3.IntegrityError:
                flash("Ин код аллакай вуҷуд дорад.", "err")
        return redirect("/admin/coupons")
    coupons = db().execute("SELECT * FROM personal_coupons ORDER BY created_at DESC").fetchall()
    body = '''<main class="wrap"><div class="panel"><h1>🎟 Купонҳои шахсӣ</h1><p class="muted">Купон ба рақами телефон баста мешавад, як маротиба истифода мешавад.</p><form method="post" class="inline" style="flex-wrap:wrap"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input class="field" name="code" placeholder="Код (VIP10)" maxlength="30" required><input class="field" name="phone" placeholder="Телефон +992..." maxlength="30" required><input class="field" type="number" name="percent" min="1" max="90" placeholder="% тахфиф" required style="max-width:100px"><input class="field" type="date" name="expires_on" title="Мӯҳлат"><button class="btn">Сохтан</button></form><div class="tablewrap" style="margin-top:14px"><table><tr><th>Код</th><th>Телефон</th><th>Тахфиф</th><th>Мӯҳлат</th><th>Вазъ</th><th>Идоракунӣ</th></tr>{% for c in coupons %}<tr><td><b>{{c.code}}</b></td><td>{{c.phone}}</td><td>{{c.percent}}%</td><td>{{c.expires_on or 'Бемӯҳлат'}}</td><td>{{'Истифода шуд' if c.used_order_id else ('Фаъол' if c.active else 'Хомӯш')}}</td><td>{% if not c.used_order_id %}<form method="post" action="/admin/coupons/toggle/{{c.code}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark sm">{{'Хомӯш' if c.active else 'Фаъол'}}</button></form>{% endif %}<form method="post" action="/admin/coupons/delete/{{c.code}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red sm">Нест</button></form></td></tr>{% else %}<tr><td colspan="6">Ҳоло купон нест.</td></tr>{% endfor %}</table></div><a class="btn dark" href="/admin">Ба Admin</a></div></main>'''
    return page(body, title="Купонҳои шахсӣ", coupons=coupons, admin=True, admin_lang=session.get("admin_lang", "tg"))

@app.route("/admin/coupons/toggle/<code>", methods=["POST"])
@admin_required
def admin_personal_coupon_toggle(code):
    db().execute("UPDATE personal_coupons SET active=1-active WHERE code=? AND used_order_id IS NULL", (code,))
    return redirect("/admin/coupons")

@app.route("/admin/coupons/delete/<code>", methods=["POST"])
@admin_required
def admin_personal_coupon_delete(code):
    db().execute("DELETE FROM personal_coupons WHERE code=?", (code,))
    flash("Купон нест карда шуд.", "ok")
    return redirect("/admin/coupons")

@app.route("/admin/expense/add", methods=["POST"])
@admin_required
def expense_add():
    title = request.form.get("title", "").strip()[:120]
    category = request.form.get("category", "Дигар").strip()[:60] or "Дигар"
    note = request.form.get("note", "").strip()[:300]
    try:
        amount = float(request.form.get("amount", ""))
    except (TypeError, ValueError):
        amount = 0
    if not title or not math.isfinite(amount) or not 0 < amount <= 10_000_000:
        flash("Номи хароҷот ё маблағ нодуруст аст.", "err")
        return redirect("/admin")
    db().execute("INSERT INTO expenses(title,category,amount,note) VALUES(?,?,?,?)",
                 (title, category, round(amount, 2), note))
    flash("Хароҷот сабт шуд.", "ok")
    return redirect("/admin")


@app.route("/admin/expense/delete/<int:eid>", methods=["POST"])
@admin_required
def expense_delete(eid):
    db().execute("DELETE FROM expenses WHERE id=?", (eid,))
    flash("Сабти хароҷот нест карда шуд.", "ok")
    return redirect("/admin")


ORDER = """<main class="wrap" style="max-width:760px"><div class="panel"><div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap"><h1 class="gold">{{ 'Заказ №' if admin_lang=='ru' else 'Фармоиши №' }}{{o.id}}</h1><a class="btn dark sm" href="/admin">{{ 'Назад' if admin_lang=='ru' else 'Бозгашт' }}</a></div><p class="muted">{{o.created_at}}</p><p>{{ 'Покупатель' if admin_lang=='ru' else 'Харидор' }}: <b>{{o.customer_name}}</b></p><p>{{ 'Телефон' if admin_lang=='ru' else 'Телефон' }}: <a class="gold" href="tel:{{o.phone}}">{{o.phone}}</a> · <a class="gold" href="https://wa.me/{{o.phone|replace('+','')|replace(' ','')|replace('-','')|replace('(','')|replace(')','')}}">WhatsApp</a></p><p>{{ 'Адрес' if admin_lang=='ru' else 'Суроға' }}: {{o.address or '—'}}</p><p>{{ 'Комментарий' if admin_lang=='ru' else 'Шарҳ' }}: {{o.note or '—'}}</p><h3>{{ 'Товары' if admin_lang=='ru' else 'Маҳсулотҳо' }}</h3>{% for x in its %}<div class="sum"><span>{{x.name}} × {{x.quantity}}</span><span>{{money(x.subtotal)}}</span></div>{% endfor %}{% if o.discount %}<div class="sum"><span>{{ 'Скидка' if admin_lang=='ru' else 'Тахфиф' }} ({{o.promo}})</span><span>−{{money(o.discount)}}</span></div>{% endif %}<div class="sum big"><span>{{ 'Итого' if admin_lang=='ru' else 'Ҳамагӣ' }}</span><span>{{money(o.total)}} {{ 'сомони' if admin_lang=='ru' else 'сомонӣ' }}</span></div><form method="post" onsubmit="return confirmStatus(this)"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>{{ 'Статус заказа' if admin_lang=='ru' else 'Вазъияти фармоиш' }}</label><select class="field" name="status">{% for s in order_statuses %}<option value="{{s}}" {{'selected' if o.status==s}}>{{ {'Нав':'Новый','Тасдиқ шуд':'Подтверждён','Омода мешавад':'Готовится','Фиристода шуд':'Отправлен','Анҷом ёфт':'Завершён','Бекор':'Отменён'}[s] if admin_lang=='ru' else s }}</option>{% endfor %}</select><button class="btn" style="margin-top:12px">{{ 'Сохранить статус' if admin_lang=='ru' else 'Сабт кардани вазъият' }}</button></form><script>function confirmStatus(f){if(f.status.value==='Бекор' && f.status.value!=={{o.status|tojson}}){return confirm({{ ('Отменить заказ? Товар вернётся на склад.' if admin_lang=='ru' else 'Фармоиш бекор шавад? Маҳсулот ба анбор бармегардад.')|tojson }});}return true;}</script><h3>{{ 'История статусов' if admin_lang=='ru' else 'Таърихи вазъият' }}</h3><div class="tablewrap"><table><tr><th>{{ 'Дата' if admin_lang=='ru' else 'Сана' }}</th><th>{{ 'Было' if admin_lang=='ru' else 'Пеш' }}</th><th>{{ 'Стало' if admin_lang=='ru' else 'Баъд' }}</th></tr>{% for h in history %}<tr><td>{{h.changed_at}}</td><td>{{h.old_status or '—'}}</td><td>{{h.new_status}}</td></tr>{% endfor %}</table></div></div></main>"""


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
        old_status = o["status"]
        if s != old_status and (s == "Бекор" or old_status == "Бекор"):
            d.execute("BEGIN IMMEDIATE")
            try:
                order_items = items_of(o)
                if s == "Бекор":
                    for item in order_items:
                        pid, qty = int(item.get("id", 0)), int(item.get("quantity", 0))
                        product = d.execute("SELECT name,stock FROM products WHERE id=?", (pid,)).fetchone()
                        if product and qty > 0:
                            d.execute("UPDATE products SET stock=stock+? WHERE id=?", (qty, pid))
                            log_stock_movement(d, pid, product["name"], product["stock"], product["stock"] + qty,
                                               "Бекор шудани фармоиш", f"Фармоиш №{oid}")
                else:
                    for item in order_items:
                        pid, qty = int(item.get("id", 0)), int(item.get("quantity", 0))
                        product = d.execute("SELECT name,stock FROM products WHERE id=?", (pid,)).fetchone()
                        if not product or qty <= 0 or product["stock"] < qty:
                            raise ValueError("stock")
                        d.execute("UPDATE products SET stock=stock-? WHERE id=?", (qty, pid))
                        log_stock_movement(d, pid, product["name"], product["stock"], product["stock"] - qty,
                                           "Барқарор кардани фармоиш", f"Фармоиш №{oid}")
                d.execute("UPDATE orders SET status=? WHERE id=?", (s, oid))
                d.execute("COMMIT")
            except ValueError:
                d.execute("ROLLBACK")
                flash("Анбор барои фаъол кардани ин фармоиш кофӣ нест.", "err")
                return redirect(url_for("order_detail", oid=oid))
            except Exception:
                d.execute("ROLLBACK")
                raise
        else:
            d.execute("UPDATE orders SET status=? WHERE id=?", (s, oid))
        if s != old_status:
            # Refund redeemed points when an order is cancelled; re-debit on reactivation.
            redeemed = int(o["points_redeemed"] or 0) if "points_redeemed" in o.keys() else 0
            if s == "Бекор" and redeemed > 0:
                n_refunds = d.execute("SELECT COUNT(*) FROM loyalty_transactions WHERE order_id=? AND reason LIKE 'refund_redeem_%'", (oid,)).fetchone()[0]
                apply_loyalty_change(d, o["phone"], o["customer_name"], oid, redeemed, f"refund_redeem_{n_refunds+1}")
            elif old_status == "Бекор" and s != "Бекор" and redeemed > 0:
                n_restores = d.execute("SELECT COUNT(*) FROM loyalty_transactions WHERE order_id=? AND reason LIKE 'redeem_restore_%'", (oid,)).fetchone()[0]
                apply_loyalty_change(d, o["phone"], o["customer_name"], oid, -redeemed, f"redeem_restore_{n_restores+1}")
            # Award 1 point per 100 som of product subtotal when order is completed.
            # Reversal is logged if a completed order is later cancelled.
            if s == "Анҷом ёфт" and old_status != "Анҷом ёфт":
                try:
                    product_sum = max(0.0, float(o["total"] or 0) - float(o["shipping_fee"] or 0))
                except (TypeError, ValueError):
                    product_sum = float(o["total"] or 0)
                earned = int(product_sum // 100)
                apply_loyalty_change(d, o["phone"], o["customer_name"], oid, earned, "award")
            elif old_status == "Анҷом ёфт" and s == "Бекор":
                award = d.execute("SELECT points_change FROM loyalty_transactions WHERE order_id=? AND reason='award'", (oid,)).fetchone()
                if award and award["points_change"]:
                    apply_loyalty_change(d, o["phone"], o["customer_name"], oid, -int(award["points_change"]), "reversal")
            d.execute("INSERT INTO order_status_history(order_id,old_status,new_status) VALUES(?,?,?)", (oid, old_status, s))
            messages = {
                "Нав": "Фармоиши шумо сабт шуд ва интизори баррасӣ аст.",
                "Тасдиқ шуд": "Фармоиши шумо тасдиқ шуд.",
                "Омода мешавад": "Маҳсулоти шумо омода шуда истодааст.",
                "Фиристода шуд": "Фармоиши шумо ба роҳ баромад.",
                "Анҷом ёфт": "Фармоиши шумо расонида шуд. Ташаккур барои харид!",
                "Бекор": "Фармоиши шумо бекор карда шуд. Барои маълумот бо мағоза тамос гиред."
            }
            d.execute("INSERT INTO order_notifications(order_id,status,message) VALUES(?,?,?)", (oid, s, messages.get(s, "Вазъи фармоиш нав шуд.")))
        flash("Вазъи фармоиш нав шуд.", "ok")
        return redirect(url_for("order_detail", oid=oid))
    history = d.execute("SELECT old_status,new_status,changed_at FROM order_status_history WHERE order_id=? ORDER BY id DESC", (oid,)).fetchall()
    return page(ORDER, title=f"Фармоиш №{oid}", o=o, its=items_of(o), history=history, admin_lang=session.get("admin_lang", "tg"))


@app.route("/admin/report/print")
@admin_required
def admin_print_report():
    """Printable finance report; use the browser's Print > Save as PDF."""
    d = db()
    period = request.args.get("report", "30")
    if period not in {"7", "30", "90", "all"}:
        period = "30"
    date_filter = "" if period == "all" else " AND datetime(created_at)>=datetime('now', ?)"
    date_args = [] if period == "all" else [f"-{int(period)} days"]
    orders = d.execute(
        "SELECT id,created_at,status,total,items_json FROM orders WHERE status!='Бекор'" + date_filter + " ORDER BY id DESC",
        date_args
    ).fetchall()
    expenses = d.execute(
        "SELECT title,category,amount,created_at FROM expenses WHERE 1=1" + date_filter + " ORDER BY id DESC",
        date_args
    ).fetchall()
    revenue = sum(max(0.0, float(o["total"] or 0)) for o in orders)
    expenses_total = sum(max(0.0, float(e["amount"] or 0)) for e in expenses)
    products = {}
    cogs = 0.0
    unknown_qty = 0
    for order in orders:
        try:
            items = json.loads(order["items_json"] or "[]")
            if not isinstance(items, list):
                continue
            valid = [x for x in items if isinstance(x, dict)]
            subtotal = sum(max(0, int(x.get("quantity", 0))) * max(0.0, float(x.get("price", 0))) for x in valid)
            total = max(0.0, float(order["total"] or 0))
            factor = min(1.0, total / subtotal) if subtotal > 0 else 1.0
            for item in valid:
                qty = max(0, int(item.get("quantity", 0)))
                if qty <= 0:
                    continue
                pid = str(item.get("id", "?"))
                row = products.setdefault(pid, {"name": str(item.get("name", "Маҳсулот"))[:120], "qty": 0, "revenue": 0.0, "cost": 0.0, "profit": 0.0, "known": True})
                row["qty"] += qty
                if "cost_price" not in item:
                    row["known"] = False
                    unknown_qty += qty
                    continue
                price = max(0.0, float(item.get("price", 0)))
                cost = max(0.0, float(item.get("cost_price", 0))) * qty
                net = price * qty * factor
                row["revenue"] += net
                row["cost"] += cost
                row["profit"] += net - cost
                cogs += cost
        except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
            continue
    product_rows = sorted(products.values(), key=lambda x: (x["profit"], x["qty"]), reverse=True)
    period_label = {"7": "7 рӯзи охир", "30": "30 рӯзи охир", "90": "90 рӯзи охир", "all": "Тамоми давра"}[period]
    report_body = """<main class='wrap print-report'><div class='panel'><div class='report-head'><div><h1 class='gold'>NEXVORA STORE</h1><h2>Ҳисоботи молиявӣ</h2><p class='muted'>Давра: {{period_label}} · Санаи таҳия: {{now}}</p></div><button class='btn no-print' onclick='window.print()'>Чоп / Сабт ҳамчун PDF</button></div>
    <div class='statgrid'><div class='stat'>Фармоишҳои ҳисобшуда<b>{{orders|length}}</b></div><div class='stat'>Даромад<b>{{money(revenue)}} сомонӣ</b></div><div class='stat'>Арзиши хариди мол<b>{{money(cogs)}} сомонӣ</b></div><div class='stat'>Хароҷот<b>{{money(expenses_total)}} сомонӣ</b></div></div>
    <div class='panel'><h3>Натиҷаи молиявӣ</h3><p>Фоидаи умумии маҳсулоти дорои арзиши харид: <b>{{money(known_profit)}} сомонӣ</b></p><p>Баъди тарҳи хароҷот (танҳо аз маълумоти арзиши хариди маълум): <b>{{money(known_profit-expenses_total)}} сомонӣ</b></p><p>Бақияи умумии даромад пас аз хароҷот: <b>{{money(revenue-expenses_total)}} сомонӣ</b></p><p class='muted'>Маҳсулоти фармоишҳои кӯҳна бе арзиши харид: {{unknown_qty}} дона. Ин ҳисобот ҳисоботи идоракунӣ аст, на ҳисоботи расмии андоз.</p></div>
    <h3>Фоидаи маҳсулот</h3><div class='tablewrap'><table><tr><th>Маҳсулот</th><th>Дона</th><th>Даромад</th><th>Арзиши харид</th><th>Фоида</th><th>Маълумот</th></tr>{% for x in product_rows %}<tr><td>{{x.name}}</td><td>{{x.qty}}</td><td>{{money(x.revenue)}}</td><td>{{money(x.cost)}}</td><td>{{money(x.profit)}}</td><td>{{'Нопурра' if not x.known else 'Пурра'}}</td></tr>{% else %}<tr><td colspan='6'>Маълумоти фурӯш нест.</td></tr>{% endfor %}</table></div>
    <h3>Хароҷот</h3><div class='tablewrap'><table><tr><th>Сана</th><th>Ном</th><th>Категория</th><th>Маблағ</th></tr>{% for e in expenses %}<tr><td>{{e.created_at}}</td><td>{{e.title}}</td><td>{{e.category}}</td><td>{{money(e.amount)}}</td></tr>{% else %}<tr><td colspan='4'>Хароҷот сабт нашудааст.</td></tr>{% endfor %}</table></div>
    <p class='muted'>NEXVORA STORE · Ҳуҷҷат барои истифодаи дохилии мағоза</p></div></main><style>@media print{header,footer,.no-print{display:none!important}.wrap{max-width:none!important;padding:0!important}.panel{box-shadow:none!important;border:0!important}.print-report{color:#111!important}body{background:#fff!important}@page{size:A4;margin:12mm}}.report-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}.print-report table{width:100%;border-collapse:collapse}.print-report th,.print-report td{border:1px solid #bbb;padding:7px;text-align:left}.print-report .statgrid{margin:15px 0}</style>"""
    from datetime import datetime
    return page(report_body, title="Ҳисоботи молиявӣ", period_label=period_label,
                now=datetime.now().strftime("%Y-%m-%d %H:%M"), orders=orders,
                revenue=revenue, cogs=cogs, expenses_total=expenses_total,
                known_profit=sum(x["profit"] for x in product_rows),
                unknown_qty=unknown_qty, product_rows=product_rows, expenses=expenses)


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
    original = getattr(e, "original_exception", None)
    if original is not None:
        logger.error("Unhandled request exception", exc_info=(type(original), original, original.__traceback__))
    else:
        logger.error("HTTP 500 error: %s", str(e))
    return err_page(500, "Хатои дохилӣ. Лутфан баъдтар дубора кӯшиш кунед.")


@app.route("/version")
def version():
    return {"store": "NEXVORA STORE", "version": "V75", "status": "ok"}, 200


if __name__ == "__main__":
    logger.info("NEXVORA STORE V75 starting")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
