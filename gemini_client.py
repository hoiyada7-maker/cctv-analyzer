"""
Gemini API 클라이언트
- Flash-Lite 우선, 모호한 경우 Flash로 폴백
- FPS 0.5로 토큰 절약
- File API 자동 정리
"""

import json
import time
import logging
from pathlib import Path
from typing import Dict, Optional

from google import genai
from google.genai import types

log = logging.getLogger(__name__)


CLIP_PROMPT = """이 CCTV 영상을 분석해서 JSON으로만 응답해.

다음 7가지 행동만 찾아줘 (번호 순으로 우선순위가 높으며, 1번을 가장 먼저 확인할 것):
1. 비닐봉지버리기: 간병인 또는 환자가 비닐봉지를 버리거나 처리하는 행동 ← 최우선 탐지
2. 기저귀체크: 간병인이 환자의 기저귀 상태를 확인하는 행동 (이불 들추기, 하체 확인 등)
3. 기저귀교체: 간병인이 환자의 기저귀를 실제로 교체하는 행동 (기저귀 제거, 새 기저귀 착용, 엉덩이 닦기 등)
4. 위생케어: 수건·물티슈 등으로 환자의 얼굴·손·몸을 닦는 행동
5. 환자분노: 환자가 간병인에게 화를 내거나 소리를 지르거나 밀치는 행동
6. 간병인학대: 간병인이 환자를 때리거나 밀거나 고함을 치는 행동
7. 환자발버둥: 환자가 크게 몸을 움직이거나 발버둥치거나 침대에서 벗어나려는 행동

응답 형식:
{
  "has_meaningful_event": true/false,
  "confidence": "high"/"medium"/"low",
  "events": [
    {"time": "MM:SS", "category": "비닐봉지버리기|기저귀체크|기저귀교체|위생케어|환자분노|간병인학대|환자발버둥", "description": "한국어 1문장"}
  ],
  "summary": "전체 영상을 한 문장으로 (한국어)"
}

규칙:
- 위 7가지 외의 행동은 이벤트에 포함하지 않음
- 해당 행동이 없으면 has_meaningful_event=false, events=[]
- 확실하지 않으면 confidence=low로 표시
- 마크다운 코드블록 금지, JSON만 출력
"""

DAILY_SUMMARY_PROMPT = """다음은 어제 ({date_str}) 우리 집 CCTV에서 감지된 이벤트들이야.

이벤트 목록:
{events_text}

다음 형식으로 한국어 일일 보고서를 만들어줘:

📅 {date_str} CCTV 시간별 요약

📦 택배/배달: (있을 때만)
🚪 방문자: (있을 때만)
🚗 차량 활동: (있을 때만)
🐕 반려동물/동물: (있을 때만)
⚠️ 주의할 만한 일: (있을 때만)

📝 종합: 2~3문장 자연어 요약

규칙:
- 해당 없는 카테고리는 생략
- 반복되는 사소한 움직임은 묶어서 1줄로
- 시간을 명확히 표시
"""


