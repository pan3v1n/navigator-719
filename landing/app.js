/* Лендинг 719-навигатор.рф: поведение макета без React и рантайма Claude Design.
   ⚠ Тексты демо и вкладок взяты из РЕАЛЬНЫХ ответов сервиса (07.10.2026), а ссылки — из их
   источников. Макет ссылался на «Правила выдачи заключения», утратившие силу 29.06.2024, и относил
   станки к разделу II — оба места исправлены по первоисточнику. */
(function () {
  'use strict';
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- шапка: тень после прокрутки ----
  var hdr = document.getElementById('hdr');
  function onScroll() { hdr.classList.toggle('scrolled', window.scrollY > 8); }
  window.addEventListener('scroll', onScroll, { passive: true }); onScroll();

  // ---- демо-чат на первом экране ----
  var DEMO = [
    { q: 'Какие документы нужны для внесения продукции в реестр российской промышленной продукции?',
      a: 'Заявка на включение сведений в реестр подаётся через ГИСП и рассматривается ТПП. К ней прилагают конструкторскую и технологическую документацию, копии лицензий и сертификатов соответствия, а также документы под выбранный критерий ПП № 719.',
      refs: ['Приказ ТПП РФ № 52, п. 4.1', 'Правила ведения реестра, п. 7'] },
    { q: 'Кто выдаёт акт экспертизы для подтверждения производства продукции?',
      a: 'Торгово-промышленная палата Российской Федерации — через уполномоченные территориальные палаты. Акт оформляется и размещается в ГИСП.',
      refs: ['Приказ ТПП РФ № 52, п. 2.1', 'п. 3.5'] },
    { q: 'Какие требования применяются к металлообрабатывающим станкам?',
      a: 'Станки входят в раздел I приложения — «Продукция станкоинструментальной промышленности». Требования балльные: например, управляющий программно-аппаратный комплекс российского производства даёт 25 баллов.',
      refs: ['Приложение, разд. I, поз. 13'] },
    { q: 'Как получить сертификат СТ-1, если продукции нет в приложении к ПП 719?',
      a: 'Подать в уполномоченную ТПП заявление о выдаче сертификата формы СТ-1 — это штатный путь для продукции, которой нет в приложении к ПП № 719.',
      refs: ['Приказ ТПП РФ № 14, прил. 3, п. 3.1'] }
  ];
  var dq = document.getElementById('demo-q'), dl = document.getElementById('demo-label'),
      da = document.getElementById('demo-a'), dr = document.getElementById('demo-refs'),
      dots = document.getElementById('demo-dots').children;
  var d = { i: 0, phase: 'typing', n: 0, dots: 0, caret: true }, caretEl = document.createElement('span');
  caretEl.className = 'caret';

  function paintDemo() {
    var it = DEMO[d.i], ans = d.phase === 'answer', out = d.phase === 'out';
    dq.textContent = it.q.slice(0, d.n);
    if (d.phase === 'typing' && d.caret) dq.appendChild(caretEl);
    dq.classList.toggle('gone', out);
    dl.textContent = d.phase === 'thinking' ? 'Навигатор ищет в тексте документов' + '.'.repeat(d.dots % 4) : 'Навигатор ПП 719';
    dl.classList.toggle('gone', d.phase === 'typing' || out);
    da.textContent = it.a;
    da.classList.toggle('fade', !ans);
    dr.classList.toggle('fade', !ans);
    for (var k = 0; k < dots.length; k++) dots[k].classList.toggle('on', k === d.i);
  }
  function renderRefs() {
    dr.innerHTML = '';
    DEMO[d.i].refs.forEach(function (r) { var s = document.createElement('span'); s.className = 'ref'; s.textContent = r; dr.appendChild(s); });
  }
  function tick() {
    var it = DEMO[d.i], wait = 0;
    if (d.phase === 'typing') {
      d.n += 2;
      if (d.n >= it.q.length) { d.n = it.q.length; d.phase = 'thinking'; d.dots = 0; wait = 350; } else wait = 28;
    } else if (d.phase === 'thinking') { d.dots++; wait = 280; if (d.dots > 5) { d.phase = 'answer'; wait = 4200; } }
    else if (d.phase === 'answer') { d.phase = 'out'; wait = 350; }
    else { d.i = (d.i + 1) % DEMO.length; d.n = 0; d.phase = 'typing'; wait = 200; renderRefs(); }
    paintDemo();
    setTimeout(tick, wait);
  }
  if (reduce) { d.phase = 'answer'; d.n = 999; renderRefs(); paintDemo(); }   // без анимации — готовый ответ
  else {
    d.n = 0; renderRefs(); paintDemo(); setTimeout(tick, 600);
    setInterval(function () { d.caret = !d.caret; if (d.phase === 'typing') paintDemo(); }, 450);
  }

  // ---- кому и зачем: вкладки ----
  var TABS = [
    { title: 'Производителю', lead: 'Подготовка к подтверждению производства без ручного поиска по тексту постановления и приложений.',
      actions: ['Определить, какие требования приложения применяются к вашей продукции', 'Узнать порядок внесения продукции в реестр и перечень документов', 'Подготовиться к экспертизе ТПП и проверить сведения заранее'],
      q: 'Какие требования применяются к металлообрабатывающим станкам?',
      a: 'Станки входят в раздел I приложения — «Продукция станкоинструментальной промышленности». Требования балльные: например, управляющий программно-аппаратный комплекс российского производства даёт 25 баллов.',
      ref: 'Приложение, разд. I', refFull: 'ПРИЛОЖЕНИЕ К ПОСТАНОВЛЕНИЮ № 719, РАЗДЕЛ I, ПОЗИЦИЯ 13' },
    { title: 'Эксперту ТПП', lead: 'Быстрая сверка с нормой при проведении экспертизы и подготовке акта.',
      actions: ['Найти норму для проверки заявленных технологических операций', 'Сверить формулировки акта экспертизы с текстом постановления и приказов ТПП РФ', 'Уточнить действующую редакцию пункта и дату изменений'],
      q: 'Кто выдаёт акт экспертизы для подтверждения производства продукции?',
      a: 'Торгово-промышленная палата Российской Федерации — через уполномоченные территориальные палаты. Акт оформляется и размещается в ГИСП.',
      ref: 'Приказ ТПП РФ № 52, п. 2.1', refFull: 'ПРИКАЗ ТПП РФ № 52, РАЗДЕЛ 2, П. 2.1' },
    { title: 'Закупщику', lead: 'Проверка российского происхождения продукции при подготовке и оценке заявок.',
      actions: ['Проверить, внесена ли продукция участника в реестр российской промышленной продукции', 'Понять, какими документами подтверждается производство в РФ', 'Сформулировать требования к участникам в документации'],
      q: 'Как участнику закупки подтвердить, что его продукция включена в реестр российской промышленной продукции?',
      a: 'Производство подтверждается одним из критериев пункта 1 ПП № 719 — например, актом экспертизы ТПП или сертификатом СТ-1. Заявку на включение сведений в реестр подают только через ГИСП.',
      ref: 'ПП № 719, п. 1', refFull: 'ПОСТАНОВЛЕНИЕ № 719, П. 1 — КРИТЕРИИ ПОДТВЕРЖДЕНИЯ ПРОИЗВОДСТВА' }
  ];
  var tabs = document.querySelectorAll('.seg [role="tab"]'), panel = document.getElementById('aud-panel'), cur = 0;
  function setText(id, t) { document.getElementById(id).textContent = t; }
  function goTab(i, focus) {
    cur = (i + TABS.length) % TABS.length;
    var t = TABS[cur];
    tabs.forEach(function (b, k) { b.setAttribute('aria-selected', String(k === cur)); b.tabIndex = k === cur ? 0 : -1; });
    if (focus) tabs[cur].focus();
    panel.setAttribute('aria-labelledby', 'tab-' + cur);
    setText('aud-title', t.title); setText('aud-lead', t.lead); setText('aud-q', t.q); setText('aud-a', t.a);
    setText('aud-ref', t.ref); setText('aud-ref-full', t.refFull); setText('aud-counter', '0' + (cur + 1) + ' / 03');
    var acts = document.getElementById('aud-acts'); acts.innerHTML = '';
    t.actions.forEach(function (text, k) {
      var row = document.createElement('div'); row.className = 'act';
      var n = document.createElement('div'); n.className = 'act-n'; n.textContent = '0' + (k + 1);
      var x = document.createElement('div'); x.className = 'act-t'; x.textContent = text;
      row.appendChild(n); row.appendChild(x); acts.appendChild(row);
    });
  }
  tabs.forEach(function (b, k) {
    b.addEventListener('click', function () { goTab(k); });
    b.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowRight') { e.preventDefault(); goTab(cur + 1, true); }
      if (e.key === 'ArrowLeft') { e.preventDefault(); goTab(cur - 1, true); }
    });
  });
  document.getElementById('aud-prev').addEventListener('click', function () { goTab(cur - 1); });
  document.getElementById('aud-next').addEventListener('click', function () { goTab(cur + 1); });

  // ---- витрина тарифов (09.10.2026, по образцу Нейроюриста; суммы — плейсхолдеры «X XXX») ----
  var ptabs = document.querySelectorAll('#price-tabs [role="tab"]'), ptitle = document.getElementById('pricing-h');
  function showPlans(kind) {
    ptabs.forEach(function (t) {
      var on = t.getAttribute('data-kind') === kind;
      t.setAttribute('aria-selected', String(on)); t.tabIndex = on ? 0 : -1;
      if (on) ptitle.textContent = t.getAttribute('data-title');
    });
    document.querySelectorAll('#pricing [data-panel]').forEach(function (p) { p.hidden = p.getAttribute('data-panel') !== kind; });
  }
  ptabs.forEach(function (t) { t.addEventListener('click', function () { showPlans(t.getAttribute('data-kind')); }); });
  document.querySelectorAll('[data-goto]').forEach(function (b) {
    b.addEventListener('click', function () { showPlans(b.getAttribute('data-goto')); ptitle.scrollIntoView({ block: 'start' }); });
  });
  document.querySelectorAll('#pricing .plan').forEach(function (card) {
    // суммы — плейсхолдеры, пересчитывать нечего: период меняет только подпись «/мес» ↔ «/год»
    card.querySelectorAll('.mini-seg input').forEach(function (r) {
      r.addEventListener('change', function () {
        card.querySelectorAll('.per').forEach(function (s) { s.textContent = r.value === 'year' ? 'год' : 'мес'; });
      });
    });
    var seats = card.querySelector('[data-seats]');
    if (!seats) return;
    card.querySelectorAll('.seat-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        var n = (parseInt(seats.value, 10) || 1) + parseInt(b.getAttribute('data-d'), 10);
        seats.value = Math.max(1, Math.min(n, parseInt(seats.max, 10)));
      });
    });
  });

  // ---- тариф: карточки и переключатель в форме ----
  // Условия карточки (вид, период, пользователи, проба за 1 ₽) едут в заявку; выбор тарифа чипом в
  // форме — заявка без условий. Доступ к источникам бесплатен во всех тарифах — выбирать нечего.
  var chips = document.querySelectorAll('#tariff-chips button'), tariff = 'Стандарт', opts = null,
      optsEl = document.getElementById('tariff-opts');
  function optsText(o) {
    if (!o) return '';
    var parts = [];
    if (o.kind === 'corporate') parts.push('корпоративный', o.period === 'year' ? 'год' : 'месяц', o.seats + ' польз.');
    if (o.trial) parts.push('пробная неделя за 1 ₽');
    return parts.join(' · ');
  }
  function setTariff(name, o) {
    tariff = name; opts = o || null;
    chips.forEach(function (c) { c.setAttribute('aria-pressed', String(c.textContent === name)); });
    var t = optsText(opts);
    optsEl.textContent = t ? 'Выбрано: ' + t : ''; optsEl.hidden = !t;
  }
  chips.forEach(function (c) { c.addEventListener('click', function () { setTariff(c.textContent); }); });
  document.querySelectorAll('[data-pick]').forEach(function (b) {
    b.addEventListener('click', function () {
      var card = b.closest('.plan'), o = null, name = b.getAttribute('data-pick');
      if (name !== 'Для организаций') {
        var corp = card.hasAttribute('data-corp'),
            per = card.querySelector('.mini-seg input:checked'), seats = card.querySelector('[data-seats]');
        o = { kind: corp ? 'corporate' : 'individual', trial: b.hasAttribute('data-trial'),
              period: per ? per.value : 'month', seats: seats ? Math.max(1, Math.min(parseInt(seats.value, 10) || 1, 500)) : 1 };
      }
      setTariff(name, o);
      showForm();
      // корпоративный тариф — только через связь с заказчиком: форма в попапе (вечер 09.10.2026)
      if ((o && o.kind === 'corporate') || name === 'Для организаций') { if (openDialog()) return; }
      var el = document.getElementById('form');
      window.scrollTo({ top: el.getBoundingClientRect().top + window.scrollY - 88, behavior: reduce ? 'auto' : 'smooth' });
    });
  });

  // ---- попап «Связаться с нами»: та же форма переезжает в <dialog> и возвращается при закрытии ----
  var dlg = document.getElementById('lead-dialog'), formbox = document.querySelector('#form .formbox'),
      home = formbox.parentNode, slot = document.getElementById('lead-dialog-slot');
  function openDialog() {
    if (!dlg || typeof dlg.showModal !== 'function') return false;   // старый браузер — форма внизу
    slot.appendChild(formbox); dlg.showModal();
    var first = formbox.querySelector('textarea[name="message"]'); if (first) first.focus();
    return true;
  }
  dlg.addEventListener('close', function () { home.appendChild(formbox); });
  document.getElementById('lead-dialog-close').addEventListener('click', function () { dlg.close(); });
  dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); });   // клик по фону

  // ---- форма заявки ----
  var form = document.getElementById('lead-form'), sent = document.getElementById('lead-sent'),
      submitBtn = document.getElementById('lead-submit'), formErr = document.getElementById('form-err'),
      consent = document.getElementById('consent'), consentL = document.getElementById('consent-l'),
      consentErr = document.getElementById('consent-err'), tried = false;
  function input(name) { return form.elements[name]; }
  function fmtPhone(v) {
    var d = v.replace(/\D/g, '');
    if (d[0] === '8') d = '7' + d.slice(1);
    if (d && d[0] !== '7') d = '7' + d;
    d = d.slice(0, 11);
    if (!d) return '';
    var p = d.slice(1), s = '+7';
    if (p.length) s += ' (' + p.slice(0, 3);
    if (p.length >= 3) s += ')';
    if (p.length > 3) s += ' ' + p.slice(3, 6);
    if (p.length > 6) s += '-' + p.slice(6, 8);
    if (p.length > 8) s += '-' + p.slice(8, 10);
    return s;
  }
  input('inn').addEventListener('input', function (e) { e.target.value = e.target.value.replace(/\D/g, '').slice(0, 12); revalidate(); });
  input('phone').addEventListener('input', function (e) { e.target.value = fmtPhone(e.target.value); revalidate(); });
  ['name', 'org', 'email', 'promo'].forEach(function (n) { input(n).addEventListener('input', revalidate); });
  consent.addEventListener('change', revalidate);

  function validate() {
    var e = {}, v = function (n) { return input(n).value; };
    if (!v('name').trim()) e.name = 'Укажите имя';
    if (!v('org').trim()) e.org = 'Укажите организацию';
    if (!/^\d{10}(\d{2})?$/.test(v('inn'))) e.inn = 'ИНН должен содержать 10 или 12 цифр';
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v('email').trim())) e.email = 'Проверьте правильность email';
    if (v('phone').replace(/\D/g, '').length !== 11) e.phone = 'Укажите телефон полностью';
    if (!consent.checked) e.consent = 'Необходимо согласие на обработку данных';
    return e;
  }
  function showErrors(e) {
    form.querySelectorAll('.field').forEach(function (f) {
      var k = f.getAttribute('data-f'), msg = e[k] || '';
      f.classList.toggle('err', !!msg);
      f.querySelector('.ferr').textContent = msg;
      f.querySelector('input, textarea').setAttribute('aria-invalid', String(!!msg));
    });
    consentL.classList.toggle('err', !!e.consent);
    consentErr.textContent = e.consent || '';
    // Ошибка по полю, у которого нет своего места в форме (например, тариф), не должна пропасть
    // молча: кнопка снова активна, а пользователь не понимает, что не так (ревью PR #158).
    var shown = { consent: 1 };
    form.querySelectorAll('.field').forEach(function (f) { shown[f.getAttribute('data-f')] = 1; });
    var rest = Object.keys(e).filter(function (k) { return !shown[k]; }).map(function (k) { return e[k]; });
    formErr.textContent = rest.join('. ');
  }
  function revalidate() { if (tried) showErrors(validate()); }
  function showForm() { if (!sent.hidden) { sent.hidden = true; form.hidden = false; } }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    formErr.textContent = '';
    var e = validate();
    if (Object.keys(e).length) {
      tried = true; showErrors(e);
      var first = form.querySelector('.field.err input, .field.err textarea') || consent; first.focus();
      return;
    }
    var payload = { tariff: tariff, consent: true, nav719_hp: input('nav719_hp').value };
    if (opts) { payload.kind = opts.kind; payload.trial = opts.trial;
                payload.period = opts.period; payload.seats = opts.seats; }
    ['name', 'org', 'inn', 'email', 'phone', 'promo', 'message'].forEach(function (n) { payload[n] = input(n).value.trim(); });
    submitBtn.disabled = true;
    fetch('/api/leads', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, j: j }; }); })
      .then(function (res) {
        if (res.status === 200 && res.j.ok) {
          var chosen = optsText(opts);
          document.getElementById('sent-d').textContent = 'Тариф «' + tariff + '»' + (chosen ? ' (' + chosen + ')' : '') +
            '. Специалист свяжется с вами по адресу ' + payload.email + ' в течение одного рабочего дня.';
          form.hidden = true; sent.hidden = false; tried = false;
        } else if (res.status === 422 && res.j.errors) {
          tried = true; showErrors(res.j.errors);
        } else {
          formErr.textContent = res.j.error || 'Не удалось отправить заявку. Попробуйте ещё раз через несколько минут.';
        }
      })
      .catch(function () { formErr.textContent = 'Не удалось отправить заявку: нет связи с сервером. Попробуйте ещё раз.'; })
      .then(function () { submitBtn.disabled = false; });
  });
  document.getElementById('lead-again').addEventListener('click', function () {
    form.reset(); setTariff(tariff, opts); showErrors({}); showForm(); input('name').focus();
  });

  // ---- отзывы: прокрутка ----
  var revs = document.getElementById('revs');
  function scrollRev(dir) {
    var card = revs.firstElementChild, w = card ? card.getBoundingClientRect().width + 20 : 400;
    revs.scrollBy({ left: dir * w, behavior: reduce ? 'auto' : 'smooth' });
  }
  document.getElementById('rev-prev').addEventListener('click', function () { scrollRev(-1); });
  document.getElementById('rev-next').addEventListener('click', function () { scrollRev(1); });

  // ---- вопросы: открыт один ----
  var qs = document.querySelectorAll('.qa-q');
  qs.forEach(function (b) {
    b.addEventListener('click', function () {
      var open = b.getAttribute('aria-expanded') === 'true';
      qs.forEach(function (o) { o.setAttribute('aria-expanded', 'false'); document.getElementById(o.getAttribute('aria-controls')).hidden = true; });
      if (!open) { b.setAttribute('aria-expanded', 'true'); document.getElementById(b.getAttribute('aria-controls')).hidden = false; }
    });
  });
})();
