"""Scripts de construction d'un bureau Linux : XFCE + xrdp compilé avec H.264 (x264).

- `linux_prepare` (synchrone, via SSH) : compte utilisateur, puis lancement détaché de l'installation
  (unité systemd `rdpm-install`) : elle survit à une coupure SSH et se suit avec `LINUX_STATUS`.
- `render_install_script` : mise à jour, bureau, langue, compilation d'xrdp, configuration, vérifications ;
  jalons « RDPM-STEP i/N libellé » et « RDPM-DONE » / « RDPM-FAILED » dans /var/log/rdpm-install.log.
- `render_build_xrdp` : compilation + configuration d'xrdp, déposée sur le bureau dans
  /usr/local/share/rdpm/build-xrdp.sh pour une mise à jour ultérieure depuis son terminal.
- `linux_finalize` : ouverture d'une vraie session XFCE de test, empreinte du certificat RDP, nettoyage
  (clé SSH temporaire remplacée par la clé d'administration de l'application, identité machine réinitialisée
  pour chaque copie).
- Étape « Logiciels » : dépose `rdpm-apps` (software/linux.py) et installe les logiciels choisis ; un échec
  n'arrête pas la construction (jalons « RDPM-APP » dans le journal).

Aucun mot de passe n'apparaît dans ces scripts, sauf dans `linux_prepare` qui transite par l'entrée
standard SSH et est effacé aussitôt exécuté.
"""

from __future__ import annotations

import base64

from ..software import linux as apps_linux
from . import linux
from .scripts import TAG_PREFIX, sh_quote

INSTALL_LOG = "/var/log/rdpm-install.log"
INSTALL_UNIT = "rdpm-install"
INSTALL_SCRIPT = "/root/rdpm-install.sh"
BUILD_XRDP_PATH = "/usr/local/share/rdpm/build-xrdp.sh"
INSTALL_STEPS = 7


