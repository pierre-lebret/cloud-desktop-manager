"""Catalogue des logiciels prêts à l'emploi : IA agentique et développement, Windows et Linux.

Règles :
- chaque logiciel s'installe depuis son canal officiel (dépôt APT de l'éditeur à clé vérifiée, winget,
  npm, installeur officiel) et dans sa **dernière version** au moment de l'installation ;
- aucune clé d'API ni aucun compte n'est demandé : l'utilisateur se connecte dans chaque logiciel au premier
  lancement (rien de secret dans l'application ni dans les snapshots) ;
- un logiciel indisponible sur un système porte la raison, affichée grisée dans la liste.

`bit` est la position du logiciel dans le label `rdpm-apps` (masque hexadécimal) : elle ne change jamais et
n'est jamais réutilisée, même si un logiciel quitte le catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..constants import OS_LINUX, OS_WINDOWS

VERSION = "2026.09"

CAT_ASSISTANTS = "Assistants IA"
CAT_AGENTS = "Agents IA autonomes"
CAT_CODE_AGENTS = "Agents de code (terminal)"
CAT_EDITORS = "Éditeurs de code"
CAT_LANGS = "Langages"
CAT_TOOLS = "Outils"
CAT_LOCAL_AI = "IA locale"
CAT_BROWSERS = "Navigateur"
CATEGORIES = [CAT_ASSISTANTS, CAT_AGENTS, CAT_CODE_AGENTS, CAT_EDITORS, CAT_LANGS, CAT_TOOLS, CAT_LOCAL_AI,
              CAT_BROWSERS]

BADGE_ACCOUNT = "connexion requise"
BADGE_API_KEY = "clé d'API ou compte"
BADGE_CPU = "CPU seulement"
BADGE_HEAVY = "volumineux"
BADGE_PREVIEW = "préversion"


@dataclass(frozen=True)
class LinuxRecipe:
    install: str            # corps de la fonction bash (root ; `as_user` pour le compte du bureau)
    check: str              # corps bash : code 0 si installé
    update: str = ""        # mise à jour hors APT (les dépôts APT suivent les mises à jour automatiques)


@dataclass(frozen=True)
class WindowsRecipe:
    install: str            # corps PowerShell (compte administrateur du bureau)
    check: str              # expression PowerShell : $true si installé
    update: str = ""


@dataclass(frozen=True)
class App:
    key: str                # identifiant (lettres, chiffres, « _ ») : fonctions bash/PowerShell
    bit: int
    name: str
    category: str
    blurb: str
    linux: LinuxRecipe | str       # recette, ou raison de l'indisponibilité
    windows: WindowsRecipe | str
    first_step: str = ""
    requires: tuple[str, ...] = ()
    size_gb: float = 0.3
    minutes: float = 1.0
    badges: tuple[str, ...] = ()
    recommended: bool = False

    def recipe(self, os_name: str):
        return self.windows if os_name == OS_WINDOWS else self.linux

    def available(self, os_name: str) -> bool:
        return not isinstance(self.recipe(os_name), str)

    def reason(self, os_name: str) -> str | None:
        r = self.recipe(os_name)
        return r if isinstance(r, str) else None


def _win_winget(winget_id: str, test: str | None = None, scope: str = "", override: str = "") -> WindowsRecipe:
    extra = (f" -Scope {scope}" if scope else "") + (f" -Override '{override}'" if override else "")
    return WindowsRecipe(install=f"Install-Winget '{winget_id}'{extra}",
                         check=test or f"Test-Winget '{winget_id}'",
                         update=f"Update-Winget '{winget_id}'")


def _npm(package: str, command: str) -> tuple[LinuxRecipe, WindowsRecipe]:
    return (LinuxRecipe(install=f"npm_global {package}@latest", check=f"have {command}",
                        update=f"npm_global {package}@latest"),
            WindowsRecipe(install=f"Install-Npm @('{package}@latest')", check=f"Test-Command '{command}'",
                          update=f"Install-Npm @('{package}@latest')"))


_CODEX = _npm("@openai/codex", "codex")
_COPILOT = _npm("@github/copilot", "copilot")
_OPENCODE = _npm("opencode-ai", "opencode")
_OPENCLAW = (
    LinuxRecipe(install="npm_global $(npm_allow_scripts openclaw) openclaw@latest\n"
                        "loginctl enable-linger \"$DESKTOP_USER\" 2>/dev/null || true",
                check="have openclaw", update="npm_global $(npm_allow_scripts openclaw) openclaw@latest"),
    WindowsRecipe(install="Install-Npm @((Get-NpmAllowScripts 'openclaw') + 'openclaw@latest')",
                  check="Test-Command 'openclaw'",
                  update="Install-Npm @((Get-NpmAllowScripts 'openclaw') + 'openclaw@latest')"))

NO_NESTED_VIRT = "Indisponible : les serveurs cloud Hetzner n'offrent pas la virtualisation imbriquée."

APPS: list[App] = [
    # --- Assistants IA -----------------------------------------------------------------------------------
    App("claude_desktop", 0, "Claude Desktop", CAT_ASSISTANTS,
        "L'application officielle d'Anthropic : discussion, projets, Claude Code intégré.",
        LinuxRecipe(
            install="printf 'CLAUDE_DESKTOP_ADD_REPO=\"false\"\\n' > /etc/default/claude-desktop\n"
                    "apt_repo claude-desktop https://downloads.claude.ai/claude-desktop/key.asc "
                    "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE "
                    "\"deb [arch=amd64,arm64 signed-by=@KEYRING@] https://downloads.claude.ai/claude-desktop/apt/stable stable main\"\n"
                    "apt_install claude-desktop",
            check="dpkg -s claude-desktop >/dev/null 2>&1"),
        _win_winget("Anthropic.Claude"),
        first_step="Menu → Claude, puis connecte-toi (Cowork indisponible : pas de virtualisation imbriquée).",
        size_gb=0.6, minutes=1.5, badges=(BADGE_ACCOUNT,)),
    App("chatgpt", 1, "ChatGPT", CAT_ASSISTANTS,
        "L'application officielle d'OpenAI, avec l'agent de code Codex.",
        LinuxRecipe(
            # .deb officiel ; son script d'installation ajoute le dépôt APT d'OpenAI (mises à jour par apt).
            install="local deb\ndeb=$(mktemp --suffix=.deb)\nchmod 0644 \"$deb\"\n"
                    "fetch \"https://persistent.oaistatic.com/codex-app-prod/linux/deb/latest/chatgpt_${ARCH}.deb\" "
                    "\"$deb\"\napt_install \"$deb\"\nrm -f \"$deb\"",
            check="dpkg -s chatgpt >/dev/null 2>&1"),
        WindowsRecipe(
            # Microsoft Store (souvent indisponible sous Windows Server), sinon MSIX officiel d'OpenAI,
            # sinon application web (Edge).
            install="try { Install-Winget '9PLM9XGG6VKS' -Source msstore }\n"
                    "catch {\n"
                    "    Log \"  Microsoft Store indisponible : MSIX officiel d'OpenAI\"\n"
                    "    try { Install-Msix 'https://persistent.oaistatic.com/codex-app-prod/ChatGPT-x64.msix' }\n"
                    "    catch {\n"
                    "        Log \"  MSIX refusé ($_) : ChatGPT installé comme application web (Edge)\"\n"
                    "        New-Shortcut 'ChatGPT' \"${env:ProgramFiles(x86)}\\Microsoft\\Edge\\Application\\msedge.exe\" "
                    "'--app=https://chatgpt.com'\n"
                    "    }\n"
                    "}",
            check="(Test-Appx '*ChatGPT*') -or (Test-Path (Join-Path ([Environment]::GetFolderPath("
                  "'CommonDesktopDirectory')) 'ChatGPT.lnk'))",
            update="Update-Winget '9PLM9XGG6VKS'"),
        first_step="Menu → ChatGPT, puis connecte-toi à ton compte OpenAI.",
        size_gb=0.5, minutes=1.5, badges=(BADGE_ACCOUNT, BADGE_PREVIEW)),
    # --- Agents IA autonomes -----------------------------------------------------------------------------------
    App("openclaw", 2, "OpenClaw", CAT_AGENTS,
        "Assistant personnel autonome open source (messageries, navigateur, tâches planifiées).",
        _OPENCLAW[0], _OPENCLAW[1],
        first_step="Terminal : openclaw onboard --install-daemon (modèle, clé d'API, canaux).",
        requires=("node",), size_gb=0.4, minutes=1.5, badges=(BADGE_API_KEY,)),
    App("hermes", 3, "Hermes Agent", CAT_AGENTS,
        "Agent autonome open source de Nous Research, qui apprend de ses tâches (CLI, passerelles).",
        LinuxRecipe(
            install="local s\ns=$(mktemp)\n"
                    "fetch https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh \"$s\"\n"
                    "chmod 0755 \"$s\"\n"
                    "as_user \"bash '$s' --non-interactive </dev/null\"\nrm -f \"$s\"",
            check="user_has .local/bin/hermes",
            update="as_user \"\\$HOME/.local/bin/hermes update </dev/null\""),
        WindowsRecipe(
            install="Invoke-WebScript 'https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.ps1' "
                    "'-NonInteractive'",
            check="Test-Command 'hermes'",
            update="& hermes update"),
        first_step="Terminal : hermes setup (modèle et clé d'API), puis hermes.",
        requires=("git",), size_gb=0.8, minutes=3, badges=(BADGE_API_KEY,)),
    # --- Agents de code ----------------------------------------------------------------------------------------
    App("claude_code", 4, "Claude Code", CAT_CODE_AGENTS,
        "L'agent de code d'Anthropic dans le terminal (dépôt officiel, mises à jour automatiques).",
        LinuxRecipe(
            install="apt_repo claude-code https://downloads.claude.ai/keys/claude-code.asc "
                    "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE "
                    "\"deb [signed-by=@KEYRING@] https://downloads.claude.ai/claude-code/apt/stable stable main\"\n"
                    "apt_install claude-code\n"
                    "menu_entry claude_code 'Claude Code' claude",
            check="have claude"),
        WindowsRecipe(
            install="Invoke-WebScript 'https://claude.ai/install.ps1'\n"
                    "New-TerminalShortcut 'Claude Code' 'claude'",
            check="(Test-Command 'claude') -or (Test-Path \"$env:USERPROFILE\\.local\\bin\\claude.exe\")"),
        first_step="Terminal : claude (connexion avec ton compte Claude ou une clé d'API).",
        requires=("git",), size_gb=0.2, minutes=1, badges=(BADGE_ACCOUNT,), recommended=True),
    App("codex", 5, "Codex CLI", CAT_CODE_AGENTS,
        "L'agent de code d'OpenAI dans le terminal.",
        _CODEX[0], _CODEX[1], first_step="Terminal : codex (connexion ChatGPT ou clé d'API).",
        requires=("node", "git"), size_gb=0.2, minutes=1, badges=(BADGE_ACCOUNT,), recommended=True),
    App("copilot_cli", 6, "GitHub Copilot CLI", CAT_CODE_AGENTS,
        "L'agent GitHub Copilot dans le terminal (abonnement Copilot).",
        _COPILOT[0], _COPILOT[1], first_step="Terminal : copilot, puis /login.",
        requires=("node", "git"), size_gb=0.2, minutes=1, badges=(BADGE_ACCOUNT,)),
    App("opencode", 7, "OpenCode", CAT_CODE_AGENTS,
        "Agent de code open source, compatible avec la plupart des fournisseurs de modèles.",
        _OPENCODE[0], _OPENCODE[1], first_step="Terminal : opencode, puis /connect pour choisir un fournisseur.",
        requires=("node", "git"), size_gb=0.2, minutes=1, badges=(BADGE_API_KEY,)),
    App("cursor_cli", 8, "Cursor CLI", CAT_CODE_AGENTS,
        "L'agent de Cursor dans le terminal (compte Cursor).",
        LinuxRecipe(
            install="local s\ns=$(mktemp)\nfetch https://cursor.com/install \"$s\"\nchmod 0755 \"$s\"\n"
                    "as_user \"bash '$s' </dev/null\"\nrm -f \"$s\"",
            check="user_has .local/bin/cursor-agent || user_has .local/bin/agent"),
        WindowsRecipe(
            install="Invoke-WebScript 'https://cursor.com/install?win32=true'",
            check="(Test-Command 'cursor-agent') -or (Test-Command 'agent')"),
        first_step="Terminal : cursor-agent login, puis cursor-agent.",
        requires=("git",), size_gb=0.2, minutes=1, badges=(BADGE_ACCOUNT,)),
    App("antigravity_cli", 25, "Google Antigravity CLI", CAT_CODE_AGENTS,
        "L'agent de code de Google (Gemini) dans le terminal, successeur de Gemini CLI.",
        LinuxRecipe(
            install="local s\ns=$(mktemp)\nfetch https://antigravity.google/cli/install.sh \"$s\"\nchmod 0755 \"$s\"\n"
                    "as_user \"bash '$s' </dev/null\"\nrm -f \"$s\"",
            check="user_has .local/bin/agy"),
        WindowsRecipe(
            install="Invoke-WebScript 'https://antigravity.google/cli/install.ps1'",
            check="(Test-Command 'agy') -or (Test-Path \"$env:LOCALAPPDATA\\Antigravity\\agy.exe\")"),
        first_step="Terminal : agy (connexion avec ton compte Google).",
        requires=("git",), size_gb=0.2, minutes=1, badges=(BADGE_ACCOUNT,)),
    # --- Éditeurs -----------------------------------------------------------------------------------------------
    App("vscode", 9, "Visual Studio Code", CAT_EDITORS,
        "L'éditeur de Microsoft, avec GitHub Copilot et des milliers d'extensions.",
        LinuxRecipe(
            install="echo 'code code/add-microsoft-repo boolean false' | debconf-set-selections\n"
                    "apt_repo vscode https://packages.microsoft.com/keys/microsoft.asc "
                    "BC528686B50D79E339D3721CEB3E94ADBE1229CF "
                    "\"deb [arch=amd64,arm64 signed-by=@KEYRING@] https://packages.microsoft.com/repos/code stable main\"\n"
                    "apt_install code",
            check="dpkg -s code >/dev/null 2>&1"),
        _win_winget("Microsoft.VisualStudioCode", scope="machine",
                    override="/VERYSILENT /NORESTART /MERGETASKS=!runcode,addcontextmenufiles,"
                             "addcontextmenufolders,associatewithfiles,addtopath"),
        first_step="Menu → Visual Studio Code (commande « code » dans le terminal).",
        size_gb=0.5, minutes=1.5, recommended=True),
    App("cursor", 10, "Cursor", CAT_EDITORS,
        "L'éditeur de code centré sur l'IA (compte Cursor).",
        LinuxRecipe(
            install="apt_repo cursor https://downloads.cursor.com/keys/anysphere.asc "
                    "380FF4BCDC34A4BD92A3565342A1772E62E492D6 "
                    "\"deb [arch=amd64,arm64 signed-by=@KEYRING@] https://downloads.cursor.com/aptrepo stable main\"\n"
                    "apt_install cursor",
            check="dpkg -s cursor >/dev/null 2>&1"),
        _win_winget("Anysphere.Cursor"),
        first_step="Menu → Cursor, puis connecte-toi.", size_gb=0.7, minutes=2, badges=(BADGE_ACCOUNT,)),
    App("antigravity", 26, "Google Antigravity", CAT_EDITORS,
        "L'éditeur de Google centré sur les agents (Gemini).",
        "Pas encore de paquet Linux installable automatiquement (archive sans adresse stable).",
        WindowsRecipe(install="Install-Winget 'Google.AntigravityIDE'", check="Test-Winget 'Google.AntigravityIDE'",
                      update="Update-Winget 'Google.AntigravityIDE'"),
        first_step="Menu Démarrer → Antigravity, puis connecte-toi avec ton compte Google.",
        size_gb=0.7, minutes=2, badges=(BADGE_ACCOUNT,)),
    # --- Langages --------------------------------------------------------------------------------------------------
    App("python", 11, "Python + uv", CAT_LANGS,
        "Python 3 avec pip et venv, et uv (Astral) pour les projets et les dernières versions de Python.",
        LinuxRecipe(
            install="apt_install python3 python3-venv python3-pip python3-dev pipx\n"
                    "install_uv",
            check="have python3 && have uv",
            update="install_uv"),
        WindowsRecipe(
            install="Install-Winget (Get-LatestPythonId) -Scope machine\nInstall-Winget 'astral-sh.uv'",
            check="(Test-Command 'python') -and (Test-Command 'uv')",
            update="Update-Winget (Get-LatestPythonId)\nUpdate-Winget 'astral-sh.uv'"),
        first_step="Terminal : python3 --version · uv init mon-projet", size_gb=0.3, minutes=1,
        recommended=True),
    App("node", 12, "Node.js LTS", CAT_LANGS,
        "Dernière version LTS officielle (nodejs.org), avec npm et pnpm.",
        LinuxRecipe(
            install="install_node\nnpm_global pnpm@latest",
            check="have node && have npm",
            update="install_node\nnpm update -g --no-fund --no-audit || true"),
        WindowsRecipe(
            install="Install-Winget 'OpenJS.NodeJS.LTS' -Scope machine\nInstall-Npm @('pnpm@latest')",
            check="Test-Command 'node'",
            update="Update-Winget 'OpenJS.NodeJS.LTS'\n& npm.cmd update -g --no-fund --no-audit"),
        first_step="Terminal : node --version · npm create vite@latest", size_gb=0.2, minutes=1,
        recommended=True),
    App("bun", 13, "Bun", CAT_LANGS,
        "Environnement JavaScript/TypeScript tout-en-un très rapide (exécution, paquets, tests).",
        LinuxRecipe(
            install="apt_install unzip\nlocal s\ns=$(mktemp)\nfetch https://bun.sh/install \"$s\"\nchmod 0755 \"$s\"\n"
                    "as_user \"bash '$s' </dev/null\"\nrm -f \"$s\"",
            check="user_has .bun/bin/bun",
            update="as_user \"\\$HOME/.bun/bin/bun upgrade </dev/null\""),
        _win_winget("Oven-sh.Bun"),
        first_step="Terminal : bun --version", size_gb=0.1, minutes=0.5),
    App("go", 14, "Go", CAT_LANGS, "Le langage Go (dernière version officielle, go.dev).",
        LinuxRecipe(install="install_go", check="[ -x /usr/local/go/bin/go ]", update="install_go"),
        _win_winget("GoLang.Go", scope="machine"),
        first_step="Terminal : go version", size_gb=0.3, minutes=1),
    App("rust", 15, "Rust", CAT_LANGS, "Rust via rustup (cargo, rustc), chaîne stable.",
        LinuxRecipe(
            install="apt_install build-essential pkg-config libssl-dev\ninstall_rustup",
            check="user_has .cargo/bin/cargo",
            update="as_user \"\\$HOME/.cargo/bin/rustup update </dev/null\""),
        WindowsRecipe(
            install="Install-Winget 'Microsoft.VisualStudio.2022.BuildTools' -Override "
                    "'--quiet --wait --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools "
                    "--includeRecommended'\n"
                    "Install-Winget 'Rustlang.Rustup'\n"
                    "Update-SessionPath\n& rustup default stable 2>&1 | ForEach-Object { Log \"  $_\" }",
            check="Test-Command 'cargo'",
            update="& rustup update"),
        first_step="Terminal : cargo new mon-projet", size_gb=1.5, minutes=4, badges=(BADGE_HEAVY,)),
    App("java", 16, "Java (JDK LTS)", CAT_LANGS, "Le JDK OpenJDK dans sa dernière version LTS (Temurin sous Windows).",
        LinuxRecipe(
            install="local jdk\njdk=$(apt-cache search --names-only '^openjdk-[0-9]+-jdk$' | awk '{print $1}' "
                    "| sort -t- -k2 -n | tail -n1)\n"
                    "[ -n \"$jdk\" ] || jdk=default-jdk\napt_install \"$jdk\"",
            check="have javac"),
        _win_winget("EclipseAdoptium.Temurin.25.JDK", scope="machine"),
        first_step="Terminal : java -version", size_gb=0.5, minutes=1.5),
    App("dotnet", 17, ".NET SDK", CAT_LANGS, "Le SDK .NET de Microsoft (dernière version).",
        LinuxRecipe(install="install_dotnet", check="have dotnet"),
        _win_winget("Microsoft.DotNet.SDK.10"),
        first_step="Terminal : dotnet new console", size_gb=0.8, minutes=2),
    # --- Outils ------------------------------------------------------------------------------------------------------
    App("git", 18, "Git", CAT_TOOLS, "Gestion de versions, avec Git LFS.",
        LinuxRecipe(install="apt_install git git-lfs", check="have git"),
        _win_winget("Git.Git", scope="machine", test="Test-Command 'git'"),
        first_step="Terminal : git config --global user.name \"Ton nom\"", size_gb=0.1, minutes=0.5,
        recommended=True),
    App("gh", 19, "GitHub CLI", CAT_TOOLS, "GitHub en ligne de commande (dépôts, pull requests, actions).",
        LinuxRecipe(
            install="apt_repo github-cli https://cli.github.com/packages/githubcli-archive-keyring.gpg "
                    "2C6106201985B60E6C7AC87323F3D4EA75716059 "
                    "\"deb [arch=$ARCH signed-by=@KEYRING@] https://cli.github.com/packages stable main\"\n"
                    "apt_install gh",
            check="have gh"),
        _win_winget("GitHub.cli", test="Test-Command 'gh'"),
        first_step="Terminal : gh auth login", requires=("git",), size_gb=0.1, minutes=0.5,
        badges=(BADGE_ACCOUNT,), recommended=True),
    App("docker", 20, "Docker", CAT_TOOLS, "Docker Engine et Compose (conteneurs), utilisable sans sudo.",
        LinuxRecipe(install="install_docker", check="have docker"),
        "Indisponible sous Windows : Docker Desktop exige la virtualisation imbriquée, absente des serveurs "
        "cloud Hetzner (utilise un bureau Linux).",
        first_step="Terminal : docker run hello-world (après une reconnexion)", size_gb=0.5, minutes=1.5),
    App("cli_tools", 21, "Outils en ligne de commande", CAT_TOOLS,
        "ripgrep, fd, jq, fzf, tmux, htop, bat, tree : ce que les agents et les développeurs utilisent.",
        LinuxRecipe(
            install="apt_install ripgrep fd-find jq fzf tmux htop bat tree unzip zip build-essential\n"
                    "[ -e /usr/local/bin/fd ] || ln -s \"$(command -v fdfind)\" /usr/local/bin/fd\n"
                    "[ -e /usr/local/bin/bat ] || ! have batcat || ln -s \"$(command -v batcat)\" /usr/local/bin/bat",
            check="have rg && have jq && have fzf"),
        WindowsRecipe(
            install="foreach ($id in 'BurntSushi.ripgrep.MSVC', 'sharkdp.fd', 'jqlang.jq', 'junegunn.fzf', "
                    "'sharkdp.bat') { Install-Winget $id }",
            check="(Test-Command 'rg') -and (Test-Command 'jq')",
            update="foreach ($id in 'BurntSushi.ripgrep.MSVC', 'sharkdp.fd', 'jqlang.jq', 'junegunn.fzf', "
                   "'sharkdp.bat') { Update-Winget $id }"),
        size_gb=0.1, minutes=0.5),
    App("terminal", 22, "Windows Terminal + PowerShell 7", CAT_TOOLS,
        "Le terminal moderne de Microsoft et la dernière version de PowerShell.",
        "Windows seulement (le bureau Linux a déjà son terminal).",
        WindowsRecipe(
            install="Install-Winget 'Microsoft.PowerShell' -Scope machine\nInstall-Winget 'Microsoft.WindowsTerminal'",
            check="Test-Winget 'Microsoft.PowerShell'",
            update="Update-Winget 'Microsoft.PowerShell'\nUpdate-Winget 'Microsoft.WindowsTerminal'"),
        size_gb=0.3, minutes=1),
    # --- IA locale --------------------------------------------------------------------------------------------------
    App("ollama", 23, "Ollama", CAT_LOCAL_AI,
        "Modèles d'IA ouverts en local. Sans GPU : petits modèles seulement, et lentement.",
        LinuxRecipe(
            install="apt_install zstd\nlocal s\ns=$(mktemp)\nfetch https://ollama.com/install.sh \"$s\"\n"
                    "sh \"$s\" </dev/null\nrm -f \"$s\"",
            check="have ollama"),
        _win_winget("Ollama.Ollama"),
        first_step="Terminal : ollama run qwen3:1.7b (petit modèle, adapté au CPU)", size_gb=1.0, minutes=2,
        badges=(BADGE_CPU,)),
    # --- Navigateur -------------------------------------------------------------------------------------------------
    App("chrome", 24, "Google Chrome", CAT_BROWSERS,
        "Le navigateur de Google, utilisé aussi par les agents pour piloter le web.",
        LinuxRecipe(
            # .deb officiel : son script d'installation ajoute le dépôt et la clé de Google (mises à jour par apt).
            install="[ \"$ARCH\" = amd64 ] || { echo 'Chrome : amd64 seulement'; return 1; }\n"
                    "local deb\ndeb=$(mktemp --suffix=.deb)\nchmod 0644 \"$deb\"\n"
                    "fetch https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \"$deb\"\n"
                    "apt_install \"$deb\"\nrm -f \"$deb\"\n"
                    "printf 'Unattended-Upgrade::Origins-Pattern { \"site=dl.google.com\"; };\\n' "
                    "> /etc/apt/apt.conf.d/52rdpm-google-chrome",
            check="have google-chrome-stable"),
        _win_winget("Google.Chrome", scope="machine"),
        size_gb=0.4, minutes=1, recommended=True),
]

APPS_BY_KEY: dict[str, App] = {a.key: a for a in APPS}


def for_os(os_name: str) -> list[App]:
    return [a for a in APPS if a.available(os_name)]


def recommended(os_name: str) -> list[str]:
    return [a.key for a in APPS if a.recommended and a.available(os_name)]


def resolve(keys, os_name: str) -> list[str]:
    """Clés demandées + dépendances (disponibles sur ce système), dans l'ordre d'installation."""
    want: set[str] = set()
    stack = [k for k in keys if k in APPS_BY_KEY]
    while stack:
        key = stack.pop()
        app = APPS_BY_KEY[key]
        if key in want or not app.available(os_name):
            continue
        want.add(key)
        stack.extend(app.requires)
    return [k for k in install_order() if k in want]


def install_order() -> list[str]:
    """Ordre du catalogue, corrigé pour que chaque dépendance passe avant ce qui la requiert."""
    done: list[str] = []

    def visit(key: str, path: tuple[str, ...] = ()) -> None:
        if key in done:
            return
        if key in path:
            raise ValueError(f"dépendance circulaire : {' → '.join(path + (key,))}")
        for dep in APPS_BY_KEY[key].requires:
            visit(dep, path + (key,))
        done.append(key)

    for app in APPS:
        visit(app.key)
    return done


def added_by_dependencies(selected, os_name: str) -> dict[str, list[str]]:
    """Dépendances ajoutées automatiquement → noms des logiciels qui les requièrent."""
    chosen = set(selected)
    out: dict[str, list[str]] = {}
    for key in resolve(chosen, os_name):
        if key in chosen:
            continue
        out[key] = [APPS_BY_KEY[k].name for k in resolve(chosen, os_name)
                    if key in APPS_BY_KEY[k].requires and k != key]
    return out


def estimate(keys, os_name: str) -> tuple[float, float]:
    """(minutes, Go ajoutés au disque et au snapshot) pour installer ces logiciels et leurs dépendances."""
    apps = [APPS_BY_KEY[k] for k in resolve(keys, os_name)]
    minutes = sum(a.minutes for a in apps)
    if os_name == OS_WINDOWS and apps:
        minutes += 3   # préparation de winget
    return minutes, sum(a.size_gb for a in apps)


def encode(keys) -> str:
    """Masque hexadécimal des logiciels installés, pour le label `rdpm-apps` (« » si aucun)."""
    mask = 0
    for key in keys:
        app = APPS_BY_KEY.get(key)
        if app:
            mask |= 1 << app.bit
    return format(mask, "x") if mask else ""


def decode(value: str | None) -> set[str]:
    try:
        mask = int(value or "0", 16)
    except ValueError:
        return set()
    return {a.key for a in APPS if mask >> a.bit & 1}


def names(keys) -> list[str]:
    return [APPS_BY_KEY[k].name for k in keys if k in APPS_BY_KEY]


__all__ = ["APPS", "APPS_BY_KEY", "App", "CATEGORIES", "LinuxRecipe", "OS_LINUX", "OS_WINDOWS", "VERSION",
           "WindowsRecipe", "added_by_dependencies", "decode", "encode", "estimate", "for_os", "install_order",
           "names", "recommended", "resolve"]
