' ============================================================
' QQ AI Bot - Windows 快捷方式创建器
'
' 双击本文件（只需一次）：
'   1. 在桌面生成「QQ AI Bot」快捷方式（带 launcher.ico 图标）
'   2. 同时启动机器人（首次启动会自动建虚拟环境、装依赖，要等几分钟）
'
' 以后想启动，直接双击桌面那个「QQ AI Bot」图标就行，
' 不用再翻这个文件夹。
' ============================================================
Dim sh, here, desktop, lnk, batPath
Set sh = CreateObject("WScript.Shell")
here = sh.CurrentDirectory
desktop = sh.SpecialFolders("Desktop")

' ---- 1. 在桌面创建带图标的快捷方式 ----
Set lnk = sh.CreateShortcut(desktop & "\QQ AI Bot.lnk")
lnk.TargetPath = here & "\start.bat"
lnk.IconLocation = here & "\launcher.ico, 0"
lnk.WorkingDirectory = here
lnk.Description = "QQ AI Bot 启动器（首次启动自动装环境，稍慢）"
lnk.WindowStyle = 1
lnk.Save

' ---- 2. 顺手把启动器拉起来 ----
' 路径可能含空格（如「下载的文件」），必须给 bat 路径包上引号。
' VBScript 字符串内的引号要写成两个，所以拼一个 " 是四个：
batPath = """" & here & "\start.bat" & """"
sh.Run batPath, 1, False
