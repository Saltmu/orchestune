<#
.SYNOPSIS
    Generates a collision-safe session directory under .orchestune/tmp/
    without requiring inline Python execution.
.PARAMETER Prefix
    The directory prefix/artifact kind (default: "task").
.PARAMETER Task
    The issue number or task slug (default: "scratch").
#>
param(
    [string]$Prefix = "task",
    [string]$Task = "scratch"
)

$ErrorActionPreference = "Stop"

$timestamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$randomHex = [guid]::NewGuid().ToString("N").Substring(0, 8)
$dir = ".orchestune/tmp/$Prefix-$Task-$timestamp-$randomHex"

if (-not (Test-Path -Path $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}

Write-Output $dir
