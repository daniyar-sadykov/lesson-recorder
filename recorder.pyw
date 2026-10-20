# -*- coding: utf-8 -*-
"""Диктофон уроков: мини-виджет поверх окон, пишет микрофон и звук компьютера в один m4a.

Запускать через pythonw.exe, тогда консоли не будет. Ярлык на рабочем столе
создаёт make_shortcut.ps1. Служебные имена устройств и ловушки описаны в README.md.
"""

import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import tkinter as tk
from tkinter import messagebox

# Служебные имена звуковых устройств. Русские имена ломаются при передаче в ffmpeg,
# поэтому только эти. Как получить заново, если сменится звуковая карта: см. README.md.
MIC_DEVICE = r"audio=@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{0D87F735-2CC7-4BBE-92CB-EE0B287F2D1B}"
MIX_DEVICE = r"audio=@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{235290CF-3D8B-4440-BA05-344A5F0669F4}"
DEVICE_LABELS = ("Микрофон", "Компьютер")

DEFAULT_RECORDS_DIR = r"C:\Users\Daniyar\Desktop\Work\Projects\Courses\vibecoding\Lessons records"
FFMPEG_FALLBACK = (
    r"C:\Users\Daniyar\AppData\Local\Microsoft\WinGet\Packages"
    r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.0.1-full_build\bin\ffmpeg.exe"
)

AUDIO_BITRATE = "96k"
METER_FLOOR_DB = -60.0
SOUND_THRESHOLD_DB = -45.0
SOUND_HOLD_SEC = 3.0
STOP_TIMEOUT_SEC = 15
TICK_MS = 120
CREATE_NO_WINDOW = 0x08000000

# Источники не склеиваются, а разводятся по каналам: микрофон сводится в моно и уходит
# в левый канал, звук компьютера в правый. Файл остаётся один, стерео 44100. Это нужно
# расшифровке: по каналу видно, где мой голос, а где голоса учеников.
#
# apad на каждом моно-потоке обязателен. У join, в отличие от amix, нет duration=longest:
# он заканчивается по самому короткому входу. Проверено 20.10.2026 на синтетических
# источниках: входы 3 и 8 секунд без apad дали файл на 3 секунды. То есть отвал одного
# dshow-устройства обрезал бы запись урока на этом месте. С apad поток живёт, пока запись
# не остановят, в замолчавшем канале пишется тишина.
#
# Ветка измерения уровней идёт мимо файла и не меняется: amerge склеивает оба устройства
# в четыре канала (1 и 2 микрофон, 3 и 4 компьютер), astats считает RMS по каждому,
# ametadata печатает его в лог. Печать в файл или stdout не годится: она буферизуется и
# приходит пачкой только при выходе, проверено 19.10.2026.
FILTER_GRAPH = (
    "[0:a]asplit=2[mic0][stat0];"
    "[1:a]asplit=2[sys0][stat1];"
    "[mic0]aformat=channel_layouts=mono,aresample=44100,apad[micmono];"
    "[sys0]aformat=channel_layouts=mono,aresample=44100,apad[sysmono];"
    "[micmono][sysmono]join=inputs=2:channel_layout=stereo[aout];"
    "[stat0][stat1]amerge=inputs=2,aresample=44100,asetnsamples=11025,"
    "astats=metadata=1:reset=1:measure_overall=none:measure_perchannel=RMS_level,"
    "ametadata=print,anullsink"
)

LEVEL_RE = re.compile(r"lavfi\.astats\.(\d+)\.RMS_level=(\S+)")

COLOR_BG = "#f4f4f4"
COLOR_TEXT = "#1d1d1d"
COLOR_MUTED = "#8a8a8a"
COLOR_LIVE = "#2f9e44"
COLOR_REC = "#d93025"
COLOR_TRACK = "#dcdcdc"


def ffmpeg_path():
    found = shutil.which("ffmpeg")
    if found:
        return found
    if os.path.exists(FFMPEG_FALLBACK):
        return FFMPEG_FALLBACK
    return None


def records_dir():
    return os.environ.get("LESSON_RECORDS_DIR") or DEFAULT_RECORDS_DIR


def parse_db(raw):
    """RMS из astats: при полной цифровой тишине приходит -inf или nan."""
    try:
        value = float(raw)
    except ValueError:
        return METER_FLOOR_DB
    if value != value or value < METER_FLOOR_DB:
        return METER_FLOOR_DB
    return min(value, 0.0)


def format_duration(seconds):
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%02d:%02d" % (minutes, secs)


def unique_target(directory):
    """ДД.ММ.ГГГГ ЧЧ.ММ.m4a, а при втором старте в ту же минуту с суффиксом (2)."""
    base = datetime.now().strftime("%d.%m.%Y %H.%M")
    candidate = os.path.join(directory, base + ".m4a")
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(directory, "%s (%d).m4a" % (base, counter))
        counter += 1
    return candidate


