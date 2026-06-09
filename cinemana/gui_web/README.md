# واجهة Aurora (gui_web) — ملاحظات المطوّر

توثيق تقنيّ للواجهة الرسوميّة الجديدة المبنيّة على [`pywebview`](https://pywebview.flowpython.org/)،
ولأهمّ المزالق (gotchas) التي تعلّمناها أثناء بنائها وتصحيحها. مكتوب لمن سيصونها لاحقاً.

> القاعدة الذهبيّة: **هذه الحزمة تستبدل الواجهة فقط.** محرّك التنزيل
> (`cinemana/service.py` وأخواته) لا يُمَسّ إطلاقاً — كل المنطق (HTTP، الاستكمال،
> المقاطع، الـ manifest، الترجمات) يبقى كما هو. الجسر هنا يربط الواجهة بـ
> `DownloadService` عبر نفس نموذج «خيط + طابور» المستخدم في واجهة Tkinter القديمة.

---

## 1) البنية والملفات

```
cinemana/gui_web/
  webview_host.py   # الجسر: صنف JsApi + مضخّة الأحداث (Python ⇄ JS)
  index.html        # هيكل صفحة Aurora
  app.css           # تصميم Aurora (الألوان، الكروت، الأشرطة المقسّمة، مرآة RTL)
  app.js            # منطق الواجهة (عرض فقط): الشجرة، التحديد، الأشرطة، i18n
  assets/cinemana-logo.png
```

- نقطة التشغيل: `app.py` → `cinemana.gui_web.run()` (الافتراضي).
  الواجهة القديمة: `python app.py --legacy-ui` → `cinemana.gui.run()`.
- الواجهة الأماميّة **JavaScript صِرف** (بلا React/CDN) لتعمل دون اتصال وبلا خطوة بناء.

---

## 2) نموذج الخيوط (threading) وتدفّق الأحداث

```
        JS  ──(window.pywebview.api.method)──►  Python (خيوط عاملة، ليست خيط الواجهة)
                                                      │
المحرّك (DownloadService) ──callbacks(Events)──► queue.Queue
                                                      │  (مضخّة daemon كل ~80ms)
        JS  ◄──(window.onAppEvent([...]))────  evaluate_js   ◄── _coalesce + batch
```

- استدعاءات **JS → Python** تنفّذها pywebview على **خيوط عاملة** (لا تُجمّد الواجهة)،
  لذا الدوال الطويلة (`fetch`/`start`) تُطلق خيطها وترجع فوراً.
- callbacks المحرّك (`on_log`, `on_status`, `on_progress`, `on_segments`,
  `on_segment_progress`, `on_rate`, `on_series_done`) **لا تلمس الواجهة أبداً** —
  تدفع dict إلى `queue.Queue` فقط.
- مضخّة `_pump_loop` (خيط daemon) تصرّف الطابور كل `PUMP_INTERVAL_S`، تُمرّره على
  `_coalesce`، ثم ترسل دفعة واحدة عبر `window.evaluate_js("window.onAppEvent([...])")`.

---

## 3) عقد الجسر (Bridge contract)

### JS → Python (`window.pywebview.api.*`)
| الدالة | الوظيفة |
|---|---|
| `get_bootstrap()` | تُرجِع كل ما تحتاجه الواجهة لأوّل رسم (اللغة، الاتجاه، جدول النصوص، الجلسة المستعادة، الخطّة، الحالات، القرص). متزامنة. |
| `ui_ready()` | تُعلِم Python أن `onAppEvent` جاهز ⇒ تُطلق المضخّة. |
| `fetch(url)` | يجلب الخطّة في خيط، ثم يدفع حدث `plan` ثم `hero`. |
| `start(payload)` | يبدأ/يستأنف التنزيل. `payload = {selected_nbs, height, dest, concurrency, segments}`. |
| `stop()` / `pause_all()` / `resume_all()` | أزرار التذييل العامّة. |
| `pause(nb)` / `resume(nb)` / `cancel(nb)` | قائمة الزرّ الأيمن لكل حلقة. |
| `browse_dest()` | حوار اختيار مجلّد؛ يُرجِع `{dest, disk}`، ويحفظه أيضاً كمجلّد افتراضي في `settings.json`. |
| `set_language(lang)` | يحفظ اللغة (`settings.json`). |
| `save_state(values)` | لقطة الجلسة عند تغيّر الإدخال/التحديد. `values = {url, dest, quality_height, concurrency, segments, selected_nbs}`. |
| `save_prefs(values)` | يحفظ تفضيلات تبويب الإعدادات (`default_quality`/`default_concurrency`/`default_segments`) في `settings.json`. |
| `get_library()` | يقرأ الـ manifest عند `dest` ويُرجِع `{dest, series:[{title, is_movie, counts, episodes:[…]}]}` لتبويب المكتبة (قراءة فقط؛ كائن `Manifest` محلّي لا يُخزَّن على `self`). |
| `open_path(path)` / `reveal_path(path)` | فتح الملف بالتطبيق الافتراضي / كشفه في مستكشف الملفات (محدَّد عند وجوده). |
| `win_minimize/maximize/close()` | أزرار شريط العنوان (نافذة frameless). |

> `get_bootstrap()` يُرجِع أيضاً كتلة `prefs = {dest, quality_height, concurrency, segments}` (من
> `settings.json`) لتعبئة تبويب الإعدادات وتغذية القِيَم الافتراضيّة لأوّل جلب جديد.

### التنقّل بين التبويبات (view router)
الشريط الجانبي فيه ثلاثة تبويبات: **تنزيل / مكتبة / إعدادات** (حُذف تبويب «قائمة الانتظار»).
في `app.js` دالة `showView(name)` تُبدّل صنف `hidden` على `#view-download`/`#view-library`/
`#view-settings` وتنقل تمييز `.active` بين عناصر `.nav-item`. الإخفاء لا يُتلف DOM شاشة التنزيل،
فأي تنزيل جارٍ يبقى يُحدَّث في الخلفيّة ويعود سليماً عند الرجوع. مفاتيح نصوص التبويبين الجديدين
في `EXTRA_STRINGS` (لا في `i18n.py` الممنوع تعديله).

### Python → JS (دفعة `window.onAppEvent([...])`)
كل عنصر `{kind, ...}`:
`log{msg}` · `plan{plan}` · `hero{hero}` · `fetch_error{err}` ·
`status{nb,status,extra}` · `progress{nb,done,total}` · `segments{nb,seg_totals}` ·
`seg_progress{nb,k,done}` · `rate{nb|null,speed,eta}` · `done{summary}`.

> الحالات الممكنة (status): `pending` · `downloading` · `paused` · `done` · `error`.

---

## 4) التعدّد اللغوي (i18n) ومرآة RTL

- `i18n.py` **ضمن الملفات الممنوع تعديلها**، لذا جدول `STRINGS` الكامل يُرسَل إلى JS عند
  الإقلاع، ويُدمَج مع `EXTRA_STRINGS` (مفاتيح Aurora الجديدة فقط: `nav_*`, `saving_to`،
  عناوين الخيارات…). دالة `t()` في JS تحاكي منطق Python وتدعم محدّد التنسيق `{n:02d}`
  (مثل `season_label`).
- تبديل اللغة يُعيد العرض في مكانه ويعكس التخطيط إلى RTL عبر `dir` + خصائص CSS
  المنطقيّة (`margin-inline`, `border-inline-start`, `inset-inline-start`…).

---

## 5) مزالق pywebview التي تعلّمناها (مهمّ)

### ⛔ (1) باگ التجميد — لا تجعل الحالة خصائص عامّة على كائن الـ API
pywebview يبني `window.pywebview.api` بأن **يمشي على كل خاصّة عامّة** في كائن الجسر
(`webview/util.py::get_functions`) و**يتعمّق recursively داخل أي خاصّة غير قابلة للنداء**.

كان `self.window` و`self.service` … خصائص **عامّة**، فتعمّق pywebview داخل:
- `self.window → window.native → AccessibilityObject.Bounds.Empty.Empty…` (تكرار لا نهائي
  ⇒ `RecursionError` لكل مسار، وبناء أثر 1000 إطار في كل مرّة)،
- و`self.service → ` كامل شبكة كائنات `requests`/`urllib3`.

النتيجة: «عاصفة معالجة» جمّدت الواجهة («Application is not responding» متكرّراً).

**الحلّ:** كل حالة البرنامج **خاصّة** (تبدأ بـ `_`) — الـ walker يتجاوز الأسماء التي تبدأ
بـ `_` (السطر `util.py:193`). يبقى عامّاً فقط دوال الـ API الحقيقيّة.
**فحص الصيانة:** كل اسم عامّ في `dir(api)` يجب أن يكون **دالة**؛ أي خاصّة بيانات عامّة = تسريب
سيُعيد الباگ.

### (2) سحب نافذة frameless يمشي لأعلى في الـ DOM
معالج السحب (`customize.js::onBodyMouseDown`) يصعد من الهدف نحو الجذر، فإن وجد أيّ سلف
يحمل `pywebview-drag-region` بدأ السحب. لذلك وضعنا الصنف على **`.tb-left` فقط** (مع
`flex:1`)، لا على شريط العنوان كلّه — وإلّا صارت أزرار التحكّم تسحب النافذة بدل النقر.

### (3) معالج الإغلاق (closing)
يُستدعى عبر `Event.set` الذي يفحص توقيع الدالة: دالة بلا وسائط تُنادى بلا وسائط، و**إرجاع
`False` يلغي الإغلاق**. لذا `_on_closing` يُرجِع `True` للسماح / `False` للإلغاء (مع حوار
التأكيد أثناء التنزيل). يحفظ الجلسة دائماً قبل الإغلاق.

### (4) `evaluate_js` رحلة متزامنة لخيط الواجهة — جمِّع الأحداث
على واجهة EdgeChromium، `evaluate_js` (وكذلك `run_js`) يعمل عبر `Control.Invoke` + انتظار
نتيجة على خيط الواجهة. لتقليل الكلفة تحت التنزيل السريع: المضخّة **تجمّع** الأحداث
(`_coalesce`) فتُبقي آخر قيمة فقط لكل `progress`/`seg_progress`/`rate` لكل مفتاح، وتُمرّر
الأحداث المنفصلة (log/status/segments/done…) بترتيبها. مثال: 1501 حدث ⇒ ~11.

### (5) متفرّقات
- `webview.FileDialog.FOLDER` (الثابت `FOLDER_DIALOG` صار deprecated).
- Playwright MCP يحجب `file://` ⇒ للاختبار البصري قدّم المجلّد عبر
  `python -m http.server` ثم افتح `http://127.0.0.1:<port>/index.html`.

---

## 6) التشغيل والتصحيح والاختبار

```bash
python app.py                 # واجهة Aurora
python app.py --legacy-ui     # واجهة Tkinter القديمة
python -m unittest discover -s tests   # 82 اختباراً للمحرّك (لا تلمس الواجهة)
```

**اختبار بصري سريع للواجهة دون المحرّك** (Chromium محرّكه نفسه محرّك WebView2):
1. `cd cinemana/gui_web && python -m http.server 8769`
2. افتح `http://127.0.0.1:8769/index.html` في المتصفّح.
3. عرّف `window.pywebview = { api: { get_bootstrap: async()=>BOOT, ui_ready(){}, … } }`
   وأطلق `window.dispatchEvent(new Event('pywebviewready'))`، ثم ادفع أحداثاً عبر
   `window.onAppEvent([...])` لمحاكاة تنزيل حيّ.

> ملاحظة: شغّل عبر `python app.py` (وليس `pythonw`) إن أردت رؤية سجلّ pywebview في الطرفيّة.
