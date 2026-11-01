Option Explicit
Dim files, shell, root, runtime, gui
Set files = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = files.GetParentFolderName(WScript.ScriptFullName)
runtime = files.BuildPath(root, "runtime\pythonw.exe")
gui = files.BuildPath(root, "WinBridgeGUI.pyw")
If Not files.FileExists(runtime) Or Not files.FileExists(gui) Then
    shell.Popup "The portable runtime or GUI is missing. Extract the complete release archive.", 0, "WinLinuxBridge", 16
    WScript.Quit 1
End If
shell.Run """" & runtime & """ """ & gui & """", 0, False