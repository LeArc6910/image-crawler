# 로컬(PC) 세션 인계 — 실제 키 만들기 · 휴대폰 연결

이 문서는 **사용자 PC에서 실행하는 Claude Code 세션**이 읽고 진행하는 작업 목록이다.
크롤링 자체는 클라우드 세션이 맡고, PC에서는 "키가 있어야 하는 일"만 한다. 사용자와는 한국어로 대화한다.

## 0. 지켜야 할 것 (먼저 읽기)

- **키·토큰·비밀번호를 출력하거나 대화에 남기지 않는다.**
  - 키 파일(`key`) 내용을 `cat` 등으로 읽지 않는다. 복사·백업은 사용자가 직접 한다.
  - R2 토큰과 뷰어 비밀번호는 사용자가 **자기 터미널에서 직접** 입력한다(아래 3단계).
  - 사용자가 대화창에 붙여 넣으려 하면 말린다.
- `config.toml`, `key`, `state.db`, `sites.txt`는 커밋하지 않는다(저장소가 공개라 특히 중요).
- 계정 ID는 이 문서에 적지 않는다. 사용자에게 받는다.

## 1. 현재 상황 (2026-10-31 기준)

| 항목 | 상태 |
|---|---|
| R2 버킷 `image-crawler` | 있음. 크롤러 토큰(Object Read & Write)은 클라우드 환경에만 있음 |
| CORS | 등록·확인 완료 (`https://learc6910.github.io`의 GET/HEAD만 허용) |
| 뷰어 | 배포됨: https://learc6910.github.io/image-crawler/ |
| 휴대폰용 읽기 전용 토큰 | 사용자가 발급함 (Object Read only, `image-crawler` 버킷만) |
| **실제 암호화 키** | **아직 없음 → 이 세션에서 만든다** |
| 버킷 내용 | 테스트 데이터 `verify-test/`(테스트 키로 암호화, 약 2.9 MB)만 있음. 실제 데이터는 최상위에 쌓일 예정 |

## 2. 할 일

### 2-1. 설치

Python 3.11 이상 필요.

```bash
pip install "image-crawler[s3,qr] @ https://github.com/LeArc6910/image-crawler/archive/refs/heads/main.zip"
image-crawler --help
```

### 2-2. 작업 폴더와 설정

저장소 밖의 폴더(예: `~/image-crawler-pc/`)를 만들고 `config.toml`을 둔다. `<ACCOUNT_ID>`는 사용자에게 받는다.

```toml
key_file = "key"
state_db = "state.db"
[storage]
backend = "s3"
[storage.s3]
bucket       = "image-crawler"
endpoint_url = "https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
region       = "auto"
# prefix는 비워 둔다: 실제 데이터는 버킷 최상위 (verify-test/는 테스트용)
```

### 2-3. 실제 키 만들기 → 백업

```bash
image-crawler keygen
```

- 같은 폴더에 `key`(base64 한 줄)가 생긴다. **이 키 하나로 모든 사진을 암호화하고 연다.** 잃어버리면 복구할 수 없다.
- 사용자에게 **오프라인 백업을 두 곳 이상** 만들게 한다(USB, 비밀번호 관리자 등).
- 백업을 확인할 때는 원본과 사본의 해시만 비교하고 내용은 출력하지 않는다.
  - macOS/Linux: `shasum -a 256 key /백업/위치/key`
  - Windows PowerShell: `Get-FileHash key, D:\backup\key`
- `image-crawler keygen`을 **두 번 실행하지 않는다**(기존 키가 있으면 거부되지만, `--force`는 절대 쓰지 않는다).

### 2-4. 휴대폰용 연결 정보(QR) 만들기 — 사용자가 직접 실행

토큰과 비밀번호를 입력해야 하므로 **사용자가 자기 터미널에서** 실행한다(Claude의 셸에서 대신 실행하지 않는다).

