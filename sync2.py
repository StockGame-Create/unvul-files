"""
평가원(수능/6월·9월 모의평가) 기출문제를 내려받아 GitHub Release로 올리는 스크립트.
(sync.py의 "모의고사 버전": 텔레그램 대신 평가원 수능 사이트 게시판이 소스)

sync.py와 같은 것
-----------------
- 모든 PDF를 GitHub Release 자산으로 업로드하고 download_url만 manifest에 기록한다.
- release 1000개 제한 대응 로테이션 (mock-files -> mock-files-2 -> ...), 업로드가
  실패한 것처럼 보여도 실제론 올라갔는지 확인하는 복구 로직, git push 재시도,
  NFC 정규화, sha256 중복 검사, 첫 페이지 썸네일, 파일 하나 끝날 때마다 manifest 저장.
  (이 부분은 sync.py 코드를 그대로 가져왔다.)

sync.py와 다른 것
-----------------
- 소스: 평가원 게시판 목록 페이지(HTML)를 page=1,2,3... 순서로 읽어서
  fn_fileDown('<fileSeq>') 링크를 뽑고, /boardCnts/fileDown.do?fileSeq=... 로 받는다.
- 중복 판별 키: message_id 대신 fileSeq (zip 안의 PDF는 "fileSeq#PDF이름").
- zip(사회·과학탐구 등)은 풀어서 안의 PDF를 개별 파일로 올린다. 영어 듣기 음원은 건너뛴다.
- 실시간 리스너/자기 예약 체인 없음: 한 바퀴 돌면 끝난다 (cron으로 주기 실행).
- manifest는 sites/manifest_mock.json (sync.py의 manifest.json과 분리 -> push 충돌 방지),
  release 태그는 mock-files (large-files와 로테이션 번호가 섞이지 않게 분리).
- message_id는 fileSeq 해시로 만든 큰 숫자(10^10 이상)라서, 기존 view.js/pdf.js가
  message_id로 조회할 때 텔레그램 id와 겹치지 않는다.

사용법
------
  python sync2.py --dry-run          # 받을 목록만 출력 (첫 실행은 반드시 이걸로)
  python sync2.py --limit 5          # 새 PDF를 5개만 처리하고 종료 (시험 삼아)
  python sync2.py                    # 전체 실행
  --min-year 2023                        # 이 학년도 이상만 (기본 2023 = 2022년 6월 모평부터)
  --board suneung | mopyeong | all

6월·9월 모평 게시판은 주소를 환경변수 MOPYEONG_LIST_URL에 넣어야 켜진다
(사이트에서 모평 목록 페이지를 연 상태의 주소창 URL 전체).
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import pymupdf

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # 로컬에 dotenv가 없어도 동작
    pass

# ---- GitHub / Release ---------------------------------------------------
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")  # "owner/repo" (Actions에서 자동)
GH_DISPATCH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN")  # contents:write (release 생성/업로드, push)

RELEASE_TAG = "mock-files"               # 1번은 이 이름 그대로, 2번부터 -2, -3 ... (sync.py의 large-files와 분리)
RELEASE_ASSET_LIMIT = 1000               # GitHub이 release 하나에 허용하는 자산 개수 상한 (참고용)
RELEASE_SOFT_LIMIT = 990                 # 이 개수에 닿으면 다음 release로 넘어감
MAX_RELEASE_INDEX = 200
MAX_ROTATIONS_PER_UPLOAD = 5
MAX_SIZE_BYTES = 1900 * 1024 * 1024
DOWNLOAD_MAX_RETRIES = 4
THUMBNAIL_WIDTH = 400

# ---- 소스(평가원) --------------------------------------------------------
BASE_URL = os.environ.get("MOCK_BASE_URL", "https://www.suneung.re.kr").rstrip("/")
USER_AGENT = os.environ.get("MOCK_USER_AGENT", "Mozilla/5.0 (compatible; unuvl-mock-sync/1.0)")
REQUEST_DELAY = float(os.environ.get("MOCK_REQUEST_DELAY", "1.0"))  # 요청 사이 대기(초). 서버에 부담 안 주기

# 2022년 6월 모평 = 2023학년도 6월 모평. 학년도 기준으로 이 값 이상만 받는다.
MIN_HAKNYEON = 2023
MAX_PAGES = 60                           # 목록 페이지 수 안전 상한 (무한 루프 방지)
MAX_RUNTIME_SECONDS = 5 * 60 * 60 + 30 * 60

# 수능 기출 게시판 (확인됨: boardID=1500234)
SUNEUNG_BOARD = {"key": "suneung", "label": "수능", "board_id": "1500234", "m": "0403", "s": "suneung"}
# 6월·9월 모평 게시판은 주소를 환경변수로 받는다 (코드 수정 없이 켜고 끌 수 있게)
MOPYEONG_LIST_URL = os.environ.get("MOPYEONG_LIST_URL", "").strip()

# message_id는 텔레그램 id와 겹치지 않게 큰 숫자 대역을 쓴다.
MESSAGE_ID_BASE = 10_000_000_000

# ---- 저장 경로 -----------------------------------------------------------
SITE_DIR = Path("sites")
THUMBS_DIR = SITE_DIR / "thumbnails"
MANIFEST_PATH = SITE_DIR / "manifest_mock.json"
WORK_DIR = Path("mock_work")             # 임시 다운로드 폴더 (sites/ 밖이라 git add에 안 걸림)


# ==========================================================================
# [sync.py에서 그대로 가져온 부분] 이름 정규화 / 해시 / 썸네일
# ==========================================================================

def normalize_name(name: str | None) -> str | None:
    """유니코드 정규화(NFC)를 적용한다. 한글 등은 완성형(NFC)/조합형(NFD) 두 표현이
    있는데 눈에는 똑같아 보여도 바이트로는 다른 문자열이라 '==' 비교가 실패한다.
    파일명이 오간 경로(텔레그램 API, GitHub API, 로컬 파일시스템, 과거 실행 결과)가
    저마다 다른 정규화 형태를 쓸 수 있어서, 이름을 비교하거나 저장하기 전에는
    항상 이걸 거쳐서 형태를 통일한다."""
    return unicodedata.normalize("NFC", name) if name is not None else None


def compute_sha256(path: Path) -> str:
    """파일 내용의 SHA-256 해시. 같은 내용의 파일(이름은 달라도)을 잡아내는 데 사용."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def make_thumbnail(pdf_path: Path, thumb_path: Path) -> bool:
    """PDF 첫 페이지를 이미지로 렌더링해서 thumb_path에 저장. 성공하면 True."""
    try:
        with pymupdf.open(pdf_path) as doc:
            if doc.page_count == 0:
                return False
            page = doc[0]
            if page.rect.width <= 0:
                return False
            zoom = THUMBNAIL_WIDTH / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
            pix.save(thumb_path)
        return True
    except Exception as e:
        print(f"    [썸네일 생성 실패] {pdf_path.name}: {e}", flush=True)
        return False


