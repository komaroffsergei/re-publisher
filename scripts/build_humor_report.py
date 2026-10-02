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
    args=p.parse_args();repo=Path(__file__).resolve().parents[1]
    if args.output.resolve().is_relative_to(repo):raise ValueError('Отчёт с постами должен остаться вне Git')
    args.output.mkdir(parents=True,exist_ok=True);assets=args.output/'assets';assets.mkdir(exist_ok=True)
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip()
    policy=json.loads((args.directory/'corpus-policy.json').read_text(encoding='utf-8'))
    paths=[f for f in sorted((args.directory/policy.get('ocr_directory','.')).glob('ocr-*.jsonl')) if re.fullmatch(r'ocr--?\d+-\d{3}\.jsonl',f.name)]
    annotations=sorted(args.directory.glob('codex-annotations-*.jsonl'))
    rows,excluded=assemble(paths,annotations,{(r['peer'],r['grouped_id']):r['members'] for r in policy['albums']},set(map(tuple,policy['reserved_sources'])))
    rows=partition(rows,list(read_rows([args.directory/'codex-repeat-groups.jsonl'])) if (args.directory/'codex-repeat-groups.jsonl').exists() else [])
    from tokenizers import Tokenizer
    if hashlib.sha256(Path(policy['tokenizer_path']).read_bytes()).hexdigest()!=policy['tokenizer_sha256']:
        raise ValueError('Tokenizer изменился')
    tokenizer=Tokenizer.from_file(policy['tokenizer_path']);tokenizer.no_truncation();tokenizer.no_padding()
    for row in rows:row['tokens']=len(tokenizer.encode(row['text']).ids)
    counts=coverage(rows);raw=list(read_rows(paths));decisions=list(read_rows(annotations))
    sections=['<header><p class="eyebrow">re-publisher · эксперимент OCR</p><h1>Подпись → надписи → оценки</h1><p>Разметка Codex, отдельный профиль юмора и проверка на VPS.</p></header>']
    sections.append(f'<section><h2>Состояние на {E(datetime.now(timezone.utc).isoformat(timespec="seconds"))}</h2><p>Код: <code>{E(revision)}</code>. Отчёт обновляется по сохранённым материалам. Наличие файлов OCR не означает, что они размечены или допущены к обучению.</p>')
    sections.append(f'<div class="metrics"><div><b>{len(raw)}</b><span>записей OCR</span></div><div><b>{len(decisions)}</b><span>сохранённых решений Codex, включая перепроверки</span></div><div><b>{len(rows)}</b><span>связанных входов и решений</span></div></div></section>')
    quotas={'train':(1100,1000),'validation':(150,150),'test':(150,150)}
    sections.append('<section><h2>Корпус</h2><p>Считаются уникальные группы с полным альбомом, пригодным OCR, достаточным контекстом и входом до 512 токенов. «Неясно» не становится отрицательной меткой.</p>')
    sections.append(table(['Выборка','Юмор / нужно','Не юмор / нужно','Контекст: да / нет'],[(E(part),f'{c["positive"]} / {quotas[part][0]}',f'{c["negative"]} / {quotas[part][1]}',f'{c["context_yes"]} / {c["context_no"]}') for part,c in counts.items()]))
    sections.append('<p>Исключения связывания: '+E(json.dumps(excluded,ensure_ascii=False))+'. Это не итоги качества модели.</p></section>')
    annotations_by_key={(a['peer'],a['message'],a['input_sha256']):a for a in decisions}
    examples=[]
    for item in raw:
        a=annotations_by_key.get((item['peer'],item['message'],item['input_sha256']))
        if not a or not item.get('file'):continue
        examples.append((item,a))
        if len(examples)==8:break
    sections.append('<section><h2>Примеры прочтения</h2><p>Ниже подпись, фактический OCR и решение агента. Это разметка, а не предсказание обученного классификатора. Модельные оценки появятся после обучения.</p><div class="examples">')
    for item,a in examples:
        from PIL import Image
        with Image.open(item['file']) as img:extension=(img.format or 'PNG').lower()
        extension='jpg' if extension=='jpeg' else extension
        name=item['media_sha256']+'.'+extension;shutil.copyfile(item['file'],assets/name)
        blocks=item['ocr'].get('blocks',[])
        sections.append(f'<article><img src="assets/{E(name)}" loading="lazy" alt="Исходное изображение или статическое превью"><h3>Пост {item["message"]}</h3><p class="tag">{E(item["ocr"]["status"])}</p><h4>Подпись</h4><pre>{E(item["caption"])}</pre><details><summary>OCR: {len(blocks)} блоков</summary><pre>{E(item["ocr"].get("text",""))}</pre></details><p><b>Шутка:</b> {E(a["is_joke"])} · <b>Контекст:</b> {E(a["input_has_context"])}</p><p>{E(a["reason"])}</p><small>Длительность OCR: {item["ocr"].get("elapsed_ms","—")} мс. Оценка чтения говорит о буквах, не о юморе.</small></article>')
    sections.append('</div></section>')
    sections.append('<section><h2>Модели и допуск</h2>')
    evaluations=list(args.directory.glob('models-*/evaluation.json'))
    if not evaluations:
        sections.append('<p>Обучение полного корпуса ещё не выполнено. Оценок качества, победителя и порога пока нет. Автоматический фильтр OCR не включён.</p>')
    else:
        value=json.loads(evaluations[-1].read_text(encoding='utf-8'))
        sections.append('<p>Измеряется согласие с разметкой Codex, не независимая человеческая точность.</p><pre>'+E(json.dumps(value,ensure_ascii=False,indent=2))+'</pre>')
    sections.append('</section><section><h2>Замеры на VPS</h2><p>100 различных вложений, один процесс, 768 MiB и 0,5 CPU. Таймауты и ошибки не исключаются из отчёта ради красивого p95.</p>')
    reports=sorted((args.directory/'vps-benchmark').glob('*-report.json'))
    if not reports:sections.append('<p>Полный пригодный замер пока не сохранён локально.</p>')
    for path in reports:
        value=json.loads(path.read_text(encoding='utf-8'))
        sections.append(f'<h3>{E(path.stem)}</h3><p>Вложений: {value["attachments"]}; ошибок: {value["failures"]}; p95: {value["p95_seconds"]:.2f} с; engine: <code>{E(value["engine_version"])}</code>.</p>')
        if value.get('conditions'):
            sections.append('<p>'+E(value['conditions'])+'</p>')
        samples=value['samples'];maximum=max(r['seconds'] for r in samples)
        bars=''.join(f'<rect x="{i*6}" y="{100-90*r["seconds"]/maximum:.2f}" width="4" height="{90*r["seconds"]/maximum:.2f}" fill="'+('#9333ea' if r['status']=='failed' else '#f97316')+'"/>' for i,r in enumerate(samples))
        sections.append(f'<svg viewBox="0 0 {len(samples)*6} 105" role="img" aria-label="Длительность OCR каждого вложения"><path d="M0 100H{len(samples)*6}" stroke="#333"/>{bars}</svg>')
    sections.append('</section><section><h2>Что где реализовано</h2>')
    sections.append(table(['Место','Код','Что происходит'],[(E(title),f'<a href="{source_link(repo,revision,file,name)}">{E(file)} · {E(name)}</a>',E(note)) for title,file,name,note in MAP]))
    sections.append('</section><section><h2>Границы</h2><p>Не распознаём речь, не смотрим всё видео и не определяем шутку только по объектам на картинке. Семь тематических фильтров используют свой прежний профиль taxonomy. OCR-текст не добавляется в публикации. Отправитель MAX не включается этим экспериментом.</p><p><a href="https://github.com/RapidAI/RapidOCR/blob/v3.9.2/python/rapidocr/default_models.yaml">RapidOCR: закреплённые модели</a> · <a href="https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.downloads.DownloadMethods.download_media">Telethon: превью</a> · <a href="https://www.ffmpeg.org/ffmpeg.html">FFmpeg</a></p></section>')
    css='''*{box-sizing:border-box}body{margin:0;background:#f6f3ed;color:#222;font:16px/1.55 system-ui}main{max-width:1240px;margin:auto;padding:24px}header{padding:48px;background:#ff8b32;border-radius:24px}h1{font-size:clamp(34px,6vw,68px);margin:0;line-height:1.05}h2{font-size:28px}section{margin:32px 0;background:white;border:1px solid #ddd;border-radius:20px;padding:28px}.eyebrow{letter-spacing:.12em;text-transform:uppercase}.metrics{display:flex;gap:24px;flex-wrap:wrap}.metrics div{background:#eee9fb;padding:16px;flex:1;min-width:180px}.metrics b{display:block;font-size:36px}.metrics span{display:block}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;min-width:620px}td,th{text-align:left;padding:12px;border-bottom:1px solid #ddd}a{color:#682abe;text-underline-offset:3px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f7f6fa;border-radius:8px;padding:12px;font:14px/1.5 ui-monospace,monospace}.examples{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}article{border:1px solid #ddd;padding:20px;border-radius:14px}article img{width:100%;height:300px;object-fit:contain;background:#f5f5f5}.tag{background:#ffe2c7;display:inline-block;padding:3px 9px;border-radius:6px}svg{width:100%;max-height:180px}summary{cursor:pointer}small{color:#555}@media(max-width:700px){main{padding:12px}section{padding:16px}.examples{grid-template-columns:1fr}header{padding:28px}}'''
    (args.output/'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>re-publisher: OCR юмора</title><style>'+css+'</style><main>'+''.join(sections)+'</main></html>',encoding='utf-8')
    print(json.dumps({'report':str(args.output/'index.html'),'linked_annotations':len(rows),'revision':revision}))


if __name__=='__main__':main()
