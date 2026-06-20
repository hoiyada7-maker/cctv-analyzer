"""CCTV 일일 자동 분석 — 단일 진입점.

녹화 → 움직임 감지 → 클립 추출 → Gemini 분석 → 일일 요약 → 텔레그램 전송
까지 한 번에 처리한다. 스케줄러는 이 파일 하나만 실행하면 된다.

화면 타임스탬프를 Gemini로 읽지 않고, 녹화 시작 시각(파일명 14자리) + 구간
오프셋으로 시각을 계산한다 → API 호출을 클립당 1회로 줄여 비용·속도 개선.
"""
import re
import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta

import yaml

sys.path.insert(0, str(Path(__file__).parent))
logging.basicConfig(level=logging.INFO, format="%(message)s")

from motion_detector import MotionDetector, extract_clip, MotionSegment
from gemini_client import GeminiClient
from record_rtsp import record
import telegram_notifier

BASE = Path(__file__).parent


def parse_base_time(name: str) -> datetime:
    """파일명의 14자리(YYYYMMDDHHMMSS)를 녹화 시작 시각으로 파싱."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", name)
    return datetime(*map(int, m.groups())) if m else datetime.now()


def clock(base: datetime, offset_sec: float) -> str:
    """녹화 시작 시각 + 오프셋 → HH:MM:SS."""
    return (base + timedelta(seconds=offset_sec)).strftime("%H:%M:%S")


def main() -> int:
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    tg = cfg["telegram"]

    # ===== 1. 녹화 =====
    video = record(cfg["rtsp"])
    if not video:
        telegram_notifier.send(tg, "⚠️ CCTV 녹화 실패 — 카메라 연결을 확인하세요.")
        return 1

    base_time = parse_base_time(video.name)
    date_str = base_time.strftime("%Y-%m-%d")
    work_dir = BASE / "work" / "single"
    work_dir.mkdir(parents=True, exist_ok=True)

    # ===== 2. 움직임 감지 =====
    m = cfg.get("motion", {})
    detector = MotionDetector(
        min_area_ratio=m.get("min_area_ratio", 0.005),
        min_duration=m.get("min_duration", 2.0),
        merge_gap=m.get("merge_gap", 10.0),
        sample_fps=m.get("sample_fps", 2.0),
    )
    segments = detector.detect(str(video))
    print(f"\n움직임 구간: {len(segments)}개")
    if not segments:
        telegram_notifier.send(
            tg, f"📹 CCTV 분석 결과 ({date_str})\n\n움직임이 감지되지 않았습니다."
        )
        return 0

    # ===== 3. 인접 클립 병합 (15초 이내) =====
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
    print(f"병합: {len(segments)}개 → {len(merged)}개")

    # ===== 4. 클립 추출 + Gemini 분석 (화면 시간 읽기 생략) =====
    g = cfg["gemini"]
    gemini = GeminiClient(
        api_key=g["api_key"],
        primary_model=g.get("primary_model", "models/gemini-2.5-flash-lite"),
        fallback_model=g.get("fallback_model", "models/gemini-2.5-flash"),
        analysis_fps=g.get("analysis_fps", 0.5),
        use_fallback=g.get("use_fallback", True),
    )

    clip_results = []   # (idx, ts_start, ts_end, summary)
    all_events = []
    for idx, seg in enumerate(merged, 1):
        ts_start = clock(base_time, seg.start_sec)
        ts_end = clock(base_time, seg.end_sec)
        print(f"\n[{idx}/{len(merged)}] {ts_start}~{ts_end} ({seg.duration:.0f}초)")

        clip_path = str(work_dir / f"clip_{idx:03d}.mp4")
        if not extract_clip(str(video), clip_path, seg.start_sec, seg.end_sec,
                            scale="640:360", fps=2):
            clip_results.append((idx, ts_start, ts_end, "(ffmpeg 실패)"))
            continue

        result = gemini.analyze_clip(clip_path)
        try:
            Path(clip_path).unlink()
        except Exception:
            pass

        if not result:
            clip_results.append((idx, ts_start, ts_end, "(분석 실패)"))
            continue

        summary_text = result.get("summary", "")
        print(f"  {result.get('confidence')} / {summary_text}")
        clip_results.append((idx, ts_start, ts_end, summary_text))

        if not result.get("has_meaningful_event"):
            continue
        for ev in result.get("events", []):
            try:
                mm, ss = map(int, ev["time"].split(":"))
                abs_t = base_time + timedelta(seconds=seg.start_sec) + timedelta(minutes=mm, seconds=ss)
            except Exception:
                abs_t = base_time + timedelta(seconds=seg.start_sec)
            all_events.append({
                "time_str": abs_t.strftime("%H:%M:%S"),
                "clip_start": ts_start[:5],
                "clip_end": ts_end[:5],
                "category": ev.get("category", "기타"),
                "description": ev.get("description", ""),
                "_abs": abs_t,
            })

    # ===== 5. 결과 중복 제거 =====
    window = cfg.get("analysis", {}).get("dedup_window_sec", 90.0)
    deduped = []
    skip = set()
    for i, ev in enumerate(all_events):
        if i in skip:
            continue
        dup = 0
        for j in range(i + 1, len(all_events)):
            if j in skip:
                continue
            if (all_events[j]["_abs"] - ev["_abs"]).total_seconds() > window:
                break
            if (all_events[j]["category"] == ev["category"]
                    and all_events[j]["description"][:15] == ev["description"][:15]):
                skip.add(j)
                dup += 1
        item = dict(ev)
        if dup:
            item["description"] += f" (외 {dup}건)"
        deduped.append(item)

    # ===== 6. 시간별 요약 (Gemini) =====
    events_text = "\n".join(
        f"- {e['time_str']} [{e['category']}]: {e['description']}" for e in deduped
    )
    daily_summary = gemini.generate_daily_summary(date_str, events_text)

    # ===== 7. 리포트 조립 =====
    header = f"📹 CCTV 분석 결과 ({date_str})\n\n"

    # 메시지 1: 클립별 요약
    clip_lines = ["클립별 요약\n"]
    for idx, ts_s, ts_e, summ in clip_results:
        clip_lines.append(f"{idx:<4} │ {ts_s}~{ts_e} │ {summ}")
    msg1 = header + "\n".join(clip_lines)

    # 종합: 로컬 계산 — 카테고리별 발생 시간 목록
    cat_times: dict = {}
    for ev in deduped:
        cat_times.setdefault(ev["category"], []).append(
            f"{ev['clip_start']}~{ev['clip_end']}"
        )
    category_summary = "\n".join(
        f"{cat}: {', '.join(times)}" for cat, times in cat_times.items()
    )

    # 메시지 2: 시간별 요약 + 종합 + 비용
    cost = gemini.get_cost_estimate()
    cost_line = (
        f"\n\n💰 분석 비용: 약 {cost['estimated_cost_krw']}원 "
        f"(API {cost['api_calls']}회, 폴백 {cost['fallback_calls']}회)"
    )
    msg2 = daily_summary
    if category_summary:
        msg2 += "\n\n📋 종합\n" + category_summary
    msg2 += cost_line

    report = msg1 + "\n\n" + msg2
    print("\n" + "=" * 60 + "\n" + report)

    # 리포트 저장
    reports_dir = BASE / "reports"
    reports_dir.mkdir(exist_ok=True)
    (reports_dir / f"report_{datetime.now():%Y%m%d_%H%M%S}.txt").write_text(
        report, encoding="utf-8")

    # ===== 8. 텔레그램 전송 (2건) =====
    ok1 = telegram_notifier.send(tg, msg1)
    ok2 = telegram_notifier.send(tg, msg2)
    if ok1 and ok2:
        print("\n📱 텔레그램 전송 완료 (2건)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
