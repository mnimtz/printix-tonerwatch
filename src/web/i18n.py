"""EFIGS translations (English, French, Italian, German, Spanish).

Rules
-----
* Every string exposed to the UI must exist in **all five** languages.
* :func:`check_translations` runs at boot; missing keys crash the app
  with a clear error rather than silently rendering the key name.
* Adding a new key: append it to all five dicts in the same commit.
  ``check_translations`` will refuse to start otherwise.
"""

from __future__ import annotations

from typing import Mapping


SUPPORTED_LANGS: tuple[str, ...] = ("en", "fr", "it", "de", "es")
DEFAULT_LANG: str = "en"

# Human-readable names for the language switcher (endonyms).
LANG_LABELS: Mapping[str, str] = {
    "en": "English",
    "fr": "Français",
    "it": "Italiano",
    "de": "Deutsch",
    "es": "Español",
}


# --------------------------------------------------------------------------
# Translations. Keep alphabetical order per section inside each language.
# --------------------------------------------------------------------------

_EN: dict[str, str] = {
    # Brand / product
    "brand.product_name":  "Printix Toner Radar",
    "brand.tagline":       "Multi-tenant toner monitoring for MSPs",
    "brand.by_line":       "for the Tungsten Printix ecosystem",
    # Navigation
    "nav.dashboard":       "Dashboard",
    "nav.toner":           "Toner status",
    "nav.orders":          "Orders",
    "nav.customers":       "Customers",
    "nav.users":           "Users",
    "nav.settings":        "Settings",
    "nav.language":        "Language",
    "nav.sign_out":        "Sign out",
    "nav.signed_in_as":    "Signed in as",
    # Authentication
    "auth.sign_in":                 "Sign in",
    "auth.email":                   "Email address",
    "auth.password":                "Password",
    "auth.sign_in_button":          "Sign in",
    "auth.invalid_credentials":     "Invalid email or password.",
    "auth.session_expired":         "Your session has expired — please sign in again.",
    "auth.forgot_password_hint":    "Passwords are managed by your Toner Radar administrator.",
    # First-run setup
    "setup.title":                  "Welcome to Printix Toner Radar",
    "setup.intro":                  "Let's create the first administrator account. This account will have full access and can create additional users afterwards.",
    "setup.name":                   "Full name",
    "setup.email":                  "Email address",
    "setup.password":               "Password",
    "setup.password_confirm":       "Confirm password",
    "setup.create_button":          "Create administrator",
    "setup.password_mismatch":      "The two passwords do not match.",
    "setup.password_too_short":     "Password must be at least 12 characters.",
    "setup.already_configured":     "This Toner Radar instance is already configured. Please sign in.",
    # Common actions & status
    "common.save":                  "Save",
    "common.cancel":                "Cancel",
    "common.delete":                "Delete",
    "common.edit":                  "Edit",
    "common.back":                  "Back",
    "common.loading":               "Loading…",
    "common.yes":                   "Yes",
    "common.no":                    "No",
    "common.error":                 "Something went wrong. Please try again.",
    "common.access_denied":         "You don't have permission to access this page.",
    "common.not_found":             "The page you are looking for was not found.",
    # Footer
    "footer.copyright":             "© 2026 Printix Toner Radar — released under the Apache License 2.0",
    "footer.tungsten_note":         "Tungsten Automation®, Tungsten Printix™ and the Tungsten Automation logo are trademarks of Tungsten Automation Corporation.",
}

