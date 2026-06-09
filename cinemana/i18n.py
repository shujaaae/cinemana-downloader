"""Tiny in-process internationalisation layer (Arabic / English).

One flat ``STRINGS`` table is the single source of truth for **every** user-facing
string — both the GUI widgets and the backend log/exception messages. A single
module-level ``_LANG`` global selects the active language; ``t(key, **kwargs)``
formats the right variant.

The backend (service / downloader / segmented) reads ``_LANG`` directly via
``t()`` — there is exactly one active language per process, so no language needs
to be threaded through the engine. Messages are formatted at call time on worker
threads but only ever *rendered* on the UI thread, so flipping the language
mid-download simply makes the next log line use the new language (intended);
lines already written stay as they were.

``t()`` never raises: a missing key returns the key itself, a missing language
falls back to Arabic, and a bad ``.format`` placeholder returns the raw template.
"""

from __future__ import annotations

DEFAULT_LANG = "ar"
LANGUAGES = ("ar", "en")

_LANG = DEFAULT_LANG


def set_language(lang: str) -> None:
    """Set the active language. Unknown values fall back to the default."""
    global _LANG
    _LANG = lang if lang in LANGUAGES else DEFAULT_LANG


def get_language() -> str:
    return _LANG


def t(key: str, **kwargs) -> str:
    """Return ``key``'s string in the active language, formatted with ``kwargs``.

    Fallback chain: missing key -> the key itself; missing language -> Arabic ->
    the key; bad placeholder -> the un-formatted template. Never raises.
    """
    entry = STRINGS.get(key)
    if entry is None:
        return key
    template = entry.get(_LANG) or entry.get(DEFAULT_LANG) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


