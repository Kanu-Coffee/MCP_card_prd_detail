# v1.0.37 릴리스 전 로컬 검증

2026-10-25 Executor. 사용자 운영테스트 인수 후 source release를 준비했다. 전체진행상태는../STATUS_v1.0.37.md이며기존handoff문서는덮어쓰지않았다. 원격CI/머지/릴리스는후속실제증거로추가기록한다.

- 패키지3종/Worker표시버전/uv.lock/workspace검사1.0.37동기화. uv lock --check 통과; 외부의존성변경없음.
- strict mypy에서기존미커밋기능의누락type90건을확인해구체적주석·분기별변수명·검증된reason literal/부분PDF재사용경계cast로정리했다. 전체mypy115파일통과. 타입검사설정이나CI기준을낮추지않았다.
- 최종전체pytest2836 passed,11 warnings,85.61초. /tmp/cardrag-v1037-tests-final.log. 앞선버전변경후전체검사도2836passed/87초로통과했으며type정리후다시전체검사했다.
- Ruff전체lint및format244files통과. actionlint/shellcheck통과. timer calendar CI검사도실제14:30으로갱신했다.
- staged patch2.22MB gitleaks통과. 019인수자료의공개backup/OCR digest3개오탐은정확한secret값일치+generic-api-key규칙에만한정해allowlist했다. 다른값/자격증명규칙을제외하지않았다. 무시되는과거운영로그까지스캔하는directory진단은이번릴리스변경/committed source검사와구분한다.
- 코드의diff whitespace검사통과. 신규추적되는기존handoff의3줄Markdown hard-break공백(FIX_22_REPORT.md두줄,옛README.md한줄)은원문보존정책에따라그대로뒀으며자동정리로역사를수정하지않았다.
- 운영이미지/볼륨/데이터/인증/serving/MCP/14:30timer는변경하지않았다. source버전상승과운영실행중image버전은구분한다. 공개이미지전용workflow는사용하지않는다.

다음단계는원격PR/CI/머지/annotated v1.0.37 tag/GitHub source release와실제결과추가기록이다.
