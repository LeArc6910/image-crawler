# 작업 인계 (2026-10-31)

다음 세션은 이 문서와 [DEVELOPMENT_PLAN.md](DEVELOPMENT_PLAN.md)를 먼저 읽고 이어서 진행한다.

- 작업 브랜치: `claude/festive-lovelace-7mpo4j` (`claude/trusting-pascal-5q25bw`에서 이어 받음, `main` 없음)
- 테스트: `pip install -e '.[dev]'` 후 `NO_PROXY=127.0.0.1,localhost python -m pytest` → 56개 통과 상태
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
- 남은 것: 아래 4~5 (읽기 전용 토큰 + `viewer-export`, 실기기 확인)


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
