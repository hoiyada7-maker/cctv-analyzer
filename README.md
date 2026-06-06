# CCTV Analyzer (비용 최적화 버전)

저장된 CCTV 영상 파일을 매일 자동 분석해서 텔레그램/이메일로
일일 요약을 보내주는 가벼운 파이썬 도구.

## 핵심 특징 — 비용 최적화

기존 "영상 통째로 Gemini에 보내기" 방식 대비 **API 비용 90~95% 절감**:

1. **OpenCV 움직임 감지**: 조용한 구간은 API 호출 자체를 안 함
2. **인접 클립 병합**: 파일 경계에 걸친 사건, NVR이 잘게 쪼갠 연속 이벤트를
   하나로 묶어 1번만 분석 (API 호출 30~50% 추가 절감)
3. **Flash-Lite 우선 + Flash 폴백**: 모호한 경우만 비싼 모델 사용
4. **저FPS 분석**: 0.5fps (2초당 1프레임)로 토큰 50% 절약
5. **결과 기반 중복 제거**: 반복되는 사소한 움직임 1줄로 정리

**예상 월 비용** (카메라 1~2대 가정용):
- 무료 티어 안에서 끝날 가능성 높음
- 유료로 넘어가도 **월 2,000~5,000원** 수준

## 동작 방식

```
[매일 새벽 3시 cron]
  ↓
어제 날짜 영상 파일 찾기
  ↓
[1단계] OpenCV 움직임 감지 (사건 있는 구간만)
  ↓
[2단계] 인접 클립 병합 (파일 경계 사건 통합)
  ↓
[3단계] Gemini Flash-Lite 분석 (모호하면 Flash)
  ↓
[4단계] 분석 결과 기반 중복 제거
  ↓
[5단계] 일일 종합 요약 생성
  ↓
텔레그램/이메일 발송 + DB 저장
```

## 필요한 것

- Python 3.10 이상
- ffmpeg
- Gemini API 키 (무료 발급)
- 영상 파일이 저장되는 로컬 폴더 (NAS 마운트도 OK)
- (선택) 텔레그램 봇 토큰

## 설치

```bash
# 1. 시스템 패키지
sudo apt update
sudo apt install -y python3-pip ffmpeg

# 2. 프로젝트 폴더로 이동
cd cctv-analyzer

# 3. 파이썬 의존성
pip install -r requirements.txt

# 4. 설정 파일 복사 후 편집
cp config.yaml.example config.yaml
nano config.yaml
```

## 설정

### Gemini API 키 발급

1. https://aistudio.google.com 접속
2. "Get API key" → 새 키 생성
3. `config.yaml`의 `gemini.api_key`에 입력

### 카메라 폴더 지정

본인 CCTV NVR이 영상을 어떻게 저장하는지 확인 후 패턴 설정.

폴더 구조 예시:

```
# 예시 1: 날짜별 하위폴더
/mnt/cctv/livingroom/
├── 2026-05-09/
│   ├── 00-00-00.mp4
│   ├── 01-00-00.mp4
│   └── ...
└── 2026-05-10/

# config.yaml 패턴: "%Y-%m-%d/*.mp4"
```

```
# 예시 2: 파일명에 날짜 포함
/mnt/cctv/entrance/
├── 2026-05-09_00-00-00.mp4
├── 2026-05-09_01-00-00.mp4
└── ...

# config.yaml 패턴: "%Y-%m-%d*.mp4"
```

여러 패턴을 동시에 지정할 수 있어요 (`file_patterns` 배열).

### 움직임 감지 튜닝

`config.yaml`의 `motion` 섹션:

| 옵션 | 설명 | 권장값 |
|---|---|---|
| `min_area_ratio` | 화면의 몇 %가 변해야 움직임? | 0.005 (실내), 0.01 (야외) |
| `min_duration` | 최소 지속시간 (초) | 2.0 |
| `merge_gap` | 단일 영상 내 통합 간격 | 10.0 |
| `merge_across_files_gap` | 파일 간 통합 간격 | 15.0 |

