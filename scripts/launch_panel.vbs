Option Explicit
' Launch the Helpdesk control panel without any console window.
' WScript.Shell.Run with window style 0 starts PowerShell with no visible
' console/ConPTY host, then the WinForms window appears on its own.
Dim fso, shell, target, command
If WScript.Arguments.Count = 0 Then
    WScript.Quit 1
End If
Set fso = CreateObject("Scripting.FileSystemObject")
target = WScript.Arguments(0)
If Not fso.FileExists(target) Then
    MsgBox "Control panel script not found:" & vbCrLf & target, vbCritical, "IT Helpdesk"
    WScript.Quit 1
End If
Set shell = CreateObject("WScript.Shell")
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & target & """"
shell.Run command, 0, False
