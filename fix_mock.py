#!/usr/bin/env python3
"""fix_mock.py - sites/manifest_mock.json 보정.

1) 제2외국어/한문 영역 항목 삭제 (manifest 항목 + 썸네일 + 선택적으로 release 자산)
2) year 필드만 -1  (2026학년도 수능은 2025년 시행이므로 2025로 표기. title 등 다른 필드는 그대로)

- 이미 고친 항목은 "year_shifted": true 로 표시해서 년도를 다시 내리지 않는다 (여러 번 실행해도 안전).
- sync2.py는 같은 규칙(SECOND_LANG_RE)으로 제2외국어/한문을 애초에 받지 않으므로,
  여기서 지운 항목이 다음 sync2 실행에서 되살아나지 않는다.
- 년도 보정에서 message_id / source_key / asset_name / download_url 은 바꾸지 않는다.

사용법:
  python fix_mock.py --dry-run                 # 바뀔 내용만 출력
  python fix_mock.py                           # manifest/썸네일 수정 (커밋/푸시는 fix_mock.yml)
  python fix_mock.py --delete-assets           # + release 자산도 삭제 (GH_TOKEN 필요)
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SITE_DIR = Path("sites")
MANIFEST_PATH = SITE_DIR / "manifest_mock.json"

# sync2.py의 SECOND_LANG_RE와 같은 규칙으로 유지할 것.
SECOND_LANG_RE = re.compile(
    r"제\s*2\s*외국어|한문|독일어|프랑스어|스페인어|중국어|일본어|러시아어|아랍어|베트남어"
)


def is_second_language(f: dict) -> bool:
    text = " ".join(str(f.get(k) or "") for k in ("subject", "filename", "title", "asset_name"))
    return bool(SECOND_LANG_RE.search(unicodedata.normalize("NFC", text)))


# ---- release 자산 삭제 ----------------------------------------------------
def _headers() -> dict:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GH_DISPATCH_TOKEN") or ""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _api(url: str, method: str = "GET"):
    req = urllib.request.Request(url, method=method, headers=_headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else None


def delete_release_asset(download_url: str) -> bool:
    """download_url(https://github.com/OWNER/REPO/releases/download/TAG/NAME)에 해당하는 자산을 지운다."""
    m = re.match(r"https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/(.+)$", download_url or "")
    if not m:
        print(f"    [자산 삭제 건너뜀] release URL이 아님: {download_url}")
        return False
    repo, tag, _name = m.groups()
    target = urllib.parse.unquote(download_url)
    try:
        rel = _api(f"https://api.github.com/repos/{repo}/releases/tags/{tag}")
        for page in range(1, 51):
            items = _api(f"https://api.github.com/repos/{repo}/releases/{rel['id']}/assets?per_page=100&page={page}")
            for it in items:
                if urllib.parse.unquote(it["browser_download_url"]) == target:
                    _api(f"https://api.github.com/repos/{repo}/releases/assets/{it['id']}", "DELETE")
                    print(f"    [자산 삭제됨] {it['name']} (tag={tag})")
                    return True
            if len(items) < 100:
                break
        print(f"    [자산 없음(이미 삭제됨?)] {download_url}")
        return False
    except Exception as e:
        print(f"    [자산 삭제 실패] {download_url}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 바뀔 내용만 출력")
    ap.add_argument("--delete-assets", action="store_true", help="삭제 대상의 release 자산도 지운다 (GH_TOKEN 필요)")
    args = ap.parse_args()

    if not MANIFEST_PATH.exists():
        print(f"{MANIFEST_PATH} 가 없습니다. 할 일 없음.")
        return 0

    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    files = data.get("files", [])

    # 1) 제2외국어/한문 삭제
    removed = [f for f in files if is_second_language(f)]
    kept = [f for f in files if not is_second_language(f)]
    print(f"[제2외국어/한문 삭제] 대상 {len(removed)}개")
    for f in removed:
        print(f"  삭제: {f.get('title') or f.get('filename')} | subject={f.get('subject')!r}")
        if args.dry_run:
            continue
        thumb = f.get("thumbnail")
        if thumb:
            (SITE_DIR / thumb).unlink(missing_ok=True)
        if args.delete_assets and f.get("download_url"):
            delete_release_asset(f["download_url"])
    data["files"] = kept

    # 2) year 필드만 -1
    changed = skipped = 0
    for f in kept:
        if f.get("year_shifted"):
            skipped += 1
            continue
        try:
            old_year = int(str(f.get("year", "")).strip())
        except ValueError:
            print(f"  [건너뜀] year를 읽을 수 없음: message_id={f.get('message_id')} year={f.get('year')!r}")
            continue
        print(f"  {old_year} -> {old_year - 1} | {f.get('title')}")
        f["year"] = str(old_year - 1)
        f["year_shifted"] = True
        changed += 1
    print(f"[년도 -1] 변경 {changed}개, 이미 처리됨 {skipped}개")

    if args.dry_run:
        print("(dry-run: 파일은 수정하지 않았습니다)")
    elif removed or changed:
        MANIFEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{MANIFEST_PATH} 저장 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
