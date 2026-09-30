"""`rdpm-apps` : l'outil d'installation des logiciels du catalogue sur un bureau Linux (bash).

Généré depuis `catalog.APPS` et déposé dans /usr/local/sbin/rdpm-apps. Il sert aux trois chemins :

- construction du bureau (étape « Logiciels » du script d'installation) ;
- installation depuis l'application (SSH root avec la clé d'administration, unité systemd détachée) ;
- lanceur « Logiciels » du menu du bureau (liste à cocher zenity, installation avec sudo dans un terminal).

Chaque logiciel s'installe dans un sous-shell isolé : un échec est signalé (« RDPM-APP fail ») sans arrêter
les suivants. Les versions viennent toujours des canaux officiels au moment de l'installation ; les dépôts
APT ajoutés sont suivis par les mises à jour automatiques, le reste par un minuteur hebdomadaire.
"""

from __future__ import annotations

import base64

from ..build.scripts import TAG_PREFIX, sh_quote
from . import catalog

TOOL_PATH = "/usr/local/sbin/rdpm-apps"
CONF_PATH = "/etc/rdpm/apps.conf"
APPS_LOG = "/var/log/rdpm-apps.log"
APPS_UNIT = "rdpm-apps"

_TOOL = r"""#!/bin/bash
# rdpm-apps — logiciels prêts à l'emploi (Cloud Desktop Manager, catalogue @@VERSION@@)
#
#   sudo rdpm-apps install <clé>…   installe (les dépendances sont ajoutées)
#   rdpm-apps status                 clés installées
#   sudo rdpm-apps update            met à jour les logiciels installés hors APT (APT : automatique)
#   rdpm-apps list                   catalogue (clé, nom, catégorie, description)
#   rdpm-apps gui                    liste à cocher (menu « Logiciels »)
set -uo pipefail

CONF=@@CONF@@
[ -r "$CONF" ] && . "$CONF"
if [ -z "${DESKTOP_USER:-}" ]; then
    DESKTOP_USER=$(getent group sudo | cut -d: -f4 | tr ',' '\n' | grep -v '^$' | head -n1)
fi
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 NEEDRESTART_MODE=l
APT=(apt-get -y -q -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold
     -o Dpkg::Options::=--force-confdef)
KEYRINGS=/etc/apt/keyrings
. /etc/os-release
CODENAME=${VERSION_CODENAME:-}
ARCH=$(dpkg --print-architecture)

ORDER=(@@ORDER@@)        # ordre d'installation (dépendances d'abord)
DISPLAY=(@@DISPLAY@@)    # ordre d'affichage (par catégorie)
declare -A NAME=(@@NAMES@@)
declare -A CATEGORY=(@@CATEGORIES@@)
declare -A BLURB=(@@BLURBS@@)
declare -A REQUIRES=(@@REQUIRES@@)
declare -A FIRST=(@@FIRST@@)

# --- outils communs --------------------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }
user_home() { getent passwd "$DESKTOP_USER" | cut -d: -f6; }
user_has() { [ -x "$(user_home)/$1" ]; }
fetch() { curl -fsSL --retry 5 --retry-delay 3 --connect-timeout 20 "$1" -o "$2"; }
apt_install() { "${APT[@]}" install --no-install-recommends "$@"; }
available() { apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ && $2 != "(none)" {ok=1} END {exit !ok}'; }

as_user() {   # commande exécutée par le compte du bureau, dans un shell de connexion (PATH, profil)
    local home
    home=$(user_home)
    runuser -u "$DESKTOP_USER" -- env -i HOME="$home" USER="$DESKTOP_USER" LOGNAME="$DESKTOP_USER" \
        SHELL=/bin/bash LANG="${LANG:-C.UTF-8}" PATH=/usr/local/bin:/usr/bin:/bin \
        bash -lc "cd \"\$HOME\" && $1"
}

apt_repo() {   # nom url_clé empreinte « ligne deb » (@KEYRING@ remplacé) — clé vérifiée avant usage
    local name=$1 key_url=$2 fpr=$3 line=$4 tmp keyring got
    install -d -m 0755 "$KEYRINGS"
    tmp=$(mktemp)
    fetch "$key_url" "$tmp"
    if grep -q -- '-----BEGIN PGP' "$tmp"; then keyring="$KEYRINGS/rdpm-$name.asc"; else keyring="$KEYRINGS/rdpm-$name.gpg"; fi
    if [ -n "$fpr" ]; then
        got=$(gpg --show-keys --with-colons "$tmp" 2>/dev/null | awk -F: '/^fpr:/ {print $10}')
        if ! grep -qx "$fpr" <<<"$got"; then
            echo "Clé du dépôt $name inattendue : $got"
            rm -f "$tmp"
            return 1
        fi
    fi
    install -m 0644 "$tmp" "$keyring"
    rm -f "$tmp"
    printf '%s\n' "${line//@KEYRING@/$keyring}" > "/etc/apt/sources.list.d/rdpm-$name.list"
    local site
    site=$(sed -E 's#.*https?://([^/ ]+).*#\1#' <<<"$line")
    printf 'Unattended-Upgrade::Origins-Pattern { "site=%s"; };\n' "$site" > "/etc/apt/apt.conf.d/52rdpm-$name"
    "${APT[@]}" update
}

npm_global() { have npm || { echo "Node.js absent"; return 1; }; npm install -g --no-fund --no-audit --loglevel=error "$@"; }

npm_allow_scripts() {   # npm ≥ 11.16 n'exécute plus les scripts d'installation sans autorisation explicite
    local v
    v=$(npm --version 2>/dev/null || echo 0)
    if [ "$(printf '%s\n' 11.16.0 "$v" | sort -V | head -n1)" = 11.16.0 ]; then echo "--allow-scripts=$1"; fi
}

sha256_ok() { echo "$1  $2" | sha256sum -c - >/dev/null; }

menu_entry() {   # clé nom commande [catégories] — ouvre un terminal qui lance l'outil
    local key=$1 name=$2 cmd=$3 cats=${4:-Development;}
    cat > "/usr/share/applications/rdpm-$key.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$name
Comment=${BLURB[$key]:-}
Exec=xfce4-terminal --title "$name" -x bash -lc "$cmd; exec bash"
Icon=utilities-terminal
Terminal=false
Categories=$cats
EOF
}

install_uv() {   # binaire officiel (GitHub astral-sh/uv), empreinte SHA-256 publiée vérifiée
    local tmp file base=https://github.com/astral-sh/uv/releases/latest/download
    file="uv-$(uname -m)-unknown-linux-gnu.tar.gz"
    tmp=$(mktemp -d)
    fetch "$base/$file" "$tmp/$file"
    fetch "$base/$file.sha256" "$tmp/sum"
    sha256_ok "$(awk '{print $1}' "$tmp/sum")" "$tmp/$file"
    tar -xzf "$tmp/$file" -C "$tmp"
    install -m 0755 "$tmp"/uv-*/uv "$tmp"/uv-*/uvx /usr/local/bin/
    rm -rf "$tmp"
    /usr/local/bin/uv --version
}

install_node() {   # dernière version LTS officielle (nodejs.org), SHASUMS256 vérifié
    local tmp version file arch
    case "$ARCH" in amd64) arch=x64 ;; arm64) arch=arm64 ;; *) echo "architecture $ARCH non prise en charge"; return 1 ;; esac
    tmp=$(mktemp -d)
    fetch https://nodejs.org/dist/index.json "$tmp/index.json"
    version=$(python3 -c 'import json, sys; print(next(r["version"] for r in json.load(open(sys.argv[1])) if r["lts"]))' "$tmp/index.json")
    if have node && [ "$(node --version)" = "$version" ]; then rm -rf "$tmp"; echo "Node.js $version (à jour)"; return 0; fi
    file="node-$version-linux-$arch.tar.xz"
    fetch "https://nodejs.org/dist/$version/$file" "$tmp/$file"
    fetch "https://nodejs.org/dist/$version/SHASUMS256.txt" "$tmp/SHASUMS256.txt"
    (cd "$tmp" && grep " $file\$" SHASUMS256.txt | sha256sum -c - >/dev/null)
    rm -rf /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/corepack /usr/local/include/node
    tar -xJf "$tmp/$file" -C /usr/local --strip-components=1 --no-same-owner \
        --exclude='*/CHANGELOG.md' --exclude='*/LICENSE' --exclude='*/README.md'
    rm -rf "$tmp"
    echo "Node.js $version"
}

install_go() {   # dernière version officielle (go.dev), SHA-256 publié vérifié
    local tmp file sha
    tmp=$(mktemp -d)
    fetch 'https://go.dev/dl/?mode=json' "$tmp/dl.json"
    read -r file sha < <(python3 -c 'import json, sys
r = json.load(open(sys.argv[1]))[0]
f = next(f for f in r["files"] if f["os"] == "linux" and f["arch"] == sys.argv[2] and f["kind"] == "archive")
print(f["filename"], f["sha256"])' "$tmp/dl.json" "$ARCH")
    if [ -x /usr/local/go/bin/go ] && [ "$(/usr/local/go/bin/go env GOVERSION).linux-$ARCH.tar.gz" = "$file" ]; then
        rm -rf "$tmp"; echo "Go à jour"; return 0
    fi
    fetch "https://go.dev/dl/$file" "$tmp/$file"
    sha256_ok "$sha" "$tmp/$file"
    rm -rf /usr/local/go
    tar -xzf "$tmp/$file" -C /usr/local
    printf '%s\n' 'export PATH="$PATH:/usr/local/go/bin:$HOME/go/bin"' > /etc/profile.d/rdpm-go.sh
    rm -rf "$tmp"
}

install_rustup() {   # rustup-init officiel (static.rust-lang.org), SHA-256 vérifié, pour le compte du bureau
    local tmp triple
    triple="$(uname -m)-unknown-linux-gnu"
    tmp=$(mktemp -d)
    chmod 0755 "$tmp"
    fetch "https://static.rust-lang.org/rustup/dist/$triple/rustup-init" "$tmp/rustup-init"
    fetch "https://static.rust-lang.org/rustup/dist/$triple/rustup-init.sha256" "$tmp/sum"
    sha256_ok "$(awk '{print $1}' "$tmp/sum")" "$tmp/rustup-init"
    chmod 0755 "$tmp/rustup-init"
    as_user "'$tmp/rustup-init' -y --profile default </dev/null"
    rm -rf "$tmp"
}

install_dotnet() {   # paquets de la distribution (Ubuntu) ou dépôt Microsoft (Debian)
    local pkg deb
    pkg=$(apt-cache search --names-only '^dotnet-sdk-[0-9]+\.[0-9]+$' | awk '{print $1}' | sort -V | tail -n1)
    if [ -z "$pkg" ] && [ "$ID" = debian ]; then
        deb=$(mktemp --suffix=.deb)
        chmod 0644 "$deb"
        fetch "https://packages.microsoft.com/config/debian/${VERSION_ID}/packages-microsoft-prod.deb" "$deb"
        apt_install "$deb"
        rm -f "$deb"
        printf 'Unattended-Upgrade::Origins-Pattern { "site=packages.microsoft.com"; };\n' \
            > /etc/apt/apt.conf.d/52rdpm-microsoft-prod
        "${APT[@]}" update
        pkg=$(apt-cache search --names-only '^dotnet-sdk-[0-9]+\.[0-9]+$' | awk '{print $1}' | sort -V | tail -n1)
    fi
    [ -n "$pkg" ] || { echo "SDK .NET introuvable pour $ID $VERSION_ID"; return 1; }
    apt_install "$pkg"
}

install_docker() {   # dépôt officiel Docker si la version de la distribution y est, sinon paquets de la distribution
    if curl -fsI "https://download.docker.com/linux/$ID/dists/$CODENAME/Release" >/dev/null 2>&1; then
        apt_repo docker "https://download.docker.com/linux/$ID/gpg" 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 \
            "deb [arch=$ARCH signed-by=@KEYRING@] https://download.docker.com/linux/$ID $CODENAME stable"
        apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    else
        echo "Dépôt Docker pas encore publié pour $CODENAME : paquets de la distribution"
        if available docker-compose-v2; then apt_install docker.io docker-compose-v2; else apt_install docker.io docker-compose; fi
    fi
    usermod -aG docker "$DESKTOP_USER"
}

# --- recettes (générées depuis le catalogue) --------------------------------------------------------------
@@RECIPES@@

# --- commandes -----------------------------------------------------------------------------------------------
known() { [ -n "${NAME[$1]:-}" ]; }
installed() { declare -F "check_$1" >/dev/null && "check_$1" >/dev/null 2>&1; }

expand() {   # clés demandées + dépendances, dans l'ordre du catalogue
    local -A want=()
    local key dep changed=1
    for key in "$@"; do known "$key" && want[$key]=1; done
    while [ $changed = 1 ]; do
        changed=0
        for key in "${!want[@]}"; do
            for dep in ${REQUIRES[$key]:-}; do
                if [ -z "${want[$dep]:-}" ]; then want[$dep]=1; changed=1; fi
            done
        done
    done
    for key in "${ORDER[@]}"; do [ -n "${want[$key]:-}" ] && echo "$key"; done
}

desktop_dir() {
    local dir
    as_user "xdg-user-dirs-update >/dev/null 2>&1 || true" >/dev/null 2>&1 || true
    dir=$(as_user "xdg-user-dir DESKTOP" 2>/dev/null || true)
    [ -n "$dir" ] && [ "$dir" != "$(user_home)" ] || dir="$(user_home)/Desktop"
    echo "$dir"
}

write_first_steps() {
    local dir file key any=0
    dir=$(desktop_dir)
    install -d -o "$DESKTOP_USER" -g "$DESKTOP_USER" "$dir"
    file="$dir/Premiers pas.txt"
    {
        echo "Cloud Desktop Manager — logiciels installés sur ce bureau"
        echo "=========================================================="
        echo
        echo "Ajouter des logiciels : menu Applications → « Logiciels » (ou depuis l'application Cloud Desktop Manager)."
        echo "Tout mettre à jour : menu Applications → « Mettre à jour les logiciels »."
        echo "Comptes et clés d'API : connecte-toi dans chaque logiciel à son premier lancement."
        echo
        for key in "${DISPLAY[@]}"; do
            if installed "$key"; then
                any=1
                printf '• %s — %s\n' "${NAME[$key]}" "${BLURB[$key]}"
                [ -n "${FIRST[$key]:-}" ] && printf '    %s\n' "${FIRST[$key]}"
            fi
        done
        [ $any = 1 ] || echo "(aucun logiciel du catalogue pour l'instant)"
    } > "$file"
    chown "$DESKTOP_USER:$DESKTOP_USER" "$file"
}

install_tool_entries() {
    cat > /usr/share/applications/rdpm-apps.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Logiciels
Comment=Installer des logiciels prêts à l'emploi (IA, développement)
Exec=rdpm-apps gui
Icon=system-software-install
Terminal=false
Categories=System;Settings;
EOF
    cat > /usr/share/applications/rdpm-apps-update.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Mettre à jour les logiciels
Comment=Mettre à jour les logiciels installés depuis le catalogue
Exec=xfce4-terminal --title "Mise à jour des logiciels" -x bash -c "sudo rdpm-apps update; echo; read -rp 'Terminé — Entrée pour fermer '"
Icon=system-software-update
Terminal=false
Categories=System;Settings;
EOF
    if [ -d /run/systemd/system ] || [ -d /etc/systemd/system ]; then
        cat > /etc/systemd/system/rdpm-apps-update.service <<EOF
[Unit]
Description=Mise à jour des logiciels du catalogue (Cloud Desktop Manager)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=@@TOOL_PATH@@ update
Nice=10
EOF
        cat > /etc/systemd/system/rdpm-apps-update.timer <<'EOF'
[Unit]
Description=Mise à jour hebdomadaire des logiciels du catalogue

[Timer]
OnCalendar=weekly
Persistent=true
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
EOF
        if [ -d /run/systemd/system ]; then
            systemctl daemon-reload
            systemctl enable rdpm-apps-update.timer >/dev/null 2>&1 || true
        else
            ln -sf /etc/systemd/system/rdpm-apps-update.timer /etc/systemd/system/timers.target.wants/ 2>/dev/null || true
        fi
    fi
}

cmd_install() {
    [ "$(id -u)" = 0 ] || { echo "À lancer avec sudo."; exit 1; }
    local keys=() key i=0 rc ok=() failed=()
    mapfile -t keys < <(expand "$@")
    if ! have curl || ! have gpg; then apt_install curl ca-certificates gnupg; fi
    install_tool_entries
    for key in "${keys[@]}"; do
        i=$((i + 1))
        echo "RDPM-APP start $i/${#keys[@]} $key ${NAME[$key]}"
        if installed "$key"; then
            echo "RDPM-APP ok $key (déjà installé)"
            ok+=("$key")
            continue
        fi
        ( set -Eeo pipefail; trap 'echo "erreur ligne $LINENO (code $?)"' ERR; "install_$key" )
        rc=$?
        if [ $rc = 0 ] && installed "$key"; then
            echo "RDPM-APP ok $key"
            ok+=("$key")
        else
            echo "RDPM-APP fail $key (code $rc)"
            failed+=("$key")
        fi
    done
    write_first_steps || true
    echo "RDPM-APPS-DONE ok=${ok[*]:-} fail=${failed[*]:-}"
    [ ${#failed[@]} = 0 ]
}

cmd_status() {
    local key out=()
    for key in "${ORDER[@]}"; do installed "$key" && out+=("$key"); done
    echo "RDPM-APPS-STATUS ${out[*]:-}"
}

cmd_update() {
    [ "$(id -u)" = 0 ] || { echo "À lancer avec sudo."; exit 1; }
    local key
    "${APT[@]}" update || true
    "${APT[@]}" upgrade || true
    for key in "${ORDER[@]}"; do
        if installed "$key" && declare -F "update_$key" >/dev/null; then
            echo "RDPM-APP update $key ${NAME[$key]}"
            ( set -Eeo pipefail; "update_$key" ) || echo "RDPM-APP fail $key (mise à jour)"
        fi
    done
    write_first_steps || true
    echo "RDPM-APPS-DONE update"
}

cmd_list() {
    local key
    for key in "${DISPLAY[@]}"; do
        printf '%s|%s|%s|%s|%s\n' "$key" "${NAME[$key]}" "${CATEGORY[$key]}" "${BLURB[$key]}" \
            "$(installed "$key" && echo installé || echo -)"
    done
}

cmd_gui() {
    local rows=() key state picked
    for key in "${DISPLAY[@]}"; do
        if installed "$key"; then state="installé"; else state=""; fi
        rows+=(FALSE "$key" "${NAME[$key]}" "${CATEGORY[$key]}" "$state" "${BLURB[$key]}")
    done
    picked=$(zenity --list --checklist --title "Logiciels" --width 980 --height 640 \
        --text "Coche les logiciels à installer (les dépendances sont ajoutées automatiquement). Ton mot de passe sera demandé." \
        --column "" --column "Clé" --column "Logiciel" --column "Catégorie" --column "État" --column "Description" \
        --hide-column 2 --print-column 2 --separator " " "${rows[@]}" 2>/dev/null) || exit 0
    [ -n "$picked" ] || exit 0
    exec xfce4-terminal --title "Installation de logiciels" -x bash -c \
        "sudo @@TOOL_PATH@@ install $picked; echo; read -rp 'Terminé — Entrée pour fermer '"
}

case "${1:-}" in
    install) shift; cmd_install "$@" ;;
    status) cmd_status ;;
    update) cmd_update ;;
    list) cmd_list ;;
    gui) cmd_gui ;;
    *) sed -n '3,8p' "$0"; exit 2 ;;
esac
"""


