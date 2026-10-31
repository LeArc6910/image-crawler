# image-crawler

등록해 둔 사이트들을 돌며 이미지를 수집하고, **내 컴퓨터에서 먼저 암호화한 뒤** 클라우드 저장소에 올리는 도구입니다.
저장소 업체에는 의미 없는 이름의 암호문 덩어리만 올라가므로, 업체가 어디든 내용·원본 URL·파일명을 볼 수 없습니다(zero-knowledge).

```
sites.txt ──► 크롤러 ──► 이미지 다운로드 ──► 썸네일 생성 ──► AES-256-GCM 암호화 ──► 저장소 (R2 등 S3 / rclone / 로컬)
                │                                              (내 키로, 로컬에서)
                └─ robots.txt 준수, 호스트별 요청 간격, 429 백오프, 중복 제거
```

원본과 함께 암호화된 **썸네일**과 **목록(인덱스)** 도 올라가므로, 휴대폰 PWA 뷰어(`pwa/`)가 원본을 받지 않고도
갤러리를 보여줄 수 있습니다. 앞으로의 개발 항목은 [docs/DEVELOPMENT_PLAN.md](docs/DEVELOPMENT_PLAN.md)에 있습니다.

## 설치

```bash
pip install -e '.[s3]'      # S3 호환 저장소를 쓸 때 (boto3 포함)
# rclone 백엔드를 쓸 때는 rclone 바이너리(v1.73 이상 권장)도 설치
```

## 사용법

```bash
cp config.example.toml config.toml          # 저장소 설정 편집
image-crawler keygen                         # 암호화 키 생성 → 반드시 오프라인 백업
image-crawler add https://example.com/gallery https://another.example/photos
image-crawler sites                          # 등록된 사이트 목록
image-crawler crawl -v                       # 수집 + 암호화 + 업로드
image-crawler restore --out ./restored       # 저장소에서 내려받아 복호화
image-crawler restore --out ./restored --from-dir ./downloaded-blobs   # 웹 UI 등으로 받은 *.bin 복호화
image-crawler remove https://another.example/photos
image-crawler reindex                        # 인덱스에 빠진 이미지(이전 버전 업로드분, 중단된 실행)의 썸네일·목록 보충
image-crawler compact                        # 목록 조각 병합 (크롤링 후 30개가 넘으면 자동 실행)
image-crawler restore --out ./r --limit 20   # 최대 20장만 복원
image-crawler cors --origin https://you.github.io   # PWA 뷰어 주소에서 버킷을 읽도록 허용 (S3/R2)
image-crawler usage                          # 이번 달 R2 사용량 / 한도
image-crawler check                          # 키·자격 증명·버킷 접근을 단계별로 점검
image-crawler viewer-export                  # 휴대폰 뷰어용 연결 정보(QR) 만들기
```

주기적으로 돌리려면 cron 등에 등록하면 됩니다. 이미 받은 URL과 같은 내용의 이미지는 자동으로 건너뜁니다.

```cron
0 */6 * * * cd /path/to/workdir && image-crawler crawl >> crawl.log 2>&1
# 매일 아침 사용량 점검: 한도의 80% 이상이면 종료 코드 1 → 원하는 알림 명령 연결
0 9 * * *   cd /path/to/workdir && image-crawler usage --check || <알림 명령>
```

## 휴대폰 뷰어 (PWA)

`pwa/`는 빌드 과정 없는 정적 웹앱입니다. 휴대폰 브라우저 안에서 목록·썸네일·원본을 R2에서 받아 **기기 안에서만**
복호화합니다. 휴대폰에는 비밀번호로 잠긴 연결 정보만 저장되고, 열 때마다 비밀번호를 입력합니다.
10분 동안 조작이 없거나 앱이 1분 넘게 백그라운드에 있으면 자동으로 잠깁니다.
한 번 받은 목록·썸네일은 **암호문 그대로** 휴대폰에 캐시되어, 다시 열 때는 R2에 목록 요청 1회만 보냅니다.
오류로 요청이 폭주하지 않도록 10분에 3,000회를 넘으면 요청을 멈춥니다.

