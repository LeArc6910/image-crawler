# 작업 인계 (2026-10-31)

다음 세션은 이 문서와 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)를 먼저 읽고 이어서 진행한다.

- 작업 브랜치: `claude/festive-lovelace-7mpo4j` (`claude/trusting-pascal-5q25bw`에서 이어 받음). 기본 브랜치는 `main`(공개 저장소)
- 테스트: `pip install -e '.[dev]'` 후 `NO_PROXY=127.0.0.1,localhost python -m pytest` → 62개 통과 상태
  (`pytest` 명령이 다른 Python에 묶여 있으면 의존성을 못 찾으므로 `python -m pytest`를 쓴다)
- 사용자와는 한국어로 대화한다.

## 1. 현재 상태

| 영역 | 상태 |
|---|---|
| 크롤러 (수집·암호화·썸네일·목록·reindex·restore) | 완료, 실제 R2에서 검증 |
| R2 무료 한도 보호 (`usage`, 요청 계량·차단) | 완료 |
| 점검 (`check`), 휴대폰용 연결 정보 (`viewer-export`) | 완료 |
| PWA 뷰어 (`pwa/`): 연결·잠금·갤러리·원본 보기·암호문 캐시·요청 폭주 방지 | 완료 (헤드리스 Chromium 테스트만, 실기기 미검증) |
| 목록 병합 (`compact`, 크롤링 후 자동), `restore --limit` | 완료 |
| GitHub Pages 배포 워크플로 (`.github/workflows/pages.yml`) | 배포됨: https://learc6910.github.io/image-crawler/ |
| 클라우드 실행: 키 환경 변수 `IMAGE_CRAWLER_KEY`, 크롤 기록·사이트 목록 동기화 `state_sync` | 완료, 실제 R2에서 검증 |

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

### 3-1. 실제 R2 크롤링 검증 — ✅ 완료 (2026-10-31)

`check` → `crawl` → `usage` → `reindex`(0 missing) → `restore`(원본과 SHA-256 일치) → 재크롤링(중복 0) 모두 통과.
자세한 수치는 DEVELOPMENT_PLAN.md의 **실제 R2 크롤링 검증 결과**. 테스트용 키 + `prefix = "verify-test/"`로 진행했고
그 데이터(약 2.9 MB)는 버킷에 남아 있다 (테스트 키는 세션과 함께 사라졌으므로 열 수 없음, 지워도 됨).

### 3-2. 뷰어 배포 — 1·2 완료 (2026-10-31)

- `main` 생성·푸시, 기본 브랜치를 `main`으로 변경, 저장소 **공개** 전환(전 커밋 기록에 비밀 값 없음 확인), Pages 소스 GitHub Actions
- *Deploy viewer* 첫 실행 성공 → **https://learc6910.github.io/image-crawler/**
  (헤드리스 Chromium 휴대폰 화면에서 연결 화면 표시, service worker 등록, 콘솔 오류 0)
- CORS 등록 완료·확인: `learc6910.github.io`의 GET 사전 요청만 204, PUT·다른 출처는 403.
  배포된 뷰어 주소에서 앱 코드(`s3.js`·`vault.js`)로 실제 R2에 서명 요청 → 목록 → 인덱스 복호화(26개)
  → 썸네일(WebP)·원본(JPEG) 복호화 성공, 틀린 키는 거부, 콘솔 오류 0 (테스트 키 + `verify-test/`)
- 휴대폰용 읽기 전용 토큰 발급됨 (사용자). 남은 것: 아래 4~5 → **PC 작업이라 [LOCAL_HANDOFF.md](LOCAL_HANDOFF.md)로 로컬 세션에 넘김**


1. `main` 브랜치 만들기 — **사용자 승인 후** 이 브랜치로 생성/푸시 (또는 사용자가 직접)
2. 사용자: 저장소 Settings → Pages → Source를 **GitHub Actions**로 → *Deploy viewer* 실행
   (비공개 저장소의 Pages는 GitHub 유료 요금제 필요)
3. 사용자: Cloudflare 대시보드 → R2 → `image-crawler` → Settings → **CORS Policy**에 등록
   (크롤러 토큰으로는 `image-crawler cors`가 권한 부족):

   ```json
   [
     {
       "AllowedOrigins": ["https://learc6910.github.io"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedHeaders": ["authorization", "x-amz-date", "x-amz-content-sha256", "range"],
       "ExposeHeaders": ["ETag", "Content-Length"],
       "MaxAgeSeconds": 3600
     }
   ]
   ```

4. 사용자: 휴대폰용 토큰(**Object Read only**) 발급 → `image-crawler viewer-export` → 휴대폰에서 QR 스캔 → 홈 화면에 추가
5. 실기기 확인: 특히 iOS Safari의 카메라 QR 스캔, 홈 화면 설치, 자동 잠금

### 3-2b. PC 작업 결과 (LOCAL_HANDOFF.md, 2026-10-31 사용자 보고)

