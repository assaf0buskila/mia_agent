# Pure settings helpers shared by push-settings.ps1 and its offline harness.

$script:MiaVmOwnedSetting = '^(MIA_DATABASE_URL|MIA_BUILD_SHA|AWS_\w+|POSTGRES_\w+|PG\w+)$'

function Assert-MiaSettingName {
    param([Parameter(Mandatory = $true)] [string]$Name)
    if ($Name -notmatch '^[A-Za-z_][A-Za-z0-9_]*\z') {
        throw "invalid setting name"
    }
    if ($Name -match $script:MiaVmOwnedSetting) {
        throw "$Name is owned by the VM and cannot be uploaded"
    }
}

function Assert-MiaSettingValue {
    param([AllowEmptyString()] [string]$Value)
    # Python's splitlines() recognizes all of these. Reject them so a value cannot
    # become a second setting when the VM materializes settings.env.
    $lineSeparators = [char[]]@(
        [char]0, [char]0x0A, [char]0x0B, [char]0x0C, [char]0x0D,
        [char]0x1C, [char]0x1D, [char]0x1E, [char]0x85, [char]0x2028, [char]0x2029
    )
    if ($Value.IndexOfAny($lineSeparators) -ge 0) {
        throw "setting values must be single-line text"
    }
}

function Merge-MiaSettings {
    param(
        [AllowEmptyString()] [string]$Current,
        [Parameter(Mandatory = $true)] [hashtable]$Set
    )

    $replacement = @{}
    foreach ($entry in $Set.GetEnumerator()) {
        $name = [string]$entry.Key
        Assert-MiaSettingName $name
        if ($null -eq $entry.Value) {
            $replacement[$name] = $null
        } else {
            $value = [string]$entry.Value
            Assert-MiaSettingValue $value
            $replacement[$name] = $value
        }
    }

    $result = [System.Collections.Generic.List[string]]::new()
    $seen = @{}
    foreach ($raw in ($Current -split "`r?`n")) {
        if ($raw -eq '') { continue }
        $line = $raw -replace '^\s*export\s+', ''
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            throw "stored settings contain an invalid line"
        }
        $name = $Matches[1]
        $value = $Matches[2]
        Assert-MiaSettingName $name
        Assert-MiaSettingValue $value
        if ($replacement.ContainsKey($name)) {
            if ($null -ne $replacement[$name] -and -not $seen.ContainsKey($name)) {
                $result.Add("$name=$($replacement[$name])")
            }
            $seen[$name] = $true
        } else {
            $result.Add("$name=$value")
        }
    }
    foreach ($name in ($replacement.Keys | Sort-Object)) {
        if ($null -ne $replacement[$name] -and -not $seen.ContainsKey($name)) {
            $result.Add("$name=$($replacement[$name])")
        }
    }
    return $result
}
