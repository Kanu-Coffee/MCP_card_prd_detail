# 022 — 우리카드 RAONK 다운로드 세션 인증 개선

작성일: 2026-10-30 KST  
역할: Planner  
상태: 계획 작성 완료, 구현·배포 미수행

## 1. 목표와 인계 범위

우리카드 공식 상품공시 페이지가 제공하는 다운로드 인증 헤더를 어댑터에 반영하여, 정상 PDF가 HTTP 200 HTML 서비스 오류로 반환돼 우리카드 전체 수집이 중단되는 문제를 해결한다. 공식 페이지의 정상 요청을 재현하며 인증 우회나 특정 상품의 무조건 skip을 구현하지 않는다.

이번 사용자 요청은 신규 handoff 계획 작성이다. Executor는 아래 구현·오프라인 검증·제한된 원본 진단을 수행하고 `REPORT.md`를 작성한다. 운영 이미지 활성화와 실패 run 재개는 준비된 변경의 검토 및 사용자 운영 적용 지시를 거쳐 별도 수행한다. 본 계획 자체가 운영 전환을 이미 승인하거나 완료한 기록은 아니다.

## 2. 착수 시 확인한 저장소·운영 상태

- 저장소: `/home/lee/projects/MCP_card_prd_detail`, HEAD `06af791b6db3fa250b8b543deed46f77f3a4a7d7`.
- 사용자 제공 Global AGENTS handoff 규약을 따른다. 저장소 내 `AGENTS.md`/`GEMINI.md` 검색 및 `/home/lee`, `/home/lee/projects`의 해당 파일 확인에서 추가 지침은 발견하지 못했다. Executor는 착수 시 재확인한다.
- 기존 미커밋 변경: Worker `capacity_v5.py`, `cli.py`, `ocr.py`, `paddleocr_runner.py`, `pipeline.py`, `providers.py`; 테스트 `test_cli_settings_provider.py`, `test_paddle_inputs_v130.py`; timer와 운영 문서. 미추적 020/021 handoff, `ocr_logging.py`, `paddle_cleanup.py`, `test_ocr_provider_logging.py`, `docs/WORKER_RUNTIME_OPTIONS.md`도 존재한다. 기존 변경을 되돌리거나 이 과제로 임의 편입하지 않는다.
- 확인된 활성 release: `/opt/cardrag/021-ocr-logging-20261029`, 이미지 `cardrag-worker:021-ocr-log-d01f2d908acc`, ID `sha256:983b2196fb1f4b75d7f15c93248602e4c0d0ed50c944d9a7d471dc35b1234f92`.
- 10/29 BC 재개 run `ec574ddd3cda4570adcf1124eee09faa`은 성공했고 generation `g-ec574ddd3cda4570adcf1124-2777b78df9fd`를 로컬 게시했다. 현재 포인터는 구현·운영 착수 시 다시 조회한다.
- 10/30 10:00 예약 run `df29d8e5dbb34048942a5296bb2d6ac8`: 우리카드 목록 922건 발견, download 단계에서 약 43초 후 종료 코드 1. OCR/provider 호출 및 신규 게시 없음.
- 실패 source: `source_02bcc6605146c858465ecc7c7b58209519d3e573a9114c6e68585b004d5ae391`, 상품코드 `100637`, 배달의민족 비장의카드 V.2. `issuer_origin_service_error`, HTTP 200 / text/html, `visible_service_error_page`. collection 보고서는 attempts=5, acquired_count=15, consecutive_failed_runs=3을 기록한다.
- 실패 보고서: `/state/runs/df29d8e5dbb34048942a5296bb2d6ac8/reports/{worker-failure,issuer-collection}.json`. 상태 DB와 원본 PDF/OCR은 보존한다.
- `.handoff/README.md`는 019 당시의 오래된 상태를 담고 있다. 그것만으로 현재 release/timer를 추정하지 않는다. 기존 역사 artifact는 수정하지 않는다.

## 3. 직접 확인한 원인과 샘플 증거

공식 진입 페이지:
`https://pc.wooricard.com/dcpc/yh1/cct/cct11/prdntc/H1CCT211S09.do`

