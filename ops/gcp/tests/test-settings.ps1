$ErrorActionPreference = "Stop"
. "$PSScriptRoot\..\settings-lib.ps1"

function Assert-Equal($Expected, $Actual, $Label) {
    if ($Expected -ne $Actual) { throw "$Label failed" }
}

$current = @'
MIA_ENV=prod
MIA_PUBLIC_BASE_URL=https://mia.assafweb.com
MIA_TELEGRAM_BOT_TOKEN=preserve-token
MIA_OPENAI_API_KEY=preserve-openai
MIA_LOG_LEVEL=INFO
'@
$merged = @(Merge-MiaSettings $current @{
    MIA_ASSAFWEB_FORM_INTAKE_SECRET = "new-intake-secret"
    MIA_LOG_LEVEL = "WARNING"
})
Assert-Equal 6 $merged.Count "preserve count plus one"
Assert-Equal $true ($merged -contains "MIA_OPENAI_API_KEY=preserve-openai") "preserve unrelated key"
Assert-Equal $true ($merged -contains "MIA_TELEGRAM_BOT_TOKEN=preserve-token") "preserve Telegram"
Assert-Equal $true ($merged -contains "MIA_LOG_LEVEL=WARNING") "replace requested key"
Assert-Equal $true ($merged -contains "MIA_ASSAFWEB_FORM_INTAKE_SECRET=new-intake-secret") "add key"

$removed = @(Merge-MiaSettings ($merged -join "`n") @{ MIA_LOG_LEVEL = $null })
Assert-Equal 0 @($removed -match '^MIA_LOG_LEVEL=').Count "remove explicit key"

foreach ($separator in @([char]0, [char]0x0A, [char]0x0B, [char]0x0C, [char]0x0D, [char]0x1C, [char]0x1D, [char]0x1E, [char]0x85, [char]0x2028, [char]0x2029)) {
    try {
        Merge-MiaSettings $current @{ SAFE = "BAD${separator}INJECT=value" } | Out-Null
        throw "unsafe value was accepted"
    } catch {
        if ($_.Exception.Message -eq "unsafe value was accepted") { throw }
    }
}
foreach ($badName in @("BAD-NAME", "X`nINJECT", "SAFE`r", "SAFE$([char]0x85)")) {
    try {
        $patch = @{}; $patch[$badName] = "value"
        Merge-MiaSettings $current $patch | Out-Null
        throw "unsafe name was accepted"
    } catch {
        if ($_.Exception.Message -eq "unsafe name was accepted") { throw }
    }
}
try {
    Merge-MiaSettings $current @{ MIA_DATABASE_URL = "forbidden" } | Out-Null
    throw "VM-owned setting was accepted"
} catch {
    if ($_.Exception.Message -eq "VM-owned setting was accepted") { throw }
}

# Exercise the complete patch path without a cloud process. The fake gcloud keeps
# payloads in memory, and fake aws fails if the patch path ever consults it.
$global:MiaTestCurrent = $current
$global:MiaTestUploaded = ""
$global:MiaTestAccessFails = $false
$global:MiaTestAwsCalls = 0
function global:gcloud {
    $command = (@($args | ForEach-Object { [string]$_ }) -join " ")
    if ($command.StartsWith("secrets versions access latest")) {
        if ($global:MiaTestAccessFails) { $global:LASTEXITCODE = 7; return }
        $global:LASTEXITCODE = 0
        Write-Output $global:MiaTestCurrent
        return
    }
    if ($command.StartsWith("secrets versions add mia-env")) {
        $global:MiaTestUploaded = @($input) -join "`n"
        $global:LASTEXITCODE = 0
        return
    }
    $global:LASTEXITCODE = 2
}
function global:aws {
    $global:MiaTestAwsCalls += 1
    throw "AWS must not be called"
}

