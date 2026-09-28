# 선율담

선율담은 음악에 사진과 문구를 얹어 **MP4 영상**을 만드는 Windows 프로그램입니다. 파형을 보며 표시 시간을 맞출 수 있고, 사진이나 음원을 같은 시간에 여러 개 겹쳐 놓을 수도 있습니다. 작업은 모두 내 PC에서 처리합니다.

**Windows용 다운로드:** [Google Drive에서 열기](https://drive.google.com/file/d/1wpl91SbwGffeCzujPD6Aezt0pU2hdmXb/view?usp=drive_link) · [웹 소개 페이지 소스](dist/index.html)

![최신 편집기 화면: 겹친 이미지 2개와 음악 2개가 각각 별도 타임라인 행에 표시되고, 오른쪽에서 이미지 앞뒤 순서를 조절하는 모습](docs/images/editor-overview.png)

위 화면은 샘플 파일로 만든 예시입니다. 사진·음원 겹치기와 선율담이라는 이름은 현재 소스에 반영돼 있습니다. 기존 다운로드 파일에는 이전 이름과 기능이 남아 있을 수 있습니다.

## 무엇을 할 수 있나요?

- FLAC·WAV·MP3 음원을 여러 개 올려 함께 재생할 수 있습니다. 소리가 겹치면 하나로 섞이고, 음원이 없는 구간은 무음으로 남습니다.
- JPG·JPEG·PNG 사진을 겹쳐 놓고 앞뒤 순서와 위치, 크기, 움직임을 조절할 수 있습니다. 사진이 없는 구간은 검게 나옵니다.
- 사진 위에 여러 줄 문구를 넣고 글꼴·색상·정렬·외곽선·반투명 배경을 설정할 수 있습니다.
- 사진이 이어지는 곳에는 **즉시 전환·크로스 디졸브·검정 페이드·좌우 슬라이드**를 넣을 수 있습니다.
- 완성한 영상은 **1920×1080, 30fps, H.264 영상과 AAC 오디오** 형식의 MP4로 저장됩니다.

화면 구성이나 프로젝트 저장 방식이 궁금하다면 [프로그램 상세 가이드](docs/프로그램_상세_가이드.md)를 보세요. 음악 분석과 MP4 출력 과정도 설명해 두었습니다.

## 시작하기

Windows용 배포 파일은 위의 Google Drive 링크에서 받을 수 있습니다. 소스로 실행할 때는 **Python 3.10 이상**, Tkinter, FFmpeg, FFprobe가 필요합니다. FFmpeg와 FFprobe는 `PATH`에 등록하거나 프로젝트의 `ffmpeg/bin`에 넣으세요.

```powershell
python -m pip install -r requirements.txt
python seonyuldam.py
```

예전 실행 명령인 `python music_to_video.py`와 해당 모듈 가져오기도 계속 지원합니다.

Python 없이 실행할 폴더를 직접 만들려면 아래 [배포 폴더 만들기](#배포-폴더-만들기)를 참고하세요.

## 3단계로 영상 만들기

### 1. 음원과 이미지를 등록하고 타임라인에 놓기

왼쪽에서 **+ 음악**을 눌러 음원을 고르세요. 분석이 끝나면 파일 이름 옆에 ✓가 붙고 첫 음원이 0초에 놓입니다. 음원을 더 넣으려면 왼쪽 목록에서 아래 **음악 트랙**으로 끌어 놓으면 됩니다.

**+ 이미지**로 사진을 등록한 뒤 **이미지 트랙**에 끌어 놓으세요. 사진을 고르고 **선택 이미지를 재생 위치에 추가**를 눌러도 됩니다. 기본 표시 시간은 5초이고, 영상 끝을 넘으면 그 지점에서 잘립니다. 같은 시간에 사진을 놓아도 기존 사진은 지워지지 않습니다.

### 2. 클립의 시간과 화면을 편집하기

클립은 **가운데를 끌어 옮기고**, **양끝을 끌어 길이를 조절**합니다. 사진을 같은 시간에 여러 장 놓으면 타임라인에 행이 생기고 화면에도 앞뒤로 겹칩니다. 순서는 오른쪽의 **뒤로 보내기 / 앞으로 가져오기**에서 바꿀 수 있습니다. 음원도 겹쳐 놓으면 함께 재생됩니다. 합친 소리가 출력 범위를 넘으면 그 범위로 제한하며, 영상 길이는 가장 늦게 끝나는 음원을 기준으로 정합니다.

문구를 넣으려면 타임라인의 **+ 텍스트**를 누르거나 **새 텍스트**를 텍스트 트랙으로 끌어 놓으세요. 오른쪽에서 내용을 입력한 뒤 다른 곳을 클릭하거나 저장·내보내기를 시작하면 반영됩니다. `Enter`는 줄바꿈, `Ctrl+Enter`는 즉시 적용입니다.

![겹친 이미지와 음악 위에서 텍스트 클립을 선택하고 문구·표시 시간을 편집하는 화면](docs/images/text-editing.png)

사진 클립을 고르면 오른쪽에서 시작·끝 시각과 위치·배율, 움직임, 전환 효과를 바꿀 수 있습니다. 미리보기에서 사진을 끌어 위치를 옮기고, 모서리 핸들이나 `Alt+휠`로 크기를 조절하세요. 문구도 미리보기에서 옮기거나 양쪽 핸들로 상자 너비를 바꿀 수 있습니다. 전환 효과는 앞 사진과 빈틈없이 이어질 때 적용됩니다.

타임라인의 **구성 보기**를 열면 음악·사진·문구가 시간순으로 보입니다. 여기서 클립을 선택해 이동·복제·삭제할 수 있고, 검은 화면이나 무음 구간, 영상 끝을 넘어가는 클립도 확인할 수 있습니다.

### 3. MP4 내보내기

오른쪽 위 **MP4 내보내기**에서 파일 이름과 저장 폴더를 정하고 **영상 만들기**를 누르세요. 만드는 동안 진행률을 확인하거나 작업을 취소할 수 있습니다. 끝나면 영상 길이·파일 크기·저장 경로를 보여주고, 파일이나 폴더를 바로 열 수도 있습니다. 저장 경로 복사도 지원합니다. 같은 이름의 파일은 자동으로 덮어쓰지 않습니다.

![파일 이름과 저장 폴더를 지정하는 MP4 내보내기 창](docs/images/export-dialog.png)

## 타임라인 조작 요약

| 동작 | 방법 |
| --- | --- |
| 원하는 시각으로 이동 | 시간 눈금이나 파형 클릭, 재생 헤드 드래그 |
| 클립 이동·길이 변경 | 클립 본체 또는 양끝 드래그. 매우 짧은 클립은 `Shift`를 누르고 끌어 이동 |
| 이미지·음악 겹쳐 놓기 | 같은 종류의 트랙에 드롭하거나 클립을 같은 시간대로 이동. 겹치면 별도 행에 표시 |
| 이미지 앞뒤 순서 변경 | 이미지 클립 선택 후 오른쪽 속성의 **뒤로 보내기 / 앞으로 가져오기** |
| 정확히 붙이기 | **스냅**을 켜면 재생 헤드·클립 경계·0초·영상 끝에 가까워질 때 자동으로 붙음. 드래그 중 `Alt`로 잠시 해제 |
| 빈 구간 확인 | 이미지·음악 트랙의 검은 화면·무음 표시 또는 **구성 보기** |
| 확대·스크롤 | `Ctrl+휠` 확대·축소, `Shift+휠` 가로 스크롤, **전체 맞춤** 버튼 |
| 편집 취소·복제·삭제 | `Ctrl+Z` / `Ctrl+Y`, `Ctrl+D`, `Delete`; 드래그 도중 `Esc` |

편집한 내용은 **파일 > 저장**에서 JSON 프로젝트로 저장하세요. 음악과 사진은 프로젝트 파일을 기준으로 상대 경로가 기록됩니다. 나중에 파일을 옮겼다면 프로젝트를 다시 열면서 연결할 수 있습니다. 이전 v1 프로젝트도 열 수 있으며, 처음 저장할 때는 `_v2.json`이라는 이름을 제안합니다. 원본 파일은 건드리지 않습니다.

## 출력 및 참고 사항

- MP4 / H.264 / yuv420p / 1920×1080 / 30fps / AAC 기본 320kbps
- 사진 비율 유지, 검은 여백, EXIF 회전 및 투명 PNG 반영
- 기본 글꼴은 Windows의 맑은 고딕. 글꼴을 찾지 못하면 대체 글꼴을 사용하고 화면에 알림
- 긴 이미지 움직임 구간은 프레임마다 계산하므로 정적 영상보다 내보내기에 시간이 더 걸릴 수 있음
- 음원 분석 캐시는 Windows 임시 폴더의 `Seonyuldam_PCM`에 보관되어 같은 파일을 다시 사용할 때 재활용됨. 이전 `MusicToVideo_PCM` 캐시가 있으면 계속 사용

## 배포 폴더 만들기

빌드에 필요한 패키지는 `requirements-build.txt`에 있습니다. PyInstaller와 FFmpeg·FFprobe 실행 파일도 준비해야 합니다. `ffmpeg/bin/ffmpeg.exe`, `ffmpeg/bin/ffprobe.exe`, `ffmpeg/LICENSE`, `ffmpeg/README.txt`를 넣거나 기존 로컬 배포 폴더의 FFmpeg를 사용하세요.

```powershell
python -m pip install -r requirements-build.txt
.\build_windows.bat
```

스크립트는 만든 EXE로 샘플 MP4를 출력하고 규격까지 확인한 뒤 `release/Seonyuldam` 폴더를 만듭니다. 배포할 때는 **폴더 전체**가 필요합니다. FFmpeg 빌드의 GPLv3 고지는 [NOTICE-FFmpeg.txt](NOTICE-FFmpeg.txt), 재생 라이브러리 고지는 `LICENSE-sounddevice.txt`와 `LICENSE-PortAudio.txt`에 있습니다.

개발·변환 검증 기록과 아직 확인하지 못한 환경은 [VALIDATION.md](VALIDATION.md)에 정리했습니다.

## GitHub Pages 소개 페이지

[dist/index.html](dist/index.html)은 화면 이미지와 스타일이 모두 들어 있는 **단일 HTML 파일**입니다. 따로 빌드하거나 이미지를 호스팅할 필요 없이 브라우저에서 열 수 있습니다. 다운로드 버튼은 위의 Google Drive 주소로 연결됩니다.
선율담 배경 음악은 [YouTube 영상](https://youtu.be/Bo8o20NVfHU)을 삽입해 페이지를 열 때 자동 재생을 시도합니다. 브라우저가 소리 있는 자동 재생을 제한하면 방문자가 플레이어에서 재생할 수 있습니다.

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
title: "선율담"
date: 2026-09-27 00:00:00 +0900
excerpt: "선율담은 음악 파형을 보며 음원·사진·문구를 배치하고 MP4로 저장하는 Windows용 로컬 편집기입니다."
project: true
comments: false
permalink: /music-to-video/
---
```

Moon의 기존 배포 흐름을 사용합니다. 변경 사항을 Moon의 `master`에 푸시하면 기존 Jekyll 워크플로가 빌드하여 게시합니다. 배포 후 안내 페이지 주소는 [https://lahuman.github.io/music-to-video/](https://lahuman.github.io/music-to-video/)이며, [Projects 목록](https://lahuman.github.io/projects/)에도 나타납니다. 안내 페이지를 수정하면 Moon의 해당 HTML 본문도 함께 갱신하세요.