# Every value is {"ar": ..., "en": ...}. Arabic mirrors the original wording so
# the app reads identically to before in Arabic.
STRINGS: dict[str, dict[str, str]] = {
    # -- window / title -------------------------------------------------------
    "app_title": {"ar": "محمّل سينمانا - Cinemana Downloader",
                  "en": "Cinemana Downloader"},
    "no_series_yet": {"ar": "لم يُجلب أي مسلسل بعد.",
                      "en": "No series fetched yet."},
    "title_with_kind": {"ar": "{title}  —  {kind}", "en": "{title}  —  {kind}"},
    "kind_movie": {"ar": "فيلم", "en": "Movie"},
    "kind_episodes": {"ar": "{n} حلقة", "en": "{n} episodes"},

    # -- top form -------------------------------------------------------------
    "lbl_url": {"ar": "رابط المسلسل:", "en": "Series URL:"},
    "btn_paste": {"ar": "لصق", "en": "Paste"},
    "btn_fetch": {"ar": "جلب الحلقات", "en": "Fetch episodes"},
    "lbl_dest": {"ar": "مجلد الحفظ:", "en": "Save folder:"},
    "btn_browse": {"ar": "استعراض", "en": "Browse"},
    "lbl_quality": {"ar": "الجودة:", "en": "Quality:"},
    "lbl_concurrency": {"ar": "تنزيلات متزامنة:", "en": "Concurrent downloads:"},
    "lbl_segments": {"ar": "مقاطع لكل ملف:", "en": "Segments per file:"},

    # -- selection toolbar ----------------------------------------------------
    "lbl_selection": {"ar": "الاختيار:", "en": "Selection:"},
    "btn_select_all": {"ar": "تحديد الكل", "en": "Select all"},
    "btn_select_none": {"ar": "لا شيء", "en": "None"},
    "btn_invert": {"ar": "عكس", "en": "Invert"},
    "hint_selection": {
        "ar": "  (نقر على المربّع لتحديد حلقة، أو زر الفأرة الأيمن لإيقاف/استئناف)",
        "en": "  (click a checkbox to pick an episode, or right-click to pause/resume)",
    },

    # -- tree headings --------------------------------------------------------
    "col_series_episode": {"ar": "المسلسل / الحلقة", "en": "Series / Episode"},
    "col_title": {"ar": "العنوان", "en": "Title"},
    "col_status": {"ar": "الحالة", "en": "Status"},
    "col_progress": {"ar": "التقدّم", "en": "Progress"},
    "col_speed": {"ar": "السرعة", "en": "Speed"},
    "col_eta": {"ar": "المتبقّي", "en": "ETA"},
    "col_size": {"ar": "الحجم", "en": "Size"},

    # -- status values --------------------------------------------------------
    "status_pending": {"ar": "بانتظار", "en": "Pending"},
    "status_downloading": {"ar": "يُحمّل", "en": "Downloading"},
    "status_paused": {"ar": "متوقّف مؤقتاً ⏸", "en": "Paused ⏸"},
    "status_done": {"ar": "مكتمل ✓", "en": "Done ✓"},
    "status_error": {"ar": "خطأ", "en": "Error"},

    # -- bottom buttons -------------------------------------------------------
    "btn_start": {"ar": "ابدأ التحميل", "en": "Start download"},
    "btn_stop": {"ar": "إيقاف الكل", "en": "Stop all"},
    "btn_pause": {"ar": "إيقاف مؤقت ⏸", "en": "Pause ⏸"},
    "btn_resume": {"ar": "استئناف ▶", "en": "Continue ▶"},

    # -- tree row labels ------------------------------------------------------
    "season_label": {"ar": "الموسم {n:02d}", "en": "Season {n:02d}"},
    "movie_label": {"ar": "الفيلم", "en": "The movie"},
    "log_panel_title": {"ar": "السجل", "en": "Log"},
    "active_downloads": {"ar": "التنزيلات النشطة", "en": "Active downloads"},

    # -- connection / aggregate labels ---------------------------------------
    "conn_label": {"ar": "الاتصالات: {n}×{m} = {total}",
                   "en": "Connections: {n}×{m} = {total}"},
    "conn_cap_warn": {"ar": "  ⚠ سيُخفَّض إلى {max}",
                      "en": "  ⚠ will be capped to {max}"},
    "agg_speed": {"ar": "السرعة الكلية: {sp}", "en": "Total speed: {sp}"},
    "agg_eta": {"ar": "المتبقّي: {et}", "en": "Remaining: {et}"},

    # -- entry context menu ---------------------------------------------------
    "menu_cut": {"ar": "قص", "en": "Cut"},
    "menu_copy": {"ar": "نسخ", "en": "Copy"},
    "menu_paste": {"ar": "لصق", "en": "Paste"},
    "menu_select_all": {"ar": "تحديد الكل", "en": "Select all"},

    # -- tree right-click menu ------------------------------------------------
    "menu_pause": {"ar": "إيقاف مؤقت ⏸", "en": "Pause ⏸"},
    "menu_resume": {"ar": "استئناف ▶", "en": "Resume ▶"},
    "menu_cancel": {"ar": "إلغاء ✖", "en": "Cancel ✖"},

    # -- dialogs / messageboxes ----------------------------------------------
    "dlg_paste_url_first": {"ar": "الرجاء لصق رابط المسلسل أولاً.",
                            "en": "Please paste the series URL first."},
    "dlg_choose_dest": {"ar": "الرجاء اختيار مجلد الحفظ.",
                        "en": "Please choose a save folder."},
    "dlg_no_episode": {"ar": "لم تختر أي حلقة. حدّد حلقة واحدة على الأقل.",
                       "en": "No episode selected. Pick at least one."},
    "dlg_fetch_failed": {"ar": "تعذّر جلب المسلسل:\n{err}",
                         "en": "Failed to fetch the series:\n{err}"},
    "dlg_close_while_downloading": {
        "ar": "التحميل جارٍ. الإيقاف والخروج؟ (يمكن الاستكمال لاحقاً)",
        "en": "A download is in progress. Stop and quit? (You can resume later.)",
    },
    "dlg_all_done": {"ar": "تم تحميل جميع الحلقات المختارة بنجاح ✓",
                     "en": "All selected episodes downloaded successfully ✓"},

    # -- GUI log lines --------------------------------------------------------
    "log_fetching_series": {"ar": "جاري جلب بيانات المسلسل...",
                            "en": "Fetching series data..."},
    "log_fetch_failed": {"ar": "فشل الجلب: {err}", "en": "Fetch failed: {err}"},
    "log_fetched_ready": {
        "ar": "تم الجلب: {title} ({kind}). اختر الحلقات والجودة ثم اضغط (ابدأ التحميل).",
        "en": "Fetched: {title} ({kind}). Pick episodes and quality, then press Start.",
    },
    "log_start_run": {
        "ar": "بدء تحميل {n} حلقة بجودة {q} ({nn}×{m} اتصال) إلى: {dest}",
        "en": "Starting download of {n} episodes at {q} ({nn}×{m} connections) to: {dest}",
    },
    "log_stopping": {"ar": "جاري إيقاف الكل بعد إنهاء القطع الحالية...",
                     "en": "Stopping all after current chunks finish..."},
    "log_pause_all": {"ar": "إيقاف جميع التنزيلات مؤقتاً...",
                      "en": "Pausing all downloads..."},
    "log_resume_all": {"ar": "استئناف التنزيلات المتوقّفة...",
                       "en": "Resuming paused downloads..."},
    "log_session_restored": {
        "ar": "تمت استعادة الجلسة السابقة. اضغط (استئناف) للمتابعة.",
        "en": "Previous session restored. Press Continue to resume.",
    },
    "log_unexpected_stop": {"ar": "توقف غير متوقع: {err}",
                            "en": "Unexpected stop: {err}"},
    "log_run_summary": {
        "ar": "انتهى. مكتمل: {done} / {total}  | أخطاء: {error}  | متوقّف: {paused}  | متبقٍّ: {pending}",
        "en": "Finished. Done: {done} / {total}  | errors: {error}  | paused: {paused}  | remaining: {pending}",
    },

    # -- service backend logs -------------------------------------------------
    "log_quality_fetch_failed": {"ar": "تعذّر جلب قائمة الجودات: {err}",
                                 "en": "Could not fetch quality list: {err}"},
    "log_conn_capped": {
        "ar": "تحديد الاتصالات إلى {n}×{m} (الحد الأقصى {max} اتصالاً).",
        "en": "Capping connections to {n}×{m} (max {max}).",
    },
    "log_disk_stop": {"ar": "توقف: {err}", "en": "Stopped: {err}"},
    "log_episode_error": {"ar": "خطأ في {label}: {err}",
                          "en": "Error in {label}: {err}"},
    "log_global_stopped": {"ar": "تم الإيقاف؛ سيُستكمل عند التشغيل التالي.",
                           "en": "Stopped; will resume on the next run."},
    "log_episode_start": {"ar": "بدء {label} — {title}",
                          "en": "Starting {label} — {title}"},
    "log_quality_fallback": {
        "ar": "تنبيه {label}: الجودة {want} غير متوفرة، سيتم استخدام {got}.",
        "en": "Note {label}: quality {want} unavailable, using {got}.",
    },
    "log_episode_done": {"ar": "اكتمل {label} ✓", "en": "Completed {label} ✓"},
    "log_subs_fetch_failed": {"ar": "تعذّر جلب الترجمات لـ {label}: {err}",
                              "en": "Could not fetch subtitles for {label}: {err}"},
    "log_sub_failed": {"ar": "تعذّر تحميل ترجمة {lang}.{ext} لـ {label}",
                       "en": "Could not download subtitle {lang}.{ext} for {label}"},
    # new verbose points
    "log_fetching_info": {"ar": "جاري جلب معلومات الفيديو...",
                          "en": "Fetching video info..."},
    "log_found_episodes": {"ar": "عُثر على {n} حلقة عبر {seasons} موسم.",
                           "en": "Found {n} episodes across {seasons} seasons."},
    "log_found_movie": {"ar": "هذا فيلم.", "en": "This is a movie."},
    "log_qualities": {"ar": "الجودات المتاحة: {qualities}",
                      "en": "Available qualities: {qualities}"},
    "log_manifest_loaded": {
        "ar": "تم تحميل التقدّم المحفوظ: {todo} للتنزيل، {done} مكتملة سابقاً.",
        "en": "Loaded saved progress: {todo} to download, {done} already done.",
    },
    "log_resolving": {"ar": "جاري تحديد رابط البث والجودة لـ {label}...",
                      "en": "Resolving stream URL and quality for {label}..."},
    "log_subs_summary": {"ar": "ترجمات {label}: {done}/{found} تم تنزيلها.",
                         "en": "Subtitles for {label}: {done}/{found} downloaded."},

    # -- downloader / segmented logs -----------------------------------------
    "log_server_ignored_resume": {
        "ar": "الخادم تجاهل الاستكمال؛ سيُعاد التحميل من البداية.",
        "en": "Server ignored resume; restarting the download from the beginning.",
    },
    "log_resume_mismatch": {
        "ar": "عدم تطابق في موضع الاستكمال؛ إعادة التحميل من البداية.",
        "en": "Resume position mismatch; restarting the download from the beginning.",
    },
    "log_url_expired": {
        "ar": "انتهت صلاحية الرابط؛ يتم تجديده والمتابعة من نفس النقطة.",
        "en": "The link expired; refreshing it and continuing from the same point.",
    },
    "log_network_retry": {"ar": "خطأ في الشبكة ({etype})؛ إعادة المحاولة #{n}...",
                          "en": "Network error ({etype}); retry #{n}..."},
    "log_resume_offset": {"ar": "استئناف من البايت {offset}.",
                          "en": "Resuming from byte {offset}."},
    "log_single_conn": {
        "ar": "سيتم التحميل باتصال واحد (الملف صغير أو الخادم لا يدعم التقسيم).",
        "en": "Downloading on a single connection (file is small or the server does not support splitting).",
    },
    "log_split": {"ar": "تقسيم الملف على {m} اتصالات ({mb:.1f} ميغابايت).",
                  "en": "Splitting file across {m} connections ({mb:.1f} MB)."},

    # -- backend exception messages (surface in the log via {err}) -----------
    "err_no_quality": {"ar": "لا توجد أي جودة متاحة لهذه الحلقة.",
                       "en": "No quality available for this episode."},
    "err_server_no_segments": {"ar": "الخادم لا يدعم تحميل المقاطع (تجاهل Range).",
                               "en": "The server does not support segmented downloads (ignored Range)."},
    "err_size_mismatch_retries": {"ar": "حجم الملف غير متطابق بعد عدة محاولات.",
                                  "en": "File size mismatch after several attempts."},
    "err_partial_only": {"ar": "اكتمل التحميل جزئياً فقط ({done}/{total}).",
                         "en": "Download completed only partially ({done}/{total})."},
    "err_url_refresh_failed": {"ar": "تعذّر تجديد الرابط بعد عدة محاولات.",
                               "en": "Could not refresh the link after several attempts."},
    "err_download_failed_attempts": {"ar": "فشل التحميل بعد {n} محاولات: {err}",
                                     "en": "Download failed after {n} attempts: {err}"},
    "err_disk_full": {"ar": "القرص ممتلئ — لا توجد مساحة كافية.",
                      "en": "The disk is full — not enough space."},
    "err_final_size_mismatch": {"ar": "الحجم النهائي غير متطابق ({size}/{total}).",
                                "en": "Final size mismatch ({size}/{total})."},
    "err_segment_incomplete": {"ar": "المقطع {k} غير مكتمل قبل الدمج ({size}/{need}).",
                               "en": "Segment {k} is incomplete before merging ({size}/{need})."},
    "err_merged_size_mismatch": {"ar": "حجم الملف المدموج غير متطابق ({size}/{total}).",
                                 "en": "Merged file size mismatch ({size}/{total})."},
}