야외 카메라는 잎사귀 흔들림 때문에 `min_area_ratio`를 0.01~0.02로 키우는 게 좋아요.

### 텔레그램 봇 만들기

1. 텔레그램에서 `@BotFather` 검색 → `/newbot`으로 봇 생성
2. 받은 토큰을 `config.yaml`의 `telegram.bot_token`에 입력
3. 만든 봇과 한 번 대화 (아무 메시지)
4. 브라우저로 `https://api.telegram.org/bot<토큰>/getUpdates` 접속
5. 응답 JSON에서 `"chat":{"id":123456789` 의 숫자를 `chat_id`에 입력

## 실행

### 수동 테스트

```bash
# 어제 영상 분석
python analyzer.py

# 특정 날짜 분석
python analyzer.py 2026-05-09

# 움직임 감지만 단독 테스트 (Gemini 호출 안 함)
python motion_detector.py /path/to/video.mp4
```

### 자동 실행 (cron)

```bash
crontab -e
```

다음 줄 추가:

```
0 3 * * * cd /path/to/cctv-analyzer && /usr/bin/python3 analyzer.py >> logs/cron.log 2>&1
```

매일 새벽 3시에 어제 영상을 분석합니다.

## 비용 모니터링

매번 분석 후 텔레그램 메시지 마지막에 비용 리포트가 표시됩니다:

```
💰 분석 비용: 약 87원 (API 호출 12회, 폴백 2회)
```

DB(`events.db`)에도 일자별 비용이 누적 저장됩니다:

```bash
sqlite3 events.db "SELECT date, event_count, cost_krw FROM daily_summaries ORDER BY date DESC LIMIT 30"
```

월 비용이 너무 높으면 다음 옵션 조정:

| 비용 ↑ 원인 | 해결 |
|---|---|
| 움직임 너무 많이 감지됨 | `min_area_ratio` ↑ (0.01~0.02) |
| 폴백 호출 비율 높음 | `use_fallback: false` |
| 사건마다 너무 자세히 | `analysis_fps: 0.3` |
| 사건이 너무 김 | `max_clip_duration_sec: 180` |

## 폴더 구조

```
cctv-analyzer/
├── analyzer.py             # 메인 스크립트
├── motion_detector.py      # OpenCV 움직임 감지
├── gemini_client.py        # Gemini API 클라이언트
├── config.yaml             # 본인 설정 (직접 작성)
├── config.yaml.example     # 설정 템플릿
├── requirements.txt
├── README.md
├── events.db               # SQLite (자동 생성)
├── logs/                   # 로그 (자동 생성)
└── work/                   # 임시 클립 (자동 생성/삭제)
```

## DB 조회 예시

```bash
# 어제 이벤트 모두 보기
sqlite3 events.db "SELECT camera, absolute_start, description FROM clips
                   WHERE date='2026-05-10' AND has_event=1
                   ORDER BY absolute_start"

# 카메라별 이벤트 수
sqlite3 events.db "SELECT camera, COUNT(*) FROM clips
                   WHERE has_event=1
                   GROUP BY camera"

# 비용 추이
sqlite3 events.db "SELECT date, cost_krw FROM daily_summaries
                   ORDER BY date DESC LIMIT 30"
```

## 트러블슈팅

### 영상 파일이 발견되지 않음

- `config.yaml`의 `cameras[].path` 경로 확인
- `file_patterns`이 실제 파일명과 일치하는지 확인
- 파일의 modification time이 어제 날짜인지: `ls -la /path/to/cctv/`

### 움직임 감지가 너무 많이/적게 됨

`python motion_detector.py /path/to/video.mp4`로 단독 테스트해서 출력 확인 후
`min_area_ratio` 값을 조정.

### Gemini API 에러

- 무료 티어 분당 RPM 제한 도달 가능 → `time.sleep` 추가 또는 유료 전환
- 영상 파일이 너무 크면 (20MB 이상) ffmpeg 다운샘플이 제대로 됐는지 확인

### ffmpeg 미설치

```bash
# Ubuntu/Debian
sudo apt install -y ffmpeg

# 확인
ffmpeg -version
```

## 라이선스

MIT
