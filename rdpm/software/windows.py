"""`apps.ps1` : l'outil d'installation des logiciels du catalogue sur un bureau Windows (PowerShell 5).

Généré depuis `catalog.APPS` et déposé dans C:\\ProgramData\\CloudDesktop\\apps.ps1 (UTF-8 avec BOM). Il sert :

- à la construction et à l'installation depuis l'application : l'application s'y connecte en SSH (OpenSSH,
  clé d'administration) et lance une tâche planifiée sous le compte administrateur du bureau, qui survit à
  la coupure SSH ; le suivi lit les jalons « RDPM-APP » de C:\\ProgramData\\CloudDesktop\\apps.log ;
- au raccourci « Logiciels » du Bureau (liste à cocher WinForms, installation dans une console).

Installation via winget (sources officielles des éditeurs), npm ou les installeurs officiels ; winget est
réparé ou installé au besoin (module Microsoft.WinGet.Client), indispensable sous Windows Server 2022.
"""

from __future__ import annotations

import base64

from ..build.scripts import TAG_PREFIX
from . import catalog

ROOT = r"C:\ProgramData\CloudDesktop"
TOOL_PATH = ROOT + r"\apps.ps1"
APPS_LOG = ROOT + r"\apps.log"
TASK_PATH = "\\rdpm\\"
TASK_NAME = "rdpm-apps"

