"""Scripts exécutés sur le serveur de construction (Ubuntu puis Alpine) et fichiers injectés dans Windows.

Chaque script commence par une ligne `# rdpm:<étiquette>` : le faux SSH de la simulation s'en sert
pour reconnaître l'étape, et le journal de l'application aussi.
"""

from __future__ import annotations

from .catalog import REINSTALL_RAW, REINSTALL_SHA256

TAG_PREFIX = "# rdpm:"


def tag_of(script: str) -> str:
    first = script.split("\n", 1)[0].strip()
    return first[len(TAG_PREFIX):].strip() if first.startswith(TAG_PREFIX) else ""


def sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


# --- Ubuntu ------------------------------------------------------------------------------------------
def ubuntu_prepare(*, iso_url: str, image_name: str, password: str, pubkey: str,
                   raw_url: str = REINSTALL_RAW, sha256: str = REINSTALL_SHA256) -> str:
    """Télécharge reinstall.sh (version figée, empreinte vérifiée), le lance en mode « hold 2 » puis
    ajoute notre clé publique dans l'initrd : l'environnement Alpine acceptera la clé alors que
    reinstall.sh refuse --ssh-key pour Windows (le mot de passe Windows est stocké à part)."""
    return f"""{TAG_PREFIX}ubuntu-prepare
set -eu
export DEBIAN_FRONTEND=noninteractive
cd /root
if command -v cloud-init >/dev/null 2>&1; then cloud-init status --wait >/dev/null 2>&1 || true; fi
curl -fsSL --retry 3 -o reinstall.sh {sh_quote(raw_url + "/reinstall.sh")}
echo "{sha256}  reinstall.sh" | sha256sum -c - >/dev/null
sed -i "s|^confhome=.*|confhome={raw_url}|" reinstall.sh
grep -q "^confhome={raw_url}$" reinstall.sh
bash reinstall.sh windows --iso {sh_quote(iso_url)} --image-name {sh_quote(image_name)} \\
    --username administrator --password {sh_quote(password)} --hold 2
test -f /reinstall-initrd
d=$(mktemp -d)
cd "$d"
zcat /reinstall-initrd | cpio -idm --quiet 2>/dev/null
mkdir -p configs
printf '%s\\n' {sh_quote(pubkey)} > configs/ssh_keys
find . | cpio --quiet -o -H newc -R 0:0 | gzip -1 > /reinstall-initrd
cd /root
rm -rf "$d"
echo RDPM_PREPARED
"""


REBOOT = f"""{TAG_PREFIX}reboot
nohup sh -c 'sleep 1; reboot' >/dev/null 2>&1 &
echo RDPM_REBOOTING
"""

PING = f"""{TAG_PREFIX}ping
echo RDPM_PONG
"""

# --- Alpine (environnement d'installation) ----------------------------------------------------------
ALPINE_STATUS = f"""{TAG_PREFIX}alpine-status
[ -f /etc/alpine-release ] || exit 3
[ -f /reinstall.log ] || exit 4
if pgrep -f trans.sh >/dev/null 2>&1; then echo RDPM_RUNNING; else echo RDPM_IDLE; fi
echo RDPM_LOG_BEGIN
tail -c 262144 /reinstall.log | tr '\\r' '\\n'
"""


def alpine_set_image_name(image_name: str) -> str:
    return f"""{TAG_PREFIX}alpine-set-image-name
printf '%s\\n' {sh_quote(image_name)} > /image-name
echo RDPM_IMAGE_SET
"""


