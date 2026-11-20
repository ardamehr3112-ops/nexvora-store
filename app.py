"""
NEXVORA STORE PREMIUM — Flask store with video advertising.
Install: pip install flask
Run: python app.py -> http://127.0.0.1:5000
Change SECRET_KEY, ADMIN_PASSWORD, WHATSAPP_NUMBER before use.
"""
from flask import Flask, request, redirect, url_for, session, render_template_string, flash, abort
from werkzeug.utils import secure_filename
from pathlib import Path
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import quote
import sqlite3, os, re, secrets, hmac, uuid

SECRET_KEY = os.environ.get("SECRET_KEY", "CHANGE_THIS_TO_A_RANDOM_SECRET")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Jurabekjon123.")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "992111301210")
STORE_NAME = "NEXVORA"
BASE = Path(__file__).resolve().parent
DB = BASE / "nexvora_store_v3.db"
UPLOADS = BASE / "static" / "uploads"
VIDEOS = BASE / "static" / "videos"
UPLOADS.mkdir(parents=True, exist_ok=True)
VIDEOS.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(MAX_CONTENT_LENGTH=45*1024*1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

IMG_EXT = {"jpg", "jpeg", "png", "webp"}
VIDEO_EXT = {"mp4", "webm", "mov"}

def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, price REAL NOT NULL,
            category TEXT NOT NULL, description TEXT DEFAULT '', image TEXT DEFAULT '',
            stock INTEGER NOT NULL DEFAULT 0, featured INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT NOT NULL,
            phone TEXT NOT NULL, address TEXT NOT NULL, note TEXT DEFAULT '',
            total REAL NOT NULL, status TEXT DEFAULT 'Нав',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS order_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL,
            product_id INTEGER, product_name TEXT NOT NULL, price REAL NOT NULL,
            quantity INTEGER NOT NULL, FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
        """)
        defaults = {
            "promo_enabled":"0", "promo_video":"", "promo_title":"NEXVORA — DISCOVER YOUR STYLE",
            "promo_subtitle":"Интихоби нав. Сабки нав.", "promo_button":"Дидани маҳсулот",
            "promo_button_link":"#products", "promo_badge":"PREMIUM COLLECTION",
            "promo_discount":""
        }
        for k,v in defaults.items():
            db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))

def settings():
    with connect() as db:
        return {r["key"]:r["value"] for r in db.execute("SELECT * FROM settings")}
def set_settings(values):
    with connect() as db:
        for k,v in values.items():
            db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(k,str(v)))
def csrf():
    if "_csrf" not in session: session["_csrf"]=secrets.token_hex(24)
    return session["_csrf"]
app.jinja_env.globals["csrf_token"]=csrf
def check_csrf():
    if not hmac.compare_digest(request.form.get("_csrf",""),session.get("_csrf","")): abort(400)
def admin_required(fn):
    @wraps(fn)
    def w(*a,**kw):
        if not session.get("admin"): return redirect(url_for("admin_login"))
        return fn(*a,**kw)
    return w
def ext_ok(filename, allowed):
    return "." in filename and filename.rsplit(".",1)[-1].lower() in allowed
def upload(file, folder, allowed):
    if not file or not file.filename: return ""
    if not ext_ok(file.filename,allowed): raise ValueError("Формати файл иҷозат нест.")
    ext=secure_filename(file.filename).rsplit(".",1)[-1].lower()
    name=uuid.uuid4().hex+"."+ext
    file.save(folder/name)
    return name
def money(v):
    return f"{float(v):,.2f}".rstrip("0").rstrip(".")
app.jinja_env.filters["money"]=money
def cart_data():
    cart=session.get("cart",{})
    if not isinstance(cart,dict): return [],0
    ids=[int(k) for k in cart if str(k).isdigit()]
    if not ids: return [],0
    with connect() as db:
        rows=db.execute("SELECT * FROM products WHERE active=1 AND id IN (%s)" % ",".join("?"*len(ids)),ids).fetchall()
    items=[]; total=0
    for p in rows:
        q=max(1,min(int(cart.get(str(p["id"]),1)),int(p["stock"] or 1),99))
        line=float(p["price"])*q; items.append({"p":p,"q":q,"line":line}); total+=line
    return items,total
def cats():
    with connect() as db: return [r[0] for r in db.execute("SELECT DISTINCT category FROM products WHERE active=1 ORDER BY category")]

CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Playfair+Display:wght@600;700&display=swap');
:root{--bg:#08090b;--panel:#121419;--panel2:#191c22;--gold:#d9b66c;--gold2:#f3dca4;--text:#f4f2ec;--muted:#a5a6ad;--line:#292d35;--green:#66c497}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Manrope,Arial,sans-serif;line-height:1.55}
a{color:var(--gold);text-decoration:none}a:hover{color:var(--gold2)}
.topline{height:3px;background:linear-gradient(90deg,#765626,var(--gold2),#765626)}
header{position:sticky;top:0;z-index:10;background:#0c0d10eF;backdrop-filter:blur(14px);border-bottom:1px solid #55452a}
.head{max-width:1200px;margin:auto;padding:14px 20px;display:flex;align-items:center;justify-content:space-between;gap:16px}
.brand{font-weight:800;letter-spacing:5px;font-size:24px;color:var(--gold2)}.brand small{display:block;font-size:9px;letter-spacing:3px;color:#a4a4aa;font-weight:500}
.nav{display:flex;align-items:center;gap:17px;flex-wrap:wrap;font-size:13px}.nav a{white-space:nowrap}
.container{max-width:1200px;margin:auto;padding:24px 18px}
.hero{position:relative;overflow:hidden;border:1px solid #534225;border-radius:24px;min-height:360px;margin:8px 0 28px;background:radial-gradient(circle at 80% 20%,#4b381d 0,#1b1916 30%,#101115 72%);display:flex;align-items:center}
.hero-content{position:relative;z-index:2;padding:42px;max-width:680px}.eyebrow{color:var(--gold);letter-spacing:3px;font-size:11px;font-weight:800}
.hero h1{font-family:'Playfair Display',serif;color:var(--gold2);font-size:clamp(30px,6vw,58px);line-height:1.08;margin:14px 0}
.hero p{color:#c7c3bb;max-width:590px}.hero-video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.42}
.hero:has(video) .hero-content{ text-shadow:0 2px 10px #000}
.hero:has(video)::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,#08090bf0,#08090b90 65%,#08090b40)}
.btn,button{display:inline-block;border:0;border-radius:10px;padding:11px 16px;background:linear-gradient(135deg,var(--gold),var(--gold2));color:#17130b;font-weight:800;font-size:13px;cursor:pointer;text-align:center}
.btn:hover,button:hover{filter:brightness(1.08);color:#111}.secondary{background:#2b3038!important;color:#eee!important;border:1px solid #4a505a}.danger{background:#803c42!important;color:#fff!important}.small{padding:8px 11px;font-size:12px}
.toolbar{display:grid;grid-template-columns:1fr 190px 150px auto;gap:9px;margin:20px 0}
input,textarea,select{width:100%;padding:12px;background:#1d2026;color:#fff;border:1px solid #383e48;border-radius:10px;margin:5px 0 12px;font:inherit}
input:focus,textarea:focus,select:focus{outline:1px solid var(--gold)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px}
.card{background:linear-gradient(180deg,#17191f,#111318);border:1px solid var(--line);border-radius:16px;padding:12px;min-width:0;transition:transform .22s,border .22s;animation:fade .5s ease both}
.card:hover{transform:translateY(-4px);border-color:#66532f}
.product-image,.placeholder{width:100%;height:220px;object-fit:cover;border-radius:11px;background:#252830;display:block}
.placeholder{display:grid;place-items:center;color:var(--gold);letter-spacing:4px}
.card h3{margin:10px 0 4px;font-size:16px}.price{color:var(--gold2);font-size:19px;font-weight:800}.muted{color:var(--muted);font-size:12px}
.pill{display:inline-block;border:1px solid #68532f;color:var(--gold);border-radius:30px;padding:4px 9px;font-size:10px;letter-spacing:.7px}
.actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}.panel{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;margin:15px 0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}.flash{padding:12px;border-radius:10px;background:#20372b;border:1px solid #3b6348;margin:10px 0}
table{border-collapse:collapse;width:100%;min-width:620px}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}th{color:var(--gold2)}.tablewrap{overflow-x:auto}
footer{text-align:center;color:#777b84;border-top:1px solid var(--line);padding:28px;margin-top:35px}
.empty{text-align:center;padding:40px;color:var(--muted)}label{font-size:13px;color:#ddd}hr{border:0;border-top:1px solid var(--line);margin:18px 0}
@keyframes fade{from{opacity:0;transform:translateY(9px)}to{opacity:1;transform:translateY(0)}}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.toolbar{grid-template-columns:1fr 1fr}.toolbar button{grid-column:span 2}}
@media(max-width:560px){.head{flex-direction:column;align-items:flex-start}.nav{gap:12px}.container{padding:15px 11px}.grid{gap:9px}.card{padding:9px}.product-image,.placeholder{height:145px}.card h3{font-size:14px}.price{font-size:16px}.hero{min-height:330px}.hero-content{padding:24px 18px}.two,.toolbar{grid-template-columns:1fr}.toolbar button{grid-column:auto}}
</style>
"""
def page(title, body, **ctx):
    s=settings()
    template="""<!doctype html><html lang="tg"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}} — NEXVORA</title>"""+CSS+"""</head><body><div class="topline"></div><header><div class="head"><a class="brand" href="/">NEXVORA<small>PREMIUM ONLINE STORE</small></a><nav class="nav"><a href="/">Мағоза</a><a href="/?featured=1">Интихобшудаҳо</a><a href="/cart">🛒 Сабад ({{count}})</a><a href="/admin">Admin</a></nav></div></header><main class="container">{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{m}}</div>{% endfor %}{% endwith %}"""+body+"""</main><footer>© {{year}} NEXVORA STORE · Discover your style</footer></body></html>"""
    from datetime import datetime
    cart=session.get("cart",{})
    ctx.update(title=title,count=sum(int(v) for v in cart.values()) if cart else 0,year=datetime.now().year,categories=cats(),promo=s)
    return render_template_string(template,**ctx)

@app.route("/")
def home():
    q=request.args.get("q","").strip(); category=request.args.get("category",""); sort=request.args.get("sort","new"); featured=request.args.get("featured")=="1"
    sql="SELECT * FROM products WHERE active=1"; args=[]
    if q: sql+=" AND (name LIKE ? OR description LIKE ? OR category LIKE ?)"; args += [f"%{q}%"]*3
    if category: sql+=" AND category=?"; args.append(category)
    if featured: sql+=" AND featured=1"
    sql+=" ORDER BY price ASC" if sort=="cheap" else (" ORDER BY price DESC" if sort=="expensive" else " ORDER BY id DESC")
    with connect() as db: products=db.execute(sql,args).fetchall()
    body="""{% if promo.promo_enabled=='1' %}<section class="hero">
    {% if promo.promo_video %}<video class="hero-video" autoplay muted loop playsinline controls><source src="{{url_for('static',filename='videos/'+promo.promo_video)}}"></video>{% endif %}
    <div class="hero-content"><div class="eyebrow">{{promo.promo_badge}}</div><h1>{{promo.promo_title}}</h1><p>{{promo.promo_subtitle}}</p>{% if promo.promo_discount %}<span class="pill">{{promo.promo_discount}}</span><br><br>{% endif %}<a class="btn" href="{{promo.promo_button_link}}">{{promo.promo_button}}</a></div></section>
    {% else %}<section class="hero"><div class="hero-content"><div class="eyebrow">WELCOME TO NEXVORA</div><h1>Интихоби нав.<br>Сабки нав.</h1><p>Маҳсулоти дӯстдоштаатро пайдо кун ва фармоишро осон фирист.</p><a class="btn" href="#products">Дидани маҳсулот</a></div></section>{% endif %}
    <form class="toolbar" method="get"><input name="q" value="{{q}}" placeholder="🔎 Ҷустуҷӯи маҳсулот"><select name="category"><option value="">Ҳама категорияҳо</option>{% for c in categories %}<option value="{{c}}" {% if c==selected %}selected{% endif %}>{{c}}</option>{% endfor %}</select><select name="sort"><option value="new" {% if sort=='new' %}selected{% endif %}>Навтарин</option><option value="cheap" {% if sort=='cheap' %}selected{% endif %}>Арзонтарин</option><option value="expensive" {% if sort=='expensive' %}selected{% endif %}>Қиматтарин</option></select><button>Ҷустуҷӯ</button></form>
    <div id="products"><div class="eyebrow">OUR COLLECTION</div><h2>Маҳсулот</h2></div><div class="grid">
    {% for p in products %}<article class="card"><a href="{{url_for('detail',pid=p.id)}}">{% if p.image %}<img class="product-image" src="{{url_for('static',filename='uploads/'+p.image)}}" alt="{{p.name}}">{% else %}<div class="placeholder">NEXVORA</div>{% endif %}</a>{% if p.featured %}<span class="pill">✦ ИНТИХОБИ МО</span>{% endif %}<h3><a href="{{url_for('detail',pid=p.id)}}">{{p.name}}</a></h3><div class="muted">{{p.category}} · Захира: {{p.stock}}</div><p class="price">{{p.price|money}} сомонӣ</p><div class="actions"><a class="btn small secondary" href="{{url_for('detail',pid=p.id)}}">Маълумот</a>{% if p.stock>0 %}<form method="post" action="{{url_for('add_cart',pid=p.id)}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="small">Ба сабад</button></form>{% else %}<span class="pill">Тамом</span>{% endif %}</div></article>{% else %}<div class="empty">Маҳсулот ёфт нашуд.</div>{% endfor %}</div>"""
    return page("Мағоза",body,products=products,q=q,sort=sort,selected=category)

@app.route("/product/<int:pid>")
def detail(pid):
    with connect() as db: p=db.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
    if not p: abort(404)
    body="""<div class="two"><div class="panel">{% if p.image %}<img class="product-image" style="height:350px" src="{{url_for('static',filename='uploads/'+p.image)}}">{% else %}<div class="placeholder" style="height:350px">NEXVORA</div>{% endif %}</div><div class="panel"><span class="pill">{{p.category}}</span><h1>{{p.name}}</h1><p class="price">{{p.price|money}} сомонӣ</p><p>{{p.description}}</p><p class="muted">Дар анбор: {{p.stock}}</p>{% if p.stock>0 %}<form method="post" action="{{url_for('add_cart',pid=p.id)}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Миқдор</label><input type="number" name="quantity" min="1" max="{{p.stock}}" value="1"><button>Ба сабад</button></form>{% endif %}<a href="/">← Ба мағоза</a></div></div>"""
    return page(p["name"],body,p=p)

@app.post("/cart/add/<int:pid>")
def add_cart(pid):
    check_csrf()
    try: q=max(1,min(int(request.form.get("quantity",1)),99))
    except: q=1
    with connect() as db: p=db.execute("SELECT id,stock FROM products WHERE id=? AND active=1",(pid,)).fetchone()
    if not p or p["stock"]<1: flash("Маҳсулот дастрас нест."); return redirect("/")
    c=session.get("cart",{}); c[str(pid)]=min(int(c.get(str(pid),0))+q,int(p["stock"]),99); session["cart"]=c
    flash("Ба сабад илова шуд."); return redirect(request.referrer or url_for("cart"))

@app.route("/cart")
def cart():
    items,total=cart_data()
    body="""<h1>Сабади харид</h1>{% if items %}<div class="panel tablewrap"><table><tr><th>Маҳсулот</th><th>Нарх</th><th>Миқдор</th><th>Ҷамъ</th><th></th></tr>{% for x in items %}<tr><td>{{x.p.name}}</td><td>{{x.p.price|money}}</td><td><form method="post" action="{{url_for('update_cart',pid=x.p.id)}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><input style="width:70px" name="quantity" type="number" min="1" max="{{x.p.stock}}" value="{{x.q}}"><button class="small">Нав</button></form></td><td>{{x.line|money}}</td><td><form method="post" action="{{url_for('remove_cart',pid=x.p.id)}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="danger small">Хориҷ</button></form></td></tr>{% endfor %}</table></div><div class="panel"><h2>Ҳамагӣ: {{total|money}} сомонӣ</h2><a class="btn" href="/checkout">Фармоиш</a></div>{% else %}<div class="empty">Сабад холӣ аст <a class="btn" href="/">Ба мағоза</a></div>{% endif %}"""
    return page("Сабад",body,items=items,total=total)
@app.post("/cart/update/<int:pid>")
def update_cart(pid):
    check_csrf(); c=session.get("cart",{})
    if str(pid) in c:
        try: q=int(request.form.get("quantity",1))
        except: q=1
        with connect() as db: p=db.execute("SELECT stock FROM products WHERE id=?",(pid,)).fetchone()
        if not p or p["stock"]<1: c.pop(str(pid),None)
        else: c[str(pid)]=max(1,min(q,int(p["stock"]),99))
    session["cart"]=c; return redirect("/cart")
@app.post("/cart/remove/<int:pid>")
def remove_cart(pid):
    check_csrf(); c=session.get("cart",{}); c.pop(str(pid),None); session["cart"]=c; return redirect("/cart")

@app.route("/checkout",methods=["GET","POST"])
def checkout():
    items,total=cart_data()
    if not items: flash("Сабад холӣ аст."); return redirect("/cart")
    if request.method=="POST":
        check_csrf(); name=request.form.get("name","").strip(); phone=request.form.get("phone","").strip(); address=request.form.get("address","").strip(); note=request.form.get("note","").strip()
        if not all([name,phone,address]): flash("Ном, телефон ва суроға пур карда шавад.")
        else:
            with connect() as db:
                fresh=[]; total2=0
                for x in items:
                    p=db.execute("SELECT * FROM products WHERE id=? AND active=1",(x["p"]["id"],)).fetchone()
                    if not p or p["stock"]<x["q"]: flash("Захираи маҳсулот кам шудааст."); return redirect("/cart")
                    fresh.append((p,x["q"])); total2+=p["price"]*x["q"]
                oid=db.execute("INSERT INTO orders(customer_name,phone,address,note,total,status) VALUES(?,?,?,?,?,'Нав')",(name,phone,address,note,total2)).lastrowid
                for p,q in fresh:
                    db.execute("INSERT INTO order_items(order_id,product_id,product_name,price,quantity) VALUES(?,?,?,?,?)",(oid,p["id"],p["name"],p["price"],q))
                    db.execute("UPDATE products SET stock=stock-? WHERE id=?",(q,p["id"]))
            session["cart"]={}; session["last_order"]=oid; return redirect(url_for("success",oid=oid))
    body="""<h1>Расмӣ кардани фармоиш</h1><div class="two"><form class="panel" method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Ному насаб</label><input name="name" required><label>Телефон</label><input name="phone" type="tel" required><label>Суроға</label><textarea name="address" required></textarea><label>Шарҳ (ихтиёрӣ)</label><textarea name="note"></textarea><button>Сабти фармоиш</button></form><div class="panel"><h2>Хулоса</h2>{% for x in items %}<p>{{x.p.name}} × {{x.q}} — {{x.line|money}}</p>{% endfor %}<hr><h2>{{total|money}} сомонӣ</h2></div></div>"""
    return page("Checkout",body,items=items,total=total)
@app.route("/order/success/<int:oid>")
def success(oid):
    if session.get("last_order")!=oid: abort(404)
    with connect() as db: o=db.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone(); lines=db.execute("SELECT * FROM order_items WHERE order_id=?",(oid,)).fetchall()
    if not o: abort(404)
    msg=[f"NEXVORA order #{oid}",f"Ном: {o['customer_name']}",f"Телефон: {o['phone']}",f"Суроға: {o['address']}","Маҳсулот:"]+[f"{x['product_name']} x{x['quantity']} — {money(x['price']*x['quantity'])}" for x in lines]+[f"Ҳамагӣ: {money(o['total'])}"]
    if o["note"]: msg.append("Шарҳ: "+o["note"])
    wa="https://wa.me/"+re.sub(r"\D","",WHATSAPP_NUMBER)+"?text="+quote("\n".join(msg))
    body="""<div class="panel"><h1>Фармоиш қабул шуд! ✓</h1><p>№ {{o.id}} · {{o.total|money}} сомонӣ</p><p>Барои мувофиқаи фармоиш WhatsApp-ро кушо.</p><a class="btn" href="{{wa}}" target="_blank">Фиристодан ба WhatsApp</a><a class="btn secondary" href="/">Ба мағоза</a></div>"""
    return page("Фармоиш",body,o=o,wa=wa)

# ADMIN
LOGIN="""<div class="panel" style="max-width:450px;margin:auto"><h1>Admin Login</h1><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Парол</label><input type="password" name="password" required><button>Дохил шудан</button></form></div>"""
@app.route("/admin",methods=["GET","POST"])
def admin_login():
    if session.get("admin"): return redirect("/admin/dashboard")
    if request.method=="POST":
        check_csrf()
        if ADMIN_PASSWORD.startswith("CHANGE") or SECRET_KEY.startswith("CHANGE"): flash("Аввал пароли админ ва SECRET_KEY-ро иваз кунед.")
        elif hmac.compare_digest(request.form.get("password",""),ADMIN_PASSWORD): session.clear(); session["admin"]=True; return redirect("/admin/dashboard")
        else: flash("Парол нодуруст.")
    return page("Admin",LOGIN)
@app.route("/admin/dashboard")
@admin_required
def dashboard():
    with connect() as db:
        stats=[db.execute("SELECT COUNT(*) FROM products").fetchone()[0],db.execute("SELECT COUNT(*) FROM orders").fetchone()[0],db.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='Бекор'").fetchone()[0]]
        orders=db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 8").fetchall()
    body="""<h1>Панели идоракунӣ</h1><div class="grid"><div class="panel"><small>Маҳсулот</small><h2>{{stats[0]}}</h2></div><div class="panel"><small>Фармоишҳо</small><h2>{{stats[1]}}</h2></div><div class="panel"><small>Ҷамъ</small><h2>{{stats[2]|money}} сомонӣ</h2></div><div class="panel"><a href="/admin/product/new">+ Маҳсулоти нав</a><p><a href="/admin/products">Маҳсулот</a></p><p><a href="/admin/orders">Фармоишҳо</a></p><p><a href="/admin/promo">🎬 Видео-реклама</a></p><p><a href="/admin/logout">Баромадан</a></p></div></div><h2>Фармоишҳои охир</h2><div class="panel tablewrap"><table><tr><th>№</th><th>Мизоҷ</th><th>Телефон</th><th>Ҷамъ</th><th>Ҳолат</th></tr>{% for o in orders %}<tr><td><a href="/admin/orders/{{o.id}}">#{{o.id}}</a></td><td>{{o.customer_name}}</td><td>{{o.phone}}</td><td>{{o.total|money}}</td><td>{{o.status}}</td></tr>{% endfor %}</table></div>"""
    return page("Dashboard",body,stats=stats,orders=orders)

@app.route("/admin/promo",methods=["GET","POST"])
@admin_required
def promo():
    s=settings()
    if request.method=="POST":
        check_csrf()
        try:
            video=upload(request.files.get("video"),VIDEOS,VIDEO_EXT)
            if video and s.get("promo_video"):
                old=VIDEOS/s["promo_video"]
                if old.exists(): old.unlink()
            vals={k:request.form.get(k,"") for k in ["promo_enabled","promo_title","promo_subtitle","promo_button","promo_button_link","promo_badge","promo_discount"]}
            vals["promo_enabled"]="1" if request.form.get("promo_enabled") else "0"
            vals["promo_video"]=video or s.get("promo_video","")
            if request.form.get("remove_video"):
                old=s.get("promo_video","")
                if old and (VIDEOS/old).exists(): (VIDEOS/old).unlink()
                vals["promo_video"]=""
            set_settings(vals); flash("Реклама захира шуд."); return redirect("/admin/promo")
        except ValueError as e: flash(str(e))
    body="""<h1>🎬 Видео-реклама</h1><p class="muted">Видео MP4 ё WEBM, то 40 MB. Барои интернет видеоҳои кӯтоҳ беҳтаранд.</p><div class="panel">{% if s.promo_video %}<video style="width:100%;max-height:320px;border-radius:12px" controls><source src="{{url_for('static',filename='videos/'+s.promo_video)}}"></video><p>Видеои ҷорӣ: {{s.promo_video}}</p>{% endif %}<form method="post" enctype="multipart/form-data"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label><input type="checkbox" style="width:auto" name="promo_enabled" {% if s.promo_enabled=='1' %}checked{% endif %}> Реклама дар саҳифаи асосӣ фаъол бошад</label><label>Видеои нав</label><input type="file" name="video" accept="video/mp4,video/webm,video/quicktime"><label>Сарлавҳа</label><input name="promo_title" value="{{s.promo_title}}"><label>Матни зер</label><textarea name="promo_subtitle">{{s.promo_subtitle}}</textarea><label>Номи тугма</label><input name="promo_button" value="{{s.promo_button}}"><label>Суроғаи тугма (масалан #products)</label><input name="promo_button_link" value="{{s.promo_button_link}}"><label>Матни хурд / badge</label><input name="promo_badge" value="{{s.promo_badge}}"><label>Тахфиф ё эълон</label><input name="promo_discount" value="{{s.promo_discount}}"><label><input type="checkbox" style="width:auto" name="remove_video"> Видеои ҳозираро нест кардан</label><button>Захира кардан</button></form></div><a href="/admin/dashboard">← Панел</a>"""
    return page("Видео-реклама",body,s=s)

FORM="""<div class="panel"><h1>{{heading}}</h1><form method="post" enctype="multipart/form-data"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Ном</label><input name="name" required value="{{p.name if p else ''}}"><label>Нарх (сомонӣ)</label><input name="price" type="number" min="0" step="0.01" required value="{{p.price if p else ''}}"><label>Категория</label><input name="category" required value="{{p.category if p else ''}}"><label>Тавсиф</label><textarea name="description">{{p.description if p else ''}}</textarea><label>Захира</label><input name="stock" type="number" min="0" value="{{p.stock if p else 1}}"><label>Акс</label><input type="file" name="image" accept=".jpg,.jpeg,.png,.webp">{% if p and p.image %}<p>Акс: {{p.image}}</p>{% endif %}<label><input type="checkbox" style="width:auto" name="featured" {% if p and p.featured %}checked{% endif %}> Интихобшуда</label><button>Захира</button></form></div>"""
@app.route("/admin/product/new",methods=["GET","POST"])
@admin_required
def product_new():
    if request.method=="POST":
        check_csrf()
        try:
            name=request.form.get("name","").strip(); category=request.form.get("category","").strip(); price=Decimal(request.form.get("price","0")); stock=int(request.form.get("stock","0")); desc=request.form.get("description","")
            if not name or not category or price<0 or stock<0: raise ValueError("Маълумот нодуруст.")
            image=upload(request.files.get("image"),UPLOADS,IMG_EXT)
            with connect() as db: db.execute("INSERT INTO products(name,price,category,description,image,stock,featured) VALUES(?,?,?,?,?,?,?)",(name,float(price),category,desc,image,stock,int(bool(request.form.get("featured")))))
            flash("Маҳсулот илова шуд."); return redirect("/admin/products")
        except (ValueError,InvalidOperation) as e: flash(str(e))
    return page("Маҳсулоти нав",FORM,heading="Маҳсулоти нав",p=None)
@app.route("/admin/products")
@admin_required
def products_admin():
    with connect() as db: ps=db.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    body="""<h1>Маҳсулот</h1><a class="btn" href="/admin/product/new">+ Маҳсулоти нав</a><div class="panel tablewrap"><table><tr><th>Ном</th><th>Категория</th><th>Нарх</th><th>Захира</th><th>Амалиёт</th></tr>{% for p in ps %}<tr><td>{{p.name}}</td><td>{{p.category}}</td><td>{{p.price|money}}</td><td>{{p.stock}}</td><td><a href="/admin/product/{{p.id}}/edit">Таҳрир</a><form method="post" action="/admin/product/{{p.id}}/delete"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="danger small">Нест</button></form></td></tr>{% endfor %}</table></div>"""
    return page("Маҳсулот",body,ps=ps)
@app.route("/admin/product/<int:pid>/edit",methods=["GET","POST"])
@admin_required
def edit_product(pid):
    with connect() as db: p=db.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
    if not p: abort(404)
    if request.method=="POST":
        check_csrf()
        try:
            name=request.form.get("name","").strip(); category=request.form.get("category","").strip(); price=Decimal(request.form.get("price","0")); stock=int(request.form.get("stock","0"))
            if not name or not category or price<0 or stock<0: raise ValueError("Маълумот нодуруст.")
            new=upload(request.files.get("image"),UPLOADS,IMG_EXT); image=new or p["image"]
            with connect() as db: db.execute("UPDATE products SET name=?,price=?,category=?,description=?,image=?,stock=?,featured=? WHERE id=?",(name,float(price),category,request.form.get("description",""),image,stock,int(bool(request.form.get("featured"))),pid))
            if new and p["image"] and (UPLOADS/p["image"]).exists(): (UPLOADS/p["image"]).unlink()
            flash("Тағйирот захира шуд."); return redirect("/admin/products")
        except (ValueError,InvalidOperation) as e: flash(str(e))
    return page("Таҳрир",FORM,heading="Таҳрири маҳсулот",p=p)
@app.post("/admin/product/<int:pid>/delete")
@admin_required
def delete_product(pid):
    check_csrf()
    with connect() as db: p=db.execute("SELECT image FROM products WHERE id=?",(pid,)).fetchone(); db.execute("DELETE FROM products WHERE id=?",(pid,))
    if p and p["image"] and (UPLOADS/p["image"]).exists(): (UPLOADS/p["image"]).unlink()
    flash("Маҳсулот нест шуд."); return redirect("/admin/products")

@app.route("/admin/orders")
@admin_required
def orders_admin():
    with connect() as db: orders=db.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    body="""<h1>Фармоишҳо</h1><div class="panel tablewrap"><table><tr><th>№</th><th>Мизоҷ</th><th>Телефон</th><th>Ҷамъ</th><th>Статус</th></tr>{% for o in orders %}<tr><td><a href="/admin/orders/{{o.id}}">#{{o.id}}</a></td><td>{{o.customer_name}}</td><td>{{o.phone}}</td><td>{{o.total|money}}</td><td>{{o.status}}</td></tr>{% endfor %}</table></div>"""
    return page("Фармоишҳо",body,orders=orders)
@app.route("/admin/orders/<int:oid>",methods=["GET","POST"])
@admin_required
def order_admin(oid):
    if request.method=="POST":
        check_csrf(); status=request.form.get("status")
        if status not in ["Нав","Тасдиқ шуд","Равон шуд","Анҷом ёфт","Бекор"]: abort(400)
        with connect() as db: db.execute("UPDATE orders SET status=? WHERE id=?",(status,oid))
        return redirect(url_for("order_admin",oid=oid))
    with connect() as db: o=db.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone(); lines=db.execute("SELECT * FROM order_items WHERE order_id=?",(oid,)).fetchall()
    if not o: abort(404)
    body="""<h1>Фармоиши #{{o.id}}</h1><div class="panel"><p>{{o.customer_name}} · {{o.phone}}</p><p>{{o.address}}</p><p>{{o.note}}</p><h2>{{o.total|money}} сомонӣ</h2><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><select name="status">{% for s in statuses %}<option {% if s==o.status %}selected{% endif %}>{{s}}</option>{% endfor %}</select><button>Навсозӣ</button></form><hr>{% for x in lines %}<p>{{x.product_name}} × {{x.quantity}} — {{(x.price*x.quantity)|money}}</p>{% endfor %}</div>"""
    return page("Фармоиш",body,o=o,lines=lines,statuses=["Нав","Тасдиқ шуд","Равон шуд","Анҷом ёфт","Бекор"])
@app.route("/admin/logout")
@admin_required
def logout():
    session.clear(); return redirect("/")
@app.errorhandler(400)
def err400(e): return page("Хато","<div class='panel'><h1>Дархости нодуруст</h1><a href='/'>Бозгашт</a></div>"),400
@app.errorhandler(404)
def err404(e): return page("Ёфт нашуд","<div class='panel'><h1>Саҳифа ёфт нашуд</h1><a href='/'>Ба мағоза</a></div>"),404

init_db()
if __name__=="__main__":
    app.run(host="127.0.0.1",port=5000,debug=False)
