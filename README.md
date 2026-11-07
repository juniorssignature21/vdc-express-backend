# VDC Express Logistics API (Django REST Framework)

Backend for the VDC Express Logistics site: express-integrator pricing, online booking with card payment on the site (Stripe), shipments, tracking events, receipts, package photos, quote requests, contact messages and staff login with JWT. The project is in `config/` and the app is `tracking/`. The React frontend lives in its own repo and talks to this API over HTTPS.

## Run it (development)

Python 3.10+:

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
python manage.py migrate
python manage.py seed           # admin account + 3 demo shipments (--no-demo for the admin only)
python manage.py runserver 8000
```

The frontend's Vite dev server proxies `/api` and `/uploads` to port 8000. Django's built-in admin is at http://127.0.0.1:8000/django-admin/.

Admin login: `admin@vdcexpresslogistics.com` / `admin123` (set `TP_ADMIN_EMAIL` / `TP_ADMIN_PASSWORD` before seeding production). Demo waybills: `4829137563` (domestic Express, in transit), `5710042181` (export to Nigeria, in customs), `3065581201` (Same Day, delivered). Demo receipt: `RCP-2611-306558`.

## Shipping rules

Pricing lives in [tracking/pricing.py](tracking/pricing.py) and zones in [tracking/countries.py](tracking/countries.py). The frontend never prices on its own; it calls `POST /api/quote/calculate`.

- **Products:** Same Day (domestic, book by 12:00), Express 9:00 and Express 12:00 (time-definite; US, Canada/Mexico, Europe, major Asia-Pacific), Express, Economy Select.
- **Routes:** domestic (US to US), export, import (+10%), third-country (+30%). International rates by zones 1–8. Cuba, Iran, North Korea, Syria, Russia and Belarus aren't served; elevated-risk destinations add a $30 surcharge.
- **Chargeable weight:** the higher of actual and volumetric weight (L×W×H ÷ 5000) per piece, rounded up to the next 0.5 kg (whole kg above 30 kg), minimum 0.5 kg. Online limits: 20 pieces, 150 kg and 150 cm per piece.
- **Rate card:** first 0.5 kg + each additional 0.5 kg up to 30 kg, then per kg. International documents (up to 2 kg, no customs value) get 15% off.
- **Surcharges:** fuel (`TP_FUEL_SURCHARGE`, 18% by default), time-definite premium, oversize piece (>100 cm, $45), overweight piece (>70 kg, $95), Duty Tax Paid/DDP ($25), direct signature ($7.50), shipment value protection (1%, min $15, up to $25,000).
- **Delivery dates:** business days (Mon–Fri) from the first possible pickup. Cut-off 17:00 (Same Day 12:00) in `TP_BUSINESS_TIME_ZONE`. Public holidays aren't modelled.
- **Waybills:** 10 digits with a mod-7 check digit. Receipts: `RCP-YYMM-NNNNNN`.

The rate numbers are starting values: review `EXPRESS_RATES`, the surcharges and the transit table against your carrier contracts before going live.

## Online booking and card payments (Stripe)

Customers pay on the booking page itself with Stripe's **Payment Element**: there is no redirect to a hosted checkout. The card fields are Stripe iframes, so card numbers go straight to Stripe and never reach this server (PCI DSS SAQ A: no certificate needed, just Stripe's yearly self-assessment questionnaire). 3-D Secure, when the bank asks for it, opens as a pop-up over the page.

1. `POST /api/bookings` validates addresses and contents, prices the shipment on the server and creates a **card-only** PaymentIntent (`payment_method_types: ["card"]`). It returns the `client_secret` and publishable key.
2. The page mounts the Payment Element and confirms the payment in place (`redirect: "if_required"`).
3. `GET /api/bookings/verify?reference=…` re-reads the PaymentIntent from Stripe (`succeeded`, `amount_received`, currency, booking reference in the metadata, paid by card) and only then creates the shipment, a tracking event and a PAID receipt.
4. `POST /api/stripe/webhook` runs the same idempotent step, so a closed browser tab still gets booked. The event is only a trigger: the PaymentIntent is always re-read from Stripe with the secret key, so no webhook signing secret is needed (a forged event can't book anything).

A declined card keeps the same PaymentIntent, so the customer can try another card on the confirmation page (`POST /api/bookings/<ref>/pay`). A payment for the wrong amount or another booking is flagged as failed and shows up under **Online bookings** in the admin.

What customers see from Stripe: only the card fields. Your business name appears on their **card statement** (the account's statement descriptor, set in Stripe → Settings → Public details), so open the Stripe account for VDC Express Logistics.

Setup:
1. Stripe must support the country your business is registered in (Nigeria isn't supported; a US company is). Create the account for VDC Express Logistics and set the statement descriptor.
2. Set `TP_STRIPE_SECRET_KEY`, `TP_STRIPE_PUBLISHABLE_KEY` (test keys first) and `TP_FRONTEND_URL`.
3. Add the webhook endpoint `https://<api-domain>/api/stripe/webhook` with `payment_intent.succeeded`, `payment_intent.payment_failed` and `payment_intent.canceled`. No signing secret is needed.
4. Test with card 4242 4242 4242 4242 (any future date, any CVC), 4000 0027 6000 3184 (3-D Secure pop-up) and 4000 0000 0000 0002 (declined).