해당 HTML에는 `raonkAuthKey`, `raonkAuthPageId` hidden input이 있다. 페이지 스크립트 `/dcpc/js/yh1/cct/cct11/prdntc/H1CCT211S09.js?20260915000001`의 `uploadConfig.AddHttpHeader`는 다음처럼 연결한다.

```javascript
AddHttpHeader: {
  RAONK_AUTH_PAGE_ID: $('#raonkAuthPageId').val(),
  RAONK_AUTH_KEY: $('#raonkAuthKey').val()
}
```

RAONK 라이브러리: `/dcpc/kupload/js/raonkupload.js?20261011000001`. 현재 다운로드 endpoint는 그대로 `/dcpc/kupload/handler/raonkhandler.jsp`이며, 기존 암호화 `k01` 폼과 Referer에 **두 HTTP 헤더**가 필요하다.

운영 021 이미지의 WooriAdapter를 진단용 일회성 컨테이너에서 사용했다. 상태 볼륨에 쓰지 않고 `httpx.AsyncClient` 하나로 landing GET → 상세 API → 기존 `prepare_download` → POST를 수행했다.

- 기존 헤더로는 샘플들이 HTTP 200, text/html, 2,581 bytes의 동일 오류 페이지를 반환했다. 안내는 홈페이지 서비스가 원활하지 않다는 내용이었다.
- 같은 세션의 landing에서 추출한 두 값을 HTTP 헤더에 추가하면 정상 PDF를 반환했다.
- 같은 두 값을 HTTP 헤더 대신 폼 필드로 추가하면 다시 HTML 오류였다. 실제 인증 값과 cookie는 인계 문서·fixture·로그에 저장하지 않는다.
- 성공 결과를 `pypdfium2.PdfDocument`로 열어 페이지 수까지 확인했다.

| 코드 | 상품 / 파일 | 크기 / 페이지 | SHA-256 |
|---|---|---|---|
| 100637 | 배달의민족 비장의카드 V.2 / `1.(100637)배달의민족 비장의카드 V2_202310.pdf` | 1,141,253 bytes / 2 | `54aacbe92971458c61970e8c7b84aa871319a0ee95b73759329d996e21447b3f` |
| 500107 | 카드의정석2 SOLID / `카드의정석2 SOLID 상품안내장.pdf` | 328,332 bytes / 8 | `5f144c6e5dc3b1f18e598e98eef3ef969d93c91db60ca775c6fe7ae1f56cdb69` |
| 104149 | the WON / `the WON 상품안내장.pdf` | 4,040,084 bytes / 9 | `0f9589481d5c08e10583d9c78b070b81aa86d9685a9cd869312288b896cedc6d` |

100637은 기존 캐시 PDF의 hash/크기와 일치했다. 원본 URL은 각각 다음과 같다.

- 100637: `https://pc.wooricard.com/upload/cardClause/2023/10/13/08455444-775a-4e0c-a180-f43e872b3871.pdf`
- 500107: `https://pc.wooricard.com/upload/cardClause/2026/9/29/c1441ee5-c310-4177-b21f-f102d0f414a3.pdf`
- 104149: `https://pc.wooricard.com/upload/cardClause/2025/9/16/f224c9bb-287b-4567-8895-576d8b4938ca.pdf`

공식 페이지와 이번 진단은 헤더 누락을 뒷받침하지만, 도입 시점, token TTL, 일회용 여부, 전체 922건의 성공은 미검증이다. 장시간 배치의 만료 동작을 추정으로 단정하지 않고 아래 제한된 갱신과 실측으로 검증한다.

## 4. 関連ファイルと実装指針

