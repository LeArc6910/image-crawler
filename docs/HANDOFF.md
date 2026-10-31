# 작업 인계 (2026-10-31)

다음 세션은 이 문서와 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)를 먼저 읽고 이어서 진행한다.

- 작업 브랜치: `claude/trusting-pascal-5q25bw` (저장소에 있는 유일한 브랜치, `main` 없음)
- 테스트: `pip install -e '.[dev]'` 후 `NO_PROXY=127.0.0.1,localhost pytest` → 56개 통과 상태
- 사용자와는 한국어로 대화한다.

## 1. 현재 상태

| 영역 | 상태 |
|---|---|
| 크롤러 (수집·암호화·썸네일·목록·reindex·restore) | 완료 |
| R2 무료 한도 보호 (`usage`, 요청 계량·차단) | 완료 |
| 점검 (`check`), 휴대폰용 연결 정보 (`viewer-export`) | 완료 |
| PWA 뷰어 (`pwa/`): 연결·잠금·갤러리·원본 보기·암호문 캐시·요청 폭주 방지 | 완료 (헤드리스 Chromium 테스트만, 실기기 미검증) |
| 목록 병합 (`compact`, 크롤링 후 자동), `restore --limit` | 완료 |
| GitHub Pages 배포 워크플로 (`.github/workflows/pages.yml`) | 파일만 있음, 미배포 |

## 2. 실제 R2에서 확인된 것

- 버킷 이름: `image-crawler` (계정 ID는 사용자에게 받는다. 이 문서에는 적지 않음)
- 크롤러 토큰(Object Read & Write)으로 `image-crawler check` 통과: 인증, 목록, 쓰기·읽기·삭제 정상
- CORS 조회는 이 토큰에 버킷 설정 권한이 없어 **경고**로 표시됨 (정상 동작)
- 키는 클라우드 환경 변수 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`로 들어 있다. **값을 출력하거나 커밋하지 않는다.**
- `config.toml`은 `.gitignore` 대상이라 새 세션에서 다시 만들어야 한다 (scratchpad 권장):

  ```toml
  key_file = "key"            # 없으면 `image-crawler keygen`
  state_db = "state.db"
  [storage]
  backend = "s3"
  [storage.s3]
  bucket       = "image-crawler"
  endpoint_url = "https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
  region       = "auto"
  ```

  주의: 테스트용으로 만든 키로 올린 이미지는 그 키가 있어야만 열린다. 사용자 PC의 실제 키와 섞이지 않게,
  실사용 데이터를 올리기 전에 어떤 키를 쓸지 사용자와 정한다.

## 3. 다음 세션에서 할 일 (순서대로)

### 3-1. 실제 R2 크롤링 검증 (키가 있는 세션에서)

1. `image-crawler check` 다시 통과 확인
2. 작은 크롤링: `image-crawler crawl --max-pages 2` (테스트용 사이트 1개)
3. `image-crawler usage`로 집계 확인, `image-crawler reindex` → `0 missing` 확인
4. `image-crawler restore --out <scratchpad>/restored --limit 5`로 복원 확인
5. 결과를 DEVELOPMENT_PLAN.md의 검증 결과에 기록

### 3-2. 뷰어 배포 (사용자 작업 + 승인 필요)

1. `main` 브랜치 만들기 — **사용자 승인 후** 이 브랜치로 생성/푸시 (또는 사용자가 직접)
2. 사용자: 저장소 Settings → Pages → Source를 **GitHub Actions**로 → *Deploy viewer* 실행
   (비공개 저장소의 Pages는 GitHub 유료 요금제 필요)
3. 사용자: Cloudflare 대시보드 → R2 → `image-crawler` → Settings → **CORS Policy**에 등록
   (크롤러 토큰으로는 `image-crawler cors`가 권한 부족):

   ```json
   [
     {
       "AllowedOrigins": ["https://<사용자>.github.io"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedHeaders": ["authorization", "x-amz-date", "x-amz-content-sha256", "range"],
       "ExposeHeaders": ["ETag", "Content-Length"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```

4. 사용자: 휴대폰용 토큰(**Object Read only**) 발급 → `image-crawler viewer-export` → 휴대폰에서 QR 스캔 → 홈 화면에 추가
5. 실기기 확인: 특히 iOS Safari의 카메라 QR 스캔, 홈 화면 설치, 자동 잠금

### 3-3. 남은 개발 항목

| # | 항목 |
|---|---|
| 3.2 | 앱 코드 변조 감지 (배포된 코드가 바뀌면 경고) |
| 3.4 | 삭제 (원본·썸네일 삭제 + 목록에 삭제 표시) |
| 3.5 | 검색·필터 (사이트, 기간, 파일명) |
| 4.1 | 주기 실행 예시 (cron / systemd timer) |
| 4.3 | 키 백업 점검 절차 문서화 |

사용자가 휴대폰에서 실제로 써 본 피드백을 반영해 순서를 정한다.

## 4. 알아둘 점

- moto의 S3 서버는 이미 있는 객체에 대한 CORS 사전 요청(OPTIONS)에 403을 돌려준다 (실제 S3/R2와 다름).
  `tests/test_pwa.py`의 `r2_like` 중계가 버킷 CORS 규칙으로 대신 응답한다.
- 이 환경의 Python에서 `moto[server]` 설치 시 debian `blinker` 충돌이 나면
  `pip install --ignore-installed blinker` 후 다시 설치한다.
- PWA 테스트는 node + Playwright(사전 설치됨)를 쓴다. 없으면 자동으로 건너뛴다.
