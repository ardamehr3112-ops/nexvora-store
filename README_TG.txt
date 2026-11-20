# NEXVORA STORE — дастур (Тоҷикӣ)

## Файлҳо
- `app.py` — коди асосии мағоза.
- `requirements.txt` — китобхонаҳои лозим.
- `README_TG.txt` — ин дастур.

## Иҷро дар Pydroid 3
1. Ҳар се файлро ба як папка гузоред.
2. Дар Terminal иҷро кунед: `pip install -r requirements.txt`
3. Сайтро оғоз кунед: `python app.py`
4. Дар браузер кушоед: `http://127.0.0.1:5000`

## Deploy дар Render
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app`
- Environment variables: `SECRET_KEY` (калиди махфии дароз), `COOKIE_SECURE=1`, `ADMIN_PASSWORD` (пароли қавии админ), `WHATSAPP_NUMBER` (рақам бо коди кишвар, танҳо рақамҳо), `DELIVERY_FEE` (ихтиёрӣ).
- Барои сохтани SECRET_KEY: дар Python `import secrets; print(secrets.token_hex(32))` иҷро кунед ва натиҷаро ба Render Environment гузоред. Калидро махфӣ нигоҳ доред.
- Агар база ва upload-ҳо бояд баъди redeploy нигоҳ дошта шаванд, Render Disk-и доимӣ пайваст кунед ва `DATA_DIR`-ро ба роҳи Disk гузоред.

## CSRF / Bad Request
Ҳамаи POST-form-ҳо токени CSRF доранд. Агар кукиҳои браузер кӯҳна бошанд, кукиҳои сайти Render-ро тоза кунед ва сайтро аз нав кушоед. `SECRET_KEY`-ро дар Render доимӣ нигоҳ доред; онро баъди ҳар deploy иваз накунед.

## Аввалин воридшавии Admin
Пароли аввал аз `ADMIN_PASSWORD` гирифта мешавад. Пароли пешфарзро истифода накунед; онро дар Render Environment иваз кунед. Дар панели Admin имкони иваз кардани парол низ ҳаст.
