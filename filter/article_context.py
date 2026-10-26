"""Preserve publisher article types and explicitly associated works, not navigation noise."""
import asyncio
from urllib.parse import urljoin
from bs4 import BeautifulSoup

def extract_context(html, url):
    soup=BeautifulSoup(html,"html.parser")
    meta={x.get("name", "").lower():x.get("content", "") for x in soup.select("meta[name]")}
    article_type=meta.get("citation_article_type", "")
    associated=[]
    for link in soup.select('a[href][data-track-category="associated content"]'):
        title=link.get_text(" ",strip=True)
        if title:associated.append({"url":urljoin(url,link["href"]),"title":title,"doi":link.get("data-track-label", "")})
    references=[]
    for section in soup.select('section[data-title="References"], section#references, #Bib1'):
        for link in section.select('a[href*="doi.org/"]'):
            references.append(link["href"])
    # A citation alone is not an assertion of shared identity.
    return {"article_type":article_type,"doi":meta.get("citation_doi", ""),
            "associated_content":associated,"reference_urls":sorted(set(references)),"retrieved_from":url}

async def enrich_context(articles, max_pages=12):
    import requests
    sem=asyncio.Semaphore(4)
    async def fetch(art):
        async with sem:
            try:
                def read():
                    res=requests.get(art["url"],timeout=20,headers={"User-Agent":"Weekly-AI-Report-Agent/1.0"})
                    res.raise_for_status()
                    return extract_context(res.text,art["url"])
                context=await asyncio.to_thread(read)
                if context["article_type"] or context["associated_content"]:
                    art.setdefault("raw_extra", {})["identity_context"]=context
            except (requests.RequestException,ValueError):pass
    # Short journal RSS descriptions often omit the commentary/research relationship.
    selected=[a for a in articles if len(a.get("summary", ""))<800
              and any(s in a.get("source_name", "").lower() for s in ("nature","science","journal"))
              and not (a.get("raw_extra") or {}).get("identity_context")][:max_pages]
    await asyncio.gather(*(fetch(a) for a in selected))
    return articles