_FR: dict[str, str] = {
    "brand.product_name":  "Printix Toner Radar",
    "brand.tagline":       "Supervision multi-clients des toners pour MSP",
    "brand.by_line":       "pour l'écosystème Tungsten Printix",
    "nav.dashboard":       "Tableau de bord",
    "nav.toner":           "État des toners",
    "nav.orders":          "Commandes",
    "nav.customers":       "Clients",
    "nav.users":           "Utilisateurs",
    "nav.settings":        "Paramètres",
    "nav.language":        "Langue",
    "nav.sign_out":        "Se déconnecter",
    "nav.signed_in_as":    "Connecté en tant que",
    "auth.sign_in":                 "Connexion",
    "auth.email":                   "Adresse e-mail",
    "auth.password":                "Mot de passe",
    "auth.sign_in_button":          "Se connecter",
    "auth.invalid_credentials":     "E-mail ou mot de passe incorrect.",
    "auth.session_expired":         "Votre session a expiré — veuillez vous reconnecter.",
    "auth.forgot_password_hint":    "Les mots de passe sont gérés par votre administrateur Toner Radar.",
    "setup.title":                  "Bienvenue dans Printix Toner Radar",
    "setup.intro":                  "Créons le premier compte administrateur. Ce compte disposera d'un accès complet et pourra créer d'autres utilisateurs par la suite.",
    "setup.name":                   "Nom complet",
    "setup.email":                  "Adresse e-mail",
    "setup.password":               "Mot de passe",
    "setup.password_confirm":       "Confirmer le mot de passe",
    "setup.create_button":          "Créer l'administrateur",
    "setup.password_mismatch":      "Les deux mots de passe ne correspondent pas.",
    "setup.password_too_short":     "Le mot de passe doit contenir au moins 12 caractères.",
    "setup.already_configured":     "Cette instance Toner Radar est déjà configurée. Veuillez vous connecter.",
    "common.save":                  "Enregistrer",
    "common.cancel":                "Annuler",
    "common.delete":                "Supprimer",
    "common.edit":                  "Modifier",
    "common.back":                  "Retour",
    "common.loading":               "Chargement…",
    "common.yes":                   "Oui",
    "common.no":                    "Non",
    "common.error":                 "Une erreur est survenue. Veuillez réessayer.",
    "common.access_denied":         "Vous n'êtes pas autorisé à accéder à cette page.",
    "common.not_found":             "La page recherchée est introuvable.",
    "footer.copyright":             "© 2026 Printix Toner Radar — publié sous licence Apache 2.0",
    "footer.tungsten_note":         "Tungsten Automation®, Tungsten Printix™ et le logo Tungsten Automation sont des marques déposées de Tungsten Automation Corporation.",
}

_IT: dict[str, str] = {
    "brand.product_name":  "Printix Toner Radar",
    "brand.tagline":       "Monitoraggio toner multi-cliente per MSP",
    "brand.by_line":       "per l'ecosistema Tungsten Printix",
    "nav.dashboard":       "Dashboard",
    "nav.toner":           "Stato toner",
    "nav.orders":          "Ordini",
    "nav.customers":       "Clienti",
    "nav.users":           "Utenti",
    "nav.settings":        "Impostazioni",
    "nav.language":        "Lingua",
    "nav.sign_out":        "Esci",
    "nav.signed_in_as":    "Connesso come",
    "auth.sign_in":                 "Accedi",
    "auth.email":                   "Indirizzo e-mail",
    "auth.password":                "Password",
    "auth.sign_in_button":          "Accedi",
    "auth.invalid_credentials":     "E-mail o password non valide.",
    "auth.session_expired":         "La sessione è scaduta — accedi di nuovo.",
    "auth.forgot_password_hint":    "Le password sono gestite dall'amministratore di Toner Radar.",
    "setup.title":                  "Benvenuto in Printix Toner Radar",
    "setup.intro":                  "Creiamo il primo account amministratore. Questo account avrà accesso completo e potrà creare altri utenti in seguito.",
    "setup.name":                   "Nome completo",
    "setup.email":                  "Indirizzo e-mail",
    "setup.password":               "Password",
    "setup.password_confirm":       "Conferma password",
    "setup.create_button":          "Crea amministratore",
    "setup.password_mismatch":      "Le due password non coincidono.",
    "setup.password_too_short":     "La password deve contenere almeno 12 caratteri.",
    "setup.already_configured":     "Questa istanza di Toner Radar è già configurata. Accedi.",
    "common.save":                  "Salva",
    "common.cancel":                "Annulla",
    "common.delete":                "Elimina",
    "common.edit":                  "Modifica",
    "common.back":                  "Indietro",
    "common.loading":               "Caricamento…",
    "common.yes":                   "Sì",
    "common.no":                    "No",
    "common.error":                 "Si è verificato un errore. Riprova.",
    "common.access_denied":         "Non hai i permessi per accedere a questa pagina.",
    "common.not_found":             "La pagina richiesta non è stata trovata.",
    "footer.copyright":             "© 2026 Printix Toner Radar — rilasciato sotto Apache License 2.0",
    "footer.tungsten_note":         "Tungsten Automation®, Tungsten Printix™ e il logo Tungsten Automation sono marchi di Tungsten Automation Corporation.",
}

