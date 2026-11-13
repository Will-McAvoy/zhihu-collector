; Inno Setup 脚本。先运行 packaging\build.ps1，再编译本文件。
; Chromium 不放进安装包。安装完成后，使用者仍需按 README 准备 Playwright 浏览器。

#define AppName "知乎内容搜集器"
#define AppVersion "0.1.7"

[Setup]
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}
DefaultDirName={autopf}\ZhihuCollector
DefaultGroupName={#AppName}
OutputBaseFilename=ZhihuCollectorSetup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64

[Files]
Source: "..\dist\知乎内容搜集器\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\知乎内容搜集器.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\知乎内容搜集器.exe"

[Run]
Filename: "{app}\知乎内容搜集器.exe"; Description: "启动知乎内容搜集器"; Flags: nowait postinstall skipifsilent
