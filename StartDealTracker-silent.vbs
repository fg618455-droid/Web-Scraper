' StartDealTracker-silent.vbs
' Startet den Deal Tracker ohne Konsolenfenster (Window-Style 0 = versteckt)
' pythonw.exe = kein Terminal-Popup; Arbeitsverzeichnis wird gesetzt damit
' relative Pfade (deals.db, config.json, templates/) korrekt aufgeloest werden.

Dim shell, fso, scriptDir, mainPy, cmd
Set shell     = CreateObject("WScript.Shell")
Set fso       = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
mainPy    = scriptDir & "\main.py"

' Wechsle ins Projektverzeichnis damit relative Pfade stimmen
shell.CurrentDirectory = scriptDir

' pythonw.exe aus dem PATH -- kein separates .venv im Deal Tracker
cmd = "pythonw.exe """ & mainPy & """"
shell.Run cmd, 0, False

Set shell = Nothing
Set fso   = Nothing
