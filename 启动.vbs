Set WScript = CreateObject("WScript.Shell")
' 无窗启动 nanoMuse 主动型 AI 项目总管（铁律19：出生即隐藏，0=SW_HIDE）
' 用 python.exe 直跑，不经 cmd/bat，避免出现可见控制台窗口
WScript.CurrentDirectory = "G:\Users\Administrator\Documents\deepseek-harness\default-workspace\nanoMuse项目总管"
WScript.AppActivate ""
Dim cmd
cmd = """F:\pyenvs\douyin\Scripts\python.exe"" ""G:\Users\Administrator\Documents\deepseek-harness\default-workspace\nanoMuse项目总管\总管.py"""
' 环境变量由启动前设置；默认 NM_PORT=9196、NM_PATROL_SECONDS=60（代码内置默认）
WScript.Run cmd, 0, False
