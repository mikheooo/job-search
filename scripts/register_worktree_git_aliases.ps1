<#
.SYNOPSIS
    Register or unregister Git aliases for job-search worktree management.

.DESCRIPTION
    Configures git aliases in local git config:
      git wt-new <branch>   -> creates worktree and automatically sets up deps/env
      git wt-setup          -> sets up deps and env in current or specified worktree
      git wt-list           -> lists all worktrees and diagnostic readiness
      git wt-rm <target>    -> safely removes worktree

.PARAMETER Unregister
    Removes the registered git aliases.
#>

[CmdletBinding()]
param(
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$repoRoot = (git rev-parse --show-toplevel 2>$null)
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($repoRoot)) {
    Write-Host "[ERROR] Current directory is not a git repository." -ForegroundColor Red
    exit 1
}

$aliases = @{
    "wt-new"   = "!powershell -NoProfile -ExecutionPolicy Bypass -File `"`$GIT_PREFIX/scripts/worktree.ps1`" new"
    "wt-setup" = "!powershell -NoProfile -ExecutionPolicy Bypass -File `"`$GIT_PREFIX/scripts/worktree.ps1`" setup"
    "wt-list"  = "!powershell -NoProfile -ExecutionPolicy Bypass -File `"`$GIT_PREFIX/scripts/worktree.ps1`" list"
    "wt-rm"    = "!powershell -NoProfile -ExecutionPolicy Bypass -File `"`$GIT_PREFIX/scripts/worktree.ps1`" remove"
}

if ($Unregister) {
    Write-Host "Unregistering Git worktree aliases..." -ForegroundColor Yellow
    foreach ($alias in $aliases.Keys) {
        git config --unset "alias.$alias" 2>$null
        Write-Host "  Removed alias: git $alias" -ForegroundColor DarkGray
    }
    Write-Host "[OK] Worktree git aliases unregistered." -ForegroundColor Green
} else {
    Write-Host "Registering Git worktree aliases in local repository..." -ForegroundColor Cyan
    foreach ($alias in $aliases.Keys) {
        $cmd = $aliases[$alias]
        git config "alias.$alias" $cmd
        Write-Host "  Registered: git $alias -> scripts/worktree.ps1 $($alias.Replace('wt-', ''))" -ForegroundColor Green
    }
    Write-Host "`n[OK] Git aliases ready! You can now run:" -ForegroundColor Green
    Write-Host "  git wt-new <branch> -b" -ForegroundColor White
    Write-Host "  git wt-setup" -ForegroundColor White
    Write-Host "  git wt-list" -ForegroundColor White
    Write-Host "  git wt-rm <branch>" -ForegroundColor White
}
