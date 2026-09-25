"""CCTV 일일 자동 분석 — 단일 진입점.

녹화 → 움직임 감지 → 클립 추출 → Gemini 분석 → 일일 요약 → 텔레그램 전송
까지 한 번에 처리한다. 스케줄러는 이 파일 하나만 실행하면 된다.

화면 타임스탬프를 Gemini로 읽지 않고, 녹화 시작 시각(파일명 14자리) + 구간
오프셋으로 시각을 계산한다 → API 호출을 클립당 1회로 줄여 비용·속도 개선.
"""
import os
import re
import sys
import shutil
import logging
from pathlib import Path
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import yaml

sys.path.insert(0, str(Path(__file__).parent))

BASE     = Path(__file__).parent
_log_dir = BASE / "logs"
_log_dir.mkdir(exist_ok=True)
_run_log = _log_dir / "run_history.log"
_run_ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
LOCK_FILE = BASE / "work" / "running.lock"

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_dir / f"run_{_run_ts}.log", encoding="utf-8"),
    ],
)


def _stage_log(stage: str) -> logging.Logger:
    """스테이지별 로거 — 루트 로거(콘솔+전체 로그)에도 동시 전파."""
    lg = logging.getLogger(stage)
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    lg.propagate = True
    fh = logging.FileHandler(
        _log_dir / f"{_run_ts}_{stage}.log", encoding="utf-8"
    )
    fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
    lg.addHandler(fh)
    return lg


def _pid_alive(pid: int) -> bool:
    """Windows에서 PID가 살아 있는지 확인."""
    import ctypes
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def acquire_lock() -> bool:
    """O_CREAT|O_EXCL 원자적 lock 획득. 프로세스 사망 시 stale lock 자동 제거."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            lines = LOCK_FILE.read_text(encoding="utf-8").strip().splitlines()
            pid = int(next(l.split("=")[1] for l in lines if l.startswith("PID=")))
            if not _pid_alive(pid):
                logging.warning(f"[LOCK] stale lock 감지 (PID={pid} 종료됨) — 제거 후 진행")
                LOCK_FILE.unlink()
            else:
                logging.warning(f"[LOCK] 다른 인스턴스 실행 중 (PID={pid}) — 종료합니다.")
                return False
        except Exception:
            LOCK_FILE.unlink(missing_ok=True)

    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\nPID={os.getpid()}")
        return True
    except FileExistsError:
        logging.warning("[LOCK] 동시 시작으로 lock 경쟁 — 종료합니다.")
        return False


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except Exception:
        pass


from motion_detector import MotionDetector, extract_clip, MotionSegment
from gemini_client import GeminiClient
from record_rtsp import record
import telegram_notifier


def parse_base_time(name: str) -> datetime:
    """파일명의 14자리(YYYYMMDDHHMMSS)를 녹화 시작 시각으로 파싱."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", name)
    return datetime(*map(int, m.groups())) if m else datetime.now()


def clock(base: datetime, offset_sec: float) -> str:
    """녹화 시작 시각 + 오프셋 → HH:MM:SS."""
    return (base + timedelta(seconds=offset_sec)).strftime("%H:%M:%S")


