$ErrorActionPreference = 'Stop'
$meetingProject = Split-Path -Parent $PSScriptRoot
$meetingPython = Join-Path $meetingProject '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $meetingPython)) {
    python -m venv (Join-Path $meetingProject '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the project Python environment.' }
}
& $meetingPython -m pip install -r (Join-Path $meetingProject 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Project dependency installation failed.' }
dotnet build (Join-Path $meetingProject 'native\MeetingAudio\MeetingAudio.csproj') -c Release --nologo
if ($LASTEXITCODE -ne 0) { throw 'Native audio helper build failed.' }
& $meetingPython -X utf8 (Join-Path $PSScriptRoot 'prepare_model.py')
if ($LASTEXITCODE -ne 0) { throw 'Speech model preparation failed.' }
& $meetingPython -X utf8 (Join-Path $meetingProject 'runner.py') doctor
