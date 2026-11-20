from flask import Flask, request, redirect, url_for, session, flash, render_template_string, send_from_directory, abort, Response
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime
from urllib.parse import quote
from functools import wraps
import os, sqlite3, uuid, json, secrets, csv, io, hmac

# ===================== NEXVORA STORE =====================
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# CONFIG: WhatsApp number should be international format, digits only, e.g. 992XXXXXXXXX
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '992919186336')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'Jurabekjon123.')
DELIVERY_FEE = max(0, int(os.environ.get('DELIVERY_FEE', '0')))
DATA_DIR = os.environ.get('DATA_DIR', os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
DB_PATH = os.path.join(DATA_DIR, 'nexvora.db')
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMAGES = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
ALLOWED_VIDEOS = {'mp4', 'webm'}

# ===================== DATABASE =====================
def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

def init_db():
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, description TEXT DEFAULT '', price REAL NOT NULL DEFAULT 0,
            old_price REAL DEFAULT 0, category TEXT DEFAULT 'Дигар', images TEXT DEFAULT '[]',
            video TEXT DEFAULT '', stock INTEGER DEFAULT 0, featured INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS orders(
            id TEXT PRIMARY KEY, customer TEXT, phone TEXT, address TEXT, items TEXT,
            subtotal REAL, discount REAL DEFAULT 0, delivery REAL DEFAULT 0, total REAL,
            promo TEXT DEFAULT '', status TEXT DEFAULT 'Қабул шуд', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, discount REAL, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        ''')
        cols = {r['name'] for r in con.execute('PRAGMA table_info(products)')}
        migrations = [('old_price','REAL DEFAULT 0'),('images',"TEXT DEFAULT '[]'"),('video',"TEXT DEFAULT ''"),('stock','INTEGER DEFAULT 0'),('featured','INTEGER DEFAULT 0'),('active','INTEGER DEFAULT 1')]
        for col, typ in migrations:
            if col not in cols:
                con.execute(f'ALTER TABLE products ADD COLUMN {col} {typ}')
        con.execute("INSERT OR IGNORE INTO promos(code,discount,active) VALUES('WELCOME',10,1)")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('banner','NEXVORA — Хариди осон ва боэътимод')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('video_ad','')")
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('whatsapp','')")
init_db()

# ===================== SECURITY / HELPERS =====================
def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(24)
    return session['csrf']
app.jinja_env.globals['csrf_token'] = csrf_token

@app.before_request
def csrf_check():
    if request.method == 'POST' and not hmac.compare_digest(str(request.form.get('csrf','')), str(session.get('csrf',''))):
        abort(400, 'CSRF validation failed')

def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('admin_login'))
        return fn(*args, **kwargs)
    return wrapped

def allowed_file(filename, video=False):
    if not filename or '.' not in filename:
        return False
    ext = filename.rsplit('.', 1)[1].lower()
    return ext in (ALLOWED_VIDEOS if video else ALLOWED_IMAGES)

def save_upload(file, video=False):
    if not file or not file.filename or not allowed_file(file.filename, video):
        return ''
    ext = file.filename.rsplit('.',1)[1].lower()
    filename = secure_filename(f'{uuid.uuid4().hex}.{ext}')
    file.save(os.path.join(UPLOAD_DIR, filename))
    return filename

def setting(key, default=''):
    with db() as con:
        r = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return r['value'] if r else default

def set_setting(key, value):
    with db() as con:
        con.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (key, value))

def p_dict(row):
    p = dict(row)
    try: p['images'] = json.loads(p.get('images') or '[]')
    except (ValueError, TypeError): p['images'] = []
    return p

def cart_data():
    cart = session.get('cart', {})
    valid = {str(k): max(1, int(v)) for k,v in cart.items() if str(k).isdigit()}
    ids = [int(k) for k in valid]
    if not ids: return [], 0
    with db() as con:
        rows = con.execute(f"SELECT * FROM products WHERE active=1 AND id IN ({','.join('?'*len(ids))})", ids).fetchall()
    items, total, clean_cart = [], 0, {}
    for row in rows:
        p = p_dict(row)
        qty = min(valid.get(str(p['id']), 1), max(0, int(p['stock'])))
        if qty < 1: continue
        p['qty'] = qty; p['line_total'] = p['price'] * qty
        items.append(p); total += p['line_total']; clean_cart[str(p['id'])] = qty
    session['cart'] = clean_cart
    return items, total

BASE = r'''<!doctype html><html lang="tg"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#101116"><title>{{ title }} · NEXVORA</title>
<style>
:root{--bg:#0b0b10;--panel:#15151d;--panel2:#1d1d27;--gold:#e7bd63;--gold2:#f6d98e;--text:#f7f5ef;--muted:#aaa8b3;--line:#30303b;--green:#6dd59a;--red:#ff7676}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 12% 0%,#211b29 0,transparent 32%),var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}a{color:inherit;text-decoration:none}img{max-width:100%}.wrap{max-width:1180px;margin:auto;padding:16px}.nav{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 0 18px}.brand{font-weight:950;font-size:26px;letter-spacing:3px;background:linear-gradient(90deg,var(--gold),#fff0bd);-webkit-background-clip:text;color:transparent}.links{display:flex;gap:8px;flex-wrap:wrap}.links a,.navlink{background:var(--panel2);border:1px solid var(--line);padding:9px 12px;border-radius:12px;font-size:14px}.hero{padding:30px 24px;border:1px solid #50432b;border-radius:22px;background:linear-gradient(130deg,#211d28,#302719 65%,#17151d);margin:10px 0 22px;box-shadow:0 12px 40px #0003}.hero h1{margin:0 0 8px;font-size:clamp(24px,5vw,38px)}.muted{color:var(--muted)}.gold{color:var(--gold)}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:15px}.card{background:linear-gradient(160deg,#1b1b24,#14141b);border:1px solid var(--line);border-radius:18px;padding:13px;overflow:hidden;box-shadow:0 8px 24px #0002}.pic{width:100%;height:200px;object-fit:cover;border-radius:13px;background:#252530}.price{font-size:20px;font-weight:850;color:var(--gold2)}.old{text-decoration:line-through;color:#8d8994;font-size:14px;margin-left:7px}.btn,button{border:0;display:inline-flex;align-items:center;justify-content:center;gap:6px;background:linear-gradient(135deg,var(--gold2),var(--gold));color:#1b1710;padding:11px 15px;border-radius:12px;font-weight:800;cursor:pointer;font-family:inherit}.btn2{background:#292934;color:white;border:1px solid #41414c}.danger{background:#47252a;color:#fff}.form{display:grid;gap:12px;max-width:720px}.input,select,textarea{width:100%;padding:12px;border-radius:11px;border:1px solid #3b3b47;background:#101017;color:#fff;font:inherit}.input:focus,select:focus,textarea:focus{outline:2px solid #cba65c;outline-offset:1px}.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center}.tag{display:inline-block;padding:5px 9px;border-radius:99px;background:#30291e;color:var(--gold2);font-size:12px;border:1px solid #55452b}.table{width:100%;border-collapse:collapse;background:#14141b;border-radius:12px;overflow:hidden}.table td,.table th{padding:10px;border-bottom:1px solid #30303b;text-align:left;vertical-align:top}.flash{padding:12px;border-radius:12px;background:#243329;border:1px solid #35523d;margin:10px 0}.footer{padding:28px 12px;text-align:center;color:var(--muted)}.small{font-size:13px}.video{width:100%;max-height:360px;border-radius:16px;margin:14px 0}.search{display:grid;grid-template-columns:2fr 1fr 1fr auto;gap:8px;margin:14px 0 20px}.adminbox{border:1px solid var(--line);padding:14px;border-radius:15px;margin:12px 0}.stat{flex:1;min-width:130px}.stat strong{font-size:24px;color:var(--gold)}.thumb{width:64px;height:64px;object-fit:cover;border-radius:9px}.status{font-weight:700;color:var(--gold2)}.wide{width:100%}.empty{text-align:center;padding:32px;border:1px dashed #41414b;border-radius:18px;color:var(--muted)}
@media(max-width:700px){.wrap{padding:11px}.search{grid-template-columns:1fr}.pic{height:165px}.brand{font-size:22px}.table{font-size:12px;display:block;overflow-x:auto;white-space:nowrap}.hero{padding:22px 16px}.links a{padding:8px 10px}.card{padding:10px}}
</style></head><body><div class="wrap"><nav class="nav"><a class="brand" href="/">NEXVORA</a><div class="links"><a href="/">🏠 Асосӣ</a><a href="/favorites">❤️</a><a href="/cart">🛒 Корзина ({{ cart_count }})</a><a href="/track">📦 Пайгирӣ</a><a href="/admin">⚙️ Admin</a></div></nav>
{% with messages=get_flashed_messages() %}{% for m in messages %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}{{ body|safe }}<footer class="footer">© {{ year }} NEXVORA STORE · Хариди онлайн осон ва қулай</footer></div></body></html>'''

def page(title, body, **kwargs):
    count = sum(int(v) for v in session.get('cart', {}).values())
    return render_template_string(BASE, title=title, body=body, cart_count=count, year=datetime.now().year, **kwargs)

# ===================== SHOP PAGES =====================
@app.route('/')
def home():
    q=request.args.get('q','').strip(); category=request.args.get('category',''); sort=request.args.get('sort','new')
    sql='SELECT * FROM products WHERE active=1'; args=[]
    if q: sql+=' AND (name LIKE ? OR description LIKE ?)'; args += [f'%{q}%',f'%{q}%']
    if category: sql+=' AND category=?'; args.append(category)
    sql+=' ORDER BY featured DESC, '+('price ASC' if sort=='cheap' else 'price DESC' if sort=='expensive' else 'id DESC')
    with db() as con:
        rows=con.execute(sql,args).fetchall(); cats=con.execute('SELECT DISTINCT category FROM products WHERE active=1 ORDER BY category').fetchall()
    products=[p_dict(r) for r in rows]
    body=render_template_string('''<section class="hero"><h1>{{ banner }}</h1><p class="muted">Маҳсулоти нав · Нархи хуб · Фармоиши осон</p><div class="row"><span class="tag">✨ Нав</span><span class="tag">🔥 Аксия</span><span class="tag">⭐ Тавсия</span></div></section>
    {% if video %}<video class="video" controls muted loop src="/uploads/{{ video }}"></video>{% endif %}
    <form class="search"><input class="input" name="q" value="{{ q }}" placeholder="🔎 Ҷустуҷӯи маҳсулот..."><select name="category"><option value="">Ҳама категорияҳо</option>{% for c in cats %}<option value="{{ c.category }}" {% if category==c.category %}selected{% endif %}>{{ c.category }}</option>{% endfor %}</select><select name="sort"><option value="new">Навтарин</option><option value="cheap" {% if sort=='cheap' %}selected{% endif %}>Арзонтарин</option><option value="expensive" {% if sort=='expensive' %}selected{% endif %}>Қиматтарин</option></select><button>Ҷустуҷӯ</button></form>
    <div class="grid">{% for p in products %}<article class="card"><a href="/product/{{ p.id }}">{% if p.images %}<img class="pic" src="/uploads/{{ p.images[0] }}" alt="{{ p.name }}">{% else %}<div class="pic"></div>{% endif %}</a>{% if p.featured %}<span class="tag">⭐ Тавсия</span>{% endif %}<h3>{{ p.name }}</h3><p class="muted small">{{ p.category }}</p><div class="price">{{ '%.2f'|format(p.price) }} сомонӣ{% if p.old_price and p.old_price>p.price %}<span class="old">{{ '%.2f'|format(p.old_price) }}</span>{% endif %}</div><p class="small muted">{% if p.stock>0 %}📦 Дар анбор: {{ p.stock }}{% else %}Нест{% endif %}</p><div class="row"><a class="btn" href="/product/{{ p.id }}">Дидан</a>{% if p.stock>0 %}<form method="post" action="/cart/add/{{ p.id }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button>🛒</button></form>{% endif %}</div></article>{% endfor %}</div>{% if not products %}<div class="empty">Маҳсулот ёфт нашуд. Ҷустуҷӯи дигарро санҷед.</div>{% endif %}''', banner=setting('banner','NEXVORA STORE'),video=setting('video_ad',''),q=q,category=category,sort=sort,cats=cats,products=products)
    return page('Асосӣ',body)

@app.route('/product/<int:pid>')
def product(pid):
    with db() as con: row=con.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
    if not row: abort(404)
    p=p_dict(row); fav=session.get('favorites',[])
    body=render_template_string('''<div class="card"><div class="grid">{% for im in p.images %}<img class="pic" src="/uploads/{{ im }}" alt="{{ p.name }}">{% endfor %}</div>{% if p.video %}<video class="video" controls src="/uploads/{{ p.video }}"></video>{% endif %}<h1>{{ p.name }}</h1><span class="tag">{{ p.category }}</span><p>{{ p.description }}</p><div class="price">{{ '%.2f'|format(p.price) }} сомонӣ{% if p.old_price>p.price %}<span class="old">{{ '%.2f'|format(p.old_price) }}</span>{% endif %}</div><p>📦 Дар анбор: {{ p.stock }}</p><div class="row">{% if p.stock>0 %}<form method="post" action="/cart/add/{{ p.id }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button>🛒 Ба корзина</button></form>{% endif %}<form method="post" action="/favorite/{{ p.id }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="btn2">❤️ {% if p.id|string in fav %}Нест кардан{% else %}Избранное{% endif %}</button></form></div></div><p><a class="btn btn2" href="/">← Ба мағоза</a></p>''',p=p,fav=fav)
    return page(p['name'],body)

@app.post('/cart/add/<int:pid>')
def cart_add(pid):
    with db() as con: p=con.execute('SELECT id,stock,active FROM products WHERE id=?',(pid,)).fetchone()
    if not p or not p['active'] or p['stock']<1: flash('Ин маҳсулот ҳоло дастрас нест.')
    else:
        cart=session.get('cart',{}); cart[str(pid)]=min(int(cart.get(str(pid),0))+1,int(p['stock'])); session['cart']=cart; flash('Ба корзина илова шуд!')
    return redirect(request.referrer or '/')

@app.post('/cart/remove/<int:pid>')
def cart_remove(pid):
    cart=session.get('cart',{}); cart.pop(str(pid),None); session['cart']=cart; return redirect('/cart')

@app.route('/cart')
def cart():
    items,total=cart_data()
    body=render_template_string('''<h1>🛒 Корзина</h1>{% if items %}<table class="table"><tr><th>Маҳсулот</th><th>Миқдор</th><th>Нарх</th><th></th></tr>{% for p in items %}<tr><td>{{ p.name }}</td><td>{{ p.qty }}</td><td>{{ '%.2f'|format(p.line_total) }} сомонӣ</td><td><form method="post" action="/cart/remove/{{ p.id }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="danger">✖</button></form></td></tr>{% endfor %}</table><h2>Ҳамагӣ: <span class="gold">{{ '%.2f'|format(total) }} сомонӣ</span></h2><a class="btn" href="/checkout">📦 Оформление</a>{% else %}<div class="empty">Корзина холӣ аст.<br><br><a class="btn" href="/">Харид кардан</a></div>{% endif %}''',items=items,total=total)
    return page('Корзина',body)

@app.route('/favorites')
def favorites():
    ids=session.get('favorites',[])
    with db() as con: rows=con.execute('SELECT * FROM products WHERE active=1 AND id IN (%s)'%(','.join('?'*len(ids)) if ids else 'NULL'),ids).fetchall()
    products=[p_dict(r) for r in rows]
    body=render_template_string('''<h1>❤️ Избранное</h1><div class="grid">{% for p in products %}<div class="card">{% if p.images %}<img class="pic" src="/uploads/{{ p.images[0] }}">{% else %}<div class="pic"></div>{% endif %}<h3>{{ p.name }}</h3><p class="price">{{ '%.2f'|format(p.price) }} сомонӣ</p><a class="btn" href="/product/{{ p.id }}">Дидан</a></div>{% endfor %}</div>{% if not products %}<div class="empty">Ҳоло маҳсулоти дӯстдошта нест.</div>{% endif %}''',products=products)
    return page('Избранное',body)

@app.post('/favorite/<int:pid>')
def favorite(pid):
    with db() as con:
        exists=con.execute('SELECT id FROM products WHERE id=? AND active=1',(pid,)).fetchone()
    if not exists: abort(404)
    fav=session.get('favorites',[]); key=str(pid)
    if key in fav: fav.remove(key)
    else: fav.append(key)
    session['favorites']=fav
    return redirect(request.referrer or '/')

@app.route('/checkout',methods=['GET','POST'])
def checkout():
    items,subtotal=cart_data()
    if not items: return redirect('/cart')
    promo=session.get('promo',''); discount=0
    if promo:
        with db() as con: pr=con.execute('SELECT discount FROM promos WHERE code=? AND active=1',(promo,)).fetchone()
        if pr: discount=min(subtotal,max(0,float(pr['discount'])))
    delivery=DELIVERY_FEE; total=max(0,subtotal-discount)+delivery
    if request.method=='POST':
        name=request.form.get('name','').strip(); phone=request.form.get('phone','').strip(); address=request.form.get('address','').strip()
        if not name or not phone or not address: flash('Ҳама маълумотро пур кунед.')
        else:
            oid=uuid.uuid4().hex[:10].upper()
            order_items=[{'product_id':p['id'],'name':p['name'],'qty':p['qty'],'price':p['price']} for p in items]
            with db() as con:
                # Verify stock before deducting, within the same transaction.
                for p in items:
                    cur=con.execute('SELECT stock,active FROM products WHERE id=?',(p['id'],)).fetchone()
                    if not cur or not cur['active'] or cur['stock']<p['qty']:
                        flash('Миқдор дар анбор тағйир ёфт. Корзинаро нав кунед.'); return redirect('/cart')
                for p in items: con.execute('UPDATE products SET stock=stock-? WHERE id=?',(p['qty'],p['id']))
                con.execute('INSERT INTO orders(id,customer,phone,address,items,subtotal,discount,delivery,total,promo) VALUES(?,?,?,?,?,?,?,?,?,?)',(oid,name,phone,address,json.dumps(order_items,ensure_ascii=False),subtotal,discount,delivery,total,promo))
            session['cart']={}; session.pop('promo',None)
            wa_num=(setting('whatsapp','').strip() or WHATSAPP_NUMBER).strip()
            textmsg=f'Фармоиш №{oid}\nНом: {name}\nТелефон: {phone}\nАдрес: {address}\nҲамагӣ: {total:.2f} сомонӣ'
            wa=f'https://wa.me/{wa_num}?text={quote(textmsg)}' if wa_num and 'X' not in wa_num else ''
            body=render_template_string('''<section class="hero"><h1>✅ Фармоиш қабул шуд!</h1><h2>Код: <span class="gold">{{ oid }}</span></h2><p>Кодро нигоҳ доред, барои пайгирӣ лозим мешавад.</p><a class="btn" href="/track?id={{ oid }}">📦 Пайгирии фармоиш</a>{% if wa %}<p><a class="btn" href="{{ wa }}" target="_blank" rel="noopener">💬 Фиристодан ба WhatsApp</a></p>{% endif %}</section>''',oid=oid,wa=wa)
            return page('Фармоиш',body)
    body=render_template_string('''<h1>📦 Оформление</h1><div class="card"><p>Маҳсулот: {{ '%.2f'|format(subtotal) }} сомонӣ</p><p>Доставка: {{ delivery }} сомонӣ</p><p>Скидка: {{ '%.2f'|format(discount) }} сомонӣ</p><h2>Ҳамагӣ: <span class="gold">{{ '%.2f'|format(total) }} сомонӣ</span></h2></div><form method="post" class="form"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><input class="input" name="name" placeholder="Номи шумо" required maxlength="100"><input class="input" name="phone" placeholder="Телефон" required maxlength="40"><textarea class="input" name="address" placeholder="Адрес / маҳалла" required maxlength="500"></textarea><button>✅ Фармоиш додан</button></form><form method="post" action="/promo" class="row" style="margin-top:14px"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><input class="input" name="code" placeholder="Промокод"><button class="btn2">🏷️ Истифода</button></form>''',subtotal=subtotal,delivery=delivery,discount=discount,total=total)
    return page('Оформление',body)

@app.post('/promo')
def promo():
    code=request.form.get('code','').strip().upper()
    with db() as con: p=con.execute('SELECT discount FROM promos WHERE code=? AND active=1',(code,)).fetchone()
    if p: session['promo']=code; flash(f'Промокод фаъол шуд: скидка {p["discount"]} сомонӣ')
    else: session.pop('promo',None); flash('Промокод нодуруст аст.')
    return redirect('/checkout')

@app.route('/track',methods=['GET','POST'])
def track():
    oid=request.values.get('id','').strip().upper(); row=None
    if oid:
        with db() as con: row=con.execute('SELECT id,status,created_at FROM orders WHERE id=?',(oid,)).fetchone()
    body=render_template_string('''<h1>📦 Пайгирии фармоиш</h1><form class="row"><input class="input" name="id" placeholder="Коди фармоиш" value="{{ oid }}"><button>Ҷустуҷӯ</button></form>{% if row %}<div class="card"><h2>Фармоиш: {{ row.id }}</h2><p>Статус: <span class="tag">{{ row.status }}</span></p><p>Сана: {{ row.created_at }}</p></div>{% elif oid %}<p>Фармоиш ёфт нашуд.</p>{% endif %}''',oid=oid,row=row)
    return page('Пайгирӣ',body)

@app.route('/uploads/<path:filename>')
def uploaded(filename):
    return send_from_directory(UPLOAD_DIR,filename)

# ===================== ADMIN =====================
@app.route('/admin',methods=['GET','POST'])
def admin_login():
    if session.get('admin'): return redirect('/admin/dashboard')
    if request.method=='POST':
        if hmac.compare_digest(request.form.get('password',''),ADMIN_PASSWORD):
            session.clear(); session['admin']=True; session['csrf']=secrets.token_hex(24); flash('Ба панели админ ворид шудед.'); return redirect('/admin/dashboard')
        flash('Парол нодуруст аст.')
    body='''<div class="hero"><h1>🔐 Admin Login</h1><p class="muted">Воридшавӣ ба идоракунии мағоза</p></div><form method="post" class="form"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><input class="input" type="password" name="password" placeholder="Пароли админ" required><button>Ворид шудан</button></form>'''
    return page('Admin',body)

@app.post('/admin/logout')
@admin_required
def admin_logout():
    session.clear(); return redirect('/')

@app.route('/admin/dashboard')
@admin_required
def dashboard():
    with db() as con:
        products=con.execute('SELECT * FROM products ORDER BY id DESC').fetchall()
        orders=con.execute('SELECT * FROM orders ORDER BY created_at DESC').fetchall()
        revenue=con.execute("SELECT COALESCE(SUM(total),0) s FROM orders WHERE status!='Бекор шуд'").fetchone()['s']
    ps=[p_dict(r) for r in products]
    body=render_template_string('''<h1>⚙️ Панели идоракунӣ</h1><div class="row"><div class="card stat">Маҳсулот<br><strong>{{ ps|length }}</strong></div><div class="card stat">Фармоишҳо<br><strong>{{ orders|length }}</strong></div><div class="card stat">Фурӯш<br><strong>{{ '%.2f'|format(revenue) }}</strong> сомонӣ</div></div><div class="links" style="margin:14px 0"><a href="/admin/product/new">➕ Маҳсулот</a><a href="/admin/settings">🎨 Танзимот</a><a href="/admin/promos">🏷️ Промокод</a><a href="/admin/export">📥 CSV</a><form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="btn2">Баромад</button></form></div><h2>📦 Фармоишҳо</h2><table class="table"><tr><th>Код</th><th>Мизоҷ</th><th>Телефон</th><th>Ҳамагӣ</th><th>Статус</th><th>Идора</th></tr>{% for o in orders %}<tr><td>{{ o.id }}</td><td>{{ o.customer }}</td><td>{{ o.phone }}</td><td>{{ '%.2f'|format(o.total) }}</td><td>{{ o.status }}</td><td><form method="post" action="/admin/order/{{ o.id }}"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><select name="status"><option {% if o.status=='Қабул шуд' %}selected{% endif %}>Қабул шуд</option><option {% if o.status=='Дар роҳ' %}selected{% endif %}>Дар роҳ</option><option {% if o.status=='Расонида шуд' %}selected{% endif %}>Расонида шуд</option><option {% if o.status=='Бекор шуд' %}selected{% endif %}>Бекор шуд</option></select><button>OK</button></form></td></tr>{% endfor %}</table><h2>🛍️ Маҳсулот</h2><table class="table"><tr><th>Ном</th><th>Нарх</th><th>Анбор</th><th>Фаъол</th><th>Идора</th></tr>{% for p in ps %}<tr><td>{{ p.name }}</td><td>{{ p.price }}</td><td>{{ p.stock }}</td><td>{{ 'Бале' if p.active else 'Не' }}</td><td><a href="/admin/product/{{ p.id }}/edit">✏️</a> <form style="display:inline" method="post" action="/admin/product/{{ p.id }}/delete"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><button class="danger">🗑</button></form></td></tr>{% endfor %}</table>''',ps=ps,orders=orders,revenue=revenue)
    return page('Admin Dashboard',body)

@app.route('/admin/product/new',methods=['GET','POST'])
@app.route('/admin/product/<int:pid>/edit',methods=['GET','POST'])
@admin_required
def product_form(pid=None):
    with db() as con: row=con.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone() if pid else None
    if pid and not row: abort(404)
    p=p_dict(row) if row else {'name':'','description':'','price':0,'old_price':0,'category':'Дигар','stock':0,'images':[],'video':'','featured':0,'active':1}
    if request.method=='POST':
        name=request.form.get('name','').strip(); description=request.form.get('description','').strip(); category=request.form.get('category','Дигар').strip() or 'Дигар'
        try: price=max(0,float(request.form.get('price','0'))); old_price=max(0,float(request.form.get('old_price','0'))); stock=max(0,int(request.form.get('stock','0')))
        except ValueError: flash('Нарх ё миқдор нодуруст аст.'); return redirect(request.url)
        images=p['images'][:]
        for f in request.files.getlist('images'):
            fn=save_upload(f,False)
            if fn: images.append(fn)
        video=save_upload(request.files.get('video'),True) or p.get('video','')
        featured=int(bool(request.form.get('featured'))); active=int(bool(request.form.get('active')))
        if not name: flash('Номи маҳсулотро нависед.'); return redirect(request.url)
        with db() as con:
            if pid: con.execute('UPDATE products SET name=?,description=?,price=?,old_price=?,category=?,images=?,video=?,stock=?,featured=?,active=? WHERE id=?',(name,description,price,old_price,category,json.dumps(images),video,stock,featured,active,pid))
            else: con.execute('INSERT INTO products(name,description,price,old_price,category,images,video,stock,featured,active) VALUES(?,?,?,?,?,?,?,?,?,?)',(name,description,price,old_price,category,json.dumps(images),video,stock,featured,active))
        flash('Маҳсулот бомуваффақият нигоҳ дошта шуд.'); return redirect('/admin/dashboard')
    body=render_template_string('''<div class="hero"><h1>{{ '✏️ Таҳрири маҳсулот' if pid else '➕ Маҳсулоти нав' }}</h1></div><form method="post" enctype="multipart/form-data" class="form"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><input class="input" name="name" placeholder="Номи маҳсулот" value="{{ p.name }}" required><textarea class="input" name="description" rows="4" placeholder="Тавсифи маҳсулот">{{ p.description }}</textarea><input class="input" name="category" placeholder="Категория" value="{{ p.category }}"><input class="input" type="number" step="0.01" min="0" name="price" placeholder="Нарх" value="{{ p.price }}" required><input class="input" type="number" step="0.01" min="0" name="old_price" placeholder="Нархи пешина" value="{{ p.old_price }}"><input class="input" type="number" min="0" name="stock" placeholder="Миқдор" value="{{ p.stock }}"><label>🖼️ Расмҳо (якчандто интихоб кунед)<input class="input" type="file" name="images" multiple accept="image/png,image/jpeg,image/webp,image/gif"></label>{% if p.images %}<div class="row">{% for im in p.images %}<img class="thumb" src="/uploads/{{ im }}">{% endfor %}</div>{% endif %}<label>🎥 Видео маҳсулот<input class="input" type="file" name="video" accept="video/mp4,video/webm"></label><label><input type="checkbox" name="featured" {% if p.featured %}checked{% endif %}> ⭐ Тавсияшаванда</label><label><input type="checkbox" name="active" {% if p.active %}checked{% endif %}> 👁️ Дар сайт нишон додан</label><button>💾 Сабт</button></form><p><a class="btn btn2" href="/admin/dashboard">← Бозгашт</a></p>''',p=p,pid=pid)
    return page('Маҳсулот',body)

@app.post('/admin/product/<int:pid>/delete')
@admin_required
def product_delete(pid):
    with db() as con: con.execute('DELETE FROM products WHERE id=?',(pid,))
    flash('Маҳсулот нест карда шуд.'); return redirect('/admin/dashboard')

@app.post('/admin/order/<oid>')
@admin_required
def order_status(oid):
    status=request.form.get('status','Қабул шуд'); allowed={'Қабул шуд','Дар роҳ','Расонида шуд','Бекор шуд'}
    if status not in allowed: abort(400)
    with db() as con:
        old=con.execute('SELECT status,items FROM orders WHERE id=?',(oid,)).fetchone()
        if not old: abort(404)
        old_status=old['status']; order_items=json.loads(old['items'] or '[]')
        if status=='Бекор шуд' and old_status!='Бекор шуд':
            for it in order_items:
                if it.get('product_id'): con.execute('UPDATE products SET stock=stock+? WHERE id=?',(int(it.get('qty',0)),int(it['product_id'])))
        elif old_status=='Бекор шуд' and status!='Бекор шуд':
            for it in order_items:
                if it.get('product_id'):
                    current=con.execute('SELECT stock FROM products WHERE id=?',(int(it['product_id']),)).fetchone()
                    if not current or current['stock']<int(it.get('qty',0)):
                        flash('Анбор барои барқарор кардани фармоиш кофӣ нест.'); return redirect('/admin/dashboard')
            for it in order_items:
                if it.get('product_id'): con.execute('UPDATE products SET stock=stock-? WHERE id=?',(int(it.get('qty',0)),int(it['product_id'])))
        con.execute('UPDATE orders SET status=? WHERE id=?',(status,oid))
    flash('Статуси фармоиш иваз шуд.'); return redirect('/admin/dashboard')

@app.route('/admin/settings',methods=['GET','POST'])
@admin_required
def settings():
    if request.method=='POST':
        set_setting('banner',request.form.get('banner','').strip())
        set_setting('whatsapp',request.form.get('whatsapp','').strip().replace('+','').replace(' ',''))
        video=save_upload(request.files.get('video'),True) or setting('video_ad','')
        set_setting('video_ad',video)
        flash('Танзимот сабт шуд.'); return redirect('/admin/settings')
    wa=setting('whatsapp','').strip() or WHATSAPP_NUMBER
    body=render_template_string('''<h1>🎨 Танзимоти сайт</h1><form method="post" enctype="multipart/form-data" class="form"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><label>Текст баннер<input class="input" name="banner" value="{{ banner }}"></label><label>WhatsApp рақам (992... бе +)<input class="input" name="whatsapp" value="{{ wa }}" placeholder="992XXXXXXXXX"></label><label>Видео-реклама<input class="input" type="file" name="video" accept="video/mp4,video/webm"></label><button>💾 Сабт кардан</button></form><p><a class="btn btn2" href="/admin/dashboard">← Бозгашт</a></p>''',banner=setting('banner',''),wa=wa)
    return page('Танзимот',body)

@app.route('/admin/promos',methods=['GET','POST'])
@admin_required
def promos():
    if request.method=='POST':
        code=request.form.get('code','').strip().upper()
        try: discount=max(0,float(request.form.get('discount','0')))
        except ValueError: discount=0
        if code and discount>0:
            with db() as con: con.execute('INSERT OR REPLACE INTO promos(code,discount,active) VALUES(?,?,1)',(code,discount))
            flash('Промокод сабт шуд.')
        else: flash('Код ва скидкаи дурустро ворид кунед.')
        return redirect('/admin/promos')
    with db() as con: rows=con.execute('SELECT * FROM promos ORDER BY code').fetchall()
    body=render_template_string('''<h1>🏷️ Промокодҳо</h1><form class="row" method="post"><input type="hidden" name="csrf" value="{{ csrf_token() }}"><input class="input" name="code" placeholder="Код, мисол SALE10" required><input class="input" name="discount" type="number" min="0.01" step="0.01" placeholder="Скидка (сомонӣ)" required><button>➕ Илова</button></form><table class="table"><tr><th>Код</th><th>Скидка</th><th>Фаъол</th></tr>{% for r in rows %}<tr><td>{{ r.code }}</td><td>{{ r.discount }} сомонӣ</td><td>{{ 'Бале' if r.active else 'Не' }}</td></tr>{% endfor %}</table><p><a class="btn btn2" href="/admin/dashboard">Бозгашт</a></p>''',rows=rows)
    return page('Promos',body)

@app.route('/admin/export')
@admin_required
def export_csv():
    with db() as con: rows=con.execute('SELECT id,customer,phone,address,items,subtotal,discount,delivery,total,promo,status,created_at FROM orders ORDER BY created_at DESC').fetchall()
    out=io.StringIO(); writer=csv.writer(out); writer.writerow(['id','customer','phone','address','items','subtotal','discount','delivery','total','promo','status','created_at'])
    for row in rows: writer.writerow(list(row))
    return Response('\ufeff'+out.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=nexvora_orders.csv'})

@app.errorhandler(404)
def not_found(_):
    return page('Ёфт нашуд','<div class="empty"><h1>404</h1><p>Саҳифа ё маҳсулот ёфт нашуд.</p><a class="btn" href="/">Ба саҳифаи асосӣ</a></div>'),404

if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')),debug=False)
