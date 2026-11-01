Option Explicit
Dim files, shell, root, supervisor
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = files.GetParentFolderName(WScript.ScriptFullName)
supervisor = files.BuildPath(root, "winbridge_supervisor.ps1")
If Not files.FileExists(supervisor) Then WScript.Quit 1
shell.Run "powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & supervisor & """", 0, False