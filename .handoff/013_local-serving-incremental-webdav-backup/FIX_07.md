# 013 FIX_07 — FIX_05/06 인수 보완

작성: 2026-10-09, Reviewer / Codex. 기준 commit `6004526`.

## 판정과 독립 근거

local content 조회와 native-only prior 제한 제거로 불필요한 provider 호출을 피하는 경로는 확인했다. 관련 테스트50건 및4파일 mypy가 통과했다. 다만 아래 보완 후 최종 인수한다. 새 전체 Worker/유료 OCR/임베딩 재생성/2일 관찰은 필요하지 않다.

Reviewer 명령:

```sh
uv run --no-sync --all-packages pytest apps/cardrag-worker/tests/test_fix_05_06_local_content_and_backup.py apps/cardrag-worker/tests/test_content_cache.py apps/cardrag-worker/tests/test_ocr_prefetch.py apps/cardrag-worker/tests/test_pipeline_v5.py apps/cardrag-worker/tests/test_local_serving_and_backup.py -q
# 50 passed, 1 failed, 1 warning / 3.19s
uv run --no-sync --all-packages mypy apps/cardrag-worker/src/cardrag_worker/content_cache.py apps/cardrag-worker/src/cardrag_worker/ocr.py apps/cardrag-worker/src/cardrag_worker/backup.py apps/cardrag-worker/src/cardrag_worker/pipeline.py
# Success, 4 files
uv run --no-sync --all-packages python .handoff/013_local-serving-incremental-webdav-backup/evidence/reviewer-fix06-repro.py
```

근거: evidence/reviewer-fix06-independent.json.현재 MCP healthy, Worker 정지. 이번 Reviewer는 운영 컨테이너·자료·설정을 변경하지 않았다.

## 1. 기존 캐시의 variant/provenance 보존

`content_cache.py:_lookup_local`은 이전에 확정된 variant를 유지하지 않고 `ContentOCRArtifactManifest.create`로 새로운 variant를 만든다. created_at이 없는 일반 generation에는 현재 시각을 사용하므로 동일 run·동일 입력을 두 번 조회해도 variant ID가 바뀐다. generation_id에는 실제 generation ID 대신 run 이름을 넣는다.

`ocr.py`는 이 lookup 결과를 prior native/sealed 처리보다 먼저 반환하므로 `PriorLocalNativeSource`에 알려진 provider/model/variant가 있어도 사라진다. 독립 fixture에서 provider0/cache hit 상태로 기존 variant `c*64`와 다른 variant, provider `local-paddleocr`→`generation-only`, model `PaddleOCR-VL-1.6`→`unrecorded`, 반복 조회 variant 불일치를 재현했다.

수정: 검증된 prior sealed identity와 원래 variant/provider/model을 유지한다. 로컬 캐시 반환값에 기존 binding을 명시적으로 전달하거나, 검증된 prior lookup을 우선하는 등 구체적인 방법은 Executor가 정한다. 원래 manifest가 없는 자료의 미상 provider를 억지로 채울 필요는 없지만, 알려진 정보를 덮어쓰면 안 된다. 실제 generation_id를 유지하고 동일 run selection/resume에서 identity가 바뀌지 않아야 한다. 임의 JSON을 sealed로 간주하지 않도록 기존 seal 검증과 결합하고 CAS/PDF/page/expected OCR identity 검사를 유지한다.

## 2. cache epoch와 입력 일치

현재 `pdf_matches`만으로 기존 content reuse_key 불일치를 무시하고, manifest 재생성 시 cache_epoch=0을 고정한다. 독립 fixture에서 epoch0 결과가 epoch1 lookup에서도 hit하고, 반환 reuse_key 역시 epoch0이었다.

