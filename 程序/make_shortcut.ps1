# 一键重建「启动机器人」桌面快捷方式
# 用法：双击同目录下的「安装快捷方式.bat」即可，无需手动改任何路径。
# 原理：旧快捷方式里记录的是创建它的那台机器上的绝对路径（C:\Users\Administrator\...），
#       文件夹一换位置图标路径就失效了。这个脚本用当前文件夹的真实位置重建快捷方式，
#       图标指向本目录的 launcher.ico（圆角矩形版），放哪都能用。

$ErrorActionPreference = 'Stop'

# 本脚本所在目录（也就是项目根目录）
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

# 桌面路径
$desktop = [Environment]::GetFolderPath('Desktop')

# 校验图标文件存在
$icon = Join-Path $dir 'launcher.ico'
if (-not (Test-Path $icon)) {
    Write-Host "[错误] 找不到 $icon" -ForegroundColor Red
    Write-Host '请确认本脚本和 launcher.ico 在同一个文件夹里。'
    exit 1
}

# 重建快捷方式（覆盖旧的）
$ws = New-Object -ComObject WScript.Shell
$lnkPath = Join-Path $desktop '启动机器人.lnk'
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath      = Join-Path $dir '启动.bat'
$sc.WorkingDirectory = $dir
$sc.IconLocation    = "$icon,0"
$sc.Description     = 'QQ AI Bot 启动器'
$sc.WindowStyle     = 1
$sc.Save()

Write-Host ''
Write-Host "[完成] 桌面快捷方式已创建/更新：" -ForegroundColor Green
Write-Host "  $lnkPath"
Write-Host '  图标：本目录 launcher.ico（圆角矩形版，含 256/48/32/16 四档尺寸）'
Write-Host ''
Write-Host '如果桌面图标看起来还是旧的方块：'
Write-Host '  那是 Windows 的图标缓存，按 F5 刷新桌面；'
Write-Host '  还不行就在任务管理器重启「Windows 资源管理器」。'
Write-Host ''
pause
