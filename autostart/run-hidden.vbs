' VieNeu Studio: start TTS engine (8000) + gateway (8001) hidden, no console window.
' Started at logon by the Startup shortcut created by install-autostart.bat.
' Pass any argument (e.g. now) to skip the 15 s logon delay.
Option Explicit
Dim fso, sh, root
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
If WScript.Arguments.Count = 0 Then WScript.Sleep 15000
sh.CurrentDirectory = root
If Not fso.FolderExists(root & "\logs") Then fso.CreateFolder(root & "\logs")
sh.Run "cmd /c start-all.bat >> logs\autostart.log 2>&1", 0, False