_TOOL = r"""# apps.ps1 - logiciels prets a l'emploi (Cloud Desktop Manager, catalogue @@VERSION@@)
#   apps.ps1 -Mode install -Keys git,node   installe (dependances ajoutees)
#   apps.ps1 -Mode status                   cles installees
#   apps.ps1 -Mode update                   met a jour les logiciels installes
#   apps.ps1                                liste a cocher (raccourci « Logiciels »)
param([string]$Mode = 'gui', [string[]]$Keys = @())
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072
$Root = '@@ROOT@@'
$Log = Join-Path $Root 'apps.log'
New-Item -ItemType Directory -Force -Path $Root | Out-Null

$Order = @(@@ORDER@@)        # ordre d'installation (dependances d'abord)
$Display = @(@@DISPLAY@@)    # ordre d'affichage (par categorie)
$Names = @{ @@NAMES@@ }
$Categories = @{ @@CATEGORIES@@ }
$Blurbs = @{ @@BLURBS@@ }
$Requires = @{ @@REQUIRES@@ }
$First = @{ @@FIRST@@ }

function Log([string]$Text) {
    Write-Output $Text
    Add-Content -LiteralPath $Log -Value $Text -Encoding UTF8
}

# --- outils communs ------------------------------------------------------------------------------------
function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Test-Command([string]$Name) {
    Update-SessionPath
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

$script:Winget = $null
function Get-Winget {
    if ($script:Winget) { return $script:Winget }
    $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        $found = Get-ChildItem -Path "$env:ProgramFiles\WindowsApps" -Filter winget.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -like '*Microsoft.DesktopAppInstaller_*_x64__8wekyb3d8bbwe*' } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { $cmd = $found.FullName }
    }
    if (-not $cmd) {
        # Windows Server 2022 : winget absent. Module officiel Microsoft.WinGet.Client, qui l'installe.
        Log '  installation de winget (module Microsoft.WinGet.Client)'
        Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Force -Scope AllUsers | Out-Null
        Set-PSRepository -Name PSGallery -InstallationPolicy Trusted -ErrorAction SilentlyContinue
        Install-Module -Name Microsoft.WinGet.Client -Force -Scope AllUsers -AllowClobber -Repository PSGallery | Out-Null
        Import-Module Microsoft.WinGet.Client
        Repair-WinGetPackageManager -AllUsers -Latest -Force | Out-Null
        Update-SessionPath
        $cmd = Get-Command winget.exe -ErrorAction SilentlyContinue
        if (-not $cmd) {
            $found = Get-ChildItem -Path "$env:ProgramFiles\WindowsApps" -Filter winget.exe -Recurse -ErrorAction SilentlyContinue |
                Sort-Object FullName -Descending | Select-Object -First 1
            if ($found) { $cmd = $found.FullName }
        }
    }
    if (-not $cmd) { throw 'winget introuvable' }
    $script:Winget = if ($cmd -is [string]) { $cmd } else { $cmd.Source }
    return $script:Winget
}

function Initialize-WingetSources {
    # Compte qui ne s'est jamais connecté en session interactive (tâche planifiée pendant la construction) :
    # App Installer et la source « winget » ne sont pas encore enregistrés pour lui.
    try { Add-AppxPackage -RegisterByFamilyName -MainPackage Microsoft.DesktopAppInstaller_8wekyb3d8bbwe -ErrorAction Stop } catch {}
    Get-ChildItem -Path "$env:ProgramFiles\WindowsApps" -Directory -Filter 'Microsoft.Winget.Source_*' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1 | ForEach-Object {
            try { Add-AppxPackage -Register (Join-Path $_.FullName 'AppxManifest.xml') -DisableDevelopmentMode -ErrorAction Stop } catch {}
        }
    & (Get-Winget) source update --disable-interactivity 2>&1 | Out-Null
}

function Install-Winget([string]$Id, [string]$Scope = '', [string]$Source = 'winget', [string]$Override = '') {
    $w = Get-Winget
    $wargs = @('install', '--id', $Id, '--exact', '--source', $Source, '--silent', '--disable-interactivity',
               '--accept-package-agreements', '--accept-source-agreements')
    if ($Scope) { $wargs += @('--scope', $Scope) }
    if ($Override) { $wargs += @('--override', $Override) }
    & $w @wargs 2>&1 | ForEach-Object { "$_".Trim() } | Where-Object { $_ -and $_ -notmatch '^[\s\-\\|/]+$' -and $_ -notmatch '[\u2580-\u259F]' } |
        ForEach-Object { Log "  $_" }
    $code = $LASTEXITCODE
    Update-SessionPath
    # 0x8A15002B : deja installe, aucune mise a jour ; 0x8A150061 : deja installe.
    if ($code -ne 0 -and $code -ne -1978335189 -and $code -ne -1978335135) { throw ("winget {0} : code 0x{1:X8}" -f $Id, $code) }
}

function Test-Winget([string]$Id) {
    $w = Get-Winget
    $out = & $w list --id $Id --exact --accept-source-agreements --disable-interactivity 2>$null | Out-String
    return ($LASTEXITCODE -eq 0 -and $out -match [regex]::Escape($Id))
}

function Update-Winget([string]$Id) {
    $w = Get-Winget
    & $w upgrade --id $Id --exact --silent --disable-interactivity --accept-package-agreements --accept-source-agreements 2>&1 |
        Out-Null
}

function Get-LatestPythonId {
    # Plus récent « Python.Python.3.X » publié par la PSF sur winget, hors préversions.
    $w = Get-Winget
    $out = & $w search --id 'Python.Python.3.' --source winget --accept-source-agreements --disable-interactivity 2>$null | Out-String
    $minors = [regex]::Matches($out, 'Python\.Python\.3\.(\d+)') | ForEach-Object { [int]$_.Groups[1].Value } |
        Sort-Object -Unique -Descending
    foreach ($m in $minors) {
        $show = & $w show --id "Python.Python.3.$m" --exact --source winget --accept-source-agreements --disable-interactivity 2>$null | Out-String
        if ($show -match '(?m)^\s*Version\s*:\s*3\.\d+\.\d+\s*$') { return "Python.Python.3.$m" }
    }
    return 'Python.Python.3.13'
}

function Get-NpmAllowScripts([string]$Package) {
    # npm >= 11.16 n'execute plus les scripts d'installation sans autorisation explicite.
    Update-SessionPath
    $v = (& npm.cmd --version 2>$null | Out-String).Trim()
    try { if ([version]$v -ge [version]'11.16.0') { return @("--allow-scripts=$Package") } } catch {}
    return @()
}

function Test-Appx([string]$Pattern) {
    return [bool](Get-AppxPackage -Name $Pattern -ErrorAction SilentlyContinue)
}

function Install-Msix([string]$Url) {
    $file = Join-Path $env:TEMP ('rdpm-' + [guid]::NewGuid().ToString('N') + '.msix')
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $file
    try { Add-AppxPackage -Path $file -ErrorAction Stop } finally { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
}

function Install-Npm([string[]]$Packages) {
    Update-SessionPath
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'Node.js absent' }
    & npm.cmd install -g --no-fund --no-audit --loglevel=error @Packages 2>&1 | ForEach-Object { Log "  $_" }
    if ($LASTEXITCODE -ne 0) { throw "npm : code $LASTEXITCODE" }
    Update-SessionPath
}

function Invoke-WebScript([string]$Url, [string]$Arguments = '') {
    $file = Join-Path $env:TEMP ('rdpm-' + [guid]::NewGuid().ToString('N') + '.ps1')
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $file
    try {
        $p = Start-Process -FilePath powershell.exe -Wait -PassThru -NoNewWindow -ArgumentList (
            "-NoProfile -ExecutionPolicy Bypass -File `"$file`" $Arguments")
        if ($p.ExitCode -ne 0) { throw "script $Url : code $($p.ExitCode)" }
    } finally { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
    Update-SessionPath
}

function New-Shortcut([string]$Name, [string]$Target, [string]$Arguments = '', [string]$Icon = '') {
    $dir = [Environment]::GetFolderPath('CommonDesktopDirectory')
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $dir "$Name.lnk"))
    $s.TargetPath = $Target
    $s.Arguments = $Arguments
    if ($Icon) { $s.IconLocation = $Icon }
    $s.Save()
}

function New-TerminalShortcut([string]$Name, [string]$Command) {
    New-Shortcut $Name "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" "-NoExit -Command $Command"
}

# --- recettes (generees depuis le catalogue) ----------------------------------------------------------
@@RECIPES@@

# --- commandes -----------------------------------------------------------------------------------------
function Test-Installed([string]$Key) {
    try { return [bool](& "Test-$Key") } catch { return $false }
}

function Expand-Keys([string[]]$Wanted) {
    $set = @{}
    foreach ($k in $Wanted) { foreach ($p in ($k -split ',')) { if ($Names.ContainsKey($p.Trim())) { $set[$p.Trim()] = 1 } } }
    do {
        $changed = $false
        foreach ($k in @($set.Keys)) {
            foreach ($d in @($Requires[$k])) { if ($d -and -not $set.ContainsKey($d)) { $set[$d] = 1; $changed = $true } }
        }
    } while ($changed)
    return @($Order | Where-Object { $set.ContainsKey($_) })
}

function Write-FirstSteps {
    $dir = [Environment]::GetFolderPath('CommonDesktopDirectory')
    $lines = @('Cloud Desktop Manager - logiciels installes sur ce bureau', ('=' * 58), '',
               'Ajouter des logiciels : raccourci « Logiciels » sur le Bureau (ou depuis l''application Cloud Desktop Manager).',
               'Tout mettre a jour : raccourci « Mettre a jour les logiciels ».',
               'Comptes et cles d''API : connecte-toi dans chaque logiciel a son premier lancement.', '')
    $any = $false
    foreach ($k in $Display) {
        if (Test-Installed $k) {
            $any = $true
            $lines += ('- {0} : {1}' -f $Names[$k], $Blurbs[$k])
            if ($First[$k]) { $lines += ('    ' + $First[$k]) }
        }
    }
    if (-not $any) { $lines += '(aucun logiciel du catalogue pour l''instant)' }
    Set-Content -LiteralPath (Join-Path $dir 'Premiers pas.txt') -Value $lines -Encoding UTF8
}

function Install-ToolEntries {
    $self = Join-Path $Root 'apps.ps1'
    $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    New-Shortcut 'Logiciels' $ps "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$self`"" "$env:SystemRoot\System32\appwiz.cpl,0"
    New-Shortcut 'Mettre a jour les logiciels' $ps "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$self`" -Mode update" "$env:SystemRoot\System32\shell32.dll,238"
}

function Invoke-Install([string[]]$Wanted) {
    $keys = Expand-Keys $Wanted
    if ($keys.Count) { try { Initialize-WingetSources } catch { Log "  winget : $_" } }
    try { Install-ToolEntries } catch { Log "  raccourcis : $_" }
    $ok = @(); $failed = @(); $i = 0
    foreach ($k in $keys) {
        $i++
        Log ('RDPM-APP start {0}/{1} {2} {3}' -f $i, $keys.Count, $k, $Names[$k])
        if (Test-Installed $k) { Log "RDPM-APP ok $k (deja installe)"; $ok += $k; continue }
        try {
            & "Install-$k"
            if (Test-Installed $k) { Log "RDPM-APP ok $k"; $ok += $k }
            else { Log "RDPM-APP fail $k (introuvable apres installation)"; $failed += $k }
        } catch {
            Log "  $($_.Exception.Message)"
            Log "RDPM-APP fail $k"
            $failed += $k
        }
    }
    try { Write-FirstSteps } catch { Log "  premiers pas : $_" }
    Log ('RDPM-APPS-DONE ok={0} fail={1}' -f ($ok -join ' '), ($failed -join ' '))
}

function Invoke-Update {
    foreach ($k in $Order) {
        if ((Test-Installed $k) -and (Get-Command "Update-$k" -ErrorAction SilentlyContinue)) {
            Log "RDPM-APP update $k $($Names[$k])"
            try { & "Update-$k" } catch { Log "RDPM-APP fail $k (mise a jour) : $_" }
        }
    }
    try { Write-FirstSteps } catch {}
    Log 'RDPM-APPS-DONE update'
}

function Show-Picker {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    [Windows.Forms.Application]::EnableVisualStyles()
    $form = New-Object Windows.Forms.Form
    $form.Text = 'Logiciels'; $form.Width = 1000; $form.Height = 640; $form.StartPosition = 'CenterScreen'
    $info = New-Object Windows.Forms.Label
    $info.Text = 'Coche les logiciels a installer (les dependances sont ajoutees automatiquement).'
    $info.Dock = 'Top'; $info.Height = 30; $info.Padding = '8,8,8,0'
    $list = New-Object Windows.Forms.ListView
    $list.View = 'Details'; $list.CheckBoxes = $true; $list.FullRowSelect = $true; $list.Dock = 'Fill'
    [void]$list.Columns.Add('Logiciel', 190); [void]$list.Columns.Add('Categorie', 150)
    [void]$list.Columns.Add('Etat', 80); [void]$list.Columns.Add('Description', 540)
    foreach ($k in $Display) {
        $item = New-Object Windows.Forms.ListViewItem($Names[$k])
        [void]$item.SubItems.Add($Categories[$k])
        $state = if (Test-Installed $k) { 'installe' } else { '' }
        [void]$item.SubItems.Add($state)
        [void]$item.SubItems.Add($Blurbs[$k])
        $item.Tag = $k
        if ($state) { $item.ForeColor = [Drawing.Color]::Gray }
        [void]$list.Items.Add($item)
    }
    $panel = New-Object Windows.Forms.FlowLayoutPanel
    $panel.Dock = 'Bottom'; $panel.Height = 44; $panel.FlowDirection = 'RightToLeft'; $panel.Padding = '8'
    $go = New-Object Windows.Forms.Button; $go.Text = 'Installer'; $go.Width = 120
    $close = New-Object Windows.Forms.Button; $close.Text = 'Fermer'; $close.Width = 100
    $close.Add_Click({ $form.Close() })
    $go.Add_Click({
        $picked = @($list.CheckedItems | ForEach-Object { $_.Tag })
        if ($picked.Count -eq 0) { return }
        $self = Join-Path $Root 'apps.ps1'
        Start-Process powershell.exe -Verb RunAs -ArgumentList (
            "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$self`" -Mode install -Keys $($picked -join ',')")
        $form.Close()
    })
    $panel.Controls.AddRange(@($go, $close))
    $form.Controls.AddRange(@($list, $panel, $info))
    [void]$form.ShowDialog()
}

switch ($Mode) {
    'install' { Invoke-Install $Keys }
    'status' { Write-Output ('RDPM-APPS-STATUS ' + ((@($Order | Where-Object { Test-Installed $_ })) -join ' ')) }
    'update' { Invoke-Update }
    default { Show-Picker }
}
"""


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _hash(values: dict[str, str]) -> str:
    return "; ".join(f"{_ps_quote(k)} = {_ps_quote(v)}" for k, v in values.items())


