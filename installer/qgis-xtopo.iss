; Инсталлятор QGIS-xtopo для Windows (Inno Setup 6).
;
; Собирается скриптом installer/build-installer.sh: он распаковывает
; архив плагина в installer/payload и запускает компилятор Inno Setup.
;
; Что делает инсталлятор:
;   * проверяет, установлен ли QGIS; если нет — предлагает скачать и
;     установить QGIS официальным сетевым установщиком OSGeo4W (только
;     QGIS LTR, около 410 МБ), а если он не сработал — официальным
;     standalone-MSI;
;   * кладёт плагин в профиль QGIS (%APPDATA%\QGIS\QGIS3\profiles\default);
;   * включает плагин в QGIS3.ini (секция [PythonPlugins]);
;   * создаёт ярлык «QGIS-xtopo» на рабочем столе и в меню «Пуск».

#define MyAppName "QGIS-xtopo"
#define MyAppVersion "0.13.24"
#define MyAppPublisher "Leonid Barsukov"
#define MyAppURL "https://github.com/xmd5a2/qgis-xtopo"
#define MyAppExeName "qgis-xtopo.cmd"

[Setup]
AppId={{9F1C7B4E-2D8A-4C6B-9E3F-5A7C1D2B8E40}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={code:DefaultDir}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=qgis-xtopo-setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=..\LICENSE
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "payload\qgis_xtopo\*"; DestDir: "{code:PluginDir}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "payload\scripts\install-qgis.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "icon\logo.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\QGIS-xtopo"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{code:QgisIcon}"; WorkingDir: "{app}"
Name: "{group}\QGIS-xtopo"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{code:QgisIcon}"; WorkingDir: "{app}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,QGIS}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{code:PluginDir}"

[Code]
var
  InstallQgis: Boolean;
  QgisPath: String;

function ProfileDir: String;
begin
  Result := ExpandConstant('{userappdata}\QGIS\QGIS3\profiles\default');
end;

function PluginDir(Param: String): String;
begin
  Result := ProfileDir + '\python\plugins\qgis_xtopo';
end;

function DefaultDir(Param: String): String;
begin
  Result := ExpandConstant('{userappdata}\QGIS\QGIS3');
end;

{ Ищет исполняемый файл QGIS в каталоге установки: сам каталог или
  вложенные версионные подкаталоги вида "QGIS 3.44.15".
  Две раскладки:
    * standalone (MSI): <корень>\bin\qgis-bin.exe;
    * OSGeo4W:           <корень>\bin\qgis-ltr-bin.exe. }
function FindQgisIn(const Base: String): String;
var
  Finder: TFindRec;
  Path: String;
  Names: array[0..1] of String;
  Index: Integer;