Without keys, the site shows prices but booking returns "Online card payments aren't set up yet".

## Tests

```bash
.venv\Scripts\python -m pytest -q
```

## Deploy on Vercel

Vercel has no persistent disk, so production needs a **Postgres database** (SQLite won't work) and an **S3-compatible bucket** for package photos (Cloudflare R2, AWS S3, Supabase Storage, B2). The settings refuse to start on Vercel without them.

Import this repo into Vercel with framework preset **Other**. [vercel.json](vercel.json) sends every request to [api/index.py](api/index.py) (Django WSGI on Python 3.12). Django admin static files are served by WhiteNoise.

Environment variables:

| Variable | Value |
|---|---|
| `TP_SECRET_KEY` | long random string (`python -c "import secrets;print(secrets.token_urlsafe(50))"`) |
| `DATABASE_URL` | Postgres URL. Adding Neon from Vercel's **Storage / Marketplace** tab sets it for you |
| `TP_S3_BUCKET`, `TP_S3_ACCESS_KEY_ID`, `TP_S3_SECRET_ACCESS_KEY` | bucket credentials |
| `TP_S3_ENDPOINT_URL`, `TP_S3_REGION`, `TP_S3_PUBLIC_DOMAIN` | for R2 / Supabase / B2 etc. (see `.env.example`) |
| `TP_CORS_ORIGINS` | the frontend URL(s), e.g. `https://vdcexpresslogistics.com,https://vdc-express.vercel.app` |
| `TP_CORS_ORIGIN_REGEX` | optional, to allow the frontend's preview deployments |
| `TP_ALLOWED_HOSTS` | your custom API domain, if any (`*.vercel.app` is allowed automatically) |
| `TP_STRIPE_SECRET_KEY`, `TP_STRIPE_PUBLISHABLE_KEY` | Stripe API keys (card payments) |
| `TP_FRONTEND_URL` | the website URL customers return to after paying, e.g. `https://vdcexpresslogistics.com` |
| `TP_ADMIN_EMAIL`, `TP_ADMIN_PASSWORD`, `TP_COMPANY_*`, `TP_CURRENCY_*`, `TP_FUEL_SURCHARGE`, `TP_VAT_RATE` | as in `.env.example` |

`TP_DEBUG` defaults to false on Vercel. The bucket must allow public reads, or set `TP_S3_SIGNED_URLS=true`.

Migrations don't run on deploy. Run them from your machine against the production database after the first deploy and after any model change:

```bash
set DATABASE_URL=postgresql://...      # macOS/Linux: export DATABASE_URL=...
python manage.py migrate
python manage.py seed --no-demo        # creates the admin account
```

Check that `https://<your-api>.vercel.app/api/health` returns `{"ok": true}`.

**Limits on Vercel:** request bodies are capped at 4.5 MB, so photos are limited to 4 MB each there (`TP_MAX_UPLOAD_MB`). Rate limits (10 logins a minute, 20 forms an hour) are kept in memory per function instance, so they're approximate.

## Other hosting

```bash
pip install gunicorn
python manage.py migrate && python manage.py seed --no-demo
gunicorn config.wsgi --bind 0.0.0.0:8000
```

Set `TP_SECRET_KEY`, `TP_DEBUG=false`, `TP_ALLOWED_HOSTS`, `TP_CORS_ORIGINS` and change the admin password before going live.
