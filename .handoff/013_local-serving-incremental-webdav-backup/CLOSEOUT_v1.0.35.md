# 013 완료 및 v1.0.35 발행 인수 마감

2026-10-10 Reviewer/Executor Codex. 사용자가운영인수·커밋·main병합·GitHub릴리스를승인했다.

## 최종 판정

**개발수정·운영반영·초기backup관리대장·후속배치의변경없음생략동작을인수한다. 추가FIX없음.** GitHub소스릴리스버전은v1.0.35이다. 공개registry새이미지발행은이번범위의결과가아니다.

## 이력과 근거

| 단계 | 기록 | 결과 |
|---|---|---|
| 계획/실행/보완 |PLAN.md, REPORT.md, FIX_01~08 및각REPORT|localserving/optionalincrementalbackup및OCRcache회귀보완|
| 최종코드인수 |ACCEPTANCE_FIX_08.md|53독립테스트,mypy4파일,실제328provider0,variant/provenance확인|
| 운영배포 |OPERATIONS_FIX_08_REPORT.md|commit50a0129Worker/MCP이미지,기존볼륨/secrets유지|
| 첫운영완료 |OPERATIONS_RUN_20261009_REPORT.md|exit0,5520문서,OCR5519재사용/1신규,localpublication/MCP성공|
| 초기백업 |OPERATIONS_BACKUP_BOOTSTRAP_REPORT.md, OPERATIONS_BACKUP_AND_SCHEDULED_20261010_REVIEW.md|12977항목완료46분31초,pending/lost0,remoteindex16783건일치|
| 후속운영완료 |OPERATIONS_RETRY_20261010_ACCEPTANCE.md|5520cachehit/provider0,embeddingmiss0,backuprequests0/bytes0,MCPgeneration실응답|

모든과거handoff파일을보존했다. 최신운영상태는후속기록을기준으로읽으며과거중간실패나부분backup기록을삭제해성공만남기지않는다.

## 현재 운영과 제한

- `/opt/cardrag/current -> /opt/cardrag/013-50a0129`; Worker/MCP각각`cardrag-worker:013-50a0129`,`cardrag-mcp:013-50a0129`. localpublication 및sharedservingvolumeRW/RO.
- WebDAVbackupimmediate/inline300초. 대기없음이면자동flush하지않음. optionalbackgroundbackup은정지이며bootstrap은exit0종료.
- 기존신한originnetwork실패는931기존자료carry로처리. 신규/변경PDF가있으면정당한OCR/백업이발생할수있다. 미래항상provider0/HTTP0을보장하지않는다.
- 03시배치는10월10일CHDIR권한실패로Worker미기동이었다. 디렉터리권한을수정하고운영UID접근을검증했으며동일운영설정수동batch는완료했다. 다음실제systemd03시기동결과는운영상후속확인이다. 이를수동batch성공으로대체해scheduled성공이라주장하지않는다.
- 백업외자동WebDAV호출은actualsetting/codepath/log/performance상없다. 독립패킷캡처는없다. 리뷰용별도container의원격index8MB1회GET은분리해기록했다.
- 변경없는batch에서도구조화/views/export를재생성해약1시간38분걸린다. 성능후속개선여지는있으나사용자가확인한운영인수의차단요건으로추가하지않는다.

## 발행 준비

3개package/Workerruntime/uv.lock을1.0.35로동기화했다. README/운영진입점/릴리스안내를현재localarchitecture로갱신했다. 기존v1.0.34정식registryworkflow·qualification·공개태그이력은보존한다.

CI통과후main에병합하고annotatedtag및GitHubRelease를발행한다. 실제PR/merge/tag/releaseURL과CI결과는별도신규RELEASE_REPORT_v1.0.35.md에기록한다. 이문서는아직발행성공의증거를대신하지않는다.
