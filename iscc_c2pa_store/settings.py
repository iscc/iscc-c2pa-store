"""Django settings, read from environment variables.

`DJANGO_SECRET_KEY` and `ISCC_C2PA_STORE_URL` are required. Store settings carry the prefix `ISCC_C2PA_STORE_`;
`.env.example` lists each environment variable read here except `DJANGO_STATIC_ROOT`, which the container image sets
(`uv run poe serve` reads a local `.env`). Declarations are enabled when both a secret key and a hub URL are set.
"""

import os
from pathlib import Path
from urllib.parse import quote, urlsplit

import dj_database_url
from django.templatetags.static import static
from django.urls import reverse_lazy

BASE_DIR = Path(__file__).resolve().parent.parent
TRUE = {"1", "true", "yes", "on"}


def env(name, default=None):
    # type: (str, str | None) -> str
    """An environment variable; a missing one without default stops the start with a clear error."""
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Set the {name} environment variable")
    return value


def env_bool(name, default):
    # type: (str, bool) -> bool
    """A boolean environment variable: 1, true, yes or on (any case) are true."""
    return env(name, str(default)).strip().lower() in TRUE


def env_list(name, default):
    # type: (str, list[str]) -> list[str]
    """A comma-separated environment variable."""
    return [item.strip() for item in env(name, ",".join(default)).split(",") if item.strip()]


def hub_id(text):
    # type: (str) -> int
    """An ISCC hub ID: the 12-bit number in the nonces of the hub's notes.

    :raises ValueError: if it is not a number from 0 to 4095.
    """
    number = int(text)
    if not 0 <= number <= 4095:
        raise ValueError(f"ISCC_C2PA_STORE_HUB_ID must be 0 to 4095, not {number}")
    return number


DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = env("DJANGO_SECRET_KEY")

# Public base URL of this store; manifest addresses, the did:web identifier and the host check derive from it.
ISCC_C2PA_STORE_URL = env("ISCC_C2PA_STORE_URL").rstrip("/")
ISCC_C2PA_STORE_HOST = urlsplit(ISCC_C2PA_STORE_URL).netloc
ISCC_C2PA_STORE_CONTROLLER = "did:web:" + quote(ISCC_C2PA_STORE_HOST, safe="")
ISCC_C2PA_STORE_DATA_DIR = Path(env("ISCC_C2PA_STORE_DATA_DIR", str(BASE_DIR / "data")))
ISCC_C2PA_STORE_DATA_DIR.mkdir(parents=True, exist_ok=True)
ISCC_C2PA_STORE_TRUST_ANCHORS = Path(
    env("ISCC_C2PA_STORE_TRUST_ANCHORS", str(BASE_DIR / "iscc_c2pa_store" / "vendor" / "C2PA-TRUST-LIST.pem"))
)
# Declarations: the Ed25519 secret key (multibase, from `iscc-crypto keygen`) and the ISCC hub.
ISCC_C2PA_STORE_SECKEY = env("ISCC_C2PA_STORE_SECKEY", "")
ISCC_C2PA_STORE_HUB_URL = env("ISCC_C2PA_STORE_HUB_URL", "").rstrip("/")
ISCC_C2PA_STORE_HUB_ID = hub_id(env("ISCC_C2PA_STORE_HUB_ID", "1"))
ISCC_C2PA_STORE_HUB_TIMEOUT = float(env("ISCC_C2PA_STORE_HUB_TIMEOUT", "5.0"))
ISCC_C2PA_STORE_HUB_TRANSPORT = None  # an httpx2 transport for the hub; tests put a fake hub here
ISCC_C2PA_STORE_DECLARE_DEFAULT = env_bool("ISCC_C2PA_STORE_DECLARE_DEFAULT", True)
# Operator access: SHA-256 hex digest of the bearer token for deletes (unset: deletes are disabled).
ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256 = env("ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256", "").strip().lower()
# Limits: per client (IPv4 address or IPv6 /64) and period, the upload cap in bytes, the lowest reported score.
ISCC_C2PA_STORE_RATE_WRITE = env("ISCC_C2PA_STORE_RATE_WRITE", "10/m")
ISCC_C2PA_STORE_RATE_READ = env("ISCC_C2PA_STORE_RATE_READ", "120/m")
ISCC_C2PA_STORE_RATE_LOGIN = env("ISCC_C2PA_STORE_RATE_LOGIN", "10/m")  # operator login attempts
# Client addresses without rate limits, e.g. the operator's own resolvers, which fetch manifests for all their users.
ISCC_C2PA_STORE_UNTHROTTLED = env_list("ISCC_C2PA_STORE_UNTHROTTLED", [])
ISCC_C2PA_STORE_MAX_UPLOAD = int(env("ISCC_C2PA_STORE_MAX_UPLOAD", str(2 * 1024 * 1024)))
ISCC_C2PA_STORE_MATCH_THRESHOLD = int(env("ISCC_C2PA_STORE_MATCH_THRESHOLD", "75"))  # the resolver's lowest score
DATA_UPLOAD_MAX_MEMORY_SIZE = ISCC_C2PA_STORE_MAX_UPLOAD
# Reverse proxies in front of the store that append to X-Forwarded-For (testnet and mainnet: one, Caddy).
NINJA_NUM_PROXIES = int(env("ISCC_C2PA_STORE_NUM_PROXIES", "0"))

