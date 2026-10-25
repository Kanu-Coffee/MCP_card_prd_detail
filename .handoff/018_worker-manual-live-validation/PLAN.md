# 018 — Worker 운영 적용 및 수동 8사 라이브 검증

사용자 요청: 017 ZIP 운영 문서를 정정하고 운영 적용 준비를 마친다. 사용자가 한 카드사씩 수동 실행·모니터링하여 8사를 검증한 뒤 예약을 맞춘다. 실제 origin 배치는 사용자가 시작하며 이번 준비 단계에서 선행 실행하지 않는다.

- 017에서 검토 완료한 dirty worktree를 식별 가능한 운영 이미지로 고정한다. 변경 없는 기존 운영 runtime/dependency를 재사용하고 worker/core 소스만 갱신한다. 임의 commit/push는 하지 않는다.
- 기존 실제 external state/auth/model/serving 볼륨, 비밀 mount, OCR/embedding/백업 계약을 보존한다. SQLite를 worker lock 아래 online backup으로 보관한다.
- ZIP은 unsupported 오류로 격리하며 압축 해제하지 않고 정상 상품을 계속 처리하는 정책을 OPERATIONS에 반영한다.
- 수동 검증 동안 timer를 중지한다. 시스템 서비스가 실행 중이거나 timer가 활성인 채로 전환/수동 배치를 시작하지 않는다.
- 새 release/host-local private runtime overlay와 activation·카드별 detached 실행·로그·결과 helper를 준비한다. 실제 프록시 주소/SSH/VPN/key를 Git에 추가하지 않는다.
- manual --issuer는 자동 cursor를 소비하지 않는다. same-day --round-robin 반복은 같은 slot이므로 8사 수동 검증에는 쓰지 않는다. 자동 순서는 woori/kb/shinhan/samsung/hyundai/hana/lotte/bc이며 KST 매일 03시 timer 재개는 8사 평가 이후 사용자 수행 단계다.
- 카드별 실행 결과에 selected scope, 성공/실패, 비선택 manifest document 변경 0, 8사 serving 보존, 신규 문서/OCR, ZIP 오류, 증분 백업을 확인한다. 기존 retained seal이 GC되기 전에 host에 각 수동 실행의 결과를 보관한다.
- 준비 완료와 운영 전환/실제 8사 정상 판정을 구분한다. 권한으로 전환이 불가능하면 재현한 거부 원인과 정확한 최종 실행 명령을 제출한다.
