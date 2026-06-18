# Web Tracker (Playwright)

사람의 웹 브라우징 행동을 trajectory 데이터로 수집하는 도구.
**브라우저 안에서 클릭/타이핑/스크롤하면 자동으로 기록됩니다.**

---

## 사용법 (3단계)

### 1) 최초 1회 setup

PowerShell 에서 이 폴더로 이동한 뒤:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\playwright install chromium
```

### 2) 실행

폴더 안의 **`start.bat` 더블클릭**.
→ 자동으로 로컬 서버가 시작되고 매뉴얼 페이지가 브라우저에서 열립니다.

### 3) 매뉴얼 페이지에서 작업

- 상단의 **Task description** 입력 → **시작** 버튼 클릭
- Chromium 작업 창이 열림 → 거기서 task 수행
- 끝나면 매뉴얼 페이지로 돌아와 **완료 (저장)** 버튼 클릭

---

## 콘솔로 직접 실행 (선택)

매뉴얼 UI 없이 CLI 로 쓰고 싶으면:

```powershell
.\.venv\Scripts\python.exe main.py
```

---

## 자세한 매뉴얼

서버 띄우지 않고 매뉴얼만 보려면 `manual.html` 을 브라우저로 그대로 열어도 됩니다.
(이 경우 페이지 안의 "시작" 버튼은 동작하지 않고 안내 메시지가 표시됩니다.)

## 시스템 요구사항

- Windows 10 / 11
- Python 3.10+ (3.12 권장)
- 디스크 1GB+ (Chromium 약 150MB + 데이터)

## 파일 구성

| 파일 | 역할 |
|---|---|
| `start.bat` | 더블클릭 실행 launcher (서버 + 브라우저) |
| `server.py` | Flask 백엔드 (매뉴얼 페이지 + 트래킹 제어) |
| `main.py` | CLI 진입점 (서버 없이 콘솔로 실행) |
| `tracker.py` | Playwright 셋업 |
| `actions.py` | raw event → action 변환 |
| `recorder.py` | jsonl + screenshot 저장 |
| `inject.js` | 브라우저 안에 주입되는 이벤트 캡처 JS |
| `utils.py` | 공통 유틸 |
| `manual.html` | 사용자 매뉴얼 (서버에서 자동 서빙) |
| `requirements.txt` | Python 의존성 |