def main() -> int:
    if not acquire_lock():
        return 2

    log_rec = _stage_log("1_record")
    log_mot = _stage_log("2_motion")
    log_ai  = _stage_log("3_analysis")
    log_rep = _stage_log("4_report")

    try:
        cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
        tg = cfg["telegram"]

        # ===== 1. 녹화 =====
        log_rec.info("녹화 시작")
        video, rec_warnings = record(cfg["rtsp"])
        if not video:
            log_rec.error("녹화 실패 — 카메라 연결을 확인하세요")
            telegram_notifier.send(tg, "⚠️ CCTV 녹화 실패 — 카메라 연결을 확인하세요.")
            return 1
        log_rec.info(f"녹화 완료: {video.name}  ({video.stat().st_size // 1024 // 1024} MB)")
        if rec_warnings:
            warn_text = "⚠️ CCTV 녹화 중 RTSP 끊김 발생\n" + "\n".join(rec_warnings)
            log_rec.warning(warn_text)
            telegram_notifier.send(tg, warn_text)

        base_time = parse_base_time(video.name)
        date_str  = base_time.strftime("%Y-%m-%d")
        work_dir  = BASE / "work" / "single"
        work_dir.mkdir(parents=True, exist_ok=True)
        clip_dir  = Path(os.path.expandvars(cfg["rtsp"]["output_dir"])) / "clip"
        clip_dir.mkdir(parents=True, exist_ok=True)

        # ===== 2. 움직임 감지 =====
        log_mot.info("움직임 감지 시작")
        m = cfg.get("motion", {})
        detector = MotionDetector(
            min_area_ratio=m.get("min_area_ratio", 0.005),
            min_duration=m.get("min_duration", 2.0),
            merge_gap=m.get("merge_gap", 10.0),
            sample_fps=m.get("sample_fps", 2.0),
        )
        segments = detector.detect(str(video))
        log_mot.info(f"감지 완료: {len(segments)}개 구간")

        if not segments:
            log_mot.info("움직임 없음 — 분석 건너뜀")
            telegram_notifier.send(
                tg, f"📹 CCTV 분석 결과 ({date_str})\n\n움직임이 감지되지 않았습니다."
            )
            return 0

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
        log_mot.info(f"인접 병합: {len(segments)}개 → {len(merged)}개")

        # ===== 3. AI 분석 =====
        log_ai.info(f"Gemini 분석 시작 — {len(merged)}개 클립")
        g = cfg["gemini"]
        gemini = GeminiClient(
            api_key=g["api_key"],
            primary_model=g.get("primary_model", "models/gemini-2.5-flash-lite"),
            fallback_model=g.get("fallback_model", "models/gemini-2.5-flash"),
            analysis_fps=g.get("analysis_fps", 0.5),
            use_fallback=g.get("use_fallback", True),
            api_timeout_sec=g.get("api_timeout_sec", 300),
        )

        clip_results = []
        all_events   = []
        for idx, seg in enumerate(merged, 1):
            ts_start = clock(base_time, seg.start_sec)
            ts_end   = clock(base_time, seg.end_sec)
            log_ai.info(f"클립 [{idx}/{len(merged)}] {ts_start}~{ts_end} ({seg.duration:.0f}초)")

            clip_path = str(work_dir / f"clip_{idx:03d}.mp4")
            if not extract_clip(str(video), clip_path, seg.start_sec, seg.end_sec,
                                scale="640:360", fps=2):
                log_ai.error(f"  ffmpeg 추출 실패: {clip_path}")
                clip_results.append((idx, ts_start, ts_end, "(ffmpeg 실패)"))
                continue

            result = gemini.analyze_clip(clip_path)

            # 분석 완료 후 clip_dir로 이동 (삭제하지 않음)
            ts_s = ts_start.replace(":", "")
            ts_e = ts_end.replace(":", "")
            clip_save_name = f"{base_time.strftime('%Y%m%d')}_{ts_s}_{ts_e}.mp4"
            clip_dest = clip_dir / clip_save_name
            try:
                if not clip_dest.exists():
                    shutil.move(clip_path, str(clip_dest))
                else:
                    Path(clip_path).unlink(missing_ok=True)
            except Exception:
                pass

            if not result:
                log_ai.warning("  Gemini 분석 실패")
                clip_results.append((idx, ts_start, ts_end, "(분석 실패)"))
                continue

            summary_text = result.get("summary", "")
            log_ai.info(f"  {result.get('confidence')} / {summary_text}")
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
                    "time_str":    abs_t.strftime("%H:%M:%S"),
                    "clip_start":  ts_start[:5],
                    "clip_end":    ts_end[:5],
                    "category":    ev.get("category", "기타"),
                    "description": ev.get("description", ""),
                    "_abs":        abs_t,
                })

        cost = gemini.get_cost_estimate()
        log_ai.info(
            f"분석 완료 — 이벤트 {len(all_events)}개 / "
            f"API {cost['api_calls']}회 / 폴백 {cost['fallback_calls']}회 / "
            f"비용 약 {cost['estimated_cost_krw']}원"
        )

        # ===== 4. 리포트 + 알림 =====
        log_rep.info("리포트 생성 시작")

        window = cfg.get("analysis", {}).get("dedup_window_sec", 90.0)
        deduped, skip = [], set()
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
        log_rep.info(f"중복 제거: {len(all_events)}건 → {len(deduped)}건")

        events_text = "\n".join(
            f"- {e['time_str']} [{e['category']}]: {e['description']}" for e in deduped
        )
        daily_summary = gemini.generate_daily_summary(date_str, events_text)

        header = f"📹 CCTV 분석 결과 ({date_str})\n\n"

        # 메시지 1: 클립별 요약
        clip_lines = ["클립별 요약\n"]
        for idx, ts_s, ts_e, summ in clip_results:
            clip_lines.append(f"{idx:<4} │ {ts_s}~{ts_e} │ {summ}")
        msg1 = header + "\n".join(clip_lines)

        # 메시지 2: 일일 요약 + 카테고리 종합 + 비용
        # 카테고리별 이벤트 발생 시각(HH:MM) — 10분 이내 동일 행동 제외
        cat_last: dict = {}
        cat_times: dict = {}
        for ev in deduped:
            last_abs = cat_last.get(ev["category"])
            if last_abs is None or (ev["_abs"] - last_abs).total_seconds() >= 600:
                cat_times.setdefault(ev["category"], []).append(ev["time_str"][:5])
                cat_last[ev["category"]] = ev["_abs"]
        category_summary = "\n".join(
            f"{cat}: {', '.join(times)}" for cat, times in cat_times.items()
        )
        cost_line = (
            f"\n\n💰 분석 비용: 약 {cost['estimated_cost_krw']}원 "
            f"(API {cost['api_calls']}회, 폴백 {cost['fallback_calls']}회)"
        )
        msg2 = daily_summary
        if category_summary:
            msg2 += "\n\n📋 종합\n" + category_summary
        msg2 += cost_line

        report = msg1 + "\n\n" + msg2

        reports_dir = BASE / "reports"
        reports_dir.mkdir(exist_ok=True)
        report_file = reports_dir / f"report_{_run_ts}.txt"
        report_file.write_text(report, encoding="utf-8")
        log_rep.info(f"리포트 저장: {report_file.name}")

        ok1 = telegram_notifier.send(tg, msg1)
        ok2 = telegram_notifier.send(tg, msg2)
        if ok1 and ok2:
            log_rep.info("텔레그램 전송 완료 (2건)")
        else:
            log_rep.warning(f"텔레그램 전송 일부 실패 (msg1={ok1}, msg2={ok2})")

        return 0

    finally:
        release_lock()


if __name__ == "__main__":
    _started   = datetime.now()
    _exit_code = main()
    _ended     = datetime.now()
    _elapsed   = int((_ended - _started).total_seconds())
    _status    = "OK" if _exit_code == 0 else ("LOCK" if _exit_code == 2 else "NG")
    _entry     = f"{_started:%Y-%m-%d %H:%M:%S} ~ {_ended:%H:%M:%S} ({_elapsed}s) [{_status}]\n"
    with open(_run_log, "a", encoding="utf-8") as _f:
        _f.write(_entry)
    sys.exit(_exit_code)