try {
    $output = (& "$PSScriptRoot\..\push-settings.ps1" -ProjectId "test-project" -PatchSecret `
        -Set @{ MIA_ASSAFWEB_FORM_INTAKE_SECRET = "new-intake-secret" } 2>&1 3>&1 4>&1 5>&1 6>&1 | Out-String)
    if ($global:MiaTestUploaded -notmatch '(?m)^MIA_OPENAI_API_KEY=preserve-openai\s*$') { throw "patch lost unrelated key" }
    if ($global:MiaTestUploaded -notmatch '(?m)^MIA_TELEGRAM_BOT_TOKEN=preserve-token\s*$') { throw "patch lost Telegram key" }
    if ($global:MiaTestUploaded -notmatch '(?m)^MIA_ASSAFWEB_FORM_INTAKE_SECRET=new-intake-secret\s*$') { throw "patch did not add requested key" }
    Assert-Equal 0 $global:MiaTestAwsCalls "AWS call count"
    foreach ($secretValue in @("preserve-token", "preserve-openai", "new-intake-secret")) {
        if ($output.Contains($secretValue)) { throw "secret payload leaked to output" }
    }

    $global:MiaTestAccessFails = $true
    $global:MiaTestUploaded = ""
    try {
        & "$PSScriptRoot\..\push-settings.ps1" -ProjectId "test-project" -PatchSecret `
            -Set @{ MIA_LOG_LEVEL = "DEBUG" } *> $null
        throw "unreadable secret was accepted"
    } catch {
        if ($_.Exception.Message -eq "unreadable secret was accepted") { throw }
        if ($_.Exception.Message -notmatch '^cannot read the current mia-env secret') { throw }
    }
    Assert-Equal "" $global:MiaTestUploaded "no upload after access failure"
    Assert-Equal 0 $global:MiaTestAwsCalls "AWS call count after failure"

    $global:MiaTestAccessFails = $false
    $global:MiaTestCurrent = ""
    try {
        & "$PSScriptRoot\..\push-settings.ps1" -ProjectId "test-project" -PatchSecret `
            -Set @{
                MIA_ENV = "prod"
                MIA_PUBLIC_BASE_URL = "https://mia.assafweb.com"
                MIA_TELEGRAM_BOT_TOKEN = "replacement-token"
            } *> $null
        throw "missing secret payload was accepted"
    } catch {
        if ($_.Exception.Message -eq "missing secret payload was accepted") { throw }
        if ($_.Exception.Message -notmatch '^current mia-env secret is missing MIA_ENV') { throw }
    }
    Assert-Equal "" $global:MiaTestUploaded "no upload after missing payload"
    Assert-Equal 0 $global:MiaTestAwsCalls "AWS call count after missing payload"

    $global:MiaTestCurrent = @'
MIA_ENV=prod
MIA_PUBLIC_BASE_URL=https://mia.assafweb.com
'@
    try {
        & "$PSScriptRoot\..\push-settings.ps1" -ProjectId "test-project" -PatchSecret `
            -Set @{ MIA_TELEGRAM_BOT_TOKEN = "replacement-token" } *> $null
        throw "required-key-deficient baseline was accepted"
    } catch {
        if ($_.Exception.Message -eq "required-key-deficient baseline was accepted") { throw }
        if ($_.Exception.Message -notmatch '^current mia-env secret is missing MIA_TELEGRAM_BOT_TOKEN') { throw }
    }
    Assert-Equal "" $global:MiaTestUploaded "no upload after required-key-deficient baseline"

    $global:MiaTestCurrent = "not-an-assignment"
    try {
        & "$PSScriptRoot\..\push-settings.ps1" -ProjectId "test-project" -PatchSecret `
            -Set @{
                MIA_ENV = "prod"
                MIA_PUBLIC_BASE_URL = "https://mia.assafweb.com"
                MIA_TELEGRAM_BOT_TOKEN = "replacement-token"
            } *> $null
        throw "malformed baseline was accepted"
    } catch {
        if ($_.Exception.Message -eq "malformed baseline was accepted") { throw }
        if ($_.Exception.Message -notmatch '^stored settings contain an invalid line') { throw }
    }
    Assert-Equal "" $global:MiaTestUploaded "no upload after malformed baseline"
    Assert-Equal 0 $global:MiaTestAwsCalls "AWS call count after invalid baselines"
} finally {
    Remove-Item function:\gcloud -ErrorAction SilentlyContinue
    Remove-Item function:\aws -ErrorAction SilentlyContinue
    Remove-Variable MiaTestCurrent, MiaTestUploaded, MiaTestAccessFails, MiaTestAwsCalls `
        -Scope Global -ErrorAction SilentlyContinue
}

Write-Host "Mia settings merge checks passed"
