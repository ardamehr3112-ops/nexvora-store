NEXVORA STORE TJ — V75 — Render deploy package

ИН ПАКЕТ БАРОИ ҶОЙГИРКУНӢ ДАР RENDER ТАЙЁР ШУДААСТ.

Муҳим:
- render.yaml барои Web Service-и пулакӣ (Starter) ва диски доимии 1 GB омода шудааст.
- Диски доимӣ барои nexvora.db, uploads, secret key ва log истифода мешавад.
- Пеш аз истифодаи мағоза ADMIN_PASSWORD ва WHATSAPP_NUMBER-ро дар Render пур кунед.
- ADMIN_PASSWORD бояд пароли қавӣ ва махсус бошад. Онро ба касе нафиристед.
- SECRET_KEY аз тарафи Render сохта мешавад.

ҚАДАМҲО:
1. ZIP-ро кушоед.
2. Папкаи nexvora_render_deploy-ро ба GitHub ҳамчун repository-и Private бор кунед.
3. Ба https://dashboard.render.com ворид шавед.
4. New + -> Blueprint интихоб кунед.
5. GitHub repository-и NEXVORA-ро пайваст кунед.
6. Render render.yaml-ро мехонад. Service-и nexvora-store-tj пайдо мешавад.
7. Дар Environment пур кунед:
   ADMIN_PASSWORD = пароли қавии Admin
   WHATSAPP_NUMBER = рақами WhatsApp бо рамзи кишвар, танҳо рақамҳо
8. Нақша ва пардохти Starter + disk-ро бо иҷозати соҳиби ҳисоб/падару модар тасдиқ кунед.
9. Deploy кунед. Баъд аз анҷом URL-и *.onrender.com-ро кушоед.
10. Санҷед: https://АДРЕСИ-ШУМО.onrender.com/healthz
    Ҷавоби дуруст бояд status=ok ва version=V75 дошта бошад.

ДОМЕН:
Пас аз фаъол шудани сомона дар Render -> Settings -> Custom Domains доменро илова кунед.
DNS record-ҳои пешниҳодкардаи Render-ро дар ширкати фурӯшандаи домен гузоред.

ОГОҲӢ:
- Барои нигоҳ доштани база ва аксҳо диски доимӣ зарур аст; ин конфигуратсия Starter-ро истифода мебарад.
- Барои мағозаи воқеӣ нусхаи эҳтиётии база ва санҷиши фармоишҳоро ташкил кунед.
- Дар ин марҳила deploy ба Render аз тарафи мо иҷро нашудааст: соҳиби аккаунт бояд GitHub ва Render-ро пайваст кунад.
