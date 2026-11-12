# Installs CViz (Windows, PowerShell 5.1 or later):
#
#   irm https://github.com/polysok/code-visualizer/releases/latest/download/install.ps1 | iex
#
# Installs uv if it is missing (in your profile, no admin rights; uv brings
# its own Python), then `cviz` with the local embedding model, then offers to
# open the graph of the Git repository you run it from. Run it again to upgrade.
#
# Environment:
#   CVIZ_VERSION   release to install (vX.Y.Z); default: the release this script comes from
#   CVIZ_WHEEL     wheel to install instead (URL or file path)
#   CVIZ_NO_START  1: install only, do not offer to open the graph
#   CVIZ_PYTHON    Python version for the tool (default 3.12)

$ReleaseTag = ""  # written by the release workflow

# Everything runs in a function: under `irm | iex`, `exit` would close the
# user's window.
function Install-CViz {
    # "Continue", whatever the caller has (GitHub Actions and some profiles set
    # "Stop"): with "Stop", Windows PowerShell turns anything a native command
    # (git, uv) writes on stderr into a terminating error. Scoped to this function.
    $ErrorActionPreference = "Continue"
    $RepoUrl = "https://github.com/polysok/code-visualizer"
    $Python = if ($env:CVIZ_PYTHON) { $env:CVIZ_PYTHON } else { "3.12" }
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

    # 1. uv
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        foreach ($dir in @($env:XDG_BIN_HOME, "$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin")) {
            if ($dir -and (Test-Path (Join-Path $dir "uv.exe"))) { $env:Path = "$dir;$env:Path"; break }
        }
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host "Installing uv (https://docs.astral.sh/uv/)..."
        powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
        if ($LASTEXITCODE -ne 0) { throw "the uv installer failed (exit code $LASTEXITCODE)" }
        $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
        if ($env:XDG_BIN_HOME) { $env:Path = "$env:XDG_BIN_HOME;$env:Path" }
        if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
            throw "uv was installed but cannot be found; open a new terminal and run this again"
        }
    }

    # 2. cviz
    if ($env:CVIZ_WHEEL) {
        $wheel = $env:CVIZ_WHEEL
    } else {
        $tag = if ($env:CVIZ_VERSION) { $env:CVIZ_VERSION } else { $ReleaseTag }
        if (-not $tag) {
            try { $tag = (Invoke-RestMethod "https://api.github.com/repos/polysok/code-visualizer/releases/latest" -UseBasicParsing -ErrorAction Stop).tag_name }
            catch { throw "cannot find the latest release at $RepoUrl/releases ($_); set CVIZ_VERSION=vX.Y.Z" }
        }
        if ($tag -match '^\d') { $tag = "v$tag" }
        if ($tag -notmatch '^v\d') { throw "not a release tag: '$tag'; set CVIZ_VERSION=vX.Y.Z" }
        $wheel = "$RepoUrl/releases/download/$tag/code_visualizer-$($tag.Substring(1))-py3-none-any.whl"
    }

    Write-Host "Installing cviz from $wheel ..."
    uv tool install --force --python $Python "code-visualizer[semantic] @ $wheel"
    if ($LASTEXITCODE -ne 0) { throw "uv tool install failed (exit code $LASTEXITCODE)" }

    $binDir = (uv tool dir --bin).Trim()
    $cviz = Join-Path $binDir "cviz.exe"
    if (-not (Test-Path $cviz)) { throw "the install finished but $cviz is missing" }
    $onPath = ($env:Path -split ';') -contains $binDir
    if (-not $onPath) {
        uv tool update-shell *> $null
        $env:Path = "$binDir;$env:Path"  # this window too
    }
    Write-Host ""
    # `cviz version` prints on stderr, which Windows PowerShell would wrap as an error: let cmd merge it.
    $version = (cmd /c "`"$cviz`" version 2>&1" | Select-Object -Last 1)
    Write-Host "$version is installed."
    if (-not $onPath) { Write-Host "Other terminals get the 'cviz' command once reopened (it is in $binDir)." }

    # 3. The graph of the repository we are in
    $repo = $null
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $repo = git rev-parse --show-toplevel 2>$null
        if ($LASTEXITCODE -ne 0) { $repo = $null }
    }
    if (-not $repo -or $env:CVIZ_NO_START -eq "1" -or -not [Environment]::UserInteractive) {
        Write-Host "Open the graph of a repository:  cd C:\path\to\repo; cviz graph --watch ."
        return
    }
    $repo = $repo.Trim() -replace '/', '\'
    $answer = Read-Host "Open the graph of $repo now? It is indexed first if needed. [Y/n]"
    if (-not $answer -or $answer -match '^[Yy]') {
        Write-Host "Starting the graph server (Ctrl+C stops it; 'cviz graph --watch .' starts it again)..."
        & $cviz graph --watch $repo
    } else {
        Write-Host "Open it later with:  cd $repo; cviz graph --watch ."
    }
}

try { Install-CViz } catch { Write-Host "cviz install: $_" -ForegroundColor Red }
