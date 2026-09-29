"""Traduction des erreurs (API, réseau, actions) en messages clairs en français."""

from __future__ import annotations

import requests
from hcloud import APIException

from ..models import ActionState


class UserError(Exception):
    def __init__(self, message: str, hint: str | None = None, code: str | None = None,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code
        self.retryable = retryable

    def __str__(self) -> str:
        return f"{self.message} — {self.hint}" if self.hint else self.message


class ReadOnlyError(UserError):
    def __init__(self, what: str) -> None:
        super().__init__(f"Mode lecture seule : « {what} » n'a pas été exécuté", code="readonly")


class OpCancelled(Exception):
    pass


class ActionFailed(Exception):
    def __init__(self, action: ActionState) -> None:
        super().__init__(action.error_message or action.error_code or "échec")
        self.action = action


class WaitTimeout(Exception):
    pass


_API_MESSAGES: dict[str, tuple[str, str | None, bool]] = {
    "unauthorized": ("Token API invalide ou révoqué",
                     "Remplacez ou retirez la clé dans la barre des projets, en haut de la fenêtre.", False),
    "forbidden": ("Le token API est en lecture seule",
                  "Créez un token « Lecture & écriture » (console Hetzner → Sécurité → Tokens API).", False),
    "token_readonly": ("Le token API est en lecture seule",
                       "Créez un token « Lecture & écriture » (console Hetzner → Sécurité → Tokens API).", False),
    "rate_limit_exceeded": ("Trop de requêtes vers l'API Hetzner", "Patientez une minute puis réessayez.", True),
    "resource_unavailable": ("Ce type de serveur est momentanément indisponible à cet emplacement",
                             "Choisissez un autre type ou un autre emplacement.", True),
    "placement_error": ("Hetzner n'a pas pu placer le serveur",
                        "Réessayez ou choisissez un autre emplacement.", True),
    "uniqueness_error": ("Ce nom est déjà utilisé dans le projet", None, False),
    "server_limit_exceeded": ("Limite de serveurs du compte atteinte",
                              "Demandez une augmentation de limite dans la console Hetzner.", False),
    "resource_limit_exceeded": ("Limite de ressources du compte atteinte",
                                "Demandez une augmentation de limite dans la console Hetzner.", False),
    "primary_ip_limit_exceeded": ("Limite d'IP primaires atteinte", None, False),
    "locked": ("Ressource verrouillée par une action en cours", "Réessayez dans quelques secondes.", True),
    "resource_in_use": ("Ressource encore utilisée", "Réessayez dans quelques secondes.", True),
    "conflict": ("Conflit avec une action en cours", "Réessayez dans quelques secondes.", True),
    "not_found": ("Ressource introuvable (déjà supprimée ?)", None, False),
    "protected": ("Ressource protégée contre la suppression", "Désépinglez-la d'abord.", False),
    "maintenance": ("Maintenance en cours chez Hetzner", "Réessayez plus tard.", True),
    "server_not_stopped": ("Le serveur doit être éteint pour cette action", None, False),
    "invalid_input": ("Paramètres refusés par l'API", None, False),
    "service_error": ("Erreur interne chez Hetzner", "Réessayez.", True),
    "unavailable": ("Service Hetzner indisponible", "Réessayez plus tard.", True),
    "timeout": ("L'API Hetzner n'a pas répondu à temps", "Réessayez.", True),
}


def api_code(exc: BaseException) -> str | None:
    if isinstance(exc, UserError):
        return exc.code
    if isinstance(exc, APIException):
        return str(exc.code)
    return None


def to_user_error(exc: BaseException) -> UserError:
    if isinstance(exc, UserError):
        return exc
    if isinstance(exc, APIException):
        code = str(exc.code)
        if code in _API_MESSAGES:
            message, hint, retryable = _API_MESSAGES[code]
            if code == "invalid_input" and exc.message:
                hint = exc.message
            return UserError(message, hint, code, retryable)
        if isinstance(exc.code, int) and exc.code >= 500:
            return UserError("L'API Hetzner est momentanément indisponible", "Réessayez dans un instant.",
                             code, True)
        return UserError(f"Erreur de l'API Hetzner : {exc.message}", None, code)
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return UserError("Connexion à l'API Hetzner impossible", "Vérifiez votre connexion Internet.",
                         "network", True)
    if isinstance(exc, ActionFailed):
        return UserError(f"L'action Hetzner a échoué : {exc}", None, exc.action.error_code, True)
    if isinstance(exc, WaitTimeout):
        return UserError(str(exc), None, "wait_timeout", True)
    return UserError(f"Erreur inattendue : {exc}", "Consultez le journal pour les détails.", "unexpected")