수정: 요청한 epoch/reuse key의 불일치를 캐시 경계로 처리한다. content/native/adopted 전환 fallback의 허용 조건을 명시하고 epoch 갱신이나 명시적 reprocess를 무효화하지 않는다. PDF SHA/size/pages 전체가 일치하는 자료만 사용하며 key만 같다고 서로 다른 PDF metadata를 허용하지 않는다. epoch0→1 miss와 정상 epoch0 hit/provider0을 검증한다.

## 3. 백업 실제 전송량 표시와 예산

`backup.py`는 transfer helper 성공 시 무조건 actual_uploaded_count/bytes에 expected_size를 더한다. 실제 `WebDAVClient.put_bytes`는 기존 객체를 HEAD/GET로 확인한 경우에도 성공을 반환하므로 새 actual_*도 실제 업로드량이 아니다. FIX_05의 지표 문제는 이름 변경만으로 해결되지 않았다.

수정: 실제 PUT 관측을 반환하는 transport 결과/측정값이 있으면 사용한다. 이를 측정할 수 없으면 processed/verified bytes로 표시하고 actual_*는 미측정 상태로 반환해도 된다. GET만 수행한 자료를 실제 업로드로 표시하지 않는다. control JSON과 OCR/PDF 본체를 구분한다.

commit reserve 추가는 적절한 방향이다. 다만 새 고정 item30초 timeout은 기존 transfer 설정과 무관하다. 정상적으로 느린 객체가 매번 선두에서 timeout되는 일이 없도록 잔여 data 예산 및 기존 transport 설정과의 관계를 정리한다. 짧은 전체 예산에서도 commit에 최소15초를 부여하여 총 예산을 넘기는 구현의 한계는 조정하거나 정확하게 보고한다. 무제한 연장은 하지 않는다.

## 4. 실패한 테스트와 보고서

`test_backup_budget_reserve_commits_partial_batch_and_retry_zero_requests` 마지막 assert가 실패했다. put_bytes 횟수에는 추가 batch manifest JSON도 포함되지만 기대값은 OCR 본체만 계산한다(실제6/기대5). 이 실패만으로 본체 재업로드를 주장하지는 않으나, REPORT의 통과 주장과 다르다.

객체와 control JSON 호출을 구분해 테스트를 바로잡는다. 현재 테스트는 이전 commit으로 pending에서 제거된 자료만 확인한다. 필요한 “index commit 실패→pending 유지→다음 retry에서 verified receipt로 본체 GET/PUT0” 사례를 추가한다. 실패를 deselect하여 통과로 보고하지 않는다. 기존1257 passed/1 deselected에서 무엇을 제외했는지와 이유를 FIX_07_REPORT에 밝힌다. 과거 REPORT는 그대로 보존한다.

## 범위와 완료 기준

- native/Paddle/content/adopted、同一PDFの別source ID、expected OCR不一致、epoch更新、同一run再lookup/resumeをprovider-freeで確認する。
- 元variant/provenanceが既知のケースは厳密一致。知らないprovenanceを新モデルに置き換えない。
- 遅いbackup mockで部分commit成功/pending減少、commit failure retry時本体再GET/PUT0、root変更時再検証、既存remote本体のactual upload0または未計測扱いを確認する。
- 既存328件preflightの実行コードと集計結果をGit内evidenceに保存する。既知5nativeのprovenanceも確認し、キャッシュ境界修正後も323contentがprovider0で解決できることを実資料read-onlyで確認する。元volume/既存WebDAVを変更してpreflightしない。
- 関連テストとmypyを通す。全体suiteは必要な既存gateとして実行してもよいが、既知failureを隠す除外は不可。
- 今回はコード/オフライン検証の補正。実Workerをレビュー中に再起動しない。新しい運用反映時は既存ユーザー承認を引き継ぎ、provider-free inventory通過後にのみ起動し、startupと最初の進行確認でターンを終了する。

FIX_07_REPORTに変更、実行コマンド、結果、残る運用確認をまとめる。過去PLAN/FIX/REPORTは上書きしない。