```bash
cd ~/image-crawler-pc
image-crawler viewer-export
```

- 묻는 것: 읽기 전용 토큰의 Access Key ID → Secret Access Key → 뷰어 비밀번호(2번, 최소 길이 있음).
- 먼저 토큰이 쓰기 불가인지 확인한다. `✓ viewer token can read the bucket and cannot write to it`가 나와야 정상.
  쓰기 가능하다고 나오면 크롤러 토큰을 넣은 것이다. 읽기 전용 토큰으로 다시 한다.
- 터미널에 QR이 출력된다. 화면이 좁아 QR이 깨지면 `image-crawler viewer-export --qr-png qr.png` 후 이미지를 열고,
  **스캔이 끝나면 `qr.png`를 지운다.**

### 2-5. 휴대폰에서 연결

1. 휴대폰 브라우저로 https://learc6910.github.io/image-crawler/ 열기 (iPhone은 Safari)
2. **QR 스캔** → 비밀번호 입력 → **연결**
3. 홈 화면에 추가 (iOS: 공유 → 홈 화면에 추가 / Android: 메뉴 → 앱 설치)
4. 지금은 실제 키로 올린 사진이 없으므로 **갤러리가 비어 있는 게 정상**이다. 연결·잠금만 확인한다.

실기기 확인 목록 (결과를 사용자에게 받아 정리):

- [ ] QR 스캔이 카메라로 되는지 (iOS Safari 특히). 안 되면 "연결 정보" 칸에 텍스트 붙여 넣기로 대체되는지
- [ ] 홈 화면 아이콘으로 열리는지, 주소창 없이 앱처럼 뜨는지
- [ ] 10분 무조작 / 백그라운드 1분 후 자동 잠금, 비밀번호로 다시 열리는지
- [ ] 콘솔 오류나 화면 깨짐 (있으면 화면 캡처)

### 2-6. 클라우드 크롤러에 키 넘기기 — 사용자가 직접

클라우드 세션은 키 파일 대신 환경 변수 `IMAGE_CRAWLER_KEY`를 읽는다.

1. claude.ai에서 이 저장소의 클라우드 환경 설정을 연다 (세션 제목 표시줄의 클라우드 환경 메뉴 → Edit)
2. 환경 변수 추가: 이름 `IMAGE_CRAWLER_KEY`, 값은 `key` 파일의 **내용 한 줄** (사용자가 직접 복사해 붙여 넣기)
3. (선택) 무료 한도를 계정 전체 기준으로 지키려면 `CLOUDFLARE_API_TOKEN`도 추가
   (Cloudflare → My Profile → API Tokens → Account Analytics **Read**)
4. 저장 후 **새 클라우드 세션**부터 적용된다

주의: 이렇게 하면 클라우드 환경 설정을 볼 수 있는 사람도 키를 얻을 수 있다. 사용자가 이를 받아들인 상태다.

## 3. 클라우드 세션에 돌려줄 정보

비밀 값 없이 아래만 정리해서 사용자에게 주고, 사용자가 클라우드 세션에 전달한다.

- 키 생성·백업 완료 여부 (백업 위치 개수만, 경로·내용은 X)
- `viewer-export`의 읽기 전용 확인 결과 (✓ 줄이 나왔는지)
- 휴대폰 기종·브라우저, 2-5 확인 목록 결과와 문제점
- `IMAGE_CRAWLER_KEY`(와 `CLOUDFLARE_API_TOKEN`) 등록 여부

## 4. 그다음 (클라우드 세션이 할 일, 참고용)

`IMAGE_CRAWLER_KEY`가 들어간 새 클라우드 세션에서: 설정에 `state_sync = true`를 켜고 `check` →
사이트 등록(`add`) → 작은 첫 크롤링 → 휴대폰 갤러리에 사진이 보이는지 사용자 확인 → 예약 작업으로 주기 실행.
자세한 내용은 [HANDOFF.md](HANDOFF.md).
