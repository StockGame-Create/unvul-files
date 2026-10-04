"""
ai_power.py - 자료실 PDF를 미리 읽어서 "문제 은행"을 만드는 일괄 처리 스크립트.

sync.py와의 관계
----------------
- sync.py는 텔레그램 -> GitHub Release로 PDF를 모아 sites/manifest.json을 만든다.
- 이 스크립트는 그 manifest.json을 읽어서, 아직 처리 안 된 PDF를 Gemini로 한 번
  분석하고 결과를 아래 두 곳에 저장한다. 텔레그램 접속은 필요 없다.
    sites/manifest_2.json   : 파일별 처리 상태/통계 색인 (가볍다)
    sites/ai/<message_id>.json : 파일 하나의 문제/해설/지문 전체 데이터 (무겁다)
    sites/bank_index.json   : api/ask.js가 유사 문제를 찾는 데 쓰는 압축 검색 색인 (실행이 끝날 때마다 재생성)
  (전부 manifest_2.json 하나에 넣으면 수십 MB가 되어 프론트가 매번 받기 부담스러워서,
   색인과 본문을 나눴다. 프론트는 색인으로 목록/검색을 하고, 문제 본문은 필요할 때
   해당 파일의 json만 받아가면 된다.)

처리 방식
---------
1. PDF를 받아서(GitHub Release URL) 페이지를 이미지로 렌더링한다.
2. N페이지씩 묶어 Gemini에 보내 "문항 / 해설 / 공유 지문"을 JSON으로 전사시킨다.
   앞뒤로 문맥용 페이지를 1장씩 더 붙여서, 페이지를 넘어가는 문제도 안 잘리게 한다.
   (문맥 페이지에서 "시작"하는 항목은 이웃 청크가 맡으므로 버린다.)
3. JSON 파싱이 실패하면(출력 잘림, 안전 필터 등) 그 구간을 반으로 쪼개 다시 시도한다.
4. 모든 청크가 끝나면 해설/정답을 문항 번호로 문제에 연결한다.

AI는 문제를 "풀지 않는다". 문서에 인쇄돼 있는 것만 전사한다 (답을 지어내는 걸 막기 위해).

안전장치
--------
- 청크 하나 처리할 때마다 파일별 json을 저장한다 -> 중간에 죽어도 이어서 한다.
- 429를 "분당 한도"와 "일일 한도"로 구분한다. 일일 한도면 그 모델은 오늘 포기하고
  다른 모델로 넘어가며, 전부 소진되면 정상 종료한다 (다음 실행에서 이어서 처리).
- 1회 실행당 요청 수/실행 시간 상한이 있다 (AI_MAX_REQUESTS, AI_MAX_RUNTIME_SECONDS).

환경변수
--------
  GEMINI_API_KEY            (필수)
  AI_MODELS                 쉼표로 구분한 모델 우선순위. 앞에서부터 쓰고, 실패/소진되면 다음으로.
  AI_PAGES_PER_REQUEST      한 요청에 담을 대상 페이지 수 (기본 4)
  AI_CONTEXT_PAGES          앞뒤 문맥 페이지 수 (기본 1)
  AI_RENDER_DPI             렌더링 해상도 (기본 150)
  AI_MAX_OUTPUT_TOKENS      응답 최대 토큰 (기본 16384)
  AI_MIN_INTERVAL_SEC       요청 사이 최소 간격 (기본 4 -> 분당 15회 이하)
  AI_MAX_REQUESTS           1회 실행당 최대 요청 수 (기본 150)
  AI_MAX_RUNTIME_SECONDS    1회 실행 최대 시간 (기본 5시간)
  AI_MAX_PAGES              이 페이지 수를 넘는 PDF는 건너뜀 (기본 300)
  AI_SAVE_CROPS             1이면 문항 영역을 잘라 sites/ai/crops/에 이미지로 저장 (기본 0)

사용 예
-------
  python ai_power.py                 # 처리 안 된 파일 전부 (최신순)
  python ai_power.py --limit 3       # 최대 3개 파일만
  python ai_power.py --id 24377      # 특정 파일만
  python ai_power.py --id 24377 --force   # 이미 처리된 파일도 처음부터 다시
  python ai_power.py --retry-failed  # 실패로 기록된 페이지만 다시 시도
  python ai_power.py --reindex       # 검색 색인(bank_index.json)만 다시 생성
  python ai_power.py --dry-run       # 대상 목록만 보기 (API 호출 없음)
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


# ---- 설정 --------------------------------------------------------------
def _env_str(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env_str(name, str(default)))
    except ValueError:
        return default


SCHEMA_VERSION = 1  # 추출 스키마/프롬프트를 크게 바꾸면 올려서 재처리 대상으로 만든다.

SITE_DIR = Path("sites")
FILES_DIR = SITE_DIR / "files"
MANIFEST_PATH = SITE_DIR / "manifest.json"
AI_MANIFEST_PATH = SITE_DIR / "manifest_2.json"
BANK_INDEX_PATH = SITE_DIR / "bank_index.json"  # ask.js가 읽는 검색용 압축 색인
AI_DATA_DIR = SITE_DIR / "ai"
CROPS_DIR = AI_DATA_DIR / "crops"

# ask.js와 같은 후보를 기본값으로 쓴다. 모델명은 환경변수로 바꿀 수 있다.
MODELS = [
    m.strip()
    for m in _env_str("AI_MODELS", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash").split(",")
    if m.strip()
]
PAGES_PER_REQUEST = max(1, _env_int("AI_PAGES_PER_REQUEST", 4))
CONTEXT_PAGES = max(0, _env_int("AI_CONTEXT_PAGES", 1))
RENDER_DPI = _env_int("AI_RENDER_DPI", 150)
MAX_OUTPUT_TOKENS = _env_int("AI_MAX_OUTPUT_TOKENS", 16384)
MIN_INTERVAL_SEC = _env_int("AI_MIN_INTERVAL_SEC", 4)
MAX_REQUESTS = _env_int("AI_MAX_REQUESTS", 150)
MAX_RUNTIME_SECONDS = _env_int("AI_MAX_RUNTIME_SECONDS", 5 * 60 * 60)
MAX_PAGES = _env_int("AI_MAX_PAGES", 300)
SAVE_CROPS = _env_str("AI_SAVE_CROPS", "0") == "1"

INDEX_TEXT_MAX = 500  # 검색 색인에 넣을 문항당 최대 글자 수 (전체 본문은 sites/ai/<id>.json)
MAX_TRANSIENT_ATTEMPTS = 3  # 모델 하나당 일시 오류(503 등) 재시도 횟수
MAX_RETRY_WAIT_SEC = 90  # 429의 retryDelay가 이보다 길면 기다리지 않고 다음 모델로
MAX_CONSECUTIVE_CHUNK_ERRORS = 5  # 연달아 이만큼 실패하면 실행 자체를 중단 (키/스키마 문제 방지)
TEXT_HINT_MAX_CHARS = 3000  # 페이지당 텍스트 레이어 힌트 최대 길이


def log(*args):
    print(*args, flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, obj, indent=None):
    """임시 파일에 쓰고 교체한다 (쓰는 도중 죽어도 기존 파일이 안 깨지게)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if indent is None:
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(obj, ensure_ascii=False, indent=indent)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---- Gemini 호출 -------------------------------------------------------
class DailyQuotaExceeded(Exception):
    """사용 가능한 모든 모델의 일일 한도가 소진됨 -> 오늘은 더 못 한다."""


