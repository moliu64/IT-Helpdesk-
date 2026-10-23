param(
    [switch]$NoBrowser,
    [switch]$SkipInstall
)

$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
Set-Location $root

# ---------------------------------------------------------------------------
# 这是一个 GUI 面板：启动时隐藏宿主控制台窗口，避免双击 .bat 后残留一个
# 一直闪烁的终端窗口。没有控制台（例如由 wscript 启动）时直接跳过。
# ---------------------------------------------------------------------------
try {
    Add-Type -Name Win32Console -Namespace Helpdesk -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr FindWindow(string lpClassName, string lpWindowName);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
'@ -ErrorAction Stop
    $consoleHandle = [Helpdesk.Win32Console]::GetConsoleWindow()
    if ($consoleHandle -ne [IntPtr]::Zero) {
        [void][Helpdesk.Win32Console]::ShowWindow($consoleHandle, 0)  # SW_HIDE
    }
} catch {
    # 无控制台句柄时无需处理。
}

# ---------------------------------------------------------------------------
# 单实例保护：重复双击 .bat 时不再叠加多个面板窗口，而是把已有面板恢复到前台。
# ---------------------------------------------------------------------------
$script:instanceMutex = $null
$createdNew = $true
try {
    $script:instanceMutex = New-Object System.Threading.Mutex($true, "Local\HelpdeskControlPanelInstance", [ref]$createdNew)
} catch {
    $createdNew = $true
}
if (-not $createdNew) {
    try {
        $existingWindow = [Helpdesk.Win32Console]::FindWindow($null, "IT 运维工单智能体 - 控制面板")
        if ($existingWindow -ne [IntPtr]::Zero) {
            [void][Helpdesk.Win32Console]::ShowWindow($existingWindow, 9)  # SW_RESTORE
            [void][Helpdesk.Win32Console]::SetForegroundWindow($existingWindow)
        }
    } catch { }
    exit 0
}

try {
    $utf8 = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    [Console]::InputEncoding = $utf8
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
} catch {
    # 控制台不可用时（隐藏/无窗口启动）跳过编码设置，GUI 不依赖它。
}

function Import-DeploymentEnv {
    # The panel must agree with start_online.ps1 about port and public URL,
    # otherwise it polls a port the launcher never opens and always reports
    # "启动失败" even when the service is running.
    $envFile = Join-Path $root ".env"
    if (-not (Test-Path -LiteralPath $envFile)) { return }
    $allowed = @("HELPDESK_ONLINE_HOST", "HELPDESK_ONLINE_PORT", "HELPDESK_PUBLIC_URL", "HELPDESK_TUNNEL_CONFIG", "CLOUDFLARED_PATH")
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        if ($line -match '^\s*#' -or $line -notmatch '^\s*([^=\s]+)\s*=\s*(.*)\s*$') { continue }
        $name = $Matches[1]
        if ($allowed -notcontains $name -or (Test-Path "Env:$name")) { continue }
        [Environment]::SetEnvironmentVariable($name, $Matches[2].Trim().Trim('"').Trim("'"), "Process")
    }
}

Import-DeploymentEnv

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
# 捕获 UI 线程异常并显示在面板里，避免弹出不可关闭的未处理异常对话框。
[System.Windows.Forms.Application]::SetUnhandledExceptionMode([System.Windows.Forms.UnhandledExceptionMode]::CatchException)

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
$script:stopping = $false
$script:stopProcess = $null
$script:stopDeadline = $null
$script:failureShown = $false

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$form = New-Object System.Windows.Forms.Form
$form.Text = "IT 运维工单智能体 - 控制面板"
$form.StartPosition = "CenterScreen"
$form.Size = New-Object -TypeName System.Drawing.Size -ArgumentList @(560, 420)
$form.MinimumSize = New-Object -TypeName System.Drawing.Size -ArgumentList @(560, 420)
$form.MaximizeBox = $false
$form.FormBorderStyle = "FixedSingle"

$title = New-Object System.Windows.Forms.Label
$title.Text = "IT 运维工单智能体"
$title.Font = $form.Font
$title.AutoSize = $true
$title.Location = New-Object -TypeName System.Drawing.Point -ArgumentList @(28, 24)
$form.Controls.Add($title)

$description = New-Object System.Windows.Forms.Label
$description.Text = '一键启动源站与 Cloudflare Tunnel。启动后可关闭此面板，项目继续在后台运行；再次打开面板可查看状态，只有点击“停止项目”才会停止服务。'
$description.AutoSize = $false
$description.Size = New-Object -TypeName System.Drawing.Size -ArgumentList @(490, 50)
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

