"""Suivi de la licence d'évaluation Windows Server (fonctions pures).

L'application ne voit pas l'intérieur de Windows : elle suit la licence par des labels Hetzner posés à
la construction (date d'expiration, prolongations restantes, tâche de prolongation installée). À chaque
lancement, elle applique la même règle que la tâche planifiée : moins de EVAL_ALERT_DAYS jours restants
(ou licence expirée) et prolongations disponibles → slmgr /rearm au démarrage, soit 180 jours de plus.
Les labels suivent le serveur puis le snapshot suivant, comme l'état de licence suit le disque.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta

from .constants import EVAL_ALERT_DAYS, EVAL_PERIOD_DAYS, L_EVAL_AUTO, L_EVAL_EXP, L_EVAL_REARMS


@dataclass(frozen=True)
class EvalLicense:
    expires: date
    rearms: int | None     # prolongations restantes (None : inconnu)
    auto: bool             # tâche de prolongation automatique installée dans Windows

    def days_left(self, today: date) -> int:
        return (self.expires - today).days

    @property
    def can_extend(self) -> bool:
        return self.rearms is None or self.rearms > 0


def from_labels(labels: dict[str, str]) -> EvalLicense | None:
    raw = labels.get(L_EVAL_EXP, "")
    if len(raw) != 8 or not raw.isdigit():
        return None
    try:
        expires = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError:
        return None
    rearms = labels.get(L_EVAL_REARMS, "")
    return EvalLicense(expires, int(rearms) if rearms.isdigit() else None, labels.get(L_EVAL_AUTO) == "1")


def to_labels(lic: EvalLicense) -> dict[str, str]:
    labels = {L_EVAL_EXP: lic.expires.strftime("%Y%m%d"), L_EVAL_AUTO: "1" if lic.auto else "0"}
    if lic.rearms is not None:
        labels[L_EVAL_REARMS] = str(lic.rearms)
    return labels


def new_license(activated: date, rearms: int | None, auto: bool, period_days: int = EVAL_PERIOD_DAYS) -> EvalLicense:
    return EvalLicense(activated + timedelta(days=period_days), rearms, auto)


def at_boot(lic: EvalLicense, today: date) -> tuple[EvalLicense, bool]:
    """État après le démarrage d'un bureau : la tâche prolonge-t-elle la licence ? (licence, prolongée)"""
    if lic.auto and lic.can_extend and lic.days_left(today) < EVAL_ALERT_DAYS:
        rearms = None if lic.rearms is None else lic.rearms - 1
        return replace(lic, expires=today + timedelta(days=EVAL_PERIOD_DAYS), rearms=rearms), True
    return lic, False


def _days(n: int) -> str:
    return f"{n} jour{'s' if abs(n) > 1 else ''}"


def summary(lic: EvalLicense, today: date) -> str:
    """Ligne « Licence » de la carte."""
    left = lic.days_left(today)
    when = lic.expires.strftime("%d/%m/%Y")
    head = (f"Évaluation expirée depuis {_days(-left)}" if left < 0
            else f"Évaluation : {_days(left)} restant{'s' if left > 1 else ''} (jusqu'au {when})")
    if lic.auto:
        if not lic.can_extend:
            return f"{head} · plus de prolongation possible"
        count = f" ({lic.rearms} restante{'s' if lic.rearms > 1 else ''})" if lic.rearms is not None else ""
        return f"{head} · prolongation automatique{count}"
    return f"{head} · prolongation manuelle (slmgr /rearm)"


def needs_alert(lic: EvalLicense, today: date) -> bool:
    return lic.days_left(today) < EVAL_ALERT_DAYS


def needs_action(lic: EvalLicense, today: date) -> bool:
    """Alerte qui demande une intervention (la tâche automatique ne s'en chargera pas)."""
    return needs_alert(lic, today) and not (lic.auto and lic.can_extend)


def alert(lic: EvalLicense, today: date) -> str | None:
    """Encadré d'alerte de la carte, à moins de EVAL_ALERT_DAYS jours de l'expiration."""
    if not needs_alert(lic, today):
        return None
    left = lic.days_left(today)
    head = ("Licence d'évaluation expirée : Windows s'éteindra seul au bout d'une heure d'utilisation."
            if left < 0 else f"Licence d'évaluation : expire dans {_days(max(left, 0))}.")
    if lic.auto and lic.can_extend:
        return (f"{head} Elle sera prolongée automatiquement de {EVAL_PERIOD_DAYS} jours au prochain lancement "
                "(un redémarrage de plus au démarrage).")
    if lic.auto:
        return (f"{head} Plus aucune prolongation n'est possible : copiez vos données ou passez à une licence "
                "définitive (DISM /Set-Edition avec une clé achetée).")
    return (f"{head} Lancez le bureau puis exécutez « slmgr /rearm » (PowerShell administrateur) et redémarrez, "
            "ou installez la prolongation automatique (menu ⋯ → Licence d'évaluation…).")
