#!/usr/bin/env bash
# MeetingAgent 설치: Homebrew 의존성 → Python 가상환경(uv) → `meet` 명령 등록
#   ./install.sh                     기본 (전사 + 요약 + 업로드)
#   ./install.sh --with-diarization  화자 분리(pyannote, torch 포함 ~2GB) 추가
set -euo pipefail
cd "$(dirname "$0")"

EXTRA=()
[[ "${1:-}" == "--with-diarization" ]] && EXTRA=(--extra diarize)

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "Apple Silicon(M 시리즈) 맥이 필요합니다 (mlx-whisper)." >&2; exit 1
fi
command -v brew >/dev/null || { echo "Homebrew가 필요합니다: https://brew.sh" >&2; exit 1; }

echo "==> 의존성 (ffmpeg, uv)"
for pkg in ffmpeg uv; do
  command -v "$pkg" >/dev/null || brew install "$pkg"
done
command -v claude >/dev/null || echo "  ! claude CLI가 없습니다. Claude Code를 설치하고 로그인하세요."

echo "==> Python 환경 (.venv)"
uv sync --python 3.12 ${EXTRA[@]+"${EXTRA[@]}"}

echo "==> meet 명령 등록 (~/.local/bin/meet)"
mkdir -p "$HOME/.local/bin"
ln -sf "$PWD/bin/meet" "$HOME/.local/bin/meet"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) echo '  ! PATH에 ~/.local/bin 추가 필요:  echo '"'"'export PATH="$HOME/.local/bin:$PATH"'"'"' >> ~/.zshrc && exec zsh' ;;
esac

echo
echo "설치 완료. 다음:  meet setup"
