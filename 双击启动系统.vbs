Set shell = CreateObject("WScript.Shell")
Set fileSystem = CreateObject("Scripting.FileSystemObject")
folder = fileSystem.GetParentFolderName(WScript.ScriptFullName)
python = "C:\Users\GuangTou\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
server = folder & "\server.py"
command = Chr(34) & python & Chr(34) & " " & Chr(34) & server & Chr(34) & " 8902 --open"
shell.Run command, 0, False
