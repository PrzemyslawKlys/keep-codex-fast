Option Explicit

Dim shell, fso, scriptDir, powerShellScript, command, exitCode

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
powerShellScript = fso.BuildPath(scriptDir, "run_hot_normalize_paths.ps1")

shell.CurrentDirectory = scriptDir
command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Quote(powerShellScript) & " -WatchSeconds 1500 -IntervalSeconds 30"
exitCode = shell.Run(command, 0, True)

WScript.Quit exitCode

Function Quote(value)
    Quote = """" & Replace(value, """", """""") & """"
End Function
