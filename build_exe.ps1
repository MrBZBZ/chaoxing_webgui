$ErrorActionPreference = 'Stop'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

python -m py_compile .\chaoxing_autowatch.py .\chaoxing_webui.py .\chaoxing_webui_launcher.py
python -m PyInstaller --noconfirm --clean --onedir --name ChaoxingWebGUI --console .\chaoxing_webui_launcher.py

$Bundle = Join-Path $ProjectDir 'dist\ChaoxingWebGUI'
$Release = Join-Path $ProjectDir 'release\ChaoxingWebGUI'
if (Test-Path -LiteralPath $Release) {
    Remove-Item -LiteralPath $Release -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Release | Out-Null
Copy-Item -Path (Join-Path $Bundle '*') -Destination $Release -Recurse -Force
Copy-Item -LiteralPath (Join-Path $ProjectDir 'webui_defaults.json') -Destination $Release -Force
Copy-Item -LiteralPath (Join-Path $ProjectDir 'README.md') -Destination $Release -Force
Copy-Item -LiteralPath (Join-Path $ProjectDir 'release\ChaoxingWebGUI.bat') -Destination $Release -Force

$Zip = Join-Path $ProjectDir 'release\ChaoxingWebGUI.zip'
if (Test-Path -LiteralPath $Zip) {
    Remove-Item -LiteralPath $Zip -Force
}
Compress-Archive -Path (Join-Path $Release '*') -DestinationPath $Zip -Force
Write-Host "打包完成: $Release"
Write-Host "压缩包: $Zip"
