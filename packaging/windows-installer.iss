#ifndef AppVersion
  #error AppVersion must be passed by the build script
#endif
#ifndef AppSource
  #define AppSource "..\dist\windows\Asteria"
#endif
[Setup]
#ifdef QAInstall
AppId={{25AE87DA-8720-4F4A-BCA5-CA1AB62D6785}
AppName=Asteria QA
DefaultDirName={localappdata}\Programs\AsteriaQA
DefaultGroupName=Asteria QA
OutputBaseFilename=Asteria-QA-Setup-x64
#else
AppId={{84D5CB94-38E8-41E3-AE3A-3D7FC897A62C}
AppName=Asteria
DefaultDirName={localappdata}\Programs\Asteria
DefaultGroupName=Asteria
OutputBaseFilename=Asteria-{#AppVersion}-Setup-x64
#endif
AppVersion={#AppVersion}
AppPublisher=LexianLix
AppPublisherURL=https://github.com/LexianLix/asteria
AppSupportURL=https://github.com/LexianLix/asteria/issues
LicenseFile=..\LICENSE
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\windows
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\Asteria.exe
SetupIconFile=..\build\windows\paper-reader.ico
CloseApplications=no
RestartApplications=no
AppMutex=Local\Asteria-Installer
DisableProgramGroupPage=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "{#AppSource}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

#ifndef QAInstall
[Icons]
Name: "{group}\Asteria"; Filename: "{app}\Asteria.exe"
Name: "{autodesktop}\Asteria"; Filename: "{app}\Asteria.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Asteria.exe"; Description: "Open Asteria"; Flags: nowait postinstall skipifsilent
#endif

; No uninstall-delete rule for user data. Upgrade/uninstall must preserve it.
