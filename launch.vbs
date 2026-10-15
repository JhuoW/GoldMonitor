Option Explicit
Dim shell, files, root, executable, source
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
executable = files.BuildPath(root, "dist\GoldMonitor.exe")
source = files.BuildPath(root, "gold_monitor.pyw")
shell.CurrentDirectory = root

On Error Resume Next
If files.FileExists(executable) Then
    shell.Run Chr(34) & executable & Chr(34), 1, False
ElseIf files.FileExists(source) Then
    shell.Run "pythonw.exe " & Chr(34) & source & Chr(34), 0, False
Else
    MsgBox "Gold Monitor was not found. Restore the application files and try again.", 48, "Gold Monitor"
    WScript.Quit 1
End If
If Err.Number <> 0 Then
    MsgBox "Unable to start Gold Monitor. Build dist\GoldMonitor.exe with build.ps1, or install Python with tkinter.", 48, "Gold Monitor"
    WScript.Quit 1
End If
