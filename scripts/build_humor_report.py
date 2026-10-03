"""Локальный HTML-отчёт. Приватные посты и изображения не публикуются в Git."""
import argparse
import ast
import html
import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from scripts.humor_corpus import assemble,partition,coverage,read_rows

MAP=[
 ('Статическое превью','app/ocr/media.py','static_thumbnail','Отбирается PhotoSize, исключается VideoSize.'),
 ('Первый кадр','app/ocr/media.py','first_frame','FFmpeg читает первый кадр локального видео.'),
 ('Полный альбом','app/ocr/jobs.py','snapshot','Пути, доступность частей, SHA файлов и подпись.'),
 ('Отдельная очередь','app/ocr/jobs.py','enqueue_ocr','Текущее задание и новая запись истории.'),
 ('OCR','app/ocr/engine.py','read','Буквы, координаты, оценка чтения и время.'),
 ('Вход модели','app/ocr/engine.py','compose_input','Подпись + блоки OCR; оригинал остаётся отдельно.'),
 ('Кеш и таймаут','app/ocr/worker.py','Reader','Отдельный процесс, кеш по файлу и версии.'),
 ('Актуальность результата','app/ocr/jobs.py','classification_input_current','Новый файл не может пройти по старой оценке.'),
 ('512 токенов','app/taxonomy/input_contract.py','InputGuard','Превышение даёт ручной разбор, без truncation.'),
 ('Выбранная модель','app/taxonomy/worker.py','run','Один загруженный профиль; версия из артефакта.'),
 ('Условия фильтра','app/content/selection_filters.py','current_assessment','Две реальные оценки и проверка входа.'),
 ('Маркировка','app/content/post_preparation.py','mark_source','Ссылка на источник и исходный текст без OCR.'),
 ('Две головы','scripts/train_humor_models.py','minilm','Четыре эпохи, batch 8, checkpoint по validation.'),
 ('Порог','scripts/train_humor_models.py','route','Не менее 50 совпадений и 92% на validation.'),
 ('Корпус','scripts/humor_corpus.py','freeze','Квоты, повторы, holdout и контрольные суммы.'),
]
E=html.escape


def source_link(repo,revision,file,name):
    tree=ast.parse((repo/file).read_text(encoding='utf-8'))
    node=next((n for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and n.name==name),None)
    return f'https://github.com/komaroffsergei/re-publisher/blob/{revision}/{file}'+(f'#L{node.lineno}' if node else '')


