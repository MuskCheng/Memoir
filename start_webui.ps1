# Memoir Web UI Launcher v2.1.0
# 右键 -> 使用 PowerShell 运行
# 自动清理旧进程，启动后打开浏览器

$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RootDir

Write-Host "Memoir Web UI - 启动中..." -ForegroundColor Cyan
Write-Host ""

# ---- 1. 查找可用的 Python ----
$python = $null
$candidates = @(
    "C:\Program Files\Python312\python.exe",
    "C:\Program Files\Python313\python.exe",
    "python",
    "python3"
)

foreach ($exe in $candidates) {
    $path = (Get-Command $exe -ErrorAction SilentlyContinue).Source
    if ($path -and (Test-Path $path)) {
        try {
            $ver = & $path --version 2>&1
            & $path -c "import flask, apscheduler, requests, PIL" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $python = $path
                Write-Host "[OK] $ver" -ForegroundColor Green
                break
            }
        } catch {}
    }
}

if (-not $python) {
    Write-Host "[ERROR] 未找到 Python 或缺少依赖" -ForegroundColor Red
    Write-Host "请运行: pip install -r $RootDir\requirements.txt" -ForegroundColor Yellow
    Read-Host "按回车退出"
    exit 1
}

# ---- 2. 清理旧 WebUI 进程 ----
Write-Host "[..] 清理旧进程..." -ForegroundColor Yellow
try {
    $oldPids = Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" } |
        Select-Object -ExpandProperty OwningProcess
    foreach ($pid in $oldPids) {
        Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
        Write-Host "  -> 已停止旧进程 PID=$pid" -ForegroundColor Gray
    }
} catch {}
Start-Sleep -Seconds 2

# ---- 3. 启动 WebUI ----
Write-Host "[OK] 启动 WebUI..." -ForegroundColor Green
Write-Host "    地址: http://localhost:5000" -ForegroundColor Cyan
Write-Host ""
Start-Process -FilePath $python -ArgumentList "$RootDir\webui\app.py" -WindowStyle Normal
Start-Sleep -Seconds 3

# ---- 4. 打开浏览器 ----
try {
    Start-Process "http://localhost:5000"
    Write-Host "[OK] 浏览器已打开" -ForegroundColor Green
} catch {
    Write-Host "[..] 请手动打开: http://localhost:5000" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "提示: 关闭此窗口不会停止 WebUI" -ForegroundColor Cyan
Write-Host "      如需停止: taskkill /f /im python.exe" -ForegroundColor Gray