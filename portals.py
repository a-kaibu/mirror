"""Distribution blocks on public catalogue pages; no purchase/download guessing."""
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit,unquote
import archiver as a


def parse_moepedia(html,page):
    original=a.original(page);parts=urlsplit(original)
    if parts.hostname!="moepedia.net" or not re.fullmatch(r"/game/\d+/?",parts.path):return []
    root=a.DOM(html).root;fields=a.labels(root)
    headings=[n.text() for n in root.all({"h2"}) if n.text()]
    brand=fields.get("ブランド")
    if not headings or not brand:raise ValueError("Moepedia game identity missing")
    title=headings[0];result={}
    for block in root.all({"div","section"}):
        if "trial" not in block.attrs.get("class","").split():continue
        if not any(h.text()=="体験版" for h in block.all({"h2","h3"})):continue
        for link in block.all({"a"}):
            target=a.resolve(page,link.attrs.get("href",""))
            if not target or not a.EXT.search(urlsplit(a.original(target)).path):continue
            filename=unquote(Path(urlsplit(a.original(target)).path).name)
            ident=hashlib.sha256(("moepedia\n"+original+"\n"+filename).encode()).hexdigest()
            from mirror_sources import clean
            raw=clean(target)
            stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
            mirrors=([a.replay(raw,stamp[1],raw=True)] if stamp else [])+[raw]
            if raw!=a.original(target):mirrors.append(a.original(target))
            if ident in result:
                result[ident]["mirrors"]=list(dict.fromkeys(result[ident]["mirrors"]+mirrors));continue
            result[ident]={"id":ident,"site":"moepedia","page_url":page,"original_page_url":original,
                "title":title,"brand":brand,"release_name":"体験版","filename":filename,
                "mirrors":mirrors,"expected_md5":None,"expected_size":None,"size_text":None,"published_at":None,
                "fields":{"moepedia_game_id":int(parts.path.strip('/').split('/')[1]),"distribution_label":link.text(),"published_link":target}}
    return list(result.values())
