"""Bounded supplementary coverage queries for fresh runs; frozen replay never fetches."""
import hashlib
import re
from filter.quality import parse_date, report_window

def evidence_sources(articles, cutoff, max_queries=3, max_results=3):
    if max_queries<=0 or max_results<=0:return []
    start,end=report_window(cutoff)
    eligible=[]
    for art in articles:
        date=parse_date(art.get("published"))
        title=art.get("title", "").strip()
        text=title+" "+art.get("summary", "")[:1800]
        if not date or not start<=date<=end or len(title)<12:continue
        if not re.search(r"agent|neural|learning|language model|AI|智能|模型",text,re.I):continue
        eligible.append(art)
    eligible.sort(key=lambda a:(-parse_date(a["published"]).timestamp(),a["url"]))
    selected=[];seen=set();categories=set()
    # First pass gives different domains a chance; second fills spare budget.
    for distinct in (True,False):
        for art in eligible:
            category=art.get("category", "")
            if art["url"] in seen or (distinct and category in categories):continue
            title=art["title"].replace('"', '').strip()[:160]
            ident=hashlib.sha256(art["url"].encode()).hexdigest()[:12]
            selected.append({"id":"exa_search:evidence-"+ident,"connector":"exa_search",
                "name":"Exa evidence: "+title,"query":'"'+title+'"',"max_results":max_results,
                "category":category,"evidence_seed_url":art["url"]})
            seen.add(art["url"]);categories.add(category)
            if len(selected)>=max_queries:return selected
    return selected