- 실제 키: PC에서 `keygen` 1회 (`--force` 없음). 백업: 같은 드라이브 사본(SHA-256 일치) + 다른 장치 오프라인 백업
- `viewer-export`: `✓ viewer token can read the bucket and cannot write to it`
- 휴대폰 Android / Chrome: QR 스캔 연결, 홈 화면 앱 실행, 자동 잠금 후 비밀번호로 다시 열기 모두 정상 (갤러리는 비어 있음)
- 클라우드 환경 변수 `IMAGE_CRAWLER_KEY`, `CLOUDFLARE_API_TOKEN`(Account Analytics Read) 등록
- `CLOUDFLARE_API_TOKEN`으로 `/accounts` 조회 시 계정 0개 → 계정 ID는 여전히 사용자에게 받는다

**사고 (2026-10-31, 클라우드 세션):** 실제 `IMAGE_CRAWLER_KEY`가 있는 환경에서 `pytest`를 돌리자 테스트가 테스트 키 대신
실제 키를 읽었고, `test_viewer`의 실패 메시지에 실제 키의 **암호화 하위 키**(이미지 복호화 가능)가 세션 출력에 찍혔다.
커밋·푸시·업로드는 없었다. 조치: `tests/conftest.py`의 autouse 픽스처가 실제 비밀 환경 변수를 모두 지우고,
키 비교는 해시로 한다. 실제 데이터를 올리기 전이라 키 교체 여부를 사용자에게 물었다.

### 3-3. 클라우드에서 실제 크롤링 시작 (PC 작업이 끝난 뒤)

크롤러는 이 클라우드 환경에서 돌린다(사용자 결정). 컨테이너가 매번 비므로 키는 환경 변수, 기록은 `state_sync`로 R2에 둔다.

1. 확인: 새 세션에 `IMAGE_CRAWLER_KEY`가 있는지 (`env | grep -c IMAGE_CRAWLER_KEY`, 값은 출력하지 않는다)
2. scratchpad에 설정 (`<ACCOUNT_ID>`는 사용자에게 받는다, `prefix` 없음 = 실제 데이터, `key_file`은 없어도 됨):

   ```toml
   state_db   = "state.db"
   sites_file = "sites.txt"
   state_sync = true
   [storage]
   backend = "s3"
   [storage.s3]
   bucket       = "image-crawler"
   endpoint_url = "https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
   region       = "auto"
   ```

3. `image-crawler check` → `encryption key: IMAGE_CRAWLER_KEY (environment)`, `crawl state backup: none yet`
4. 사용자에게 받은 사이트를 `add` (사이트 목록은 공개 저장소에 커밋하지 않는다. `state_sync`가 암호화해 R2에 둔다)
5. 사이트마다 `crawl --site URL --max-pages 2`로 작게 시험 (클라우드 IP 차단, 네트워크 정책 차단 여부) → 휴대폰 갤러리에 보이는지 사용자 확인
6. 예약 작업으로 주기 실행 (새 세션 방식, 프롬프트에 위 설정 만들기 + `crawl` + `usage --check` 포함. 계정 ID는 프롬프트에만)
7. 테스트 데이터 `verify-test/`(약 2.9 MB, 테스트 키는 사라져 열 수 없음)는 사용자 동의 후 삭제

state_sync 실제 R2 검증 (테스트 키 + `verify-test/`): 기존 폴더에서 `crawl` → 백업 세대 1 →
빈 폴더 + `IMAGE_CRAWLER_KEY`만으로 `sites`(목록 복원) → `crawl`(42개 already seen, 이미지 재다운로드 없음, 78초 → 5초) →
`usage`가 원래 폴더와 같은 저장량·요청 수. 업로드 자체의 Class A 1회는 스냅샷 뒤에 세어져 실행마다 1씩 덜 잡힌다(무시 가능).

### 3-4. 남은 개발 항목

| # | 항목 |
|---|---|
| 3.2 | 앱 코드 변조 감지 (배포된 코드가 바뀌면 경고) |
| 3.4 | 삭제 (원본·썸네일 삭제 + 목록에 삭제 표시) |
| 3.5 | 검색·필터 (사이트, 기간, 파일명) |
| 4.1 | 주기 실행 (클라우드 예약 작업, PC용 cron 예시) |
| 4.3 | 키 백업 점검 절차 문서화 |

사용자가 휴대폰에서 실제로 써 본 피드백을 반영해 순서를 정한다.

## 4. 알아둘 점

- 테스트는 실제 비밀 환경 변수(`IMAGE_CRAWLER_KEY`, `AWS_*`, `CLOUDFLARE_API_TOKEN` 등)를 자동으로 지운 채 돈다
  (`tests/conftest.py`). 새 테스트도 키 값을 직접 비교·출력하지 말고 해시로 비교한다.

- moto의 S3 서버는 이미 있는 객체에 대한 CORS 사전 요청(OPTIONS)에 403을 돌려준다 (실제 S3/R2와 다름).
  `tests/test_pwa.py`의 `r2_like` 중계가 버킷 CORS 규칙으로 대신 응답한다.
- 이 환경의 Python에서 `moto[server]` 설치 시 debian `blinker` 충돌이 나면
  `pip install --ignore-installed blinker` 후 다시 설치한다.
- PWA 테스트는 node + Playwright(사전 설치됨)를 쓴다. 없으면 자동으로 건너뛴다.
