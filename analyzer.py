"""
CCTV 일일 분석 메인 파이프라인 (비용 최적화 버전)

흐름:
1. 어제 날짜 영상 파일 탐색
2. OpenCV로 움직임 구간만 추출 (대부분의 영상은 여기서 걸러짐)
3. 인접한 움직임 구간을 하나의 사건으로 병합 (중복 제거 1단계)
4. 각 구간을 ffmpeg로 다운샘플
5. Gemini Flash-Lite로 분석 (모호하면 Flash 폴백)
6. 분석 결과 기반 추가 중복 제거 (2단계)
7. 일일 요약 생성 → 텔레그램/이메일 발송
8. 비용 리포트 출력
"""

import os
import sys
import json
import sqlite3
import logging
import shutil
from pathlib import Path
from datetime import datetime, timedelta, date
from typing import List, Dict, Optional
from dataclasses import dataclass, field

import yaml

from motion_detector import MotionDetector, extract_clip, MotionSegment
from gemini_client import GeminiClient

# ===== 설정 =====
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.yaml"

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

log_dir = SCRIPT_DIR / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / f"analyzer_{date.today()}.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

DB_PATH = SCRIPT_DIR / "events.db"


# ===== 클립 데이터 구조 =====
@dataclass
class Clip:
    """분석 대상 클립. 여러 원본 파일에 걸쳐 있을 수 있음."""
    camera: str
    source_files: List[str] = field(default_factory=list)
    # 절대 시간 (datetime) - 카메라 간 비교의 기준
    absolute_start: Optional[datetime] = None
    absolute_end: Optional[datetime] = None
    # 첫 번째 source_file 내에서의 상대 시간
    relative_start_sec: float = 0.0
    relative_end_sec: float = 0.0
    peak_intensity: float = 0.0

    @property
    def duration(self) -> float:
        if self.absolute_start and self.absolute_end:
            return (self.absolute_end - self.absolute_start).total_seconds()
        return self.relative_end_sec - self.relative_start_sec


# ===== DB =====
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS clips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            camera TEXT NOT NULL,
            absolute_start TEXT,
            absolute_end TEXT,
            duration_sec REAL,
            has_event INTEGER,
            confidence TEXT,
            description TEXT,
            events_json TEXT,
            tokens_used INTEGER,
            source_files TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_summaries (
            date TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            event_count INTEGER,
            cost_krw INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_clips_date ON clips(date)")
    conn.commit()
    conn.close()


def save_clip_result(date_str: str, clip: Clip, result: Dict):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO clips
        (date, camera, absolute_start, absolute_end, duration_sec,
         has_event, confidence, description, events_json,
         tokens_used, source_files)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        date_str,
        clip.camera,
        clip.absolute_start.isoformat() if clip.absolute_start else None,
        clip.absolute_end.isoformat() if clip.absolute_end else None,
        clip.duration,
        1 if result.get("has_meaningful_event") else 0,
        result.get("confidence", "low"),
        result.get("summary", ""),
        json.dumps(result.get("events", []), ensure_ascii=False),
        result.get("_tokens", 0),
        json.dumps(clip.source_files, ensure_ascii=False),
    ))
    conn.commit()
    conn.close()