class Capture(object):
    """Один сеанс записи: процесс ffmpeg плюс чтение уровней из его лога."""

    def __init__(self, binary, path):
        self.binary = binary
        self.path = path
        self.proc = None
        self.started_at = 0.0
        self.lock = threading.Lock()
        self.channels = {}
        self.last_sound = [0.0, 0.0]
        self.got_levels = False
        self.log = deque(maxlen=14)

    def command(self):
        return [
            self.binary, "-hide_banner", "-nostats", "-loglevel", "info",
            "-f", "dshow", "-i", MIC_DEVICE,
            "-f", "dshow", "-i", MIX_DEVICE,
            "-filter_complex", FILTER_GRAPH,
            "-map", "[aout]", "-c:a", "aac", "-b:a", AUDIO_BITRATE,
            "-n", self.path,
        ]

    def start(self):
        self.proc = subprocess.Popen(
            self.command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        self.started_at = time.monotonic()
        threading.Thread(target=self._read_log, daemon=True).start()

    def _read_log(self):
        for raw in iter(self.proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            match = LEVEL_RE.search(line)
            if match:
                channel = int(match.group(1))
                level = parse_db(match.group(2))
                index = 0 if channel <= 2 else 1
                with self.lock:
                    self.channels[channel] = level
                    self.got_levels = True
                    if level > SOUND_THRESHOLD_DB:
                        self.last_sound[index] = time.monotonic()
            elif line.strip() and "frame:" not in line:
                with self.lock:
                    self.log.append(line.strip())

    def levels(self):
        """Текущий уровень и признак живого звука по каждому устройству."""
        now = time.monotonic()
        with self.lock:
            ready = self.got_levels
            pairs = (
                max(self.channels.get(1, METER_FLOOR_DB), self.channels.get(2, METER_FLOOR_DB)),
                max(self.channels.get(3, METER_FLOOR_DB), self.channels.get(4, METER_FLOOR_DB)),
            )
            recent = [now - stamp < SOUND_HOLD_SEC for stamp in self.last_sound]
        return ready, pairs, recent

    def log_tail(self, count=2):
        with self.lock:
            lines = [line for line in self.log if "Guessed Channel Layout" not in line]
        return " | ".join(lines[-count:])

    def elapsed(self):
        return time.monotonic() - self.started_at if self.started_at else 0.0

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        """Корректная остановка: q в stdin. Возвращает (всё_хорошо, сообщение)."""
        proc = self.proc
        if proc is None:
            return True, ""
        if proc.poll() is not None:
            return False, "ffmpeg завершился сам"
        for timeout in (STOP_TIMEOUT_SEC, 5):
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
            except OSError:
                pass
            try:
                proc.wait(timeout=timeout)
                return True, ""
            except subprocess.TimeoutExpired:
                continue
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return False, "ffmpeg не ответил на q, файл может быть повреждён"


class Meter(object):
    """Строка индикатора: подпись, полоса уровня, точка живого звука."""

    def __init__(self, parent, label, row):
        tk.Label(
            parent, text=label, bg=COLOR_BG, fg=COLOR_MUTED,
            font=("Segoe UI", 8), anchor="w", width=10,
        ).grid(row=row, column=0, sticky="w")

        self.bar = tk.Canvas(
            parent, width=132, height=8, bg=COLOR_TRACK,
            highlightthickness=0, bd=0,
        )
        self.bar.grid(row=row, column=1, padx=(0, 8), pady=3)
        self.fill = self.bar.create_rectangle(0, 0, 0, 8, fill=COLOR_LIVE, width=0)

        self.dot = tk.Canvas(parent, width=10, height=10, bg=COLOR_BG, highlightthickness=0, bd=0)
        self.dot.grid(row=row, column=2, sticky="w")
        self.light = self.dot.create_oval(1, 1, 9, 9, fill=COLOR_TRACK, width=0)

    def update(self, level_db, live):
        fraction = (level_db - METER_FLOOR_DB) / (0.0 - METER_FLOOR_DB)
        fraction = max(0.0, min(1.0, fraction))
        self.bar.coords(self.fill, 0, 0, int(132 * fraction), 8)
        self.bar.itemconfigure(self.fill, fill=COLOR_LIVE if live else COLOR_MUTED)
        self.dot.itemconfigure(self.light, fill=COLOR_LIVE if live else COLOR_TRACK)

    def reset(self):
        self.bar.coords(self.fill, 0, 0, 0, 8)
        self.dot.itemconfigure(self.light, fill=COLOR_TRACK)


class App(tk.Tk):
    def __init__(self):
        tk.Tk.__init__(self)
        self.title("Диктофон уроков")
        self.configure(bg=COLOR_BG)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.binary = ffmpeg_path()
        self.capture = None
        self.state = "idle"          # idle, recording, stopping, error
        self.stop_result = None

        self._build()
        self._place()
        if self.binary is None:
            self.state = "error"
            self.button.configure(state="disabled")
            self.set_status("ffmpeg не найден ни в PATH, ни по известному пути", error=True)
        self.after(TICK_MS, self.tick)

    def _build(self):
        head = tk.Frame(self, bg=COLOR_BG)
        head.pack(fill="x", padx=12, pady=(10, 2))

        self.state_dot = tk.Canvas(head, width=12, height=12, bg=COLOR_BG, highlightthickness=0, bd=0)
        self.state_dot.pack(side="left", pady=(6, 0))
        self.state_light = self.state_dot.create_oval(1, 1, 11, 11, fill=COLOR_TRACK, width=0)

        self.timer = tk.Label(
            head, text="00:00", bg=COLOR_BG, fg=COLOR_TEXT, font=("Segoe UI", 17, "bold"),
        )
        self.timer.pack(side="left", padx=(8, 0))

        self.button = tk.Button(
            self, text="Запись", command=self.on_button, font=("Segoe UI", 10, "bold"),
            relief="flat", bg="#e7e7e7", activebackground="#dadada", bd=0, pady=5, cursor="hand2",
        )
        self.button.pack(fill="x", padx=12, pady=(4, 8))

        meters = tk.Frame(self, bg=COLOR_BG)
        meters.pack(fill="x", padx=12)
        self.meters = [Meter(meters, DEVICE_LABELS[i], i) for i in range(2)]

        self.status = tk.Label(
            self, text="Готов к записи", bg=COLOR_BG, fg=COLOR_MUTED,
            font=("Segoe UI", 8), anchor="w", justify="left", wraplength=250,
        )
        self.status.pack(fill="x", padx=12, pady=(8, 10))

    def _place(self):
        self.update_idletasks()
        width = max(self.winfo_reqwidth(), 276)
        height = self.winfo_reqheight()
        self.geometry("%dx%d+%d+%d" % (width, height, self.winfo_screenwidth() - width - 40, 60))

    def set_status(self, text, error=False):
        self.status.configure(text=text, fg=COLOR_REC if error else COLOR_MUTED)

    def on_button(self):
        if self.state == "recording":
            self.begin_stop()
        elif self.state in ("idle", "error"):
            self.start_recording()

    def start_recording(self):
        directory = records_dir()
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            self.set_status("Не удалось создать папку записей: %s" % exc, error=True)
            return
        target = unique_target(directory)
        capture = Capture(self.binary, target)
        try:
            capture.start()
        except OSError as exc:
            self.set_status("ffmpeg не запустился: %s" % exc, error=True)
            return
        self.capture = capture
        self.state = "recording"
        self.stop_result = None
        self.button.configure(text="Стоп", state="normal")
        self.set_status("Подключаюсь к устройствам")

    def begin_stop(self):
        self.state = "stopping"
        self.button.configure(text="Останавливаю", state="disabled")
        self.set_status("Закрываю файл, это пара секунд")
        capture = self.capture
        duration = capture.elapsed()

        def worker():
            ok, message = capture.stop()
            self.stop_result = (ok, message, duration)

        threading.Thread(target=worker, daemon=True).start()

    def finish_stop(self, ok, message, duration):
        self.state = "idle" if ok else "error"
        self.button.configure(text="Запись", state="normal")
        self.timer.configure(text="00:00")
        self.state_dot.itemconfigure(self.state_light, fill=COLOR_TRACK)
        for meter in self.meters:
            meter.reset()
        path = self.capture.path
        name = os.path.basename(path)
        if not os.path.exists(path):
            self.set_status("%s: файл не создан. %s" % (name, message or self.capture.log_tail()), error=True)
            return
        size_mb = os.path.getsize(path) / (1024.0 * 1024.0)
        summary = "%s  %s  %.1f МБ" % (name, format_duration(duration), size_mb)
        if ok:
            self.set_status(summary)
        else:
            self.set_status("%s. %s" % (message, summary), error=True)

    def tick(self):
        if self.stop_result is not None:
            result = self.stop_result
            self.stop_result = None
            self.finish_stop(*result)

        if self.state in ("recording", "stopping") and self.capture is not None:
            capture = self.capture
            self.timer.configure(text=format_duration(capture.elapsed()))
            ready, levels, live = capture.levels()
            for index, meter in enumerate(self.meters):
                meter.update(levels[index], live[index])
            if self.state == "recording":
                blink = int(time.monotonic() * 2) % 2 == 0
                self.state_dot.itemconfigure(self.state_light, fill=COLOR_REC if blink else COLOR_BG)
                if ready and self.status.cget("text") == "Подключаюсь к устройствам":
                    self.set_status(os.path.basename(capture.path))
                if not capture.is_running():
                    self.state = "error"
                    self.button.configure(text="Запись", state="normal")
                    self.state_dot.itemconfigure(self.state_light, fill=COLOR_TRACK)
                    self.set_status(
                        "ffmpeg остановился сам: %s" % (capture.log_tail() or "причина не ясна"),
                        error=True,
                    )

        self.after(TICK_MS, self.tick)

    def on_close(self):
        if self.state in ("recording", "stopping"):
            if not messagebox.askyesno(
                "Идёт запись",
                "Запись ещё идёт. Остановить её и закрыть виджет?\n"
                "Если закрыть иначе, файл останется повреждённым.",
                parent=self,
            ):
                return
            if self.state == "recording":
                self.set_status("Закрываю файл")
                self.update_idletasks()
                ok, message = self.capture.stop()
                if not ok:
                    messagebox.showwarning("Остановка", message, parent=self)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
