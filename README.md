# image-crawler

등록해 둔 사이트들을 돌며 이미지를 수집하고, **내 컴퓨터에서 먼저 암호화한 뒤** 클라우드 저장소에 올리는 도구입니다.
저장소 업체에는 의미 없는 이름의 암호문 덩어리만 올라가므로, 업체가 어디든 내용·원본 URL·파일명을 볼 수 없습니다(zero-knowledge).

```
sites.txt ──► 크롤러 ──► 이미지 다운로드 ──► 썸네일 생성 ──► AES-256-GCM 암호화 ──► 저장소 (R2 등 S3 / rclone / 로컬)
                │                                              (내 키로, 로컬에서)
                └─ robots.txt 준수, 호스트별 요청 간격, 429 백오프, 중복 제거
```

원본과 함께 암호화된 **썸네일**과 **목록(인덱스)** 도 올라가므로, 휴대폰 PWA 뷰어(개발 중)가 원본을 받지 않고도
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
image-crawler cors --origin https://you.github.io   # PWA 뷰어 주소에서 버킷을 읽도록 허용 (S3/R2)
image-crawler usage                          # 이번 달 R2 사용량 / 한도
```

주기적으로 돌리려면 cron 등에 등록하면 됩니다. 이미 받은 URL과 같은 내용의 이미지는 자동으로 건너뜁니다.

```cron
0 */6 * * * cd /path/to/workdir && image-crawler crawl >> crawl.log 2>&1
# 매일 아침 사용량 점검: 한도의 80% 이상이면 종료 코드 1 → 원하는 알림 명령 연결
0 9 * * *   cd /path/to/workdir && image-crawler usage --check || <알림 명령>
```

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

## 수집 대상

- `<img>`의 `src`, `srcset`(가장 큰 해상도), lazy-load 속성(`data-src`, `data-original`, `data-lazy-src`, `data-srcset` 등)
- `<picture><source srcset>`, `og:image` / `twitter:image` 메타 태그, 이미지 파일로 직접 연결된 `<a href>`
- `max_depth` 만큼 같은 호스트의 링크를 따라가며 페이지당 위 항목 수집 (이미지 자체는 CDN 등 다른 호스트여도 수집)
- 파일 시그니처로 실제 이미지인지 확인하고, `min_image_bytes` 미만(아이콘·픽셀)과 `max_image_bytes` 초과는 제외

## 암호화 / 익명성 설계

| 항목 | 저장소 업체가 보는 것 |
| --- | --- |
| 이미지 내용 | AES-256-GCM 암호문 (키는 로컬에만 존재) |
| 원본 URL, 파일명, 형식, 수집 시각 | 암호문 *내부*에 포함 → 볼 수 없음 |
| 객체 이름 | 키가 있는 HMAC-SHA256 값 → 내용·출처 추측 불가, 같은 이미지는 같은 이름이라 중복 저장 안 됨 |
| 썸네일·목록 | 각각 암호화. 썸네일 이름도 HMAC이라 어느 원본의 것인지 알 수 없음 |
| 파일 크기 | 원본은 64 KiB, 썸네일·목록은 4 KiB 단위로 패딩 → 정확한 크기 숨김 |
| 암호문 바꿔치기 | 각 암호문은 자기 위치(종류·이름)에 묶여 있어, 서버가 다른 암호문으로 바꾸면 복호화 실패 |
| 계정 정보, 결제 정보, 접속 IP, 업로드 시각·총 용량 | **보임** (아래 참고) |

- 키 파일(`key_file`)을 잃어버리면 **누구도(본인 포함) 복구할 수 없습니다.** 여러 곳에 오프라인으로 백업하세요.
- `state_db`(로컬 크롤 기록)에는 원본 URL이 평문으로 남습니다. 이 기록도 민감하다면 암호화된 디스크에 두세요.
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
