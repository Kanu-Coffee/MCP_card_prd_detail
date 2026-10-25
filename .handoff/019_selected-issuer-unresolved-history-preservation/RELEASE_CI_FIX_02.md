# v1.0.37 이미지 보안 갱신 및 재검증

2026-10-25 Executor. CI40192392151은단위/타입/Compose/systemd를모두통과했으나Worker이미지scan에서수정가능HIGH/CRITICAL을검출했다. scanner/ignore정책을약화하지않고release의존성을수정했다.

- aiohttp3.14.3→3.14.5, certifi2026.7.22→2026.10.23, filelock3.32.4→4.2.4, Pillow12.3.0→13.0.0. uv의기존lock에서해당4개만upgrade했으며rootconstraint의보안floor와WorkerPillow지원범위를맞췄다.
- [certifi안내](https://github.com/advisories/GHSA-6q48-6c7w-9gw2)는2026.10.19수정, [aiohttp안내](https://github.com/advisories/GHSA-2g87-pp6c-29x5)는3.14.4수정, [Pillow공식노트](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)에서13.0.0보안수정을확인했다. filelock4.2.1/OSCA20260909-r3의수정floor는CITrivy증거를따른다.
- Worker최종stage가ca-certificates-bundle>=20260909-r3을명시적으로설치한다. MCP는기존Python3.14base를그대로쓰고runtime-security빌드단계에서원래minimal runtime전체root에대한실제signed APK package upgrade를수행한후그root를복사한다. 최종stage에는RUN/셸/패키지관리자를추가하지않는다. APKmetadata를임의수정하지않는다.
- CA데이터만교체하므로실행파일링커처리가필요없으며shell-free target에서실행불가능한ldconfig commit hook만--no-commit-hooks로생략한다. 패키지서명검증/파일설치/원장갱신은정상수행한다.
- 최신ChainguardPython이미지를조사했으나3.15임을실제실행확인하여교체하지않았다. 모든기존base digest와프로젝트PythonABI유지. 조사한latest digest나기존v1.0.34전용provenance조건을새버전증거로쓰지않는다.
- 갱신env전체pytest2836passed/11warnings/84.75초. type115파일,컨테이너/workspace계약17tests,hadolint/actionlint/lock/diff통과. 실제runtime-security이미지빌드성공, CA20260611-r0→20260909-r3및실제원장/신뢰인증서읽기검증성공. 원격전체CI에서최종Worker/MCP이미지scan을다시확인한다.
- localcheck image cardrag-release-check:v1.0.37-ca, immutable ID는로컬Docker에존재한다. 운영이미지/볼륨/실제예약은변경하지않았다. 운영테스트성공을새third-party버전의실배치증거로표기하지않는다.