$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Multiline = $true
$logBox.ReadOnly = $true
$logBox.ScrollBars = "Vertical"
$logBox.Size = New-Object -TypeName System.Drawing.Size -ArgumentList @(490, 96)
$logBox.Location = New-Object -TypeName System.Drawing.Point -ArgumentList @(30, 212)
$logBox.BackColor = [System.Drawing.Color]::FromArgb(246, 246, 246)
$logBox.ForeColor = [System.Drawing.Color]::DimGray
$logBox.Font = New-Object System.Drawing.Font("Consolas", 9)
$logBox.Text = "点击「一键启动项目」后，这里会显示启动进度与失败原因。"
$form.Controls.Add($logBox)

$urlLabel = New-Object System.Windows.Forms.Label
$urlLabel.Text = "线上地址：$publicUrl`n日志目录：$logDir"
$urlLabel.AutoSize = $false
$urlLabel.Size = New-Object -TypeName System.Drawing.Size -ArgumentList @(490, 42)
$urlLabel.Location = New-Object -TypeName System.Drawing.Point -ArgumentList @(30, 318)
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

function Get-LauncherError {
    # Surface the real cause instead of a generic "请查看日志": the launcher
    # writes stdout to control-panel.log and the thrown error to .err.
    $lines = @()
    foreach ($file in @($panelErrorLog, $panelLog)) {
        if (-not (Test-Path -LiteralPath $file)) { continue }
        $tail = Get-Content -LiteralPath $file -Encoding UTF8 -ErrorAction SilentlyContinue |
            Where-Object { $_.Trim() } | Select-Object -Last 10
        if ($tail) { $lines += $tail }
    }
    return (($lines -join [Environment]::NewLine).Trim())
}

function Show-LauncherFailure {
    if ($script:failureShown) { return }
    $script:failureShown = $true
    $reason = Get-LauncherError
    if (-not $reason) { $reason = "启动脚本已退出，但没有留下可读日志。" }
    $logBox.Text = $reason
    Set-PanelStatus "启动失败，原因见下方" ([System.Drawing.Color]::Firebrick)
    [void][System.Windows.Forms.MessageBox]::Show(
        $form,
        ("启动失败：`r`n`r`n" + $reason + "`r`n`r`n日志目录：" + $logDir),
        "IT 运维工单智能体",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error)
}

function Start-HelpdeskProject {
    try {
        if (Test-HelpdeskHealth) {
            Set-PanelStatus "运行中" ([System.Drawing.Color]::FromArgb(32, 128, 76))
            $logBox.Text = "服务正在运行：$publicUrl/"
            $startButton.Enabled = $false
            $stopButton.Enabled = $true
            return
        }
        if ($script:starting -or $script:stopping) { return }
        $script:starting = $true
        $script:failureShown = $false
        $startButton.Enabled = $false
        $stopButton.Enabled = $false
        $logBox.Text = "正在启动源站与 Cloudflare Tunnel（首次运行可能需要 1-2 分钟）..."
        Set-PanelStatus "正在启动，请稍候..." ([System.Drawing.Color]::DarkOrange)
        $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $startScript + '"'), "-NoBrowser")
        if ($SkipInstall) { $arguments += "-SkipInstall" }
        $script:launcherProcess = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList $arguments -WorkingDirectory $root -RedirectStandardOutput $panelLog -RedirectStandardError $panelErrorLog -PassThru
    } catch {
        $script:starting = $false
        $script:launcherProcess = $null
        Set-PanelStatus "启动失败" ([System.Drawing.Color]::Firebrick)
        $logBox.Text = "启动失败：$($_.Exception.Message)"
        $startButton.Enabled = $true
        $stopButton.Enabled = $false
    }
}

function Stop-HelpdeskProject {
    # 停止脚本放在后台执行，面板保持响应：旧实现用 -Wait 同步等待，
    # 一旦 stop.ps1 卡在 WMI/网络查询上，整个窗口会冻结、无法关闭。
    if ($script:stopping) { return }
    $script:stopping = $true
    $script:stopProcess = $null
    $script:stopDeadline = (Get-Date).AddSeconds(60)
    $startButton.Enabled = $false
    $stopButton.Enabled = $false
    Set-PanelStatus "正在停止..." ([System.Drawing.Color]::DarkOrange)
    $logBox.Text = "正在停止服务与 Cloudflare Tunnel..."
    try {
        $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ('"' + $stopScript + '"'))
        $script:stopProcess = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList $arguments -WorkingDirectory $root -PassThru
    } catch {
        $script:stopping = $false
        $script:stopProcess = $null
        $script:stopDeadline = $null
        Set-PanelStatus "停止失败" ([System.Drawing.Color]::Firebrick)
        $logBox.Text = "停止失败：$($_.Exception.Message)"
        $startButton.Enabled = $true
        $stopButton.Enabled = $false
    }
}

