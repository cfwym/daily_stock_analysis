#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每周文献检索：PubMed E-utilities + aihubmix 中文导读 + 163 邮件
仅依赖 Python 标准库。由 GitHub Actions 每周五北京时间 18:00 触发。
"""
import os, json, re, smtplib, sys, time
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

# ---- 配置（GitHub Actions 通过环境变量注入）----
AIHUBMIX_KEY = os.environ.get("AIHUBMIX_API_KEY", "")
AIHUBMIX_URL = os.environ.get("AIHUBMIX_BASE_URL", "https://api.aihubmix.com/v1/chat/completions")
AIHUBMIX_MODEL = os.environ.get("AIHUBMIX_MODEL", "glm-4.7-flash-free")
EMAIL_SENDER = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "")
EMAIL_RECEIVERS = os.environ.get("EMAIL_RECEIVERS", EMAIL_SENDER)

# ---- 检索主题：(名称, PubMed 检索式, 回看天数, 最大篇数) ----
TOPICS = [
    ("安非他酮/快感缺失/抑郁",
     'bupropion AND (anhedonia OR depression)', 7, 10),
    ("光照治疗/青少年抑郁/昼夜节律",
     '("light therapy" OR "bright light therapy") AND (adolescent* OR youth) AND (depressi* OR circadian)', 7, 10),
    ("精神药物/心脏安全/心电图",
     '(antipsychotic* OR psychotropic) AND (QT OR "cardiac safety" OR arrhythmia OR ECG)', 7, 10),
    ("精神分裂症/多模态预测",
     'schizophrenia AND (fMRI OR EEG OR ECG) AND (predict* OR "treatment response")', 7, 10),
]


def http_json(url, data=None, headers=None, timeout=90):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def pubmed_search(term, reldate, retmax):
    """esearch 拿 PMID 列表"""
    q = urllib.parse.urlencode({
        "db": "pubmed", "term": term, "reldate": reldate,
        "datetype": "pdat", "retmax": retmax, "retmode": "json",
        "sort": "pub date",
    })
    try:
        j = http_json(EUTILS + "esearch.fcgi?" + q)
        return j["esearchresult"].get("idlist", [])
    except Exception as e:
        print(f"[warn] esearch 失败: {e}")
        return []


def pubmed_summary(pmids):
    """esummary 拿元数据（标题/期刊/作者/DOI）"""
    if not pmids:
        return {}
    q = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(pmids), "retmode": "json"})
    try:
        return http_json(EUTILS + "esummary.fcgi?" + q).get("result", {})
    except Exception as e:
        print(f"[warn] esummary 失败: {e}")
        return {}


def pubmed_abstracts(pmids):
    """efetch 批量拿摘要（XML）"""
    if not pmids:
        return {}
    q = urllib.parse.urlencode({"db": "pubmed", "id": ",".join(pmids),
                                "rettype": "abstract", "retmode": "xml"})
    out = {}
    try:
        req = urllib.request.Request(EUTILS + "efetch.fcgi?" + q)
        with urllib.request.urlopen(req, timeout=60) as r:
            root = ET.fromstring(r.read())
        for art in root.findall(".//PubmedArticle"):
            pmid = art.findtext(".//PMID", "")
            parts = [t.text or "" for t in art.findall(".//AbstractText")]
            if pmid:
                out[pmid] = " ".join(parts)[:600]
    except Exception as e:
        print(f"[warn] efetch 失败: {e}")
    return out


def make_guides(docs):
    """aihubmix 批量生成中文导读（每批最多 12 篇）"""
    if not docs or not AIHUBMIX_KEY:
        return {}
    guides = {}
    for i in range(0, len(docs), 12):
        batch = docs[i:i + 12]
        lines = []
        for d in batch:
            ab = (d.get("abstract") or "")[:300]
            lines.append(f"PMID {d['pmid']}: {d['title']}\n{ab}")
        prompt = (
            "以下是 PubMed 新检索到的文献（PMID+标题+摘要片段）。\n"
            "请为每篇写 1-2 句中文导读：一句说明研究主题，一句说明主要发现或结论。\n"
            "严格按格式输出，每篇一行：PMID: 中文导读\n\n"
            + "\n\n".join(lines)
        )
        body = {
            "model": AIHUBMIX_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "thinking": {"type": "disabled"},  # glm-4.7 默认推理，禁用避免思考占满 max_tokens 导致 content 为空
            "temperature": 0.3, "max_tokens": 2048,
        }
        try:
            j = http_json(AIHUBMIX_URL, data=json.dumps(body).encode(),
                          headers={"Authorization": f"Bearer {AIHUBMIX_KEY}",
                                   "Content-Type": "application/json"}, timeout=180)
            content = j["choices"][0]["message"]["content"]
            for line in content.splitlines():
                s = line.strip()
                if not s:
                    continue
                # aihubmix 可能输出 "PMID 12345: 导读" 或 "12345: 导读"，兼容两种
                m = re.match(r"^(?:PMID\s*)?(\d+)\s*:", s)
                if m:
                    guides[m.group(1)] = s.split(":", 1)[1].strip()
        except Exception as e:
            print(f"[warn] 导读生成失败: {e}")
    return guides


def send_email(subject, html):
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVERS
    with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=60) as s:
        s.login(EMAIL_SENDER, EMAIL_PASSWORD)
        s.sendmail(EMAIL_SENDER,
                   [x.strip() for x in EMAIL_RECEIVERS.split(",") if x.strip()],
                   msg.as_string())


def main():
    print(f"[start] {datetime.now().strftime('%Y-%m-%d %H:%M')} 主题数={len(TOPICS)}")
    all_docs, seen = [], set()
    for name, term, reldate, retmax in TOPICS:
        pmids = pubmed_search(term, reldate, retmax)
        time.sleep(0.4)
        summ = pubmed_summary(pmids)
        time.sleep(0.4)
        abs_map = pubmed_abstracts(pmids)
        time.sleep(0.4)
        docs = []
        for pid in pmids:
            if pid in seen:
                continue
            seen.add(pid)
            d = summ.get(pid, {})
            if not d:
                continue
            doi = next((i.get("value", "") for i in d.get("articleids", [])
                        if i.get("idtype") == "doi"), "")
            docs.append({
                "pmid": pid,
                "title": d.get("title", ""),
                "source": d.get("source", ""),
                "pubdate": d.get("pubdate", ""),
                "authors": [a.get("name", "") for a in d.get("authors", [])][:3],
                "doi": doi,
                "abstract": abs_map.get(pid, ""),
                "link": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
                "topic": name,
            })
        all_docs.extend(docs)
        print(f"[{name}] {len(docs)} 篇")

    date_str = datetime.now().strftime("%Y-%m-%d")
    if not all_docs:
        html = "<p>本周无新增文献。</p>"
        total = 0
    else:
        guides = make_guides(all_docs)
        print(f"[guide] 中文导读生成 {len(guides)}/{len(all_docs)} 篇")
        blocks = []
        for name, *_ in TOPICS:
            tdocs = [d for d in all_docs if d["topic"] == name]
            if not tdocs:
                continue
            items = []
            for d in tdocs:
                g = guides.get(d["pmid"], "")
                g_html = f"<p style='color:#1a5276;font-weight:bold;'>{g}</p>" if g else ""
                au = ", ".join(d["authors"])
                doi_html = (f" | DOI: <a href='https://doi.org/{d['doi']}'>{d['doi']}</a>"
                            if d["doi"] else "")
                ab_html = (f"<p style='color:#666;font-size:12px;'>{d['abstract']}</p>"
                           if d["abstract"] else "")
                items.append(
                    "<div style='margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid #eee;'>"
                    f"{g_html}"
                    f"<a href='{d['link']}' style='font-size:14px;color:#1a5276;'>{d['title']}</a>"
                    f"<p style='color:#888;font-size:12px;margin:3px 0;'>{d['source']} | {d['pubdate']} | {au}{doi_html}</p>"
                    f"{ab_html}</div>"
                )
            blocks.append(f"<h3 style='color:#c0392b;margin-top:20px;'>{name}</h3>{''.join(items)}")
        total = len(all_docs)
        html = (
            "<div style='font-family:Arial,Microsoft YaHei,sans-serif;max-width:720px;margin:auto;'>"
            f"<h2 style='color:#333;'>每周文献检索 - {date_str}</h2>"
            f"<p style='color:#999;'>PubMed 新增 {total} 篇（自动检索 + AI 中文导读）</p>"
            f"{''.join(blocks)}"
            "<p style='color:#aaa;font-size:11px;margin-top:30px;'>由 GitHub Actions 自动生成，每周五 18:00 发送。</p></div>"
        )

    subject = f"每周文献检索 {date_str}（{total} 篇）"
    send_email(subject, html)
    print(f"[done] 邮件已发送至 {EMAIL_RECEIVERS}，共 {total} 篇")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[error] {e}")
        sys.exit(1)