def _assoc(values: dict[str, str]) -> str:
    return " ".join(f"[{k}]={sh_quote(v)}" for k, v in values.items())


def render_tool() -> str:
    apps = catalog.for_os(catalog.OS_LINUX)
    recipes = []
    for app in apps:
        r: catalog.LinuxRecipe = app.linux
        recipes.append(f"install_{_fn(app.key)}() {{\n{_indent(r.install)}\n}}")
        recipes.append(f"check_{_fn(app.key)}() {{\n{_indent(r.check)}\n}}")
        if r.update:
            recipes.append(f"update_{_fn(app.key)}() {{\n{_indent(r.update)}\n}}")
    values = {
        "VERSION": catalog.VERSION, "CONF": sh_quote(CONF_PATH), "TOOL_PATH": TOOL_PATH,
        "ORDER": " ".join(k for k in catalog.install_order() if catalog.APPS_BY_KEY[k].available(catalog.OS_LINUX)),
        "DISPLAY": " ".join(a.key for a in apps),
        "NAMES": _assoc({a.key: a.name for a in apps}),
        "CATEGORIES": _assoc({a.key: a.category for a in apps}),
        "BLURBS": _assoc({a.key: a.blurb for a in apps}),
        "REQUIRES": _assoc({a.key: " ".join(d for d in a.requires) for a in apps if a.requires}),
        "FIRST": _assoc({a.key: a.first_step for a in apps if a.first_step}),
        "RECIPES": "\n\n".join(recipes),
    }
    text = _TOOL
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", value)
    assert "@@" not in text, "variable de gabarit non remplacée"
    return text


