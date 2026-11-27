Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
venvPyw = fso.BuildPath(appDir, ".venv\Scripts\pythonw.exe")
localPyw = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\Python\bin\pythonw.exe")

If fso.FileExists(venvPyw) Then
    pywCmd = """" & venvPyw & """"
ElseIf fso.FileExists(localPyw) Then
    pywCmd = """" & localPyw & """"
Else
    pywCmd = "pythonw.exe"
End If

shell.CurrentDirectory = appDir
shell.Run pywCmd & " """ & appDir & "\main.py""", 0, False
