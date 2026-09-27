# 음악 파형 슬라이드 편집기

음원을 타임라인에 놓고, 파형을 보면서 이미지와 문구의 표시 시간을 맞춰 **MP4 영상**을 만드는 Windows 프로그램입니다. 여러 음원과 이미지를 사용할 수 있고, 모든 작업은 PC 안에서 처리됩니다.

**Windows용 다운로드:** [Google Drive에서 열기](https://drive.google.com/file/d/1wpl91SbwGffeCzujPD6Aezt0pU2hdmXb/view?usp=drive_link) · [웹 소개 페이지 소스](dist/index.html)

![편집기 전체 화면: 왼쪽 미디어 목록, 가운데 미리보기, 오른쪽 이미지 속성, 아래 클립 타임라인](docs/images/editor-overview.png)

## 무엇을 할 수 있나요?

- FLAC·WAV·MP3 음원을 여러 개 등록하고 원하는 시각에 배치합니다. 음원 사이의 빈 구간은 무음입니다.
- JPG·JPEG·PNG 이미지를 배치하고 위치·배율·움직임을 조절합니다. 이미지가 없는 구간은 검은 화면입니다.
- 이미지 위에 여러 줄 문구를 올리고 글꼴, 색상, 정렬, 외곽선, 반투명 배경을 설정합니다.
- 이미지가 맞닿는 지점에 **즉시 전환·크로스 디졸브·검정 페이드·좌우 슬라이드**를 적용합니다.
- 결과를 **1920×1080, 30fps, H.264 영상과 AAC 오디오**가 들어간 MP4로 저장합니다.

화면 구성부터 프로젝트 저장 방식, 음악 분석과 MP4 생성 흐름까지 더 자세히 알고 싶다면 [프로그램 상세 가이드](docs/프로그램_상세_가이드.md)를 참고하세요.

## 시작하기

Windows용 배포 파일은 위의 Google Drive 링크에서 받을 수 있습니다. 소스에서 실행하려면 **Python 3.10 이상**, Tkinter, FFmpeg, FFprobe가 필요합니다. FFmpeg·FFprobe를 `PATH`에 두거나 이 프로젝트의 `ffmpeg/bin`에 배치하세요.

```powershell
python -m pip install -r requirements.txt
python music_to_video.py
```

Python 없이 실행할 폴더를 직접 만들려면 아래 [배포 폴더 만들기](#배포-폴더-만들기)를 참고하세요.

## 3단계로 영상 만들기

### 1. 음원과 이미지를 등록하고 타임라인에 놓기

왼쪽의 **+ 음악**에서 음원을 선택합니다. 분석이 끝나면 파일 이름 옆에 ✓가 나타납니다. 첫 음원은 0초에 자동 배치됩니다. 추가 음원은 왼쪽 목록에서 아래 **음악 트랙**으로 끌어 놓으세요.

**+ 이미지**에서 사진을 등록한 뒤 **이미지 트랙**으로 끌어 놓습니다. 이미지를 선택하고 **선택 이미지를 재생 위치에 추가** 버튼을 눌러도 됩니다. 이미지의 기본 표시 길이는 5초입니다.

### 2. 클립의 시간과 화면을 편집하기

이미지·음원·텍스트 클립은 **가운데를 끌면 이동**, **왼쪽·오른쪽 끝을 끌면 시간 조절**이 됩니다. 이미지와 음원은 같은 트랙에서 겹칠 수 없으며, 겹치는 위치는 빨간 잔상으로 알려줍니다. 영상 길이는 가장 늦게 끝나는 음원 클립을 따릅니다.

타임라인의 **+ 텍스트**를 누르거나 **새 텍스트** 항목을 텍스트 트랙으로 끌어 놓습니다. 오른쪽 속성에서 문구를 입력하세요. 다른 곳을 클릭하거나 저장·내보내기를 시작하면 수정한 문구가 적용됩니다. `Enter`는 줄바꿈, `Ctrl+Enter`는 즉시 적용입니다.

![텍스트 클립 선택과 문구·표시 시간 편집 화면](docs/images/text-editing.png)

이미지 클립을 선택하면 오른쪽에서 시작·끝 시각, 위치·배율, 움직임과 전환 효과를 설정할 수 있습니다. **미리보기에서 이미지 이동**을 켜고 이미지를 끌어 위치를 바꾸세요. 모서리 핸들이나 `Alt+휠`로 배율을 조절합니다. 전환은 앞 이미지와 빈틈없이 맞닿아 있을 때 적용됩니다.

### 3. MP4 내보내기

오른쪽 위 **MP4 내보내기**를 누르고 파일 이름과 저장 폴더를 정한 뒤 **영상 만들기**를 누릅니다. 작업 중 진행률을 볼 수 있고 취소할 수 있습니다. 완료 후 결과 파일이나 저장 폴더를 열 수 있습니다. 기존 파일은 자동으로 덮어쓰지 않습니다.

![파일 이름과 저장 폴더를 지정하는 MP4 내보내기 창](docs/images/export-dialog.png)

## 타임라인 조작 요약

| 동작 | 방법 |
| --- | --- |
| 원하는 시각으로 이동 | 시간 눈금이나 파형 클릭, 재생 헤드 드래그 |
| 클립 이동·길이 변경 | 클립 본체 또는 양끝 드래그 |
| 정확히 붙이기 | **스냅**을 켜면 재생 헤드·클립 경계·0초·영상 끝에 가까워질 때 자동으로 붙음. 드래그 중 `Alt`로 잠시 해제 |
| 확대·스크롤 | `Ctrl+휠` 확대·축소, `Shift+휠` 가로 스크롤, **전체 맞춤** 버튼 |
| 편집 취소·복제·삭제 | `Ctrl+Z` / `Ctrl+Y`, `Ctrl+D`, `Delete`; 드래그 도중 `Esc` |

프로젝트는 **파일 > 저장**에서 JSON으로 저장합니다. 미디어는 프로젝트 기준 상대 경로로 참조하고, 파일을 옮겼다면 프로젝트를 다시 열 때 연결할 수 있습니다. 이전 v1 프로젝트도 열 수 있으며 첫 저장 때 `_v2.json` 이름을 제안합니다. 원본 음악·이미지 파일은 변경하지 않습니다.

## 출력 및 참고 사항

- MP4 / H.264 / yuv420p / 1920×1080 / 30fps / AAC 기본 320kbps
- 사진 비율 유지, 검은 여백, EXIF 회전 및 투명 PNG 반영
- 기본 글꼴은 Windows의 맑은 고딕. 글꼴을 찾지 못하면 대체 글꼴을 사용하고 화면에 알림
- 긴 이미지 움직임 구간은 프레임마다 계산하므로 정적 영상보다 내보내기에 시간이 더 걸릴 수 있음
- 음원 분석 캐시는 Windows 임시 폴더의 `MusicToVideo_PCM`에 보관되어 같은 파일을 다시 사용할 때 재활용됨

## 배포 폴더 만들기

빌드 의존성은 `requirements-build.txt`에 있습니다. PyInstaller와 FFmpeg·FFprobe 실행 파일이 필요합니다. `ffmpeg/bin/ffmpeg.exe`, `ffmpeg/bin/ffprobe.exe`, `ffmpeg/LICENSE`, `ffmpeg/README.txt`를 준비하거나 기존 로컬 배포 폴더의 FFmpeg를 사용하세요.

```powershell
python -m pip install -r requirements-build.txt
.\build_windows.bat
```

스크립트는 후보 EXE로 실제 MP4를 변환하고 규격을 검사한 뒤 `release/MusicToVideo` 폴더를 만듭니다. **폴더 전체**를 함께 배포해야 합니다. 포함된 FFmpeg 빌드의 GPLv3 고지는 [NOTICE-FFmpeg.txt](NOTICE-FFmpeg.txt)를, 재생 라이브러리의 고지는 `LICENSE-sounddevice.txt`와 `LICENSE-PortAudio.txt`를 확인하세요.

개발·변환 검증 기록과 아직 확인하지 못한 환경은 [VALIDATION.md](VALIDATION.md)에 정리했습니다.

## GitHub Pages 소개 페이지

[dist/index.html](dist/index.html)은 화면 이미지와 스타일을 포함한 **단일 HTML 파일**입니다. 별도 빌드나 외부 이미지 호스팅 없이 브라우저에서 열 수 있습니다. 다운로드 버튼은 위와 같은 Google Drive 주소를 사용합니다.

### 현재 저장소에서 게시하기

1. [저장소 Pages 설정](https://github.com/lahuman/makeMp4/settings/pages)에서 **Build and deployment → Source → GitHub Actions**를 선택합니다.
2. [Actions](https://github.com/lahuman/makeMp4/actions/workflows/pages.yml)에서 **Deploy GitHub Pages → Run workflow → main**으로 첫 배포를 실행합니다.
3. 배포가 성공하면 [https://lahuman.github.io/makeMp4/](https://lahuman.github.io/makeMp4/)에서 안내 페이지를 확인합니다.

[배포 워크플로](.github/workflows/pages.yml)는 `dist`만 게시합니다. 이후 `main`의 `dist/**` 또는 워크플로가 변경되면 자동으로 배포합니다. Pages를 활성화하기 전 실행이 실패했다면 설정을 마친 후 워크플로를 다시 실행하세요.

### Moon 프로젝트 목록에 등록하기

Moon 블로그의 프로젝트로 등록할 때는 `dist/index.html`의 내용 앞에 아래 Jekyll 머리말을 붙여 `/workspace/Moon/_posts/2026-09-27-music-to-video.html`로 저장합니다. `layout: null`은 안내 페이지 자체의 디자인을 사용하고, `project: true`는 기존 Projects 목록에 자동으로 표시되게 합니다.

```yaml
---
layout: null
title: "음악 파형 슬라이드 편집기"
date: 2026-09-27 00:00:00 +0900
excerpt: "음악 파형을 보며 음원·사진·문구를 배치하고 MP4로 저장하는 Windows용 로컬 편집기. 다운로드와 사용 방법을 안내합니다."
project: true
comments: false
permalink: /music-to-video/
---
```

Moon의 기존 배포 흐름을 사용합니다. 변경 사항을 Moon의 `master`에 푸시하면 기존 Jekyll 워크플로가 빌드하여 게시합니다. 배포 후 안내 페이지 주소는 [https://lahuman.github.io/music-to-video/](https://lahuman.github.io/music-to-video/)이며, [Projects 목록](https://lahuman.github.io/projects/)에도 나타납니다. 안내 페이지를 수정하면 Moon의 해당 HTML 본문도 함께 갱신하세요.
