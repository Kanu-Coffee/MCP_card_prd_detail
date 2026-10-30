# 022 FIX_05 — 인수된 우리카드 어댑터 운영 배포 및 실패 worker 재개

작성일: 2026-10-30 KST  
역할: Reviewer / 운영 실행 지시  
상태: Executor 실행 대기

## 1. 목적·사용자 승인

사용자가 “운영배포와 워커재개를 지시하는 fix를 발행하라”고 명시했다. 이 FIX는 `REVIEW_01.md`에서 인수한 022 구현을 운영에 적용하고 우리카드 실패 run을 정상 재개하여 수집·게시·MCP 반영·해당 범위 백업을 검증하도록 지시한다. 해당 작업은 **사용자 승인된 실행 범위**다. 이전 PLAN/REPORT의 “운영 적용 지시 대기” 조건은 이 지시로 충족됐다. 같은 승인 질문을 반복하지 않는다.

사용자의 기존 **timer 유지** 지시도 유효하다. 배포·재개 중 timer를 disable/stop하지 않으며, 실제 writer lock과 기존 배포/manual-start lock으로 동시 실행을 제어한다. 예약과 충돌하면 정상 실행을 종료하거나 lock을 삭제하지 말고 기존 실행과 실행 창을 재확인한다.

지금 Reviewer는 운영 상태를 읽고 이 지시서만 작성했다. 배포·resume을 이미 수행했다는 뜻은 아니다.

## 2. 지시서 작성 시 기준 상태

아래는 2026-10-30 저녁 조회 결과다. Executor는 실행 직전에 다시 확인하고 변경됐으면 차이를 평가한다.

- HEAD `06af791b6db3fa250b8b543deed46f77f3a4a7d7`, 020/021/022 미커밋·미추적 변경이 공존한다. 전부를 022 배포에 임의 포함하지 않는다.
- active release `/opt/cardrag/021-ocr-logging-20261029`.
- active Worker image `cardrag-worker:021-ocr-log-d01f2d908acc`, ID `sha256:983b2196fb1f4b75d7f15c93248602e4c0d0ed50c944d9a7d471dc35b1234f92`.
- worker service는 10/30 10:00:43에 실패한 상태. 실행 중 worker 컨테이너는 관측되지 않았다. `cardrag-backup.service`는 설치돼 있지 않다. unit 부재만으로 backup writer가 없다고 단정하지 말고 컨테이너·프로세스·backup lock도 확인한다.
- timer active/enabled, 다음 예약 10/31 10:00 Asia/Seoul, Persistent=false.
- `cardrag-mcp`, 고객 web/edge는 running/healthy. root 가용량 `91,841,871,872` bytes. 이 값은 배포 시점의 용량 보장이 아니다.
- 재개 대상 run `df29d8e5dbb34048942a5296bb2d6ac8`, issuer=woori. download 단계 `issuer_origin_service_error`, HTTP200 HTML 오류로 중단. 잘못된 인증 요청 문제는 인수된 022 코드에서 해결했다.
- 현재 protected snapshot은 `021-ocr-logging-20261029`. 기존 code rollback은 020이다. 기존 `historical_release`의 fix32 snapshot은 `pruned_missing`이므로 유효 복구점으로 사용하지 않는다. 실제 현존 복구본과 지침을 재검증한다.

## 3. 배포 입력과 변경 경계

`REVIEW_01.md`의 인수 범위와 현재 source SHA-256이 일치해야 한다.

| 파일 (`apps/cardrag-worker/` 기준) | SHA-256 |
|---|---|
| `src/cardrag_worker/issuers/woori.py` | `cc454a9ad01db4ad29a45592b6efdc2839744f8fe34bd593a7bbe97eb1d95039` |
| `src/cardrag_worker/downloader.py` | `b3d9e4021d086f9866e99944c7fc5c6890ccf109454a06840a801b5f9800a2d1` |
| `src/cardrag_worker/pipeline.py` | `c3f060cc2665b8596524f8491d48f7667ee19192bfad4ead9ce48ced60f72e2e` |

pipeline hash는 선행 020/021 변경을 포함한 현재 파일 식별용이며, 전체를 새로운 022 변경으로 간주하지 않는다. 운영 021과 세 파일의 실제 차이를 확인하고 022에 필요한 변경만 포함되는지 검증한다. 불필요한 차이가 있으면 운영 021에 좁게 patch한 배포 파일을 준비하고 저장소와의 차이·hash·테스트 대상을 보고한다.

