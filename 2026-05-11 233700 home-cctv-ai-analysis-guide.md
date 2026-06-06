# 집 CCTV AI 자동 분석 시스템 구축 가이드

> Frigate + LLM Vision + Gemini API 조합으로 집 CCTV 영상을 주기적으로 분석하고
> 자연어로 무슨 일이 있었는지 알려주는 시스템 구축 가이드

---

## 목차

1. [개요 및 접근법 비교](#1-개요-및-접근법-비교)
2. [전체 아키텍처](#2-전체-아키텍처)
3. [하드웨어 최소 사양](#3-하드웨어-최소-사양)
4. [구축 비용 (한국 시장 기준)](#4-구축-비용-한국-시장-기준)
5. [설치 가이드](#5-설치-가이드)
6. [핵심 자동화 — 일일 요약](#6-핵심-자동화--일일-요약)
7. [운영 비용 및 트러블슈팅](#7-운영-비용-및-트러블슈팅)
8. [한국 환경 특수 고려사항](#8-한국-환경-특수-고려사항)

---

## 1. 개요 및 접근법 비교

집 CCTV 영상을 AI로 분석해서 "오늘 무슨 일이 있었는지" 자연어로 요약받는 방법은 크게 3가지 접근법이 있습니다.

### 1.1 Frigate + LLM Vision (가장 보편적, 추천)

- **Frigate**: 로컬 NVR. OpenCV와 TensorFlow로 IP 카메라 객체 감지 (사람·차량·동물 1차 필터링)
- **LLM Vision**: Home Assistant 통합. Frigate 이벤트를 멀티모달 LLM(Gemini, Claude, GPT 등)으로 분석
- **Gemini API**: 자연어 요약 생성 (Flash 모델 무료 티어로 가정용 충분)

### 1.2 DeepCamera (올인원)

- SharpAI의 오픈소스. VLM 직접 내장 (Qwen, DeepSeek, SmolVLM, LLaVA)
- Telegram·Discord·Slack으로 "경비원과 대화"하듯 질문 가능
- Mac Mini 또는 AI PC에서 로컬 실행 (프라이버시 우수)

### 1.3 직접 구축 (개발자용)

- OpenCV + YOLO (Ultralytics) + Gemini/Claude API + cron + Telegram Bot
- Python 100~200줄 정도면 구현 가능

### 1.4 어떤 걸 고를까?

| 상황 | 추천 |
|---|---|
| Home Assistant 이미 쓰고 있음 | **Frigate + LLM Vision** |
| 코딩 부담 없이 통합 솔루션 원함 | **DeepCamera** |
| 프라이버시 최우선, 로컬 전용 | DeepCamera 또는 Frigate + Ollama |
| 직접 만들고 싶음 | OpenCV + YOLO + Gemini API |

---

## 2. 전체 아키텍처

```
IP 카메라 (RTSP)
  → Frigate (객체 감지, 이벤트 클립 생성)
  → MQTT (이벤트 알림)
  → Home Assistant (자동화 트리거)
  → LLM Vision (이벤트 영상을 Gemini로 전송)
  → 알림 / 타임라인 / 일일 요약
```

---

## 3. 하드웨어 최소 사양

### 3.1 중요 업데이트 (2026년 기준)

**Coral TPU는 더 이상 새 설치에 권장되지 않습니다.** Intel N100 미니PC의 내장 GPU(OpenVINO)만으로도 충분합니다. N100 + OpenVINO 조합이면 카메라 1~6대까지 무리 없이 처리 가능합니다.

### 3.2 최소 사양 (카메라 1~2대)

| 항목 | 권장 | 최소 |
|---|---|---|
| **CPU** | Intel N100 (Alder Lake-N) 이상 | Intel N95, Ryzen 5 5500U |
| **RAM** | 16GB | 8GB |
| **저장공간** | SSD 500GB + 녹화용 HDD 1TB | SSD 256GB |
| **네트워크** | 기가비트 유선 LAN | Wi-Fi 5 이상 |
| **OS** | Debian 12 / Ubuntu 22.04 | HA OS |

핵심 조건: Intel CPU (AVX + AVX2 명령어 지원), Debian 실행 가능.

### 3.3 권장 사양 (카메라 3~6대)

| 항목 | 사양 |
|---|---|
| **본체** | Beelink EQ13 (N100, 듀얼 LAN) 또는 GMKtec G3 Plus |
| **RAM** | 16GB DDR5 |
| **SSD** | NVMe 500GB |
| **녹화용** | 외장 HDD 2TB 또는 NAS |

> ⚠️ Beelink EQ14는 알려진 호환성 문제가 있어 현재 피해야 합니다.

### 3.4 저장 공간 산정

- 카메라 1대당 연속 녹화 시 하루 약 10~15GB
- 1TB → 4대 기준 약 2주 분량 (오래된 것부터 덮어쓰기)

---

## 4. 구축 비용 (한국 시장 기준)

### 4.1 미니PC 옵션

#### 옵션 A: 최저가 — Beelink EQ13 베어본 (해외직구)
- Beelink EQ13 베어본 (핫딜 시): 약 15만원
- RAM 16GB DDR5: 4~5만원
- NVMe SSD 500GB: 5~7만원
- **합계: 약 25~28만원**

#### 옵션 B: 즉시 사용 — 국내 A/S 미니PC
- 국내 A/S 미니PC (N95/N100/N150) 메모리·SSD 포함: 약 35만원
- G마켓 등에서 무료배송, 당일발송

#### 옵션 C: 신뢰성 우선 — HP/Lenovo 중고
- HP EliteDesk Mini G9 (i5-12500T 등): 중고 30~50만원
- 더 강력한 처리 능력, 5년 이상 안정성

### 4.2 카메라 (RTSP 지원 필수)

| 모델 | 특징 | 한국 가격 |
|---|---|---|
| **TP-Link Tapo C220** | 2K, RTSP 지원, 가성비 | 4~5만원 |
| **Reolink RLC-410** | PoE, 5MP, 야외용 | 8~10만원 |
| **샤오미 미지아 외부용** | RTSP 활성화 가능 | 4~6만원 |
| **EZVIZ C6N** | 실내 회전형 | 3~4만원 |

> ⚠️ **국내 통신사 IoT 카메라(SKT/KT/LGU+)는 대부분 RTSP가 막혀 있습니다.** Tapo, Reolink 추천.

### 4.3 부가 장비

- **PoE 스위치** (PoE 카메라용): TP-Link TL-SF1005P 5포트 — 3~4만원
- **UPS** (정전 대비): APC BE600M1-KR — 8~10만원
- **외장 HDD 2TB**: WD Elements — 8~10만원

### 4.4 총 예상 비용

#### 💰 최저가형 (카메라 2대) — 약 40만원
| 항목 | 비용 |
|---|---|
| Beelink EQ13 베어본 + RAM/SSD | 25만원 |
| Tapo C220 × 2대 | 9만원 |
| 외장 HDD 1TB | 6만원 |

#### 💰 표준형 (카메라 4대, 권장) — 약 55~60만원
| 항목 | 비용 |
|---|---|
| Beelink EQ13 베어본 + 16GB RAM + 500GB NVMe | 28만원 |
| Tapo C220 × 4대 | 18만원 |
| 외장 HDD 2TB | 9만원 |
| PoE 스위치 (선택) | 4만원 |

#### 💰 프리미엄형 (카메라 6~8대, 야외 포함) — 약 140만원
| 항목 | 비용 |
|---|---|
| HP EliteDesk Mini G9 중고 + 32GB RAM | 50만원 |
| Reolink PoE 카메라 × 6대 | 60만원 |
| PoE 스위치 8포트 | 7만원 |
| NAS용 HDD 4TB | 15만원 |
| UPS | 9만원 |

---

## 5. 설치 가이드

### 5.1 디렉터리 구조

```
/opt/smarthome/
├── docker-compose.yml
├── .env
├── homeassistant/        (HA config)
├── frigate/
│   ├── config.yml
│   └── storage/          (녹화본)
└── mosquitto/
    ├── config/
    ├── data/
    └── log/
```

### 5.2 docker-compose.yml

```yaml
services:
  homeassistant:
    container_name: homeassistant
    image: ghcr.io/home-assistant/home-assistant:stable
    restart: unless-stopped
    network_mode: host
    volumes:
      - ./homeassistant:/config
      - /etc/localtime:/etc/localtime:ro

  mqtt:
    container_name: mqtt
    image: eclipse-mosquitto:latest
    restart: unless-stopped
    ports:
      - "1883:1883"
    volumes:
      - ./mosquitto/config:/mosquitto/config
      - ./mosquitto/data:/mosquitto/data
      - ./mosquitto/log:/mosquitto/log

  frigate:
    container_name: frigate
    image: ghcr.io/blakeblackshear/frigate:stable
    restart: unless-stopped
    privileged: true
    shm_size: "256mb"
    devices:
      - /dev/bus/usb:/dev/bus/usb       # Coral USB 쓸 때만
      - /dev/dri/renderD128:/dev/dri/renderD128  # Intel iGPU 하드웨어 가속
    volumes:
      - /etc/localtime:/etc/localtime:ro
      - ./frigate/config.yml:/config/config.yml
      - ./frigate/storage:/media/frigate
      - type: tmpfs
        target: /tmp/cache
        tmpfs:
          size: 1000000000
    ports:
      - "5000:5000"     # 내부 UI
      - "8971:8971"     # 외부 UI (인증 있음, 권장)
      - "8554:8554"     # RTSP 재스트림
    environment:
      FRIGATE_RTSP_PASSWORD: "${FRIGATE_RTSP_PASSWORD}"
```

### 5.3 .env 파일

```
FRIGATE_RTSP_PASSWORD=your_strong_password
```

### 5.4 Mosquitto 부트스트랩 (2단계)

**1단계** — `mosquitto/config/mosquitto.conf` (임시):

```
persistence true
persistence_location /mosquitto/data/
log_dest file /mosquitto/log/mosquitto.log
allow_anonymous true
listener 1883
```

`docker compose up -d` 실행 후 컨테이너 안에서:

```bash
mosquitto_passwd -c /mosquitto/config/passwd 사용자명
```

**2단계** — 인증 활성화:

```
allow_anonymous false
password_file /mosquitto/config/passwd
```

재시작.

### 5.5 Frigate config.yml

```yaml
mqtt:
  enabled: true
  host: mqtt
  port: 1883
  user: frigate_user
  password: your_mqtt_password

detectors:
  ov:
    type: openvino       # Intel iGPU 사용 시 (Coral 불필요)
    device: GPU

ffmpeg:
  hwaccel_args: preset-vaapi   # Intel/AMD GPU 가속

cameras:
  front_door:
    enabled: true
    ffmpeg:
      inputs:
        - path: rtsp://user:pass@192.168.0.10:554/stream1
          roles: [detect, record]
    detect:
      width: 1280
      height: 720
      fps: 5            # 탐지 5fps 권장
    objects:
      track:
        - person
        - car
        - dog
    record:
      enabled: true
      retain:
        days: 7
        mode: motion
      alerts:
        retain:
          days: 30
    snapshots:
      enabled: true
      retain:
        default: 10
    zones:
      driveway:
        coordinates: "0,720,640,720,640,360,0,360"
```

**성능 최적화 팁:**
- 탐지 해상도: 1280×720 (4K 금지 — AI 모델은 특정 해상도에서 최적)
- 탐지 fps: 5fps (사람 추적에 충분)
- 녹화 fps: 15fps (저장 공간 절약)
- CPU 사용량 60% 감소 효과

### 5.6 Home Assistant + Frigate 통합

1. HACS 설치 (`hacs.xyz/docs/installation/container`)
2. HACS → Integrations → "+ EXPLORE & ADD REPOSITORIES" → "Frigate" 설치
   - ⚠️ Docker 설치 시 HA 내장 통합이 아닌 **HACS 커스텀 통합** 사용 필수
3. Home Assistant 재시작
4. 설정 → 기기 및 서비스 → 통합 추가 → "Frigate" → URL: `http://172.17.0.1:5000`

### 5.7 LLM Vision + Gemini 연결

1. **Gemini API 키 발급**: `aistudio.google.com` → "Get API key"
2. HACS → "LLM Vision" 검색 → Download → HA 재시작
3. 설정 → 기기 및 서비스 → 통합 추가 → "LLM Vision"
4. Provider: **Google Gemini**, API 키 입력
5. **Event Calendar** 옵션 활성화 (캘린더 엔티티 생성, 나중에 질문 가능)

### 5.8 이벤트별 즉시 요약 자동화 (Blueprint)

설정 → 자동화 → Blueprints → "Import Blueprint" → URL 입력:

```
https://raw.githubusercontent.com/valentinfrlch/ha-llmvision/refs/heads/main/blueprints/event_summary.yaml
```

블루프린트 자동화 생성:
- Frigate 카메라 엔티티 선택
- Provider: Gemini
- Notification device: 휴대폰 (HA Companion 앱 사전 설치)
- Remember: ON (캘린더 저장)

---

## 6. 핵심 자동화 — 일일 요약

매일 아침 8시에 어제 하루를 한국어로 요약해서 알림:

```yaml
- alias: "일일 CCTV 요약 (매일 아침 8시)"
  trigger:
    - platform: time
      at: "08:00:00"
  action:
    # 1) 어제 캘린더 이벤트 가져오기
    - service: calendar.get_events
      target:
        entity_id: calendar.llm_vision_events
      data:
        start_date_time: "{{ (now() - timedelta(days=1)).replace(hour=0, minute=0, second=0).isoformat() }}"
        end_date_time: "{{ now().replace(hour=0, minute=0, second=0).isoformat() }}"
      response_variable: yesterday_events

    # 2) Gemini로 자연어 요약
    - service: llmvision.data_analyzer
      data:
        provider: "01HXXXXX..."   # LLM Vision provider 설정 ID
        model: "gemini-2.5-flash"
        message: >
          아래는 어제 우리 집 CCTV에서 감지된 이벤트 목록이야.
          시간 순으로 정리해서 한국어로 자연스러운 일일 요약을 만들어줘.
          중요한 일(택배, 방문자, 차량 움직임 등)은 강조하고,
          반복되는 사소한 움직임은 묶어서 정리해.

          이벤트:
          {% for event in yesterday_events['calendar.llm_vision_events'].events %}
          - {{ event.start }}: {{ event.summary }} — {{ event.description }}
          {% endfor %}
      response_variable: daily_summary

    # 3) 알림 전송
    - service: notify.mobile_app_your_phone
      data:
        title: "어제의 집 요약"
        message: "{{ daily_summary.response_text }}"
```

### 6.1 카테고리별 요약 (고급)

```yaml
message: >
  어제 CCTV 이벤트를 다음 카테고리로 분류해서 요약해줘:

  📦 택배/배달:
  🚪 방문자:
  🚗 차량 활동:
  🐕 반려동물/동물:
  ⚠️ 주의할 만한 일:

  각 카테고리에 해당 사항이 없으면 "없음"이라고 적고,
  마지막에 "오늘 확인이 필요한 일" 1~2가지를 짚어줘.
```

---

## 7. 운영 비용 및 트러블슈팅

### 7.1 월 운영 비용

| 항목 | 비용 |
|---|---|
| **전기료** | N100 미니PC 풀로드 25W × 24시간 = 월 2,000~3,000원 |
| **Gemini API** | Flash 모델 기준 일일 요약 + 이벤트 분석 = 무료 티어로 충분, 넘어가도 월 5,000~10,000원 |
| **클라우드 저장** | 0원 (로컬 저장) |
| **합계** | **월 3,000~13,000원** |

> 비교: Ring Protect Plus 같은 클라우드 CCTV는 카메라당 월 5,000~10,000원 → 4대만 써도 월 2~4만원

### 7.2 트러블슈팅

| 문제 | 해결법 |
|---|---|
| **MQTT 연결 실패** | `docker logs frigate`로 인증 로그 확인, 사용자명/비번 일치 확인 |
| **Frigate 통합이 안 보임** | HACS 커스텀 통합 사용 확인, Frigate v0.16+로 업데이트 |
| **객체 탐지 CPU 100%** | 탐지 fps↓, 해상도 1280×720, OpenVINO 가속 활성화 |
| **Gemini 응답이 한국어가 아님** | 프롬프트에 "반드시 한국어로 답해" 명시 |
| **이벤트가 캘린더에 안 쌓임** | LLM Vision 자동화 `remember: true`, Event Calendar 활성화 확인 |

---

## 8. 한국 환경 특수 고려사항

### 8.1 개인정보보호법

- 얼굴 인식 기능은 가족·본인 영상에만 사용
- 외부인이 찍히는 위치(현관 밖, 골목 등)에 "CCTV 작동 중" 안내문 부착 필수

### 8.2 공동주택 설치

- 복도·엘리베이터 같은 공용 공간 설치 시 입주자대표회의 결의 필요
- 본인 현관 안쪽, 발코니 안쪽은 사적 공간으로 자유 설치 가능

### 8.3 인터넷 회선

- Gemini API 호출은 인터넷 필요
- 단, **영상 자체는 로컬 저장**이라 인터넷 끊겨도 녹화는 계속 됨

---

## 9. 단계별 시작 추천 경로

비용 최소화 + 학습 단계로 진행:

1. **1주차 (10만원)**: Tapo C220 카메라 1대 + 기존 PC에 Docker로 Frigate 시험 운영
2. **2주차 (+25만원)**: 동작 검증 후 N100 미니PC 구입, 24시간 운영 전환
3. **3주차 (+15만원)**: 카메라 2~3대 추가, LLM Vision 일일 요약 자동화 완성
4. **이후 (선택)**: NAS, PoE 카메라, UPS 등 점진 확장

---

## 정리

- **카메라 2~4대 가정용 기준**: 초기 비용 40~60만원, 월 운영비 1만원 미만
- **클라우드 CCTV 대비 장점**: 프라이버시 보장, 구독료 없음, 자연어 요약 가능
- **핵심 조합**: Frigate(객체 감지) + LLM Vision(Gemini 분석) + Home Assistant(자동화)
- **2026년 변화**: Coral TPU 더 이상 필수 아님 → Intel N100 + OpenVINO만으로 충분

---

## 참고 링크

- Frigate 공식: https://frigate.video/
- Frigate 문서: https://docs.frigate.video/
- LLM Vision: https://llmvision.org/
- LLM Vision GitHub: https://github.com/valentinfrlch/ha-llmvision
- Home Assistant: https://www.home-assistant.io/
- Google AI Studio (Gemini API): https://aistudio.google.com/
- DeepCamera (대안): https://github.com/SharpAI/DeepCamera