_DE: dict[str, str] = {
    "brand.product_name":  "Printix Toner Radar",
    "brand.tagline":       "Mandantenübergreifende Tonerüberwachung für MSPs",
    "brand.by_line":       "für das Tungsten-Printix-Ökosystem",
    "nav.dashboard":       "Übersicht",
    "nav.toner":           "Tonerstatus",
    "nav.orders":          "Bestellungen",
    "nav.customers":       "Kunden",
    "nav.users":           "Benutzer",
    "nav.settings":        "Einstellungen",
    "nav.language":        "Sprache",
    "nav.sign_out":        "Abmelden",
    "nav.signed_in_as":    "Angemeldet als",
    "auth.sign_in":                 "Anmelden",
    "auth.email":                   "E-Mail-Adresse",
    "auth.password":                "Passwort",
    "auth.sign_in_button":          "Anmelden",
    "auth.invalid_credentials":     "E-Mail oder Passwort ist ungültig.",
    "auth.session_expired":         "Ihre Sitzung ist abgelaufen — bitte erneut anmelden.",
    "auth.forgot_password_hint":    "Passwörter werden von Ihrer Toner-Radar-Administration verwaltet.",
    "setup.title":                  "Willkommen bei Printix Toner Radar",
    "setup.intro":                  "Legen wir das erste Administratorkonto an. Dieses Konto hat vollen Zugriff und kann anschließend weitere Benutzer anlegen.",
    "setup.name":                   "Vollständiger Name",
    "setup.email":                  "E-Mail-Adresse",
    "setup.password":               "Passwort",
    "setup.password_confirm":       "Passwort bestätigen",
    "setup.create_button":          "Administrator anlegen",
    "setup.password_mismatch":      "Die beiden Passwörter stimmen nicht überein.",
    "setup.password_too_short":     "Das Passwort muss mindestens 12 Zeichen lang sein.",
    "setup.already_configured":     "Diese Toner-Radar-Instanz ist bereits eingerichtet. Bitte melden Sie sich an.",
    "common.save":                  "Speichern",
    "common.cancel":                "Abbrechen",
    "common.delete":                "Löschen",
    "common.edit":                  "Bearbeiten",
    "common.back":                  "Zurück",
    "common.loading":               "Wird geladen…",
    "common.yes":                   "Ja",
    "common.no":                    "Nein",
    "common.error":                 "Etwas ist schiefgelaufen. Bitte versuchen Sie es erneut.",
    "common.access_denied":         "Sie haben keine Berechtigung für diese Seite.",
    "common.not_found":             "Die gesuchte Seite wurde nicht gefunden.",
    "footer.copyright":             "© 2026 Printix Toner Radar — veröffentlicht unter der Apache-Lizenz 2.0",
    "footer.tungsten_note":         "Tungsten Automation®, Tungsten Printix™ und das Tungsten-Automation-Logo sind Marken der Tungsten Automation Corporation.",
}

