# board-toast.ps1 -- toast Windows pour "la board de board".
#
# Aucune dependance : le XML ToastNotification est compose a la main et publie
# sous l'AppId de WindowsPowerShell, qui est toujours enregistre.
#
# Ce fichier est volontairement en pur ASCII : PowerShell 5.1 lit un .ps1 sans
# BOM comme de l'ANSI, donc aucun litteral accentue ne doit y figurer. Tout le
# texte affiche (emoji, accents, guillemets francais) arrive par -Title/-Body.
#
# Sortie silencieuse, code retour 0 en toute circonstance : le board ne doit
# jamais tomber a cause d'un toast.

param(
    [string]$Title = '',
    [string]$Body = '',
    [string]$Tag = 'board',
    [string]$Group = 'board',
    [string]$Scenario = 'default',
    [string]$Audio = '',
    [switch]$Silent,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$AppId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function Get-Trimmed {
    param([string]$Value, [int]$Max)
    if ([string]::IsNullOrEmpty($Value)) { return '' }
    if ($Value.Length -gt $Max) { return $Value.Substring(0, $Max) }
    return $Value
}

function Escape-Xml {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return '' }
    # & < > " ' -- l'ordre importe, & en premier.
    $out = $Value.Replace('&', '&amp;')
    $out = $out.Replace('<', '&lt;')
    $out = $out.Replace('>', '&gt;')
    $out = $out.Replace('"', '&quot;')
    $out = $out.Replace("'", '&apos;')
    # caracteres de controle interdits en XML 1.0
    $out = [regex]::Replace($out, '[\x00-\x08\x0B\x0C\x0E-\x1F]', '')
    return $out
}

function Build-Xml {
    param([string]$ScenarioName)

    $sb = New-Object System.Text.StringBuilder
    if ([string]::IsNullOrEmpty($ScenarioName) -or $ScenarioName -eq 'default') {
        [void]$sb.Append('<toast')
    }
    else {
        [void]$sb.Append('<toast scenario="' + $ScenarioName + '"')
    }
    if ($script:Looping) { [void]$sb.Append(' duration="long"') }
    [void]$sb.Append('><visual><binding template="ToastGeneric">')

    [void]$sb.Append('<text>' + (Escape-Xml $Title) + '</text>')
    $lines = @()
    if (-not [string]::IsNullOrEmpty($Body)) {
        $lines = $Body -split "`r`n|`n|`r"
    }
    $count = 0
    foreach ($line in $lines) {
        if ($count -ge 3) { break }
        [void]$sb.Append('<text>' + (Escape-Xml $line) + '</text>')
        $count = $count + 1
    }
    [void]$sb.Append('</binding></visual>')

    if ($script:Quiet) {
        [void]$sb.Append('<audio silent="true"/>')
    }
    else {
        $loop = 'false'
        if ($script:Looping) { $loop = 'true' }
        [void]$sb.Append('<audio src="' + (Escape-Xml $Audio) + '" loop="' + $loop + '"/>')
    }

    if ($ScenarioName -eq 'reminder' -or $ScenarioName -eq 'urgent') {
        [void]$sb.Append('<actions><action content="OK" arguments="dismiss" activationType="system"/></actions>')
    }

    [void]$sb.Append('</toast>')
    return $sb.ToString()
}

try {
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

    $Tag = Get-Trimmed -Value $Tag -Max 60
    $Group = Get-Trimmed -Value $Group -Max 60
    if ([string]::IsNullOrEmpty($Tag)) { $Tag = 'board' }
    if ([string]::IsNullOrEmpty($Group)) { $Group = 'board' }

    if ($Remove) {
        try {
            [Windows.UI.Notifications.ToastNotificationManager]::History.Remove($Tag, $Group, $AppId)
        }
        catch { }
        exit 0
    }

    # Un toast est SONORE par defaut : la balise <audio silent="true"/> doit
    # etre presente explicitement des qu'on veut le silence.
    $script:Quiet = ($Silent.IsPresent) -or ([string]::IsNullOrEmpty($Audio))
    $script:Looping = (-not $script:Quiet) -and ($Audio -like '*Looping*')

    $wanted = $Scenario
    if ($wanted -ne 'reminder' -and $wanted -ne 'urgent') { $wanted = 'default' }

    $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId)
    $shown = $false
    # 'urgent' n'existe pas partout : on retombe sur 'reminder' puis 'default'.
    $chain = @($wanted)
    if ($wanted -eq 'urgent') { $chain = @('urgent', 'reminder', 'default') }
    elseif ($wanted -eq 'reminder') { $chain = @('reminder', 'default') }

    foreach ($candidate in $chain) {
        if ($shown) { break }
        try {
            $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
            $doc.LoadXml((Build-Xml -ScenarioName $candidate))
            $toast = New-Object Windows.UI.Notifications.ToastNotification -ArgumentList $doc
            $toast.Tag = $Tag
            $toast.Group = $Group
            $notifier.Show($toast)
            $shown = $true
        }
        catch { }
    }
}
catch { }

exit 0