# [sync.py에서 그대로 가져온 부분] git 커밋/푸시 (재시도 포함)

def _run_git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _has_unpushed_commits() -> bool:
    """현재 브랜치가 원격(업스트림)보다 앞서있는 커밋이 있는지 확인."""
    result = _run_git("rev-list", "@{u}..HEAD", "--count")
    if result.returncode != 0:
        return False
    try:
        return int(result.stdout.strip()) > 0
    except ValueError:
        return False


def _push_with_retry(max_retries: int = 5) -> bool:
    """현재 HEAD를 push. 실패하면(다른 실행/수동 편집과 충돌 등) pull --rebase로
    원격 변경사항을 받아와서 재시도한다. 이게 없으면, push가 실패한 커밋은
    이 컨테이너가 폐기되는 순간 그대로 유실되고 -> 그 파일이 "다운로드됨"으로
    기록되지 않은 채로 다음 실행에서 재다운로드되는 문제로 이어진다."""
    for attempt in range(1, max_retries + 1):
        push = _run_git("push")
        if push.returncode == 0:
            print("    [git push 성공]", flush=True)
            return True

        print(f"    [git push 실패 (시도 {attempt}/{max_retries})] {push.stderr.strip()}", flush=True)

        pull = _run_git("pull", "--rebase", "--autostash")
        if pull.returncode != 0:
            print(f"    [git pull --rebase 실패] {pull.stderr.strip()}", flush=True)

        time.sleep(min(2 ** attempt, 20))

    print(
        f"    [git push 최종 실패] {max_retries}번 재시도했지만 실패했습니다. "
        "이 커밋은 로컬에만 남아있어 이번 실행 종료 시 유실될 수 있습니다.",
        flush=True,
    )
    return False


def git_commit_and_push(commit_message: str) -> None:
    """sites/ 폴더의 변경사항을 즉시 커밋하고, 실패해도 재시도하며 push한다.
    변경사항이 없으면(이미 커밋된 상태 등) 커밋은 건너뛰지만, 혹시 이전에
    push만 실패해서 로컬에 밀린 커밋이 남아있다면 그것까지 함께 재시도한다."""
    _run_git("add", "sites/")

    diff = _run_git("diff", "--staged", "--quiet")
    if diff.returncode != 0:
        commit = _run_git("commit", "-m", commit_message)
        print(f"    [git commit] {commit.stdout.strip()}{commit.stderr.strip()}", flush=True)

    if _has_unpushed_commits():
        _push_with_retry()


# ---- GitHub Release 자산 업로드 (샤딩: 1000개 제한 대응) ---------------------
# GitHub은 release 하나당 자산을 최대 1000개까지만 허용한다(초과 시 HTTP 422
# "file_count limited to 1000 assets per release"). 그래서 release를 번호로
# 나눠서 쓴다: large-files(1번) -> large-files-2 -> large-files-3 ...
# 이미 올라간 파일은 manifest의 download_url에 태그가 박혀 있으므로 영향 없다.
_release_state: dict = {"index": 1, "id": None, "count": None}


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GH_DISPATCH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _tag_for_index(index: int) -> str:
    """1번은 기존 태그(large-files) 그대로, 2번부터는 large-files-2, -3 ..."""
    return RELEASE_TAG if index == 1 else f"{RELEASE_TAG}-{index}"


def _count_release_assets(release_id: int) -> int | None:
    """release에 실제로 올라가 있는 자산 개수를 API로 센다 (페이지네이션 처리)."""
    total = 0
    for page in range(1, 51):  # 최대 5000개까지 (사실상 무제한)
        url = (
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/"
            f"{release_id}/assets?per_page=100&page={page}"
        )
        req = urllib.request.Request(url, headers=_github_headers())
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                items = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print(f"    [release 자산 개수 조회 실패] HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}", flush=True)
            return None
        except Exception as e:
            print(f"    [release 자산 개수 조회 실패] {e}", flush=True)
            return None
        total += len(items)
        if len(items) < 100:
            break
    return total


