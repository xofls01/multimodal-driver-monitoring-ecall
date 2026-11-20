# Windows 한 번에 실행: Unity 에디터 + e-Call 서비스 + e-Call 수신 화면 + 차량 서버(E4)
#
#   powershell -ExecutionPolicy Bypass -File .\run_e4_demo.ps1          # 저장된 E4 분석 결과 + CAN danger 재생
#   powershell -ExecutionPolicy Bypass -File .\run_e4_demo.ps1 -Live    # 모델이 E4 영상·생체신호를 지금 분석 (실시간)
#
# 실행 후 Unity 에서 BasicDrivingStage1 씬을 열고 Play 를 누른다. 갓길 정차 후 e-Call 이 오면
# 브라우저에 e-Call 수신 화면(http://localhost:8765)이 열린다. 이 창에서 Ctrl+C(또는 창 닫기)로 모두 종료.
# PC 마다 다른 값은 옵션이나 환경 변수로 준다 (ecall_mqtt/README.md 'Windows·macOS 한 번에 실행' 참고).
param(
    [switch]$Live,                                    # 실시간 모델 분석 (없으면 저장된 결과 재생)
    [string]$UnityProject = $env:UNITY_PROJECT,       # 기본: %USERPROFILE%\My project
    [string]$UnityExe = $env:UNITY_EXE,               # 기본: 프로젝트 버전에 맞는 Unity Hub 에디터 자동 선택
    [string]$Recording = $env:E4_RECORDING,           # 재생용 분석 결과 폴더 (preview.mp4, unity_packets.jsonl)
    [string]$Pairs = $env:E4_PAIRS,                   # -Live 입력 목록 (기본: data\local_e4\pairs.json → 없으면 공식 system_pairs.json)
    [string]$Broker = $(if ($env:BROKER) { $env:BROKER } else { 'localhost' })   # Pi 를 쓰면 Pi IP
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$modelRoot = Join-Path $root 'driver-state-monitoring-system'
$ecallDir = Join-Path $root 'ecall_mqtt'
$tag = if ($Live) { '[LIVE]' } else { '[DEMO]' }
function Fail($msg) { Write-Host "$tag $msg" -ForegroundColor Red; exit 1 }

# --- Python: paho-mqtt·opencv(·torch) 가 설치된 모델 가상환경을 직접 쓴다 (PATH 의 python 은 바뀔 수 있음)
$python = Join-Path $modelRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { Fail "모델 가상환경이 없습니다: $python (driver-state-monitoring-system README 대로 .venv 를 먼저 만드세요)" }
& $python -c "import paho.mqtt, cv2" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "$python 에 paho-mqtt 또는 opencv 가 없습니다. 설치: & '$python' -m pip install paho-mqtt opencv-python" }

# --- 입력 데이터
if ($Live) {
    if (-not $Pairs) {
        $Pairs = if (Test-Path (Join-Path $modelRoot 'data\local_e4\pairs.json')) { 'data\local_e4\pairs.json' } else { 'config\multimodal\system_pairs.json' }
    }
    if (-not (Test-Path (Join-Path $modelRoot $Pairs)) -and -not (Test-Path $Pairs)) { Fail "입력 목록이 없습니다: $Pairs (-Pairs 로 지정)" }
} else {
    if (-not $Recording) {
        $Recording = @('outputs\local_e4_test\E4', 'outputs\e4_overlay_crop\E4') |
            ForEach-Object { Join-Path $modelRoot $_ } | Where-Object { Test-Path (Join-Path $_ 'unity_packets.jsonl') } | Select-Object -First 1
    }
    if (-not $Recording -or -not (Test-Path (Join-Path $Recording 'unity_packets.jsonl')) -or -not (Test-Path (Join-Path $Recording 'preview.mp4'))) {
        Fail "저장된 E4 분석 결과가 없습니다. preview.mp4, unity_packets.jsonl 이 있는 폴더를 -Recording 으로 지정하거나, -Live 로 실행하세요."
    }
}

# --- MQTT 브로커 확인 (이 PC 의 Mosquitto 또는 Pi)
$tcp = New-Object Net.Sockets.TcpClient
try { $ok = $tcp.ConnectAsync($Broker, 1883).Wait(1500) } catch { $ok = $false } finally { $tcp.Close() }
if (-not $ok) { Fail "$Broker`:1883 에 MQTT 브로커가 없습니다. Mosquitto 를 설치하고 서비스를 시작하세요 (관리자 PowerShell: Start-Service mosquitto)." }