# The public host, and the loopback names the container health check uses.
PUBLIC_HOSTNAME = urlsplit(ISCC_C2PA_STORE_URL).hostname or ""
ALLOWED_HOSTS = [*env_list("DJANGO_ALLOWED_HOSTS", [PUBLIC_HOSTNAME]), "localhost", "127.0.0.1"]
SECURE = ISCC_C2PA_STORE_URL.startswith("https://")
CSRF_TRUSTED_ORIGINS = [ISCC_C2PA_STORE_URL]
CSRF_COOKIE_SECURE = SECURE
SESSION_COOKIE_SECURE = SECURE
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if NINJA_NUM_PROXIES else None
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
APPEND_SLASH = False

INSTALLED_APPS = [
    "unfold",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "servestatic.runserver_nostatic",
    "django.contrib.staticfiles",
    "iscc_c2pa_store",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "servestatic.middleware.ServeStaticMiddleware",
    "iscc_c2pa_store.middleware.PublicCorsMiddleware",  # first, so that refusals below carry CORS headers too
    "iscc_c2pa_store.middleware.UploadLimitMiddleware",
    "iscc_c2pa_store.middleware.LoginThrottleMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "iscc_c2pa_store.urls"
WSGI_APPLICATION = "iscc_c2pa_store.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASE_URL = env("DATABASE_URL", f"sqlite:///{(ISCC_C2PA_STORE_DATA_DIR / 'store.sqlite3').as_posix()}")
SQLITE_OPTIONS = {"init_command": "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;", "transaction_mode": "IMMEDIATE"}
DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)}
DATABASES["default"]["OPTIONS"] = SQLITE_OPTIONS if DATABASE_URL.startswith("sqlite") else {}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": ISCC_C2PA_STORE_DATA_DIR / "media"},
    },
    "staticfiles": {"BACKEND": "servestatic.storage.CompressedManifestStaticFilesStorage"},
}
STATIC_URL = "static/"
STATIC_ROOT = Path(env("DJANGO_STATIC_ROOT", str(BASE_DIR / "staticfiles")))  # filled by collectstatic
SERVESTATIC_AUTOREFRESH = SERVESTATIC_USE_FINDERS = DEBUG  # serve from the app directories while developing

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}  # rate limit counters

LOGIN_URL = "admin:login"
LOGIN_REDIRECT_URL = "admin:index"  # the operator dashboard is the only place to log in to

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}

UNFOLD = {
    "SITE_TITLE": "ISCC C2PA Store",
    "SITE_HEADER": "ISCC C2PA Store",
    "SITE_SUBHEADER": ISCC_C2PA_STORE_HOST,
    "SITE_URL": "/",
    "SITE_SYMBOL": "inventory_2",
    "SITE_FAVICONS": [{"rel": "icon", "type": "image/svg+xml", "href": lambda request: static("brand/favicon.svg")}],
    "SHOW_HISTORY": False,
    "DASHBOARD_CALLBACK": "iscc_c2pa_store.admin.dashboard",
    "SIDEBAR": {
        "show_search": True,
        "show_all_applications": False,
        "navigation": [
            {
                "title": "Store",
                "items": [
                    {"title": "Dashboard", "icon": "dashboard", "link": reverse_lazy("admin:index")},
                    {
                        "title": "Manifests",
                        "icon": "inventory_2",
                        "link": reverse_lazy("admin:iscc_c2pa_store_manifest_changelist"),
                    },
                    {
                        "title": "Declarations",
                        "icon": "campaign",
                        "link": reverse_lazy("admin:iscc_c2pa_store_declaration_changelist"),
                        "badge": "iscc_c2pa_store.admin.attention_badge",
                    },
                ],
            },
            {
                "title": "Access",
                "collapsible": True,
                "items": [
                    {"title": "Users", "icon": "person", "link": reverse_lazy("admin:auth_user_changelist")},
                    {"title": "Groups", "icon": "group", "link": reverse_lazy("admin:auth_group_changelist")},
                ],
            },
        ],
    },
    "COLORS": {
        "primary": {
            "50": "240 248 255",
            "100": "219 234 254",
            "200": "191 219 254",
            "300": "122 194 247",
            "400": "69 150 245",
            "500": "0 84 178",  # ISCC Blue
            "600": "0 84 178",
            "700": "14 43 79",
            "800": "10 32 59",
            "900": "7 22 39",
            "950": "4 11 20",
        },
    },
}
