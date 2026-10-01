; Inno Setup script for SongSnag. Built by .github/workflows/release.yml:
;   iscc /DAppVersion=1.0.0 packaging\windows\songsnag.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6C4B0F0E-5E0B-4E55-9A63-1D5C3B7A9E21}
AppName=SongSnag
AppVersion={#AppVersion}
AppPublisher=BlindingBright
DefaultDirName={localappdata}\Programs\SongSnag
DefaultGroupName=SongSnag
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=SongSnag-Setup-{#AppVersion}
SetupIconFile=songsnag.ico
UninstallDisplayIcon={app}\SongSnag.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
LicenseFile=..\..\LICENSE

[Tasks]
Name: "autostart"; Description: "Start SongSnag when I sign in"; GroupDescription: "Options:"
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Options:"; Flags: unchecked

[Files]
Source: "..\..\dist\SongSnag\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SongSnag"; Filename: "{app}\SongSnag.exe"
Name: "{group}\Uninstall SongSnag"; Filename: "{uninstallexe}"
Name: "{autodesktop}\SongSnag"; Filename: "{app}\SongSnag.exe"; Tasks: desktopicon

[Registry]
; Same value the app's own "Start at login" setting manages.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SongSnag"; \
    ValueData: """{app}\SongSnag.exe"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\SongSnag.exe"; Description: "Start SongSnag now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM SongSnag.exe"; Flags: runhidden; RunOnceId: "StopSongSnag"

[UninstallDelete]
; The downloaded yt-dlp updates; settings, catalog and music are kept.
Type: filesandordirs; Name: "{localappdata}\SongSnag\ytdlp"
