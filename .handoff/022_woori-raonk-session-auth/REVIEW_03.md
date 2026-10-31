# 022 FIX_06 리뷰와 빌드 잔여물 정리

작성일: 2026-10-31 KST  
역할: Reviewer

## 판정

GitHub 정식 v1.0.38 발행과 소스 검증은 확인했다. 운영 완료 인수는 OCI image 출처 label 및 최신 복구점/rollback guidance 정합성 2건을 보완한 뒤 확정한다. 구체 증거와 Executor 요구는 신규 FIX_07.md에 있다. 이번 리뷰에서 GitHub나 운영 version을 재발행·변경하지 않았다.

Reviewer 직접 전체 테스트 **2,893 passed / 11 warnings**, Ruff check 및 format check(244 files), diff check 통과. PR CI `41143931129`, merge commit push CI `41144473379` 모두 success 확인. 실제 tag는 annotated이고 merge commit `99b5ba69fb5907be2218873c10bbb945d6181dcd`를 가리킨다. GitHub release는 공개 정식 상태다.

운영 Worker package는 1.0.38이며 실제 image의 Worker Python source 전부가 tag source와 일치했다. 실행 중 MCP package는 1.0.34이고, 1.0.38 MCP는 build-only다. 기존 MCP를 유지하면서 새 generation을 읽는 경로는 정상이다. 실효 Worker compose는 `cardrag-worker:v1.0.38`, startup은 ready/warning(.99745655 cosine), timer active/enabled, MCP와 고객 web/edge는 healthy였다. 다음 10:00 예약 배치 완주는 이 리뷰에서 관측하지 않았다.

## 디스크 진단과 정리

시작 시 root 가용량: `78,577,889,280` bytes (약 78.58GB).

배포 빌드 후 Docker build cache가 24.58GB로 늘었고, CI 전용 Worker/MCP 이미지와 실제 v1.0.38 이미지가 별도로 존재했다. 이들 CI 이미지는 어떤 container에서도 참조하지 않았다. 운영 snapshot은 단일 022만 보존된 상태였으므로 이번에는 snapshot이나 state/serving generation을 삭제하지 않았다.

실행 중 build를 확인한 뒤 다음 표적 정리를 수행했다.

1. `docker builder prune -f`: 미사용 build cache **8.197GB** 회수.
2. `uv cache prune`: 미사용 26개 파일, **57.1KiB** 회수.
3. `docker image rm cardrag-ci:worker cardrag-ci:mcp`: 재생성 가능한 CI 전용 두 이미지 제거. image IDs는 `0e0a298070b92de74a7ac2c9afd83b19e937809649b911cd039fadb69d4cd309`, `cec9dfce4b681437e2f1240b2cfefbe457b1d5828a750918c9da2bb9f66de91c`.
4. CI image 참조 해제 후 `docker builder prune -f`: 뒤에 남은 미사용 빌드 층 **2.320GB** 추가 회수.

최종 관측 root 가용량: `90,723,020,800` bytes (약 **90.72GB / 84.49GiB**), 순증 `12,145,131,520` bytes (약 **12.15GB**). background 작업과 테스트 임시 파일 때문에 bytes는 이후 소폭 변할 수 있다.

Docker build cache는 14.06GB까지 감소했으며 남은 private reclaimable은 약35.76MB다. 공유 이미지 층의 표기 크기를 즉시 회수 가능한 공간으로 해석하지 않는다. 운영 v1.0.38 Worker/MCP 이미지, 현재 MCP 이미지, rollback 022/021/020 이미지, 타 프로젝트 local images는 유지했다. `--all`, image/volume 전역 prune, DB/VACUUM·원본 OCR/PDF cache 삭제는 수행하지 않았다.

정리 후 실제 live MCP에서 OCR page 읽기 두 건과 current generation 일치를 확인했다. stable/MCP 모두 `g-df29d8e5dbb34048942a5296-5a3a99224f92`를 유지하며 timer도 active다. 용량이 확보됐다는 사실은 다음 배치 peak capacity 검사를 대체하지 않는다.

## 다음 단계

Executor가 FIX_07의 두 운영 정합성 항목을 기존 사용자 승인 범위에서 보완하고 FIX_07_REPORT.md로 인계한다. source release나 이미 성공한 우리카드 run을 다시 수행할 필요는 없다. 보호 snapshot 1개 정책 및 timer 유지 지시는 계속 적용한다.