def alpine_hook(image_name: str, postinstall_ps1: str, locale_xml: str) -> str:
    """Monte install.wim, dépose nos fichiers à la racine de l'image et enchaîne notre script à la fin
    de SetupComplete.cmd (après ceux de reinstall.sh : redimensionnement, réseau)."""
    cmd_line = ("powershell -NoProfile -ExecutionPolicy Bypass -File %SystemDrive%\\rdpm-postinstall.ps1 "
                ">> %SystemDrive%\\rdpm-postinstall.log 2>&1")
    return f"""{TAG_PREFIX}alpine-hook
set -e
apk add wimlib >/dev/null 2>&1 || true
wim=$(find /os/installer -maxdepth 2 -iname 'install.wim' | head -n1)
if [ -z "$wim" ]; then echo "RDPM_ERR install.wim introuvable (ISO en install.esd ?)"; exit 2; fi
idx=$(wiminfo "$wim" | awk -F': *' -v n={sh_quote(image_name.lower())} \\
    'tolower($1)=="index"{{i=$2}} tolower($1)=="name"{{if (tolower($2)==n) print i}}' | head -n1)
if [ -z "$idx" ]; then echo "RDPM_ERR image introuvable dans install.wim"; wiminfo "$wim" | grep -i '^Name:'; exit 2; fi
mkdir -p /wim
if mountpoint -q /wim; then wimunmount /wim || true; fi
wimmountrw "$wim" "$idx" /wim
cat > /wim/rdpm-postinstall.ps1 <<'RDPM_PS1_EOF'
{postinstall_ps1}
RDPM_PS1_EOF
cat > /wim/rdpm-locale.xml <<'RDPM_XML_EOF'
{locale_xml}
RDPM_XML_EOF
sc=$(find /wim -maxdepth 4 -iname 'SetupComplete.cmd' | grep -i '/setup/scripts/' | head -n1)
if [ -z "$sc" ]; then
    win=$(find /wim -maxdepth 1 -iname windows | head -n1)
    [ -n "$win" ] || win=/wim/Windows
    mkdir -p "$win/Setup/Scripts"
    sc="$win/Setup/Scripts/SetupComplete.cmd"
fi
printf '%s\\r\\n' {sh_quote(cmd_line)} >> "$sc"
wimunmount --commit /wim
echo RDPM_HOOKED
"""


# --- fichiers injectés dans Windows -----------------------------------------------------------------
def render_locale_xml(keyboard: str, iso_keyboard: str) -> str:
    """Disposition clavier pour le compte courant (SYSTEM), copiée vers le profil par défaut (nouveaux
    utilisateurs, dont l'administrateur à sa première ouverture) et l'écran de connexion."""
    remove = ""
    if iso_keyboard and iso_keyboard.lower() != keyboard.lower():
        remove = f'\n    <gs:InputLanguageID Action="remove" ID="{iso_keyboard}"/>'
    return f"""<gs:GlobalizationServices xmlns:gs="urn:longhornGlobalizationUnattend">
  <gs:UserList>
    <gs:User UserID="Current" CopySettingsToDefaultUserAcct="true" CopySettingsToSystemAcct="true"/>
  </gs:UserList>
  <gs:InputPreferences>
    <gs:InputLanguageID Action="add" ID="{keyboard}" Default="true"/>{remove}
  </gs:InputPreferences>
</gs:GlobalizationServices>"""


EVAL_TASK_DIR = r"C:\ProgramData\rdpm"
EVAL_TASK_NAME = "rdpm-eval-rearm"


