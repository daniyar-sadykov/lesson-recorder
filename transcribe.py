# -*- coding: utf-8 -*-
"""Расшифровка записи урока: текстовый файл рядом с аудио.

Руками:

    python transcribe.py "C:\\...\\Lessons records\\19.10.2026 20.26.m4a"

Виджет запускает этот же файл сам после остановки записи, отдельным процессом с
пониженным приоритетом. Поэтому здесь нет ничего от окна: ни tkinter, ни диалогов.
Прогресс печатается в stdout строками "PROGRESS 0.37", остальное идёт в stderr.

Решения, формат текста и почему не на лету: docs/tasks/live-transcription/plan.md
"""

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

MODEL_NAME = "large-v3-turbo"
LANGUAGE = "ru"
COMPUTE_TYPE = "int8"
BEAM_SIZE = 5
SAMPLE_RATE = 16000

# Та же развилка, что в recorder.pyw: ffmpeg из PATH, иначе известный путь установки.
FFMPEG_FALLBACK = (
    r"C:\Users\Daniyar\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
)

# Метка, которой recorder.pyw подписывает свои записи с разведёнными каналами. Старые
# записи её не имеют: до 20.10.2026 оба источника склеивались в amix.
RECORDER_MARK = "channels=mic,system"

# Если метки нет, раскладку приходится угадывать по звуку. Смешанные записи дают в двух
# каналах почти одно и то же, разведённые не дают почти ничего общего. Замерено
# 20.10.2026 на отрезке из середины файла: 06.08 даёт +0,569, 19.10 даёт +0,982,
# разведённая запись 20.10 даёт +0,003. Порог посередине с большим запасом в обе стороны.
# Число каналов признаком не годится: у старых записей их тоже два.
SPLIT_CORRELATION = 0.3
SNIFF_SECONDS = 60

LABEL_ME = "Я"
LABEL_CLASS = "Ученики"
LABEL_MIXED = "один, смешанный"

# Турбо-модель рвёт речь мельче, чем small, и иногда посреди фразы: на срезе 120 секунд
# 63 сегмента против 34. Куски склеиваются в строку, пока она не дошла до MIN_LINE и не
# закончилась на знаке конца фразы. MAX_LINE страхует от реплики на полстраницы.
MIN_LINE = 80
MAX_LINE = 300
SENTENCE_END = ".!?…"

# Слова, которые модель стабильно искажает: «скел» вместо «скилл», «супобейс» вместо
# Supabase. initial_prompt настраивает её на лексику курса. Обратная сторона: иногда она
# вставляет подсказанное слово там, где его не было, поэтому первый текст по новой теме
# читается глазами. Список правится по мере находок в расшифровках.
TERMS = (
    "Скилл, Supabase, Claude Code, RLS, промпт, воркфлоу, n8n, коммит, пулреквест, "
    "деплой, токены, контекстное окно, спека, эпик."
)


def ffmpeg_path():
    found = shutil.which("ffmpeg")
    if found:
        return found
    if os.path.exists(FFMPEG_FALLBACK):
        return FFMPEG_FALLBACK
    raise SystemExit("ffmpeg не найден ни в PATH, ни по известному пути")


def note(text):
    """Диагностика идёт в stderr: stdout занят прогрессом, его читает виджет."""
    sys.stderr.write(text + "\n")
    sys.stderr.flush()


def format_timecode(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return "%d:%02d:%02d" % (hours, minutes, secs)


def audio_info(path):
    """Число каналов, длительность в секундах и подпись записи из метаданных."""
    import av

    with av.open(path) as container:
        stream = container.streams.audio[0]
        channels = stream.codec_context.layout.nb_channels
        if stream.duration and stream.time_base:
            duration = float(stream.duration * stream.time_base)
        else:
            duration = float(container.duration or 0) / 1000000.0
        comment = container.metadata.get("comment", "")
    return channels, duration, comment


def correlation(binary, path, duration):
    """Насколько похожи каналы: отрезок из середины, чтобы не читать файл целиком."""
    import numpy as np

    start = max(0.0, duration / 2.0 - SNIFF_SECONDS / 2.0)
    command = [
        binary, "-hide_banner", "-v", "error",
        "-ss", "%.3f" % start, "-t", str(SNIFF_SECONDS), "-i", path,
        "-ac", "2", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]
    raw = subprocess.run(command, capture_output=True, check=True).stdout
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)
    if samples.size < 2 * SAMPLE_RATE:
        return 1.0
    left, right = samples[0::2], samples[1::2]
    if left.std() == 0 or right.std() == 0:
        return 0.0
    return abs(float(np.corrcoef(left, right)[0, 1]))


def resolve_layout(binary, path, choice):
    """split: каналы разведены, mixed: в файле одна смесь. Возвращает список подписей."""
    channels, duration, comment = audio_info(path)
    split = [LABEL_ME, LABEL_CLASS]
    if choice == "split":
        if channels < 2:
            raise SystemExit("в файле один канал, разводка невозможна")
        return split, duration, channels, "задано ключом"
    if choice == "mixed":
        return [LABEL_MIXED], duration, channels, "задано ключом"
    if channels < 2:
        return [LABEL_MIXED], duration, channels, "в файле один канал"
    if RECORDER_MARK in comment:
        return split, duration, channels, "подпись записи в метаданных"
    value = correlation(binary, path, duration)
    if value < SPLIT_CORRELATION:
        return split, duration, channels, "каналы не похожи, корреляция %+.3f" % value
    return [LABEL_MIXED], duration, channels, "каналы похожи, корреляция %+.3f" % value