class RunBudgetExceeded(Exception):
    """이번 실행의 요청 수/시간 상한에 도달 -> 지금까지 저장하고 종료."""


class FatalError(Exception):
    """API 키 오류 등 재시도해도 소용없는 문제."""


class ChunkError(Exception):
    """이 구간은 처리하지 못함 (영구 오류, 전 모델 일시 실패 등)."""


class ParseError(Exception):
    """응답은 왔지만 쓸 수 있는 JSON이 아님 (잘림/차단 등) -> 구간을 쪼개 재시도."""


_STATE = {"requests": 0, "last_request_at": 0.0}
_dead_models: set = set()  # 존재하지 않는 모델 (404)
_exhausted_models: set = set()  # 오늘 일일 한도를 다 쓴 모델

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "problems": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "section": {"type": "STRING"},
                    "number": {"type": "STRING"},
                    "page": {"type": "INTEGER"},
                    "text": {"type": "STRING"},
                    "choices": {"type": "ARRAY", "items": {"type": "STRING"}},
                    "passage_label": {"type": "STRING"},
                    "has_figure": {"type": "BOOLEAN"},
                    "topic": {"type": "STRING"},
                    "box_2d": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                    "uncertain": {"type": "BOOLEAN"},
                },
                "required": ["number", "page", "text"],
            },
        },
        "solutions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "section": {"type": "STRING"},
                    "number": {"type": "STRING"},
                    "page": {"type": "INTEGER"},
                    "answer": {"type": "STRING"},
                    "explanation": {"type": "STRING"},
                    "uncertain": {"type": "BOOLEAN"},
                },
                "required": ["number", "page"],
            },
        },
        "passages": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "section": {"type": "STRING"},
                    "label": {"type": "STRING"},
                    "page": {"type": "INTEGER"},
                    "text": {"type": "STRING"},
                },
                "required": ["label", "page", "text"],
            },
        },
    },
    "required": ["problems", "solutions", "passages"],
}