# --- 1) 이전 실행에서 남은 서버 정리 (MQTT 접속 ID·포트 5001/5002/8765 가 겹치지 않게)
function Stop-DemoPython {
    Get-CimInstance Win32_Process -Filter "name='python.exe'" |
        Where-Object { $_.CommandLine -match 'ecall_service\.py|run_vehicle_with_ecall\.py|ecall_screen\.py|run_multimodal_system\.py' } |
        ForEach-Object { Write-Host "$tag 종료: PID $($_.ProcessId)"; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
Stop-DemoPython

# --- 2) Unity 에디터: 이 프로젝트가 이미 열려 있으면 그대로 쓰고, 아니면 연다
if (-not $UnityProject) { $UnityProject = Join-Path $env:USERPROFILE 'My project' }
function Find-UnityExe {
    # 프로젝트 버전(ProjectVersion.txt)과 같은 버전의 Hub 에디터. 같은 버전이 여럿이면 패키지가 온전히 깔린 쪽
    $versionFile = Join-Path $UnityProject 'ProjectSettings\ProjectVersion.txt'
    if (-not (Test-Path $versionFile)) { return $null }
    $want = (Select-String -Path $versionFile -Pattern '^m_EditorVersion:\s*(\S+)').Matches[0].Groups[1].Value
    $hub = Join-Path $env:ProgramFiles 'Unity\Hub\Editor'
    $found = Get-ChildItem $hub -Directory -ErrorAction SilentlyContinue |
        Where-Object { ($_.Name -eq $want -or $_.Name -like "$want-*") -and (Test-Path (Join-Path $_.FullName 'Editor\Unity.exe')) } |
        Sort-Object { -not (Test-Path (Join-Path $_.FullName 'Editor\Data\Resources\PackageManager\BuiltInPackages')) }
    if ($found) { return Join-Path @($found)[0].FullName 'Editor\Unity.exe' }
    return $null
}
$unityOpen = Get-CimInstance Win32_Process -Filter "name='Unity.exe'" | Where-Object { $_.CommandLine -like "*$UnityProject*" }
if ($unityOpen) {
    Write-Host "$tag Unity 이미 실행 중 (PID $(@($unityOpen)[0].ProcessId))"
} else {
    if (-not $UnityExe) { $UnityExe = Find-UnityExe }
    if ($UnityExe -and (Test-Path $UnityExe)) {
        Write-Host "$tag Unity 에디터 실행 중... ($UnityExe, 창이 뜨기까지 1~2분)"
        Start-Process $UnityExe -ArgumentList '-projectPath', "`"$UnityProject`""
    } else {
        Write-Host "$tag Unity 에디터를 찾지 못했습니다. Unity Hub 에서 '$UnityProject' 를 직접 여세요 (또는 -UnityExe 로 지정)." -ForegroundColor Yellow
    }
}

$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
[Console]::OutputEncoding = [Text.Encoding]::UTF8   # 로그 한글이 깨지지 않게

# --- 3) (-Live) 모델 (새 창): E4 영상 + 생체신호를 분석해 포트 5002 로 보낸다.
#        차량 서버는 Unity Play 뒤에 모델에 접속하고, 모델은 접속될 때까지 분석을 멈추고 기다린다.
#        --show: 분석 영상·ECG/PPG 창 / --pace: 영상 시간보다 빨리 처리하지 않음 (느린 PC 에서는 더 늦어질 수 있음)
$started = @()
if ($Live) {
    $started += Start-Process $python -WorkingDirectory $modelRoot -PassThru -ArgumentList `
        '-B', 'scripts\run_multimodal_system.py', '--pairs', "`"$Pairs`"", '--only', 'E4',
        '--unity', '--unity-port', '5002', '--show', '--pace', '--output', 'outputs\local_e4_unity'
}

# --- 4) e-Call 서비스(Pi 역할, 이 PC 브로커일 때만) + e-Call 수신 화면(폰 역할, e-Call 이 올 때 브라우저가 열림)
if ($Broker -in @('localhost', '127.0.0.1')) {
    $started += Start-Process $python -ArgumentList '-B', 'ecall_service.py' -WorkingDirectory $ecallDir -NoNewWindow -PassThru
} else {
    Write-Host "$tag 브로커 $Broker 의 Pi ecall-service 를 사용합니다 (이 PC 에서 ecall_service.py 를 띄우지 않음)."
}
$started += Start-Process $python -ArgumentList '-B', 'ecall_screen.py', '--broker', $Broker, '--open-on-ecall' -WorkingDirectory $ecallDir -NoNewWindow -PassThru

# --- 5) 차량 서버 (이 창): Unity(5001) ↔ 모델(5002) 또는 저장된 결과, 정차하면 e-Call 요청
try {
    Write-Host "$tag Unity 에서 BasicDrivingStage1 씬을 열고 Play 를 누르세요."
    $vehicle = Join-Path $ecallDir 'run_vehicle_with_ecall.py'
    if ($Live) {
        & $python -B $vehicle --ecall-broker $Broker --model-port 5002
    } else {
        & $python -B $vehicle --ecall-broker $Broker --ecall-entry play_can danger --recording $Recording
    }
} finally {
    # venv 의 python.exe 는 실제 파이썬을 자식 프로세스로 띄우므로 명령줄로 찾아 함께 종료한다
    foreach ($p in $started) { Stop-Process -Id $p.Id -ErrorAction SilentlyContinue }
    Stop-DemoPython
}
