# WebTracker 빌드 가이드 (Windows)

이 폴더 소스로 `WebTracker.exe`를 직접 빌드하는 방법입니다.
받는 사람 PC에서 그대로 따라 하면 됩니다.

## 사전 요구
- Windows 10 / 11
- **Python 3.12** (3.12.x, tkinter 포함된 python.org 공식 설치본)
  - 설치 시 **"Add Python to PATH"** 체크
- **빌드하는 PC와 exe를 실행할 PC 모두 Google Chrome 설치 필요**
  (Playwright가 번들 Chromium이 아니라 시스템에 설치된 실제 Chrome을 띄우는 방식으로 바뀌었습니다 —
  봇 탐지 우회 목적. Chrome이 없는 PC에서는 exe가 트래킹을 시작하지 못합니다.)

## 빌드 단계 (PowerShell, 이 폴더에서)

```powershell
# 1) 가상환경 생성 + 활성화
python -m venv .venv
.\.venv\Scripts\Activate.ps1
#   (활성화가 막히면 먼저:  Set-ExecutionPolicy -Scope Process Bypass)

# 2) 의존성 설치
pip install -r requirements.txt

# 3) 빌드
pyinstaller --clean --noconfirm WebTracker.spec
```

## 결과물
- `dist\WebTracker.exe`  (단일 파일; Chromium을 더 이상 내장하지 않아 기존 약 240MB보다 작아집니다)

## 참고
- **Tcl/Tk(tkinter) 데이터는 `WebTracker.spec`이 자동으로** 챙깁니다.
- 브라우저는 더 이상 exe에 내장하지 않습니다 (`channel="chrome"`로 시스템 Chrome을 직접 실행).
  그만큼 exe 크기도 작아집니다.
- 실행: `WebTracker.exe` 더블클릭 → 콘솔에 `serving on ...` 뜨고 매뉴얼이 자동으로 열립니다.
  - 첫 실행은 내부 추출로 20~30초 걸립니다.
  - 데이터(`users/`, `browser_profile/`)는 **exe 옆 폴더**에 쌓이니, 쓰기 권한 있는
    위치(바탕화면 등)에 두고 실행하세요.
  - 서명 안 된 exe라 SmartScreen 경고가 뜨면 **추가 정보 → 실행**.

## 빌드 없이 소스로 바로 실행하려면
```powershell
# 위 1~2단계까지 한 뒤 (빌드 단계 3은 건너뜀)
python webtracker_launcher.py
```