1. **휴대폰용 R2 토큰 발급**: Cloudflare → R2 → API 토큰 → 해당 버킷 **Object Read only**
2. **뷰어 배포 (GitHub Pages)**: 저장소 Settings → Pages → Source를 **GitHub Actions**로 설정한 뒤,
   `main`에 반영하거나 Actions 탭에서 *Deploy viewer*를 수동 실행 → `https://<사용자>.github.io/<저장소>/`
   - 뷰어는 저장소(Cloudflare)와 **다른 회사**(GitHub)에서 배포하세요. 한 회사가 암호문과 앱 코드를 모두 쥐면,
     앱 코드를 바꿔 키를 빼돌릴 수 있기 때문입니다.
   - 비공개 저장소에서 Pages를 쓰려면 GitHub 유료 요금제가 필요합니다. 뷰어 코드에는 비밀이 없으므로 공개돼도 됩니다.
3. **CORS 등록**: `image-crawler cors --origin https://<사용자>.github.io`
4. **연결 정보 만들기**: `image-crawler viewer-export` → 휴대폰 토큰 ID/비밀키와 뷰어 비밀번호(10자 이상) 입력
   - 토큰이 읽기 전용인지(쓰기가 거부되는지) 자동으로 확인합니다.
   - 터미널에 QR이 표시됩니다. `--qr-png qr.png`로 이미지 저장도 가능합니다(사용 후 삭제).
5. **휴대폰**: 뷰어 주소 열기 → *QR 스캔* → 비밀번호 → *연결* → 브라우저 메뉴에서 **홈 화면에 추가**

연결 정보에는 원본 키가 들어가지 않습니다(복호화 키 + 읽기 전용 토큰만). 그래도 유출이 의심되면 R2에서 휴대폰
토큰을 폐기하고 새로 만드세요.

## R2 무료 한도 보호

Cloudflare는 R2에 사용 상한(하드 캡)을 제공하지 않습니다(예산 알림은 이메일만 보냄). 그래서 이 도구가
**모든 저장소 요청을 보내기 전에 직접 세고**, 설정한 한도에 닿으면 멈춥니다(`[quota]`, s3 백엔드에 적용).

| 항목 | 무료 한도/월 | 기본 한도 | 세는 요청 |
|---|---|---|---|
| 저장 용량 | 10 GB-month | 9 GB | 업로드한 바이트 |
| Class A | 100만 | 90만 | 업로드, 목록 조회(1,000개당 1회) |
| Class B | 1,000만 | 900만 | 다운로드, 존재 확인 |

- 원본은 설정 한도(9 GB)에서 멈추고, 이미 올라간 원본의 썸네일·목록은 무료 한도(10 GB)까지 마저 올려 짝을 맞춥니다.
- 한 번 실행에 올리는 원본 수에도 상한(`max_images_per_run`)이 있어, 오류로 폭주하는 것을 막습니다.
- 한도에 닿으면 `stopped early: ...`를 출력하고 종료 코드 3으로 끝납니다. 이미 한도면 크롤링을 시작하지 않습니다.
- `cloudflare_account_id` + `CLOUDFLARE_API_TOKEN`(Account Analytics 읽기 권한)을 설정하면 Cloudflare가 집계한
  **계정 전체** 사용량(휴대폰 뷰어 사용분 포함)과 이 PC의 집계 중 큰 값을 기준으로 삼습니다.
  설정하지 않으면 이 PC가 보낸 요청만 셉니다.
- Cloudflare 집계는 실시간이 아니고 청구 기준치도 아니므로 한도에 여유를 두세요. 마지막 안전장치로
  대시보드의 예산 알림(Manage Account → Billing → Billable Usage)도 켜 두기를 권장합니다.

## 클라우드에서 실행 (매번 빈 환경에서 시작하는 경우)

Claude Code 클라우드 세션, CI 같은 곳은 실행할 때마다 디스크가 비어 있습니다. 두 가지를 켜 둡니다.

1. **키를 환경 변수로**: `IMAGE_CRAWLER_KEY`에 키 파일 내용(한 줄짜리 base64)을 넣으면 키 파일 대신 이것을 씁니다.
   키는 PC에서 `image-crawler keygen`으로 만들어 오프라인에 백업한 뒤, 그 내용을 실행 환경의 비밀 환경 변수로 등록하세요.