begin
  Result := '';
  if Base = '' then
    Exit;
  Names[0] := 'qgis-bin.exe';
  Names[1] := 'qgis-ltr-bin.exe';
  for Index := 0 to 1 do
  begin
    if FileExists(Base + '\bin\' + Names[Index]) then
    begin
      Result := Base + '\bin\' + Names[Index];
      Exit;
    end;
  end;
  if FindFirst(Base + '\QGIS *', Finder) then
  begin
    try
      repeat
        if DirExists(Base + '\' + Finder.Name) then
        begin
          for Index := 0 to 1 do
          begin
            Path := Base + '\' + Finder.Name + '\bin\' + Names[Index];
            if FileExists(Path) then
            begin
              Result := Path;
              Exit;
            end;
          end;
        end;
      until not FindNext(Finder);
    finally
      FindClose(Finder);
    end;
  end;
end;

{ Системный диск ("C:") — нужен для поиска OSGeo4W. }
function SystemDrive: String;
begin
  Result := Copy(ExpandConstant('{win}'), 1, 2);
end;

function QgisExe: String;
var
  Candidates: array[0..6] of String;
  Index: Integer;
  RegPath: String;
begin
  Result := '';
  if RegQueryStringValue(HKLM, 'SOFTWARE\QGIS\QGIS3', 'InstallPath', RegPath) then
    Result := FindQgisIn(RegPath);
  if Result = '' then
    if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\QGIS\QGIS3', 'InstallPath', RegPath) then
      Result := FindQgisIn(RegPath);
  Candidates[0] := ExpandConstant('{commonpf}\QGIS 3');
  Candidates[1] := ExpandConstant('{commonpf32}\QGIS 3');
  Candidates[2] := ExpandConstant('{localappdata}\Programs\QGIS 3');
  Candidates[3] := SystemDrive + '\OSGeo4W';
  Candidates[4] := ExpandConstant('{commonpf}\QGIS 4');
  Candidates[5] := ExpandConstant('{commonpf32}\QGIS 4');
  Candidates[6] := ExpandConstant('{localappdata}\OSGeo4W');
  for Index := 0 to 6 do
  begin
    if Result <> '' then
      Break;
    Result := FindQgisIn(Candidates[Index]);
  end;
end;

{ Чем запускать QGIS: у установки OSGeo4W приложение требует переменных
  окружения из qgis-ltr.bat, поэтому запускаем через него. }
function QgisLauncher: String;
var
  Path: String;
begin
  Path := QgisExe;
  Result := Path;
  if Path = '' then
    Exit;
  if (ExtractFileExt(Path) = '.exe') and
     (ExtractFileName(Path) = 'qgis-ltr-bin.exe') then
  begin
    if FileExists(ExtractFilePath(Path) + '\qgis-ltr.bat') then
      Result := ExtractFilePath(Path) + '\qgis-ltr.bat';
  end;
end;

{ Значок QGIS: у standalone — рядом с exe, у OSGeo4W — в каталоге
  приложения. }
function QgisIconFile: String;
var
  Path: String;
begin
  Path := QgisExe;
  Result := '';
  if Path = '' then
    Exit;
  if ExtractFileName(Path) = 'qgis-ltr-bin.exe' then
  begin
    if FileExists(ExtractFilePath(ExtractFilePath(Path)) + '\apps\qgis-ltr\icons\QGIS.ico') then
    begin
      Result := ExtractFilePath(ExtractFilePath(Path)) + '\apps\qgis-ltr\icons\QGIS.ico';
      Exit;
    end;
  end;
  Result := Path;
end;

{ Иконка ярлыка: значок QGIS, если он найден, иначе — значок плагина. }
function QgisIcon(Param: String): String;
begin
  Result := QgisIconFile;
  if Result = '' then
    Result := ExpandConstant('{app}\logo.ico');
end;

function RunPowerShell(const Arguments: String): Integer;
var
  ResultCode: Integer;
  Shell: String;
begin
  Result := -1;
  ResultCode := -1;
  Shell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  if Exec(Shell, '-NoProfile -ExecutionPolicy Bypass -File "' +
    ExpandConstant('{app}\scripts\install-qgis.ps1') + '" ' + Arguments,
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := ResultCode;
end;

procedure InstallQgisIfNeeded;
var
  LogFile: String;
  ResultCode: Integer;
begin
  if QgisExe <> '' then
    Exit;
  LogFile := ExpandConstant('{tmp}\qgis-install.log');
  WizardForm.StatusLabel.Caption :=
    'Установка QGIS: скачивание установщика и установка...';  WizardForm.Refresh;
  ResultCode := RunPowerShell('-Install -LogFile "' + LogFile + '"');
  WizardForm.StatusLabel.Caption := '';
  if ResultCode <> 0 then
    MsgBox('Не удалось установить QGIS автоматически.' + #13#10 +
      'Установите QGIS вручную: https://qgis.org/download/' + #13#10 +
      'Журнал: ' + LogFile, mbError, MB_OK);
end;

{ Пишет ярлык запуска: QGIS сразу с открытой панелью плагина. }
procedure WriteLauncher;
var
  Lines: TStringList;
  Path: String;
begin
  Path := ExpandConstant('{app}\') + '{#MyAppExeName}';
  Lines := TStringList.Create;
  try
    Lines.Add('@echo off');
    Lines.Add('rem Launch QGIS with the QGIS-xtopo panel opened.');
    Lines.Add('setlocal enabledelayedexpansion');
    Lines.Add('set "QGIS_XTOPO_OPEN=1"');
    Lines.Add('set "QGIS_LAUNCHER=' + QgisLauncher + '"');
    Lines.Add('rem If QGIS was installed later, look for it ourselves.');
    Lines.Add('if not defined QGIS_LAUNCHER (');
    Lines.Add('  for /f "tokens=2,*" %%A in (''reg query "HKLM\SOFTWARE\QGIS\QGIS3" /v InstallPath 2^>nul ^| find "InstallPath"'') do set "QGIS_PATH=%%B"');
    Lines.Add('  if defined QGIS_PATH if exist "!QGIS_PATH!\bin\qgis-bin.exe" set "QGIS_LAUNCHER=!QGIS_PATH!\bin\qgis-bin.exe"');
    Lines.Add('  if defined QGIS_PATH if exist "!QGIS_PATH!\bin\qgis-ltr.bat" set "QGIS_LAUNCHER=!QGIS_PATH!\bin\qgis-ltr.bat"');
    Lines.Add(')');
    Lines.Add('if not defined QGIS_LAUNCHER if exist "%SystemDrive%\OSGeo4W\bin\qgis-ltr.bat" set "QGIS_LAUNCHER=%SystemDrive%\OSGeo4W\bin\qgis-ltr.bat"');
    Lines.Add('if not defined QGIS_LAUNCHER if exist "%SystemDrive%\OSGeo4W\bin\qgis-bin.exe" set "QGIS_LAUNCHER=%SystemDrive%\OSGeo4W\bin\qgis-bin.exe"');
    Lines.Add('if not defined QGIS_LAUNCHER if exist "%LOCALAPPDATA%\OSGeo4W\bin\qgis-ltr.bat" set "QGIS_LAUNCHER=%LOCALAPPDATA%\OSGeo4W\bin\qgis-ltr.bat"');
    Lines.Add('if not defined QGIS_LAUNCHER for /f "delims=" %%D in (''dir /b /ad "%ProgramFiles%\QGIS *" 2^>nul'') do if exist "%ProgramFiles%\%%D\bin\qgis-bin.exe" set "QGIS_LAUNCHER=%ProgramFiles%\%%D\bin\qgis-bin.exe"');
    Lines.Add('if not defined QGIS_LAUNCHER (');
    Lines.Add('  echo QGIS not found. Install QGIS from https://qgis.org/download/');
    Lines.Add('  pause');
    Lines.Add('  exit /b 1');
    Lines.Add(')');
    Lines.Add('rem Установка OSGeo4W запускается через qgis-ltr.bat: он задаёт переменные окружения.');
    Lines.Add('if "%QGIS_LAUNCHER:~-4%"==".bat" (call "%QGIS_LAUNCHER%" %*) else (start "" "%QGIS_LAUNCHER%" %*)');
    Lines.Add('endlocal');
    Lines.SaveToFile(Path);
  finally
    Lines.Free;
  end;
end;

{ Включает плагин в профиле QGIS: секция [PythonPlugins]. }
procedure EnablePlugin;
var
  IniPath: String;
  Lines: TStringList;
  Index: Integer;
  InSection: Boolean;
  Replaced: Boolean;
  SectionFound: Boolean;
begin
  IniPath := ProfileDir + '\QGIS\QGIS3.ini';
  Lines := TStringList.Create;
  try
    if FileExists(IniPath) then
      Lines.LoadFromFile(IniPath);
    InSection := False;
    Replaced := False;
    SectionFound := False;
    for Index := 0 to Lines.Count - 1 do
    begin
      if Lines[Index] = '[PythonPlugins]' then
      begin
        InSection := True;
        SectionFound := True;
      end
      else if InSection and (Pos('[', Lines[Index]) = 1) then
      begin
        InSection := False;
      end
      else if InSection and (Copy(Lines[Index], 1, 11) = 'qgis_xtopo=') then
      begin
        Lines[Index] := 'qgis_xtopo=true';
        Replaced := True;
        Break;
      end;
    end;
    if not Replaced then
    begin
      if SectionFound then
      begin
        for Index := 0 to Lines.Count - 1 do
        begin
          if Lines[Index] = '[PythonPlugins]' then
          begin
            Lines.Insert(Index + 1, 'qgis_xtopo=true');
            Break;
          end;
        end;
      end
      else
      begin
        if Lines.Count > 0 then
          Lines.Add('');
        Lines.Add('[PythonPlugins]');
        Lines.Add('qgis_xtopo=true');
      end;
    end;
    ForceDirectories(ExtractFilePath(IniPath));
    Lines.SaveToFile(IniPath);
  finally
    Lines.Free;
  end;
end;

function InitializeSetup: Boolean;
var
  Answer: Integer;
begin
  Result := True;
  InstallQgis := False;
  QgisPath := QgisExe;
  if QgisPath <> '' then
    Exit;
  if WizardSilent then
  begin
    // В молчаливом режиме QGIS не качаем: файл большой, а решение
    // должен принимать человек.
    Log('QGIS не найден, автоматическая установка отключена (молчаливый режим)');
    Exit;
  end;
  Answer := MsgBox('QGIS не найден на этом компьютере.' + #13#10 + #13#10 +
    'Установить QGIS сейчас? Будет скачан официальный установщик ' +
    '(QGIS LTR, около 410 МБ) и установлен без дополнительных вопросов.' + #13#10 + #13#10 +
    'Если отказаться, плагин всё равно будет установлен — запустить QGIS ' +
    'придётся отдельно.',
    mbConfirmation, MB_YESNO);
  InstallQgis := Answer = IDYES;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if InstallQgis then
      InstallQgisIfNeeded;
    QgisPath := QgisExe;
    WriteLauncher;
    EnablePlugin;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  IniPath: String;
  Lines: TStringList;
  Index: Integer;
  InSection: Boolean;
  Found: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    IniPath := ProfileDir + '\QGIS\QGIS3.ini';
    if FileExists(IniPath) then
    begin
      Lines := TStringList.Create;
      try
        Lines.LoadFromFile(IniPath);
        InSection := False;
        Found := -1;
        for Index := 0 to Lines.Count - 1 do
        begin
          if Lines[Index] = '[PythonPlugins]' then
          begin
            InSection := True;
          end
          else if InSection and (Pos('[', Lines[Index]) = 1) then
          begin
            InSection := False;
          end
          else if InSection and (Copy(Lines[Index], 1, 11) = 'qgis_xtopo=') then
          begin
            Found := Index;
            Break;
          end;
        end;
        if Found >= 0 then
        begin
          Lines.Delete(Found);
          Lines.SaveToFile(IniPath);
        end;
      finally
        Lines.Free;
      end;
    end;
  end;
end;
