# Cloud Desktop Manager

Des bureaux dans le cloud **à la demande** : un bureau dort sous forme de snapshot (quelques centimes par mois) et ne
devient un serveur facturé à l'heure que pendant que tu t'en sers.

| | Aujourd'hui | Prévu |
|---|---|---|
| Fournisseurs cloud | Hetzner Cloud | d'autres fournisseurs |
| Systèmes | Windows et Linux (Ubuntu 26.04 LTS, Debian 13 — bureau XFCE), connexion RDP en 1 clic | |

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
`build_install_error`, `build_rdp_never` ; pour un bureau Linux : `build_prepare`, `build_ssh_timeout`,
`linux_install_error`, `linux_unit_killed`, `linux_rdp_never`.

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

## Créer un bureau Linux

*+ Nouveau bureau → Linux → Créer un bureau Linux…* : nom, distribution (**Ubuntu 26.04 LTS** ou **Debian 13**),
identifiant du compte, langue, fuseau horaire, emplacement et type. Tout est automatique (environ 10 à 20 minutes,
plus le snapshot) ; la carte affiche l'étape en cours (« Installation 4/7 : compilation d'xrdp… »).

**Pourquoi ce choix** : un bureau distant sur un serveur sans carte graphique doit éviter tout rendu OpenGL
logiciel (c'est ce qui rend Cinnamon ou GNOME saccadés à distance) et envoyer une image compressée efficacement.

- **Bureau XFCE**, compositeur désactivé : léger, net, réactif ; thème Greybird, icônes elementary, polices Noto.
- **xrdp 0.10.6.1 compilé avec x264** : l'image est envoyée en **H.264** (RDP GFX), bien plus fluide pour la vidéo
  et le défilement que les versions des dépôts (Ubuntu 26.04 n'a que xrdp 0.10.1, sans H.264). Sources officielles
  (neutrinolabs) figées et vérifiées par SHA-256 : xrdp, xorgxrdp 0.10.5, pipewire-module-xrdp (son).
- **Connexion identique à Windows** : mstsc, 1 clic, mot de passe enregistré, **ouverture de session automatique**,
  pas d'alerte de certificat (son empreinte est préenregistrée), **son**, presse-papiers, fenêtre redimensionnable ;
  on retrouve sa session là où on l'a laissée. Le clavier suit celui du PC.
- **Firefox** : dépôt APT officiel de Mozilla sur Ubuntu (pas de snap), Firefox ESR sur Debian.
- **Sauvegarder & fermer** fonctionne comme pour Windows : le bouton d'arrêt ACPI éteint toujours le système.
- **Sécurité** : RDP en TLS uniquement, compte root refusé en RDP, SSH sans mot de passe (et port 22 fermé par les
  listes d'accès), clé SSH temporaire retirée avant le snapshot, mises à jour de sécurité automatiques.
- **Volumes** : créés déjà formatés (ext4), visibles dans le gestionnaire de fichiers ; l'aide affichée donne la
  ligne `/etc/fstab` pour les monter automatiquement.
- **Conseils** : 4 vCPU ou plus pour la vidéo (l'encodage H.264 se fait sur le serveur). Après un lancement, les
  mises à jour automatiques peuvent occuper le système quelques minutes.
- **Mettre xrdp à jour** : depuis un terminal du bureau, modifier version et SHA-256 en tête de
  `/usr/local/share/rdpm/build-xrdp.sh`, puis `sudo bash /usr/local/share/rdpm/build-xrdp.sh`.
- **En cas d'échec** : fin du journal affichée ; serveur conservable pour diagnostic
  (`/var/log/rdpm-install.log`, `journalctl -u rdpm-install`).
- **Importer** un snapshot ou serveur Linux existant : onglet Linux de *+ Nouveau bureau* (xrdp doit y être installé).

## Logiciels prêts à l'emploi

À la création d'un bureau (Windows ou Linux), une **liste à cocher** propose les outils de l'IA agentique et du
développement ; le préréglage **Recommandé** est coché par défaut (Git, GitHub CLI, Python + uv, Node.js,
VS Code, Claude Code, Codex CLI, Chrome), avec **Tout** et **Aucun**. Le formulaire affiche le temps et la place
ajoutés, et le coût du stockage en tient compte.

| Catégorie | Logiciels |
|---|---|
| Assistants IA | Claude Desktop, ChatGPT (avec Codex) |
| Agents IA autonomes | OpenClaw, Hermes Agent (Nous Research) |
| Agents de code | Claude Code, Codex CLI, GitHub Copilot CLI, OpenCode, Cursor CLI, Google Antigravity CLI |
| Éditeurs | Visual Studio Code, Cursor, Google Antigravity (Windows) |
| Langages | Python + uv, Node.js LTS (npm, pnpm), Bun, Go, Rust, Java (JDK LTS), .NET SDK |
| Outils | Git, GitHub CLI, Docker (Linux), outils en ligne de commande (ripgrep, fd, jq, fzf…), Windows Terminal + PowerShell 7 |
| IA locale | Ollama (sans GPU : petits modèles seulement) |
| Navigateur | Google Chrome |

- **Toujours à jour** : chaque logiciel vient de son canal officiel (dépôt APT de l'éditeur à clé vérifiée, winget,
  npm, installeur officiel) dans sa dernière version. Les dépôts APT suivent les mises à jour automatiques ; le reste
  est mis à jour chaque semaine (minuteur systemd sous Linux) ou par le raccourci « Mettre à jour les logiciels ».
- **Dépendances** ajoutées d'office (OpenClaw → Node.js…), avec la mention dans la liste.
- **Comptes et clés d'API** : jamais demandés par l'application ; tu te connectes dans chaque logiciel à son premier
  lancement. Le fichier **« Premiers pas »** du Bureau donne la première commande de chacun (`claude`, `codex`,
  `openclaw onboard --install-daemon`, `hermes setup`…).
- **Un échec n'arrête rien** : le bureau est créé, le récapitulatif liste ce qui manque, à réessayer ensuite.
- **Indisponibles et grisés, avec la raison** : Docker sous Windows et Claude Cowork (pas de virtualisation imbriquée
  chez Hetzner), l'éditeur Antigravity sous Linux (pas de paquet installable automatiquement).

**Plus tard, deux chemins :**

- **Depuis l'application** (bureau lancé) : menu ⋯ → **Logiciels…**. Les logiciels déjà présents sont cochés et
  grisés ; « Installer (n) » ou « Tout mettre à jour ». L'installation se fait pendant que tu travailles (carte
  « Logiciels en cours… ») ; **Sauvegarder & fermer** la conserve dans la sauvegarde.
- **Depuis le bureau** : raccourci **Logiciels** (menu Applications sous Linux, Bureau sous Windows), même liste.

**Sécurité de l'installation depuis l'application** : une **clé d'administration** propre à ce PC
(`%APPDATA%\cloud-desktop-manager\keys\admin_ed25519`) est posée sur chaque bureau construit (root sous Linux ;
OpenSSH activé sous Windows, connexion par clé seulement). Le **port SSH reste fermé** : chaque opération crée un
pare-feu temporaire limité à ton IP publique, puis le retire et le supprime (même en cas d'annulation ; un reste
éventuel est nettoyé au rafraîchissement suivant). Sous Windows, l'installation tourne en tâche planifiée sous ton
compte (winget en a besoin) ; la tâche, qui garde le mot de passe, est supprimée à la fin. Un bureau créé avant
cette fonction (ou depuis un autre PC) : « Logiciels… » affiche une commande à coller une fois dans le bureau.

Journaux sur le bureau : `/var/log/rdpm-apps.log` (Linux), `C:\ProgramData\CloudDesktop\apps.log` (Windows).
En ligne de commande sous Linux : `rdpm-apps list`, `sudo rdpm-apps install codex openclaw`, `sudo rdpm-apps update`.

## Utilisation au quotidien

- **Lancer** : choisis la version, l'emplacement et le type ; le prix est affiché et seuls les types compatibles et disponibles sont proposés. Ton IP est autorisée si besoin, pour ce bureau seulement ou pour tous les bureaux.
- **Connecter** : un clic ouvre mstsc déjà authentifié. La connexion peut aussi se faire automatiquement dès que Windows répond.
- **Sauvegarder & fermer** : arrêt propre de Windows, snapshot vérifié, suppression du serveur, rétention des anciennes versions.
- **Sauvegarder (reste allumé)** : snapshot puis redémarrage automatique.
- **Fermer sans sauvegarder** : suppression immédiate, après avoir tapé CONFIRM (majuscules ou minuscules) pour confirmer. Les autres suppressions (volume, sauvegarde, bureau) demandent la même confirmation.
- **Menu ⋯** : logiciels, volumes, accès RDP, IP fixe, historique des sauvegardes, duplication, renommage, identifiants, suppression.
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
- `build\` : clés SSH éphémères d'une création en cours (supprimées à la fin) ;
- `keys\admin_ed25519` : clé d'administration des bureaux (installation de logiciels depuis l'application) ;
  à sauvegarder si tu changes de PC, sinon « Logiciels… » propose de réactiver l'accès.

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
| `rdpm/build/`, `rdpm/remote.py` | création d'un bureau de référence : catalogues Windows et Linux (`linux.py`), scripts distants (`scripts.py`, `linux_scripts.py`), lecture du journal, SSH |
| `rdpm/state.py`, `offers.py`, `retention.py`, `pricing.py` | logique pure et testée |
| `rdpm/controller.py` | état de l'application, rafraîchissements, sondes RDP, fermeture |
| `rdpm/ui/` | interface customtkinter |
| `rdpm/ui/assets/`, `rdpm/ui/icons.py` | icônes PNG (Windows, Linux, fournisseur, application), lues par Tk sans Pillow |
| `tools/make_icons.py` | régénère les icônes (Python pur, aucune dépendance) |
