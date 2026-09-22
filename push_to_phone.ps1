# Automated Android APK installer script
$ErrorActionPreference = "Continue"

$adb = "C:\Users\rudra\AppData\Local\Android\Sdk\platform-tools\adb.exe"
if (-not (Test-Path $adb)) {
    $adb = "adb"
}

$apk = "app\build\outputs\apk\debug\app-debug.apk"
if (-not (Test-Path $apk)) {
    Write-Host "Error: APK not found at $apk. Please run './gradlew assembleDebug' first." -ForegroundColor Red
    exit 1
}

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  IMU Logger: Automated ADB Phone Installer" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "Checking for connected Android device..."

while ($true) {
    $devices = & $adb devices
    $lines = $devices -split "`n" | Where-Object { $_.Trim() -ne "" }
    $deviceLine = $lines | Select-Object -Skip 1 | Where-Object { $_ -match "\bdevice\b" -and $_ -notmatch "offline" }

    if ($deviceLine) {
        $serial = ($deviceLine -split "`t")[0].Trim()
        Write-Host "Found authorized device: $serial" -ForegroundColor Green
        Write-Host "Installing $apk (Size: $([math]::Round((Get-Item $apk).Length / 1MB, 1)) MB)..." -ForegroundColor Yellow
        
        $installResult = & $adb -s $serial install -r $apk
        Write-Host $installResult
        if ($installResult -match "Success") {
            Write-Host "`nInstallation successful!" -ForegroundColor Green
            Write-Host "Launching IMU Logger..." -ForegroundColor Cyan
            & $adb -s $serial shell monkey -p com.example.imulogger -c android.intent.category.LAUNCHER 1
            break
        } else {
            Write-Host "Install encountered an error. Retrying in 3 seconds..." -ForegroundColor Red
        }
    } else {
        $offlineLine = $lines | Select-Object -Skip 1 | Where-Object { $_ -match "offline" }
        if ($offlineLine) {
            Write-Host "`r[WAITING] Device detected but OFFLINE. Please UNLOCK your phone and tap 'Allow USB debugging'." -NoNewline -ForegroundColor Yellow
        } else {
            Write-Host "`r[WAITING] No device connected. Please plug in your phone via USB with USB debugging enabled." -NoNewline -ForegroundColor DarkYellow
        }
    }
    Start-Sleep -Seconds 2
}
