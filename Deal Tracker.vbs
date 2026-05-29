Set sh = CreateObject("WScript.Shell")
appDir = "C:\Users\User\CodingProjekte\Web scraper"
sh.Run "C:\Python314\pythonw.exe """ & appDir & "\main.py""", 0, False
