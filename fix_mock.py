#!/usr/bin/env python3
"""fix_mock.py - sites/manifest_mock.json에 표기된 년도를 -1 한다.

이유: 평가원은 '2026학년도 수능'이라고 부르지만 실제 시행은 2025년이다.
      그래서 year 필드만 N-1 로 내린다 (title 등 다른 필드는 그대로 둔다).

- 이미 고친 항목은 "year_shifted": true 로 표시해서 건너뛴다 (여러 번 실행해도 안전).
- sync2.py가 새로 저장하는 항목은 처음부터 -1 된 값 + year_shifted 로 들어가므로 건드리지 않는다.
- message_id / source_key / asset_name / download_url 은 바꾸지 않는다 (링크·중복검사 유지).

사용법:
  python fix_mock.py --dry-run   # 바뀔 내용만 출력
  python fix_mock.py             # 파일 수정 (커밋/푸시는 fix_mock.yml이 한다)
"""
import argparse
import json
import sys
from pathlib import Path

MANIFEST_PATH = Path("sites") / "manifest_mock.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 바뀔 내용만 출력")
    args = ap.parse_args()

    if not MANIFEST_PATH.exists():
        print(f"{MANIFEST_PATH} 가 없습니다. 할 일 없음.")
        return 0

    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    changed = skipped = 0

    for f in data.get("files", []):
        if f.get("year_shifted"):
            skipped += 1
            continue
        try:
            old_year = int(str(f.get("year", "")).strip())
        except ValueError:
            print(f"  [건너뜀] year를 읽을 수 없음: message_id={f.get('message_id')} year={f.get('year')!r}")
            continue

        new_year = old_year - 1
        print(f"  {old_year} -> {new_year} | {f.get('title')}")
        f["year"] = str(new_year)
        f["year_shifted"] = True
        changed += 1

    print(f"변경 {changed}개, 이미 처리됨 {skipped}개")
    if changed and not args.dry_run:
        MANIFEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{MANIFEST_PATH} 저장 완료")
    elif args.dry_run:
        print("(dry-run: 파일은 수정하지 않았습니다)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