def channel_filter(channels, index):
    """Какой фильтр отдать ffmpeg: нужный канал или сведение всего файла в моно."""
    if index is not None:
        return "pan=mono|c0=c%d" % index
    if channels < 2:
        return "anull"
    return "pan=mono|c0=0.5*c0+0.5*c1"


def extract(binary, path, expression, target):
    """Один канал в wav 16 кГц моно. Отдельным файлом, чтобы в памяти не висели оба."""
    command = [
        binary, "-hide_banner", "-v", "error", "-y", "-i", path,
        "-af", expression, "-ar", str(SAMPLE_RATE), "-ac", "1",
        "-c:a", "pcm_s16le", target,
    ]
    subprocess.run(command, check=True)


class Progress(object):
    """PROGRESS 0.00 .. 1.00 построчно в stdout. Печать в файл не годится: буферизуется."""

    def __init__(self, passes):
        self.passes = passes
        self.sent = -1.0
        self.at = 0.0

    def update(self, index, fraction):
        fraction = max(0.0, min(1.0, fraction))
        total = (index + fraction) / float(self.passes)
        now = time.monotonic()
        if total - self.sent < 0.01 and now - self.at < 2.0:
            return
        self.sent, self.at = total, now
        sys.stdout.write("PROGRESS %.2f\n" % total)
        sys.stdout.flush()


def transcribe_channel(model, wav, duration, index, progress, language):
    """Один проход модели. Возвращает сегменты и длительность речи после VAD."""
    segments, info = model.transcribe(
        wav,
        language=language,
        beam_size=BEAM_SIZE,
        vad_filter=True,
        initial_prompt=TERMS,
    )
    collected = []
    span = duration or info.duration or 1.0
    for segment in segments:
        collected.append((segment.start, segment.end, segment.text.strip()))
        progress.update(index, segment.end / span)
    progress.update(index, 1.0)
    return collected, info.duration, info.duration_after_vad


def glue(segments, label):
    """Мелкие куски в читаемые строки. Время строки от первого куска."""
    lines = []
    start = None
    parts = []
    for begin, _end, text in segments:
        if not text:
            continue
        if start is None:
            start = begin
        parts.append(text)
        joined = " ".join(parts)
        long_enough = len(joined) >= MIN_LINE and joined[-1] in SENTENCE_END
        if long_enough or len(joined) >= MAX_LINE:
            lines.append((start, label, joined))
            start, parts = None, []
    if parts:
        lines.append((start, label, " ".join(parts)))
    return lines


def merge(groups):
    """Строки двух каналов в один список по времени. При равном времени «Я» первым."""
    lines = []
    for group in groups:
        lines.extend(group)
    lines.sort(key=lambda row: (row[0], 0 if row[1] == LABEL_ME else 1))
    return lines


def write_text(target, header, lines, labelled):
    """Сначала .part, потом переименование: недоделанный файл не выглядит готовым."""
    part = target + ".part"
    with open(part, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(header + "\n")
        for start, label, text in lines:
            if labelled:
                handle.write("[%s] %s: %s\n" % (format_timecode(start), label, text))
            else:
                handle.write("[%s] %s\n" % (format_timecode(start), text))
    os.replace(part, target)


def main():
    parser = argparse.ArgumentParser(description="Расшифровка записи урока в текст рядом с аудио")
    parser.add_argument("audio", help="путь к записи")
    parser.add_argument("--model", default=MODEL_NAME, help="модель, по умолчанию %s" % MODEL_NAME)
    parser.add_argument("--language", default=LANGUAGE, help="язык, по умолчанию %s" % LANGUAGE)
    parser.add_argument(
        "--layout", default="auto", choices=("auto", "split", "mixed"),
        help="раскладка каналов, по умолчанию определяется сама",
    )
    args = parser.parse_args()

    audio = os.path.abspath(args.audio)
    if not os.path.exists(audio):
        raise SystemExit("файла нет: %s" % audio)
    target = os.path.splitext(audio)[0] + ".txt"

    binary = ffmpeg_path()
    labels, duration, channels, reason = resolve_layout(binary, audio, args.layout)
    labelled = labels != [LABEL_MIXED]
    note("раскладка: %s (%s)" % (", ".join(labels), reason))
    note("длительность: %s" % format_timecode(duration))

    from faster_whisper import WhisperModel

    started = time.monotonic()
    model = WhisperModel(args.model, device="cpu", compute_type=COMPUTE_TYPE)
    note("модель загружена за %.1f с" % (time.monotonic() - started))

    progress = Progress(len(labels))
    groups = []
    workdir = tempfile.mkdtemp(prefix="lesson-transcribe-")
    try:
        for index, label in enumerate(labels):
            wav = os.path.join(workdir, "channel%d.wav" % index)
            extract(binary, audio, channel_filter(channels, index if labelled else None), wav)
            pass_started = time.monotonic()
            segments, heard, after_vad = transcribe_channel(
                model, wav, duration, index, progress, args.language
            )
            spent = time.monotonic() - pass_started
            note(
                "канал «%s»: речи %s из %s, сегментов %d, прошло %s, скорость x%.2f"
                % (
                    label, format_timecode(after_vad), format_timecode(heard),
                    len(segments), format_timecode(spent),
                    spent / heard if heard else 0.0,
                )
            )
            groups.append(glue(segments, label))
            os.remove(wav)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    lines = merge(groups)
    header = "# %s | %s | faster-whisper %s %s %s | каналы: %s | %s" % (
        os.path.basename(audio), format_timecode(duration), args.model, COMPUTE_TYPE,
        args.language, ", ".join(labels), datetime.now().strftime("%d.%m.%Y"),
    )
    write_text(target, header, lines, labelled)

    note("всего строк: %d, прошло %s" % (len(lines), format_timecode(time.monotonic() - started)))
    sys.stdout.write("DONE %s\n" % target)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