def _indent(body: str) -> str:
    return "\n".join(("    " + line) if line.strip() else "" for line in body.strip("\n").splitlines())


def render_tool() -> str:
    apps = catalog.for_os(catalog.OS_WINDOWS)
    recipes = []
    for app in apps:
        r: catalog.WindowsRecipe = app.windows
        recipes.append(f"function Install-{app.key} {{\n{_indent(r.install)}\n}}")
        recipes.append(f"function Test-{app.key} {{\n{_indent(r.check)}\n}}")
        if r.update:
            recipes.append(f"function Update-{app.key} {{\n{_indent(r.update)}\n}}")
    values = {
        "VERSION": catalog.VERSION, "ROOT": ROOT,
        "ORDER": ", ".join(_ps_quote(k) for k in catalog.install_order()
                           if catalog.APPS_BY_KEY[k].available(catalog.OS_WINDOWS)),
        "DISPLAY": ", ".join(_ps_quote(a.key) for a in apps),
        "NAMES": _hash({a.key: a.name for a in apps}),
        "CATEGORIES": _hash({a.key: a.category for a in apps}),
        "BLURBS": _hash({a.key: a.blurb for a in apps}),
        "REQUIRES": "; ".join(f"{_ps_quote(a.key)} = @({', '.join(_ps_quote(d) for d in a.requires)})"
                              for a in apps if a.requires),
        "FIRST": _hash({a.key: a.first_step for a in apps if a.first_step}),
        "RECIPES": "\n\n".join(recipes),
    }
    text = _TOOL
    for key, value in values.items():
        text = text.replace(f"@@{key}@@", value)
    assert "@@" not in text, "variable de gabarit non remplacée"
    return text


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def deploy_snippet() -> str:
    """Lignes PowerShell qui déposent apps.ps1 (UTF-8 avec BOM) ; construction et installation depuis
    l'application (l'outil du bureau est remplacé par celui de cette version)."""
    return (f"New-Item -ItemType Directory -Force -Path '{ROOT}' | Out-Null\n"
            f"$bytes = [Convert]::FromBase64String('{_b64(render_tool())}')\n"
            f"[IO.File]::WriteAllText('{TOOL_PATH}', [Text.Encoding]::UTF8.GetString($bytes), "
            "(New-Object Text.UTF8Encoding $true))\n")


