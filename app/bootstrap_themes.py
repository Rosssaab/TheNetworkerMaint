"""Bootstrap / Bootswatch theme slugs (shared helpers). Admin console uses its own session/cookie keys."""

BOOTSTRAP_DIST_VERSION = "5.3.3"
BOOTSWATCH_DIST_VERSION = "5.3.3"

TNW_ADMIN_BOOTSTRAP_THEME_COOKIE = "tnw_admin_bootstrap_theme"
TNW_ADMIN_BOOTSTRAP_THEME_SESSION = "tnw_admin_bootstrap_theme"
TNW_SITE_BOOTSTRAP_THEME_COOKIE = "tnw_site_bootstrap_theme"
TNW_SITE_BOOTSTRAP_THEME_SESSION = "tnw_site_bootstrap_theme"
# First visit (other admin_base pages): stock Bootstrap; Bootswatch via cookie/session if set.
TNW_ADMIN_DEFAULT_BOOTSTRAP_THEME = "default"
TNW_SITE_DEFAULT_BOOTSTRAP_THEME = "default"

# Bootswatch 5.3.3 dist themes plus vanilla Bootstrap.
TNW_BOOTSTRAP_THEMES: tuple[tuple[str, str], ...] = (
    ("default", "Bootstrap (default)"),
    ("cerulean", "Cerulean"),
    ("cosmo", "Cosmo"),
    ("cyborg", "Cyborg"),
    ("darkly", "Darkly"),
    ("flatly", "Flatly"),
    ("journal", "Journal"),
    ("litera", "Litera"),
    ("lumen", "Lumen"),
    ("lux", "Lux"),
    ("materia", "Materia"),
    ("minty", "Minty"),
    ("morph", "Morph"),
    ("pulse", "Pulse"),
    ("quartz", "Quartz"),
    ("sandstone", "Sandstone"),
    ("simplex", "Simplex"),
    ("sketchy", "Sketchy"),
    ("slate", "Slate"),
    ("solar", "Solar"),
    ("spacelab", "Spacelab"),
    ("superhero", "Superhero"),
    ("united", "United"),
    ("vapor", "Vapor"),
    ("yeti", "Yeti"),
    ("zephyr", "Zephyr"),
)

TNW_BOOTSTRAP_THEME_SLUGS = frozenset(slug for slug, _ in TNW_BOOTSTRAP_THEMES)

# Use a light layout shell (background, text) with these Bootstrap skins.
TNW_ADMIN_LIGHT_SHELL_THEMES = frozenset(
    {
        "default",
        "cerulean",
        "cosmo",
        "flatly",
        "journal",
        "litera",
        "lumen",
        "materia",
        "minty",
        "morph",
        "pulse",
        "quartz",
        "sandstone",
        "simplex",
        "sketchy",
        "spacelab",
        "united",
        "yeti",
        "zephyr",
    }
)


def normalize_bootstrap_theme_slug(raw: str | None) -> str:
    s = (raw or "default").strip().lower()
    return s if s in TNW_BOOTSTRAP_THEME_SLUGS else "default"


def bootstrap_theme_stylesheet_url(slug: str) -> str:
    slug = normalize_bootstrap_theme_slug(slug)
    if slug == "default":
        return (
            "https://cdn.jsdelivr.net/npm/bootstrap@"
            f"{BOOTSTRAP_DIST_VERSION}/dist/css/bootstrap.min.css"
        )
    return (
        "https://cdn.jsdelivr.net/npm/bootswatch@"
        f"{BOOTSWATCH_DIST_VERSION}/dist/{slug}/bootstrap.min.css"
    )


def resolve_admin_bootstrap_theme_slug(session_val, cookie_val) -> str:
    raw = session_val or cookie_val
    if not raw:
        return TNW_ADMIN_DEFAULT_BOOTSTRAP_THEME
    return normalize_bootstrap_theme_slug(raw)


def resolve_site_bootstrap_theme_slug(session_val, cookie_val) -> str:
    raw = session_val or cookie_val
    if not raw:
        return TNW_SITE_DEFAULT_BOOTSTRAP_THEME
    return normalize_bootstrap_theme_slug(raw)


def resolve_bootstrap_theme_slug(
    site_session_val,
    site_cookie_val,
    admin_session_val=None,
    admin_cookie_val=None,
) -> str:
    """Single site-wide theme (site cookie/session, then legacy admin keys)."""
    raw = site_session_val or site_cookie_val or admin_session_val or admin_cookie_val
    if not raw:
        return TNW_SITE_DEFAULT_BOOTSTRAP_THEME
    return normalize_bootstrap_theme_slug(raw)


def bootstrap_theme_is_bootswatch(slug: str) -> bool:
    return normalize_bootstrap_theme_slug(slug) != "default"


def admin_bootstrap_uses_light_shell(slug: str) -> bool:
    """Custom light admin chrome only when stock Bootstrap is active."""
    slug = normalize_bootstrap_theme_slug(slug)
    if bootstrap_theme_is_bootswatch(slug):
        return False
    return slug in TNW_ADMIN_LIGHT_SHELL_THEMES