def save_daily_summary(date_str: str, summary: str, count: int, cost_krw: int):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO daily_summaries
        (date, summary, event_count, cost_krw)
        VALUES (?, ?, ?, ?)
    """, (date_str, summary, count, cost_krw))
    conn.commit()
    conn.close()


# ===== 영상 파일 탐색 =====
def find_videos_for_date(target_date: date) -> List[Dict]:
    """카메라별 어제 날짜 영상 파일 목록 반환."""
    videos = []
    for cam in CONFIG["cameras"]:
        cam_name = cam["name"]
        base_path = Path(cam["path"])
        patterns = cam.get("file_patterns", ["*.mp4"])

        if not base_path.exists():
            log.warning(f"카메라 폴더 없음: {base_path}")
            continue

        seen = set()
        for pattern in patterns:
            resolved = target_date.strftime(pattern)
            for fp in base_path.rglob(resolved):
                if fp in seen:
                    continue
                seen.add(fp)
                mtime = datetime.fromtimestamp(fp.stat().st_mtime)
                # 어제 또는 오늘 자정 직후 파일까지 포함
                if mtime.date() == target_date or (
                    mtime.date() == target_date + timedelta(days=1)
                    and mtime.hour < 1
                ):
                    videos.append({
                        "camera": cam_name,
                        "path": str(fp),
                        "size_mb": fp.stat().st_size / 1024 / 1024,
                        "mtime": mtime,
                        "filename_time": parse_time_from_filename(
                            fp.name, target_date
                        ),
                    })

    # 카메라별 시간순 정렬
    videos.sort(key=lambda v: (v["camera"], v["filename_time"] or v["mtime"]))
    log.info(f"{target_date} 영상 {len(videos)}개 발견")
    return videos


def parse_time_from_filename(filename: str, target_date: date) -> Optional[datetime]:
    """
    파일명에서 시작 시각을 추출.
    지원 패턴:
      2026-05-10_14-00-00.mp4
      2026-05-10 14-00-00.mp4
      20260510_140000.mp4
      14-00-00.mp4 (하위 폴더가 날짜인 경우)
    """
    import re
    name = filename.rsplit(".", 1)[0]

    # 풀 datetime 패턴
    patterns = [
        (r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})[_\s-]+(\d{2})[-_:]?(\d{2})[-_:]?(\d{2})",
         lambda m: datetime(*map(int, m.groups()))),
        # 시간만 (날짜는 target_date)
        (r"(\d{2})[-_:]?(\d{2})[-_:]?(\d{2})$",
         lambda m: datetime.combine(target_date,
                                    datetime.strptime(
                                        ":".join(m.groups()), "%H:%M:%S"
                                    ).time())),
    ]
    for pat, ctor in patterns:
        m = re.search(pat, name)
        if m:
            try:
                return ctor(m)
            except (ValueError, TypeError):
                continue
    return None


# ===== 움직임 감지 + 클립 생성 =====
def detect_motion_clips(videos: List[Dict], detector: MotionDetector) -> List[Clip]:
    """각 영상 파일에서 움직임 구간을 찾아 Clip 객체로 변환."""
    clips = []
    for v in videos:
        log.info(f"[{v['camera']}] {Path(v['path']).name} ({v['size_mb']:.1f}MB)")

        # 절대 시간 기준 설정
        base_time = v["filename_time"] or v["mtime"]

        segments = detector.detect(v["path"])
        for seg in segments:
            clip = Clip(
                camera=v["camera"],
                source_files=[v["path"]],
                absolute_start=base_time + timedelta(seconds=seg.start_sec),
                absolute_end=base_time + timedelta(seconds=seg.end_sec),
                relative_start_sec=seg.start_sec,
                relative_end_sec=seg.end_sec,
                peak_intensity=seg.peak_intensity,
            )
            clips.append(clip)

    return clips


# ===== 중복 제거 1단계: 인접 클립 병합 =====
def merge_adjacent_clips(clips: List[Clip], max_gap_sec: float = 15.0) -> List[Clip]:
    """
    같은 카메라에서 max_gap_sec 이내 간격의 클립들은 하나의 사건으로 병합.

    파일 경계에 걸친 사건 + NVR이 잘게 쪼갠 연속 이벤트를 해결.
    카메라가 다르면 절대 병합하지 않음 (각 시점은 별개 정보).
    """
    if not clips:
        return []

    # 카메라별 → 시간순 정렬
    clips = sorted(clips, key=lambda c: (c.camera, c.absolute_start or datetime.min))

    merged = [clips[0]]
    for clip in clips[1:]:
        last = merged[-1]

        # 같은 카메라 + 시간 가까움 조건
        same_camera = last.camera == clip.camera
        gap = (clip.absolute_start - last.absolute_end).total_seconds() \
            if last.absolute_end and clip.absolute_start else float("inf")

        if same_camera and gap <= max_gap_sec:
            # 병합
            last.absolute_end = clip.absolute_end
            for sf in clip.source_files:
                if sf not in last.source_files:
                    last.source_files.append(sf)
            last.peak_intensity = max(last.peak_intensity, clip.peak_intensity)
        else:
            merged.append(clip)

    log.info(f"인접 클립 병합: {len(clips)}개 → {len(merged)}개")
    return merged


# ===== 중복 제거 2단계: 분석 결과 기반 =====
def deduplicate_results(
    items: List[Dict], time_window_sec: float = 90.0
) -> List[Dict]:
    """
    분석 결과 기반 후처리 중복 제거.

    조건 (모두 만족하면 중복으로 간주):
    - 같은 카메라
    - time_window_sec 이내 시간
    - 같은 카테고리
    - description 첫 부분이 매우 유사

    중복이면 첫 번째 것만 남기고 나머지는 "(외 N건)"으로 표시.
    """
    if not items:
        return items

    items = sorted(items, key=lambda x: (x["camera"], x["absolute_start"]))

    result = []
    skip_idx = set()

    for i, item in enumerate(items):
        if i in skip_idx:
            continue

        # 비슷한 후속 항목 찾기
        duplicate_count = 0
        for j in range(i + 1, len(items)):
            if j in skip_idx:
                continue
            other = items[j]
            if other["camera"] != item["camera"]:
                break  # 정렬돼있으므로 카메라 바뀌면 종료

            time_gap = (other["absolute_start"] - item["absolute_start"]).total_seconds()
            if time_gap > time_window_sec:
                break

            # description 유사도 (간단히 앞 15자 비교)
            same_category = (
                other.get("category") == item.get("category")
                and item.get("category")
            )
            desc_a = (item.get("description") or "")[:15]
            desc_b = (other.get("description") or "")[:15]

            if same_category and desc_a == desc_b:
                skip_idx.add(j)
                duplicate_count += 1

        if duplicate_count > 0:
            item = dict(item)
            item["description"] += f" (외 {duplicate_count}건)"

        result.append(item)

    if len(result) < len(items):
        log.info(f"결과 중복 제거: {len(items)}개 → {len(result)}개")
    return result


# ===== 메인 처리 =====
def process_clip(
    clip: Clip,
    gemini: GeminiClient,
    work_dir: Path,
    clip_idx: int,
) -> Optional[Dict]:
    """단일 클립을 ffmpeg 추출 → Gemini 분석."""
    duration = clip.duration
    if duration < 1.0:
        return None

    # 너무 긴 클립은 잘라냄 (Gemini API 토큰 절약)
    max_clip_duration = CONFIG.get("analysis", {}).get("max_clip_duration_sec", 300)
    effective_duration = min(duration, max_clip_duration)
    if duration > max_clip_duration:
        log.info(f"  긴 클립 ({duration:.0f}초) → {max_clip_duration}초로 단축")

    # 다운샘플 추출 (첫 번째 source file 기준)
    out_path = work_dir / f"clip_{clip_idx:04d}.mp4"

    success = extract_clip(
        input_path=clip.source_files[0],
        output_path=str(out_path),
        start_sec=clip.relative_start_sec,
        end_sec=clip.relative_start_sec + effective_duration,
        scale="640:360",
        fps=2,
    )
    if not success:
        return None

    # Gemini 분석
    result = gemini.analyze_clip(str(out_path))

    # 다운샘플 파일 삭제 (디스크 절약)
    try:
        out_path.unlink()
    except Exception:
        pass

    return result


def format_events_for_summary(items: List[Dict]) -> str:
    """일일 요약용 이벤트 텍스트 생성."""
    lines = []
    for item in items:
        time_str = item["absolute_start"].strftime("%H:%M")
        lines.append(
            f"- {time_str} [{item['camera']}] "
            f"{item.get('category', '기타')}: {item['description']}"
        )
    return "\n".join(lines)


def run(target_date: Optional[date] = None):
    init_db()

    if target_date is None:
        target_date = date.today() - timedelta(days=1)
    date_str = target_date.isoformat()

    log.info(f"========== {date_str} 분석 시작 ==========")

    # 1. 영상 파일 찾기
    videos = find_videos_for_date(target_date)
    if not videos:
        log.warning("분석할 영상 없음")
        send_notification(f"📅 {date_str}\n\n어제 녹화된 영상이 없습니다.")
        return

    # 2. 움직임 감지
    detector_cfg = CONFIG.get("motion", {})
    detector = MotionDetector(
        min_area_ratio=detector_cfg.get("min_area_ratio", 0.005),
        min_duration=detector_cfg.get("min_duration", 2.0),
        merge_gap=detector_cfg.get("merge_gap", 10.0),
        sample_fps=detector_cfg.get("sample_fps", 2.0),
    )

    log.info("===== [1단계] 움직임 감지 =====")
    raw_clips = detect_motion_clips(videos, detector)
    log.info(f"움직임 구간: {len(raw_clips)}개")

    if not raw_clips:
        summary = f"📅 {date_str}\n\n어제는 특별한 움직임이 감지되지 않았습니다."
        save_daily_summary(date_str, summary, 0, 0)
        send_notification(summary)
        return

    # 3. 중복 제거 1단계: 인접 클립 병합
    log.info("===== [2단계] 인접 클립 병합 =====")
    clips = merge_adjacent_clips(
        raw_clips,
        max_gap_sec=detector_cfg.get("merge_across_files_gap", 15.0),
    )

    # 4. Gemini 분석
    log.info(f"===== [3단계] Gemini 분석 ({len(clips)}개 클립) =====")
    gemini = GeminiClient(
        api_key=CONFIG["gemini"]["api_key"],
        primary_model=CONFIG["gemini"].get(
            "primary_model", "models/gemini-2.5-flash-lite"
        ),
        fallback_model=CONFIG["gemini"].get(
            "fallback_model", "models/gemini-2.5-flash"
        ),
        analysis_fps=CONFIG["gemini"].get("analysis_fps", 0.5),
        use_fallback=CONFIG["gemini"].get("use_fallback", True),
    )

    work_dir = SCRIPT_DIR / "work" / date_str
    work_dir.mkdir(parents=True, exist_ok=True)

    analyzed_items = []  # 중복 제거 2단계 입력용 평탄화 리스트
    for idx, clip in enumerate(clips, 1):
        log.info(
            f"[{idx}/{len(clips)}] {clip.camera} "
            f"{clip.absolute_start.strftime('%H:%M:%S')} "
            f"({clip.duration:.0f}초)"
        )

        result = process_clip(clip, gemini, work_dir, idx)
        if result is None:
            continue

        save_clip_result(date_str, clip, result)

        # 의미 있는 이벤트만 요약에 포함
        if not result.get("has_meaningful_event"):
            log.info(f"  → 의미 없는 움직임, 스킵")
            continue

        events = result.get("events", [])
        if not events:
            # events 배열이 비어있지만 summary가 있는 경우
            analyzed_items.append({
                "camera": clip.camera,
                "absolute_start": clip.absolute_start,
                "category": "기타",
                "description": result.get("summary", ""),
            })
        else:
            for ev in events:
                # 클립 내 상대 시간 + 클립 시작 시간 = 절대 시간
                try:
                    m, s = map(int, ev["time"].split(":"))
                    ev_abs = clip.absolute_start + timedelta(minutes=m, seconds=s)
                except Exception:
                    ev_abs = clip.absolute_start

                analyzed_items.append({
                    "camera": clip.camera,
                    "absolute_start": ev_abs,
                    "category": ev.get("category", "기타"),
                    "description": ev.get("description", ""),
                })

    # 5. 중복 제거 2단계: 분석 결과 기반
    log.info("===== [4단계] 결과 기반 중복 제거 =====")
    deduplicated = deduplicate_results(
        analyzed_items,
        time_window_sec=CONFIG.get("analysis", {}).get("dedup_window_sec", 90.0),
    )

    # 6. 일일 요약 생성
    log.info("===== [5단계] 일일 종합 요약 =====")
    if deduplicated:
        events_text = format_events_for_summary(deduplicated)
        summary = gemini.generate_daily_summary(date_str, events_text)
        if not summary:
            summary = f"📅 {date_str} CCTV 일일 요약\n\n{events_text}"
    else:
        summary = f"📅 {date_str}\n\n주목할 만한 이벤트가 없었습니다."

    # 7. 비용 리포트
    cost = gemini.get_cost_estimate()
    cost_report = (
        f"\n\n---\n"
        f"💰 분석 비용: 약 {cost['estimated_cost_krw']}원 "
        f"(API 호출 {cost['api_calls']}회, 폴백 {cost['fallback_calls']}회)"
    )

    final_message = summary + cost_report
    log.info(final_message)

    save_daily_summary(
        date_str, summary, len(deduplicated), cost["estimated_cost_krw"]
    )

    # 8. 알림 발송
    send_notification(final_message)

    # 9. 작업 폴더 정리
    try:
        shutil.rmtree(work_dir)
    except Exception:
        pass

    log.info(f"========== {date_str} 분석 종료 ==========")


# ===== 알림 =====
def send_notification(message: str):
    """텔레그램 + 이메일 발송"""
    if CONFIG.get("telegram", {}).get("enabled"):
        send_telegram(message)
    if CONFIG.get("email", {}).get("enabled"):
        send_email(f"[CCTV] 일일 요약", message)


def send_telegram(message: str):
    import requests
    cfg = CONFIG["telegram"]
    url = f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage"
    # 텔레그램 4096자 제한 대응
    for chunk in [message[i:i+4000] for i in range(0, len(message), 4000)]:
        try:
            r = requests.post(url, json={
                "chat_id": cfg["chat_id"],
                "text": chunk,
            }, timeout=15)
            r.raise_for_status()
        except Exception as e:
            log.error(f"텔레그램 전송 실패: {e}")


def send_email(subject: str, body: str):
    import smtplib
    from email.mime.text import MIMEText
    cfg = CONFIG["email"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    try:
        with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.login(cfg["smtp_user"], cfg["smtp_password"])
            s.send_message(msg)
    except Exception as e:
        log.error(f"이메일 발송 실패: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
        run(target)
    else:
        run()
