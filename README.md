# Cloud Desktop Manager

Des bureaux dans le cloud **à la demande** : un bureau dort sous forme de snapshot (quelques centimes par mois) et ne
devient un serveur facturé à l'heure que pendant que tu t'en sers.

| | Aujourd'hui | Prévu |
|---|---|---|
| Fournisseurs cloud | Hetzner Cloud | d'autres fournisseurs |
| Systèmes | Windows (RDP) | Linux (visible dans *+ Nouveau bureau*, grisé « Bientôt ») |

Exemple chez Hetzner : 30 h par mois sur un cpx32 reviennent à environ 1,70 € de calcul et 0,18 € de stockage, au lieu
d'environ 35 € pour un serveur allumé en permanence.

## Installation

```powershell
pip install -r requirements.txt
```

### Clés API et projets

Une clé API d'un fournisseur (aujourd'hui Hetzner Cloud, en **lecture et écriture**) donne accès à un projet. Le
formulaire d'ajout demande le fournisseur, puis la clé ; la barre des projets affiche le logo du fournisseur de chaque
projet. L'application affiche **tous les projets ensemble** : cartes, coûts additionnés, bandeaux préfixés par le nom du projet, « Tout sauvegarder et quitter » global.

- La clé `HETZNER_TOKEN` du fichier `.env` (à côté de `app.py`) est **toujours chargée, en premier** ; à défaut, celle
  de la variable d'environnement `HETZNER_TOKEN`. Elle ne se retire pas depuis l'application.
- Les autres clés s'ajoutent avec « + Ajouter une clé API » dans la barre des projets, en haut de la fenêtre. Cochée par
  défaut, « Mémoriser » les range dans le Gestionnaire d'identifiants (rechargées au démarrage suivant). Sinon elles
  restent en mémoire pour la session.
- Chaque projet peut être renommé ; « Retirer » le fait disparaître de l'application (ses serveurs restent chez le
  fournisseur) et supprime sa clé du Gestionnaire.
- Sans aucune clé, l'application s'ouvre bloquée sur le formulaire de saisie.
- **Clé refusée** (révoquée ou supprimée), au démarrage, à la saisie ou pendant une action : le projet n'est plus
  interrogé et une fenêtre propose de supprimer la clé du Gestionnaire d'identifiants. Pour la clé du `.env`, elle
  indique de la remplacer dans le fichier.
- Les préférences et mots de passe des bureaux sont rangés par projet : deux bureaux de même nom dans deux projets ne se
  mélangent pas.

## Lancement

| Commande | Effet |
|---|---|
| double-clic sur `app.pyw` | ouvre l'application sans console |
| `python app.py` | ouvre l'application sur le vrai compte |
| `python app.py --readonly` | lit le vrai compte sans jamais rien modifier |
| `python app.py --fake` | simulation complète (temps accéléré, aucun appel au fournisseur) |
| `python app.py --fake --fake-fail=snapshot,shutdown_timeout` | simulation avec pannes injectées |
| `python app.py --fake --fake-projects=2` | simulation avec deux projets |

Pannes injectables : `snapshot`, `shutdown_timeout`, `resource_unavailable`, `delete`, `probe`, `change_type`, et pour
la création d'un Windows de référence : `build_iso_missing`, `build_ssh_timeout`, `build_prepare`, `build_image_name`,
`build_install_error`, `build_rdp_never`.

## Premier démarrage

0. **Pas encore de snapshot ?** *+ Nouveau bureau → Windows → Créer un Windows de référence…* installe Windows tout
   seul (voir ci-dessous) et en fait ton premier bureau. Les étapes 1 et 4 deviennent alors inutiles.
1. **Importer ton snapshot.** Dans le bandeau « snapshot à importer », clique sur *Importer…*, puis choisis un nom, le
   compte du bureau et son mot de passe. Le mot de passe est stocké dans le Gestionnaire d'identifiants de ton PC,
   jamais en clair.
2. **Importer ton pare-feu RDP existant** (bandeau « Pare-feu détecté »). L'application le gère alors comme liste
   d'accès commune à tous les bureaux et y ajoute l'UDP 3389, ce qui rend RDP plus fluide.
3. **Régler les ressources dormantes.** Une IP non assignée reste facturée : dans *Dormants*, supprime-la ou fais-en
   l'IP fixe d'un bureau.
4. **Préparer Windows une fois.** Suis *Réglages → Préparer Windows…* : arrêt ACPI, pas d'hibernation, TRIM. Les
   sauvegardes seront plus rapides et plus petites.

## Créer un Windows de référence

Un formulaire demande le nom du bureau, l'édition, la langue, le clavier, le fuseau horaire, l'emplacement et le type
de serveur. Ensuite tout est automatique, sans aucun clic dans l'installeur (environ 30 à 40 minutes) :