# --- scripts pilotés en SSH (OpenSSH Windows, compte administrateur) --------------------------------------
def apps_start(keys: list[str], password: str, mode: str = "install") -> str:
    """Dépose l'outil puis lance une tâche planifiée sous le compte administrateur du bureau (winget a besoin
    d'une vraie session utilisateur ; la tâche survit à la coupure SSH). Le mot de passe ne transite que par
    l'entrée standard SSH et n'est jamais écrit dans un fichier."""
    arg = f" -Keys {','.join(keys)}" if mode == "install" and keys else ""
    return f"""{TAG_PREFIX}win-apps-start
$ErrorActionPreference = 'Stop'
{deploy_snippet()}$task = Get-ScheduledTask -TaskName '{TASK_NAME}' -TaskPath '{TASK_PATH}' -ErrorAction SilentlyContinue
if ($task -and $task.State -eq 'Running') {{ Write-Output 'RDPM_BUSY'; exit 0 }}
Set-Content -LiteralPath '{APPS_LOG}' -Value '' -Encoding UTF8
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{TOOL_PATH}" -Mode {mode}{arg}')
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName '{TASK_NAME}' -TaskPath '{TASK_PATH}' -Action $action -Settings $settings -RunLevel Highest `
    -User $env:USERNAME -Password {_ps_quote(password)} -Force | Out-Null
Start-ScheduledTask -TaskName '{TASK_NAME}' -TaskPath '{TASK_PATH}'
Write-Output 'RDPM_STARTED'
"""