def _fill(template: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        template = template.replace(f"@@{key}@@", value)
    assert "@@" not in template, "variable de gabarit non remplacée"
    return template


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# --- compilation et configuration d'xrdp ------------------------------------------------------------------
_BUILD_XRDP = r"""#!/bin/bash
# Compilation et configuration d'xrdp avec H.264 (x264) — Cloud Desktop Manager.
#
#   sudo bash build-xrdp.sh            compile, installe et configure (mise à jour d'xrdp)
#   sudo bash build-xrdp.sh configure  réapplique seulement la configuration
#
# Pour passer à une nouvelle version : remplacer version, URL et SHA-256 ci-dessous par ceux publiés sur
# https://github.com/neutrinolabs/xrdp/releases (et xorgxrdp), puis relancer ce script.
set -Eeuo pipefail

XRDP_URL=@@XRDP_URL@@
XRDP_SHA256=@@XRDP_SHA256@@
XORGXRDP_URL=@@XORGXRDP_URL@@
XORGXRDP_SHA256=@@XORGXRDP_SHA256@@
PWXRDP_URL=@@PWXRDP_URL@@
PWXRDP_COMMIT=@@PWXRDP_COMMIT@@

SRC=/usr/local/src/rdpm-xrdp
APT=(apt-get -y -q -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold
     -o Dpkg::Options::=--force-confdef)
BUILD_DEPS=(build-essential pkg-config autoconf automake libtool nasm git curl ca-certificates openssl
            libssl-dev libpam0g-dev libx11-dev libxfixes-dev libxrandr-dev libjpeg-dev libfuse3-dev
            libpixman-1-dev libx264-dev libopus-dev libmp3lame-dev libimlib2-dev xserver-xorg-dev
            libpipewire-0.3-dev libspa-0.2-dev)
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1

fetch() {   # url sha256 fichier
    curl -fsSL --retry 5 --retry-delay 3 -o "$3" "$1"
    echo "$2  $3" | sha256sum -c -
}

make_certificate() {
    # 10 ans et une empreinte stable : l'application la préenregistre auprès de mstsc (pas d'alerte).
    install -d -m 0755 /etc/xrdp
    if [ ! -s /etc/xrdp/cert.pem ] || [ ! -s /etc/xrdp/key.pem ]; then
        openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 -subj "/CN=cloud-desktop" \
            -keyout /etc/xrdp/key.pem -out /etc/xrdp/cert.pem 2>/dev/null
    fi
    chmod 0600 /etc/xrdp/key.pem
    chmod 0644 /etc/xrdp/cert.pem
}

keep_runtime_libs() {
    # Les bibliothèques utilisées par nos binaires ne doivent jamais partir avec un « apt autoremove ».
    local files
    files=$(ls /usr/sbin/xrdp* /usr/lib/xrdp/*.so* /usr/lib/xorg/modules/*xrdp* \
                /usr/lib/xorg/modules/*/*xrdp* 2>/dev/null || true)
    files="$files $(find /usr/lib -name 'libpipewire-module-xrdp*.so' 2>/dev/null || true)"
    for f in $files; do ldd "$f" 2>/dev/null || true; done \
        | awk '$2 == "=>" && $3 ~ /^\// {print $3}' | sort -u \
        | while read -r lib; do
            pkg=$( (dpkg -S "$lib" 2>/dev/null || dpkg -S "$(readlink -f "$lib")" 2>/dev/null || true) \
                   | head -n1 | cut -d: -f1)
            if [ -n "$pkg" ]; then echo "$pkg"; fi
        done | sort -u | xargs -r apt-mark manual >/dev/null
}

build() {
    "${APT[@]}" install --no-install-recommends "${BUILD_DEPS[@]}"
    rm -rf "$SRC"
    mkdir -p "$SRC"
    cd "$SRC"
    fetch "$XRDP_URL" "$XRDP_SHA256" xrdp.tar.gz
    fetch "$XORGXRDP_URL" "$XORGXRDP_SHA256" xorgxrdp.tar.gz
    mkdir xrdp xorgxrdp
    tar -xzf xrdp.tar.gz -C xrdp --strip-components=1
    tar -xzf xorgxrdp.tar.gz -C xorgxrdp --strip-components=1
    make_certificate   # avant « make install », qui sinon créerait un certificat d'un an
    (cd xrdp && ./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
        --with-systemdsystemunitdir=/usr/lib/systemd/system \
        --enable-x264 --enable-fuse --enable-pixman --enable-opus --enable-mp3lame --enable-jpeg --enable-utmp \
        && make -j"$(nproc)" && make install)
    (cd xorgxrdp && ./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
        && make -j"$(nproc)" && make install)
    git init -q pwxrdp
    (cd pwxrdp && git fetch -q --depth 1 "$PWXRDP_URL" "$PWXRDP_COMMIT" && git checkout -q FETCH_HEAD \
        && test "$(git rev-parse HEAD)" = "$PWXRDP_COMMIT" \
        && ./bootstrap && ./configure --prefix=/usr --sysconfdir=/etc && make -j"$(nproc)" && make install)
    keep_runtime_libs
    cd /
    rm -rf "$SRC"
}

configure() {
    make_certificate
    local xorg=/usr/lib/xorg/Xorg   # le vrai serveur : /usr/bin/Xorg n'accepte que la console
    [ -x "$xorg" ] || xorg=$(command -v Xorg)

    cat > /etc/xrdp/xrdp.ini <<'EOF'
; Cloud Desktop Manager : TLS seulement, session Xorg (xorgxrdp), H.264 via gfx.toml.
[Globals]
ini_version=1
fork=true
port=3389
tcp_nodelay=true
tcp_keepalive=true
security_layer=tls
crypt_level=high
certificate=/etc/xrdp/cert.pem
key_file=/etc/xrdp/key.pem
ssl_protocols=TLSv1.2, TLSv1.3
autorun=Xorg
allow_channels=true
allow_multimon=true
bitmap_cache=true
bitmap_compression=true
bulk_compression=true
max_bpp=32
new_cursors=true
use_fastpath=both
ls_title=Cloud Desktop
ls_top_window_bg_color=1f2430
ls_bg_color=f0f0f0

[Logging]
LogFile=xrdp.log
LogLevel=INFO
EnableSyslog=true

[Channels]
rdpdr=true
rdpsnd=true
drdynvc=true
cliprdr=true
rail=true
xrdpvr=true

[Xorg]
name=Xorg
lib=libxup.so
username=ask
password=ask
port=-1
code=20
h264_frame_interval=16
rfx_frame_interval=32
normal_frame_interval=40
EOF

    cat > /etc/xrdp/sesman.ini <<EOF
; Cloud Desktop Manager : on retrouve sa session à chaque connexion, rien n'est fermé à la déconnexion.
[Globals]
EnableUserWindowManager=true
UserWindowManager=startwm.sh
DefaultWindowManager=startwm.sh
ReconnectScript=reconnectwm.sh

[Security]
AllowRootLogin=false
MaxLoginRetry=4
TerminalServerUsers=tsusers
TerminalServerAdmins=tsadmins
AlwaysGroupCheck=false
RestrictOutboundClipboard=none
RestrictInboundClipboard=none

[Sessions]
X11DisplayOffset=10
MaxSessions=8
KillDisconnected=false
DisconnectedTimeLimit=0
IdleTimeLimit=0
Policy=Default

[Logging]
LogFile=xrdp-sesman.log
LogLevel=INFO
EnableSyslog=true

[Xorg]
param=$xorg
param=-config
param=xrdp/xorg.conf
param=-noreset
param=-nolisten
param=tcp
param=-logfile
param=.xorgxrdp.%s.log

[Chansrv]
FuseMountName=thinclient_drives
FileUmask=077

[ChansrvLogging]
LogLevel=INFO
EnableSyslog=true

[SessionVariables]
PULSE_SCRIPT=/etc/xrdp/pulse/default.pa
EOF

    cat > /etc/pam.d/xrdp-sesman <<'EOF'
#%PAM-1.0
# Cloud Desktop Manager : session systemd complète (XDG_RUNTIME_DIR, PipeWire, D-Bus) et langue du système.
auth     required  pam_env.so readenv=1
auth     required  pam_env.so readenv=1 envfile=/etc/default/locale
@include common-auth
@include common-account
@include common-password
session  required  pam_limits.so
session  required  pam_loginuid.so
-session optional  pam_lastlog2.so silent
@include common-session
EOF

    cat > /etc/xrdp/startwm.sh <<'EOF'
#!/bin/sh
# Cloud Desktop Manager : session XFCE.
if [ -r /etc/profile ]; then . /etc/profile; fi
if [ -r /etc/default/locale ]; then
    . /etc/default/locale
    export LANG LANGUAGE
fi
export XDG_SESSION_TYPE=x11 XDG_CURRENT_DESKTOP=XFCE XDG_SESSION_DESKTOP=xfce DESKTOP_SESSION=xfce
# Bus de session de l'utilisateur (systemd --user, paquet dbus-user-session) ; à défaut, un bus dédié.
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "${XDG_RUNTIME_DIR:-/nonexistent}/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
fi
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
    exec dbus-run-session -- startxfce4
fi
exec startxfce4
EOF
    chmod 0755 /etc/xrdp/startwm.sh

    systemctl enable xrdp.service xrdp-sesman.service >/dev/null 2>&1 || true
    if [ -d /run/systemd/system ]; then
        systemctl restart xrdp-sesman.service xrdp.service
    fi
}

case "${1:-all}" in
    build) build ;;
    configure) configure ;;
    all) build; configure ;;
    *) echo "usage : $0 [all|build|configure]" >&2; exit 2 ;;
esac
"""


def render_build_xrdp() -> str:
    x, o, p = linux.XRDP, linux.XORGXRDP, linux.PIPEWIRE_XRDP
    return _fill(_BUILD_XRDP, {
        "XRDP_URL": sh_quote(x.url), "XRDP_SHA256": x.sha256,
        "XORGXRDP_URL": sh_quote(o.url), "XORGXRDP_SHA256": o.sha256,
        "PWXRDP_URL": sh_quote(p.url), "PWXRDP_COMMIT": p.commit,
    })


# --- installation complète ------------------------------------------------------------------------------
_INSTALL = r"""#!/bin/bash
# rdpm:linux-install — journal : /var/log/rdpm-install.log
set -Eeuo pipefail
STEP="démarrage"
TOTAL=@@TOTAL@@
step() { STEP="$2"; echo "RDPM-STEP $1/$TOTAL $2"; }
trap 'rc=$?; echo "RDPM-FAILED ${STEP} (code ${rc}, ligne ${LINENO})"; exit ${rc}' ERR

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 NEEDRESTART_MODE=l
APT=(apt-get -y -q -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold
     -o Dpkg::Options::=--force-confdef)
FAMILY=@@FAMILY@@
USERNAME=@@USERNAME@@
LOCALE=@@LOCALE@@
LANGUAGE_LIST=@@LANGUAGE@@
TZ_NAME=@@TIMEZONE@@
XKB_LAYOUT=@@XKB_LAYOUT@@
XKB_VARIANT=@@XKB_VARIANT@@
FIREFOX_LANG=@@FIREFOX_LANG@@
UBUNTU_PACKS=(@@UBUNTU_PACKS@@)
MOZILLA_FPR=@@MOZILLA_FPR@@
BUILD_XRDP=@@BUILD_XRDP_PATH@@

has_systemd() { [ -d /run/systemd/system ]; }
available() { apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ && $2 != "(none)" {ok=1} END {exit !ok}'; }

install_firefox() {
    local pkgs=() l10n
    if [ "$FAMILY" = ubuntu ]; then
        # Dépôt officiel de Mozilla : paquet .deb à jour, sans snap (démarrage rapide dans une session RDP).
        install -d -m 0755 /etc/apt/keyrings
        curl -fsSL --retry 5 https://packages.mozilla.org/apt/repo-signing-key.gpg \
            -o /etc/apt/keyrings/packages.mozilla.org.asc
        local fpr
        fpr=$(gpg --show-keys --with-colons /etc/apt/keyrings/packages.mozilla.org.asc 2>/dev/null \
              | awk -F: '/^fpr:/ {print $10; exit}')
        if [ "$fpr" != "$MOZILLA_FPR" ]; then echo "Clé du dépôt Mozilla inattendue : $fpr"; false; fi
        echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
            > /etc/apt/sources.list.d/mozilla.list
        printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
        printf 'Unattended-Upgrade::Origins-Pattern { "site=packages.mozilla.org"; };\n' \
            > /etc/apt/apt.conf.d/51rdpm-mozilla
        "${APT[@]}" update
        pkgs=(firefox)
        l10n="firefox-l10n-${FIREFOX_LANG,,}"
    else
        pkgs=(firefox-esr)
        l10n="firefox-esr-l10n-${FIREFOX_LANG,,}"
    fi
    if [ -n "$FIREFOX_LANG" ] && available "$l10n"; then pkgs+=("$l10n"); fi
    "${APT[@]}" install --no-install-recommends "${pkgs[@]}"
    local policies='{"policies": {"DisableAppUpdate": true, "DontCheckDefaultBrowser": true,
  "OverrideFirstRunPage": "", "OverridePostUpdatePage": "", "DisableTelemetry": true,
  "UserMessaging": {"WhatsNew": false, "ExtensionRecommendations": false, "FeatureRecommendations": false,
                    "SkipOnboarding": true, "MoreFromMozilla": false}}}'
    for dir in /etc/firefox/policies /etc/firefox-esr/policies; do
        install -d -m 0755 "$dir"
        printf '%s\n' "$policies" > "$dir/policies.json"
    done
}

xfconf_default() {   # canal chemin type valeur — retouche les réglages XFCE par défaut sans écraser le reste
    python3 - "$@" <<'PY'
import os, sys, xml.etree.ElementTree as ET
channel, path, kind, value = sys.argv[1:5]
f = f"/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/{channel}.xml"
if os.path.exists(f):
    tree = ET.parse(f)
else:
    tree = ET.ElementTree(ET.Element("channel", name=channel, version="1.0"))
node = tree.getroot()
for part in [p for p in path.split("/") if p]:
    child = next((c for c in node.findall("property") if c.get("name") == part), None)
    if child is None:
        child = ET.SubElement(node, "property", name=part, type="empty")
    node = child
node.set("type", kind)
node.set("value", value)
ET.indent(tree)
tree.write(f, encoding="UTF-8", xml_declaration=True)
PY
}

configure_desktop() {
    install -d -m 0755 /etc/xdg/xfce4/xfconf/xfce-perchannel-xml
    # Pas de compositeur : rien à calculer en OpenGL logiciel, image nette et fluide à distance.
    xfconf_default xfwm4 /general/use_compositing bool false
    xfconf_default xfwm4 /general/theme string Greybird
    xfconf_default xfwm4 /general/title_font string "Noto Sans Bold 10"
    xfconf_default xsettings /Net/ThemeName string Greybird
    xfconf_default xsettings /Net/IconThemeName string elementary-Xfce-darker
    xfconf_default xsettings /Net/EnableEventSounds bool false
    xfconf_default xsettings /Xft/Antialias int 1
    xfconf_default xsettings /Xft/HintStyle string hintslight
    xfconf_default xsettings /Xft/RGBA string none
    xfconf_default xsettings /Gtk/FontName string "Noto Sans 10"
    xfconf_default xsettings /Gtk/MonospaceFontName string "Noto Sans Mono 10"
    xfconf_default xsettings /Gtk/CursorThemeName string Adwaita
    # Fond d'écran : l'image par défaut compilée dans xfdesktop n'existe pas (écran noir). L'écran d'une
    # session xrdp s'appelle rdp0 ; un seul fond pour tous les espaces de travail.
    local wallpaper
    for wallpaper in /usr/share/backgrounds/xfce/xfce-shapes.svg /usr/share/backgrounds/greybird.svg ""; do
        [ -z "$wallpaper" ] || [ -f "$wallpaper" ] && break
    done
    if [ -n "$wallpaper" ]; then
        xfconf_default xfce4-desktop /backdrop/single-workspace-mode bool true
        xfconf_default xfce4-desktop /backdrop/single-workspace-number int 0
        xfconf_default xfce4-desktop /backdrop/screen0/monitorrdp0/workspace0/last-image string "$wallpaper"
        xfconf_default xfce4-desktop /backdrop/screen0/monitorrdp0/workspace0/image-style int 5
    fi
    # Panneau par défaut sans la question « configuration par défaut ou panneau vide ? », menu Whisker.
    if [ -f /etc/xdg/xfce4/panel/default.xml ]; then
        sed 's/value="applicationsmenu"/value="whiskermenu"/' /etc/xdg/xfce4/panel/default.xml \
            > /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml
    fi

    # Pas de fenêtres d'authentification parasites (profils couleur, sources de paquets) ; montage des
    # volumes sans mot de passe pour les administrateurs.
    install -d -m 0755 /etc/polkit-1/rules.d
    cat > /etc/polkit-1/rules.d/45-rdpm.rules <<'EOF'
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.color-manager.") == 0 ||
        action.id == "org.freedesktop.packagekit.system-sources-refresh") {
        return polkit.Result.YES;
    }
    if (subject.isInGroup("sudo") &&
        (action.id == "org.freedesktop.udisks2.filesystem-mount" ||
         action.id == "org.freedesktop.udisks2.filesystem-mount-system" ||
         action.id == "org.freedesktop.udisks2.filesystem-mount-other-seat")) {
        return polkit.Result.YES;
    }
});
EOF

    # « Sauvegarder & fermer » envoie le signal d'arrêt ACPI : il doit toujours éteindre, vite.
    install -d -m 0755 /etc/systemd/logind.conf.d /etc/systemd/system.conf.d
    printf '[Login]\nHandlePowerKey=poweroff\nPowerKeyIgnoreInhibited=yes\n' \
        > /etc/systemd/logind.conf.d/50-rdpm.conf
    printf '[Manager]\nDefaultTimeoutStopSec=30s\n' > /etc/systemd/system.conf.d/50-rdpm.conf

    # SSH : jamais de mot de passe (un serveur créé sans clé reçoit un mot de passe root du fournisseur).
    if [ -d /etc/ssh/sshd_config.d ]; then
        printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\n' \
            > /etc/ssh/sshd_config.d/00-rdpm.conf
    fi
    if [ -d /etc/cloud/cloud.cfg.d ]; then
        printf 'ssh_pwauth: false\n' > /etc/cloud/cloud.cfg.d/99-rdpm.cfg
    fi

    # Mises à jour de sécurité automatiques.
    printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
        > /etc/apt/apt.conf.d/20auto-upgrades
}

step 1 "Mise à jour du système"
"${APT[@]}" update
"${APT[@]}" full-upgrade
if [ "$FAMILY" = ubuntu ] && dpkg -s snapd >/dev/null 2>&1; then
    if has_systemd && command -v snap >/dev/null 2>&1; then snap wait system seed.loaded || true; fi
    "${APT[@]}" purge snapd
    rm -rf /snap /var/snap /var/lib/snapd /var/cache/snapd
fi
printf '%s\n' "# Cloud Desktop Manager : xrdp est compilé avec H.264 ; pas de snap." \
    "Package: xrdp xorgxrdp pipewire-module-xrdp snapd" "Pin: release *" "Pin-Priority: -1" \
    > /etc/apt/preferences.d/rdpm

step 2 "Bureau XFCE, son et Firefox"
PKGS=(xserver-xorg-core xauth x11-xserver-utils xfce4-session xfwm4 xfce4-panel xfdesktop4 xfce4-settings xfconf
      xfce4-notifyd thunar thunar-volman tumbler gvfs gvfs-backends udisks2 fuse3 xfce4-terminal
      xfce4-whiskermenu-plugin xfce4-pulseaudio-plugin mousepad ristretto xfce4-screenshooter xfce4-taskmanager
      file-roller greybird-gtk-theme elementary-xfce-icon-theme adwaita-icon-theme fonts-noto-core
      fonts-noto-color-emoji fonts-dejavu-core dbus-user-session at-spi2-core xdg-user-dirs xdg-utils pipewire
      pipewire-pulse wireplumber pipewire-bin pavucontrol locales tzdata sudo curl ca-certificates gnupg
      unattended-upgrades openssl zenity xz-utils python3)
for agent in policykit-1-gnome mate-polkit lxpolkit; do
    if available "$agent"; then PKGS+=("$agent"); break; fi
done
if [ "$FAMILY" = ubuntu ]; then
    for p in "${UBUNTU_PACKS[@]}"; do if available "$p"; then PKGS+=("$p"); fi; done
fi
"${APT[@]}" install --no-install-recommends "${PKGS[@]}"
install_firefox

step 3 "Langue, clavier et fuseau horaire"
if [ -f /etc/locale.gen ]; then
    sed -i "s/^# *${LOCALE} UTF-8/${LOCALE} UTF-8/" /etc/locale.gen
    grep -q "^${LOCALE} UTF-8" /etc/locale.gen || echo "${LOCALE} UTF-8" >> /etc/locale.gen
fi
if [ "$FAMILY" = ubuntu ]; then locale-gen "$LOCALE"; else locale-gen; fi
update-locale "LANG=$LOCALE" "LANGUAGE=$LANGUAGE_LIST"
ln -sf "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime
echo "$TZ_NAME" > /etc/timezone
printf 'XKBMODEL="pc105"\nXKBLAYOUT="%s"\nXKBVARIANT="%s"\nXKBOPTIONS=""\nBACKSPACE="guess"\n' \
    "$XKB_LAYOUT" "$XKB_VARIANT" > /etc/default/keyboard
for group in sudo audio video; do
    if getent group "$group" >/dev/null; then usermod -aG "$group" "$USERNAME"; fi
done

step 4 "Compilation d'xrdp avec H.264 (quelques minutes)"
install -d -m 0755 "$(dirname "$BUILD_XRDP")"
echo @@BUILD_XRDP_B64@@ | base64 -d > "$BUILD_XRDP"
chmod 0755 "$BUILD_XRDP"
bash "$BUILD_XRDP" build

step 5 "Configuration du bureau distant"
bash "$BUILD_XRDP" configure
configure_desktop

step 6 "Vérifications"
xrdp --version | grep -q -- "--enable-x264" || { echo "xrdp a été compilé sans H.264"; false; }
test -f /usr/lib/xorg/modules/drivers/xrdpdev_drv.so
test -x "$(sed -n 's/^param=\(\/.*Xorg\)$/\1/p' /etc/xrdp/sesman.ini | head -n1)"
if has_systemd; then
    systemctl is-active --quiet xrdp-sesman.service
    systemctl is-active --quiet xrdp.service
fi

step 7 "Logiciels"
@@APPS_DEPLOY@@
# Un logiciel en échec est signalé (RDPM-APP fail) sans faire échouer le bureau.
rdpm-apps install @@APPS@@ || true
echo RDPM-DONE
"""


def render_install_script(*, distro: str, username: str, locale: str, timezone: str,
                          apps: tuple[str, ...] | list[str] = ()) -> str:
    d = linux.DISTROS[distro]
    loc = linux.LOCALES[locale]
    return _fill(_INSTALL, {
        "TOTAL": str(INSTALL_STEPS),
        "FAMILY": sh_quote(d.family), "USERNAME": sh_quote(username), "LOCALE": sh_quote(loc.locale),
        "LANGUAGE": sh_quote(loc.language), "TIMEZONE": sh_quote(timezone),
        "XKB_LAYOUT": sh_quote(loc.xkb[0]), "XKB_VARIANT": sh_quote(loc.xkb[1]),
        "FIREFOX_LANG": sh_quote(loc.firefox_lang or ""),
        "UBUNTU_PACKS": " ".join(sh_quote(p) for p in loc.ubuntu_packs),
        "MOZILLA_FPR": linux.MOZILLA_KEY_FINGERPRINT, "BUILD_XRDP_PATH": sh_quote(BUILD_XRDP_PATH),
        "BUILD_XRDP_B64": _b64(render_build_xrdp()),
        "APPS_DEPLOY": apps_linux.deploy_snippet(username).rstrip("\n"),
        "APPS": " ".join(sh_quote(k) for k in apps),
    })


# --- étapes pilotées en SSH ---------------------------------------------------------------------------------
def linux_prepare(*, username: str, password: str, install_script: str) -> str:
    """Compte utilisateur + lancement détaché de l'installation. Rejouable (reprise après coupure SSH)."""
    return f"""{TAG_PREFIX}linux-prepare
set -eu
export DEBIAN_FRONTEND=noninteractive
if command -v cloud-init >/dev/null 2>&1; then cloud-init status --wait >/dev/null 2>&1 || true; fi
# Les mises à jour automatiques du premier démarrage bloqueraient apt : suspendues jusqu'à l'arrêt seulement.
for unit in apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service \\
            unattended-upgrades.service; do
    systemctl stop "$unit" >/dev/null 2>&1 || true
    systemctl mask --runtime "$unit" >/dev/null 2>&1 || true
done
USERNAME={sh_quote(username)}
if ! id -u "$USERNAME" >/dev/null 2>&1; then useradd -m -s /bin/bash "$USERNAME"; fi
printf '%s:%s\\n' "$USERNAME" {sh_quote(password)} | chpasswd
if getent group sudo >/dev/null; then usermod -aG sudo "$USERNAME"; fi
echo {_b64(install_script)} | base64 -d > {INSTALL_SCRIPT}
chmod 0700 {INSTALL_SCRIPT}
if systemctl is-active --quiet {INSTALL_UNIT}; then echo RDPM_STARTED; exit 0; fi
if grep -q '^RDPM-DONE' {INSTALL_LOG} 2>/dev/null; then echo RDPM_STARTED; exit 0; fi
systemctl reset-failed {INSTALL_UNIT} >/dev/null 2>&1 || true
: > {INSTALL_LOG}
chmod 0600 {INSTALL_LOG}
systemd-run --unit={INSTALL_UNIT} --service-type=exec --quiet \\
    -p StandardOutput=append:{INSTALL_LOG} -p StandardError=append:{INSTALL_LOG} \\
    /bin/bash {INSTALL_SCRIPT}
echo RDPM_STARTED
"""


LINUX_STATUS = f"""{TAG_PREFIX}linux-status
state=$(systemctl show {INSTALL_UNIT} -p ActiveState --value 2>/dev/null || true)
result=$(systemctl show {INSTALL_UNIT} -p Result --value 2>/dev/null || true)
echo "RDPM_UNIT ${{state:-unknown}} ${{result:-unknown}}"
echo RDPM_MARKS_BEGIN
grep -a '^RDPM-' {INSTALL_LOG} 2>/dev/null | tail -n 200 || true
echo RDPM_TAIL_BEGIN
tail -n 12 {INSTALL_LOG} 2>/dev/null || true
"""


def linux_finalize(username: str, admin_pubkey: str = "") -> str:
    """Session XFCE de test, empreinte du certificat, puis nettoyage avant l'arrêt et le snapshot. La clé SSH
    temporaire est remplacée par la clé d'administration de l'application (installation de logiciels plus
    tard ; le port SSH reste fermé par le pare-feu du fournisseur hors opération)."""
    return f"""{TAG_PREFIX}linux-finalize
set -u
USERNAME={sh_quote(username)}
ok=0
if command -v xrdp-sesrun >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    # Vraie session xrdp pour le compte (sans mot de passe : l'utilisateur courant est authentifié par sesman).
    runuser -u "$USERNAME" -- xrdp-sesrun -t Xorg -g 1280x720 >/root/rdpm-sesrun.log 2>&1 || true
    for i in $(seq 1 45); do
        if pgrep -u "$USERNAME" -x xfce4-panel >/dev/null 2>&1; then ok=1; break; fi
        sleep 1
    done
    sleep 3
    loginctl terminate-user "$USERNAME" >/dev/null 2>&1 || pkill -u "$USERNAME" >/dev/null 2>&1 || true
    sleep 2
fi
echo "RDPM_SESSION $ok"
if [ "$ok" != 1 ]; then
    tail -n 5 /root/rdpm-sesrun.log 2>/dev/null || true
    tail -n 8 /home/"$USERNAME"/.xorgxrdp.*.log 2>/dev/null || true
fi
echo "RDPM_CERT $(openssl x509 -in /etc/xrdp/cert.pem -noout -fingerprint -sha1 2>/dev/null | sed 's/.*=//; s/://g')"
echo "RDPM_XRDP $(xrdp --version 2>/dev/null | head -n1 | sed 's/[^0-9.]*\\([0-9][0-9.]*\\).*/\\1/')"
rm -f /root/rdpm-sesrun.log /home/"$USERNAME"/.xorgxrdp.*.log
rm -rf /home/"$USERNAME"/.cache
apt-get clean
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
rm -f /root/.bash_history /root/.ssh/authorized_keys
ADMIN_KEY={sh_quote(admin_pubkey)}
if [ -n "$ADMIN_KEY" ]; then
    install -d -m 0700 /root/.ssh
    printf '%s\n' "$ADMIN_KEY" > /root/.ssh/authorized_keys
    chmod 0600 /root/.ssh/authorized_keys
    echo RDPM_ADMIN 1
fi
if command -v cloud-init >/dev/null 2>&1; then
    cloud-init clean --logs --machine-id >/dev/null 2>&1 || cloud-init clean --logs >/dev/null 2>&1 || true
fi
[ -s /etc/machine-id ] && truncate -s 0 /etc/machine-id
fstrim -a >/dev/null 2>&1 || true
sync
echo RDPM_FINALIZED
"""