def render_eval_rearm_ps1(threshold_days: int) -> str:
    """Script de la tâche planifiée (au démarrage, compte SYSTEM) qui prolonge la licence d'évaluation.

    - licence active et moins de `threshold_days` jours restants, ou licence expirée : slmgr /rearm puis
      redémarrage immédiat (avant que l'utilisateur se connecte) ;
    - licence en période de grâce (juste après un rearm) : slmgr /ato pour réactiver 180 jours ;
    - jamais deux prolongations à moins de 2 jours d'intervalle (garde-fou contre une boucle).
    Lecture de l'état par WMI (indépendant de la langue de Windows). ASCII seulement."""
    return f"""# {EVAL_TASK_NAME}.ps1 - prolonge la licence d'evaluation Windows Server (tache planifiee au demarrage, SYSTEM)
param([int]$Threshold = {threshold_days})
$ErrorActionPreference = 'Continue'
$dir = '{EVAL_TASK_DIR}'
$log = Join-Path $dir 'eval-rearm.log'
$stamp = Join-Path $dir 'last-rearm.txt'
$slmgr = Join-Path $env:SystemRoot 'System32\\slmgr.vbs'
function Log([string]$m) {{ Add-Content -Path $log -Value ("{{0:yyyy-MM-dd HH:mm:ss}} {{1}}" -f (Get-Date), $m) }}
function Product {{
    Get-CimInstance -ClassName SoftwareLicensingProduct `
        -Filter "ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f' AND PartialProductKey IS NOT NULL" |
        Where-Object {{ $_.Name -like 'Windows*' }} | Select-Object -First 1
}}
function Days($p) {{ [math]::Floor([double]$p.GracePeriodRemaining / 1440) }}
try {{
    $p = Product
    if (-not $p) {{ Log 'aucune licence Windows trouvee'; exit 0 }}
    $svc = Get-CimInstance -ClassName SoftwareLicensingService
    $eval = ('{{0}} {{1}}' -f $p.Description, $p.ProductKeyChannel) -match 'EVAL'
    Log ('statut={{0}} jours={{1}} rearm={{2}} rearm_sku={{3}} canal={{4}} eval={{5}}' -f $p.LicenseStatus, (Days $p),
         $svc.RemainingWindowsReArmCount, $p.RemainingSkuReArmCount, $p.ProductKeyChannel, $eval)
    if (-not $eval) {{ exit 0 }}
    $grace = @(2, 3, 4, 6) -contains [int]$p.LicenseStatus
    if ($grace) {{
        Log 'activation : slmgr /ato'
        & cscript.exe //nologo $slmgr /ato 2>&1 | ForEach-Object {{ Log "  $_" }}
        $p = Product
        Log ('apres activation : statut={{0}} jours={{1}}' -f $p.LicenseStatus, (Days $p))
        exit 0
    }}
    $expiring = ([int]$p.LicenseStatus -eq 1 -and (Days $p) -lt $Threshold) -or @(0, 5) -contains [int]$p.LicenseStatus
    if (-not $expiring) {{ exit 0 }}
    if ([int]$svc.RemainingWindowsReArmCount -le 0) {{ Log 'plus aucune prolongation possible'; exit 0 }}
    if ((Test-Path $stamp) -and ((Get-Date) - (Get-Item $stamp).LastWriteTime).TotalDays -lt 2) {{
        Log 'prolongation deja faite il y a moins de 2 jours : rien a faire'; exit 0
    }}
    Log ('prolongation : slmgr /rearm ({{0}} jours restants, seuil {{1}})' -f (Days $p), $Threshold)
    & cscript.exe //nologo $slmgr /rearm 2>&1 | ForEach-Object {{ Log "  $_" }}
    Set-Content -Path $stamp -Value (Get-Date -Format s)
    Log 'redemarrage pour appliquer la prolongation'
    Restart-Computer -Force
}} catch {{ Log "erreur : $_" }}
"""


def render_eval_task_install(threshold_days: int) -> str:
    """Bloc PowerShell (SYSTEM ou administrateur) qui installe la tâche planifiée de prolongation.
    Sert à la construction et, collé dans une session, aux bureaux créés avant cette fonction."""
    body = render_eval_rearm_ps1(threshold_days)
    return f"""$dir = '{EVAL_TASK_DIR}'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
# SYSTEM et Administrateurs seulement en ecriture : le script s'execute en SYSTEM.
& icacls.exe $dir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null
$script = Join-Path $dir '{EVAL_TASK_NAME}.ps1'
Set-Content -Path $script -Encoding ASCII -Value @'
{body}'@
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName '{EVAL_TASK_NAME}' -TaskPath '\\rdpm\\' -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskPath '\\rdpm\\' | Select-Object TaskName, State | Out-String
"""