def _load_release(index: int) -> tuple[int, int] | None:
    """index번 release를 찾아 (id, 현재 자산 개수)를 반환. 없으면 새로 만든다."""
    tag = _tag_for_index(index)
    api_base = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases"

    release_id = None
    req = urllib.request.Request(f"{api_base}/tags/{tag}", headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            release_id = json.loads(resp.read().decode("utf-8"))["id"]
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"    [release 조회 실패] tag={tag} HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}", flush=True)
            return None
    except Exception as e:
        print(f"    [release 조회 실패] tag={tag} {e}", flush=True)
        return None

    if release_id is not None:
        count = _count_release_assets(release_id)
        if count is None:
            return None
        print(f"    [release 사용] tag={tag} (현재 자산 {count}개)", flush=True)
        return release_id, count

    body = json.dumps({
        "tag_name": tag,
        "name": f"모의고사 PDF 저장소 #{index}",
        "body": (
            "sync2.py가 평가원 모의고사 PDF를 모아두는 release입니다. GitHub의 release당 자산 1000개 제한 때문에 "
            "번호를 붙여 여러 개로 나눠 씁니다. 자동으로 관리되니 직접 수정하지 마세요."
        ),
        "draft": False,
        "prerelease": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        api_base, data=body, method="POST",
        headers={**_github_headers(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            release_id = json.loads(resp.read().decode("utf-8"))["id"]
            print(f"    [release 생성됨] tag={tag}", flush=True)
            return release_id, 0
    except urllib.error.HTTPError as e:
        print(f"    [release 생성 실패] tag={tag} HTTP {e.code}: {e.read().decode('utf-8', 'ignore')}", flush=True)
        return None
    except Exception as e:
        print(f"    [release 생성 실패] tag={tag} {e}", flush=True)
        return None


def _find_asset(release_id: int, asset_name: str) -> dict | None:
    """release 안에서 이름이 asset_name인 자산을 찾아 {id, size, browser_download_url}을
    반환한다. 없으면 None. (already_exists 충돌이나, 업로드는 성공했는데 응답을
    못 받아 실패로 오인한 경우를 확인하는 데 쓴다.)"""
    for page in range(1, 51):
        url = (
            f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/"
            f"{release_id}/assets?per_page=100&page={page}"
        )
        req = urllib.request.Request(url, headers=_github_headers())
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                items = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"    [자산 조회 실패] {e}", flush=True)
            return None
        for item in items:
            # 정규화해서 비교: GitHub에 저장된 이름이 (예전 코드가 만들었거나,
            # 다른 경로로 올라와서) 다른 유니코드 정규화 형태일 수 있어서,
            # 바이트 그대로 비교하면 눈에는 같은 이름인데도 못 찾는 경우가 있다.
            if normalize_name(item.get("name")) == normalize_name(asset_name):
                return {
                    "id": item["id"],
                    "size": item.get("size"),
                    "browser_download_url": item["browser_download_url"],
                }
        if len(items) < 100:
            break
    return None


def _delete_asset(asset_id: int) -> bool:
    """찌꺼기/손상된 자산을 지운다 (이름 충돌을 풀고 재업로드하기 위함)."""
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/assets/{asset_id}"
    req = urllib.request.Request(url, method="DELETE", headers=_github_headers())
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:
        print(f"    [자산 삭제 실패] id={asset_id} {e}", flush=True)
        return False


def _reconcile_existing_asset(release_id: int, asset_name: str, expected_size: int) -> str | None:
    """업로드가 실패한 것처럼 보였을 때, 사실은 서버에 이미 올라가 있는지 확인한다.
    용량까지 일치하면 그 자산을 그대로 쓰고(재업로드 안 함) URL을 반환한다.
    이름은 같은데 용량이 다르면(부분 업로드 등 찌꺼기) 지우고 None을 반환해서
    호출부가 새로 업로드하게 한다. 아예 없으면 None."""
    existing = _find_asset(release_id, asset_name)
    if existing is None:
        return None
    if existing["size"] == expected_size:
        print(
            f"    [업로드 확인됨] {asset_name} 는 이미 release에 정상적으로 올라가 있음 "
            "(이전 시도가 응답만 못 받고 실제로는 성공했던 것으로 보임)",
            flush=True,
        )
        return existing["browser_download_url"]
    print(
        f"    [찌꺼기 자산 발견] {asset_name} (용량 불일치: 서버 {existing['size']} vs "
        f"로컬 {expected_size}) -> 삭제 후 재업로드",
        flush=True,
    )
    _delete_asset(existing["id"])
    return None


def _advance_release() -> bool:
    """현재 release를 '가득 참'으로 보고 다음 번호로 넘어간다. 한도(MAX_RELEASE_INDEX)를 넘으면 False."""
    if _release_state["index"] >= MAX_RELEASE_INDEX:
        return False
    _release_state["index"] += 1
    _release_state["id"] = None
    _release_state["count"] = None
    return True


def _current_release_id() -> int | None:
    """지금 업로드에 쓸 release id를 반환. 로드/생성이 필요하면 하고,
    자산 개수가 RELEASE_SOFT_LIMIT 이상이면 자동으로 다음 번호로 넘어간다."""
    while True:
        if _release_state["id"] is None:
            loaded = _load_release(_release_state["index"])
            if loaded is None:
                return None
            _release_state["id"], _release_state["count"] = loaded

        if _release_state["count"] >= RELEASE_SOFT_LIMIT:
            print(
                f"    [release 가득 참] tag={_tag_for_index(_release_state['index'])} "
                f"({_release_state['count']}개) -> 다음 release로 넘어갑니다",
                flush=True,
            )
            if not _advance_release():
                print(f"    [release 번호 한도({MAX_RELEASE_INDEX}) 초과]", flush=True)
                return None
            continue

        return _release_state["id"]


def upload_release_asset(local_path: Path, asset_name: str, max_retries: int = 5) -> str | None:
    """PDF를 GitHub Release 자산으로 업로드하고 다운로드 URL을 반환한다.

    현재 release가 꽉 찼으면(미리 센 개수 또는 422 file_count 응답) 재시도 횟수를
    쓰지 않고 바로 다음 번호의 release로 바꿔서 다시 올린다.

    업로드가 실패한 것처럼 보이는 경우(422 already_exists, 또는 브로큰 파이프/커넥션
    끊김 같은 네트워크 에러) 대용량 파일은 실제로는 서버에 업로드가 끝났는데 그
    응답만 못 받아서 실패로 오인하는 경우가 흔하다. 그래서 어떤 이유로 실패하든,
    재시도하기 전에 먼저 release에 같은 이름의 자산이 이미 올라가 있고 용량까지
    일치하는지 확인한다 - 맞으면 재업로드 없이 그 URL을 그대로 쓴다.

    끝내 실패하면 None을 반환하며, 호출부는 이 메시지를 known으로 기록하지 않고
    넘어가서 다음 실행에서 자연스럽게 재시도하게 된다."""
    if not (GITHUB_REPOSITORY and GH_DISPATCH_TOKEN):
        print("    [release 업로드 건너뜀] GITHUB_REPOSITORY 또는 GH_DISPATCH_TOKEN이 없음", flush=True)
        return None

    file_size = local_path.stat().st_size
    attempt = 0
    rotations = 0
    reconcile_tries = 0  # already_exists 확인이 실패해서 재확인한 횟수 (무한루프 방지용 상한)
    MAX_RECONCILE_TRIES = 5

    while attempt < max_retries:
        release_id = _current_release_id()
        if release_id is None:
            return None

        upload_url = (
            f"https://uploads.github.com/repos/{GITHUB_REPOSITORY}/releases/"
            f"{release_id}/assets?name={urllib.parse.quote(asset_name)}"
        )

        try:
            with open(local_path, "rb") as f:
                req = urllib.request.Request(
                    upload_url,
                    data=f,
                    method="POST",
                    headers={
                        **_github_headers(),
                        "Content-Type": "application/pdf",
                        "Content-Length": str(file_size),
                        "Connection": "close",
                    },
                )
                with urllib.request.urlopen(req, timeout=900) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    _release_state["count"] = (_release_state["count"] or 0) + 1
                    print(
                        f"    [release 업로드 성공] {asset_name} "
                        f"(tag={_tag_for_index(_release_state['index'])}, {_release_state['count']}개째)",
                        flush=True,
                    )
                    return data["browser_download_url"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")
            if e.code == 422 and "file_count" in detail:
                # 개수를 잘못 셌거나 다른 곳에서 채워진 경우: 시도 횟수/대기 없이 바로 다음 release로.
                rotations += 1
                print(
                    f"    [release 자산 한도 도달] tag={_tag_for_index(_release_state['index'])} "
                    f"-> 다음 release로 전환 ({rotations}회째)",
                    flush=True,
                )
                if rotations > MAX_ROTATIONS_PER_UPLOAD or not _advance_release():
                    print(f"    [release 전환 한도 초과] {asset_name}", flush=True)
                    return None
                continue
            if e.code == 422 and "already_exists" in detail:
                print(
                    f"    [release 업로드 실패 (시도 {attempt + 1}/{max_retries})] "
                    f"HTTP {e.code} already_exists -> 실제로 이미 올라가 있는지 확인", flush=True,
                )
                reconciled = _reconcile_existing_asset(release_id, asset_name, file_size)
                if reconciled is not None:
                    _release_state["count"] = (_release_state["count"] or 0) + 1
                    return reconciled
                # 자산을 못 찾았거나(막 생성된 자산이 목록 API에 아직 안 뜬 경우) 찌꺼기라서
                # 지웠거나 - 어느 쪽이든 이 경로는 무한 루프로 새지 않도록 반드시 횟수와
                # 대기시간을 둔다. (예전 버그: 여기서 카운트/대기 없이 곧바로 continue 해서,
                # 자산 목록 반영이 늦어지면 이 파일 하나에 영원히 멈춰 뒤의 새 파일들이
                # 아예 처리되지 못했음 - 동기 코드라 이벤트 루프 전체가 막힘.)
                reconcile_tries += 1
                attempt += 1
                if reconcile_tries >= MAX_RECONCILE_TRIES:
                    print(f"    [already_exists 재확인 한도 초과] {asset_name} -> 이번 실행은 포기", flush=True)
                    break
                time.sleep(min(2 * reconcile_tries, 10))
                continue
            attempt += 1
            print(f"    [release 업로드 실패 (시도 {attempt}/{max_retries})] HTTP {e.code}: {detail}", flush=True)
        except Exception as e:
            # 브로큰 파이프/커넥션 리셋 등: 업로드 자체는 서버에 끝났는데 응답만
            # 못 받았을 가능성이 있으므로, 실패로 단정하기 전에 먼저 확인해본다.
            print(
                f"    [release 업로드 중 네트워크 오류 (시도 {attempt + 1}/{max_retries})] {e} "
                "-> 실제로 업로드가 됐는지 확인", flush=True,
            )
            reconciled = _reconcile_existing_asset(release_id, asset_name, file_size)
            if reconciled is not None:
                _release_state["count"] = (_release_state["count"] or 0) + 1
                return reconciled
            attempt += 1
            print(f"    [release 업로드 실패 (시도 {attempt}/{max_retries})] {e}", flush=True)
        time.sleep(min(2 ** attempt, 30))

    print(f"    [release 업로드 최종 실패] {asset_name}", flush=True)
    return None


# =========================================================================
# 여기부터는 sync2.py 전용 코드 (위쪽은 sync.py에서 그대로 가져온 부분)
# =========================================================================

_FILEDOWN_RE = re.compile(r"fn_fileDown\(\s*['\"]([0-9A-Za-z]+)['\"]\s*\)")
_AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac")


def log(msg: str) -> None:
    print(msg, flush=True)


# ---- 게시판 주소 ---------------------------------------------------------
def board_from_url(url: str, key: str, label: str) -> dict:
    """모평 게시판 목록 URL에서 boardID/m/s를 뽑아 board 설정으로 만든다."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    board_id = (q.get("boardID") or [""])[0]
    if not board_id:
        raise ValueError(f"MOPYEONG_LIST_URL에 boardID가 없습니다: {url}")
    return {
        "key": key,
        "label": label,
        "board_id": board_id,
        "m": (q.get("m") or [""])[0],
        "s": (q.get("s") or ["suneung"])[0],
    }


def list_url(board: dict, page: int) -> str:
    return (
        f"{BASE_URL}/boardCnts/list.do?type=default&page={page}&searchStr="
        f"&m={board['m']}&C06=&boardID={board['board_id']}&C05=&C04=&C03="
        f"&searchType=S&C02=&C01=&s={board['s']}"
    )


def file_url(seq: str) -> str:
    return f"{BASE_URL}/boardCnts/fileDown.do?fileSeq={seq}"


# ---- HTTP ----------------------------------------------------------------
def http_get(url: str, timeout: int = 60, retries: int = 4) -> bytes:
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # 429/5xx/네트워크 오류 모두 재시도
            last = e
            if attempt == retries:
                break
            wait = min(3 * (2 ** (attempt - 1)), 30)
            log(f"    [요청 실패 (시도 {attempt}/{retries})] {e} -> {wait}초 후 재시도")
            time.sleep(wait)
    raise RuntimeError(f"요청 최종 실패: {url} ({last})")


def decode_html(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("euc-kr", errors="replace")


# ---- 목록 페이지 파싱 ----------------------------------------------------
class BoardParser(HTMLParser):
    """게시판 표에서 (헤더, 행들)을 뽑는다. 행마다 셀 텍스트와
    fn_fileDown('<fileSeq>') 링크(파일명은 a의 title, 없으면 img alt)를 모은다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headers: list[str] = []
        self.rows: list[dict] = []
        self._cur = None
        self._cell = None
        self._cell_tag = None
        self._last_file = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._cur = {"cells": [], "th": [], "files": []}
        elif tag in ("td", "th") and self._cur is not None:
            self._cell, self._cell_tag = [], tag
        elif tag == "a" and self._cur is not None:
            m = _FILEDOWN_RE.search(a.get("onclick") or "") or _FILEDOWN_RE.search(a.get("href") or "")
            if m:
                self._last_file = {"seq": m.group(1), "name": (a.get("title") or "").strip()}
                self._cur["files"].append(self._last_file)
        elif tag == "img" and self._last_file is not None and not self._last_file["name"]:
            self._last_file["name"] = (a.get("alt") or "").strip()

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._cur is not None:
            text = " ".join("".join(self._cell).split())
            (self._cur["th"] if self._cell_tag == "th" else self._cur["cells"]).append(text)
            self._cell = None
        elif tag == "a":
            self._last_file = None
        elif tag == "tr" and self._cur is not None:
            if self._cur["th"] and not self._cur["cells"]:
                self.headers = self._cur["th"]
            elif self._cur["files"]:
                self.rows.append(self._cur)
            self._cur = None


def parse_list(html_text: str) -> tuple[list[str], list[dict]]:
    p = BoardParser()
    p.feed(html_text)
    return p.headers, p.rows


def row_fields(headers: list[str], cells: list[str]) -> dict:
    """헤더 이름으로 칸을 찾고, 헤더가 없으면 수능 게시판 기본 순서
    (번호, 학년도, 영역, 제목, 등록일, 조회, 파일)로 가정한다."""
    idx = {h: i for i, h in enumerate(headers)}

    def col(name: str, default: int) -> str:
        i = idx.get(name, default)
        return cells[i] if 0 <= i < len(cells) else ""

    return {
        "number": col("번호", 0),
        "year": col("학년도", 1),
        "subject": col("영역", 2),
        "title": col("제목", 3),
        "posted": col("등록일", 4),
    }


def parse_haknyeon(text: str) -> int | None:
    m = re.search(r"(20\d{2})", text or "")
    return int(m.group(1)) if m else None


def detect_exam(board: dict, cells: list[str], names: list[str]) -> str:
    """시험 이름. 제목/파일명에 6월·9월이 들어 있으면 모의평가, 아니면 게시판 기본값."""
    text = " ".join(cells) + " " + " ".join(names)
    m = re.search(r"(\d{1,2})\s*월", text)
    if m and int(m.group(1)) in (6, 9):
        return f"{int(m.group(1))}월 모의평가"
    return board["label"]


def file_kind(filename: str) -> str:
    for word, kind in (("문제", "문제"), ("정답", "정답"), ("해설", "해설"), ("대본", "대본")):
        if word in filename:
            return kind
    return "기타"


# ---- 발견(discover): 목록 페이지를 돌며 후보 파일 수집 ----------------------
def discover(boards: list[dict], min_year: int) -> list[dict]:
    cands: list[dict] = []
    for board in boards:
        log(f"[게시판] {board['label']} (boardID={board['board_id']})")
        prev_numbers = None
        for page in range(1, MAX_PAGES + 1):
            html_text = decode_html(http_get(list_url(board, page)))
            headers, rows = parse_list(html_text)
            if not rows:
                log(f"  page {page}: 파일이 있는 행이 없음 -> 종료")
                break
            numbers = [r["cells"][0] if r["cells"] else "" for r in rows]
            if numbers == prev_numbers:
                log(f"  page {page}: 이전 페이지와 동일 -> 마지막 페이지로 보고 종료")
                break
            prev_numbers = numbers

            years = []
            for row in rows:
                f = row_fields(headers, row["cells"])
                hak = parse_haknyeon(f["year"])
                years.append(hak)
                if hak is None:
                    log(f"  [경고] 학년도를 읽지 못해 건너뜀: {row['cells']}")
                    continue
                if hak < min_year:
                    continue
                names = [x["name"] for x in row["files"]]
                exam = detect_exam(board, row["cells"], names)
                if board["key"] == "mopyeong" and exam == board["label"]:
                    log(f"  [경고] 6월/9월을 판별하지 못함(시험='{exam}'): {row['cells']}")
                for fl in row["files"]:
                    cands.append({
                        "board": board["key"],
                        "haknyeon": hak,
                        "exam": exam,
                        "subject": f["subject"],
                        "title": f["title"],
                        "posted": f["posted"],
                        "seq": fl["seq"],
                        "name": unicodedata.normalize("NFC", fl["name"]),
                    })
            log(f"  page {page}: {len(rows)}행 확인 (학년도 {sorted({y for y in years if y})})")
            if years and all(y is not None and y < min_year for y in years):
                log(f"  {min_year}학년도 미만만 나와서 종료")
                break
            time.sleep(REQUEST_DELAY)
    return cands


# ---- 다운로드 / zip ------------------------------------------------------
class _TooBig(Exception):
    pass


def download_to(seq: str, dest: Path) -> bool:
    url = file_url(seq)
    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            total = 0
            with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_SIZE_BYTES:
                        raise _TooBig()
                    out.write(chunk)
            return True
        except _TooBig:
            dest.unlink(missing_ok=True)
            log(f"    [용량 초과로 건너뜀] fileSeq={seq}")
            return False
        except Exception as e:
            dest.unlink(missing_ok=True)
            if attempt == DOWNLOAD_MAX_RETRIES:
                log(f"    [다운로드 최종 실패] fileSeq={seq}: {e}")
                return False
            wait = min(3 * (2 ** (attempt - 1)), 30)
            log(f"    [다운로드 실패 (시도 {attempt}/{DOWNLOAD_MAX_RETRIES})] {e} -> {wait}초 후 재시도")
            time.sleep(wait)
    return False


def sniff(path: Path) -> str:
    """내용으로 종류 판별. 로그인/에러 HTML이 PDF로 올라가는 사고를 막는다.
    zip 시그니처를 먼저 본다: 압축 없이 저장된 zip은 안쪽 PDF 바이트가 앞부분에
    그대로 보여서, PDF 검사를 먼저 하면 zip을 PDF로 오인한다."""
    with open(path, "rb") as f:
        head = f.read(1024)
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if b"%PDF-" in head:
        return "pdf"
    return "other"


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    """zip 안 파일명 복원. UTF-8 플래그가 없는 zip(한국 윈도우 압축)은 cp437로
    잘못 읽히므로 원래 바이트로 되돌려 utf-8 -> cp949 순서로 해석한다."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except UnicodeEncodeError:
        return info.filename
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return info.filename


def extract_pdfs(zip_path: Path, seq: str) -> list[tuple[str, Path]]:
    """zip 안의 PDF를 WORK_DIR로 꺼낸다. [(원래 파일명, 로컬 경로)]. 경로 조작(zip-slip)은
    이름의 마지막 부분만 쓰는 것으로 막고, 압축 해제 총량에도 상한을 둔다."""
    out: list[tuple[str, Path]] = []
    with zipfile.ZipFile(zip_path) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i in infos) > MAX_SIZE_BYTES:
            log(f"    [zip 해제 용량 초과로 건너뜀] fileSeq={seq}")
            return out
        for n, info in enumerate(infos, 1):
            name = unicodedata.normalize("NFC", Path(_zip_member_name(info).replace("\\", "/")).name)
            if not name.lower().endswith(".pdf"):
                log(f"    (zip 안 PDF 아님, 건너뜀) {name}")
                continue
            target = WORK_DIR / f"{seq}_{n}.pdf"
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if sniff(target) != "pdf":
                log(f"    (zip 안 파일이 PDF 형식이 아님, 건너뜀) {name}")
                target.unlink(missing_ok=True)
                continue
            out.append((name, target))
    return out


# ---- manifest ------------------------------------------------------------
def load_manifest() -> dict:
    data = {"files": []}
    if MANIFEST_PATH.exists():
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    data.setdefault("files", [])
    data.setdefault("duplicate_keys", [])      # 내용이 같아서 저장 안 한 항목의 키
    data.setdefault("completed_zip_seqs", [])  # 안의 PDF를 전부 처리한 zip의 fileSeq
    return data


def save_manifest(manifest: dict) -> None:
    manifest["files"].sort(key=lambda f: (f.get("telegram_date") or "", f["message_id"]), reverse=True)
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def make_message_id(key: str, used: set[int]) -> int:
    mid = MESSAGE_ID_BASE + int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16)
    while mid in used:
        mid += 1
    used.add(mid)
    return mid


def build_asset_name(haknyeon: int, exam: str, filename: str) -> str:
    """같은 파일명(국어영역_문제지.pdf)이 해마다 반복되므로 학년도/시험을 앞에 붙인다."""
    raw = f"{haknyeon}학년도_{exam}_{filename}"
    raw = re.sub(r"\s+", "", raw)
    raw = re.sub(r'[\\/:*?"<>|#%&]', "_", raw)
    return unicodedata.normalize("NFC", raw)


class Ctx:
    def __init__(self, manifest: dict, auto_git: bool):
        self.manifest = manifest
        self.auto_git = auto_git
        self.known_keys = {f["source_key"] for f in manifest["files"] if f.get("source_key")}
        self.duplicate_keys = set(manifest["duplicate_keys"])
        self.done_zips = set(manifest["completed_zip_seqs"])
        self.known_hashes = {f["sha256"]: f for f in manifest["files"] if f.get("sha256")}
        self.used_ids = {f["message_id"] for f in manifest["files"]}
        self.used_assets = {f["asset_name"] for f in manifest["files"] if f.get("asset_name")}
        self.new_count = 0

    def commit(self, msg: str) -> None:
        save_manifest(self.manifest)
        if self.auto_git:
            git_commit_and_push(msg)


def store_unit(ctx: Ctx, cand: dict, filename: str, path: Path, key: str) -> str:
    """PDF 하나를 처리: 중복검사 -> 썸네일 -> Release 업로드 -> manifest/git.
    반환: 'stored' | 'dup' | 'skip' | 'fail'"""
    if key in ctx.known_keys or key in ctx.duplicate_keys:
        return "skip"

    size = path.stat().st_size
    sha = compute_sha256(path)
    if sha in ctx.known_hashes:
        orig = ctx.known_hashes[sha]
        log(f"  중복 파일 감지 (기존 '{orig['filename']}'와 내용 동일), 저장하지 않음: {filename}")
        ctx.duplicate_keys.add(key)
        ctx.manifest["duplicate_keys"].append(key)
        ctx.commit(f"chore: 모의고사 중복 스킵 - {filename} [skip ci]")
        return "dup"

    asset = build_asset_name(cand["haknyeon"], cand["exam"], filename)
    if asset in ctx.used_assets:  # 이름 충돌 시 다른 파일을 덮어쓰거나 지우지 않도록 구분자를 붙인다
        stem, dot, ext = asset.rpartition(".")
        asset = f"{stem}_{cand['seq'][:6]}{dot}{ext}"

    mid = make_message_id(key, ctx.used_ids)
    thumb_name = f"{mid}.png"
    thumb_ok = make_thumbnail(path, THUMBS_DIR / thumb_name)

    log(f"    (release로 업로드 중): {asset}")
    url = upload_release_asset(path, asset)
    if url is None:
        log(f"  건너뜀 (release 업로드 실패, 다음 실행에서 재시도): {filename}")
        (THUMBS_DIR / thumb_name).unlink(missing_ok=True)
        ctx.used_ids.discard(mid)
        return "fail"

    posted = cand["posted"] or ""
    entry = {
        "message_id": mid,
        "source_key": key,
        "source": "suneung.re.kr",
        "source_url": file_url(cand["seq"]),
        "category": "평가원",
        "filename": filename,
        "asset_name": asset,
        "stored_as": None,
        "download_url": url,
        "size_bytes": size,
        # index.html이 정렬/표시에 쓰는 sync.py 필드명과 맞춘다 (등록일 기준, KST 자정)
        "telegram_date": f"{posted}T00:00:00+09:00" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", posted) else datetime.now(timezone.utc).isoformat(),
        "posted_date": posted,
        "title": f"{cand['haknyeon']}학년도 {cand['exam']} {Path(filename).stem}",
        "year": str(cand["haknyeon"]),
        "exam": cand["exam"],
        "instructor": "한국교육과정평가원",
        "subject": cand["subject"] or None,
        "kind": file_kind(filename),
        "thumbnail": f"thumbnails/{thumb_name}" if thumb_ok else None,
        "sha256": sha,
    }
    ctx.manifest["files"].append(entry)
    ctx.known_keys.add(key)
    ctx.known_hashes[sha] = entry
    ctx.used_assets.add(asset)
    ctx.new_count += 1
    ctx.commit(f"chore: 모의고사 PDF 동기화 - {asset} [skip ci]")
    return "stored"


def handle_candidate(ctx: Ctx, cand: dict) -> None:
    seq, name = cand["seq"], cand["name"]
    low = name.lower()
    if "음원" in name or low.endswith(_AUDIO_EXTS):
        return
    if name and not low.endswith((".pdf", ".zip")):
        log(f"  (PDF/zip 아님, 건너뜀) {name}")
        return
    if seq in ctx.known_keys or seq in ctx.duplicate_keys or seq in ctx.done_zips:
        return

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = WORK_DIR / f"{seq}.download"
    log(f"  내려받는 중: {cand['haknyeon']}학년도 {cand['exam']} / {name or seq}")
    if not download_to(seq, tmp):
        return
    time.sleep(REQUEST_DELAY)

    kind = sniff(tmp)
    try:
        if kind == "pdf":
            filename = name if name.lower().endswith(".pdf") else f"{seq}.pdf"
            store_unit(ctx, cand, filename, tmp, seq)
        elif kind == "zip":
            units = extract_pdfs(tmp, seq)
            if not units:
                log(f"    [zip에서 꺼낼 PDF가 없음] {name}")
                return
            results = []
            for fname, p in units:
                try:
                    results.append(store_unit(ctx, cand, fname, p, f"{seq}#{fname}"))
                finally:
                    p.unlink(missing_ok=True)
            if all(r != "fail" for r in results):
                ctx.done_zips.add(seq)
                ctx.manifest["completed_zip_seqs"].append(seq)
                ctx.commit(f"chore: 모의고사 zip 처리 완료 - {name} [skip ci]")
        else:
            log(f"    [PDF/zip이 아닌 응답(에러 페이지?)이라 건너뜀] fileSeq={seq} ({name})")
    finally:
        tmp.unlink(missing_ok=True)


# ---- main ----------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="받을 목록만 출력하고 종료")
    ap.add_argument("--limit", type=int, default=0, help="새 PDF를 이 개수만 처리하고 종료 (0=제한 없음)")
    ap.add_argument("--min-year", type=int, default=MIN_HAKNYEON, help="이 학년도 이상만")
    ap.add_argument("--board", choices=["all", "suneung", "mopyeong"], default="all")
    args = ap.parse_args()
    dry = args.dry_run or os.environ.get("DRY_RUN", "").lower() in ("1", "true")
    limit = args.limit or int(os.environ.get("MOCK_LIMIT", "0") or 0)

    boards = []
    if args.board in ("all", "suneung"):
        boards.append(SUNEUNG_BOARD)
    if args.board in ("all", "mopyeong"):
        if MOPYEONG_LIST_URL:
            boards.append(board_from_url(MOPYEONG_LIST_URL, "mopyeong", "모의평가"))
        elif args.board == "mopyeong":
            log("MOPYEONG_LIST_URL이 비어 있습니다. 모평 목록 페이지 주소를 환경변수로 넣어주세요.")
            return 1
        else:
            log("[안내] MOPYEONG_LIST_URL이 없어 6월·9월 모평 게시판은 건너뜁니다 (수능 게시판만 진행).")

    if not dry and not (GITHUB_REPOSITORY and GH_DISPATCH_TOKEN):
        log("GITHUB_REPOSITORY / GH_DISPATCH_TOKEN 환경변수가 필요합니다 (확인만 하려면 --dry-run).")
        return 1

    auto_git = (os.environ.get("GITHUB_ACTIONS") == "true") and not dry
    if auto_git:
        _run_git("config", "user.name", "github-actions[bot]")
        _run_git("config", "user.email", "github-actions[bot]@users.noreply.github.com")

    start = time.monotonic()
    manifest = load_manifest()
    ctx = Ctx(manifest, auto_git)

    cands = discover(boards, args.min_year)
    pending = [
        c for c in cands
        if not ("음원" in c["name"] or c["name"].lower().endswith(_AUDIO_EXTS))
        and c["seq"] not in ctx.known_keys and c["seq"] not in ctx.duplicate_keys and c["seq"] not in ctx.done_zips
    ]
    log(f"\n후보 파일 {len(cands)}개 중 새로 처리할 것 {len(pending)}개 (이미 처리했거나 음원이라 제외된 것 {len(cands) - len(pending)}개)")

    if dry:
        for c in pending:
            tag = " (zip: 풀어서 안의 PDF 각각 업로드)" if c["name"].lower().endswith(".zip") else ""
            log(f"  [예정] {c['haknyeon']}학년도 | {c['exam']} | {c['subject']} | {c['name'] or '(이름없음)'}{tag} | fileSeq={c['seq']}")
        log("\n--dry-run: 아무것도 내려받거나 업로드하지 않았습니다.")
        return 0

    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        for c in pending:
            if time.monotonic() - start > MAX_RUNTIME_SECONDS:
                log("실행 시간 제한 도달, 나머지는 다음 실행에서 이어받습니다.")
                break
            if limit and ctx.new_count >= limit:
                log(f"--limit {limit} 도달, 여기서 종료합니다.")
                break
            try:
                handle_candidate(ctx, c)
            except Exception as e:  # 파일 하나의 오류가 전체를 멈추지 않게
                log(f"  [처리 중 오류, 건너뛰고 계속] fileSeq={c['seq']}: {e}")
    finally:
        shutil.rmtree(WORK_DIR, ignore_errors=True)
        if auto_git and _has_unpushed_commits():
            log("종료 전 마지막 push 재시도...")
            _push_with_retry()

    log(f"이번 실행 종료. 새로 올린 PDF: {ctx.new_count}개 (manifest 전체 {len(ctx.manifest['files'])}개)")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"new_count={ctx.new_count}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
