# AI 작업표시줄 모니터 (TrafficMonitor 스타일)

Windows 작업표시줄 위에 **Claude Code · ChatGPT (Codex CLI) · Gemini CLI** 사용량과 **CPU · RAM · GPU** 리소스를 상시 표시하는 가벼운 오버레이 프로그램입니다.

## 주요 기능

- **AI CLI 로컬 로그 자동 집계** (API 키 불필요, 기존 구독/CLI 사용 기록 자동 읽기)
  - **Claude Code**: `~/.claude/projects/**/*.jsonl`
  - **ChatGPT (Codex CLI)**: `~/.codex/sessions/**/*.jsonl` (세션 한도 `%` 포함)
  - **Gemini CLI**: `~/.gemini/tmp/**/chats/*.json`
- **시스템 리소스 상시 표시 (TrafficMonitor 스타일)**
  - `CPU`, `RAM`, `GPU` (NVIDIA GPU 지원) 사용률 실시간 표시
  - 75% 이상 주황색, 90% 이상 빨간색 강조
- **표시 옵션 & 커스터마이징**
  - 우클릭 메뉴 또는 `설정` 창에서 Claude / ChatGPT / Gemini / CPU / RAM / GPU 개별 켜기/끄기
  - 레이아웃 선택: `2줄 (AI / 시스템)` 또는 `1줄 가로`
  - 집계 기간: `오늘 (00:00 이후)` / `최근 5시간` / `최근 7일`
  - 표기 단위: `토큰 수 (K/M)` / `예상 비용 ($)` / `목표·한도 대비 %`
  - 작업표시줄 트레이 옆 자동 도킹, 드래그 이동 및 위치 잠금, 전체화면 시 자동 숨김, Windows 시작프로그램 등록

## 실행 방법

### Windows 배포 파일

[GitHub Releases](https://github.com/praktos100-create/ai-taskbar-monitor/releases/latest)에서 PC에 맞는 ZIP을 받습니다.

| Windows 환경 | 다운로드 파일 |
| --- | --- |
| Windows 10 1809 이상 및 Windows 11 (Intel·AMD 64비트) | `AITaskbarMonitor-windows-x64.zip` |
| Windows 11 (ARM64) | `AITaskbarMonitor-windows-arm64.zip` |

ZIP 전체를 압축 해제하고 `AITaskbarMonitor.exe`를 실행합니다. Python은 배포 파일에 포함되어 있습니다. 설정과 알림 상태는 `%LOCALAPPDATA%\AITaskbarMonitor`에 저장됩니다.

### 소스에서 실행

프로젝트 폴더에서 가상환경과 의존성을 준비합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

- **일반 실행 (콘솔 창 없이 백그라운드 실행)**: `run_silent.vbs` 더블클릭
- **디버그 실행 (콘솔 창 포함)**: `run.bat` 실행

### 배포 파일 빌드

대상과 같은 아키텍처의 Windows에서 다음 명령을 실행합니다. 결과 ZIP은 `dist`에 생성됩니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe build_windows.py
```

## 조작법

- **왼쪽 클릭**: 상세 팝업 열기 (입력/출력/캐시 토큰, 예상 비용, 세션 수, VRAM/온도 확인)
- **오른쪽 클릭**: 빠른 설정 메뉴 (기간 변경, 항목 켜기/끄기, 도킹/잠금, 설정, 종료)
- **드래그**: 위치 이동 (위치 잠금이 꺼져 있을 때)