_ES: dict[str, str] = {
    "brand.product_name":  "Printix Toner Radar",
    "brand.tagline":       "Supervisión multi-cliente de tóner para MSP",
    "brand.by_line":       "para el ecosistema Tungsten Printix",
    "nav.dashboard":       "Panel",
    "nav.toner":           "Estado del tóner",
    "nav.orders":          "Pedidos",
    "nav.customers":       "Clientes",
    "nav.users":           "Usuarios",
    "nav.settings":        "Ajustes",
    "nav.language":        "Idioma",
    "nav.sign_out":        "Cerrar sesión",
    "nav.signed_in_as":    "Sesión iniciada como",
    "auth.sign_in":                 "Iniciar sesión",
    "auth.email":                   "Correo electrónico",
    "auth.password":                "Contraseña",
    "auth.sign_in_button":          "Iniciar sesión",
    "auth.invalid_credentials":     "Correo o contraseña no válidos.",
    "auth.session_expired":         "Tu sesión ha expirado — inicia sesión de nuevo.",
    "auth.forgot_password_hint":    "Las contraseñas las gestiona tu administrador de Toner Radar.",
    "setup.title":                  "Te damos la bienvenida a Printix Toner Radar",
    "setup.intro":                  "Vamos a crear la primera cuenta de administrador. Esta cuenta tendrá acceso completo y podrá crear más usuarios después.",
    "setup.name":                   "Nombre completo",
    "setup.email":                  "Correo electrónico",
    "setup.password":               "Contraseña",
    "setup.password_confirm":       "Confirmar contraseña",
    "setup.create_button":          "Crear administrador",
    "setup.password_mismatch":      "Las dos contraseñas no coinciden.",
    "setup.password_too_short":     "La contraseña debe tener al menos 12 caracteres.",
    "setup.already_configured":     "Esta instancia de Toner Radar ya está configurada. Inicia sesión.",
    "common.save":                  "Guardar",
    "common.cancel":                "Cancelar",
    "common.delete":                "Eliminar",
    "common.edit":                  "Editar",
    "common.back":                  "Volver",
    "common.loading":               "Cargando…",
    "common.yes":                   "Sí",
    "common.no":                    "No",
    "common.error":                 "Algo ha ido mal. Vuelve a intentarlo.",
    "common.access_denied":         "No tienes permiso para acceder a esta página.",
    "common.not_found":             "No se ha encontrado la página que buscas.",
    "footer.copyright":             "© 2026 Printix Toner Radar — publicado bajo la licencia Apache 2.0",
    "footer.tungsten_note":         "Tungsten Automation®, Tungsten Printix™ y el logotipo de Tungsten Automation son marcas comerciales de Tungsten Automation Corporation.",
}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": _EN, "fr": _FR, "it": _IT, "de": _DE, "es": _ES,
}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def check_translations() -> None:
    """Ensure every key exists in every language. Raise on any gap.

    Called once at server start-up so a translation regression causes an
    immediate, loud failure rather than a silent placeholder in production.
    """
    reference_keys = set(_EN.keys())
    problems: list[str] = []
    for lang in SUPPORTED_LANGS:
        keys = set(TRANSLATIONS[lang].keys())
        missing = reference_keys - keys
        extra = keys - reference_keys
        if missing:
            problems.append(f"{lang}: missing {sorted(missing)}")
        if extra:
            problems.append(f"{lang}: extra {sorted(extra)} (not in EN reference)")
    if problems:
        raise RuntimeError(
            "Translation gaps detected — every key must be defined in all "
            f"languages:\n  " + "\n  ".join(problems)
        )


def t(key: str, lang: str) -> str:
    """Return the translated string for ``key`` in ``lang``.

    Falls back to English, then to the raw key. In production the boot-time
    ``check_translations`` should prevent the raw-key fallback from ever
    firing.
    """
    lang = lang if lang in TRANSLATIONS else DEFAULT_LANG
    return (TRANSLATIONS[lang].get(key)
            or TRANSLATIONS[DEFAULT_LANG].get(key)
            or key)
