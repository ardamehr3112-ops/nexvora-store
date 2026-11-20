"""
NEXVORA STORE — Flask интернет-магазин барои омӯзиш ва оғози кори хурд.

Насб:
    pip install flask

Пеш аз истифода:
    1) SECRET_KEY ва ADMIN_PASSWORD-ро дар қисми SETTINGS иваз кунед.
    2) WHATSAPP_NUMBER-ро бо рақами худ пур кунед (танҳо рақамҳо, бе +).
    3) Файлро дар Pydroid 3 иҷро кунед.
    4) Дар Chrome кушоед: http://127.0.0.1:5000

Маълумот дар nexvora_store_v3.db нигоҳ дошта мешавад.
"""

from flask import (
    Flask, request, redirect, url_for, session,
    render_template_string, flash, abort
)
from werkzeug.utils import secure_filename
from decimal import Decimal, InvalidOperation
from functools import wraps
from pathlib import Path
import sqlite3
import os
import re
import secrets
import hmac
import uuid

# ========================= SETTINGS =========================
SECRET_KEY = "Jurajurajurajon123."
ADMIN_PASSWORD = "Jurabekjon123."
WHATSAPP_NUMBER = "992111301210"  # мисол: 992900000000
STORE_NAME = "NEXVORA"
CURRENCY = "сомонӣ"
# ============================================================

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "nexvora_store_v3.db"
UPLOAD_DIR = BASE_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db_connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            category TEXT NOT NULL,
            description TEXT DEFAULT '',
            image TEXT DEFAULT '',
            stock INTEGER NOT NULL DEFAULT 0,
            featured INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            address TEXT NOT NULL,
            note TEXT DEFAULT '',
            total REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'Нав',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER,
            product_name TEXT NOT NULL,
            price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
        );
        """)
    print("NEXVORA STORE DATABASE READY")


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(24)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


def check_csrf():
    sent = request.form.get("_csrf", "")
    saved = session.get("_csrf", "")
    if not sent or not saved or not hmac.compare_digest(sent, saved):
        abort(400, description="Дархости нодуруст. Саҳифаро нав кунед ва дубора кӯшиш кунед.")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return fn(*args, **kwargs)
    return wrapper


def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_image(file):
    if not file or not file.filename:
        return ""
    if not allowed_image(file.filename):
        raise ValueError("Фақат JPG, JPEG, PNG ё WEBP иҷозат аст.")
    ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    file.save(UPLOAD_DIR / filename)
    return filename


def money(value):
    return f"{float(value):,.2f}".rstrip("0").rstrip(".")


app.jinja_env.filters["money"] = money


def get_cart():
    cart = session.get("cart", {})
    return cart if isinstance(cart, dict) else {}


def cart_details():
    cart = get_cart()
    if not cart:
        return [], 0.0
    ids = []
    for key in cart:
        if str(key).isdigit():
            ids.append(int(key))
    if not ids:
        return [], 0.0

    placeholders = ",".join("?" for _ in ids)
    with db_connect() as db:
        rows = db.execute(
            f"SELECT * FROM products WHERE active=1 AND id IN ({placeholders})",
            ids
        ).fetchall()

    items = []
    total = 0.0
    for product in rows:
        qty = max(1, min(int(cart.get(str(product["id"]), 1)), 99))
        line_total = float(product["price"]) * qty
        items.append({"product": product, "quantity": qty, "line_total": line_total})
        total += line_total
    return items, total


def categories_list():
    with db_connect() as db:
        rows = db.execute(
            "SELECT DISTINCT category FROM products WHERE active=1 ORDER BY category"
        ).fetchall()
    return [r["category"] for r in rows]


BASE_CSS = """
<style>
:root{--bg:#0b0c0f;--panel:#15171c;--panel2:#1d2027;--gold:#d7b56d;--gold2:#f0d99b;--text:#f4f1e9;--muted:#a7a7ad;--line:#333640;--red:#c45b5b;--green:#65b88a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:Arial,sans-serif;line-height:1.5}
a{color:var(--gold);text-decoration:none}
a:hover{color:var(--gold2)}
.topline{height:3px;background:linear-gradient(90deg,#80612b,var(--gold2),#80612b)}
header{background:#111318;border-bottom:1px solid #5b4828;position:sticky;top:0;z-index:5}
.head{max-width:1180px;margin:auto;padding:16px 20px;display:flex;align-items:center;justify-content:space-between;gap:14px}
.brand{font-size:25px;letter-spacing:5px;font-weight:800;color:var(--gold)}
.brand small{display:block;font-size:9px;letter-spacing:3px;color:var(--muted);font-weight:normal}
.nav{display:flex;gap:16px;align-items:center;flex-wrap:wrap;font-size:14px}
.nav a{white-space:nowrap}
.container{max-width:1180px;margin:auto;padding:24px 18px}
.hero{padding:48px 25px;border:1px solid #514126;border-radius:18px;background:radial-gradient(circle at 85% 15%,#3a2d19 0,#1b1a18 28%,#111318 70%);margin:10px 0 26px}
.hero h1{font-size:clamp(30px,6vw,54px);line-height:1.08;margin:8px 0;color:var(--gold2)}
.hero p{color:#c6c2b9;max-width:620px}
.eyebrow{color:var(--gold);letter-spacing:3px;font-size:11px;font-weight:bold}
h2,h3{line-height:1.2}
h2{color:var(--gold2);margin:12px 0}
.toolbar{display:grid;grid-template-columns:1fr 210px 150px;gap:10px;margin:20px 0}
input,textarea,select{width:100%;padding:12px 13px;background:#20232a;color:white;border:1px solid #414550;border-radius:9px;margin:5px 0 12px;font:inherit}
input:focus,textarea:focus,select:focus{outline:1px solid var(--gold)}
button,.btn{display:inline-block;background:var(--gold);color:#151515;border:0;border-radius:9px;padding:11px 16px;font-weight:bold;cursor:pointer;text-align:center;font-size:14px}
button:hover,.btn:hover{background:var(--gold2);color:#111}
.btn.secondary,button.secondary{background:#30343d;color:white;border:1px solid #555}
.btn.danger,button.danger{background:#803b3b;color:white}
.btn.small,button.small{padding:8px 11px;font-size:12px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:12px;min-width:0}
.product-image{width:100%;height:205px;object-fit:cover;border-radius:10px;background:#24262c;display:block}
.placeholder{height:205px;border-radius:10px;background:#24262c;display:grid;place-items:center;color:var(--gold);letter-spacing:4px}
.card h3{margin:12px 0 5px;font-size:17px}
.price{color:var(--gold2);font-weight:bold;font-size:19px}
.muted{color:var(--muted);font-size:13px}
.pill{display:inline-block;border:1px solid #67532e;color:var(--gold);border-radius:30px;padding:4px 10px;font-size:11px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px;margin:16px 0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.flash{padding:12px 14px;border-radius:9px;background:#26392e;border:1px solid #41684e;margin:12px 0}
table{width:100%;border-collapse:collapse;min-width:650px}
th,td{text-align:left;padding:11px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}
th{color:var(--gold2)}
.tablewrap{overflow-x:auto}
footer{text-align:center;color:#85858c;border-top:1px solid var(--line);padding:28px;margin-top:35px}
.empty{text-align:center;padding:40px 15px;color:var(--muted)}
label{font-size:13px;color:#d8d3c7}
hr{border:0;border-top:1px solid var(--line);margin:18px 0}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.toolbar{grid-template-columns:1fr 1fr}.toolbar button{grid-column:span 2}}
@media(max-width:560px){.head{align-items:flex-start;flex-direction:column}.nav{gap:12px}.container{padding:16px 12px}.grid{grid-template-columns:1fr 1fr;gap:10px}.product-image,.placeholder{height:145px}.card{padding:9px}.card h3{font-size:14px}.price{font-size:16px}.hero{padding:30px 18px}.two{grid-template-columns:1fr}.toolbar{grid-template-columns:1fr}.toolbar button{grid-column:auto}}
</style>
"""


def page(title, body, **context):
    template = """
    <!doctype html><html lang="tg"><head>
    <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ title }} — NEXVORA</title>""" + BASE_CSS + """
    </head><body><div class="topline"></div>
    <header><div class="head">
      <a class="brand" href="/">NEXVORA<small>PREMIUM ONLINE STORE</small></a>
      <nav class="nav">
        <a href="/">Мағоза</a>
        <a href="/?featured=1">Интихобшуда</a>
        <a href="/cart">Сабад ({{ cart_count }})</a>
        <a href="/admin">Admin</a>
      </nav>
    </div></header>
    <main class="container">
      {% with messages=get_flashed_messages() %}{% for message in messages %}
        <div class="flash">{{ message }}</div>
      {% endfor %}{% endwith %}
      """ + body + """
    </main><footer>© {{ year }} NEXVORA STORE · Хариди осон, интихоби хуб</footer>
    </body></html>
    """
    from datetime import datetime
    cart = get_cart()
    context.update({
        "title": title,
        "cart_count": sum(int(v) for v in cart.values()) if cart else 0,
        "year": datetime.now().year,
        "categories": categories_list()
    })
    return render_template_string(template, **context)


@app.route("/")
def home():
    q = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    sort = request.args.get("sort", "new")
    featured = request.args.get("featured") == "1"

    sql = "SELECT * FROM products WHERE active=1"
    params = []
    if q:
        sql += " AND (name LIKE ? OR description LIKE ? OR category LIKE ?)"
        params += [f"%{q}%"] * 3
    if category:
        sql += " AND category=?"
        params.append(category)
    if featured:
        sql += " AND featured=1"
    if sort == "cheap":
        sql += " ORDER BY price ASC"
    elif sort == "expensive":
        sql += " ORDER BY price DESC"
    else:
        sql += " ORDER BY id DESC"

    with db_connect() as db:
        products = db.execute(sql, params).fetchall()

    body = """
    <section class="hero">
      <div class="eyebrow">WELCOME TO NEXVORA</div>
      <h1>Интихоби нав.<br>Сабки нав.</h1>
      <p>Маҳсулоти дӯстдоштаатро пайдо кун, ба сабад илова намо ва фармоишатро ба осонӣ фирист.</p>
      <a class="btn" href="#products">Дидани маҳсулот</a>
    </section>
    <form class="toolbar" method="get">
      <input name="q" value="{{ q }}" placeholder="Ҷустуҷӯи маҳсулот...">
      <select name="category"><option value="">Ҳама категорияҳо</option>
        {% for c in categories %}<option value="{{ c }}" {% if c==selected_category %}selected{% endif %}>{{ c }}</option>{% endfor %}
      </select>
      <select name="sort">
        <option value="new" {% if sort=="new" %}selected{% endif %}>Навтарин</option>
        <option value="cheap" {% if sort=="cheap" %}selected{% endif %}>Аввал арзон</option>
        <option value="expensive" {% if sort=="expensive" %}selected{% endif %}>Аввал қимат</option>
      </select>
      <button>Ҷустуҷӯ / Филтр</button>
    </form>
    <div id="products"><div class="eyebrow">OUR COLLECTION</div><h2>Маҳсулот</h2></div>
    <div class="grid">
    {% for p in products %}
      <article class="card">
        <a href="{{ url_for('product_detail', product_id=p.id) }}">
        {% if p.image %}<img class="product-image" src="{{ url_for('static', filename='uploads/' + p.image) }}" alt="{{ p.name }}">
        {% else %}<div class="placeholder">NEXVORA</div>{% endif %}</a>
        {% if p.featured %}<span class="pill">ИНТИХОБИ МО</span>{% endif %}
        <h3><a href="{{ url_for('product_detail', product_id=p.id) }}">{{ p.name }}</a></h3>
        <div class="muted">{{ p.category }} · Дар анбор: {{ p.stock }}</div>
        <p class="price">{{ p.price|money }} сомонӣ</p>
        <div class="actions">
          <a class="btn small" href="{{ url_for('product_detail', product_id=p.id) }}">Дидан</a>
          {% if p.stock > 0 %}
          <form method="post" action="{{ url_for('cart_add', product_id=p.id) }}">
            <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
            <button class="small">Ба сабад</button>
          </form>
          {% else %}<span class="pill">Тамом шуд</span>{% endif %}
        </div>
      </article>
    {% else %}<div class="empty">Маҳсулот ёфт нашуд. Ҷустуҷӯро дигар кунед.</div>{% endfor %}
    </div>
    """
    return page(
        "Мағоза", body, products=products, q=q, sort=sort,
        selected_category=category
    )


@app.route("/product/<int:product_id>")
def product_detail(product_id):
    with db_connect() as db:
        product = db.execute(
            "SELECT * FROM products WHERE id=? AND active=1", (product_id,)
        ).fetchone()
    if not product:
        abort(404)
    body = """
    <div class="two">
      <div class="panel">
        {% if product.image %}<img class="product-image" style="height:360px" src="{{ url_for('static', filename='uploads/' + product.image) }}" alt="{{ product.name }}">
        {% else %}<div class="placeholder" style="height:360px">NEXVORA</div>{% endif %}
      </div>
      <div class="panel">
        <span class="pill">{{ product.category }}</span>
        <h1>{{ product.name }}</h1>
        <p class="price">{{ product.price|money }} сомонӣ</p>
        <p>{{ product.description or "Тавсифи маҳсулот ҳоло илова нашудааст." }}</p>
        <p class="muted">Дар анбор: {{ product.stock }}</p>
        {% if product.stock > 0 %}
        <form method="post" action="{{ url_for('cart_add', product_id=product.id) }}">
          <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
          <label>Миқдор</label><input type="number" name="quantity" min="1" max="{{ product.stock }}" value="1">
          <button>Ба сабад илова кардан</button>
        </form>
        {% else %}<span class="pill">Ҳоло дастрас нест</span>{% endif %}
        <p><a href="/">← Бозгашт ба мағоза</a></p>
      </div>
    </div>
    """
    return page(product["name"], body, product=product)


@app.post("/cart/add/<int:product_id>")
def cart_add(product_id):
    check_csrf()
    qty = request.form.get("quantity", "1")
    try:
        qty = max(1, min(int(qty), 99))
    except ValueError:
        qty = 1
    with db_connect() as db:
        product = db.execute(
            "SELECT id, stock FROM products WHERE id=? AND active=1", (product_id,)
        ).fetchone()
    if not product or product["stock"] < 1:
        flash("Ин маҳсулот ҳоло дастрас нест.")
        return redirect(url_for("home"))
    cart = get_cart()
    current = int(cart.get(str(product_id), 0))
    cart[str(product_id)] = min(current + qty, int(product["stock"]), 99)
    session["cart"] = cart
    flash("Маҳсулот ба сабад илова шуд.")
    return redirect(request.referrer or url_for("cart"))


@app.route("/cart")
def cart():
    items, total = cart_details()
    body = """
    <h1>Сабади харид</h1>
    {% if items %}
    <div class="panel tablewrap"><table>
      <tr><th>Маҳсулот</th><th>Нарх</th><th>Миқдор</th><th>Ҷамъ</th><th></th></tr>
      {% for item in items %}<tr>
        <td>{{ item.product.name }}</td>
        <td>{{ item.product.price|money }}</td>
        <td>
          <form method="post" action="{{ url_for('cart_update', product_id=item.product.id) }}">
            <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
            <input style="width:75px" type="number" min="1" max="{{ item.product.stock }}" name="quantity" value="{{ item.quantity }}">
            <button class="small">Навсозӣ</button>
          </form>
        </td>
        <td>{{ item.line_total|money }}</td>
        <td><form method="post" action="{{ url_for('cart_remove', product_id=item.product.id) }}">
          <input type="hidden" name="_csrf" value="{{ csrf_token() }}"><button class="danger small">Хориҷ</button>
        </form></td>
      </tr>{% endfor %}
    </table></div>
    <div class="panel"><h2>Ҳамагӣ: {{ total|money }} сомонӣ</h2>
      <a class="btn" href="/checkout">Идома ба фармоиш</a>
      <a class="btn secondary" href="/">Харидро давом додан</a>
    </div>
    {% else %}<div class="empty"><h2>Сабад холӣ аст</h2><a class="btn" href="/">Ба мағоза</a></div>{% endif %}
    """
    return page("Сабади харид", body, items=items, total=total)


@app.post("/cart/update/<int:product_id>")
def cart_update(product_id):
    check_csrf()
    cart = get_cart()
    if str(product_id) in cart:
        try:
            qty = int(request.form.get("quantity", "1"))
        except ValueError:
            qty = 1
        with db_connect() as db:
            p = db.execute("SELECT stock FROM products WHERE id=?", (product_id,)).fetchone()
        if not p or p["stock"] < 1:
            cart.pop(str(product_id), None)
        else:
            cart[str(product_id)] = max(1, min(qty, p["stock"], 99))
    session["cart"] = cart
    return redirect(url_for("cart"))


@app.post("/cart/remove/<int:product_id>")
def cart_remove(product_id):
    check_csrf()
    cart = get_cart()
    cart.pop(str(product_id), None)
    session["cart"] = cart
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    items, total = cart_details()
    if not items:
        flash("Аввал ба сабад маҳсулот илова кунед.")
        return redirect(url_for("cart"))

    if request.method == "POST":
        check_csrf()
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        note = request.form.get("note", "").strip()
        if not name or not phone or not address:
            flash("Ном, телефон ва суроғаро пур кунед.")
        else:
            with db_connect() as db:
                # Боз санҷидани захира пеш аз сабти фармоиш
                fresh_items = []
                fresh_total = 0.0
                for item in items:
                    pid = item["product"]["id"]
                    row = db.execute(
                        "SELECT * FROM products WHERE id=? AND active=1", (pid,)
                    ).fetchone()
                    qty = item["quantity"]
                    if not row or row["stock"] < qty:
                        flash(f"Миқдори {item['product']['name']} дигар дастрас нест.")
                        return redirect(url_for("cart"))
                    fresh_items.append((row, qty))
                    fresh_total += float(row["price"]) * qty

                cur = db.execute(
                    """INSERT INTO orders(customer_name,phone,address,note,total,status)
                       VALUES(?,?,?,?,?,'Нав')""",
                    (name, phone, address, note, fresh_total)
                )
                order_id = cur.lastrowid
                for row, qty in fresh_items:
                    db.execute(
                        """INSERT INTO order_items(order_id,product_id,product_name,price,quantity)
                           VALUES(?,?,?,?,?)""",
                        (order_id, row["id"], row["name"], row["price"], qty)
                    )
                    db.execute(
                        "UPDATE products SET stock=stock-? WHERE id=?",
                        (qty, row["id"])
                    )
            session["cart"] = {}
            session["last_order_id"] = order_id
            return redirect(url_for("order_success", order_id=order_id))

    body = """
    <h1>Расмӣ кардани фармоиш</h1>
    <div class="two">
      <form class="panel" method="post">
        <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
        <label>Ному насаб</label><input name="name" required>
        <label>Рақами телефон</label><input name="phone" type="tel" required placeholder="+992...">
        <label>Суроғаи расонидан</label><textarea name="address" rows="2" required></textarea>
        <label>Шарҳ (ихтиёрӣ)</label><textarea name="note" rows="3"></textarea>
        <p class="muted">Пардохт ва расониданро бо фурӯшанда мувофиқа кунед.</p>
        <button>Фиристодани фармоиш</button>
      </form>
      <div class="panel"><h2>Хулосаи фармоиш</h2>
        {% for item in items %}<p>{{ item.product.name }} × {{ item.quantity }} — {{ item.line_total|money }} сомонӣ</p>{% endfor %}
        <hr><h2>Ҳамагӣ: {{ total|money }} сомонӣ</h2>
      </div>
    </div>
    """
    return page("Фармоиш", body, items=items, total=total)


@app.route("/order/success/<int:order_id>")
def order_success(order_id):
    if session.get("last_order_id") != order_id:
        abort(404)
    with db_connect() as db:
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        lines = db.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    if not order:
        abort(404)
    msg = [
        f"Салом! Фармоиши NEXVORA №{order_id}",
        f"Ном: {order['customer_name']}",
        f"Телефон: {order['phone']}",
        f"Суроға: {order['address']}",
        "Маҳсулот:"
    ]
    msg += [f"- {x['product_name']} x{x['quantity']} = {money(x['price']*x['quantity'])} сомонӣ" for x in lines]
    msg.append(f"Ҳамагӣ: {money(order['total'])} сомонӣ")
    if order["note"]:
        msg.append(f"Шарҳ: {order['note']}")
    whatsapp_url = f"https://wa.me/{WHATSAPP_NUMBER}?text={request.args.get('text', '')}"
    from urllib.parse import quote
    whatsapp_url = "https://wa.me/" + re.sub(r"\\D", "", WHATSAPP_NUMBER) + "?text=" + quote("\\n".join(msg))
    body = """
    <div class="panel"><div class="eyebrow">ORDER RECEIVED</div>
      <h1>Ташаккур! Фармоиш қабул шуд.</h1>
      <p>Рақами фармоиш: <strong>#{{ order.id }}</strong></p>
      <p>Ҳамагӣ: <strong class="price">{{ order.total|money }} сомонӣ</strong></p>
      <p class="muted">Барои мувофиқа кардани расонидан, маълумоти фармоишро ба WhatsApp фиристед.</p>
      <a class="btn" target="_blank" rel="noopener" href="{{ whatsapp_url }}">Фиристодан ба WhatsApp</a>
      <a class="btn secondary" href="/">Бозгашт ба мағоза</a>
    </div>
    """
    return page("Фармоиш қабул шуд", body, order=order, whatsapp_url=whatsapp_url)


# -------------------------- ADMIN --------------------------

LOGIN_BODY = """
<div class="panel" style="max-width:480px;margin:35px auto">
  <div class="eyebrow">PRIVATE ACCESS</div><h1>Воридшавии Admin</h1>
  <form method="post">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <label>Пароли админ</label><input type="password" name="password" required>
    <button>Дохил шудан</button>
  </form>
  <p class="muted">Паролро бо дигарон мубодила накун.</p>
</div>
"""


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        check_csrf()
        supplied = request.form.get("password", "")
        if ADMIN_PASSWORD.startswith("CHANGE_") or SECRET_KEY.startswith("CHANGE_"):
            flash("Аввал ADMIN_PASSWORD ва SECRET_KEY-ро дар аввали файл иваз кунед.")
        elif hmac.compare_digest(supplied, ADMIN_PASSWORD):
            session.clear()
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        else:
            flash("Парол нодуруст аст.")
    return page("Admin Login", LOGIN_BODY)


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    with db_connect() as db:
        products = db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        orders = db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 10").fetchall()
        stats = {
            "products": db.execute("SELECT COUNT(*) FROM products").fetchone()[0],
            "orders": db.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
            "sales": db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='Бекор'").fetchone()[0]
        }
    body = """
    <div class="eyebrow">CONTROL PANEL</div><h1>Идоракунии NEXVORA</h1>
    <div class="grid">
      <div class="panel"><div class="muted">Маҳсулот</div><h2>{{ stats.products }}</h2></div>
      <div class="panel"><div class="muted">Фармоишҳо</div><h2>{{ stats.orders }}</h2></div>
      <div class="panel"><div class="muted">Ҷамъи фармоишҳо</div><h2>{{ stats.sales|money }} сомонӣ</h2></div>
      <div class="panel"><div class="muted">Идоракунӣ</div><p><a href="/admin/product/new">+ Маҳсулоти нав</a></p><p><a href="/admin/orders">Дидани фармоишҳо</a></p><p><a href="/admin/products">Ҳамаи маҳсулот</a></p><p><a href="/admin/logout">Баромадан</a></p></div>
    </div>
    <h2>Фармоишҳои охирин</h2><div class="panel tablewrap"><table>
      <tr><th>№</th><th>Мизоҷ</th><th>Телефон</th><th>Ҷамъ</th><th>Ҳолат</th></tr>
      {% for o in orders %}<tr><td><a href="/admin/orders/{{ o.id }}">#{{ o.id }}</a></td><td>{{ o.customer_name }}</td><td>{{ o.phone }}</td><td>{{ o.total|money }}</td><td>{{ o.status }}</td></tr>{% else %}<tr><td>Ҳоло фармоиш нест.</td></tr>{% endfor %}
    </table></div>
    """
    return page("Admin", body, stats=stats, orders=orders)


PRODUCT_FORM = """
<div class="panel">
<h1>{{ heading }}</h1>
<form method="post" enctype="multipart/form-data">
<input type="hidden" name="_csrf" value="{{ csrf_token() }}">
<label>Номи маҳсулот</label><input name="name" required value="{{ p.name if p else '' }}">
<label>Нарх (сомонӣ)</label><input name="price" type="number" step="0.01" min="0" required value="{{ p.price if p else '' }}">
<label>Категория</label><input name="category" required placeholder="Телефон, Либос, Аксессуар..." value="{{ p.category if p else '' }}">
<label>Тавсиф</label><textarea name="description" rows="4">{{ p.description if p else '' }}</textarea>
<label>Миқдор дар анбор</label><input name="stock" type="number" min="0" max="100000" value="{{ p.stock if p else 1 }}">
<label>Сурати маҳсулот</label><input type="file" name="image" accept=".jpg,.jpeg,.png,.webp">
{% if p and p.image %}<p class="muted">Сурати ҳозира: {{ p.image }}</p>{% endif %}
<label><input style="width:auto" type="checkbox" name="featured" value="1" {% if p and p.featured %}checked{% endif %}> Дар саҳифаи интихобшудаҳо нишон дода шавад</label><br>
<button>Захира кардан</button> <a class="btn secondary" href="/admin/products">Бекор</a>
</form></div>
"""


@app.route("/admin/product/new", methods=["GET", "POST"])
@admin_required
def product_new():
    if request.method == "POST":
        check_csrf()
        try:
            name = request.form.get("name", "").strip()
            category = request.form.get("category", "").strip()
            description = request.form.get("description", "").strip()
            price = Decimal(request.form.get("price", "0"))
            stock = int(request.form.get("stock", "0"))
            if not name or not category or price < 0 or stock < 0:
                raise ValueError("Маълумотро дуруст пур кунед.")
            image = save_image(request.files.get("image"))
            with db_connect() as db:
                db.execute(
                    """INSERT INTO products(name,price,category,description,image,stock,featured)
                       VALUES(?,?,?,?,?,?,?)""",
                    (name, float(price), category, description, image, stock,
                     1 if request.form.get("featured") else 0)
                )
            flash("Маҳсулот илова шуд.")
            return redirect(url_for("admin_dashboard"))
        except (ValueError, InvalidOperation) as e:
            flash(str(e) or "Нарх ё миқдор нодуруст аст.")
    return page("Маҳсулоти нав", PRODUCT_FORM, heading="Илова кардани маҳсулот", p=None)


@app.route("/admin/products")
@admin_required
def admin_products():
    with db_connect() as db:
        products = db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    body = """
    <h1>Ҳамаи маҳсулот</h1><p><a class="btn" href="/admin/product/new">+ Илова кардани маҳсулот</a></p>
    <div class="panel tablewrap"><table>
    <tr><th>Ном</th><th>Категория</th><th>Нарх</th><th>Захира</th><th>Амалиёт</th></tr>
    {% for p in products %}<tr>
      <td>{{ p.name }}</td><td>{{ p.category }}</td><td>{{ p.price|money }}</td><td>{{ p.stock }}</td>
      <td><a href="/admin/product/{{ p.id }}/edit">Таҳрир</a>
      <form style="display:inline" method="post" action="/admin/product/{{ p.id }}/delete" onsubmit="return confirm('Маҳсулот нест шавад?')">
        <input type="hidden" name="_csrf" value="{{ csrf_token() }}"><button class="danger small">Нест кардан</button>
      </form></td>
    </tr>{% else %}<tr><td>Маҳсулот нест.</td></tr>{% endfor %}
    </table></div>
    """
    return page("Маҳсулот", body, products=products)


@app.route("/admin/product/<int:product_id>/edit", methods=["GET", "POST"])
@admin_required
def product_edit(product_id):
    with db_connect() as db:
        p = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not p:
        abort(404)
    if request.method == "POST":
        check_csrf()
        try:
            name = request.form.get("name", "").strip()
            category = request.form.get("category", "").strip()
            description = request.form.get("description", "").strip()
            price = Decimal(request.form.get("price", "0"))
            stock = int(request.form.get("stock", "0"))
            if not name or not category or price < 0 or stock < 0:
                raise ValueError("Маълумотро дуруст пур кунед.")
            new_image = save_image(request.files.get("image"))
            image = new_image or p["image"]
            with db_connect() as db:
                db.execute(
                    """UPDATE products SET name=?,price=?,category=?,description=?,
                       image=?,stock=?,featured=? WHERE id=?""",
                    (name, float(price), category, description, image, stock,
                     1 if request.form.get("featured") else 0, product_id)
                )
            if new_image and p["image"]:
                old = UPLOAD_DIR / p["image"]
                if old.exists():
                    old.unlink()
            flash("Маҳсулот нав карда шуд.")
            return redirect(url_for("admin_products"))
        except (ValueError, InvalidOperation) as e:
            flash(str(e) or "Маълумот нодуруст аст.")
    return page("Таҳрири маҳсулот", PRODUCT_FORM, heading="Таҳрири маҳсулот", p=p)


@app.post("/admin/product/<int:product_id>/delete")
@admin_required
def product_delete(product_id):
    check_csrf()
    with db_connect() as db:
        p = db.execute("SELECT image FROM products WHERE id=?", (product_id,)).fetchone()
        db.execute("DELETE FROM products WHERE id=?", (product_id,))
    if p and p["image"]:
        old = UPLOAD_DIR / p["image"]
        if old.exists():
            old.unlink()
    flash("Маҳсулот нест карда шуд.")
    return redirect(url_for("admin_products"))


@app.route("/admin/orders")
@admin_required
def admin_orders():
    with db_connect() as db:
        orders = db.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    body = """
    <h1>Фармоишҳо</h1><div class="panel tablewrap"><table>
    <tr><th>№</th><th>Мизоҷ</th><th>Телефон</th><th>Суроға</th><th>Ҷамъ</th><th>Ҳолат</th></tr>
    {% for o in orders %}<tr>
      <td><a href="/admin/orders/{{ o.id }}">#{{ o.id }}</a></td><td>{{ o.customer_name }}</td>
      <td>{{ o.phone }}</td><td>{{ o.address }}</td><td>{{ o.total|money }}</td><td>{{ o.status }}</td>
    </tr>{% else %}<tr><td>Ҳоло фармоиш нест.</td></tr>{% endfor %}
    </table></div>
    """
    return page("Фармоишҳо", body, orders=orders)


@app.route("/admin/orders/<int:order_id>", methods=["GET", "POST"])
@admin_required
def admin_order_detail(order_id):
    if request.method == "POST":
        check_csrf()
        status = request.form.get("status", "")
        if status not in {"Нав", "Тасдиқ шуд", "Равон шуд", "Анҷом ёфт", "Бекор"}:
            abort(400)
        with db_connect() as db:
            db.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        flash("Ҳолати фармоиш нав карда шуд.")
        return redirect(url_for("admin_order_detail", order_id=order_id))
    with db_connect() as db:
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        lines = db.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    if not order:
        abort(404)
    body = """
    <h1>Фармоиши #{{ order.id }}</h1><div class="two">
    <div class="panel"><h2>Маълумоти мизоҷ</h2>
      <p>{{ order.customer_name }}</p><p>{{ order.phone }}</p><p>{{ order.address }}</p><p>{{ order.note }}</p>
      <p class="price">{{ order.total|money }} сомонӣ</p>
      <form method="post"><input type="hidden" name="_csrf" value="{{ csrf_token() }}">
        <label>Ҳолати фармоиш</label><select name="status">
        {% for s in statuses %}<option {% if s==order.status %}selected{% endif %}>{{ s }}</option>{% endfor %}
        </select><button>Нав кардани ҳолат</button>
      </form>
    </div><div class="panel"><h2>Маҳсулот</h2>
      {% for x in lines %}<p>{{ x.product_name }} × {{ x.quantity }} — {{ (x.price*x.quantity)|money }} сомонӣ</p>{% endfor %}
    </div></div><a href="/admin/orders">← Бозгашт ба фармоишҳо</a>
    """
    return page("Фармоиш", body, order=order, lines=lines,
                statuses=["Нав", "Тасдиқ шуд", "Равон шуд", "Анҷом ёфт", "Бекор"])


@app.route("/admin/logout")
@admin_required
def admin_logout():
    session.clear()
    flash("Шумо аз Admin баромадед.")
    return redirect(url_for("home"))


@app.errorhandler(400)
def bad_request(error):
    return page("Хато", "<div class='panel'><h1>Дархост нодуруст аст</h1><p>Саҳифаро нав кунед ва дубора кӯшиш кунед.</p><a href='/'>Ба саҳифаи асосӣ</a></div>"), 400


@app.errorhandler(404)
def not_found(error):
    return page("Ёфт нашуд", "<div class='panel'><h1>Саҳифа ёфт нашуд</h1><a href='/'>Ба саҳифаи асосӣ</a></div>"), 404


init_db()

if __name__ == "__main__":
    # Барои санҷиш дар худи телефон. Барои интернети умумӣ hosting лозим аст.
    app.run(host="127.0.0.1", port=5000, debug=False)