APPS_STATUS = f"""{TAG_PREFIX}win-apps-status
$task = Get-ScheduledTask -TaskName '{TASK_NAME}' -TaskPath '{TASK_PATH}' -ErrorAction SilentlyContinue
$state = if (-not $task) {{ 'unknown' }} elseif ($task.State -eq 'Running') {{ 'active' }} else {{ 'inactive' }}
Write-Output "RDPM_UNIT $state"
Write-Output 'RDPM_MARKS_BEGIN'
if (Test-Path '{APPS_LOG}') {{
    Get-Content -LiteralPath '{APPS_LOG}' -Encoding UTF8 | Where-Object {{ $_ -like 'RDPM-APP*' }} | Select-Object -Last 80
}}
Write-Output 'RDPM_TAIL_BEGIN'
if (Test-Path '{APPS_LOG}') {{ Get-Content -LiteralPath '{APPS_LOG}' -Encoding UTF8 -Tail 12 }}
"""

APPS_STOP = f"""{TAG_PREFIX}win-apps-stop
Stop-ScheduledTask -TaskName '{TASK_NAME}' -TaskPath '{TASK_PATH}' -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName '{TASK_NAME}' -TaskPath '{TASK_PATH}' -Confirm:$false -ErrorAction SilentlyContinue
Write-Output 'RDPM_STOPPED'
"""

