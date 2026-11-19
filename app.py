from flask import Flask, request, redirect, session, render_template_string, url_for, flash, abort
 = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "Jurabekjon123.")
WHATSAPP_NUMBER = "".join(c for c in os.environ.get("WHATSAPP_NUMBER", "992919186336") if c.isdigit())

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "nexvora.db")
UPLOADS = os.path.join(BASE, "static", "uploads")
os.makedirs(UPLOADS, exist_ok=True)
app.config.update(MAX_CONTENT_LENGTH=5*1024*1024,
                  SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1")
ALLOWED = {"jpg", "jpeg", "png", "webp"}
STATUSES = ["Нав", "Тасдиқ шуд", "Омода мешавад", "Фиристода шуд", "Анҷом ёфт", "Бекор"]

def dbcon():
    con = sqlite3.connect(DB, timeout=20)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with dbcon() as d:
        d.execute("""CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        price REAL NOT NULL DEFAULT 0, image TEXT DEFAULT '',
        category TEXT DEFAULT 'Умумӣ', description TEXT DEFAULT '',
        stock INTEGER NOT NULL DEFAULT 0, sale_price REAL,
        featured INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        d.execute("""CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT NOT NULL,
        phone TEXT NOT NULL, address TEXT DEFAULT '', note TEXT DEFAULT '',
        items_json TEXT NOT NULL, total REAL NOT NULL,
        status TEXT DEFAULT 'Нав', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
init_db()

def csrf():
    if not session.get("_csrf"):
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]

@app.before_request
def check_csrf():
    if request.method == "POST":
        a, b = request.form.get("_csrf", ""), session.get("_csrf", "")
        if not a or not b or not hmac.compare_digest(a, b):
            abort(400, description="Санҷиши амниятӣ нодуруст аст. Саҳифаро нав кун.")

@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return r

def admin_required(fn):
    @wraps(fn)
    def inner(*a, **kw):
        if session.get("admin") is not True:
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return inner

def money(x): return f"{float(x or 0):,.2f}"
def price(p):
    return float(p["sale_price"]) if p["sale_price"] and float(p["sale_price"]) > 0 else float(p["price"])

def get_cart():
    raw = session.get("cart", {})
    if not isinstance(raw, dict): raw = {}
    clean = {}
    for k, v in raw.items():
        try:
            i, q = int(k), int(v)
            if i > 0 and 1 <= q <= 99: clean[str(i)] = q
        except (ValueError, TypeError): pass
    session["cart"] = clean
    return clean

def cart_info():
    items, total, count = [], 0.0, 0
    with dbcon() as d:
        for k, q in get_cart().items():
            p = d.execute("SELECT * FROM products WHERE id=? AND active=1", (int(k),)).fetchone()
            if p:
                pr = price(p); sub = pr*q
                items.append({"product":p, "quantity":q, "price":pr, "subtotal":sub})
                total += sub; count += q
    return items, total, count

@app.context_processor
def globals_for_templates():
    _, total, count = cart_info()
    return dict(cart_count=count, cart_total=money(total), money=money,
                csrf_token=csrf, order_statuses=STATUSES)

STYLE = """
:root{--bg:#08090c;--panel:#12151b;--gold:#e6c46b;--muted:#a5a9b2}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#f7f7f7;font-family:Arial}
a{color:inherit;text-decoration:none}button,input,textarea,select{font:inherit}
header{padding:18px;background:#11151c;border-bottom:1px solid #80652c}
.head{max-width:1150px;margin:auto;display:flex;justify-content:space-between;gap:15px;flex-wrap:wrap}
.logo{font-size:27px;font-weight:900;letter-spacing:5px;color:var(--gold)}
.nav{display:flex;gap:15px;align-items:center;flex-wrap:wrap;font-size:13px}
.wrap{max-width:1150px;margin:auto;padding:20px 14px}
.hero,.panel,.product{background:var(--panel);border:1px solid #393329;border-radius:16px;padding:18px;margin:12px 0}
.hero{padding:30px;background:radial-gradient(circle at 80% 20%,#51401f,#12151b 65%)}
.hero h1{font-size:clamp(28px,6vw,46px)}.gold,.price{color:var(--gold)}
.btn{display:inline-block;border:0;border-radius:9px;padding:12px 16px;background:linear-gradient(90deg,#b08c3e,#f2d889);color:#111;font-weight:bold;cursor:pointer;text-align:center}
.dark{background:#292e37;color:white}.green{background:#168653;color:white}.red{background:#a72c36;color:white}
.field{width:100%;padding:12px;background:#0c1016;color:white;border:1px solid #4a453a;border-radius:9px}
label{display:block;margin:10px 0 6px;color:#ccc;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.photo{height:175px;background:#0b0e13;display:flex;align-items:center;justify-content:center;overflow:hidden}
.photo img{width:100%;height:100%;object-fit:contain}.muted{color:var(--muted);font-size:13px;line-height:1.5}
.pbody{padding:12px}.price{font-weight:bold;font-size:19px}.old{text-decoration:line-through;color:#888;font-size:12px}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-top:10px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.statgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.stat{background:#1b2029;border:1px solid #54452a;border-radius:12px;padding:14px}
.stat b{display:block;color:var(--gold);font-size:22px;margin-top:6px}
table{width:100%;border-collapse:collapse;min-width:560px}td,th{text-align:left;padding:10px;border-bottom:1px solid #333;font-size:13px}th{color:var(--gold)}
.tablewrap{overflow-x:auto}.flash{background:#1b382d;padding:12px;border-radius:9px;margin:10px 0}
footer{text-align:center;color:#777;padding:25px;border-top:1px solid #333;margin-top:25px}
@media(max-width:850px){.grid{grid-template-columns:repeat(3,1fr)}}
@media(max-width:600px){.grid{grid-template-columns:repeat(2,1fr)}.photo{height:135px}.cols{grid-template-columns:1fr}.statgrid{grid-template-columns:1fr 1fr}.hero{padding:20px}}
@media(max-width:360px){.grid{grid-template-columns:1fr}}
"""
BASE_HTML = """<!doctype html><html lang="tg"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#08090c"><title>{{title}}</title><style>""" + STYLE + """</style></head><body>
<header><div class="head"><a href="/"><div class="logo">NEXVORA<small style="display:block;font-size:9px;letter-spacing:3px">PREMIUM ONLINE STORE</small></div></a>
<nav class="nav"><a href="/">Мағоза</a><a href="/cart">Сабад ({{cart_count}})</a><a href="/#categories">Категорияҳо</a>{% if session.get('admin') %}<a href="/admin">Admin</a>{% else %}<a href="/login">Admin</a>{% endif %}</nav></div></header>
{% with ms=get_flashed_messages() %}{% for m in ms %}<div class="wrap"><div class="flash">{{m}}</div></div>{% endfor %}{% endwith %}
{{body|safe}}<footer>NEXVORA STORE © 2026 • Сифат • Эътимод • Интихоби беҳтарин</footer></body></html>"""

def page(body, title="NEXVORA STORE", **ctx):
    ctx["title"] = title
    ctx["body"] = render_template_string(body, **ctx)
    return render_template_string(BASE_HTML, **ctx)

HOME = """<main class="wrap"><section class="hero"><small class="gold">WELCOME TO NEXVORA</small><h1>Интихоби беҳтарин — дар як ҷо.</h1><p class="muted">Маҳсулотро интихоб кун, ба сабад илова намо ва фармоишатро фирист.</p><a class="btn" href="#products">ҲОЗИР ХАРИД КУН</a></section>
<h2 id="categories">Категорияҳо</h2><div style="display:flex;gap:8px;overflow:auto;padding:8px 0"><a class="btn dark" href="/">Ҳама</a>{% for c in cats %}<a class="btn dark" href="/?category={{c|urlencode}}">{{c}}</a>{% endfor %}</div>
<h2 id="products">Маҳсулоти мағоза ({{products|length}})</h2><form method="get" style="display:flex;gap:8px;margin:12px 0"><input class="field" name="q" value="{{q}}" placeholder="Ҷустуҷӯи маҳсулот"><select class="field" name="sort" style="max-width:140px"><option value="new">Навтарин</option><option value="low" {{'selected' if sort=='low' else ''}}>Арзонтарин</option><option value="high" {{'selected' if sort=='high' else ''}}>Қиматтарин</option></select><button class="btn">Ҷустуҷӯ</button></form>
<div class="grid">{% for p in products %}<article class="product"><a href="/product/{{p.id}}"><div class="photo">{% if p.image %}<img src="{{url_for('static',filename='uploads/'+p.image)}}">{% else %}NEXVORA{% endif %}</div></a><div class="pbody"><div class="gold">{{p.category}} {% if p.featured %} • МАШҲУР{% endif %}</div><h3>{{p.name}}</h3><div class="price">{{money(p.sale_price if p.sale_price and p.sale_price>0 else p.price)}} сомонӣ</div><p class="muted">Дар анбор: {{p.stock}}</p><div class="actions"><form method="post" action="/cart/add/{{p.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn" style="width:100%">+ Сабад</button></form><a class="btn dark" href="/product/{{p.id}}">Дидан</a></div></div></article>{% else %}<div class="panel">Ҳоло маҳсулот нест.</div>{% endfor %}</div></main>"""

@app.route("/")
def home():
    q=request.args.get("q","").strip()[:80]; cat=request.args.get("category","").strip()[:80]
    sort=request.args.get("sort","new"); order={"low":"price ASC","high":"price DESC","new":"id DESC"}.get(sort,"id DESC")
    with dbcon() as d:
        cats=[r[0] for r in d.execute("SELECT DISTINCT category FROM products WHERE active=1 ORDER BY category")]
        sql="SELECT * FROM products WHERE active=1"; args=[]
        if q: sql+=" AND (name LIKE ? OR category LIKE ? OR description LIKE ?)"; args += [f"%{q}%"]*3
        if cat: sql+=" AND category=?"; args.append(cat)
        products=d.execute(sql+f" ORDER BY {order}",args).fetchall()
    return page(HOME, products=products,cats=cats,q=q,sort=sort)

@app.route("/product/<int:pid>")
def product_detail(pid):
    with dbcon() as d: p=d.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
    if not p: abort(404)
    body="""<main class="wrap"><div class="cols"><div class="panel"><div class="photo" style="height:320px">{% if p.image %}<img src="{{url_for('static',filename='uploads/'+p.image)}}">{% else %}NEXVORA{% endif %}</div></div><div class="panel"><p class="gold">{{p.category}}</p><h1>{{p.name}}</h1><p class="price">{{money(p.sale_price if p.sale_price and p.sale_price>0 else p.price)}} сомонӣ</p><p class="muted">{{p.description}}</p><p>Дар анбор: {{p.stock}}</p><form method="post" action="/cart/add/{{p.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn" style="width:100%" {% if p.stock<1 %}disabled{% endif %}>ИЛОВА БА САБАД</button></form><a class="btn green" style="display:block;margin-top:10px" href="https://wa.me/{{phone}}?text={{('Салом! Дар бораи '+p.name+' маълумот мехоҳам.')|urlencode}}">САВОЛ ДАР WHATSAPP</a></div></div></main>"""
    return page(body,p=p,phone=WHATSAPP_NUMBER)

CART = """<main class="wrap"><h1 class="gold">Сабади харид</h1>{% for x in items %}<div class="panel"><b>{{x.product.name}}</b><p>{{money(x.price)}} × {{x.quantity}} = {{money(x.subtotal)}} сомонӣ</p><div style="display:flex;gap:8px">{% for action,label in [('minus','−'),('add','+'),('remove','Нест')] %}<form method="post" action="/cart/{{action}}/{{x.product.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark">{{label}}</button></form>{% endfor %}</div></div>{% else %}<div class="panel">Сабад холӣ аст.</div>{% endfor %}<div class="panel"><h2>Ҳамагӣ: <span class="gold">{{money(total)}} сомонӣ</span></h2>{% if items %}<a class="btn" href="/checkout">РАСМИКУНОНИИ ФАРМОИШ</a>{% endif %} <a class="btn dark" href="/">Идома</a></div></main>"""
@app.route("/cart")
def cart():
    items,total,_=cart_info(); return page(CART,items=items,total=total)

@app.route("/cart/<action>/<int:pid>",methods=["POST"])
def cart_change(action,pid):
    c=get_cart(); key=str(pid)
    if action=="add":
        with dbcon() as d: p=d.execute("SELECT stock FROM products WHERE id=? AND active=1",(pid,)).fetchone()
        if p and c.get(key,0)<min(99,p["stock"]): c[key]=c.get(key,0)+1
        else: flash("Миқдори маҳсулот дар анбор кофӣ нест.")
    elif action=="minus":
        if key in c:
            c[key]-=1
            if c[key]<=0: c.pop(key)
    elif action=="remove": c.pop(key,None)
    session["cart"]=c
    return redirect("/cart")

CHECKOUT = """<main class="wrap" style="max-width:700px"><h1 class="gold">Расмикунонии фармоиш</h1><div class="panel"><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Ному насаб</label><input class="field" name="name" required maxlength="100"><label>Телефон</label><input class="field" name="phone" required maxlength="30" placeholder="+992..."><label>Суроға (ихтиёрӣ)</label><input class="field" name="address" maxlength="250"><label>Шарҳ (ихтиёрӣ)</label><textarea class="field" name="note" maxlength="500"></textarea><h3 class="gold">Ҳамагӣ: {{money(total)}} сомонӣ</h3><button class="btn" style="width:100%">САБТИ ФАРМОИШ</button></form></div></main>"""
@app.route("/checkout",methods=["GET","POST"])
def checkout():
    items,total,_=cart_info()
    if not items: return redirect("/cart")
    if request.method=="GET": return page(CHECKOUT,total=total)
    name=request.form.get("name","").strip()[:100]; phone=request.form.get("phone","").strip()[:30]
    address=request.form.get("address","").strip()[:250]; note=request.form.get("note","").strip()[:500]
    if not name or not re.fullmatch(r"[0-9+() -]{5,30}",phone):
        flash("Ном ё рақами телефон нодуруст аст."); return redirect("/checkout")
    order_items=[]
    with dbcon() as d:
        for x in items:
            p=d.execute("SELECT * FROM products WHERE id=? AND active=1",(x["product"]["id"],)).fetchone()
            if not p or p["stock"]<x["quantity"]:
                flash("Маҳсулот ё миқдораш дигар дастрас нест."); return redirect("/cart")
            pr=price(p); order_items.append({"id":p["id"],"name":p["name"],"quantity":x["quantity"],"price":pr,"subtotal":pr*x["quantity"]})
        total=sum(x["subtotal"] for x in order_items)
        cur=d.execute("INSERT INTO orders(customer_name,phone,address,note,items_json,total) VALUES(?,?,?,?,?,?)",
          (name,phone,address,note,json.dumps(order_items,ensure_ascii=False),total))
        oid=cur.lastrowid
        for x in order_items: d.execute("UPDATE products SET stock=stock-? WHERE id=?",(x["quantity"],x["id"]))
    session["cart"]={}
    msg=f"Салом! Фармоиши №{oid} аз NEXVORA STORE\nХаридор: {name}\nТелефон: {phone}\n"
    if address: msg+=f"Суроға: {address}\n"
    msg+="\nМаҳсулотҳо:\n"
    for x in order_items: msg+=f"{x['name']} × {x['quantity']} = {x['subtotal']:.2f} сомонӣ\n"
    msg+=f"\nҲамагӣ: {total:.2f} сомонӣ"
    if note: msg+=f"\nШарҳ: {note}"
    wa="https://wa.me/"+WHATSAPP_NUMBER+"?text="+quote(msg)
    return page("""<main class="wrap"><div class="panel" style="text-align:center"><h1 class="gold">Фармоиш сабт шуд!</h1><p>Фармоиши №{{oid}}</p><p class="muted">WhatsApp-ро кушо ва матни тайёрро фирист.</p><a class="btn green" href="{{wa}}">КУШОДАНИ WHATSAPP</a><p><a href="/">Бозгашт ба мағоза</a></p></div></main>""",oid=oid,wa=wa)

LOGIN="""<main class="wrap" style="max-width:450px"><div class="panel"><h1 class="gold">NEXVORA ADMIN</h1><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Пароли Admin</label><input class="field" type="password" name="password" required><button class="btn" style="width:100%;margin-top:12px">ВОРОД ШУДАН</button></form></div></main>"""
@app.route("/login",methods=["GET","POST"])
def login():
    if request.method=="POST":
        pw=request.form.get("password","")
        if ADMIN_PASSWORD!="CHANGE_THIS_ADMIN_PASSWORD" and hmac.compare_digest(pw,ADMIN_PASSWORD):
            session.clear(); session["admin"]=True; session["_csrf"]=secrets.token_urlsafe(32)
            return redirect("/admin")
        flash("Парол нодуруст аст. ADMIN_PASSWORD-ро танзим кун.")
    return page(LOGIN,title="Admin Login")

@app.route("/logout",methods=["POST"])
def logout():
    session.clear(); return redirect("/")

ADMIN="""<main class="wrap"><div style="display:flex;justify-content:space-between;align-items:center"><h1 class="gold">Панели Admin</h1><form method="post" action="/logout"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn dark">Баромадан</button></form></div>
<div class="statgrid"><div class="stat">Маҳсулот<b>{{pc}}</b></div><div class="stat">Фармоишҳо<b>{{oc}}</b></div><div class="stat">Даромад<b>{{money(revenue)}} см.</b></div></div>
<div class="panel"><h2 class="gold">Иловаи маҳсулот</h2><form method="post" action="/admin/add" enctype="multipart/form-data"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><div class="cols"><div><label>Ном *</label><input class="field" name="name" required maxlength="120"></div><div><label>Категория</label><input class="field" name="category" maxlength="80"></div><div><label>Нарх *</label><input class="field" type="number" step=".01" min="0" name="price" required></div><div><label>Нархи тахфифӣ</label><input class="field" type="number" step=".01" min="0" name="sale"></div><div><label>Миқдори анбор</label><input class="field" type="number" min="0" name="stock" value="0"></div><div><label>Акс</label><input class="field" type="file" name="image" accept=".jpg,.jpeg,.png,.webp"></div></div><label>Тавсиф</label><textarea class="field" name="description" maxlength="1000"></textarea><label><input type="checkbox" name="featured"> Маҳсулоти машҳур</label><button class="btn">ИЛОВА КАРДАН</button></form></div>
<div class="panel"><h2>Маҳсулотҳо</h2><div class="tablewrap"><table><tr><th>Ном</th><th>Нарх</th><th>Анбор</th><th>Амалиёт</th></tr>{% for p in products %}<tr><td>{{p.name}}<br>{{p.category}}</td><td>{{money(p.sale_price if p.sale_price else p.price)}}</td><td>{{p.stock}}</td><td><a class="gold" href="/admin/edit/{{p.id}}">Таҳрир</a> <form style="display:inline" method="post" action="/admin/delete/{{p.id}}"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><button class="btn red">Нест</button></form></td></tr>{% endfor %}</table></div></div>
<div class="panel"><h2>Фармоишҳо</h2><div class="tablewrap"><table><tr><th>№</th><th>Харидор</th><th>Ҳамагӣ</th><th>Вазъият</th><th>Сана</th><th></th></tr>{% for o in orders %}<tr><td>#{{o.id}}</td><td>{{o.customer_name}}<br>{{o.phone}}</td><td>{{money(o.total)}}</td><td>{{o.status}}</td><td>{{o.created_at}}</td><td><a class="gold" href="/admin/order/{{o.id}}">Кушодан</a></td></tr>{% endfor %}</table></div></div></main>"""
@app.route("/admin")
@admin_required
def admin():
    with dbcon() as d:
        ps=d.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
        os_=d.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 50").fetchall()
        pc=d.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        oc=d.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        rev=d.execute("SELECT COALESCE(SUM(total),0) FROM orders WHERE status!='Бекор'").fetchone()[0]
    return page(ADMIN,products=ps,orders=os_,pc=pc,oc=oc,revenue=rev)

@app.route("/admin/add",methods=["POST"])
@admin_required
def admin_add():
    name=request.form.get("name","").strip()[:120]
    cat=request.form.get("category","").strip()[:80] or "Умумӣ"
    desc=request.form.get("description","").strip()[:1000]
    try:
        pr=float(request.form.get("price","")); stock=int(request.form.get("stock","0"))
        sr=request.form.get("sale","").strip(); sale=float(sr) if sr else None
        if not name or pr<0 or stock<0 or (sale is not None and (sale<=0 or sale>=pr)): raise ValueError
    except ValueError:
        flash("Маълумоти маҳсулот ё нарх нодуруст аст."); return redirect("/admin")
    f=request.files.get("image"); img=""
    if f and f.filename:
        ext=f.filename.rsplit(".",1)[-1].lower() if "." in f.filename else ""
        if ext not in ALLOWED: flash("Формати акс нодуруст аст."); return redirect("/admin")
        img=uuid.uuid4().hex+"."+ext; f.save(os.path.join(UPLOADS,img))
    with dbcon() as d:
        d.execute("INSERT INTO products(name,price,image,category,description,stock,sale_price,featured) VALUES(?,?,?,?,?,?,?,?)",
          (name,pr,img,cat,desc,stock,sale,1 if request.form.get("featured") else 0))
    flash("Маҳсулот илова шуд."); return redirect("/admin")

EDIT="""<main class="wrap"><div class="panel"><h1 class="gold">Таҳрири маҳсулот</h1><form method="post" enctype="multipart/form-data"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Ном</label><input class="field" name="name" value="{{p.name}}" required><label>Категория</label><input class="field" name="category" value="{{p.category}}"><label>Нарх</label><input class="field" type="number" step=".01" min="0" name="price" value="{{p.price}}"><label>Тахфиф</label><input class="field" type="number" step=".01" min="0" name="sale" value="{{p.sale_price or ''}}"><label>Анбор</label><input class="field" type="number" min="0" name="stock" value="{{p.stock}}"><label>Тавсиф</label><textarea class="field" name="description">{{p.description}}</textarea>{% if p.image %}<img style="max-width:150px" src="{{url_for('static',filename='uploads/'+p.image)}}">{% endif %}<label>Акси нав</label><input class="field" type="file" name="image" accept=".jpg,.jpeg,.png,.webp"><label><input type="checkbox" name="featured" {{'checked' if p.featured else ''}}> Машҳур</label><label><input type="checkbox" name="active" {{'checked' if p.active else ''}}> Намоиш дода шавад</label><button class="btn">НИГОҲ ДОШТАН</button></form><a href="/admin">Бозгашт</a></div></main>"""
@app.route("/admin/edit/<int:pid>",methods=["GET","POST"])
@admin_required
def edit_product(pid):
    with dbcon() as d:
        p=d.execute("SELECT * FROM products WHERE id=?",(pid,)).fetchone()
        if not p: abort(404)
        if request.method=="POST":
            try:
                name=request.form.get("name","").strip()[:120]; cat=request.form.get("category","").strip()[:80] or "Умумӣ"
                desc=request.form.get("description","").strip()[:1000]; pr=float(request.form.get("price",""))
                sr=request.form.get("sale","").strip(); sale=float(sr) if sr else None; stock=int(request.form.get("stock","0"))
                if not name or pr<0 or stock<0 or (sale is not None and (sale<=0 or sale>=pr)): raise ValueError
            except ValueError:
                flash("Маълумоти воридшуда нодуруст аст."); return redirect(url_for("edit_product",pid=pid))
            img=p["image"]; f=request.files.get("image")
            if f and f.filename:
                ext=f.filename.rsplit(".",1)[-1].lower() if "." in f.filename else ""
                if ext not in ALLOWED: flash("Формати акс нодуруст аст."); return redirect(url_for("edit_product",pid=pid))
                new=uuid.uuid4().hex+"."+ext; f.save(os.path.join(UPLOADS,new))
                if img:
                    old=os.path.join(UPLOADS,os.path.basename(img))
                    if os.path.isfile(old): os.remove(old)
                img=new
            d.execute("UPDATE products SET name=?,category=?,description=?,price=?,sale_price=?,stock=?,image=?,featured=?,active=? WHERE id=?",
              (name,cat,desc,pr,sale,stock,img,1 if request.form.get("featured") else 0,1 if request.form.get("active") else 0,pid))
            flash("Маҳсулот нав шуд."); return redirect("/admin")
    return page(EDIT,p=p)

@app.route("/admin/delete/<int:pid>",methods=["POST"])
@admin_required
def delete_product(pid):
    with dbcon() as d:
        p=d.execute("SELECT image FROM products WHERE id=?",(pid,)).fetchone()
        if p:
            d.execute("DELETE FROM products WHERE id=?",(pid,))
            if p["image"]:
                path=os.path.join(UPLOADS,os.path.basename(p["image"]))
                if os.path.isfile(path): os.remove(path)
    c=get_cart(); c.pop(str(pid),None); session["cart"]=c
    flash("Маҳсулот нест карда шуд."); return redirect("/admin")

ORDER="""<main class="wrap"><div class="panel"><h1 class="gold">Фармоиши №{{o.id}}</h1><p>Харидор: {{o.customer_name}}</p><p>Телефон: {{o.phone}}</p><p>Суроға: {{o.address}}</p><p>Шарҳ: {{o.note}}</p><h3>Маҳсулотҳо</h3>{% for x in its %}<p>{{x.name}} × {{x.quantity}} = {{money(x.subtotal)}} сомонӣ</p>{% endfor %}<h2 class="gold">Ҳамагӣ: {{money(o.total)}} сомонӣ</h2><form method="post"><input type="hidden" name="_csrf" value="{{csrf_token()}}"><label>Вазъият</label><select class="field" name="status">{% for s in order_statuses %}<option {{'selected' if o.status==s else ''}}>{{s}}</option>{% endfor %}</select><button class="btn">НАВ КАРДАН</button></form><a href="/admin">Бозгашт</a></div></main>"""
@app.route("/admin/order/<int:oid>",methods=["GET","POST"])
@admin_required
def order_detail(oid):
    with dbcon() as d:
        o=d.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
        if not o: abort(404)
        if request.method=="POST":
            s=request.form.get("status","")
            if s not in STATUSES: abort(400)
            d.execute("UPDATE orders SET status=? WHERE id=?",(s,oid)); flash("Вазъи фармоиш нав шуд."); return redirect(url_for("order_detail",oid=oid))
    try: its=json.loads(o["items_json"])
    except Exception: its=[]
    return page(ORDER,o=o,its=its)

@app.errorhandler(400)
def bad_request(e):
    return page('<main class="wrap"><div class="panel"><h1 class="gold">400</h1><p>{{msg}}</p><a class="btn" href="/">Ба мағоза</a></div></main>',title="400",msg=getattr(e,"description","Дархост нодуруст аст.")),400
@app.errorhandler(404)
def not_found(e):
    return page('<main class="wrap"><div class="panel"><h1 class="gold">404</h1><p>Саҳифа ёфт нашуд.</p><a class="btn" href="/">Ба мағоза</a></div></main>',title="404"),404
@app.errorhandler(413)
def too_large(e):
    return page('<main class="wrap"><div class="panel"><h2 class="gold">Акс хеле калон аст</h2><p>Ҳадди аксар 5 MB.</p><a href="/admin">Бозгашт</a></div></main>'),413

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