function Complete-StopIfFinished {
    $stopProcess = $script:stopProcess
    $exited = (-not $stopProcess) -or $stopProcess.HasExited
    if (-not $exited -and $script:stopDeadline -and (Get-Date) -gt $script:stopDeadline) {
        try { $stopProcess.Kill() } catch { }
        $exited = $true
        $logBox.Text = "停止操作超时，已结束停止进程；请稍后重试，或在任务管理器中确认 python/cloudflared 已退出。"
    }
    if (-not $exited) {
        Set-PanelStatus "正在停止..." ([System.Drawing.Color]::DarkOrange)
        return
    }
    $script:stopping = $false
    $script:stopProcess = $null
    $script:stopDeadline = $null
    Set-PanelStatus "已停止" ([System.Drawing.Color]::DimGray)
    if ($logBox.Text -notlike "停止操作超时*") {
        $logBox.Text = "服务已停止。可以点击「一键启动项目」重新启动。"
    }
    $startButton.Enabled = $true
    $stopButton.Enabled = $false
}

$startButton.Add_Click({ Start-HelpdeskProject })
$stopButton.Add_Click({ Stop-HelpdeskProject })
$openButton.Add_Click({
    try {
        if (-not (Test-HelpdeskHealth)) {
            [void][System.Windows.Forms.MessageBox]::Show(
                $form,
                "项目当前未运行，请先点击「一键启动项目」。",
                "IT 运维工单智能体",
                [System.Windows.Forms.MessageBoxButtons]::OK,
                [System.Windows.Forms.MessageBoxIcon]::Warning)
            return
        }
        Start-Process $publicUrl
    } catch {
        $logBox.Text = "打开网站失败：$($_.Exception.Message)"
    }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    try {
        if ($script:stopping) {
            Complete-StopIfFinished
            return
        }
        if (Test-HelpdeskHealth) {
            $script:starting = $false
            Set-PanelStatus "运行中" ([System.Drawing.Color]::FromArgb(32, 128, 76))
            $logBox.Text = "服务已上线：$publicUrl/`r`n后台管理：$publicUrl/backend"
            $startButton.Enabled = $false
            $stopButton.Enabled = $true
            if (-not $NoBrowser -and -not $script:browserOpened) {
                Start-Process $publicUrl
                $script:browserOpened = $true
            }
        } elseif ($script:starting) {
            if ($script:launcherProcess -and $script:launcherProcess.HasExited) {
                $script:starting = $false
                $script:launcherProcess = $null
                $startButton.Enabled = $true
                $stopButton.Enabled = $false
                Show-LauncherFailure
            } else {
                Set-PanelStatus "正在启动，请稍候..." ([System.Drawing.Color]::DarkOrange)
            }
        } else {
            Set-PanelStatus "已停止" ([System.Drawing.Color]::DimGray)
            $startButton.Enabled = $true
            $stopButton.Enabled = $false
        }
    } catch {
        # 定时器里的异常绝不能冒泡成阻塞窗口的未处理异常对话框。
        try { Set-PanelStatus "面板异常：$($_.Exception.Message)" ([System.Drawing.Color]::Firebrick) } catch { }
    }
})
$timer.Start()

$form.Add_Shown({ Start-HelpdeskProject })

# 关闭面板永远不阻塞（停止脚本在后台继续执行），也永远不停止项目。
$form.Add_FormClosing({ })
$form.Add_FormClosed({
    try {
        $timer.Stop()
        $timer.Dispose()
    } catch { }
    try {
        if ($script:instanceMutex) {
            $script:instanceMutex.ReleaseMutex()
            $script:instanceMutex.Dispose()
        }
    } catch { }
})

[System.Windows.Forms.Application]::add_ThreadException({
    param($sender, $eventArgs)
    try {
        $message = if ($eventArgs -and $eventArgs.Exception) { $eventArgs.Exception.Message } else { "未知错误" }
        Set-PanelStatus "面板异常：$message" ([System.Drawing.Color]::Firebrick)
        if ($logBox) { $logBox.Text = "面板发生异常：$message" }
    } catch { }
})

[System.Windows.Forms.Application]::Run($form)