- `apps/cardrag-worker/src/cardrag_worker/issuers/woori.py`: `discover_current`, `prepare_download`, `raonk_plaintext`, `encrypt_raonk`。
- `pipeline.py`: discoveryの `create_issuer_client` とdownload側client生成、`acquire` 内の `adapter.prepare_download(current_client, source)`、stage retry経路。
- `rate_limit.py`: `RateLimitedClient` は `.client` を持つラッパー。`get`/`post`/`stream` の制限・計測・サービスエラー分類を維持する。
- `downloader.py`: SecurePDFDownloaderのstream経路、conditional GET相当、redirect、MIME/PDF/DRM/ZIP検証。
- `detail_inventory.py`、`issuer_collection.py`、`issuer_http.py`、`issuer_scope.py`: 状態を跨ぐsource再利用・失敗分類・session管理を必要箇所のみ確認。
- テスト: `test_issuers.py`, `test_issuer_http.py`, `test_issuer_collection.py`, `test_downloader.py`, `test_fix_30_origin_retry.py`、関連PDF cache/scopeテスト。
- 文書: `docs/OPERATIONS.md` のissuer originエラー・ZIP隔離、`docs/RECOVERY.md` の再開、020/021最新運用報告。019 `LIVE_REVIEW_12.md` / `REVIEW_17.md` には過去の同様な未確定診断がある。今回の成功再現で旧診断を補う。

### 4.1 セッションと認証情報

1. landing HTMLからinputの属性順・引用符・空白等に耐える方法で二値を抽出する。既存依存とparser慣例を優先し、このためだけの依存追加を避ける。欠損・空値・重複矛盾・不正なヘッダー値は限定した分類で拒否し、未認証ダウンロードを大量実行しない。
2. **discoveryとdownloadは別clientであり得る。** discovery時の値だけをadapter共有フィールドに保存して使う修正では不足。`prepare_download` 単独、detail inventory hit、resume、新しいclientでも、実際のdownload clientと同一cookie jarで新鮮な二値を取得できるようにする。
3. 認証はclient/session単位の一時状態として管理する。ラッパー再生成と実clientの関係を検査し、ID再利用・閉じたclientの参照保持・他clientへの漏洩を防ぐ。client.cookies/headersの実装詳細へ無制限に依存しない。
4. 同じsessionで並行2ダウンロードが走ってもtoken取得を重複発行せず、更新中のtokenとcookieの不整合を防ぐ。clientごとのsingle-flight相当を設計・検証する。ファイルごとのlanding GETは原則避ける。
5. HTTPヘッダーに二値を設定する。`k01`生成、公式Referer、filename、source identity、HTTP予算、rate limit、TLS/host検証を維持する。認証値をSourceRecord metadata、discovery hash、PDF cache key、DB、seal、execution-planに追加しない。
6. redirect時にセッション認証ヘッダーが意図しないhostへ流れないことを確認する。共有downloaderを変更する必要があるなら限定的変更と他カード社回帰を伴わせる。

### 4.2 失効・再試行

- 現在の共通stage retry budgetと呼び出し順序を実装前に追う。HTTP200サービスエラー後に同じ古いtokenを何度も使わないよう、限定した無効化・再取得を接続する。初回取得、明示更新、再downloadの各呼び出し数を制限する。
- 認証失効の専用サーバー信号はまだ確認されていない。一般サービスエラーをすべて認証失効と断定しない。更新後もHTMLなら従来のorigin failureを返し、無限retryや複数層でのretry増幅を防ぐ。
- 403/429/Retry-After、challenge/login/未知HTML、DRM/ZIP、PDF構造損傷の既存分類を維持する。サービスエラーをZIP/unsupportedへ変換しない。特定product codeやhashをskip条件にしない。
- 認証key・cookie・暗号化フォームや生HTMLをログ/例外へ出さない。試験用sentinelで漏洩検査する。

## 5. 必須検証と受入基準

意味のある次の回帰テストを追加・更新する。従来の `test_woori_discovery_uses_natural_version_and_post_download_contract` はform存在しか確認しておらず、認証付き実downloadを保証しない。

1. landing cookieと二値が揃った場合だけPDFを返すHTTP fixtureで、実際の `SecurePDFDownloader` / `RateLimitedClient` を通して成功する。裸httpxのPOSTだけで合格にしない。
2. discovery client A → download client B、同じadapterでclient Cへ再開、prepare_download単独、detail inventory再利用を検証し、古いsession値が混ざらない。
3. 同一clientで並行要求した場合の取得回数・cookie整合性と別client間の分離を検証する。
4. token抽出の欠損/空/矛盾/危険文字でfail closed。認証値のログ・exception・保存payloadへの混入なし。
5. 最初のtokenでHTML→更新後PDF成功、更新後もHTML、ページ取得自体失敗を検証する。呼び出し数と共通retry予算の上限をassertする。
6. 条件付き再検証の304、既存PDF/OCR CAS再利用、ZIP/DRM、issuer失敗時の非選択7社と改定履歴保全を維持する。