PROMPT = """당신은 학습자료 PDF를 구조화된 데이터로 옮기는 정확한 전사(OCR) 도우미입니다.

연속된 PDF 페이지 이미지가 순서대로 주어집니다. 각 이미지 앞에 "PDF n페이지"라고 표시돼 있습니다.
- 추출 대상 페이지: {s}~{e}페이지
- 그 밖의 페이지는 앞뒤에서 이어지는 지문/문제/해설을 확인하기 위한 문맥용일 뿐입니다.
  문맥용 페이지에서 *시작*하는 항목은 추출하지 마세요 (이웃 구간이 따로 처리합니다).
  단, 대상 페이지에서 시작해 다음 페이지로 이어지는 항목은 이어지는 부분까지 포함해서 전사하세요.

추출 규칙:
1. problems: 대상 페이지에서 시작하는 모든 문항.
   - text: 발문, <보기>, 조건 등 문제 본문 전체를 읽히는 순서대로 전사.
   - choices: 선택지를 번호기호(①~⑤) 없이 순서대로. 주관식이면 빈 배열.
   - 여러 문항이 공유하는 지문은 passages에 따로 넣고, 문항에는 passage_label(예: "[16~20]")만 적기.
2. solutions: 해설/풀이/정답표가 대상 페이지에 있을 때의 항목.
   - 빠른 정답표는 number와 answer만 채우고 explanation은 빈 문자열.
   - answer는 정답 표기 그대로(예: "③", "12"). explanation은 풀이 전체.
3. passages: 대상 페이지에서 시작하는 공유 지문 (label, text).
4. 수식/기호/화학식은 LaTeX($...$ 또는 $$...$$)로, 표는 마크다운 표로 적으세요.
5. 그림/그래프/도형은 text에 [그림: 한 줄 설명]으로 표시하고 has_figure=true.
6. 절대 문제를 직접 풀지 마세요. 정답은 문서에 인쇄된 것만 옮기고, 없으면 비워두세요. 추측 금지.
7. 글자가 불확실하거나 가려져 있으면 uncertain=true.
8. section: 머리글/표지에서 보이는 시험 회차·영역·과목 구분(예: "2027 수능 수학영역", "3회"). 안 보이면 "".
9. box_2d: 문항이 *시작된 페이지* 위에서의 영역 [ymin, xmin, ymax, xmax], 각 0~1000으로 정규화. 모르면 빈 배열.
10. topic: 단원/주제를 짧게(예: "미분 - 접선의 방정식"). 확신이 없으면 "".
11. page: 항목이 시작되는 PDF 페이지 번호(위 표시 기준).
12. 표지, 광고, 목차처럼 문항/해설이 없는 페이지는 빈 배열로 두세요.

JSON만 출력하세요."""


def _throttle():
    wait = MIN_INTERVAL_SEC - (time.time() - _STATE["last_request_at"])
    if wait > 0:
        time.sleep(wait)
    _STATE["last_request_at"] = time.time()


