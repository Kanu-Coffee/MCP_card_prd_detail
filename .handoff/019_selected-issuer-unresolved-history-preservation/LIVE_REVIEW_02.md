# 019 수정 배포 후 선택 백업 테스트 — 보류 2건 회수, 별도 variant 충돌 확인

2026-10-20 Codex. 사용자 제공 `backup status/flush --run 41ce678e665b47179f8bfb2d82273882 --issuer woori` 결과와 실제 원장/원격 객체를 대조했다. 운영 release는 `/opt/cardrag/019-ba273106158e`다.

## 테스트 결과

- 사용자의 flush가 기존 manifest1791B/READY285B, 총2076B를 처리했다. pending2→0, lost0, processed/flushed2, requests5, error=null이다.
- Reviewer 후속 GET 대조에서 key `452c10444aa6e815dd85d14b47d7298162c579b66d52bd9a0c0eb41e3a3f01e1`의 두 파일은 원격 바이트 SHA/size가 receipt 및 선택 expected와 일치했고, committed backup index에도 동일 digest로 존재했다. manifest/READY의 canonical/짝 연결도 정상이다. **원래 누락된 2건의 회수는 확인됐다.**
- `backup_scope_complete=false`, conflicts2/missing2는 별도의 key `94cdb144c916a6cae490f6cae6fe89139bf6dfc3f7ec32643149261efa73352f`의 두 파일 때문이다. missing2는 실제 원격 파일 부재가 아니라 현재 expected와 일치하는 완료 근거의 부재다.

## 별도 충돌 원인

원격 manifest/READY는 모두 존재하고 기존 receipt SHA/size와 일치한다. 모델 검증과 canonical 바이트, READY의 manifest/OCR hash 연결도 정상이다. local/remote는 source와 contract가 같지만 OCR output·ocr_chars·page_output_sha256·created_at가 다르다.

- local variant created_at: `2026-10-14T23:41:26.846090+00:00`.
- remote variant created_at: `2026-10-14T18:39:33.396353+00:00`.
- 현재 seal OCR은 local variant와 일치하고 remote variant와는 다르다.
- manifest SHA local `1aabcc1159576caa8a3f1839789d120dbb9fe0d0ed0edcbe44d71b5f253d1fff`, remote/receipt `7ec3b38f7c19d0db6079e775c8bfdb1ee3e78682ccde5093eb7ef10ffc3e7a9a`.

따라서 receipt 위조/원격 손상이나 created_at만 다른 동일 결과가 아니다. native cache key가 입력/계약에 묶여 있어 서로 다른 정상 결과 variant가 같은 cache 경로를 공유하는 사례다. 현재 generation-only 결과를 그 canonical cache 경로에 백업하려는 설계가 충돌한다. 기존 원격 variant를 덮어쓰거나 현재 serving OCR을 과거 variant로 바꾸면 안 된다.

사용자 flush 후 이 충돌 두 항목은 backup index에서 빠져 있지만 원격 객체 및 receipt는 남아 있다. 기존 index 필터가 불일치 항목을 제거한 영향이다. 현재 variant metadata의 복구 가능성과 기존 유효 variant의 index 보존을 함께 보완해야 한다. 같은 flush를 반복해도 해결되지 않는다.

## 검토 경계와 후속

후속 audit는 SQLite `mode=ro`와 원격 GET만 사용했다. 제품 코드·원장·원격 객체/index·serving을 추가 수정하지 않았다. 새 배치/provider 또는 timer를 시작하지 않았다. 증거는 host `operations/postflush-readonly-audit.json`에 있다.

Compose orphan 경고의 세 컨테이너는 보존 중인 이전 실행 기록이며 이번 전송 실패 원인이 아니다. `--remove-orphans`로 삭제하지 않았다. `actual_uploaded_* = null`은 해당 client가 카운터 속성을 제공하지 않는 출력이며 전송 실패의 근거가 아니다. 전송 성공은 원격 바이트/receipt/index 대조로 확인했다.

원래 보류 회수는 성공했지만 선택 전체 백업은 아직 미완료다. 별도 FIX_12에서 충돌 variant의 백업 보존을 다룬다. REVIEW_02의 로컬 코드 검토 통과를 운영 전체 완료로 확대하지 않는다.
