param(
    [switch]$NoBrowser,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root

$utf8 = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$port = if ($env:HELPDESK_ONLINE_PORT) { [int]$env:HELPDESK_ONLINE_PORT } else { 8787 }
$publicUrl = if ($env:HELPDESK_PUBLIC_URL) { $env:HELPDESK_PUBLIC_URL.TrimEnd('/') } else { "https://060115.top" }
$startScript = Join-Path $root "start_online.ps1"
$stopScript = Join-Path $root "stop.ps1"
$logDir = Join-Path $root "output"
$panelLog = Join-Path $logDir "control-panel.log"
$panelErrorLog = Join-Path $logDir "control-panel.err"
$script:launcherProcess = $null
$script:browserOpened = $false
$script:starting = $false

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$form = New-Object System.Windows.Forms.Form
$form.Text = "IT 运维工单智能体 - 控制面板"
$form.StartPosition = "CenterScreen"
$form.Size = New-Object -TypeName System.Drawing.Size -ArgumentList @(560, 330)
$form.MinimumSize = New-Object -TypeName System.Drawing.Size -ArgumentList @(560, 330)
$form.MaximizeBox = $false
$form.FormBorderStyle = "FixedSingle"

$title = New-Object System.Windows.Forms.Label
$title.Text = "IT 运维工单智能体"
$title.Font = $form.Font
$title.AutoSize = $true
$title.Location = New-Object -TypeName System.Drawing.Point -ArgumentList @(28, 24)
$form.Controls.Add($title)

$description = New-Object System.Windows.Forms.Label
$description.Text = '一键启动源站与 Cloudflare Tunnel。关闭此窗口不会停止项目，必须点击“停止项目”。'
$description.AutoSize = $false
$description.Size = New-Object -TypeName System.Drawing.Size -ArgumentList @(490, 42)
$description.Location = New-Object -TypeName System.Drawing.Point -ArgumentList @(30, 62)
$description.ForeColor = [System.Drawing.Color]::DimGray
$form.Controls.Add($description)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "状态：检查中..."
$statusLabel.AutoSize = $true
$statusLabel.Location = New-Object -TypeName System.Drawing.Point -ArgumentList @(30, 120)
$statusLabel.Font = $form.Font
$form.Controls.Add($statusLabel)

$startButton = New-Object System.Windows.Forms.Button
$startButton.Text = "一键启动项目"
$startButton.Size = New-Object -TypeName System.Drawing.Size -ArgumentList @(145, 42)
$startButton.Location = New-Object -TypeName System.Drawing.Point -ArgumentList @(30, 160)
$startButton.BackColor = [System.Drawing.Color]::FromArgb(32, 128, 76)
$startButton.ForeColor = [System.Drawing.Color]::White
$startButton.FlatStyle = "Flat"
$form.Controls.Add($startButton)

$stopButton = New-Object System.Windows.Forms.Button
$stopButton.Text = "停止项目"
$stopButton.Size = New-Object -TypeName System.Drawing.Size -ArgumentList @(145, 42)
$stopButton.Location = New-Object -TypeName System.Drawing.Point -ArgumentList @(190, 160)
$stopButton.BackColor = [System.Drawing.Color]::FromArgb(180, 55, 55)
$stopButton.ForeColor = [System.Drawing.Color]::White
$stopButton.FlatStyle = "Flat"
$form.Controls.Add($stopButton)

$openButton = New-Object System.Windows.Forms.Button
$openButton.Text = "打开网站"
$openButton.Size = New-Object -TypeName System.Drawing.Size -ArgumentList @(145, 42)
$openButton.Location = New-Object -TypeName System.Drawing.Point -ArgumentList @(350, 160)
$openButton.FlatStyle = "Flat"
$form.Controls.Add($openButton)

$urlLabel = New-Object System.Windows.Forms.Label
$urlLabel.Text = "线上地址：$publicUrl`n日志目录：$logDir"
$urlLabel.AutoSize = $false
$urlLabel.Size = New-Object -TypeName System.Drawing.Size -ArgumentList @(490, 42)
$urlLabel.Location = New-Object -TypeName System.Drawing.Point -ArgumentList @(30, 220)
$urlLabel.ForeColor = [System.Drawing.Color]::DimGray
$form.Controls.Add($urlLabel)

function Set-PanelStatus([string]$text, [System.Drawing.Color]$color) {
    $statusLabel.Text = "状态：$text"
    $statusLabel.ForeColor = $color
}

function Test-HelpdeskHealth {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 1
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Start-HelpdeskProject {
    if (Test-HelpdeskHealth) {
        Set-PanelStatus "运行中" ([System.Drawing.Color]::FromArgb(32, 128, 76))
        $startButton.Enabled = $false
        $stopButton.Enabled = $true
        return
    }
    if ($script:starting) { return }
    $script:starting = $true
    $startButton.Enabled = $false
    $stopButton.Enabled = $false
    Set-PanelStatus "正在启动，请稍候..." ([System.Drawing.Color]::DarkOrange)
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $startScript + '"'), "-NoBrowser")
    if ($SkipInstall) { $arguments += "-SkipInstall" }
    $script:launcherProcess = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList $arguments -WorkingDirectory $root -RedirectStandardOutput $panelLog -RedirectStandardError $panelErrorLog -PassThru
}

function Stop-HelpdeskProject {
    $startButton.Enabled = $false
    $stopButton.Enabled = $false
    Set-PanelStatus "正在停止..." ([System.Drawing.Color]::DarkOrange)
    $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $stopScript + '"'))
    $stopProcess = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList $arguments -WorkingDirectory $root -Wait -PassThru
    Set-PanelStatus "已停止" ([System.Drawing.Color]::DimGray)
    $startButton.Enabled = $true
    $stopButton.Enabled = $false
}

$startButton.Add_Click({ Start-HelpdeskProject })
$stopButton.Add_Click({ Stop-HelpdeskProject })
$openButton.Add_Click({ Start-Process $publicUrl })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    if (Test-HelpdeskHealth) {
        $script:starting = $false
        Set-PanelStatus "运行中" ([System.Drawing.Color]::FromArgb(32, 128, 76))
        $startButton.Enabled = $false
        $stopButton.Enabled = $true
        if (-not $NoBrowser -and -not $script:browserOpened) {
            Start-Process $publicUrl
            $script:browserOpened = $true
        }
    } elseif ($script:starting) {
        if ($script:launcherProcess -and $script:launcherProcess.HasExited) {
            $script:starting = $false
            Set-PanelStatus "启动失败，请查看日志" ([System.Drawing.Color]::Firebrick)
            $startButton.Enabled = $true
            $stopButton.Enabled = $false
        } else {
            Set-PanelStatus "正在启动，请稍候..." ([System.Drawing.Color]::DarkOrange)
        }
    } else {
        Set-PanelStatus "已停止" ([System.Drawing.Color]::DimGray)
        $startButton.Enabled = $true
        $stopButton.Enabled = $false
    }
})
$timer.Start()
$form.Add_Shown({ Start-HelpdeskProject })
$form.Add_FormClosed({ $timer.Stop(); $timer.Dispose() })

[System.Windows.Forms.Application]::Run($form)