2. **크롤 기록 동기화**: `config.toml`에 `state_sync = true`.
   명령을 시작할 때 저장소의 `state/crawl-state.bin`(크롤 기록 `state_db` + 사이트 목록, 암호화)을 받아 오고,
   명령이 기록이나 사이트 목록을 바꿨으면 끝날 때 다시 올립니다. 그래서 새 환경에서도
   - 이미 본 이미지를 다시 내려받지 않고,
   - R2 사용량 집계(무료 한도 보호)가 0부터 다시 시작하지 않으며,
   - `add`/`remove`로 바꾼 사이트 목록이 유지됩니다.

   백업마다 세대 번호가 붙어, 오래된 로컬 기록이 더 새로운 백업을 덮어쓰지 않습니다(더 새로운 백업이 있으면 그걸 받아 오고,
   기존 파일은 `state.db.bak`으로 남김). 동시에 두 곳에서 실행하는 것은 가정하지 않습니다. 겹치면 나중 실행은 백업을 덮어쓰지 않고 경고만 냅니다.
   `image-crawler check`가 백업의 세대·저장 시각·항목 수를 보여 줍니다.

```toml
key_file   = "~/.image-crawler/key"   # IMAGE_CRAWLER_KEY가 있으면 무시됨
state_db   = "state.db"
sites_file = "sites.txt"
state_sync = true
```

## 수집 대상

- `<img>`의 `src`, `srcset`(가장 큰 해상도), lazy-load 속성(`data-src`, `data-original`, `data-lazy-src`, `data-srcset` 등)
- `<picture><source srcset>`, `og:image` / `twitter:image` 메타 태그, 이미지 파일로 직접 연결된 `<a href>`
- `max_depth` 만큼 같은 호스트의 링크를 따라가며 페이지당 위 항목 수집 (이미지 자체는 CDN 등 다른 호스트여도 수집)
- 파일 시그니처로 실제 이미지인지 확인하고, `min_image_bytes` 미만(아이콘·픽셀)과 `max_image_bytes` 초과는 제외
- 이미지마다 찾은 페이지의 제목(첫 `<h1>`, 없으면 `<title>`)과 페이지 안 순서를 기록합니다.
  휴대폰 뷰어는 이것으로 **페이지(앨범)별 폴더**를 보여 주고, `restore`는 `사이트/앨범/001_…` 폴더로 풀어 놓습니다.

### 목록 → 앨범 구조의 사이트 (`[[crawl.rules]]`)

목록 페이지에 앨범 표지가 있고, 표지를 누르면 앨범 페이지가 나오는 사이트는 규칙을 하나 적어 두면
목록을 따라가며 **앨범 사진만** 앨범별 폴더로 모읍니다. 패턴은 링크의 경로(+`?쿼리`)에서 찾는 정규식입니다.

```toml
[[crawl.rules]]
host         = "gallery.example.com"
follow       = ['^/list/\d+$', '^/album/\d+$']  # 이 링크만 따라감 (목록 페이지, 앨범 페이지)
album_pages  = '^/album/\d+$'                     # 앨범 페이지: 다 받은 앨범은 다음 실행부터 다시 열지 않음
album_images = 'img.photo'                          # 앨범 페이지에서 앨범 사진만 고르는 CSS 선택자 (옆의 다른 앨범 표지 제외)
title        = 'h1'                                 # 폴더 이름 (생략하면 첫 <h1>, 없으면 <title>)
other_images = false                                # 목록 페이지의 표지 이미지는 받지 않음
max_depth    = 1000                                 # 목록 페이지가 "다음"으로 이어지므로 크게
```

- **"더 보기(More)" 버튼으로 나머지를 보여 주는 앨범**: 이런 페이지는 대개 앨범 전체 목록을 페이지 데이터(스크립트 안 JSON)에
  이미 담고 있고, 버튼은 보여 주는 개수만 늘립니다. 그 목록을 직접 읽도록 지정합니다.
  목록을 못 찾으면 화면에 보이는 사진(`album_images`)으로 대신합니다.

  ```toml
  album_list = '\\"images\\":\[(.*?)\]'        # 페이지 소스에서 목록을 찾는 정규식 (그룹 1 = 목록)
  album_item = '[0-9a-f-]{36}'                  # 목록 안의 각 항목
  album_url  = 'https://img.example.com/{}'     # 항목 → 이미지 주소 ({} 자리에 항목)
  ```

- 규칙을 바꾼 뒤 이미 끝낸 앨범을 다시 확인하려면 `crawl --redo-albums` (새로 찾은 사진만 받습니다).
  목록 페이지에서 표지로 먼저 받은 이미지가 앨범 사진이면, 그 앨범 폴더로 옮겨집니다(인덱스는 나중 항목이 우선).
- 목록 페이지는 실행마다 다시 읽어 새 앨범을 찾고, 끝까지 받은 앨범은 상태 DB에 기록해 건너뜁니다
  (중간에 오류가 난 앨범은 다음 실행에서 다시 엽니다). 한 번에 얼마나 받을지는 `--max-pages`로 조절합니다.
