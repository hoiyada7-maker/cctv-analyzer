"""단일 영상 전체 파이프라인: 움직임 감지 + 병합 + Gemini Flash-Lite + 중복 제거 + 요약"""
import re
import sys
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.INFO, format="%(message)s")

import cv2
import yaml
from google.genai import types as gtypes

from motion_detector import MotionDetector, extract_clip, MotionSegment
from gemini_client import GeminiClient

VIDEO = sys.argv[1] if len(sys.argv) > 1 else "KakaoTalk_20260601_155532341.mp4"
_cfg = yaml.safe_load((Path(__file__).parent / "config.yaml").read_text(encoding="utf-8"))
API_KEY = _cfg["gemini"]["api_key"]
WORK_DIR = Path("work/single")
WORK_DIR.mkdir(parents=True, exist_ok=True)

# 파일명에서 날짜/시간 추출 (예: 20260424134400)
def parse_base_time(path: str) -> datetime:
    m = re.search(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", Path(path).name)
    if m:
        return datetime(*map(int, m.groups()))
    return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

BASE_TIME = parse_base_time(VIDEO)
DATE_STR = BASE_TIME.strftime("%Y-%m-%d")


def get_onscreen_time(video_path: str, time_sec: float, client) -> str:
    """프레임 상단 CCTV 타임스탬프를 Gemini로 읽기. 실패 시 MM:SS 반환."""
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(time_sec * fps))
    ret, frame = cap.read()
    cap.release()

    if not ret:
        m, s = divmod(int(time_sec), 60)
        return f"{m:02d}:{s:02d}"

    h = frame.shape[0]
    top = frame[: max(80, h // 8), :]
    _, buf = cv2.imencode(".jpg", top, [cv2.IMWRITE_JPEG_QUALITY, 95])

    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model="models/gemini-2.5-flash-lite",
                contents=gtypes.Content(parts=[
                    gtypes.Part(inline_data=gtypes.Blob(
                        mime_type="image/jpeg", data=buf.tobytes()
                    )),
                    gtypes.Part(text=(
                        "이 CCTV 화면 상단에 표시된 시간을 읽어줘. "
                        "HH:MM:SS 형식으로만 출력. 설명 없이."
                    )),
                ]),
            )
            text = response.text.strip()
            m = re.search(r"\d{2}:\d{2}:\d{2}", text)
            return m.group() if m else text
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                wait = 40 * (attempt + 1)
                print(f"  [rate limit] {wait}초 대기 후 재시도...")
                time.sleep(wait)
            else:
                break

    m, s = divmod(int(time_sec), 60)
    return f"{m:02d}:{s:02d}"


def add_duration(ts: str, seconds: float) -> str:
    """HH:MM:SS에 초를 더한 HH:MM:SS 반환."""
    try:
        h, m, s = map(int, ts.split(":"))
        total = h * 3600 + m * 60 + s + int(seconds)
        return f"{total//3600:02d}:{(total%3600)//60:02d}:{total%60:02d}"
    except Exception:
        return ts


# ===== 1단계: 움직임 감지 =====
print("\n===== [1단계] 움직임 감지 =====")
detector = MotionDetector(
    min_area_ratio=0.005,
    min_duration=2.0,
    merge_gap=10.0,
    sample_fps=2.0,
)
segments = detector.detect(VIDEO)
print(f"움직임 구간: {len(segments)}개")
for i, s in enumerate(segments, 1):
    s_m, s_s = divmod(int(s.start_sec), 60)
    e_m, e_s = divmod(int(s.end_sec), 60)
    print(f"  {i}. {s_m:02d}:{s_s:02d}~{e_m:02d}:{e_s:02d} ({s.duration:.0f}초, 강도 {s.peak_intensity*100:.1f}%)")

if not segments:
    print("움직임이 감지되지 않았습니다.")
    sys.exit(0)

# ===== 2단계: 인접 클립 병합 (15초 이내) =====
print("\n===== [2단계] 인접 클립 병합 =====")
merged = [segments[0]]
for seg in segments[1:]:
    last = merged[-1]
    if seg.start_sec - last.end_sec <= 15.0:
        merged[-1] = MotionSegment(
            start_sec=last.start_sec,
            end_sec=seg.end_sec,
            peak_intensity=max(last.peak_intensity, seg.peak_intensity),
            avg_intensity=(last.avg_intensity + seg.avg_intensity) / 2,
        )
    else:
        merged.append(seg)
print(f"{len(segments)}개 → {len(merged)}개")

# ===== 3단계: 화면 타임스탬프 읽기 (시작만 읽고 끝은 계산) =====
print("\n===== [3단계] 화면 타임스탬프 읽기 =====")
gemini = GeminiClient(api_key=API_KEY, analysis_fps=0.5, use_fallback=True)

clip_times = []
for i, seg in enumerate(merged, 1):
    ts_start = get_onscreen_time(VIDEO, seg.start_sec, gemini.client)
    ts_end = add_duration(ts_start, seg.duration)
    clip_times.append((ts_start, ts_end))
    print(f"  클립 {i}: {ts_start} ~ {ts_end}")

# ===== 4단계: ffmpeg 다운샘플 + Gemini 분석 =====
print(f"\n===== [4단계] Gemini 분석 ({len(merged)}개 클립) =====")
clip_results = []  # (idx, ts_start, ts_end, summary)
all_events = []

for idx, (seg, (ts_start, ts_end)) in enumerate(zip(merged, clip_times), 1):
    s_m, s_s = divmod(int(seg.start_sec), 60)
    e_m, e_s = divmod(int(seg.end_sec), 60)
    print(f"\n[{idx}/{len(merged)}] {s_m:02d}:{s_s:02d}~{e_m:02d}:{e_s:02d} ({seg.duration:.0f}초)")

    clip_path = str(WORK_DIR / f"clip_{idx:03d}.mp4")
    ok = extract_clip(VIDEO, clip_path, seg.start_sec, seg.end_sec, scale="640:360", fps=2)
    if not ok:
        print("  ffmpeg 실패, 스킵")
        clip_results.append((idx, ts_start, ts_end, "(ffmpeg 실패)"))
        continue

    result = gemini.analyze_clip(clip_path)
    try:
        Path(clip_path).unlink()
    except Exception:
        pass

    if not result:
        print("  Gemini 분석 실패")
        clip_results.append((idx, ts_start, ts_end, "(분석 실패)"))
        continue

    summary_text = result.get("summary", "")
    has_event = result.get("has_meaningful_event")
    print(f"  confidence={result.get('confidence')}, has_event={has_event}")
    print(f"  요약: {summary_text}")

    clip_results.append((idx, ts_start, ts_end, summary_text))

    if not has_event:
        continue

    for ev in result.get("events", []):
        try:
            m, s = map(int, ev["time"].split(":"))
            abs_t = (BASE_TIME + timedelta(seconds=seg.start_sec)
                     + timedelta(minutes=m, seconds=s))
        except Exception:
            abs_t = BASE_TIME + timedelta(seconds=seg.start_sec)
        all_events.append({
            "time_str": abs_t.strftime("%H:%M:%S"),
            "category": ev.get("category", "기타"),
            "description": ev.get("description", ""),
            "_abs": abs_t,
        })

# ===== 5단계: 결과 중복 제거 =====
print(f"\n===== [5단계] 결과 중복 제거 ({len(all_events)}개) =====")
deduped = []
skip = set()
for i, ev in enumerate(all_events):
    if i in skip:
        continue
    dup = 0
    for j in range(i + 1, len(all_events)):
        if j in skip:
            continue
        gap = (all_events[j]["_abs"] - ev["_abs"]).total_seconds()
        if gap > 90:
            break
        if (all_events[j]["category"] == ev["category"]
                and all_events[j]["description"][:15] == ev["description"][:15]):
            skip.add(j)
            dup += 1
    item = dict(ev)
    if dup:
        item["description"] += f" (외 {dup}건)"
    deduped.append(item)
print(f"→ {len(deduped)}개")

# ===== 6단계: 일일 요약 =====
print("\n===== [6단계] 일일 요약 =====")
events_text = "\n".join(
    f"- {e['time_str']} [{e['category']}]: {e['description']}"
    for e in deduped
)
daily_summary = gemini.generate_daily_summary(DATE_STR, events_text)

# ===== 최종 출력 =====
print("\n" + "=" * 60)
print("클립별 요약\n")
for idx, ts_start, ts_end, summary in clip_results:
    print(f"{idx:<4} │ {ts_start}~{ts_end} │ {summary}")

print()
print(daily_summary)

cost = gemini.get_cost_estimate()
print(f"\n💰 분석 비용: 약 {cost['estimated_cost_krw']}원 "
      f"(API {cost['api_calls']}회, 폴백 {cost['fallback_calls']}회)")