1. un serveur Ubuntu temporaire est créé, protégé par un pare-feu qui n'accepte SSH et RDP que depuis ton IP ;
2. [reinstall.sh](https://github.com/bin456789/reinstall) (GPLv3) y prépare un environnement d'installation qui
   télécharge l'ISO officielle Microsoft, injecte les pilotes VirtIO, active le Bureau à distance et pose le mot de
   passe administrateur généré ;
3. l'application ajoute ses réglages de premier démarrage : clavier, fuseau, arrêt ACPI, pas de veille prolongée,
   Gestionnaire de serveur silencieux, TRIM ;
4. Windows s'installe ; dès qu'il répond en RDP, il est arrêté proprement (le premier arrêt d'un Windows neuf
   peut prendre plusieurs minutes ; au-delà de 10 min l'arrêt est forcé, sans risque sur une installation vierge),
   snapshotté (épinglé comme version d'origine) et le serveur temporaire est supprimé. Le mot de passe est
   enregistré dans le Gestionnaire d'identifiants : le bureau se lance et se connecte en 1 clic.

Mesuré sur un cpx22 : environ 13 min jusqu'à la réponse RDP, puis le snapshot Hetzner (5 à 20 min selon leur
charge) — 36 min au total pour un Windows Server 2025 de 5,3 Go.

- **Éditions** : Windows Server 2025 ou 2022 Évaluation (ISO Microsoft, licence d'évaluation de 180 jours,
  prolongeable avec `slmgr /rearm`), ou une ISO personnalisée (lien direct + nom d'image DISM, ta licence).
- **Licence d'évaluation** : la carte du bureau affiche le compte à rebours (« Licence : 42 jours restants ») et
  une alerte à moins de 15 jours. Une tâche planifiée installée pendant la construction prolonge la licence au
  démarrage quand il reste moins de 15 jours (`slmgr /rearm`, puis un redémarrage avant que tu te connectes), tant
  que des prolongations restent. À l'expiration, rien n'est effacé, mais Windows s'éteint seul au bout d'une heure.
  L'application ne voit pas l'intérieur de Windows : elle suit la date par des labels Hetzner et applique la même
  règle que la tâche ; `slmgr /dli` dans Windows donne la valeur exacte, et le journal de la tâche est dans
  `C:\ProgramData\rdpm\eval-rearm.log`. Pour un bureau créé avant cette fonction : menu ⋯ → *Licence
  d'évaluation…* donne le bloc PowerShell à coller dans la session.
- **Disque** : le snapshot aura le disque du type choisi pour la construction. Plus il est petit, plus tu gardes le
  choix des types les moins chers au lancement.
- **Sécurité** : reinstall.sh est figé à un commit précis et vérifié par SHA-256 avant d'être exécuté ; la clé SSH est
  éphémère (`%APPDATA%\cloud-desktop-manager\build\`), supprimée avec le pare-feu et le serveur temporaires. Le mot de passe
  n'apparaît ni dans les journaux ni sur une ligne de commande locale.
- **En cas d'échec**, l'application propose de supprimer le serveur temporaire (par défaut, au bout de 2 minutes) ou de
  le conserver pour diagnostic (journal de l'installeur sur `http://<ip>/`, RDP avec le compte administrateur). Un
  serveur oublié apparaît dans *Dormants* comme serveur temporaire.
- Le client OpenSSH de Windows (`ssh.exe`, présent par défaut sur Windows 10/11) est requis sur ce poste.

## Utilisation au quotidien

- **Lancer** : choisis la version, l'emplacement et le type ; le prix est affiché et seuls les types compatibles et disponibles sont proposés. Ton IP est autorisée si besoin, pour ce bureau seulement ou pour tous les bureaux.
- **Connecter** : un clic ouvre mstsc déjà authentifié. La connexion peut aussi se faire automatiquement dès que Windows répond.
- **Sauvegarder & fermer** : arrêt propre de Windows, snapshot vérifié, suppression du serveur, rétention des anciennes versions.
- **Sauvegarder (reste allumé)** : snapshot puis redémarrage automatique.
- **Fermer sans sauvegarder** : suppression immédiate, après avoir tapé CONFIRM (majuscules ou minuscules) pour confirmer. Les autres suppressions (volume, sauvegarde, bureau) demandent la même confirmation.
- **Menu ⋯** : volumes, accès RDP, IP fixe, historique des sauvegardes, duplication, renommage, identifiants, suppression.
- Les boutons sans objet sont grisés (formulaire incomplet ou inchangé, opération en cours, bureau qui démarre…) ;
  une info-bulle explique pourquoi quand ce n'est pas évident.

## Accès RDP (pare-feu)

Seules les adresses autorisées peuvent joindre le port RDP (3389, TCP et UDP). Deux niveaux s'additionnent :

- **Tous les bureaux du projet** : la liste commune (pare-feu `RDP-WINDOWS` géré par l'application) ;
- **Ce bureau uniquement** : une liste propre à chaque bureau (pare-feu `rdpm-<bureau>-rdp`), créée à la première
  adresse ajoutée, appliquée à son serveur à chaque lancement et supprimée avec le bureau.

Exemple : liste commune vide, IP de la maison sur le bureau « Perso », IP du bureau sur le bureau « Travail ».
Le bouton *Accès RDP* de l'en-tête montre toutes les listes ; *⋯ → Accès RDP…* sur une carte montre celles du bureau.
Une même adresse ne peut pas être saisie deux fois : ni deux fois dans une liste, ni sur un bureau si elle (ou un réseau
qui la contient, ex. `/24`) est déjà dans la liste commune, ni dans la liste commune si un bureau l'a déjà.

Plusieurs bureaux peuvent tourner en même temps ; chaque carte suit son propre état.

## Sécurités

- **Le serveur n'est jamais supprimé tant que le snapshot n'est pas confirmé « available ».** Si le snapshot échoue, le serveur est conservé et une action « Réessayer » est proposée.
- **Fermer l'application avec des serveurs allumés** affiche le coût horaire et trois choix : *Annuler*, *Quitter quand même* ou *Tout sauvegarder et quitter*. Dans ce dernier cas, l'application ne se ferme qu'une fois tout sauvegardé.
- **Si Windows refuse de s'arrêter**, trois choix sont proposés : attendre, forcer l'arrêt ou annuler. En mode fermeture, l'arrêt est forcé après un compte à rebours.
- **Rappel « Toujours besoin ? »** au bout de 3 h d'allumage (réglable).
- **Reprise après un plantage ou une coupure** : les opérations sont notées dans les labels du fournisseur, et l'application propose de les terminer au démarrage suivant.
- **Identifiants RDP** : ceux d'une IP dynamique sont effacés à la fermeture du serveur, car le fournisseur peut réattribuer l'IP à un inconnu.
- **Disque conservé** : lancer sur un type plus gros ne fait pas grossir le disque. Le bureau peut toujours repartir sur le type le moins cher.

## À savoir sur les coûts

- **Un serveur est facturé à l'heure entamée tant qu'il existe, même éteint.** Chaque carte indique la minute de l'heure en cours.
- **Snapshots** : 0,0143 €/Go/mois. Par défaut, les 2 dernières versions sont conservées ; les versions épinglées ne sont jamais supprimées.
- **Volumes** : 0,0572 €/Go/mois. Ils ne font pas partie du snapshot, restent facturés quand le bureau est archivé, et sont rattachés automatiquement au lancement suivant.
- **IP fixe** : facturée en permanence (0,50 €/mois chez Hetzner), et elle impose l'emplacement du bureau. Rien n'est
  réservé tant que tu ne cliques pas sur *Activer l'IP fixe*.

## Fichiers locaux

`%APPDATA%\cloud-desktop-manager\` (nom historique, conservé pour garder préférences et clés existantes) contient :

- `config.json` : préférences ;
- `sessions.jsonl` : historique des coûts ;
- `logs\app.log` : journal, avec le token et les mots de passe générés masqués ;
- `rdp\*.rdp` : fichiers de connexion ;
- `build\` : clés SSH éphémères d'une création de Windows en cours (supprimées à la fin).

L'état des bureaux, lui, vit dans les labels du fournisseur (labels Hetzner) : il se retrouve tel quel sur un autre PC.

## Développement

```powershell
python -m pytest tests
```

| Chemin | Rôle |
|---|---|
| `rdpm/providers.py` | fournisseurs connus de l'interface (nom, logo, aide pour la clé API) |
| `rdpm/hetzner/` | API Hetzner (`service.py`), attentes robustes, erreurs traduites, `fake.py` (API émulée) |
| `rdpm/ops/` | opérations longues (lancer, sauvegarder, dupliquer, construire…) exécutées hors du thread Tk |
| `rdpm/build/`, `rdpm/remote.py` | création d'un Windows de référence : catalogue, scripts distants, lecture du journal, SSH |
| `rdpm/state.py`, `offers.py`, `retention.py`, `pricing.py` | logique pure et testée |
| `rdpm/controller.py` | état de l'application, rafraîchissements, sondes RDP, fermeture |
| `rdpm/ui/` | interface customtkinter |
| `rdpm/ui/assets/`, `rdpm/ui/icons.py` | icônes PNG (Windows, Linux, fournisseur, application), lues par Tk sans Pillow |
| `tools/make_icons.py` | régénère les icônes (Python pur, aucune dépendance) |
