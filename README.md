# Kyobo eBook Export

macOS에 다운로드한 **교보 eBook 소장 도서를 독립적인 PDF 또는 EPUB 파일로 저장하는 CLI와 AI 에이전트용 스킬**입니다.

도서를 검색하고 선택하면 스크립트가 격리 환경 준비, 추출, 파일 검증, 저장, 시험 환경 정리까지 처리합니다. 스킬은 사용자의 요청을 해석하고 책과 저장 위치를 선택하는 역할에 집중합니다.

교보문고의 공식 도구는 아닙니다. 현재는 아래에 명시된 앱 빌드와 로컬 저장 상태를 지원하는 실험적 구현입니다.

## 주요 기능

- **소장 서재 검색:** 제목이나 도서 ID로 검색하고, 같은 제목의 여러 형식을 구분합니다.
- **원본 형식 유지:** PDF는 복호화된 바이트를 그대로 저장합니다. EPUB은 복호화된 본문과 기존 이미지·글꼴·목차를 표준 컨테이너로 묶습니다.
- **로컬 처리:** 내보내기 과정에서 책이나 계정 데이터를 외부 서비스로 전송하지 않습니다.
- **원본 상태 보호:** 원본 앱을 실행·수정하는 대신 복사본을 사용하고, 작업 전후 원본 파일의 해시를 비교합니다.
- **결과 검증:** PDF의 전체 페이지 렌더링과 텍스트 추출, EPUB의 구조·리소스·이미지·ZIP 무결성을 검사합니다.
- **재실행 지원:** 검증된 기존 파일은 해시가 일치하면 재사용하고, 다른 파일을 덮어쓰지 않습니다.
- **자동 정리:** 작업이 끝나면 시험용 앱과 임시 실행 파일을 제거합니다. 도서별 실패와 정리 결과는 JSON으로 반환합니다.

## 지원 환경

| 항목 | 조건 |
| --- | --- |
| 운영체제 | Apple Silicon Mac에서 실행하는 macOS |
| Python | 3.10 이상, arm64 환경 |
| 개발 도구 | Xcode와 Mac Catalyst용 SDK, `xcrun`, `codesign` |
| PDF 도구 | Poppler의 `pdfinfo`, `pdftotext` |
| 교보 eBook | 3.7.10 빌드 1, 3.7.16 빌드 8의 검증된 실행 파일 |
| 도서 | 로컬에 다운로드된 영구 소장 PDF·EPUB |
| 로컬 저장소 | 사용 가능한 이전 인증서 저장소가 남아 있는 환경 |

**같은 버전 번호라도 실행 파일이나 프레임워크의 해시가 다르면 어댑터를 적용하지 않습니다.** 앱 업데이트, 새로운 기기나 계정, 로컬 인증서 상태에 따라 추가 지원이 필요할 수 있습니다. 저장소를 복제하는 것만으로 모든 환경에서 작동하는 도구는 아닙니다.

Python 패키지를 별도로 설치할 필요는 없습니다. Xcode는 Mac Catalyst 빌드가 가능한 SDK를 포함해야 합니다. Poppler가 없다면 Homebrew로 설치할 수 있습니다.

```sh
brew install poppler
```

## 시작하기

```sh
git clone https://github.com/jjangga0214/kyobo-ebook-export.git
cd kyobo-ebook-export

python3 .agents/skills/kyobo-ebook-export/scripts/kyobo.py doctor
```

`doctor`는 앱과 데이터 경로를 찾아 도구 설치 여부와 지원 빌드를 확인합니다. 도서 추출이나 앱 수정은 실행하지 않습니다. 실제 인증서와 도서의 읽기 가능 여부는 내보내기 단계에서 추가로 검사합니다.

### AI 에이전트에서 사용

스킬은 다음 경로에 있습니다.

```text
.agents/skills/kyobo-ebook-export/SKILL.md
```

OpenCode는 이 프로젝트에서 해당 경로를 자동으로 검색합니다. 저장소를 연 뒤 다음처럼 요청할 수 있습니다.

> 교보 소장 도서에서 원하는 제목을 찾아 지정한 폴더에 저장해 줘.

> 교보에 다운로드한 소장 도서를 모두 내 전자책 폴더로 내보내 줘.

**저장 위치는 고정돼 있지 않습니다.** 스킬은 사용자가 지정한 경로를 우선 사용하고, 지정하지 않았다면 현재 프로젝트나 대화 맥락에 맞는 위치를 정합니다. 여러 책이 검색되면 임의로 선택하지 않고 확인합니다.

스킬 파일을 새로 추가하거나 수정한 뒤에는 OpenCode를 재시작해 반영하세요. 다른 에이전트에서도 `SKILL.md`를 읽고 같은 스크립트를 실행할 수 있습니다.

### CLI로 직접 사용

아래 명령은 저장소 루트에서 실행합니다.

**도서 검색**

```sh
python3 .agents/skills/kyobo-ebook-export/scripts/kyobo.py list --query "찾을 책 제목"
```

검색 결과의 `id`에는 도서 ID와 형식이 함께 들어 있습니다. 반환된 값을 그대로 사용하면 제목 중복을 피할 수 있습니다.

**선택한 도서 내보내기**

```sh
python3 .agents/skills/kyobo-ebook-export/scripts/kyobo.py export \
  --book "검색 결과에서 반환된 ID" \
  --out "./exports"
```

`--book`은 여러 번 지정할 수 있습니다. `--out`은 필수이며 상대 경로와 절대 경로를 모두 받습니다. 위의 `./exports`는 사용 예시일 뿐입니다.

