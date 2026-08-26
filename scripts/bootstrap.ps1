<#
    CodeLens bootstrap - Windows / PowerShell

    Verifies locally, publishes to GitHub, configures the secret and workflow
    permissions, then opens a test pull request so you can watch the review run.

    Run from inside the extracted codelens folder:

        powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1

    Prerequisites (checked below):
      - Python 3.10+          https://python.org
      - git                   https://git-scm.com
      - GitHub CLI, signed in https://cli.github.com   then:  gh auth login

    Your Groq key is typed directly into `gh secret set`. It goes from your
    keyboard to GitHub - it is never written to a file or echoed to the screen.
#>

$ErrorActionPreference = "Stop"

function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Ok($text)       { Write-Host "    OK  $text" -ForegroundColor Green }
function Fail($text)     { Write-Host "    !!  $text" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- checks ----
Step 1 "Checking prerequisites"

try { $pyVersion = (python --version 2>&1) } catch { Fail "Python not found. Install Python 3.10+ and reopen this terminal." }
if ($pyVersion -notmatch "Python 3\.(1[0-9]|[2-9][0-9])") {
    Fail "Need Python 3.10 or newer. Found: $pyVersion"
}
Ok $pyVersion

try { git --version | Out-Null } catch { Fail "git not found. Install from https://git-scm.com" }
Ok "git present"

try { gh --version | Out-Null } catch { Fail "GitHub CLI not found. Install from https://cli.github.com, then run: gh auth login" }

$authOutput = (gh auth status 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0) { Fail "GitHub CLI is not signed in. Run:  gh auth login" }
Ok "GitHub CLI signed in"

# The workflow file can only be pushed by a token carrying the workflow scope.
if ($authOutput -notmatch "workflow") {
    Write-Host "    Adding the 'workflow' scope (needed to push .github/workflows)" -ForegroundColor Yellow
    gh auth refresh -h github.com -s workflow
    if ($LASTEXITCODE -ne 0) { Fail "Could not add the workflow scope." }
}
Ok "workflow scope present"

# ------------------------------------------------------- local verification --
Step 2 "Installing dependencies and running the test suite"

if (-not (Test-Path ".venv")) { python -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
Ok "dependencies installed"

& .\.venv\Scripts\python.exe -m pytest tests -q
if ($LASTEXITCODE -ne 0) { Fail "Tests failed. Fix this before publishing - CI will fail the same way." }
Ok "41 tests passed"

Step 3 "Running the demo so you can see it find real bugs"
& .\.venv\Scripts\python.exe demo\make_demo_repo.py "$env:TEMP\codelens-demo" | Out-Null
& .\.venv\Scripts\python.exe -m codelens.cli index "$env:TEMP\codelens-demo" | Out-Null
& .\.venv\Scripts\python.exe -m codelens.cli review "$env:TEMP\codelens-demo"

# --------------------------------------------------------------- publish ----
Step 4 "Publishing to GitHub"

$repoName = Read-Host "    Repository name [codelens]"
if ([string]::IsNullOrWhiteSpace($repoName)) { $repoName = "codelens" }

# A stray .env would push a live API key to a public repository.
if (Test-Path ".env") {
    Write-Host "    .env found - it is gitignored and will NOT be pushed." -ForegroundColor Yellow
}

if (-not (Test-Path ".git")) {
    git init --quiet
    git add .
    git commit --quiet -m "CodeLens: repository intelligence and AI code review"
    git branch -M main
    Ok "repository initialised"
} else {
    Ok "existing git repository reused"
}

# Refuse to continue if a key somehow got staged.
$staged = git ls-files | Select-String -Pattern "^\.env$"
if ($staged) { Fail ".env is tracked by git. Run: git rm --cached .env" }

gh repo create $repoName --public --source=. --push
if ($LASTEXITCODE -ne 0) { Fail "Repository creation failed. A repo with that name may already exist." }

$owner = (gh api user --jq .login)
$slug  = "$owner/$repoName"
Ok "pushed to https://github.com/$slug"

# ------------------------------------------------------------ configure -----
Step 5 "Granting the workflow permission to comment"
gh api -X PUT "/repos/$slug/actions/permissions/workflow" -f default_workflow_permissions=write | Out-Null
Ok "workflow permissions set to read and write"

Step 6 "Adding your Groq API key as a repository secret"
Write-Host "    Get a free key at https://console.groq.com  (API Keys -> Create)"
Write-Host "    Paste it below, or press Enter to skip and use static analysis only."
$key = Read-Host "    Groq API key" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($key))

if ([string]::IsNullOrWhiteSpace($plain)) {
    Write-Host "    Skipped. Reviews will run static analysis only." -ForegroundColor Yellow
} else {
    $plain | gh secret set CODELENS_LLM_API_KEY --repo $slug
    if ($LASTEXITCODE -ne 0) { Fail "Could not set the secret." }
    Ok "secret CODELENS_LLM_API_KEY stored"
}
$plain = $null

# ----------------------------------------------------------- test the PR ----
Step 7 "Opening a pull request with deliberately flawed code"

git checkout -q -b test-review
New-Item -ItemType Directory -Force -Path "src" | Out-Null
$scratch = @'
import subprocess

API_KEY = "sk-abcdefghijklmnopqrstuvwxyz123456"


def run_it(command, seen=[]):
    seen.append(command)
    try:
        return subprocess.run(command, shell=True)
    except:
        pass
'@
# NOT Set-Content -Encoding UTF8: on Windows PowerShell 5.1 that writes a UTF-8
# BOM, and a leading U+FEFF makes Python refuse to parse the file. Write the
# bytes explicitly with a BOM-less encoder.
[System.IO.File]::WriteAllText(
    (Join-Path (Get-Location) "src\scratch.py"),
    $scratch,
    (New-Object System.Text.UTF8Encoding($false)))

git add src\scratch.py
git commit --quiet -m "Add scratch helper"
git push --quiet -u origin test-review

gh pr create --title "Test: CodeLens review" --body "Deliberately flawed code to exercise the review workflow." | Out-Null
Ok "pull request opened"

# ------------------------------------------------------------------ done ----
Write-Host "`nDone.`n" -ForegroundColor Green
Write-Host "  Repository:   https://github.com/$slug"
Write-Host "  Actions:      https://github.com/$slug/actions"
Write-Host "  Pull request: https://github.com/$slug/pulls"
Write-Host "`nThe review takes about 90 seconds. Then open the PR's Conversation tab."
Write-Host "Expect a comment listing a critical finding for the committed API key,"
Write-Host "plus shell=True, a mutable default argument, and a bare except."
Write-Host "`nThe workflow will fail on purpose - that is --fail-on critical working."
Write-Host "Screenshot that comment and put it at the top of your README.`n"