class GeminiClient:
    """
    비용 최적화 전략:
    1. Flash-Lite로 1차 분석 (저렴)
    2. confidence=low거나 의심스러우면 Flash로 재확인
    3. FPS 0.5로 토큰 절감 (CCTV는 정적이라 충분)
    """

    def __init__(
        self,
        api_key: str,
        primary_model: str = "models/gemini-2.5-flash-lite",
        fallback_model: str = "models/gemini-2.5-flash",
        analysis_fps: float = 0.5,
        use_fallback: bool = True,
    ):
        self.client = genai.Client(api_key=api_key)
        self.primary_model = primary_model
        self.fallback_model = fallback_model
        self.analysis_fps = analysis_fps
        self.use_fallback = use_fallback

        # 비용 추적
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.api_calls = 0
        self.fallback_calls = 0

    def analyze_clip(self, video_path: str) -> Optional[Dict]:
        """클립 분석. 결과가 모호하면 더 강한 모델로 재시도."""
        # 1차: Flash-Lite
        result = self._call_api(video_path, self.primary_model)
        if result is None:
            return None

        # 폴백 조건: confidence가 낮고, 이벤트가 있다고 했을 때
        # (조용한 영상은 굳이 Flash로 다시 안 돌림)
        needs_fallback = (
            self.use_fallback
            and result.get("has_meaningful_event")
            and result.get("confidence") == "low"
        )

        if needs_fallback:
            log.info(f"  confidence=low → Flash로 재분석")
            self.fallback_calls += 1
            better = self._call_api(video_path, self.fallback_model)
            if better is not None:
                return better

        return result

    def _call_api(
        self, video_path: str, model: str, max_retries: int = 3
    ) -> Optional[Dict]:
        """실제 API 호출. File API 업로드 → 분석 → 파일 정리."""
        myfile = None
        for attempt in range(max_retries):
            try:
                log.info(f"  [{model.split('/')[-1]}] 업로드: {Path(video_path).name}")
                myfile = self.client.files.upload(file=video_path)

                # ACTIVE 대기
                wait = 0
                while myfile.state.name == "PROCESSING":
                    if wait > 120:
                        log.error("  파일 처리 시간 초과")
                        return None
                    time.sleep(3)
                    wait += 3
                    myfile = self.client.files.get(name=myfile.name)

                if myfile.state.name != "ACTIVE":
                    log.error(f"  파일 상태 비정상: {myfile.state.name}")
                    return None

                # FPS 0.5로 분석 (토큰 절감)
                response = self.client.models.generate_content(
                    model=model,
                    contents=types.Content(parts=[
                        types.Part(
                            file_data=types.FileData(
                                file_uri=myfile.uri,
                                mime_type=myfile.mime_type,
                            ),
                            video_metadata=types.VideoMetadata(
                                fps=self.analysis_fps
                            ),
                        ),
                        types.Part(text=CLIP_PROMPT),
                    ]),
                )

                # 토큰 사용량 기록
                if response.usage_metadata:
                    self.total_input_tokens += response.usage_metadata.prompt_token_count or 0
                    self.total_output_tokens += response.usage_metadata.candidates_token_count or 0
                self.api_calls += 1

                # 파싱
                text = (response.text or "").strip()
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                    text = text.strip("` \n")

                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    log.warning(f"  JSON 파싱 실패, 폴백")
                    parsed = {
                        "has_meaningful_event": True,
                        "confidence": "low",
                        "events": [],
                        "summary": text[:300],
                    }

                return parsed

            except Exception as e:
                log.error(f"  API 호출 실패 ({attempt+1}/{max_retries}): {e}")
                wait = 40 if "429" in str(e) else 5 * (attempt + 1)
                time.sleep(wait)
            finally:
                # 파일 정리 (실패해도 시도)
                if myfile is not None:
                    try:
                        self.client.files.delete(name=myfile.name)
                    except Exception:
                        pass

        return None

    def generate_daily_summary(self, date_str: str, events_text: str) -> str:
        """일일 종합 요약 - 텍스트 입력이라 매우 저렴"""
        try:
            response = self.client.models.generate_content(
                model=self.primary_model,
                contents=[DAILY_SUMMARY_PROMPT.format(
                    date_str=date_str,
                    events_text=events_text,
                )],
            )
            if response.usage_metadata:
                self.total_input_tokens += response.usage_metadata.prompt_token_count or 0
                self.total_output_tokens += response.usage_metadata.candidates_token_count or 0
            return response.text.strip()
        except Exception as e:
            log.error(f"일일 요약 생성 실패: {e}")
            return ""

    def get_cost_estimate(self) -> Dict:
        """현재까지의 비용 추정 (USD)"""
        # Flash-Lite: $0.10/M 입력, $0.40/M 출력
        # 비디오 분석은 대부분 Flash-Lite로 처리된다고 가정
        input_cost = self.total_input_tokens / 1_000_000 * 0.10
        output_cost = self.total_output_tokens / 1_000_000 * 0.40

        # 폴백 비율 보정 (Flash는 3배 비쌈)
        if self.api_calls > 0:
            fallback_ratio = self.fallback_calls / self.api_calls
            input_cost *= (1 + fallback_ratio * 2)
            output_cost *= (1 + fallback_ratio * 5.25)

        total_usd = input_cost + output_cost
        return {
            "api_calls": self.api_calls,
            "fallback_calls": self.fallback_calls,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "estimated_cost_usd": round(total_usd, 4),
            "estimated_cost_krw": round(total_usd * 1400),  # 환율 가정
        }