**전체 소장 도서 내보내기**

```sh
python3 .agents/skills/kyobo-ebook-export/scripts/kyobo.py export \
  --all \
  --out "$HOME/Documents/ebooks"
```

선택한 도서가 다운로드되지 않았다면 공식 앱에서 다운로드한 뒤 다시 실행해야 합니다. 대여·구독 도서는 현재 지원 대상이 아닙니다.

### 주요 옵션

| 옵션 | 용도 |
| --- | --- |
| `--book ID` | 정확한 ID 또는 하나의 책으로 좁혀지는 제목으로 선택 |
| `--all` | 전체 영구 소장 도서 선택 |
| `--out PATH` | 결과 저장 위치 지정. `export`에서 필수 |
| `--format pdf` / `--format epub` | 선택할 형식 제한 |
| `--app PATH` | 앱이 여러 개일 때 사용할 앱 경로 지정 |
| `--data PATH` | 데이터 컨테이너가 여러 개일 때 `Data` 경로 지정 |
| `--clock current` | 현재 시각으로만 로컬 라이선스 검사 |
| `--clock license-window` | 만료된 오프라인 캐시의 유효 시각을 격리 프로세스 안에서 재현. 기본값 |

`license-window`는 영구 소장 기록이 확인된 도서에만 적용됩니다. Mac의 시스템 시계나 원본 라이선스 파일을 변경하지 않으며, 적용 여부가 결과에 기록됩니다.

## 결과와 검증 범위

### PDF

PDF를 OCR·캡처·인쇄로 재구성하지 않습니다. 앱의 읽기 함수가 반환한 PDF 데이터를 저장하고 다음을 확인합니다.

- 일반 PDF 파서에서 열리는지와 페이지 수
- 암호화·잠금 여부
- 전체 문서의 텍스트 추출
- Apple PDFKit/CoreGraphics를 통한 모든 페이지 렌더링

화면에 잘 표시되는 PDF라도 복사·검색할 때 일부 문자가 다르게 해석될 수 있습니다. 대체문자나 폰트 경고는 결과에 남깁니다. 이 현상만으로 저자나 출판사의 제작 오류를 단정하지 않습니다.

### EPUB

EPUB은 글자 크기와 화면 크기에 따라 배치가 바뀌는 형식이므로 EPUB 리더가 필요합니다. 본문·스타일·이미지·글꼴을 유지해 새 ZIP 컨테이너로 포장하며, 최초 배포된 압축 파일과 바이트 단위로 같다는 의미는 아닙니다.

컨테이너, 패키지, 목차 순서, 리소스 참조, XML/XHTML, 이미지 디코딩과 ZIP CRC를 검사합니다. 원문 마크업을 임의로 고치지 않으며, 내장 검사는 EPUBCheck의 전체 규격 검사를 대체하지 않습니다.

## 작업 파일과 정리

선택한 출력 경로에는 책 파일과 `kyobo-export-manifest.json`이 생성됩니다. 이 매니페스트는 파일 해시, 검증 결과와 재사용 판단에 쓰입니다.

실행 기록과 상태 백업은 출력 경로의 `.kyobo-export/run-…/`에 로컬로 저장됩니다. 이 디렉터리에는 계정 관련 상태가 포함될 수 있으므로 저장소나 이슈에 첨부하지 마세요. 실행이 강제로 중단됐다면 해당 실행 경로를 지정해 임시 환경을 정리할 수 있습니다.

```sh
python3 .agents/skills/kyobo-ebook-export/scripts/kyobo.py cleanup \
  --run "/출력경로/.kyobo-export/run-실행ID"
```

이 공개 저장소에는 책 파일, 계정 상태, 인증서·라이선스 저장소, 실행 로그를 포함하지 않습니다. `books/`, `.local/`, `.kyobo-export/` 및 전자책 파일은 Git 추적에서 제외됩니다.

## 문제 해결

| 결과 코드 | 확인할 내용 |
| --- | --- |
| `book_selection_required` | 후보를 확인하고 정확한 ID나 형식으로 다시 선택 |
| `books_unavailable` | 해당 소장 도서의 다운로드와 지원 형식 확인 |
| `missing_dependencies` | 표시된 로컬 도구 설치 여부 확인 |
| `unsupported_app_build` / `unsupported_framework_build` | 현재 빌드에 맞는 새 어댑터 필요 |
| `legacy_repository_unavailable` | 현재 지원 경로에 필요한 이전 인증서 저장소가 없음 |
| `extraction_failed` | 해당 실행의 로컬 오류 코드와 로그 확인 |
| `output_collision` | 기존 파일을 보존하고 다른 출력 경로 선택 |
| `output_busy` | 같은 출력 경로를 사용하는 실행이 끝날 때까지 대기 |

## 개발과 기여

```sh
python3 -m unittest discover -s .agents/skills/kyobo-ebook-export/tests -v
```

테스트는 실제 계정이나 도서 파일 없이 선택 로직, 경로 처리, 출력 충돌, 정리 범위 등을 검사합니다.

새 앱 빌드를 지원하려면 버전 문자열이나 해시만 추가하지 말고 실제 실행 파일 구조와 함수 시그니처를 확인해야 합니다. 구현은 [implementation.md](.agents/skills/kyobo-ebook-export/references/implementation.md), 어댑터 갱신 절차는 [maintenance.md](.agents/skills/kyobo-ebook-export/references/maintenance.md)를 참고하세요.

## 라이선스

프로젝트 코드는 [MIT License](LICENSE)로 제공합니다. 교보 앱이나 전자책 파일은 이 저장소에 포함되지 않습니다.