def _call(model: str, parts: list) -> dict:
    """generateContent 한 번 호출. HTTPError는 그대로 올린다."""
    body = {
        "system_instruction": {"parts": [{"text": "한국어 학습자료를 정확히 전사해 JSON으로만 답한다."}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": os.environ["GEMINI_API_KEY"],
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _classify_http_error(e: urllib.error.HTTPError):
    raw = e.read().decode("utf-8", "ignore")
    retry_after = None
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', raw)
    if m:
        retry_after = float(m.group(1))
    compact = raw.lower().replace(" ", "").replace("_", "")
    daily = "perday" in compact
    msg = raw[:300].replace("\n", " ")
    return e.code, msg, retry_after, daily


def generate(parts: list, deadline: float):
    """모델 우선순위대로 시도해서 (모델명, 응답dict)를 반환한다."""
    last_detail = ""
    for model in MODELS:
        if model in _dead_models or model in _exhausted_models:
            continue

        for attempt in range(1, MAX_TRANSIENT_ATTEMPTS + 1):
            if time.time() >= deadline or _STATE["requests"] >= MAX_REQUESTS:
                raise RunBudgetExceeded()
            _throttle()
            _STATE["requests"] += 1

            try:
                return model, _call(model, parts)
            except urllib.error.HTTPError as e:
                status, msg, retry_after, daily = _classify_http_error(e)
                last_detail = f"{model} HTTP {status}: {msg}"

                if status in (401, 403):
                    raise FatalError(f"인증/권한 오류: {last_detail}")
                if status == 404:
                    log(f"  [모델 없음, 제외] {model}")
                    _dead_models.add(model)
                    break
                if status == 429 and daily:
                    log(f"  [일일 한도 소진] {model} -> 다음 모델로")
                    _exhausted_models.add(model)
                    break
                if status in (429, 500, 502, 503, 504):
                    wait = retry_after if retry_after is not None else min(3 * 2**attempt, 60)
                    if wait > MAX_RETRY_WAIT_SEC:
                        log(f"  [대기시간이 너무 김({wait:.0f}s)] {model} -> 다음 모델로")
                        break
                    log(f"  [일시 오류 {status}] {model} ({attempt}/{MAX_TRANSIENT_ATTEMPTS}) {wait:.0f}초 후 재시도")
                    time.sleep(wait + 1)
                    continue
                raise ChunkError(last_detail)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_detail = f"{model} 네트워크 오류: {e}"
                log(f"  [네트워크 오류] {model} ({attempt}/{MAX_TRANSIENT_ATTEMPTS}): {e}")
                time.sleep(min(3 * 2**attempt, 30))

    usable = [m for m in MODELS if m not in _dead_models and m not in _exhausted_models]
    if not usable:
        raise DailyQuotaExceeded("사용 가능한 모델이 없습니다 (전부 일일 한도 소진 또는 존재하지 않음).")
    raise ChunkError(f"모든 모델이 일시적으로 실패: {last_detail}")


def parse_response(resp: dict) -> dict:
    cands = resp.get("candidates") or []
    if not cands:
        raise ParseError(f"candidates 없음 (차단?) {str(resp.get('promptFeedback'))[:200]}")
    finish = cands[0].get("finishReason")
    parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise ParseError(f"빈 응답 (finishReason={finish})")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"JSON 파싱 실패 (finishReason={finish}): {e}")
    if not isinstance(data, dict):
        raise ParseError("최상위가 객체가 아님")
    return data


# ---- PDF 처리 ----------------------------------------------------------
def build_parts(doc, s: int, e: int) -> list:
    """대상 [s,e]와 앞뒤 문맥 페이지를 이미지 파트로 만든다 (페이지 번호는 1부터)."""
    total = len(doc)
    first = max(1, s - CONTEXT_PAGES)
    last = min(total, e + CONTEXT_PAGES)
    parts = [{"text": PROMPT.format(s=s, e=e)}]
    for pno in range(first, last + 1):
        page = doc[pno - 1]
        pix = page.get_pixmap(dpi=RENDER_DPI)
        jpg = pix.tobytes("jpeg", jpg_quality=80)
        role = "대상" if s <= pno <= e else "문맥용"
        parts.append({"text": f"PDF {pno}페이지 ({role})"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpg).decode("ascii")}})
        hint = page.get_text().strip()
        if len(hint) >= 40:
            parts.append({"text": f"(참고: PDF {pno}페이지 텍스트 레이어. 깨져 있을 수 있으니 이미지가 우선)\n{hint[:TEXT_HINT_MAX_CHARS]}"})
    return parts


def _s(x) -> str:
    return x.strip() if isinstance(x, str) else ("" if x is None else str(x).strip())


