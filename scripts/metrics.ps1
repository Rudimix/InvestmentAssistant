<#
.SYNOPSIS
  Снимает метрики кода готовыми инструментами и сохраняет их вывод в каталог отчёта.

.EXAMPLE
  .\scripts\metrics.ps1 -Out metrics\after
  # замер первой версии: git worktree add ..\ia-before f91db41
  .\scripts\metrics.ps1 -Src ..\ia-before\src\investment_journal -Out metrics\before -SkipCoverage
#>
param(
    [string]$Src = "src\investment_journal",
    [Parameter(Mandatory = $true)][string]$Out,
    [string]$Python = ".venv\Scripts\python.exe",
    [switch]$SkipCoverage
)

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
New-Item -ItemType Directory -Force $Out | Out-Null
$pyscn = Join-Path (Split-Path $Python) "pyscn.exe"

function Save([string]$name, [scriptblock]$cmd) {
    Write-Host "-> $name"
    & $cmd 2>&1 | ForEach-Object { "$_" } | Out-File -Encoding utf8 (Join-Path $Out $name)
}

# radon: индекс сопровождаемости, цикломатическая сложность, строки кода, метрики Холстеда
Save "radon_mi.txt"   { & $Python -m radon mi -s $Src }
Save "radon_cc.txt"   { & $Python -m radon cc -s -a -o SCORE $Src }
Save "radon_cc.json"  { & $Python -m radon cc -s -j $Src }
Save "radon_raw.txt"  { & $Python -m radon raw -s $Src }
Save "radon_hal.txt"  { & $Python -m radon hal $Src }

# lizard: NLOC, CCN, число параметров; -C 10 — предупреждения о сложности больше 10
Save "lizard.txt"          { & $Python -m lizard $Src -s cyclomatic_complexity --sort cyclomatic_complexity }
Save "lizard_warnings.txt" { & $Python -m lizard $Src -C 10 -w }

# flake8 + mccabe: правило C901 (CC > 10, порог в .flake8) — аналог CA1502
Save "flake8_c901.txt" { & $Python -m flake8 --select C901 $Src }

# pylint: глубина наследования (R0901, порог 0 — чтобы вывести значения для всех классов),
# проверки дизайна с порогами из pyproject.toml и общая оценка
Save "pylint_ancestors.txt" { & $Python -m pylint $Src --disable=all --enable=too-many-ancestors --max-parents=0 --score=n }
Save "pylint_design.txt"    { & $Python -m pylint $Src --disable=all --enable=design --score=n }
Save "pylint.txt"           { & $Python -m pylint $Src }

# pyscn: связанность классов CBO, связность LCOM4, зависимости модулей (Ca/Ce/I),
# когнитивная сложность, дубли кода, мёртвый код
& $pyscn analyze --json --output (Join-Path $Out "pyscn.json") $Src 2>&1 | Out-Null
& $pyscn analyze --html --no-open --output (Join-Path $Out "pyscn.html") $Src 2>&1 | Out-Null
Write-Host "-> pyscn.json, pyscn.html"

# coverage: покрытие тестами (операторы и ветви)
if (-not $SkipCoverage) {
    & $Python -m coverage run -m unittest discover -s tests 2>&1 | Out-Null
    Save "coverage.txt" { & $Python -m coverage report }
}
Write-Host "Готово: $Out"
