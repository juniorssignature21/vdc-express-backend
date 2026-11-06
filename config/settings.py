"""Django settings for the TrackPro API. Configure via environment variables / backend/.env (TP_ prefix)."""
import os
import warnings
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name, default=""):
    return os.environ.get(f"TP_{name}", default)


def env_bool(name, default=False):
    return env(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [v.strip() for v in env(name, default).split(",") if v.strip()]


ON_VERCEL = bool(os.environ.get("VERCEL"))

SECRET_KEY = env("SECRET_KEY", "django-insecure-change-me-in-production")
DEBUG = env_bool("DEBUG", not ON_VERCEL)
ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
if ON_VERCEL:
    ALLOWED_HOSTS.append(".vercel.app")
    for var in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL", "VERCEL_BRANCH_URL"):
        if os.environ.get(var):
            CSRF_TRUSTED_ORIGINS.append(f"https://{os.environ[var]}")
    if not DEBUG and SECRET_KEY.startswith("django-insecure"):
        from django.core.exceptions import ImproperlyConfigured
        raise ImproperlyConfigured("Set TP_SECRET_KEY in the Vercel project environment variables.")

# Vercel terminates TLS in front of the function
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "tracking",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serves /static/ (Django admin CSS/JS)
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
APPEND_SLASH = False  # the React client calls /api/... without trailing slashes

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": env("DB_PATH", str(BASE_DIR / "trackpro.db")),
    }
}
# Postgres: set TP_DB_ENGINE=postgresql plus TP_DB_NAME/USER/PASSWORD/HOST/PORT (pip install "psycopg[binary]")
# A connection URL wins (Vercel's Neon / Supabase integrations set DATABASE_URL or POSTGRES_URL).
DATABASE_URL = env("DATABASE_URL") or os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if DATABASE_URL:
    import dj_database_url
    DATABASES["default"] = dj_database_url.parse(DATABASE_URL, conn_max_age=0, conn_health_checks=True)
elif env("DB_ENGINE") == "postgresql":
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "trackpro"),
        "USER": env("DB_USER", "postgres"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env("DB_PORT", "5432"),
    }
elif ON_VERCEL:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Vercel has no persistent disk: set DATABASE_URL (e.g. Neon Postgres).")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
WHITENOISE_USE_FINDERS = True  # serve app static files without a collectstatic step (serverless)
warnings.filterwarnings("ignore", message="No directory at")  # whitenoise: no collectstatic output
MEDIA_URL = "/uploads/"
MEDIA_ROOT = Path(env("UPLOAD_DIR", str(BASE_DIR / "uploads")))

# Package photos. Local disk by default; on Vercel use an S3-compatible bucket
# (AWS S3, Cloudflare R2, Supabase Storage, Backblaze B2...), since the function filesystem is not persistent.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
if env("S3_BUCKET"):
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": env("S3_BUCKET"),
            "access_key": env("S3_ACCESS_KEY_ID"),
            "secret_key": env("S3_SECRET_ACCESS_KEY"),
            "endpoint_url": env("S3_ENDPOINT_URL") or None,
            "region_name": env("S3_REGION") or None,
            "custom_domain": env("S3_PUBLIC_DOMAIN") or None,  # e.g. pub-xxxx.r2.dev or a CDN host
            "location": env("S3_PREFIX", "uploads"),
            "querystring_auth": env_bool("S3_SIGNED_URLS", False),
            "default_acl": None,
            "file_overwrite": False,
        },
    }
elif ON_VERCEL:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Vercel has no persistent disk: set TP_S3_BUCKET and the TP_S3_* credentials for photo uploads.")

# Vercel rejects request bodies over 4.5 MB, so keep a single upload below that there.
MAX_UPLOAD_MB = float(env("MAX_UPLOAD_MB", "4" if ON_VERCEL else "8"))
FRONTEND_DIST = BASE_DIR.parent / "frontend" / "dist"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

CORS_ALLOWED_ORIGINS = env_list("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
# optional, e.g. ^https://trackpro-frontend-[a-z0-9-]+\.vercel\.app$ to allow Vercel preview deployments
CORS_ALLOWED_ORIGIN_REGEXES = env_list("CORS_ORIGIN_REGEX")

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework_simplejwt.authentication.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    "EXCEPTION_HANDLER": "tracking.exceptions.exception_handler",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"login": "10/min", "public_form": "20/hour"},
    "COERCE_DECIMAL_TO_STRING": False,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(env("TOKEN_MINUTES", str(60 * 12)))),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "SIGNING_KEY": SECRET_KEY,
}

# ---- Business settings
TRACKPRO = {
    "ADMIN_EMAIL": env("ADMIN_EMAIL", "admin@vdcexpresslogistics.com"),
    "ADMIN_PASSWORD": env("ADMIN_PASSWORD", "admin123"),
    "SEED_DEMO_DATA": env_bool("SEED_DEMO_DATA", True),
    "COMPANY_NAME": env("COMPANY_NAME", "VDC Express Logistics"),
    "COMPANY_PHONE": env("COMPANY_PHONE", "+1 (555) 010-0199"),
    "COMPANY_EMAIL": env("COMPANY_EMAIL", "support@vdcexpresslogistics.com"),
    "COMPANY_ADDRESS": env("COMPANY_ADDRESS", "100 Express Way, Suite 200, Houston, TX 77032, United States"),
    "CURRENCY_SYMBOL": env("CURRENCY_SYMBOL", "$"),
    "CURRENCY_CODE": env("CURRENCY_CODE", "USD"),
    "VAT_RATE": float(env("VAT_RATE", "0")),          # sales tax / VAT on shipping; 0 = none
    "TAX_LABEL": env("TAX_LABEL", "Sales tax"),
    "FUEL_SURCHARGE": float(env("FUEL_SURCHARGE", "0.18")),  # share of transport charges, reviewed monthly
    "DOMESTIC_COUNTRY": "US",                          # rate zones in tracking/countries.py are measured from the US
    "TIME_ZONE": env("BUSINESS_TIME_ZONE", "America/Chicago"),  # for the daily pickup cut-off
}

# Public site URL (no trailing slash): Paystack returns customers here after paying.
FRONTEND_URL = env("FRONTEND_URL", "http://localhost:5173").rstrip("/")

# Paystack (card-only online checkout). Use sk_test_... keys until you go live.
PAYSTACK = {
    "SECRET_KEY": env("PAYSTACK_SECRET_KEY"),
}