基本コマンド（実ファイル構成・プロジェクト慣例に合わせて調整し、結果を報告）:

```bash
uv run --no-sync pytest apps/cardrag-worker/tests/test_issuers.py apps/cardrag-worker/tests/test_issuer_http.py apps/cardrag-worker/tests/test_issuer_collection.py apps/cardrag-worker/tests/test_downloader.py apps/cardrag-worker/tests/test_fix_30_origin_retry.py -q
uv run --no-sync ruff check <変更したPythonファイル>
git diff --check
```

共有pipeline/downloaderを変更した場合は影響範囲のscope/cache/securityテストを追加実行する。テストを通すため既存の安全性assertを削除しない。既存変更由来の失敗は今回差分と区別して記録する。

ネットワーク診断は3商品だけを対象とし、新実装の本番同等downloader経路でPDF検証・hash/size/page数を確認する。上表との差は現時点の公式改定の可能性も確認する。HTTP budgetとrate limitを通し、運用DB・cache・pointerへ書かない。922件全downloadやOCRをサンプル試験のためだけに実施しない。

## 6. 運用適用を行う場合の手順

1. ユーザーから適用指示を受けた後、現release・image ID・service/timer・実行中writer/backup・現在stable generation・容量を再確認する。既存manual-start lockとworker lockを使い、DBを別processで開く場合はwriter非実行を確認する。
2. ユーザーはこの会話で手動再開時も **timerを維持する** 方針を明示した。実行時刻と次回予約の重複を確認し、この指示を踏まえる。漫然とtimerをdisableしたり、権限不足を理由に同じ確認を繰り返さない。既存writerを停止してlockを空ける方法は使わない。
3. 活性021画像はrepository全体と一致するとは限らない。狭い変更の新image/releaseを準備し、既存021のOCRログ・020のPaddle修正、設定、auth/model cacheとserving volumeを保全する。ソース全体の無検証置換やグローバル依存更新は避ける。image provenanceと差分hashを残す。
4. 既存手順でサービスアカウントのconfigアクセス・startup・復旧点を検証する。保護snapshotの容量と既存retentionを守り、不要な全DB複製を増やさない。現在の復旧案内と直前imageを維持する。
5. 原則として当日失敗run `df29d8e5dbb34048942a5296bb2d6ac8` の通常resumeを検討する。実行日・run状態・scope・後続publication・predecessor fenceを確認し、再開不能時のみ理由を記録して新runを選ぶ。`resume-publication`やSQLによる成功/rotation状態修正で収集を迂回しない。
6. 完走判定はexit0だけでなく、woori collection成功、origin確認、OCR再利用/追加呼出し、seal・local publish・MCP実current generation、他7社/未解決履歴保全まで連結する。バックアップは今回scope完結と範囲外pendingを分けて報告する。構造fallback/unsupported件数も隠さない。
7. 失敗時は直前正常servingを維持し、token/cookieを含めず限定した診断を残す。image rollbackとDB restoreを混同しない。翌予約の状態と実行後空き容量を報告する。

## 7. Executor / Reviewerへの成果物要件

- Executorは既存Git状態と本計画を読み直し、変更理由、session所有/refresh設計、変更ファイル、テストの実コマンドと結果、サンプル取得結果、計画との差異、未実行の運用範囲を `REPORT.md` に記録する。
- 実装完了・サンプル成功・運用完走を分ける。今回Plannerの診断をExecutor自身の実装テストとして数えない。
- ReviewerはPLAN/REPORT、既存関連history、現在diff/statusと証拠を確認し、必要な場合のみ次の未作成 `FIX_NN.md` を作成する。Executorは未報告の最新FIXを実行する。過去artifactを上書きしない。
- 本task作成時点では新規 `PLAN.md` のみ。既存コード・運用設定・旧handoff indexは変更していない。
