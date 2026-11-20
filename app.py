from flask import Flask, request, redirect, url_for, session, flash, render_template_string, send_from_directory, abort
import sqlite3, os, uuid, secrets, json, re
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from datetime import datetime
from urllib.parse import quote

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['MAX_CONTENT_LENGTH'] = 120 * 1024 * 1024
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'Jurabekjon123.')
WHATSAPP_NUMBER = os.environ.get('WHATSAPP_NUMBER', '992111301210')
DELIVERY_FEE = int(os.environ.get('DELIVERY_FEE', '0'))
DATA = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(__file__), 'data'))
UPLOADS = os.path.join(DATA, 'uploads')
DB = os.path.join(DATA, 'nexvora.db')
os.makedirs(UPLOADS, exist_ok=True)

# If upgrading from V4, copy old static uploads into persistent storage once.
OLD_UPLOADS = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
if os.path.isdir(OLD_UPLOADS):
    for fn in os.listdir(OLD_UPLOADS):
        src, dst = os.path.join(OLD_UPLOADS, fn), os.path.join(UPLOADS, fn)
        if os.path.isfile(src) and not os.path.exists(dst):
            try:
                import shutil; shutil.copy2(src, dst)
            except Exception: pass

ALLOWED_IMAGES = {'jpg','jpeg','png','webp'}
ALLOWED_VIDEOS = {'mp4','webm'}
def ext_ok(name, allowed): return '.' in name and name.rsplit('.',1)[1].lower() in allowed
def media_save(file, allowed):
    if not file or not file.filename: return ''
    if not ext_ok(file.filename, allowed): return ''
    ext = file.filename.rsplit('.',1)[1].lower()
    name = uuid.uuid4().hex + '.' + ext
    file.save(os.path.join(UPLOADS, name))
    return name

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init_db():
    c=db(); q=c.cursor()
    q.execute('''CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,price REAL NOT NULL,image TEXT DEFAULT '',category TEXT DEFAULT 'Дигар',description TEXT DEFAULT '',stock INTEGER DEFAULT 0,sale_price REAL DEFAULT 0,featured INTEGER DEFAULT 0,active INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP,video TEXT DEFAULT '')''')
    q.execute('''CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_name TEXT,phone TEXT,address TEXT,note TEXT,items_json TEXT,total REAL,status TEXT DEFAULT 'Нав',created_at TEXT DEFAULT CURRENT_TIMESTAMP,discount REAL DEFAULT 0,promo TEXT DEFAULT '',delivery_fee REAL DEFAULT 0,stock_restored INTEGER DEFAULT 0)''')
    q.execute('CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY,discount REAL,active INTEGER DEFAULT 1)')
    q.execute('CREATE TABLE IF NOT EXISTS product_images(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id INTEGER,filename TEXT,position INTEGER DEFAULT 0)')
    q.execute('CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id INTEGER,name TEXT,rating INTEGER,comment TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    q.execute('CREATE TABLE IF NOT EXISTS hits(id INTEGER PRIMARY KEY AUTOINCREMENT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    # V4-compatible migrations
    for table,col,definition in [('products','video',"TEXT DEFAULT ''"),('orders','discount','REAL DEFAULT 0'),('orders','promo',"TEXT DEFAULT ''"),('orders','delivery_fee','REAL DEFAULT 0'),('orders','stock_restored','INTEGER DEFAULT 0')]:
        cols=[r['name'] for r in q.execute(f'PRAGMA table_info({table})')]
        if col not in cols: q.execute(f'ALTER TABLE {table} ADD COLUMN {col} {definition}')
    # import old single image into gallery when gallery is empty for that product
    for p in q.execute('SELECT id,image FROM products').fetchall():
        if p['image'] and not q.execute('SELECT 1 FROM product_images WHERE product_id=? LIMIT 1',(p['id'],)).fetchone():
            q.execute('INSERT INTO product_images(product_id,filename,position) VALUES(?,?,0)',(p['id'],p['image']))
    c.commit(); c.close()
init_db()

@app.before_request
def csrf():
    if 'csrf' not in session: session['csrf']=secrets.token_hex(16)
def token_field(): return f'<input type="hidden" name="csrf" value="{session.get("csrf","")}">' 
def valid_csrf(): return secrets.compare_digest(request.form.get('csrf',''),session.get('csrf',''))

@app.before_request
def hit():
    if request.endpoint=='home':
        try:
            c=db(); c.execute('INSERT INTO hits DEFAULT VALUES'); c.commit(); c.close()
        except: pass

def admin_required(f):
    @wraps(f)
    def inner(*a,**kw):
        if not session.get('admin'): return redirect(url_for('login'))
        return f(*a,**kw)
    return inner

def money(v): return f'{float(v or 0):,.0f}'
def get_images(pid):
    c=db(); rows=c.execute('SELECT filename FROM product_images WHERE product_id=? ORDER BY position,id',(pid,)).fetchall(); c.close()
    return [r['filename'] for r in rows]
def get_product(pid):
    c=db(); p=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone(); c.close(); return p

def current_price(p): return float(p['sale_price'] or p['price'])
def cart_items():
    cart=session.get('cart',{}); items=[]; total=0
    c=db()
    for pid,qty in cart.items():
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
        if p and int(qty)>0:
            price=current_price(p); sub=price*int(qty); total+=sub
            items.append({'product':p,'qty':int(qty),'price':price,'subtotal':sub})
    c.close(); return items,total

BASE_STYLE='''<style>*{box-sizing:border-box}body{margin:0;background:#090909;color:#eee;font-family:Arial,sans-serif}a{color:#e6c15a;text-decoration:none}header{padding:16px 5%;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #3b321c;gap:12px;flex-wrap:wrap}.brand{font-size:24px;font-weight:900;color:#e6c15a;letter-spacing:2px}.nav{display:flex;gap:14px;flex-wrap:wrap}main{max-width:1150px;margin:auto;padding:22px 4%}.gold{color:#e6c15a}input,textarea,select,button{padding:11px;border-radius:8px;border:1px solid #55451e;background:#151515;color:#eee;max-width:100%}button,.btn{background:#c7a347;color:#111;font-weight:bold;border:0;padding:11px 15px;border-radius:8px;cursor:pointer;display:inline-block}.card{border:1px solid #332b19;background:#111;border-radius:13px;padding:14px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:16px}.product img{width:100%;height:220px;object-fit:cover;border-radius:9px}.price{font-size:19px;color:#e6c15a;font-weight:bold}.muted{color:#aaa;font-size:13px}.formrow{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.formrow>*{flex:1}.wide{width:100%}table{width:100%;border-collapse:collapse}td,th{padding:9px;border-bottom:1px solid #333;text-align:left}video{width:100%;max-height:400px;border-radius:10px}.gallery{display:flex;gap:8px;overflow:auto}.gallery img{width:110px;height:100px;object-fit:cover;border-radius:8px}.alert{padding:10px;background:#332715;border-radius:8px;margin:10px 0}.footer{padding:25px;text-align:center;color:#999}@media(max-width:600px){header{padding:13px}.brand{font-size:20px}main{padding:15px 3%}.grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}.product img{height:150px}.card{padding:10px}.nav{font-size:13px;gap:9px}input,select,textarea{width:100%}}''' 

def page(title,body):
    return render_template_string('''<!doctype html><html lang="tg"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{title}} | NEXVORA</title>'''+BASE_STYLE+'''<header><a class="brand" href="{{url_for('home')}}">NEXVORA</a><nav class="nav"><a href="{{url_for('home')}}">Магазин</a><a href="{{url_for('cart')}}">🛒 Сабад</a><a href="{{url_for('favorites')}}">❤️ Дӯстдошта</a><a href="{{url_for('track')}}">📦 Tracking</a><a href="{{url_for('admin')}}">Админ</a></nav></header><main>{% with messages=get_flashed_messages() %}{% if messages %}{% for msg in messages %}<div class="alert">{{msg}}</div>{% endfor %}{% endif %}{% endwith %}{{body|safe}}</main><div class="footer">NEXVORA STORE • Black & Gold</div></html>''',title=title,body=body)

@app.route('/media/<path:filename>')
def media(filename):
    return send_from_directory(UPLOADS,filename)

@app.route('/')
def home():
    search=request.args.get('q','').strip(); cat=request.args.get('cat',''); lo=request.args.get('min',''); hi=request.args.get('max',''); sort=request.args.get('sort','new')
    c=db(); sql='SELECT * FROM products WHERE active=1'; args=[]
    if search: sql+=' AND (name LIKE ? OR description LIKE ?)'; args += [f'%{search}%',f'%{search}%']
    if cat: sql+=' AND category=?'; args.append(cat)
    try:
        if lo.strip(): sql+=' AND COALESCE(NULLIF(sale_price,0),price)>=?'; args.append(float(lo))
        if hi.strip(): sql+=' AND COALESCE(NULLIF(sale_price,0),price)<=?'; args.append(float(hi))
    except ValueError: pass
    sql+= ' ORDER BY price ASC' if sort=='cheap' else (' ORDER BY price DESC' if sort=='expensive' else ' ORDER BY id DESC')
    products=c.execute(sql,args).fetchall(); cats=[r['category'] for r in c.execute('SELECT DISTINCT category FROM products WHERE active=1 ORDER BY category')]; c.close()
    cards=''.join(f'''<div class="card product"><a href="{url_for('product',pid=p['id'])}"><img src="{url_for('media',filename=(get_images(p['id']) or [p['image']])[0])}" onerror="this.style.display='none'"><h3>{p['name']}</h3></a><p class="price">{money(current_price(p))} сомонӣ</p><p class="muted">{p['category']} • Дар анбор: {p['stock']}</p><form method="post" action="{url_for('cart_action',action='add',pid=p['id'])}">{token_field()}<button>Ба сабад</button></form><form method="post" action="{url_for('favorite_toggle',pid=p['id'])}">{token_field()}<button>♡ Дӯстдошта</button></form></div>''' for p in products)
    body=f'''<h1 class="gold">NEXVORA STORE</h1><p>Магазини онлайн • Сифат ва услуб</p><form><div class="formrow"><input name="q" placeholder="Ҷустуҷӯи маҳсулот..." value="{search}"><select name="cat"><option value="">Ҳама категория</option>{''.join(f'<option {"selected" if cat==x else ""}>{x}</option>' for x in cats)}</select><input name="min" type="number" placeholder="Аз нарх" value="{lo}"><input name="max" type="number" placeholder="То нарх" value="{hi}"><select name="sort"><option value="new">Навтарин</option><option value="cheap" {'selected' if sort=='cheap' else ''}>Арзон</option><option value="expensive" {'selected' if sort=='expensive' else ''}>Қимат</option></select><button>Ҷустуҷӯ</button></div></form><div class="grid">{cards or '<p>Маҳсулот ёфт нашуд.</p>'}</div>'''
    return page('Магазин',body)

@app.route('/product/<int:pid>',methods=['GET','POST'])
def product(pid):
    p=get_product(pid)
    if not p or not p['active']: abort(404)
    if request.method=='POST':
        if not valid_csrf(): abort(400)
        rating=max(1,min(5,int(request.form.get('rating',5)))); name=request.form.get('name','Меҳмон')[:80]; comment=request.form.get('comment','')[:1000]
        c=db(); c.execute('INSERT INTO reviews(product_id,name,rating,comment) VALUES(?,?,?,?)',(pid,name,rating,comment)); c.commit(); c.close(); flash('Шарҳи шумо илова шуд!'); return redirect(url_for('product',pid=pid))
    c=db(); reviews=c.execute('SELECT * FROM reviews WHERE product_id=? ORDER BY id DESC',(pid,)).fetchall(); avg=c.execute('SELECT AVG(rating) a FROM reviews WHERE product_id=?',(pid,)).fetchone()['a']; c.close()
    imgs=get_images(pid) or ([p['image']] if p['image'] else [])
    gallery=''.join(f'<img src="{url_for("media",filename=im)}">' for im in imgs)
    video=f'<video controls src="{url_for("media",filename=p["video"])}"></video>' if p['video'] else ''
    reviews_html=''.join(f'<div class="card"><b>{r["name"]}</b> • {"⭐"*r["rating"]}<p>{r["comment"]}</p></div>' for r in reviews)
    body=f'''<a href="/">← Бозгашт</a><h1>{p['name']}</h1><div class="gallery">{gallery}</div>{video}<p class="price">{money(current_price(p))} сомонӣ</p><p>Категория: {p['category']} • Миқдор: {p['stock']}</p><p>{p['description']}</p><form method="post" action="{url_for('cart_action',action='add',pid=pid)}">{token_field()}<button>Ба сабад илова кардан</button></form><p>⭐ Рейтинг: {round(avg or 0,1)} / 5 ({len(reviews)} шарҳ)</p><h3>Шарҳ нависед</h3><form method="post"><input name="csrf" value="{session.get('csrf')}" type="hidden"><div class="formrow"><input name="name" placeholder="Номи шумо"><select name="rating"><option>5</option><option>4</option><option>3</option><option>2</option><option>1</option></select></div><textarea class="wide" name="comment" placeholder="Шарҳ..."></textarea><button>Фиристодан</button></form><h3>Шарҳҳо</h3>{reviews_html}'''
    return page(p['name'],body)

@app.route('/favorite/toggle/<int:pid>',methods=['POST'])
def favorite_toggle(pid):
    if not valid_csrf(): abort(400)
    fav=session.get('favorites',[]); key=str(pid)
    if key in fav: fav.remove(key)
    else: fav.append(key)
    session['favorites']=fav; return redirect(request.referrer or url_for('home'))
@app.route('/favorites')
def favorites():
    fav=session.get('favorites',[]); c=db(); ps=[]
    for key in fav:
        p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(key,)).fetchone()
        if p: ps.append(p)
    c.close(); cards=''.join(f'<div class="card product"><a href="{url_for("product",pid=p["id"])}"><img src="{url_for("media",filename=(get_images(p["id"]) or [p["image"]])[0])}"><h3>{p["name"]}</h3></a><p class="price">{money(current_price(p))} сомонӣ</p></div>' for p in ps)
    return page('Дӯстдошта',f'<h1>❤️ Дӯстдошта</h1><div class="grid">{cards or "Ҳоло маҳсулот нест."}</div>')

@app.route('/cart')
def cart():
    items,total=cart_items(); discount=session.get('discount',0); promo=session.get('promo',''); fee=DELIVERY_FEE if items else 0
    rows=''.join(f'''<div class="card"><b>{it['product']['name']}</b> • {it['qty']} дона • {money(it['subtotal'])} сомонӣ <form class="formrow" method="post" action="{url_for('cart_action',action='remove',pid=it['product']['id'])}">{token_field()}<button>Хориҷ</button></form></div>''' for it in items)
    body=f'''<h1>🛒 Сабад</h1>{rows or 'Сабад холӣ аст.'}<p>Маблағ: {money(total)} сомонӣ</p><p>Тахфиф: {money(discount)} сомонӣ</p><p>Доставка: {money(fee)} сомонӣ</p><h2 class="gold">Ҳамагӣ: {money(max(0,total-discount)+fee)} сомонӣ</h2><form method="post" action="{url_for('apply_promo')}">{token_field()}<div class="formrow"><input name="code" placeholder="Промокод" value="{promo}"><button>Apply</button></div></form><a class="btn" href="/checkout">Оформить заказ</a>'''
    return page('Сабад',body)
@app.route('/cart/<action>/<int:pid>',methods=['POST'])
def cart_action(action,pid):
    if not valid_csrf(): abort(400)
    p=get_product(pid); cart=session.get('cart',{}); k=str(pid)
    if p and action=='add': cart[k]=min(int(cart.get(k,0))+1,int(p['stock']))
    elif action=='remove': cart.pop(k,None)
    session['cart']=cart; return redirect(request.referrer or url_for('cart'))
@app.route('/cart/promo',methods=['POST'])
def apply_promo():
    if not valid_csrf(): abort(400)
    c=db(); row=c.execute('SELECT * FROM promos WHERE code=? AND active=1',(request.form.get('code','').upper(),)).fetchone(); c.close()
    if row:
        _,total=cart_items(); discount=min(total,float(row['discount'])); session['discount']=discount; session['promo']=row['code']; flash('Промокод қабул шуд!')
    else: session['discount']=0; session['promo']=''; flash('Промокод нодуруст аст.')
    return redirect(url_for('cart'))

@app.route('/checkout',methods=['GET','POST'])
def checkout():
    items,total=cart_items()
    if not items: return redirect(url_for('cart'))
    if request.method=='POST':
        if not valid_csrf(): abort(400)
        name=request.form.get('name','').strip(); phone=request.form.get('phone','').strip(); address=request.form.get('address','').strip(); note=request.form.get('note','')[:500]
        if not name or not phone or not address: flash('Ном, телефон ва адресро пур кунед.'); return redirect(url_for('checkout'))
        c=db()
        for it in items:
            current=c.execute('SELECT stock FROM products WHERE id=?',(it['product']['id'],)).fetchone()
            if not current or current['stock']<it['qty']: c.close(); flash('Миқдори маҳсулот кам аст.'); return redirect(url_for('cart'))
        for it in items: c.execute('UPDATE products SET stock=stock-? WHERE id=?',(it['qty'],it['product']['id']))
        disc=min(total,float(session.get('discount',0))); fee=DELIVERY_FEE; final=max(0,total-disc)+fee
        items_json=json.dumps([{'id':it['product']['id'],'name':it['product']['name'],'qty':it['qty'],'price':it['price']} for it in items],ensure_ascii=False)
        c.execute('INSERT INTO orders(customer_name,phone,address,note,items_json,total,status,discount,promo,delivery_fee) VALUES(?,?,?,?,?,?,?,?,?,?)',(name,phone,address,note,items_json,final,'Нав',disc,session.get('promo',''),fee))
        oid=c.execute('SELECT last_insert_rowid()').fetchone()[0]; c.commit(); c.close(); session['cart']={}; session['discount']=0; session['promo']=''
        wa=f"Салом! Заказ #{oid}. Ном: {name}. Телефон: {phone}. Адрес: {address}. Маблағ: {money(final)} сомонӣ."
        return redirect(url_for('done',oid=oid,wa=wa))
    body=f'''<h1>Оформление заказа</h1><p>Ҳамагӣ: {money(total-min(total,session.get('discount',0))+DELIVERY_FEE)} сомонӣ (доставка {DELIVERY_FEE})</p><form method="post">{token_field()}<input class="wide" name="name" placeholder="Ном" required><input class="wide" name="phone" placeholder="Телефон" required><input class="wide" name="address" placeholder="Адрес" required><textarea class="wide" name="note" placeholder="Эзоҳ"></textarea><button>Фармоиш додан</button></form>'''
    return page('Checkout',body)
@app.route('/done/<int:oid>')
def done(oid):
    wa_text=request.args.get('wa', f'Order #{oid}')
    wa_link=f'https://wa.me/{WHATSAPP_NUMBER}?text={quote(wa_text)}'
    return page('Фармоиш',f'<h1 class="gold">Фармоиш қабул шуд!</h1><p>Рақами фармоиш: #{oid}</p><p>Ин рақамро нигоҳ доред барои tracking.</p><a class="btn" href="{url_for("track",oid=oid)}">Пайгирии фармоиш</a><p><a href="{wa_link}" target="_blank">Фиристодани маълумоти фармоиш ба WhatsApp</a></p>')
@app.route('/track',methods=['GET','POST'])
def track():
    oid=request.args.get('oid') or request.form.get('oid','')
    order=None
    if oid:
        try: c=db(); order=c.execute('SELECT * FROM orders WHERE id=?',(int(oid),)).fetchone(); c.close()
        except: order=None
    body=f'''<h1>📦 Tracking</h1><form method="post"><div class="formrow"><input name="oid" placeholder="Рақами фармоиш" value="{oid}"><button>Пайгирӣ</button></div></form>'''
    if order: body+=f'<div class="card"><h2>Фармоиш #{order["id"]}</h2><p>Ном: {order["customer_name"]}</p><p>Ҳолат: <b class="gold">{order["status"]}</b></p><p>Ҳамагӣ: {money(order["total"])} сомонӣ</p><p>Сана: {order["created_at"]}</p></div>'
    elif oid: body+='<p>Фармоиш ёфт нашуд.</p>'
    return page('Tracking',body)

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        if not valid_csrf(): abort(400)
        pw=request.form.get('password','')
        stored=os.environ.get('ADMIN_PASSWORD_HASH')
        ok=check_password_hash(stored,pw) if stored else secrets.compare_digest(pw,ADMIN_PASSWORD)
        if ok: session['admin']=True; return redirect(url_for('admin'))
        flash('Парол нодуруст аст.')
    return page('Админ',f'<h1>Admin login</h1><form method="post">{token_field()}<input type="password" name="password" placeholder="Парол"><button>Ворид шудан</button></form>')
@app.route('/logout')
def logout(): session.pop('admin',None); return redirect('/')

@app.route('/admin')
@admin_required
def admin():
    c=db(); products=c.execute('SELECT * FROM products ORDER BY id DESC').fetchall(); orders=c.execute('SELECT * FROM orders ORDER BY id DESC').fetchall(); new=c.execute("SELECT COUNT(*) n FROM orders WHERE status='Нав'").fetchone()['n']; visits=c.execute('SELECT COUNT(*) n FROM hits').fetchone()['n']; revenue=c.execute("SELECT COALESCE(SUM(total),0) s FROM orders WHERE status!='Бекор'").fetchone()['s']; c.close()
    rows=''.join(f'<tr><td>{p["id"]}</td><td>{p["name"]}</td><td>{money(p["price"])} сом</td><td>{p["stock"]}</td><td><a href="{url_for("edit_product",pid=p["id"])}">Edit</a> | <form style="display:inline" method="post" action="{url_for("delete_product",pid=p["id"])}">{token_field()}<button>Delete</button></form></td></tr>' for p in products)
    order_rows=''.join(f'<tr><td>#{o["id"]}</td><td>{o["customer_name"]}</td><td>{money(o["total"])} сом</td><td>{o["status"]}</td><td><a href="{url_for("order_admin",oid=o["id"])}">Дидан</a></td></tr>' for o in orders)
    body=f'''<h1>Админ панел <a href="/logout">Logout</a></h1><div class="grid"><div class="card">📦 Маҳсулот: {len(products)}</div><div class="card">🔔 Фармоиши нав: <b class="gold">{new}</b></div><div class="card">💰 Даромад: {money(revenue)} сомонӣ</div><div class="card">👁️ Боздид: {visits}</div></div><h2>Иловаи маҳсулот</h2><form method="post" action="/admin/add" enctype="multipart/form-data">{token_field()}<div class="formrow"><input name="name" placeholder="Ном" required><input name="price" type="number" step="0.01" placeholder="Нарх" required><input name="sale_price" type="number" placeholder="Нархи тахфиф"></div><div class="formrow"><input name="category" placeholder="Категория"><input name="stock" type="number" placeholder="Миқдор"><input name="description" placeholder="Тавсиф"></div><label>Суратҳо (якчанд):</label><input type="file" name="images" multiple accept="image/*"><label>Видео:</label><input type="file" name="video" accept="video/mp4,video/webm"><button>Илова кардан</button></form><h2>Маҳсулотҳо</h2><table><tr><th>ID</th><th>Ном</th><th>Нарх</th><th>Миқдор</th><th>Action</th></tr>{rows}</table><h2>Фармоишҳо</h2><table><tr><th>ID</th><th>Ном</th><th>Маблағ</th><th>Ҳолат</th><th></th></tr>{order_rows}</table><h2>Промокод</h2><form method="post" action="/admin/promo">{token_field()}<div class="formrow"><input name="code" placeholder="Код"><input name="discount" type="number" placeholder="Тахфиф (сомонӣ)"><button>Создан</button></div></form>'''
    return page('Админ',body)

@app.route('/admin/add',methods=['POST'])
@admin_required
def add_product():
    if not valid_csrf(): abort(400)
    name=request.form.get('name','').strip(); price=float(request.form.get('price',0) or 0); sale=float(request.form.get('sale_price',0) or 0); cat=request.form.get('category','Дигар'); stock=int(request.form.get('stock',0) or 0); desc=request.form.get('description','')
    imgs=[media_save(f,ALLOWED_IMAGES) for f in request.files.getlist('images')]; imgs=[x for x in imgs if x]
    vid=media_save(request.files.get('video'),ALLOWED_VIDEOS)
    c=db(); c.execute('INSERT INTO products(name,price,image,category,description,stock,sale_price,video) VALUES(?,?,?,?,?,?,?,?)',(name,price,imgs[0] if imgs else '',cat,desc,stock,sale,vid)); pid=c.execute('SELECT last_insert_rowid()').fetchone()[0]
    for i,im in enumerate(imgs): c.execute('INSERT INTO product_images(product_id,filename,position) VALUES(?,?,?)',(pid,im,i))
    c.commit(); c.close(); flash('Маҳсулот илова шуд!'); return redirect('/admin')
@app.route('/admin/edit/<int:pid>',methods=['GET','POST'])
@admin_required
def edit_product(pid):
    p=get_product(pid)
    if not p: abort(404)
    if request.method=='POST':
        if not valid_csrf(): abort(400)
        name=request.form.get('name'); price=float(request.form.get('price',0)); stock=int(request.form.get('stock',0)); cat=request.form.get('category','Дигар'); desc=request.form.get('description',''); sale=float(request.form.get('sale_price',0) or 0)
        imgs=[media_save(f,ALLOWED_IMAGES) for f in request.files.getlist('images')]; imgs=[x for x in imgs if x]; vid=media_save(request.files.get('video'),ALLOWED_VIDEOS)
        c=db(); c.execute('UPDATE products SET name=?,price=?,stock=?,category=?,description=?,sale_price=? WHERE id=?',(name,price,stock,cat,desc,sale,pid))
        if imgs:
            for im in imgs: c.execute('INSERT INTO product_images(product_id,filename,position) VALUES(?,?,?)',(pid,im,99))
            c.execute('UPDATE products SET image=? WHERE id=?',(imgs[0],pid))
        if vid: c.execute('UPDATE products SET video=? WHERE id=?',(vid,pid))
        c.commit(); c.close(); return redirect('/admin')
    return page('Edit',f'<h1>Таҳрири маҳсулот</h1><form method="post" enctype="multipart/form-data">{token_field()}<input name="name" value="{p["name"]}" placeholder="Ном"><input name="price" type="number" value="{p["price"]}"><input name="sale_price" type="number" value="{p["sale_price"]}"><input name="stock" type="number" value="{p["stock"]}"><input name="category" value="{p["category"]}"><textarea name="description">{p["description"]}</textarea><label>Иловаи суратҳо</label><input type="file" name="images" multiple><label>Иваз кардани видео</label><input type="file" name="video"><button>Сохранить</button></form>')
@app.route('/admin/delete/<int:pid>',methods=['POST'])
@admin_required
def delete_product(pid):
    if not valid_csrf(): abort(400)
    c=db(); c.execute('DELETE FROM products WHERE id=?',(pid,)); c.execute('DELETE FROM product_images WHERE product_id=?',(pid,)); c.execute('DELETE FROM reviews WHERE product_id=?',(pid,)); c.commit(); c.close(); return redirect('/admin')
@app.route('/admin/promo',methods=['POST'])
@admin_required
def promo_add():
    if not valid_csrf(): abort(400)
    code=request.form.get('code','').strip().upper(); discount=float(request.form.get('discount',0) or 0)
    if code and discount>0:
        c=db(); c.execute('INSERT OR REPLACE INTO promos(code,discount,active) VALUES(?,?,1)',(code,discount)); c.commit(); c.close()
    return redirect('/admin')
@app.route('/admin/order/<int:oid>')
@admin_required
def order_admin(oid):
    c=db(); o=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone(); c.close()
    if not o: abort(404)
    return page('Фармоиш',f'<h1>Заказ #{oid}</h1><p>{o["customer_name"]} • {o["phone"]} • {o["address"]}</p><p>Маблағ: {money(o["total"])} сомонӣ</p><pre>{o["items_json"]}</pre><form method="post" action="{url_for("order_status",oid=oid)}">{token_field()}<select name="status"><option>Нав</option><option>Қабул шуд</option><option>Дар роҳ</option><option>Расонида шуд</option><option>Бекор</option></select><button>Тағйир додан</button></form>')
@app.route('/admin/order/<int:oid>/status',methods=['POST'])
@admin_required
def order_status(oid):
    if not valid_csrf(): abort(400)
    status=request.form.get('status','Нав'); c=db(); o=c.execute('SELECT * FROM orders WHERE id=?',(oid,)).fetchone()
    if o:
        if status=='Бекор' and not o['stock_restored']:
            try:
                for it in json.loads(o['items_json']): c.execute('UPDATE products SET stock=stock+? WHERE id=?',(int(it.get('qty',0)),int(it.get('id',0))))
            except: pass
            c.execute('UPDATE orders SET stock_restored=1,status=? WHERE id=?',(status,oid))
        elif o['stock_restored'] and status!='Бекор':
            try:
                for it in json.loads(o['items_json']): c.execute('UPDATE products SET stock=MAX(0,stock-?) WHERE id=?',(int(it.get('qty',0)),int(it.get('id',0))))
            except: pass
            c.execute('UPDATE orders SET stock_restored=0,status=? WHERE id=?',(status,oid))
        else: c.execute('UPDATE orders SET status=? WHERE id=?',(status,oid))
    c.commit(); c.close(); return redirect(url_for('order_admin',oid=oid))

@app.route('/admin/export.csv')
@admin_required
def export_csv():
    import csv, io
    c=db(); rows=c.execute('SELECT * FROM orders ORDER BY id DESC').fetchall(); c.close()
    output=io.StringIO(); w=csv.writer(output); w.writerow(['id','name','phone','address','total','status','created']);
    for r in rows: w.writerow([r['id'],r['customer_name'],r['phone'],r['address'],r['total'],r['status'],r['created_at']])
    from flask import Response
    return Response(output.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename=orders.csv'})

@app.errorhandler(413)
def too_large(e): return page('Хатогӣ','Файл калон аст. Лутфан файли хурдтар интихоб кунед.'),413
if __name__=='__main__':
    app.run(host='0.0.0.0',port=int(os.environ.get('PORT',5000)),debug=False)