def render_postinstall_ps1(timezone: str, eval_rearm_days: int | None = None) -> str:
    """Réglages appliqués une fois par SetupComplete.cmd (compte SYSTEM, avant la première ouverture
    de session). ASCII seulement : PowerShell 5 lit un .ps1 sans BOM en ANSI.

    `eval_rearm_days` : installe la tâche de prolongation automatique de la licence d'évaluation (seuil
    en jours) ; None pour une ISO personnalisée.

    Le réseau n'est jamais modifié : reinstall.sh laisse Windows en DHCP (Ubuntu Hetzner l'est), et forcer
    le DHCP ici a coupé le réseau lors des essais réels."""
    tz = timezone.replace('"', "")
    eval_step = ""
    if eval_rearm_days is not None:
        eval_step = ("Step 'Prolongation automatique de la licence d evaluation' {\n"
                     + render_eval_task_install(eval_rearm_days) + "}\n")
    return f"""# rdpm-postinstall.ps1 - execute une fois par SetupComplete.cmd (SYSTEM) ; journal C:\\rdpm-postinstall.log
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
function Step($name, [scriptblock]$body) {{
    Write-Output ("== {{0}} ({{1:HH:mm:ss}})" -f $name, (Get-Date))
    try {{
        & $body 2>&1 | Out-String -Width 200 | ForEach-Object {{ if ($_.Trim()) {{ Write-Output $_.TrimEnd() }} }}
    }} catch {{ Write-Output "!! $name : $_" }}
}}
Step 'Clavier (intl.cpl)' {{
    if (Test-Path 'C:\\rdpm-locale.xml') {{
        $p = Start-Process -FilePath "$env:SystemRoot\\System32\\rundll32.exe" `
            -ArgumentList 'shell32.dll,Control_RunDLL intl.cpl,,/f:"C:\\rdpm-locale.xml"' `
            -Wait -PassThru -WindowStyle Hidden
        Write-Output "intl.cpl code $($p.ExitCode)"
    }}
}}
Step 'Fuseau horaire' {{
    & tzutil.exe /s "{tz}"
    & tzutil.exe /g
}}
Step 'Alimentation (arret ACPI, pas de veille prolongee)' {{
    & powercfg.exe /h off
    & powercfg.exe /setacvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 3
    & powercfg.exe /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS PBUTTONACTION 3
    & powercfg.exe /setactive SCHEME_CURRENT
    & reg.exe add 'HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power' /v HiberbootEnabled /t REG_DWORD /d 0 /f
    # Windows Server ignore le signal d'arret ACPI tant que personne n'est connecte : autoriser l'arret sans session.
    & reg.exe add 'HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System' /v shutdownwithoutlogon /t REG_DWORD /d 1 /f
}}
Step 'Bureau a distance' {{
    & reg.exe add 'HKLM\\SYSTEM\\CurrentControlSet\\Control\\Terminal Server' /v fDenyTSConnections /t REG_DWORD /d 0 /f
    & reg.exe add 'HKLM\\SYSTEM\\CurrentControlSet\\Control\\Terminal Server\\WinStations\\RDP-Tcp' /v UserAuthentication /t REG_DWORD /d 1 /f
    Get-NetFirewallRule -Name 'RemoteDesktop-UserMode-In-TCP','RemoteDesktop-UserMode-In-UDP' -ErrorAction SilentlyContinue | Enable-NetFirewallRule
}}
Step 'Gestionnaire de serveur' {{
    Disable-ScheduledTask -TaskPath '\\Microsoft\\Windows\\Server Manager\\' -TaskName ServerManager -ErrorAction SilentlyContinue | Out-Null
    & reg.exe add 'HKLM\\SOFTWARE\\Microsoft\\ServerManager' /v DoNotOpenServerManagerAtLogon /t REG_DWORD /d 1 /f
}}
Step 'Reseau (etat, sans modification)' {{
    Get-NetIPConfiguration -ErrorAction SilentlyContinue | Out-String -Width 200
}}
{eval_step}Step 'TRIM du disque' {{ Optimize-Volume -DriveLetter C -ReTrim -ErrorAction SilentlyContinue }}
Set-Content -Path 'C:\\rdpm-postinstall.done' -Value (Get-Date -Format s)
Remove-Item 'C:\\rdpm-locale.xml' -ErrorAction SilentlyContinue
Write-Output '== termine'
"""
