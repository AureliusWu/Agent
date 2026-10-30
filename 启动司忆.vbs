Option Explicit

' Keep this source ASCII: Windows Script Host otherwise decodes UTF-8 as ANSI.
' Paths come from Unicode Windows APIs, so Chinese install paths remain valid.
Dim fileSystem, shell, processEnvironment, taskRoot, launcher, powershell
Dim command, exitCode, launchError, diagnosticEntry
Set fileSystem = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
taskRoot = fileSystem.GetParentFolderName(WScript.ScriptFullName)
launcher = fileSystem.BuildPath(taskRoot, "scripts\start-local-desktop.ps1")
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%") & "\System32\WindowsPowerShell\v1.0\powershell.exe"
diagnosticEntry = fileSystem.GetBaseName(WScript.ScriptFullName) & ".cmd"

If Not fileSystem.FileExists(powershell) Then
    Fail "Windows PowerShell 5.1 is missing. No application was started.", 2
End If
If Not fileSystem.FileExists(launcher) Then
    Fail "The desktop startup script is missing. Restore the complete application package.", 2
End If

' This affects this child process only, never the user's saved environment.
Set processEnvironment = shell.Environment("PROCESS")
processEnvironment("PSModulePath") = ""
shell.CurrentDirectory = taskRoot
command = Quote(powershell) & " -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File " & Quote(launcher)

' Wait for all integrity/profile checks. Do not weaken them to avoid a console.
On Error Resume Next
exitCode = shell.Run(command, 0, True)
launchError = Err.Number
Err.Clear
On Error GoTo 0
If launchError <> 0 Then
    Fail "Windows could not start the desktop launcher. No existing application was stopped.", 3
End If
If exitCode <> 0 Then
    Fail "SiYi did not start (exit " & CStr(exitCode) & "). No existing application was stopped.", exitCode
End If
WScript.Quit 0

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function

Sub Fail(message, code)
    Dim help
    help = message & vbCrLf & vbCrLf & "For details, open a terminal in the application folder and run:" & vbCrLf & diagnosticEntry
    If LCase(fileSystem.GetFileName(WScript.FullName)) = "cscript.exe" Then
        ' Explicit command-line diagnostics must not block on a GUI dialog.
        WScript.Echo help
    Else
        MsgBox help, vbOKOnly + vbExclamation, "SiYi startup failed"
    End If
    WScript.Quit code
End Sub
