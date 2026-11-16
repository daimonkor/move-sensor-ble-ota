#!/bin/sh
# Включить хуки репозитория (.githooks): черновик CHANGELOG прошивки после коммита, проверка типа коммита.
cd "$(dirname "$0")/.." && git config core.hooksPath .githooks && chmod +x .githooks/* && echo "hooks: .githooks включены"
