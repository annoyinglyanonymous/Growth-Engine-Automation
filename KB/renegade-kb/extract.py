#!/usr/bin/env python
"""Stdlib-only HTML -> markdown extractor for the Renegade Insurance knowledgebase."""
import html, json, re, sys, pathlib
from html.parser import HTMLParser

DROP = {"script","style","noscript","svg","template","iframe","picture","source","select","option"}
BLOCK = {"p","div","section","article","header","footer","main","aside","ul","ol","table","tr",
         "h1","h2","h3","h4","h5","h6","li","td","th","br","hr","blockquote","form","label","figcaption"}
HEAD = {"h1":"#","h2":"##","h3":"###","h4":"####","h5":"#####","h6":"######"}

class Ex(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out=[]; self.drop=0; self.jsonld=[]; self._ld=False
        self.meta={}; self.intitle=False; self.pending=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=="script":
            if a.get("type","").lower()=="application/ld+json": self._ld=True; self._ldbuf=""
            self.drop+=1; return
        if tag in DROP: self.drop+=1; return
        if self.drop: return
        if tag=="title": self.intitle=True; return
        if tag=="meta":
            n=(a.get("name") or a.get("property") or "").lower()
            if n in ("description","og:description","og:title","keywords"): self.meta[n]=a.get("content","")
            return
        if tag=="link" and a.get("rel")=="canonical": self.meta["canonical"]=a.get("href",""); return
        if tag=="img":
            alt=(a.get("alt") or "").strip()
            if alt: self.out.append(f"\n[image: {alt}]\n")
            return
        if tag in ("input","textarea"):
            ph=(a.get("placeholder") or a.get("aria-label") or a.get("name") or "").strip()
            ty=(a.get("type") or "").lower()
            if ph and ty not in ("hidden",): self.out.append(f"\n[field: {ph}]\n")
            return
        if tag=="a":
            h=a.get("href","")
            self.pending=h if h.startswith(("http","/","tel:","mailto:")) else None
            return
        if tag in HEAD: self.out.append("\n\n"+HEAD[tag]+" ")
        elif tag=="li": self.out.append("\n- ")
        elif tag in ("br","hr"): self.out.append("\n")
        elif tag in BLOCK: self.out.append("\n")
    def handle_endtag(self,tag):
        if tag=="script":
            if self._ld:
                self._ld=False
                try: self.jsonld.append(json.loads(self._ldbuf))
                except Exception: pass
            self.drop=max(0,self.drop-1); return
        if tag in DROP: self.drop=max(0,self.drop-1); return
        if self.drop: return
        if tag=="title": self.intitle=False; return
        if tag=="a":
            if self.pending and self.pending.startswith(("tel:","mailto:")):
                self.out.append(f" <{self.pending}>")
            self.pending=None; return
        if tag in HEAD or tag in BLOCK: self.out.append("\n")
    def handle_data(self,d):
        if self._ld: self._ldbuf+=d; return
        if self.drop: return
        if self.intitle: self.meta["title"]=self.meta.get("title","")+d; return
        if d.strip(): self.out.append(d)
    def text(self):
        t="".join(self.out)
        t=re.sub(r"[ \t ]+"," ",t)
        t=re.sub(r" *\n *","\n",t)
        t=re.sub(r"\n{3,}","\n\n",t)
        # drop lines that are pure punctuation/empty markers
        lines=[l.rstrip() for l in t.split("\n")]
        keep=[]
        for l in lines:
            if re.fullmatch(r"[#\-\s*_|]*",l or ""): 
                if keep and keep[-1]!="": keep.append("")
                continue
            keep.append(l)
        t="\n".join(keep)
        return re.sub(r"\n{3,}","\n\n",t).strip()

def run(p):
    raw=pathlib.Path(p).read_bytes().decode("utf-8","replace")
    e=Ex(); e.feed(raw)
    return e.meta, e.text(), e.jsonld

if __name__=="__main__":
    m,t,l=run(sys.argv[1])
    print("META:",json.dumps(m,indent=1)[:1200])
    print("JSONLD blocks:",len(l))
    print("TEXT chars:",len(t))
    print("="*70)
    print(t[:int(sys.argv[2]) if len(sys.argv)>2 else 3000])