고정 ID의 021을 base로 세 파일만 적용한 최소 overlay image를 권장한다. 기존 021의 `ocr_logging`, 주 OCR `codex-exec/gpt-6.1-terra/high`, Paddle fallback, 용량·복구 수정을 유지한다. 기존 의존성인 bs4/lxml 등을 실제 image에서 사용할 수 있는지 확인한다. 무관한 의존성 갱신·모델 변경·DB migration은 도입하지 않는다.

충돌하지 않는 신규 022 release/image 이름을 선택하고 base image ID·입력 hash·배포 파일 hash·image ID·Dockerfile을 기록한다. 기존 tag/release는 덮어쓰지 않는다. runtime private 설정·secret·auth/model cache·state/serving volume·UID/GID·proxy·read-only/security 조건을 유지하고, 변경은 필요한 image 지정 등에 한정한다. secret이나 token을 공개 handoff에 옮기지 않는다.

## 4. Executor 実行手順

### A. 調査・候補準備

1. PLAN、全REPORT/FIX、REVIEW_01、現在Git、適用AGENTS/GEMINI、現行運用ガイドを読み直す。現在のworker・backup・timer、stable pointerとMCP実current generation、issuer scope、run状態を確認する。生きたDBを外部processで開かず、必要なDB調査はwriter非実行とlockの下で行う。
2. 上記入力を元にcandidate image/releaseを準備する。`operations/worker-compose.sh`はscriptの実在releaseを解決する構造を保つ。全実効composeを比較し、承認外の環境・volume・実行権限変更がないことを機械的に確認する。
3. 174件の既存検証証拠を使いつつ、実際に配布するcandidate imageのPython環境・import、3ファイルhash、関連回帰を確認する。必要なら一時test overlayで実行し、運用stateをテストに使わない。candidateがインストール済みsourceと違う場合はそれを隠さず、差分に対応した検証を行う。
4. 修正後の実imageで3商品 `100637`, `500107`, `104149` の認証付き取得を必要最小限で検証する。RateLimitedClient/SecurePDFDownloaderと一時保存先を使い、運用cache/DB/pointerは変更しない。PDF hash/size/page数を既存実査と比較する。

### B. 復旧点・排他・有効化

5. `/opt/cardrag/.deployment.lock`、旧/新releaseの `operations/live/manual-start.lock` と既存Worker/backup lockの順序を既存手順に合わせる。active切替と復旧本取得を実writer lockで保護し、timer経由のwriterにも同じ排他が効くことを確認する。manual-start lockだけで予約を防げると仮定しない。lock保持中に同じlockを必要とするCLIを自己デッドロックさせない。
6. 運用直前に現在のworker-state/backup-ledgerについて既存の検証済み方法で整合した新しい復旧snapshotを作る。`quick_check`、hash、WALの扱い、snapshot実在性を記録する。021の古いsnapshotだけを今日のDB復旧点と表示しない。復旧本作成・image・バッチpeakを含む容量を確認し、前回のcapacity検査を無効化しない。
7. 既存の `deploy/worker/verify-service-access.sh` で実 `cardrag` アカウントとdocker groupによる新releaseのconfigアクセスを確認する。rootで自分だけconfigが読めることを代替根拠にしない。candidate `startup-check`も実行し結果を保存する。startup成功をfull batch成功としない。
8. ユーザー承認済み範囲で `/opt/cardrag/current` を新releaseへ原子的に切り替え、直前021をcode rollbackとして残す。recovery-point/deployment記録とsnapshot-retentionを現実の存在・検証結果に合わせて更新する。DB restoreは行わない。旧snapshotは新復旧点検証と参照関係確認前に削除しない。Docker volume pruneは使わない。
9. 既存 `apply-021-ocr-logging.sh` をそのまま再実行しない。このscriptは旧020→021、期限2026-10-29、timer停止処理等を含み、今回の入力・ユーザー指示に適合しない。必要な022用scriptを具体化して使う。権限が必要な操作が実行不能なら、先に候補・検証・厳密なroot実行scriptを完成させた上で、必要な権限操作と理由を一度だけ明示する。パスワード取得や権限迂回は行わない。

