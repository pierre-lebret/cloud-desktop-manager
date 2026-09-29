# Hetzner RDP Manager

Des bureaux Windows sur Hetzner Cloud **à la demande** : un bureau dort sous forme de snapshot (quelques centimes par mois) et ne devient un serveur facturé à l'heure que pendant que tu t'en sers.

30 h par mois sur un cpx32 reviennent à environ 1,70 € de calcul et 0,18 € de stockage, au lieu d'environ 35 € pour un serveur allumé en permanence.

## Installation

```powershell
pip install -r requirements.txt
```

Le token API Hetzner (en **lecture et écriture**) est cherché dans cet ordre :

1. la variable d'environnement `HETZNER_TOKEN` ;
2. le fichier `.env` placé à côté de `app.py` ;
3. le Gestionnaire d'identifiants Windows.

S'il n'est trouvé nulle part, l'application s'ouvre bloquée sur la barre « Token Hetzner », en haut de la
fenêtre, jusqu'à ce qu'un token soit saisi et accepté par Hetzner. Ce token reste en mémoire pour la session. Cochez
« Mémoriser » pour l'enregistrer dans le Gestionnaire d'identifiants.

Quand un token est chargé, la barre se réduit à une ligne (token masqué et provenance) :
- « Changer » bascule sur un autre token et recharge le projet. C'est refusé tant qu'une opération est en cours.
- « Oublier » retire le token du Gestionnaire d'identifiants.

## Lancement

| Commande | Effet |
|---|---|
| double-clic sur `HetznerRDP.pyw` | ouvre l'application sans console |
| `python app.py` | ouvre l'application sur le vrai compte |
| `python app.py --readonly` | lit le vrai compte sans jamais rien modifier |
| `python app.py --fake` | simulation complète (temps accéléré, aucun appel à Hetzner) |
| `python app.py --fake --fake-fail=snapshot,shutdown_timeout` | simulation avec pannes injectées |

Pannes injectables : `snapshot`, `shutdown_timeout`, `resource_unavailable`, `delete`, `probe`, `change_type`.

## Premier démarrage

1. **Importer ton snapshot Windows.** Dans le bandeau « snapshot à importer », clique sur *Importer…*, puis choisis un nom, le compte Windows et son mot de passe. Le mot de passe est stocké dans le Gestionnaire d'identifiants Windows, jamais en clair.
2. **Adopter le pare-feu `RDP-WINDOWS`.** L'application le gère alors et y ajoute l'UDP 3389, ce qui rend RDP plus fluide.
3. **Régler l'IP 178.105.239.115.** Elle est non assignée et facturée 0,50 €/mois. Dans *Dormants*, supprime-la ou fais-en l'IP fixe d'un bureau.
4. **Préparer Windows une fois.** Suis *Réglages → Préparer Windows…* : arrêt ACPI, pas d'hibernation, TRIM. Les sauvegardes seront plus rapides et plus petites.

## Utilisation au quotidien

- **Lancer** : choisis la version, l'emplacement et le type ; le prix est affiché et seuls les types compatibles et disponibles sont proposés. Ton IP est autorisée sur le pare-feu si besoin.
- **Connecter** : un clic ouvre mstsc déjà authentifié. La connexion peut aussi se faire automatiquement dès que Windows répond.
- **Sauvegarder & fermer** : arrêt propre de Windows, snapshot vérifié, suppression du serveur, rétention des anciennes versions.
- **Sauvegarder (reste allumé)** : snapshot puis redémarrage automatique.
- **Fermer sans sauvegarder** : suppression immédiate, après avoir tapé le nom du bureau pour confirmer.
- **Menu ⋯** : volumes, pare-feu, IP fixe, historique des sauvegardes, duplication, renommage, identifiants, suppression.

Plusieurs bureaux peuvent tourner en même temps ; chaque carte suit son propre état.

## Sécurités

- **Le serveur n'est jamais supprimé tant que le snapshot n'est pas confirmé « available ».** Si le snapshot échoue, le serveur est conservé et une action « Réessayer » est proposée.
- **Fermer l'application avec des serveurs allumés** affiche le coût horaire et trois choix : *Annuler*, *Quitter quand même* ou *Tout sauvegarder et quitter*. Dans ce dernier cas, l'application ne se ferme qu'une fois tout sauvegardé.
- **Si Windows refuse de s'arrêter**, trois choix sont proposés : attendre, forcer l'arrêt ou annuler. En mode fermeture, l'arrêt est forcé après un compte à rebours.
- **Rappel « Toujours besoin ? »** au bout de 3 h d'allumage (réglable).
- **Reprise après un plantage ou une coupure** : les opérations sont notées dans les labels Hetzner, et l'application propose de les terminer au démarrage suivant.
- **Identifiants RDP** : ceux d'une IP dynamique sont effacés à la fermeture du serveur, car Hetzner peut réattribuer l'IP à un inconnu.
- **Disque conservé** : lancer sur un type plus gros ne fait pas grossir le disque. Le bureau peut toujours repartir sur le type le moins cher.

## À savoir sur les coûts

- **Un serveur est facturé à l'heure entamée tant qu'il existe, même éteint.** Chaque carte indique la minute de l'heure en cours.
- **Snapshots** : 0,0143 €/Go/mois. Par défaut, les 2 dernières versions sont conservées ; les versions épinglées ne sont jamais supprimées.
- **Volumes** : 0,0572 €/Go/mois. Ils ne font pas partie du snapshot, restent facturés quand le bureau est archivé, et sont rattachés automatiquement au lancement suivant.
- **IP fixe** : 0,50 €/mois, et elle impose l'emplacement du bureau.

## Fichiers locaux

`%APPDATA%\HetznerRDP\` contient :

- `config.json` : préférences ;
- `sessions.jsonl` : historique des coûts ;
- `logs\app.log` : journal, avec le token masqué ;
- `rdp\*.rdp` : fichiers de connexion.

L'état des bureaux, lui, vit dans les labels Hetzner : il se retrouve tel quel sur un autre PC.

## Développement

```powershell
python -m pytest tests
```

| Chemin | Rôle |
|---|---|
| `rdpm/hetzner/` | API (`service.py`), attentes robustes, erreurs traduites, `fake.py` (API émulée) |
| `rdpm/ops/` | opérations longues (lancer, sauvegarder, dupliquer…) exécutées hors du thread Tk |
| `rdpm/state.py`, `offers.py`, `retention.py`, `pricing.py` | logique pure et testée |
| `rdpm/controller.py` | état de l'application, rafraîchissements, sondes RDP, fermeture |
| `rdpm/ui/` | interface customtkinter |