def table(headers,rows):
    return '<div class="scroll"><table><thead><tr>'+''.join(f'<th>{E(h)}</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join(f'<td>{cell}</td>' for cell in row)+'</tr>' for row in rows)+'</tbody></table></div>'


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);p.add_argument('output',type=Path)
    p.add_argument('--dataset',type=Path,help='Зафиксированный набор, на котором реально обучались модели')
    p.add_argument('--models',type=Path,help='Законченный артефакт обучения и evaluation.json')
    args=p.parse_args();repo=Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(repo):raise ValueError('Отчёт с постами должен остаться вне Git')
    args.output.mkdir(parents=True,exist_ok=True);assets=args.output/'assets';assets.mkdir(exist_ok=True)
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    policy=json.loads((args.directory/'corpus-policy.json').read_text(encoding='utf-8'))
    paths=[f for f in sorted((args.directory/policy.get('ocr_directory','.')).glob('ocr-*.jsonl')) if re.fullmatch(r'ocr--?\d+-\d{3}\.jsonl',f.name)]
    annotations=sorted(args.directory.glob('codex-annotations-*.jsonl'))
    rows,excluded=assemble(paths,annotations,{(r['peer'],r['grouped_id']):r['members'] for r in policy['albums']},set(map(tuple,policy['reserved_sources'])),set(policy.get('reserved_caption_sha256',[])))
    rows=partition(rows,list(read_rows([args.directory/'codex-repeat-groups.jsonl'])) if (args.directory/'codex-repeat-groups.jsonl').exists() else [])
    from tokenizers import Tokenizer
    if hashlib.sha256(Path(policy['tokenizer_path']).read_bytes()).hexdigest()!=policy['tokenizer_sha256']:
        raise ValueError('Tokenizer изменился')
    tokenizer=Tokenizer.from_file(policy['tokenizer_path']);tokenizer.no_truncation();tokenizer.no_padding()
    for row in rows:row['tokens']=len(tokenizer.encode(row['text']).ids)
    if args.dataset:
        content=(args.dataset/'dataset.jsonl').read_bytes()
        manifest=json.loads((args.dataset/'dataset-manifest.json').read_text(encoding='utf-8'))
        if hashlib.sha256(content).hexdigest()!=manifest['dataset_sha256']:
            raise ValueError('Зафиксированный корпус изменён')
        rows=[json.loads(line) for line in content.splitlines()]
        shutil.copyfile(args.dataset/'dataset-manifest.json',assets/'dataset-manifest.json')
    counts=coverage(rows);raw=list(read_rows(paths));decisions=list(read_rows(annotations))
    sections=['<header><p class="eyebrow">re-publisher · эксперимент OCR</p><h1>Подпись → надписи → оценки</h1><p>Разметка Codex, отдельный профиль юмора и проверка на VPS.</p></header>']
    sections.append(f'<section><h2>Состояние на {E(datetime.now(timezone.utc).isoformat(timespec="seconds"))}</h2><p>Код: <code>{E(revision)}</code>. Отчёт обновляется по сохранённым материалам. Наличие файлов OCR не означает, что они размечены или допущены к обучению.</p>')
    acceptance=args.directory/'release-acceptance.json'
    if acceptance.is_file():
        release=json.loads(acceptance.read_text(encoding='utf-8'))
        sections.append('<p class="verdict">'+E(release['conclusion'])+'</p>')
    sections.append(f'<div class="metrics"><div><b>{len(raw)}</b><span>записей OCR</span></div><div><b>{len(decisions)}</b><span>сохранённых решений Codex, включая перепроверки</span></div><div><b>{len(rows)}</b><span>связанных входов и решений</span></div></div></section>')
    quotas={'train':(1100,1000),'validation':(150,150),'test':(150,150)}
    sections.append('<section><h2>Корпус</h2><p>Считаются уникальные группы с полным альбомом, пригодным OCR, достаточным контекстом и входом до 512 токенов. «Неясно» не становится отрицательной меткой.</p>')
    sections.append(table(['Выборка','Юмор / нужно','Не юмор / нужно','Контекст: да / нет'],[(E(part),f'{c["positive"]} / {quotas[part][0]}',f'{c["negative"]} / {quotas[part][1]}',f'{c["context_yes"]} / {c["context_no"]}') for part,c in counts.items()]))
    sections.append('<p>Исключения связывания: '+E(json.dumps(excluded,ensure_ascii=False))+'. Это не итоги качества модели.</p></section>')
    if args.dataset:
        sections.append('<section><h2>Зафиксированная версия</h2><p>SHA корпуса: <code>'+E(manifest['dataset_sha256'])+'</code>.</p><p><a href="assets/dataset-manifest.json">Manifest корпуса</a>. Сырые тексты и разметка хранятся отдельно; файл описывает версию и покрытие.</p></section>')
    annotations_by_key={(a['peer'],a['message'],a['input_sha256']):a for a in decisions}
    frozen_inputs={r['input_sha256'] for r in rows if r['ocr_eligible'] and r['tokens']<=512}
    predictors={}
    if args.models and (args.models/'evaluation.json').is_file():
        from app.taxonomy.inference import TaxonomyModel
        from app.taxonomy.minilm import MiniLmTaxonomyModel
        predictors={'TF-IDF':TaxonomyModel(args.models),'MiniLM':MiniLmTaxonomyModel(args.models)}
    candidates=[]
    for item in raw:
        a=annotations_by_key.get((item['peer'],item['message'],item['input_sha256']))
        if not a or not item.get('file') or item['input_sha256'] not in frozen_inputs:continue
        from PIL import Image
        try:
            with Image.open(item['file']) as img:img.verify()
        except (OSError,ValueError):continue
        candidates.append((item,a))
    # Иллюстрации обеих меток, не первые восемь новостей из одного источника.
    examples=[];seen_media=set()
    for label,limit in (("да",4),("нет",4)):
        for item,a in candidates:
            if a["is_joke"]!=label or item["media_sha256"] in seen_media:continue
            examples.append((item,a));seen_media.add(item["media_sha256"])
            if sum(annotation["is_joke"]==label for _,annotation in examples)>=limit:break
    sections.append('<section><h2>Примеры прочтения</h2><p>Подпись, фактический OCR, решение Codex и отдельно оценки загруженных моделей. Примеры иллюстрируют вход; таблица качества ниже считается на отложенном test.</p><div class="examples">')
    for item,a in examples:
        from PIL import Image
        with Image.open(item['file']) as img:extension=(img.format or 'PNG').lower()
        extension='jpg' if extension=='jpeg' else extension
        name=item['media_sha256']+'.'+extension;shutil.copyfile(item['file'],assets/name)
        blocks=item['ocr'].get('blocks',[])
        sections.append(f'<article><img src="assets/{E(name)}" loading="lazy" alt="Исходное изображение или статическое превью"><h3>Пост {item["message"]}</h3><p class="tag">{E(item["ocr"]["status"])}</p><h4>Подпись</h4><pre>{E(item["caption"])}</pre><details><summary>OCR: {len(blocks)} блоков</summary><pre>{E(item["ocr"].get("text",""))}</pre></details><p><b>Шутка:</b> {E(a["is_joke"])} · <b>Контекст:</b> {E(a["input_has_context"])}</p><p>{E(a["reason"])}</p><small>Длительность OCR: {item["ocr"].get("elapsed_ms","—")} мс. Оценка чтения говорит о буквах, не о юморе.</small></article>')
        if predictors:
            results=[]
            for title,model in predictors.items():
                value=model.classify(item['input'])
                results.append((E(title),f'{value["scores"]["is_joke"]*100:.1f}%',f'{value["scores"]["input_has_context"]*100:.1f}%',E(model.model_version)))
            sections[-1]=sections[-1].replace('</article>',table(['Модель','Шутка','Хватает контекста','Версия'],results)+'</article>')
    sections.append('</div></section>')
    sections.append('<section><h2>Модели и допуск</h2>')
    evaluations=[args.models/'evaluation.json'] if args.models and (args.models/'evaluation.json').is_file() else []
    if not evaluations:
        sections.append('<p>Обучение полного корпуса ещё не выполнено. Оценок качества, победителя и порога пока нет. Автоматический фильтр OCR не включён.</p>')
    else:
        value=json.loads(evaluations[-1].read_text(encoding='utf-8'))
        sections.append('<p>Измеряется согласие с разметкой Codex, не независимая человеческая точность. Порог и победитель выбраны на validation до чтения test. «Неясно» маскируется при обучении, но не исчезает из знаменателя проверки автоматического маршрута.</p>')
        selected=value.get('route')
        if selected:
            sections.append('<h3>Допуск совместного условия</h3><p>Выбрано на validation: '+E(selected['model_key'])+'; Шутка ≥ '+f'{selected["is_joke_threshold"]*100:.0f}%'+', Хватает контекста ≥ '+f'{selected["input_has_context_threshold"]*100:.0f}%'+'. Проверяются оба условия через И.</p>')
            sections.append(table(['Выборка','Совпадений','Подтверждено да/да','Известные ошибки','Неясно','Подтверждённая доля'],[
                ('validation',str(selected['matched']),str(selected['correct']),str(selected['confirmed_wrong']),str(selected['unresolved']),f'{selected["precision"]*100:.1f}%'),
                ('test',str(selected['test_matched']),str(selected['test_correct']),str(selected['test_confirmed_wrong']),str(selected['test_unresolved']),f'{selected["test_precision"]*100:.1f}%')]))
            sections.append('<p class="verdict">'+('Качество прошло проверку; это только один из допусков. Дополнительно нужны ресурсы и runtime.' if value['automatic_filter_allowed'] else 'Допуск качества не пройден. Победителя и пороги после test не меняли. Автоматический отбор выключен.')+'</p>')
        else:
            sections.append('<p class="verdict">На validation не найден маршрут с требуемыми 92% и минимум 50 совпадениями. Автоматический отбор выключен.</p>')
        results=[]
        for key,item in value['models'].items():
            for feature,label in (('is_joke','Шутка'),('input_has_context','Контекст')):
                m=item['test'][feature]
                results.append((E(key),E(label),f'{m["precision"]:.3f}',f'{m["recall"]:.3f}',f'{m["f1"]:.3f}',f'{item["local_cpu_p95_seconds"]:.3f} с'))
        sections.append(table(['Модель','Выход','Precision test','Recall test','F1 test','Локальный p95'],results))
        diagnostic=[]
        for key,item in value['models'].items():
            diagnostic.append((E(key), 'Подпись без OCR' if key=='caption_only' else 'Подпись + OCR',f'{item["test"]["is_joke"]["f1"]:.3f}',f'{item["test"]["input_has_context"]["f1"]:.3f}'))
            ablation=item.get('source_marks_ablation')
            if ablation:
                diagnostic.append((E(key),'Без отдельных строк названия канала',f'{ablation["test"]["is_joke"]["f1"]:.3f}',f'{ablation["test"]["input_has_context"]["f1"]:.3f}'))
        sections.append('<h3>Что добавил OCR и что делает название источника</h3>')
        sections.append(table(['Модель','Вход','F1: шутка','F1: контекст'],diagnostic))
        sections.append('<p>Контроль на одной подписи обучен на том же корпусе и разделении. В диагностике отдельно убраны строки водяных знаков и названия канала. Если качество падает, модель использует источник как подсказку. Это ограничивает выводы о новых каналах. Диагностика не использовалась для перенастройки по test.</p>')
        shutil.copyfile(evaluations[-1],assets/'evaluation.json')
        sections.append('<p><a href="assets/evaluation.json">Скачать расчёты</a>. Доля совпадений относится к этому корпусу и разметке Codex. Это не обещание качества на любом новом канале.</p>')
        sections.append('<details><summary>Полный отчёт: пороги, калибровка, срезы и влияние подписей каналов</summary><pre>'+E(json.dumps(value,ensure_ascii=False,indent=2))+'</pre></details>')
        by_sha={r['sha']:r for r in rows}
        sections.append('<h3>Конкретные ошибки на test</h3><p>Здесь сравнение по порогу 0,5. У маршрута собственные пороги, выбранные раньше на validation. Разбор ошибок не меняет тестовые метки и не используется для перенастройки.</p>')
        for key in ('tfidf','minilm'):
            errors=json.loads((args.models/f'{key}-test-errors.json').read_text(encoding='utf-8'))
            sections.append('<h4>'+E(key)+': '+str(len(errors))+' расхождений по двум выходам</h4>')
            for error in errors[:6]:
                row=by_sha[error['sha']]
                sections.append('<details><summary>'+E(error['feature'])+' · Codex: '+E(error['label'])+' · оценка модели: '+f'{error["score"]*100:.1f}%'+ '</summary><pre>'+E(row['text'])+'</pre><p>'+E(row['reason'])+'</p></details>')
    sections.append('</section><section><h2>Замеры на VPS</h2><p>100 различных вложений, один процесс, 768 MiB и 0,5 CPU. Таймауты и ошибки не исключаются из отчёта ради красивого p95.</p>')
    reports=sorted((args.directory/'vps-benchmark').glob('*-report.json'))
    if not reports:sections.append('<p>Полный пригодный замер пока не сохранён локально.</p>')
    for path in reports:
        value=json.loads(path.read_text(encoding='utf-8'))
        if path.name=='isolated-dcbc774-report.json':
            sections.append('<p class="verdict">Основной замер в заданных ограничениях. Лимит OCR — 10 секунд на p95. Он не пройден; автоматический фильтр выключен. Остальные замеры ниже сохранены как история эксперимента.</p>')
        sections.append(f'<h3>{E(path.stem)}</h3><p>Вложений: {value["attachments"]}; ошибок: {value["failures"]}; p95: {value["p95_seconds"]:.2f} с; engine: <code>{E(value["engine_version"])}</code>.</p>')
        if value.get('conditions'):
            sections.append('<p>'+E(value['conditions'])+'</p>')
        samples=value['samples'];maximum=max(r['seconds'] for r in samples)
        bars=''.join(f'<rect x="{i*6}" y="{100-90*r["seconds"]/maximum:.2f}" width="4" height="{90*r["seconds"]/maximum:.2f}" fill="'+('#9333ea' if r['status']=='failed' else '#f97316')+'"/>' for i,r in enumerate(samples))
        sections.append(f'<svg viewBox="0 0 {len(samples)*6} 105" role="img" aria-label="Длительность OCR каждого вложения"><path d="M0 100H{len(samples)*6}" stroke="#333"/>{bars}</svg>')
    inference=args.directory/'vps-inference-report.json'
    if inference.is_file():
        measured=json.loads(inference.read_text(encoding='utf-8'))
        sections.append('<h3>Прогретая классификация на VPS</h3><p>'+E(measured['conditions'])+'. Дата: '+E(measured['date_utc'])+'.</p>')
        sections.append(table(['Модель','Входов','Ошибок','p95','Медиана','Холодная загрузка','Версия'],[
            (E(key),str(measured['inputs']),str(item['failures']),f'{item["p95_seconds"]:.3f} с',f'{item["median_seconds"]:.3f} с',f'{item["cold_load_seconds"]:.2f} с',E(item['version']))
            for key,item in measured['models'].items()]))
        shutil.copyfile(inference,assets/'vps-inference-report.json')
        sections.append('<p><a href="assets/vps-inference-report.json">Полный замер без текстов постов</a>. Время очереди, OCR и холодная загрузка не выдаются за прогретый inference.</p>')
    sections.append('''</section><section><h2>Как пользоваться</h2>
    <p>Открыть карточку на <a href="https://publisher.komaroff-dev.ru/pipeline">доске Publisher</a>. В выборе профиля поставить «Юмор + OCR», затем нажать стрелку TF‑IDF или MiniLM. Это запуск для одной карточки, без обработки всего старого буфера.</p>
    <ol><li>Для короткой подписи сначала работает OCR всех известных картинок или превью альбома. На видео берётся статическое превью, а если его нет — первый кадр.</li>
    <li>В раскрываемом блоке OCR видно прочитанный текст, оценки чтения строк, версию и длительность. Оценка ниже 50% хотя бы у одной строки блокирует смысловую модель. Даже один плохо прочитанный символ не выкидывается молча.</li>
    <li>После OCR загружается выбранная модель и считает «Шутка» и «Хватает контекста». Вторая стрелка добавляет отдельный результат; первый сохраняется. В сравнении можно посмотреть версии и отдельное время OCR и классификации.</li>
    <li>Ошибка или ручной разбор видны на карточке. Кнопка повтора запускает новый OCR и сохраняет предыдущий в истории. Она не исправляет буквы самостоятельно.</li>
    <li>Фильтр профиля юмора проверяет обе оценки через И. «Смешное» — пользовательский лейбл из словаря, а не название оценки модели. При выключенном автоматическом допуске разрешён только ручной запуск и предпросмотр.</li></ol>
    <p>Подпись длиннее 500 символов классифицируется без OCR. Пустая подпись с читаемой надписью на картинке — обычный вход модели. Без текста после OCR получается «Только медиа»; без вложений — «Пустой».</p>
    </section><section><h2>Путь одного поста</h2>
    <div class="flow" role="img" aria-label="Сообщение, проверка вложений, OCR, проверка полного входа, модель, фильтр">
    <b>Оригинал</b><span>→</span><b>Вложения</b><span>→</span><b>OCR / кеш</b><span>→</span><b>Полный вход</b><span>→</span><b>TF‑IDF / MiniLM</b><span>→</span><b>Фильтр</b></div>
    <p>Подпись и OCR соединяются разделителями <code>[Подпись]</code> и <code>[Медиа N]</code>. Это отдельная строка для модели. Текст поста в БД сохраняется как был. На маркировку и публикацию идут оригинальный текст, ссылка на источник и исходные вложения.</p>
    <p>Неполный альбом, недоступное медиа, слабое чтение или больше 512 токенов ведут в ручной разбор. Не отдаём модели только ту часть, которую получилось прочитать. Замена подписи, файла или OCR отзывает актуальность результата.</p>
    </section><section><h2>Хранение и настройки</h2>''')
    sections.append(table(['Что','Где','Зачем'],[
        ('Корпус и решения Codex','<code>publisher-taxonomy-private/humor-ocr-v1</code> на компьютере','Приватные исходники, решения, группы повторов и замороженные выборки; вне Git.'),
        ('Задания и история OCR','PostgreSQL: <code>ocr_jobs</code>, <code>ocr_runs</code>','Текущее состояние отдельно от запусков; строки, координаты, оценки, SHA и длительность.'),
        ('Классификация','<code>taxonomy_classifications</code>, <code>taxonomy_runs</code>','Модель и профиль входят в ключ задания. История TF‑IDF и MiniLM сохраняется отдельно.'),
        ('Модели на VPS','<code>/srv/portfolio/publisher/models</code>','Только артефакты, tokenizer и manifests с контрольными суммами; обучение здесь не запускается.'),
        ('Превью и кеш','<code>cache/ocr-previews</code>, <code>cache/ocr-results</code>','Превью отделены от исходных вложений, кеш зависит от файла и версии движка.'),
        ('Настройки профиля','<code>OCR_ENABLED</code>, <code>OCR_MODEL_DIR</code>, <code>HUMOR_MODEL_DIR</code>, <code>HUMOR_AUTO_ENABLED</code>','Файл настроек вне Git. OCR-worker получает только подключение БД и OCR-настройки, без Telegram/MAX.'),
        ('Откат','<code>/srv/portfolio/publisher/releases</code>','Предыдущие закреплённые образы и конфигурация. История и данные не удаляются, схема не понижается.'),
    ]))
    sections.append('<p>MiniLM-worker держит один encoder: при смене профиля выгружает прежний и загружает нужный. OCR работает одной задачей и одним потоком ONNX. Эти ограничения нужны для небольшой VPS, а не для максимальной скорости.</p></section><section><h2>Что где реализовано</h2>')
    if acceptance.is_file():
        sections.append('<h3>Фактическая проверка выпуска</h3><pre>'+E(acceptance.read_text(encoding='utf-8'))+'</pre>')
    gallery=args.directory/'browser-gallery.json'
    if gallery.is_file():
        sections.append('<h3>Реальный интерфейс после выпуска</h3><div class="examples">')
        for index,item in enumerate(json.loads(gallery.read_text(encoding='utf-8'))):
            image=(args.directory/'browser-evidence'/item['file']).resolve()
            if not image.is_relative_to((args.directory/'browser-evidence').resolve()):
                raise ValueError('Изображение вне каталога проверки')
            from PIL import Image
            with Image.open(image) as opened:opened.verify()
            name=f'browser-{index}-{image.name}';shutil.copyfile(image,assets/name)
            sections.append('<article><a href="assets/'+E(name)+'"><img src="assets/'+E(name)+'" loading="lazy" alt="'+E(item['caption'])+'"></a><p>'+E(item['caption'])+'</p><small>'+E(item['date_utc'])+'</small></article>')
        sections.append('</div>')
    sections.append(table(['Место','Код','Что происходит'],[(E(title),f'<a href="{source_link(repo,revision,file,name)}">{E(file)} · {E(name)}</a>',E(note)) for title,file,name,note in MAP]))
    sections.append('</section><section><h2>Границы</h2><p>Не распознаём речь, не смотрим всё видео и не определяем шутку только по объектам на картинке. Семь тематических фильтров используют свой прежний профиль taxonomy. OCR-текст не добавляется в публикации. Отправитель MAX не включается этим экспериментом.</p><p><a href="https://github.com/RapidAI/RapidOCR/blob/v3.9.2/python/rapidocr/default_models.yaml">RapidOCR: закреплённые модели</a> · <a href="https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.downloads.DownloadMethods.download_media">Telethon: превью</a> · <a href="https://www.ffmpeg.org/ffmpeg.html">FFmpeg</a></p></section>')
    css='''*{box-sizing:border-box}body{margin:0;background:#f6f3ed;color:#222;font:16px/1.55 system-ui}main{max-width:1240px;margin:auto;padding:24px}header{padding:48px;background:#ff8b32;border-radius:24px}h1{font-size:clamp(34px,6vw,68px);margin:0;line-height:1.05}h2{font-size:28px}section{margin:32px 0;background:white;border:1px solid #ddd;border-radius:20px;padding:28px}.eyebrow{letter-spacing:.12em;text-transform:uppercase}.metrics{display:flex;gap:24px;flex-wrap:wrap}.metrics div{background:#eee9fb;padding:16px;flex:1;min-width:180px}.metrics b{display:block;font-size:36px}.metrics span{display:block}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;min-width:620px}td,th{text-align:left;padding:12px;border-bottom:1px solid #ddd}a{color:#682abe;text-underline-offset:3px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f6fa;border-radius:8px;padding:12px;font:14px/1.5 ui-monospace,monospace}.examples{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}article{border:1px solid #ddd;padding:20px;border-radius:14px}article img{width:100%;height:300px;object-fit:contain;background:#f5f5f5}.tag{background:#ffe2c7;display:inline-block;padding:3px 9px;border-radius:6px}svg{width:100%;max-height:180px}summary{cursor:pointer}small{color:#555}@media(max-width:700px){main{padding:12px}section{padding:16px}.examples{grid-template-columns:1fr}header{padding:28px}}'''
    css+=' .flow{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:20px;background:#f6f1fd;border-radius:14px}.flow b{padding:10px 14px;background:#ffe2c7;border-radius:8px}code{overflow-wrap:anywhere}.examples article{min-width:0}.examples details{margin:12px 0}.verdict{padding:16px;border-left:5px solid #f97316;background:#fff0e3;font-weight:600}'
    (args.output/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>re-publisher: OCR юмора</title><style>'+css+'</style><main>'+''.join(sections)+'</main></html>',encoding='utf-8')
    print(json.dumps({'report':str(args.output/'index.html'),'linked_annotations':len(rows),'revision':revision}))


if __name__=='__main__':main()