### C. 同じ失敗 run の再開

10. 対象runが現在もresumableでscope=woori、後続の競合publicationがなく、候補実効設定がそのrunに適合することを確認する。日付を跨いでも自動round-robinで過去slotを作り直さず、明示resumeを用いる。scope/rotation行をSQLで削除・修正しない。
11. 新releaseのcompose wrapperで、run IDを明示して起動する。意図するworkerコマンドは次のとおり。imageと実効設定の確認後に実行する。

```bash
/opt/cardrag/current/operations/worker-compose.sh run -d --no-deps \
  --name <新しい一意な022-woori-resume名> \
  worker run --resume df29d8e5dbb34048942a5296bb2d6ac8
```

`resume-publication`や通常 `worker run` の新規自動slotで置き換えない。実際のrun状態が変わりresume不能なら、判明した理由と対応可能な範囲を報告する。実際に未処理runの範囲を再取得する必要がある場合のみ、明示woori新runという調整を根拠付きで行い、元runとの関係を記録する。正常の先行writerは終了させない。

12. 起動metadata（時刻、release、image ID、container ID、run ID、mode、timer状態）を新releaseのprivate運用証拠に保存し、ログ確認コマンドをユーザーへ伝える。認証header/cookie/formを保存・表示しない。サービスの昨日のfailed表示を手動containerの実状態と混同しない。

### D. 完走・公開・バックアップ検証

13. 起動確認だけで終了せず、終了または具体的障害まで追跡する。collectionのwoori成功/必要なdegraded、各download結果、OCR provider呼出しとcache再利用、capacity、seal、CLI最終JSON、exit codeを記録する。想定外のunsupported/degradedは原因を確認し、HTMLをPDF/unsupportedとして通さない。
14. `status=succeeded/no_change`、exit0、local publication有無とgeneration IDを結び、Worker stable pointerとMCPの実current generationおよび読み取り結果を確認する。health/liveのみで公開成功としない。非選択7社と未解決改定履歴の保全も既存scope検証で確認する。
15. backupは対象run/issuerのscopeでstatusを確認し、必要なら既存 `backup flush --run RUN_ID --issuer woori` で送信してscope完結を検証する。このrun範囲の正常化のためのflushは今回の承認範囲。`check-issuer.sh`を使う場合は `live/woori.json` とcontainer/resultの紐付けを新実行に正しく設定し、古いmetadataをそのまま読まない。範囲外pendingを成功に見せるために全件削除・一括転送しない。
16. timer active/enabled・次回日時と現release・MCP状態・ディスク余裕を最終確認する。予約が実行中resumeと重なった場合のlock拒否は重複実行防止の結果として記録し、resumeの成功判定と分離する。

## 5. 障害時の扱い

- 失敗したら原本・checkpoint・直前正常servingを保全し、例外分類・stage・run/reportパス・容量等の安全な診断を記録する。正常を装うSQL修正、lock削除、cache全消去は不可。
- 今回配布差分に原因のある回帰でcode rollbackが必要なら、保存した021へのコード/image復帰を既存排他の下で行う。DB restoreや正常published generationの強制巻き戻しは含まない。未確定状態で再開を何度も繰り返さない。
- 必要な権限や外部応答の問題が残る場合は、準備済み成果物、実行済み範囲、残った具体的操作を報告する。開始だけで「完了」と記載しない。

## 6. 成果物・完了条件

Executorは新規 `FIX_05_REPORT.md` に実施内容と実コマンド・結果を記録する。最低限次を含める。

- 運用変更前後のrelease/image ID・配布source hash・設定差分と実サービスアカウント検証。
- 新復旧snapshotの実在・quick_check/hash・retentionとcode rollback先。
- resume container/run ID、開始終了KST、terminal状態、exit、collection/OCR/capacity概要。
- seal/local pointer/MCP current generationの対応、非選択資料・履歴保全結果。
- 今回backup scopeの完結/未完と範囲外pendingの区別。
- timer状態・次回日時、残り容量、未完事項やblocker。

レビュー合格の実装が **実運用に反映され、対象runの再開が完走し、公開と対象backup scopeが検証されたこと**を完了条件とする。候補準備だけ/起動だけ/ログが流れた段階で完了としない。既存PLAN/REPORT/FIX/REVIEWは上書きしない。