def _i(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def normalize(data: dict, s: int, e: int) -> dict:
    """모델 출력을 정리하고, 대상 페이지 밖에서 시작한 항목은 버린다. 같은 구간 내 중복도 제거."""
    out = {"problems": [], "solutions": [], "passages": []}

    seen = set()
    for p in data.get("problems") or []:
        page = _i(p.get("page"))
        number = _s(p.get("number"))
        text = _s(p.get("text"))
        if page is None or not (s <= page <= e) or not number or not text:
            continue
        key = (_s(p.get("section")), number, page)
        if key in seen:
            continue
        seen.add(key)
        box = p.get("box_2d")
        box = [int(v) for v in box] if isinstance(box, list) and len(box) == 4 and all(_i(v) is not None for v in box) else None
        out["problems"].append({
            "section": _s(p.get("section")),
            "number": number,
            "page": page,
            "text": text,
            "choices": [_s(c) for c in (p.get("choices") or []) if _s(c)],
            "passage_label": _s(p.get("passage_label")),
            "has_figure": bool(p.get("has_figure")),
            "topic": _s(p.get("topic")),
            "box_2d": box,
            "uncertain": bool(p.get("uncertain")),
        })

    seen = set()
    for x in data.get("solutions") or []:
        page = _i(x.get("page"))
        number = _s(x.get("number"))
        if page is None or not (s <= page <= e) or not number:
            continue
        key = (_s(x.get("section")), number, page)
        if key in seen:
            continue
        seen.add(key)
        out["solutions"].append({
            "section": _s(x.get("section")),
            "number": number,
            "page": page,
            "answer": _s(x.get("answer")),
            "explanation": _s(x.get("explanation")),
            "uncertain": bool(x.get("uncertain")),
        })

    for ps in data.get("passages") or []:
        page = _i(ps.get("page"))
        label = _s(ps.get("label"))
        text = _s(ps.get("text"))
        if page is None or not (s <= page <= e) or not label or not text:
            continue
        out["passages"].append({"section": _s(ps.get("section")), "label": label, "page": page, "text": text})

    return out


def _merge_results(a: dict, b: dict) -> dict:
    return {
        "items": {k: a["items"][k] + b["items"][k] for k in ("problems", "solutions", "passages")},
        "failed_pages": a["failed_pages"] + b["failed_pages"],
        "model": b["model"] or a["model"],
    }


def extract_range(doc, s: int, e: int, deadline: float) -> dict:
    """[s,e] 구간을 처리한다. 응답이 못 쓸 모양이면 반으로 쪼개 재시도한다."""
    empty = {"problems": [], "solutions": [], "passages": []}
    try:
        parts = build_parts(doc, s, e)
        model, resp = generate(parts, deadline)
        items = normalize(parse_response(resp), s, e)
        _STATE["chunk_errors"] = 0
        return {"items": items, "failed_pages": [], "model": model}
    except ParseError as err:
        log(f"  [응답 사용 불가 p.{s}-{e}] {err}")
        if e > s:
            mid = (s + e) // 2
            log(f"  -> 구간을 쪼개 재시도: p.{s}-{mid} / p.{mid + 1}-{e}")
            return _merge_results(extract_range(doc, s, mid, deadline), extract_range(doc, mid + 1, e, deadline))
        return {"items": empty, "failed_pages": [s], "model": None}
    except ChunkError as err:
        _STATE["chunk_errors"] = _STATE.get("chunk_errors", 0) + 1
        log(f"  [구간 실패 p.{s}-{e}] {err}")
        if _STATE["chunk_errors"] >= MAX_CONSECUTIVE_CHUNK_ERRORS:
            raise FatalError(f"연속 {MAX_CONSECUTIVE_CHUNK_ERRORS}회 실패라 중단합니다. 마지막 오류: {err}")
        return {"items": empty, "failed_pages": list(range(s, e + 1)), "model": None}


def _norm_section(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def link_solutions(data: dict) -> dict:
    """해설/정답을 문항 번호로 문제에 붙인다. 통계를 반환."""
    problems = data["problems"]
    by_number = {}
    for p in problems:
        by_number.setdefault(p["number"], []).append(p)
        p.pop("answer", None)
        p.pop("explanation", None)
        p.pop("solution_page", None)

    matched = 0
    for sol in data["solutions"]:
        cands = by_number.get(sol["number"], [])
        sec = _norm_section(sol["section"])
        pick = [c for c in cands if _norm_section(c["section"]) == sec] if sec else []
        if not pick and len(cands) == 1:
            pick = cands
        if not pick:
            sol["matched"] = False
            continue
        target = pick[0]
        sol["matched"] = True
        # 정답표(답만)와 상세 해설이 따로 있을 수 있으니 채워진 쪽을 우선해 합친다.
        if sol["answer"] and not target.get("answer"):
            target["answer"] = sol["answer"]
        if sol["explanation"] and len(sol["explanation"]) > len(target.get("explanation", "")):
            target["explanation"] = sol["explanation"]
            target["solution_page"] = sol["page"]
        matched += 1

    return {
        "problem_count": len(problems),
        "solution_count": len(data["solutions"]),
        "matched_count": matched,
        "with_answer": sum(1 for p in problems if p.get("answer")),
        "with_explanation": sum(1 for p in problems if p.get("explanation")),
    }


def save_crops(doc, message_id: int, problems: list, only_pages: range):
    """box_2d가 있는 문항의 영역을 이미지로 잘라 저장하고 problem['crop']에 경로를 기록."""
    for idx, p in enumerate(problems):
        if p["page"] not in only_pages or not p.get("box_2d") or p.get("crop"):
            continue
        ymin, xmin, ymax, xmax = p["box_2d"]
        if not (0 <= ymin < ymax <= 1000 and 0 <= xmin < xmax <= 1000):
            continue
        try:
            page = doc[p["page"] - 1]
            r = page.rect
            pad = 0.01
            clip = pymupdf.Rect(
                r.x0 + max(0, xmin / 1000 - pad) * r.width,
                r.y0 + max(0, ymin / 1000 - pad) * r.height,
                r.x0 + min(1, xmax / 1000 + pad) * r.width,
                r.y0 + min(1, ymax / 1000 + pad) * r.height,
            )
            safe_no = re.sub(r"[^0-9A-Za-z가-힣_-]", "_", p["number"])
            rel = f"crops/{message_id}/p{p['page']}_{safe_no}_{idx}.jpg"
            out = AI_DATA_DIR / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            page.get_pixmap(dpi=RENDER_DPI, clip=clip).save(str(out), jpg_quality=80)
            p["crop"] = f"ai/{rel}"
        except Exception as ex:  # 크롭 실패는 치명적이지 않다.
            log(f"  [크롭 실패] p.{p['page']} #{p['number']}: {ex}")


def fetch_pdf(entry: dict) -> Path | None:
    """PDF를 로컬 임시 경로로 가져온다. 실패하면 None."""
    stored = entry.get("stored_as")
    if stored and (FILES_DIR / stored).exists():
        return FILES_DIR / stored
    url = entry.get("download_url")
    if not url:
        return None
    tmp_dir = Path(tempfile.gettempdir()) / "ai_power"
    tmp_dir.mkdir(exist_ok=True)
    dest = tmp_dir / f"{entry['message_id']}.pdf"
    for attempt in range(1, 4):
        try:
            # 공개 저장소의 Release 자산이라 인증 헤더는 보내지 않는다
            # (리다이렉트 대상에 Authorization이 같이 가면 오히려 거부된다).
            with urllib.request.urlopen(url, timeout=300) as resp, open(dest, "wb") as f:
                while chunk := resp.read(1024 * 1024):
                    f.write(chunk)
            return dest
        except Exception as ex:
            log(f"  [다운로드 실패 {attempt}/3] {ex}")
            time.sleep(3 * attempt)
    return None


def fresh_data(entry: dict, sha: str, total: int) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "message_id": entry["message_id"],
        "filename": entry.get("filename"),
        "sha256": sha,
        "pages_total": total,
        "pages_done": 0,
        "complete": False,
        "failed_pages": [],
        "model": None,
        "updated_at": now_iso(),
        "passages": [],
        "problems": [],
        "solutions": [],
    }


def process_file(entry: dict, ai_index: dict, force: bool, deadline: float, retry_failed: bool = False) -> str:
    """파일 하나 처리. 반환: 'done' | 'partial' | 'skipped' | 'failed'"""
    mid = entry["message_id"]
    sha = entry.get("sha256") or str(entry.get("size_bytes") or "")
    data_path = AI_DATA_DIR / f"{mid}.json"

    log(f"\n=== {entry.get('filename')} (id={mid}, {(entry.get('size_bytes') or 0) / 1048576:.1f}MB) ===")

    pdf_path = fetch_pdf(entry)
    if pdf_path is None:
        log("  [건너뜀] PDF를 가져오지 못했습니다.")
        _set_status(ai_index, entry, sha, "failed", note="PDF 다운로드 실패")
        return "failed"

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as ex:
        log(f"  [건너뜀] PDF를 열 수 없습니다: {ex}")
        _set_status(ai_index, entry, sha, "failed", note=f"PDF 열기 실패: {ex}")
        return "failed"

    total = len(doc)
    if total > MAX_PAGES:
        log(f"  [건너뜀] {total}페이지 > AI_MAX_PAGES({MAX_PAGES})")
        _set_status(ai_index, entry, sha, "skipped", note=f"{total}페이지로 상한 초과", pages_total=total)
        doc.close()
        return "skipped"

    # 이어하기: 같은 파일(sha)/같은 스키마의 진행 상황이 있으면 거기서부터.
    data = None
    if data_path.exists() and not force:
        try:
            old = json.loads(data_path.read_text(encoding="utf-8"))
            if old.get("sha256") == sha and old.get("schema_version") == SCHEMA_VERSION and old.get("pages_total") == total:
                data = old
                log(f"  이어서 처리: {data['pages_done']}/{total}페이지 완료 상태")
        except Exception:
            pass
    if data is None:
        data = fresh_data(entry, sha, total)

    status = "partial"
    try:
        if retry_failed and data["complete"] and data["failed_pages"]:
            # 실패했던 페이지만 한 장씩 다시 시도한다.
            retry_pages = list(data["failed_pages"])
            data["failed_pages"] = []
            log(f"  실패 페이지 재시도: {retry_pages}")
            for pno in retry_pages:
                result = extract_range(doc, pno, pno, deadline)
                items = result["items"]
                data["problems"] += items["problems"]
                data["solutions"] += items["solutions"]
                data["passages"] += items["passages"]
                data["failed_pages"] = sorted(set(data["failed_pages"] + result["failed_pages"]))
                data["model"] = result["model"] or data["model"]
                if SAVE_CROPS:
                    save_crops(doc, mid, data["problems"], range(pno, pno + 1))
                write_json(data_path, data)

        while data["pages_done"] < total:
            s = data["pages_done"] + 1
            e = min(s + PAGES_PER_REQUEST - 1, total)
            log(f"  p.{s}-{e} / {total} 처리 중 ...")

            result = extract_range(doc, s, e, deadline)
            items = result["items"]
            data["problems"] += items["problems"]
            data["solutions"] += items["solutions"]
            data["passages"] += items["passages"]
            data["failed_pages"] = sorted(set(data["failed_pages"] + result["failed_pages"]))
            data["model"] = result["model"] or data["model"]
            data["pages_done"] = e
            data["updated_at"] = now_iso()

            if SAVE_CROPS:
                save_crops(doc, mid, data["problems"], range(s, e + 1))

            write_json(data_path, data)  # 청크마다 저장 (중간에 죽어도 이어하기 가능)
            log(f"    -> 문항 {len(items['problems'])} / 해설 {len(items['solutions'])} / 지문 {len(items['passages'])}")

        data["complete"] = True
        status = "done"
    except RunBudgetExceeded:
        log("  [이번 실행의 요청/시간 상한 도달] 진행 상황을 저장하고 멈춥니다.")
        raise
    finally:
        stats = link_solutions(data)
        data["stats"] = stats
        data["updated_at"] = now_iso()
        write_json(data_path, data)
        _set_status(
            ai_index, entry, sha, "done" if data["complete"] else "partial",
            pages_total=total, pages_done=data["pages_done"],
            failed_pages=data["failed_pages"], model=data["model"], **stats,
        )
        doc.close()

    log(f"  완료: 문항 {stats['problem_count']} / 해설 {stats['solution_count']} / 연결 {stats['matched_count']}"
        + (f" / 실패 페이지 {data['failed_pages']}" if data["failed_pages"] else ""))
    return status


def _set_status(ai_index: dict, entry: dict, sha: str, status: str, **extra):
    mid = entry["message_id"]
    row = {
        "message_id": mid,
        "filename": entry.get("filename"),
        "title": entry.get("title"),
        "year": entry.get("year"),
        "instructor": entry.get("instructor"),
        "subject": entry.get("subject"),
        "sha256": sha,
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "data": f"ai/{mid}.json",
        "updated_at": now_iso(),
    }
    row.update(extra)
    ai_index["files"] = [r for r in ai_index["files"] if r["message_id"] != mid] + [row]
    ai_index["updated_at"] = now_iso()
    write_json(AI_MANIFEST_PATH, ai_index, indent=2)


def load_ai_index() -> dict:
    if AI_MANIFEST_PATH.exists():
        try:
            idx = json.loads(AI_MANIFEST_PATH.read_text(encoding="utf-8"))
            idx.setdefault("files", [])
            return idx
        except Exception:
            log("[경고] manifest_2.json을 읽지 못해 새로 시작합니다.")
    return {"schema_version": SCHEMA_VERSION, "updated_at": now_iso(), "files": []}


def needs_work(entry: dict, ai_index: dict, retry_failed: bool = False) -> bool:
    sha = entry.get("sha256") or str(entry.get("size_bytes") or "")
    row = next((r for r in ai_index["files"] if r["message_id"] == entry["message_id"]), None)
    if row is None:
        return True
    if row.get("sha256") != sha or row.get("schema_version") != SCHEMA_VERSION:
        return True
    if retry_failed and row.get("failed_pages"):
        return True
    return row.get("status") == "partial"  # done / skipped / failed는 --force 없이는 다시 안 한다.


def build_bank_index():
    """sites/ai/*.json 전체를 훑어 ask.js용 검색 색인(sites/bank_index.json)을 만든다.
    API 호출 없이 로컬 파일만 읽으므로 언제든 다시 돌려도 안전하다 (--reindex).
    문항 본문은 앞부분만 넣고, 해설 전문 등은 ask.js가 필요할 때 개별 json에서 가져간다."""
    ai_index = load_ai_index()
    files_meta, items = [], []
    for row in ai_index["files"]:
        dp = AI_DATA_DIR / f"{row['message_id']}.json"
        if not dp.exists():
            continue
        try:
            data = json.loads(dp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not data.get("problems"):
            continue
        fi = len(files_meta)
        files_meta.append({
            "id": row["message_id"],
            "title": row.get("title") or row.get("filename"),
            "subject": row.get("subject"),
            "year": row.get("year"),
            "instructor": row.get("instructor"),
        })
        for p in data["problems"]:
            body = p["text"]
            if p.get("choices"):
                body += " " + " / ".join(p["choices"])
            items.append({
                "f": fi,
                "n": p["number"],
                "p": p["page"],
                "s": p.get("section", ""),
                "t": body[:INDEX_TEXT_MAX],
                "k": p.get("topic", ""),
                "a": p.get("answer", ""),
                "e": 1 if p.get("explanation") else 0,
                "u": 1 if p.get("uncertain") else 0,
            })
    write_json(BANK_INDEX_PATH, {"version": 1, "built_at": now_iso(), "files": files_meta, "items": items})
    log(f"검색 색인 갱신: 파일 {len(files_meta)}개 / 문항 {len(items)}개 -> {BANK_INDEX_PATH}")


def main():
    ap = argparse.ArgumentParser(description="자료실 PDF -> 문제 은행(manifest_2.json) 생성")
    ap.add_argument("--id", type=int, action="append", help="특정 message_id만 (여러 번 지정 가능)")
    ap.add_argument("--limit", type=int, default=0, help="이번 실행에서 처리할 최대 파일 수")
    ap.add_argument("--force", action="store_true", help="이미 처리된 파일도 처음부터 다시")
    ap.add_argument("--retry-failed", action="store_true", help="처리 실패로 기록된 페이지만 다시 시도")
    ap.add_argument("--reindex", action="store_true", help="API 호출 없이 검색 색인(bank_index.json)만 다시 만든다")
    ap.add_argument("--dry-run", action="store_true", help="대상 목록만 출력 (API 호출 없음)")
    args = ap.parse_args()

    if args.reindex:
        build_bank_index()
        return

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    ai_index = load_ai_index()

    files = sorted(manifest["files"], key=lambda f: f.get("telegram_date") or "", reverse=True)  # 최신순
    if args.id:
        files = [f for f in files if f["message_id"] in set(args.id)]
    targets = [f for f in files if args.force or needs_work(f, ai_index, args.retry_failed)]
    if args.limit:
        targets = targets[: args.limit]

    log(f"처리 대상: {len(targets)}개 (전체 {len(manifest['files'])}개 중) / 모델: {', '.join(MODELS)}")
    if args.dry_run:
        for f in targets:
            log(f"  - {f['message_id']} {f.get('filename')} ({(f.get('size_bytes') or 0) / 1048576:.1f}MB)")
        return
    if not targets:
        return
    if not os.environ.get("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY가 설정돼있지 않습니다.")

    deadline = time.time() + MAX_RUNTIME_SECONDS
    counts = {"done": 0, "partial": 0, "skipped": 0, "failed": 0}
    try:
        for f in targets:
            counts[process_file(f, ai_index, args.force, deadline, args.retry_failed)] += 1
    except RunBudgetExceeded:
        counts["partial"] += 1
    except DailyQuotaExceeded as ex:
        log(f"\n[일일 한도] {ex} 내일 다시 실행하면 이어서 처리됩니다.")
    except FatalError as ex:
        log(f"\n[중단] {ex}")
        sys.exit(1)

    try:
        build_bank_index()
    except Exception as ex:
        log(f"[경고] 검색 색인 생성 실패: {ex}")

    log(f"\n요약: 완료 {counts['done']} / 진행중(이어하기 필요) {counts['partial']} / 건너뜀 {counts['skipped']} / 실패 {counts['failed']} "
        f"/ 이번 실행 API 요청 {_STATE['requests']}회")


if __name__ == "__main__":
    main()