- 사이트 주소가 들어가는 규칙은 공개 저장소에 커밋하지 말고 각자의 `config.toml`에만 두세요.

## 암호화 / 익명성 설계

| 항목 | 저장소 업체가 보는 것 |
| --- | --- |
| 이미지 내용 | AES-256-GCM 암호문 (키는 로컬에만 존재) |
| 원본 URL, 파일명, 형식, 수집 시각 | 암호문 *내부*에 포함 → 볼 수 없음 |
| 객체 이름 | 키가 있는 HMAC-SHA256 값 → 내용·출처 추측 불가, 같은 이미지는 같은 이름이라 중복 저장 안 됨 |
| 썸네일·목록 | 각각 암호화. 썸네일 이름도 HMAC이라 어느 원본의 것인지 알 수 없음 |
| 크롤 기록 백업 (`state_sync`) | 원본 URL·사이트 목록이 든 기록 전체를 암호화한 한 덩어리 → 내용 볼 수 없음 |
| 파일 크기 | 원본은 64 KiB, 썸네일·목록·기록 백업은 4 KiB 단위로 패딩 → 정확한 크기 숨김 |
| 암호문 바꿔치기 | 각 암호문은 자기 위치(종류·이름)에 묶여 있어, 서버가 다른 암호문으로 바꾸면 복호화 실패 |
| 계정 정보, 결제 정보, 접속 IP, 업로드 시각·총 용량 | **보임** (아래 참고) |

- 키 파일(`key_file`)을 잃어버리면 **누구도(본인 포함) 복구할 수 없습니다.** 여러 곳에 오프라인으로 백업하세요.
- `state_db`(로컬 크롤 기록)에는 원본 URL이 평문으로 남습니다. 이 기록도 민감하다면 암호화된 디스크에 두세요.
  (`state_sync`로 저장소에 올라가는 사본은 암호화됩니다.)
- `IMAGE_CRAWLER_KEY`로 클라우드에 키를 맡기면, 그 실행 환경의 설정을 볼 수 있는 사람도 키를 얻을 수 있습니다.
- 업체가 내용을 볼 수 없더라도 *누가* 쓰는 계정인지는 알 수 있습니다. 계정 단위의 익명성까지 원하면
  익명 이메일, 익명성 있는 결제 수단, VPN/Tor 사용을 고려하세요.

## 저장소 백엔드

| backend | 대상 | 비고 |
| --- | --- | --- |
| `s3` | **Cloudflare R2**(권장), Backblaze B2, Wasabi, AWS S3 등 | 자격 증명은 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` 환경변수로 |
| `rclone` | Filen, MEGA, pCloud, Proton Drive, SFTP 등 rclone이 지원하는 모든 곳 | `rclone config`로 remote를 먼저 만들 것 |
| `local` | 로컬 폴더 | 테스트용, 또는 동기화 클라이언트가 올리는 폴더 |

R2 설정 절차는 [개발 계획서의 R2 설정](docs/DEVELOPMENT_PLAN.md#4-r2-설정-절차-phase-1-기준)을 참고하세요.

저장소 안의 배치:

```
ab/<name>.bin            원본 (64 KiB 패딩)
thumbs/cd/<thumb>.bin    썸네일, 최대 320px WebP (4 KiB 패딩)
index/<시각>-<난수>.bin  목록 조각 — 크롤링할 때마다 추가 (4 KiB 패딩)
state/crawl-state.bin    크롤 기록 + 사이트 목록 백업 — state_sync = true일 때만 (4 KiB 패딩)
```

어느 백엔드든 업로드 전에 이 도구가 직접 암호화하므로, 업체 자체의 E2EE 여부와 무관하게 내용은 보호됩니다.
(rclone `crypt` 원격을 겹쳐 쓸 필요는 없습니다.)

## 주의

- 다른 사람의 사이트에서 이미지를 수집하는 것은 저작권과 사이트 이용약관의 제약을 받을 수 있습니다.
  개인 보관 목적이라도 대상 사이트의 약관을 확인하고, 기본값인 robots.txt 준수와 요청 간격을 유지하세요.
- JavaScript로만 렌더링되는 사이트(무한 스크롤 등)의 이미지는 HTML에 없으므로 수집되지 않습니다.

## 개발

```bash
pip install -e '.[dev]'
pytest          # node가 있으면 PWA 복호화 코드(pwa/js/vault.js)와의 교차 테스트도 실행
```