def _fn(key: str) -> str:
    return key   # les clés du catalogue sont déjà des identifiants bash valides (lettres, chiffres, _)


def _indent(body: str) -> str:
    return "\n".join(("    " + line) if line.strip() else "" for line in body.strip("\n").splitlines())


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def deploy_snippet(desktop_user: str | None) -> str:
    """Lignes bash (root) qui déposent l'outil et sa configuration ; réutilisées par la construction et par
    l'installation depuis l'application (l'outil du bureau est remplacé par celui de cette version)."""
    conf = f"printf 'DESKTOP_USER=%s\\n' {sh_quote(desktop_user)} > {CONF_PATH}\n" if desktop_user else ""
    return (f"install -d -m 0755 /etc/rdpm /usr/local/sbin\n{conf}"
            f"echo {_b64(render_tool())} | base64 -d > {TOOL_PATH}.new\n"
            f"chmod 0755 {TOOL_PATH}.new && mv -f {TOOL_PATH}.new {TOOL_PATH}\n"
            f"ln -sf {TOOL_PATH} /usr/local/bin/rdpm-apps\n")


# --- scripts pilotés en SSH (installation depuis l'application) ---------------------------------------------
def apps_start(keys: list[str], mode: str = "install") -> str:
    """Dépose l'outil à jour puis lance l'installation (ou la mise à jour) détachée : elle survit à une
    coupure SSH et se suit avec APPS_STATUS."""
    args = " ".join(sh_quote(k) for k in keys) if mode == "install" else ""
    return f"""{TAG_PREFIX}apps-start
set -eu
{deploy_snippet(None)}if systemctl is-active --quiet {APPS_UNIT}; then echo RDPM_BUSY; exit 0; fi
systemctl reset-failed {APPS_UNIT} >/dev/null 2>&1 || true
: > {APPS_LOG}
chmod 0600 {APPS_LOG}
systemd-run --unit={APPS_UNIT} --service-type=exec --quiet \\
    -p StandardOutput=append:{APPS_LOG} -p StandardError=append:{APPS_LOG} \\
    {TOOL_PATH} {mode} {args}
echo RDPM_STARTED
"""


APPS_STATUS = f"""{TAG_PREFIX}apps-status
state=$(systemctl show {APPS_UNIT} -p ActiveState --value 2>/dev/null || true)
echo "RDPM_UNIT ${{state:-unknown}}"
echo RDPM_MARKS_BEGIN
grep -a '^RDPM-APP' {APPS_LOG} 2>/dev/null | tail -n 80 || true
echo RDPM_TAIL_BEGIN
tail -n 12 {APPS_LOG} 2>/dev/null || true
"""

APPS_STOP = f"""{TAG_PREFIX}apps-stop
systemctl stop {APPS_UNIT} >/dev/null 2>&1 || true
echo RDPM_STOPPED
"""


def apps_inventory() -> str:
    """Clés réellement installées (l'outil du bureau peut être ancien : on dépose d'abord celui-ci)."""
    return f"""{TAG_PREFIX}apps-inventory
set -eu
{deploy_snippet(None)}{TOOL_PATH} status
"""
