' Hidden-window launcher for the album service (used by logon autostart).
'
' Why .vbs: a scheduled task that runs a .bat flashes a console window;
' WScript.Shell.Run with 0 hides it completely while the service stays up.
'
' It calls start_album.bat, which pins port 8787 and refuses to start on a
' port the bot is not configured for. All output goes to tools\autostart.log
' via autostart_album.bat, so nothing is lost by hiding the window.
Option Explicit
Dim shell, fso, base, bat
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
base = fso.GetParentFolderName(WScript.ScriptFullName)
bat = base & "\start_album.bat"
If Not fso.FileExists(bat) Then
  WScript.Quit 2
End If
shell.CurrentDirectory = base
' 0 = hidden window, False = do not wait (the service runs until stopped)
shell.Run "cmd /c """ & bat & """", 0, False
