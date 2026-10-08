# 012 PLAN — v1.0.34 발행과 운영 반영

2026-10-08 사용자 승인: 운영 반영·버전 증가·커밋·main 병합·GitHub Release 완료.

## 범위

011 ACCEPTANCE의 runtime 72c28ac 및 이후 인수 문서를 포함하여 v1.0.34로 발행한다. 009/010 이후의 stage skip도 main에 포함되어 있다. 패키지 3개·lock·README·release notes와 버전별 release 검증기를 맞춘다. 기존 공개 이력을 보존한다.

## 구현·검증·발행

1. 버전별 gate를 v1.0.34로 갱신하고 qualification 스키마는 기존32/33도 계속 검증한다. gate 자체를 우회하지 않는다.
2. PR CI 성공 후 main 병합한다. 정확한 source commit의 원격 Git context로 Worker/MCP OCI 후보를 고정 BuildKit/Syft와 provenance v0.2로 빌드한다.
3. 실제 CI와 후보 digest를 경량 qualification JSON으로 봉인한다. 봉인 commit은 해당 evidence 1개만 변경한다. CI 후 annotated tag를 생성하고 공식 release workflow를 실행한다. 이미지·GitHub 자산을 원격 재검증한다.
4. /opt/cardrag 새 source snapshot의 host overlay·wrapper는 기존 canonical 이름과 secret 경로를 유지한다. 운영 Worker 이미지는 현행009를 유지하고 MCP만 새 공개 digest로 교체한다. 기존 state 볼륨을 복제하지 않는다.
5. Worker 비실행 확인 후 timer를 일시 중지하고 MCP 교체·readiness 최대15분·인증된 실제 상품/같은 revision bundle smoke·LibreChat 중계 확인·timer 복원을 수행한다. 실패하면 이전009로 복원한다. sudo 인증이 불가하면 준비 완료된 root script를 사용자에게 제공한다.
6. 정상 반영 후 직전009 rollback 1세트 보존. 활성 Worker009는 삭제하지 않는다. 더 오래된008 등은 참조 확인 후 정리한다.

## 인수 기준

main CI/Release success, 버전34 일치, 자산checksum/digest 일치. 운영은 신고4건·연회비3건·500107 인증 HTTP 확인과 timer active가 필요하다. OCR/임베딩/전량 Worker/새벽2회는 실행하지 않는다. 수행하지 않은 공개 후보 전체 런타임 검증은 미수행으로 명시한다.
