# Сторонние компоненты

В репозитории нет весов моделей и нет скопированного кода GigaAMGUI.

| Компонент | Использование | Источник |
|---|---|---|
| GigaAM | Модель распознавания русской речи; upstream code под MIT | https://github.com/salute-developers/GigaAM |
| onnx-asr | ONNX inference и загрузка конвертированной модели, MIT | https://github.com/istupakov/onnx-asr |
| Silero VAD | Выделение речевых сегментов через onnx-asr | https://github.com/snakers4/silero-vad |
| FFmpeg | Декодирование аудио/видео в worker | https://ffmpeg.org/legal.html |
| yt-dlp 2026.8.19 | Извлечение метаданных публичных видео; PyPI wheel под Unlicense | https://github.com/yt-dlp/yt-dlp |
| yt-dlp-ejs 0.8.0 | Локально установленный solver для YouTube; Unlicense, MIT и ISC | https://github.com/yt-dlp/ejs |
| Deno 2.9.5 | JS runtime для solver; MIT | https://github.com/denoland/deno |

Модели загружаются во время работы из источников, выбранных установленной версией onnx-asr.
MIT-лицензия этого сервиса распространяется на его код, а не автоматически на все веса
и сторонние бинарники. Перед распространением собственного контейнера с включёнными весами
зафиксируйте модель/revision и приложите лицензии её model card и всех включённых компонентов.
Лицензия FFmpeg зависит от сборки; здесь устанавливается пакет Debian.
Пакеты yt-dlp, yt-dlp-ejs и Deno устанавливаются из PyPI с собственными LICENSE-файлами;
их исходный код и бинарники в этот репозиторий не скопированы. Deno запускается без выдачи
доступа к сети/файлам через флаги разрешений; дополнительные удалённые компоненты yt-dlp отключены.
