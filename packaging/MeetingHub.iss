; Установщик Meeting Hub (Inno Setup 6).
; Сборка: ISCC /DAppVer=7.41.0 packaging\MeetingHub.iss  (из корня репозитория, после PyInstaller)
; Ставится без прав администратора в %LOCALAPPDATA%\Programs\Meeting Hub.
; Данные пользователя лежат отдельно в %LOCALAPPDATA%\Meeting Hub и при удалении программы сохраняются.

#ifndef AppVer
  #define AppVer "0.0.0"
#endif

[Setup]
AppId={{8C1F2B6E-5A43-4C3B-9E2D-6F1D0B7A9C11}
AppName=Meeting Hub
AppVersion={#AppVer}
AppVerName=Meeting Hub {#AppVer}
AppPublisher=Meeting Hub
DefaultDirName={localappdata}\Programs\Meeting Hub
DefaultGroupName=Meeting Hub
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\pkg
OutputBaseFilename=MeetingHubSetup-v{#AppVer}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=MeetingHub.ico
UninstallDisplayIcon={app}\MeetingHub.exe
UninstallDisplayName=Meeting Hub
CloseApplications=force
RestartApplications=no
SetupLogging=yes
VersionInfoVersion={#AppVer}
VersionInfoProductName=Meeting Hub
VersionInfoDescription=Meeting Hub — установщик

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"
Name: "autostart"; Description: "Запускать Meeting Hub вместе с Windows (в фоне, значок у часов) — иначе встречи не записываются, пока программа закрыта"; GroupDescription: "Работа в фоне:"

[Registry]
; То же значение меняет галочка «Запускать вместе с Windows» в настройках программы (daily.py, RUN_NAME).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Meeting Hub"; ValueData: """{app}\MeetingHub.exe"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart

[InstallDelete]
; При обновлении убираем библиотеки прошлой версии, чтобы не смешивались.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\MeetingHub\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "..\README.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "setup_ollama.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "MeetingHub.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Meeting Hub"; Filename: "{app}\MeetingHub.exe"; IconFilename: "{app}\MeetingHub.ico"
Name: "{autoprograms}\Meeting Hub — проверка звука"; Filename: "{app}\MeetingHub.exe"; Parameters: "--audio-check --open"
Name: "{autodesktop}\Meeting Hub"; Filename: "{app}\MeetingHub.exe"; IconFilename: "{app}\MeetingHub.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\MeetingHub.exe"; Description: "Запустить Meeting Hub"; Flags: nowait postinstall skipifsilent
; Обновление из программы: тихая установка с /launch=1 сама запускает новую версию.
Filename: "{app}\MeetingHub.exe"; Flags: nowait; Check: LaunchAfterSilentUpdate

[Messages]
ru.FinishedLabel=Meeting Hub установлен.%n%nПри первом запуске откроется мастер настройки: он проверит запись и перенесёт данные из прежней версии, если она была.%n%nЕсли на компьютере Kaspersky — мастер подскажет, как разрешить программе запись.

[Code]
function LaunchAfterSilentUpdate: Boolean;
begin
  Result := WizardSilent and (ExpandConstant('{param:launch|0}') = '1');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { Тихое удаление (/VERYSILENT) не должно ждать нажатия кнопки. }
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent) then
    SuppressibleMsgBox('Программа удалена. Ваши записи, шаблоны и журнал сохранены в папке:' + #13#10 +
           ExpandConstant('{localappdata}\Meeting Hub') + #13#10 +
           'Удалите её вручную, если они больше не нужны.', mbInformation, MB_OK, IDOK);
end;
