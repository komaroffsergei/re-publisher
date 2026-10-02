"""Только обучение на прочитанной Codex разметке. Посты остаются вне Git.

TF-IDF, MiniLM и контроль без OCR используют одни группы и выборки.
Пороги и победитель фиксируются до чтения test. Неясные метки маскируются.
"""
from __future__ import annotations
import argparse
import gc
import hashlib
import json
import random
import shutil
import time
from pathlib import Path
import joblib
import numpy as np
from scipy.sparse import hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import precision_recall_fscore_support
from app.taxonomy.artifact import file_sha256
from app.ocr.engine import CONTRACT
from scripts.humor_corpus import NAMES, coverage

CONFIG = {"profile":"humor_ocr", "version":"humor_ocr_v1", "categories":[],
    "binary_features":list(NAMES), "feature_names":{"is_joke":"Шутка", "input_has_context":"Хватает контекста"}}


def known(rows, name):
    indices = [i for i, row in enumerate(rows) if row["labels"][name] in {"да", "нет"}]
    return np.asarray(indices, dtype=int), np.asarray([rows[i]["labels"][name] == "да" for i in indices], dtype=int)


def dataset(directory):
    raw = (directory / "dataset.jsonl").read_bytes()
    manifest = json.loads((directory / "dataset-manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(raw).hexdigest() != manifest["dataset_sha256"] or manifest["contract"] != CONTRACT:
        raise ValueError("Dataset изменён или имеет другой контракт")
    rows = [json.loads(line) for line in raw.splitlines()]
    if any(r["provenance"] != "codex_agent" for r in rows):
        raise ValueError("Предсказания не заменяют разметку Codex")
    groups, selected = {}, []
    for row in sorted(rows, key=lambda r:r["sha"]):
        if not row["ocr_eligible"] or row["tokens"] > 512 or not any(row["labels"][n] in {"да","нет"} for n in NAMES):
            continue
        key = row["group_id"]
        if key in groups:
            previous = groups[key]
            if previous["split"] != row["split"] or previous["labels"] != row["labels"]:
                raise ValueError("Группа повторов требует согласованной ручной перепроверки")
            continue
        groups[key] = row; selected.append(row)
    counts = coverage(selected)
    for split, pos, neg in (("train",1100,1000),("validation",150,150),("test",150,150)):
        if counts[split]["positive"] < pos or counts[split]["negative"] < neg:
            raise ValueError("Квоты пригодного входа не выполнены: " + split)
    return selected, manifest


def metrics(scores, rows, thresholds=None):
    output = {}
    for col, name in enumerate(NAMES):
        ix, target = known(rows, name)
        if not len(ix):
            output[name] = {"known":0}; continue
        probability = scores[ix,col]
        threshold = .5 if thresholds is None else thresholds[name]
        precision, recall, f1, _ = precision_recall_fscore_support(target, probability >= threshold, average="binary", zero_division=0)
        output[name] = {"known":len(ix), "positive":int(target.sum()), "threshold":threshold,
            "precision":float(precision), "recall":float(recall), "f1":float(f1),
            "brier":float(np.mean((probability-target)**2))}
        bins=[]
        for low in np.arange(0,1,.1):
            selected=(probability>=low)&(probability<low+.1 if low<.9 else probability<=1)
            if selected.any():bins.append({"lower":float(low),"count":int(selected.sum()),
                "mean_score":float(probability[selected].mean()),"positive_share":float(target[selected].mean())})
        output[name]["calibration_bins"]=bins
    return output


def route(scores, rows):
    scores = np.round(scores,4)
    ix = [i for i,r in enumerate(rows) if all(r["labels"][n] in {"да","нет"} for n in NAMES)]
    positive = sum(all(rows[i]["labels"][n]=="да" for n in NAMES) for i in ix)
    best = None
    for joke in np.linspace(.3,.99,70):
        for context in np.linspace(.5,.99,50):
            matched = [i for i in ix if scores[i,0]>=joke and scores[i,1]>=context]
            correct = sum(all(rows[i]["labels"][n]=="да" for n in NAMES) for i in matched)
            if len(matched)<50 or correct/len(matched)<.92: continue
            item = {"is_joke_threshold":float(joke),"input_has_context_threshold":float(context),
                "matched":len(matched),"correct":correct,"precision":correct/len(matched),"recall":correct/max(1,positive)}
            if best is None or (item["recall"],item["precision"])>(best["recall"],best["precision"]): best=item
    return best


def tfidf(train, validation, output, caption_only=False):
    texts = lambda rows:[r["caption"] if caption_only else r["text"] for r in rows]
    word = TfidfVectorizer(ngram_range=(1,2),max_features=70000,min_df=2,sublinear_tf=True)
    char = TfidfVectorizer(analyzer="char_wb",ngram_range=(3,5),max_features=100000,min_df=2,sublinear_tf=True)
    x = hstack([word.fit_transform(texts(train)),char.fit_transform(texts(train))]).tocsr()
    v = hstack([word.transform(texts(validation)),char.transform(texts(validation))]).tocsr()
    models, scores = {}, np.zeros((len(validation),2))
    for col,name in enumerate(NAMES):
        ix,target = known(train,name)
        if len(set(target))!=2: raise ValueError("Не хватает обоих классов: " + name)
        model = LogisticRegression(C=2,class_weight="balanced",max_iter=2000,solver="liblinear")
        model.fit(x[ix],target);models[name]=model;scores[:,col]=model.predict_proba(v)[:,1]
    bundle = {"word":word,"char":char,"models":models,"label_names":list(NAMES),"taxonomy_version":CONFIG["version"]}
    joblib.dump(bundle,output)
    return scores


def minilm(train, validation, encoder_path, output):
    import torch
    from torch.utils.data import DataLoader,Dataset
    from transformers import AutoModel,AutoTokenizer
    from safetensors.torch import save_file
    from app.taxonomy.minilm import Classifier
    torch.set_num_threads(4);torch.manual_seed(20261003);random.seed(20261003);np.random.seed(20261003)
    tokenizer = AutoTokenizer.from_pretrained(encoder_path,local_files_only=True)
    encoder = AutoModel.from_pretrained(encoder_path,local_files_only=True)
    output.mkdir();encoder.config.save_pretrained(output);tokenizer.save_pretrained(output/"tokenizer")
    model = Classifier(encoder,2,with_complexity=False)
    class Posts(Dataset):
        def __init__(self,rows): self.rows=rows
        def __len__(self): return len(self.rows)
        def __getitem__(self,index):
            r=self.rows[index]
            encoded=tokenizer(r["text"],truncation=False)
            if len(encoded["input_ids"])>512:raise ValueError("Corpus превышает контракт входа")
            return encoded,[float(r["labels"][n]=="да") for n in NAMES],[float(r["labels"][n] in {"да","нет"}) for n in NAMES]
    def collate(items):
        encoded,target,mask=zip(*items)
        return tokenizer.pad(encoded,return_tensors="pt"),torch.tensor(target),torch.tensor(mask)
    training=DataLoader(Posts(train),batch_size=8,shuffle=True,collate_fn=collate)
    validating=DataLoader(Posts(validation),batch_size=8,collate_fn=collate)
    optimizer=torch.optim.AdamW(model.parameters(),lr=2e-5,weight_decay=.01)
    weights=[]
    for name in NAMES:
        _,target=known(train,name);weights.append(min(10.,max(1.,(len(target)-target.sum())/max(1,target.sum()))))
    weights=torch.tensor(weights,dtype=torch.float32)
    best,history,best_scores=-1,[],None
    for epoch in range(1,5):
        start=time.perf_counter();model.train()
        for step,(inputs,target,mask) in enumerate(training,1):
            optimizer.zero_grad();logits,_=model(inputs)
            loss=(torch.nn.functional.binary_cross_entropy_with_logits(logits,target,reduction="none",pos_weight=weights)*mask).sum()/mask.sum().clamp(min=1)
            loss.backward();torch.nn.utils.clip_grad_norm_(model.parameters(),1.);optimizer.step()
            if step==1 or step%50==0:print(json.dumps({"epoch":epoch,"step":step,"steps":len(training),"seconds":round(time.perf_counter()-start,1)}),flush=True)
        model.eval();scores=[]
        with torch.inference_mode():
            for inputs,_,_ in validating:
                logits,_=model(inputs);scores.extend(torch.sigmoid(logits).tolist())
        scores=np.asarray(scores);report=metrics(scores,validation)
        macro_f1=float(np.mean([report[n]["f1"] for n in NAMES]))
        history.append({"epoch":epoch,"validation_macro_f1":macro_f1,"seconds":time.perf_counter()-start})
        if macro_f1>best:
            best,best_scores,best_epoch=macro_f1,scores,epoch
            save_file(model.state_dict(),str(output/"best.safetensors"))
        print(json.dumps(history[-1]),flush=True)
    metadata={"taxonomy_version":CONFIG["version"],"names":list(NAMES),"max_length":512,
        "best_checkpoint":"best.safetensors","best_epoch":best_epoch,"epochs":4,"batch_size":8,
        "device":"cpu","validation_selection":"macro F1 at fixed 0.5","history":history,
        "positive_weights_from_train":dict(zip(NAMES,weights.tolist()))}
    (output/"training.json").write_text(json.dumps(metadata,indent=2),encoding="utf-8")
    return best_scores


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("dataset",type=Path);p.add_argument("output",type=Path);p.add_argument("--encoder",type=Path,required=True)
    args=p.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):raise ValueError("Артефакты должны оставаться вне Git")
    rows,manifest=dataset(args.dataset)
    from tokenizers import Tokenizer
    tokenizer_path=args.encoder/"tokenizer.json"
    if file_sha256(tokenizer_path)!=manifest["tokenizer_sha256"]:raise ValueError("Tokenizer отличается от зафиксированного корпуса")
    guard=Tokenizer.from_file(str(tokenizer_path));guard.no_truncation();guard.no_padding()
    if any(len(guard.encode(r["text"]).ids)!=r["tokens"] for r in rows):raise ValueError("Tokenizer корпуса и модели различаются")
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/"taxonomy.json").write_text(json.dumps(CONFIG,ensure_ascii=False,indent=2),encoding="utf-8")
    shutil.copyfile(tokenizer_path,args.output/"input-tokenizer.json")
    (args.output/"dataset-manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    train=[r for r in rows if r["split"]=="train"];validation=[r for r in rows if r["split"]=="validation"]
    scores={"tfidf":tfidf(train,validation,args.output/"baseline.joblib"),
        "caption_only":tfidf(train,validation,args.output/"caption-only.joblib",True),
        "minilm":minilm(train,validation,args.encoder,args.output/"minilm-humor")}
    models={}
    for key,path in (("tfidf",args.output/"baseline.joblib"),("minilm",args.output/"minilm-humor/best.safetensors")):
        files=[args.output/"taxonomy.json",args.output/"input-tokenizer.json"]
        if key=="minilm":files.extend(f for f in path.parent.rglob('*') if f.is_file() and f!=path)
        auxiliary={f.relative_to(args.output).as_posix():file_sha256(f) for f in sorted(files)}
        checksum=file_sha256(path);identity=hashlib.sha256(json.dumps({"weights":checksum,"auxiliary":auxiliary},sort_keys=True).encode()).hexdigest()[:12]
        models[key]={"version":f"humor-ocr-v1-{key}-{identity}","weights_sha256":checksum,"auxiliary_sha256":auxiliary,
            "path":"baseline.joblib" if key=="tfidf" else "minilm-humor"}
    (args.output/"model-manifest.json").write_text(json.dumps({"models":models,"dataset_sha256":manifest["dataset_sha256"],"profile":"humor_ocr","contract":CONTRACT},indent=2))
    candidates=[(key,route(values,validation)) for key,values in scores.items() if key!='caption_only']
    candidates=[(key,r) for key,r in candidates if r]
    choice=max(candidates,key=lambda item:(item[1]["recall"],item[1]["precision"],item[0]=='tfidf')) if candidates else None
    selected={"model_key":choice[0],**choice[1]} if choice else None
    (args.output/"validation-choice.json").write_text(json.dumps(selected,indent=2))
    # Test впервые оценивается ниже: менять победителя/пороги по нему запрещено.
    test=[r for r in rows if r["split"]=="test"]
    from app.taxonomy.inference import TaxonomyModel
    from app.taxonomy.minilm import MiniLmTaxonomyModel
    report={"agreement_with":"Codex annotation, not independent human accuracy","coverage":coverage(rows),"models":{},"route":selected,"automatic_filter_allowed":False}
    for key in ('tfidf','caption_only','minilm'):
        model=MiniLmTaxonomyModel(args.output) if key=='minilm' else TaxonomyModel(args.output)
        if key=='caption_only':model.bundle=joblib.load(args.output/"caption-only.joblib")
        predictions,times=[],[]
        for row in test:
            start=time.perf_counter();result=model.classify(row["caption"] if key=='caption_only' else row["text"])
            times.append(time.perf_counter()-start);predictions.append([result['scores'][n] for n in NAMES])
        prediction=np.asarray(predictions)
        report["models"][key]={"validation":metrics(scores[key],validation),"test":metrics(prediction,test),
            "local_cpu_p95_seconds":float(np.percentile(times,95)),"version":model.model_version if key!='caption_only' else "control@"+file_sha256(args.output/"caption-only.joblib")[:16]}
        errors=[]
        for i,row in enumerate(test):
            for col,name in enumerate(NAMES):
                if row["labels"][name] in {"да","нет"} and bool(prediction[i,col]>=.5)!=(row["labels"][name]=='да'):
                    errors.append({"sha":row["sha"],"feature":name,"label":row["labels"][name],"score":float(prediction[i,col])})
        (args.output/f"{key}-test-errors.json").write_text(json.dumps(errors,ensure_ascii=False,indent=2),encoding="utf-8")
        if selected and selected["model_key"]==key:
            ix=[i for i,r in enumerate(test) if all(r["labels"][n] in {"да","нет"} for n in NAMES)]
            matched=[i for i in ix if prediction[i,0]>=selected["is_joke_threshold"] and prediction[i,1]>=selected["input_has_context_threshold"]]
            correct=sum(all(test[i]["labels"][n]=='да' for n in NAMES) for i in matched)
            report["route"]={**selected,"test_matched":len(matched),"test_correct":correct,"test_precision":correct/max(1,len(matched)),"model_version":model.model_version}
            report["automatic_filter_allowed"]=len(matched)>=50 and correct/max(1,len(matched))>=.90
        del model;gc.collect()
    (args.output/"evaluation.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"models_saved":True,"quality_gate":report["automatic_filter_allowed"]}),flush=True)


if __name__=="__main__":main()