# La tâche garde le mot de passe du compte : retirée dès la fin de l'installation.
APPS_CLEANUP = f"""{TAG_PREFIX}win-apps-cleanup
$task = Get-ScheduledTask -TaskName '{TASK_NAME}' -TaskPath '{TASK_PATH}' -ErrorAction SilentlyContinue
if ($task -and $task.State -ne 'Running') {{
    Unregister-ScheduledTask -TaskName '{TASK_NAME}' -TaskPath '{TASK_PATH}' -Confirm:$false -ErrorAction SilentlyContinue
}}
Write-Output 'RDPM_CLEANED'
"""


def apps_inventory() -> str:
    return f"""{TAG_PREFIX}win-apps-inventory
$ErrorActionPreference = 'Stop'
{deploy_snippet()}& powershell.exe -NoProfile -ExecutionPolicy Bypass -File '{TOOL_PATH}' -Mode status
"""

POSTINSTALL_DONE = f"""{TAG_PREFIX}win-postinstall-status
if (Test-Path 'C:\\rdpm-postinstall.done') {{ Write-Output 'RDPM_POSTINSTALL_DONE' }} else {{ Write-Output 'RDPM_POSTINSTALL_RUNNING' }}
"""


def render_admin_access_ps1(pubkey: str) -> str:
    """Active OpenSSH Server avec la clé d'administration de l'application (connexion par clé seulement).
    ASCII : sert au post-install (SetupComplete, SYSTEM) et, collé dans PowerShell administrateur, aux
    bureaux créés avant cette fonction. Le port 22 reste fermé par le pare-feu Hetzner hors opération."""
    key = pubkey.replace("'", "")
    return f"""$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($cap -and $cap.State -ne 'Installed') {{ Add-WindowsCapability -Online -Name $cap.Name | Out-Null }}
$sshDir = Join-Path $env:ProgramData 'ssh'
New-Item -ItemType Directory -Force -Path $sshDir | Out-Null
$keys = Join-Path $sshDir 'administrators_authorized_keys'
$line = '{key}'
$current = if (Test-Path $keys) {{ @(Get-Content -LiteralPath $keys) }} else {{ @() }}
if ($current -notcontains $line) {{ Set-Content -LiteralPath $keys -Value (@($current | Where-Object {{ $_ -notlike '*cloud-desktop-manager-admin*' }}) + $line) -Encoding ASCII }}
& icacls.exe $keys /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null
Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
$cfg = Join-Path $sshDir 'sshd_config'
if (Test-Path $cfg) {{
    $text = (Get-Content -LiteralPath $cfg) | Where-Object {{ $_ -notmatch '^\\s*(PasswordAuthentication|KbdInteractiveAuthentication)\\s' }}
    Set-Content -LiteralPath $cfg -Value (@('PasswordAuthentication no', 'KbdInteractiveAuthentication no') + $text) -Encoding ASCII
    Restart-Service sshd
}}
$rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if ($rule) {{ $rule | Set-NetFirewallRule -Profile Any -Enabled True }}
else {{ New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow -Profile Any | Out-Null }}
Get-Service sshd | Select-Object Name, Status, StartType | Out-String
"""
