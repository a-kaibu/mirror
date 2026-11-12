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


def parse_multi_inside(html,page):
    parts=urlsplit(a.original(page))
    if parts.hostname not in {"www.pachi.ac","pachi.ac"} or not parts.path.startswith("/~multi/download/index/"):return []
    root=a.DOM(html).root
    def classified(node,name):return name in node.attrs.get("class", "").split()
    brands=[n for n in root.all({"div"}) if classified(n,"brand")]
    titles=[n for n in root.all({"div"}) if classified(n,"title")]
    if not brands or not titles:return []
    brand_links=list(brands[0].all({"a"}))
    if not brand_links:return []
    brand=brand_links[0].text()
    title=re.sub(r"\s*\[.*", "", titles[0].text()).strip()
    result=[]
    for block in root.all({"div"}):
        if not classified(block,"mirror"):continue
        names=[n.text() for n in block.all({"span"}) if classified(n,"filename")]
        hashes=[n.text() for n in block.all({"span"}) if classified(n,"md5")]
        if not names:continue
        filename=names[0];mirrors=[]
        for link in block.all({"a"}):
            target=a.resolve(page,link.attrs.get("href", ""))
            if not target or unquote(Path(urlsplit(a.original(target)).path).name)!=filename:continue
            stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
            raw=a.original(target)
            mirrors.extend(([a.replay(raw,stamp[1],raw=True)] if stamp else [])+[raw])
        if not mirrors:continue
        materials=[n.text().replace("◇", "").strip() for n in block.all({"span"}) if classified(n,"material")]
        dates=[n.text().strip("[]").replace("/","-") for n in block.all({"span"}) if classified(n,"date")]
        size=re.search(r"/\s*([\d.]+\s*[KMG]B)\s*/",block.text(),re.I)
        md5=hashes[0].lower() if hashes and re.fullmatch(r"[a-fA-F0-9]{32}",hashes[0]) else None
        ident=hashlib.sha256(("multi_inside\n"+a.original(page)+"\n"+filename).encode()).hexdigest()
        result.append({"id":ident,"site":"multi_inside","page_url":page,"original_page_url":a.original(page),
            "title":title,"brand":brand,"release_name":materials[0] if materials else filename,
            "filename":filename,"mirrors":list(dict.fromkeys(mirrors)),"expected_md5":md5,
            "expected_size":None,"size_text":size[1] if size else None,"published_at":dates[0] if dates else None,
            "fields":{"parser":"multi_inside","size_is_rounded":True}})
    return result


def parse_mimimaid(html,page):
    if urlsplit(a.original(page)).hostname!="mimimaid.moe.hm":return []
    root=a.DOM(html).root
    heads=list(root.all({"title"}))
    if not heads or "みみめいど" not in heads[0].text():return []
    brand=heads[0].text().split("|")[0].strip().lstrip("■").strip()
    results=[]
    for heading in root.all({"span"}):
        if "style9" not in heading.attrs.get("class", "").split():continue
        title=heading.text();group=heading.parent
        while group.parent and group.tag!="table":group=group.parent
        for block in group.all({"table"}):
            if block.attrs.get("width")!="700":continue
            fields=a.labels(block)
            if not fields.get("素材名"):continue
            text=block.text();digest=re.search(r"MD5[：:]\s*([a-fA-F0-9]{32})",text)
            size=re.search(r"\(([0-9,]+)\s*バイト\)",text)
            date=re.search(r"\d{4}/\d{2}/\d{2}",fields.get("公開日", ""))
            for link in block.all({"a"}):
                target=a.resolve(page,link.attrs.get("href", ""))
                if not target or not a.EXT.search(urlsplit(a.original(target)).path):continue
                raw=a.original(target);filename=unquote(Path(urlsplit(raw).path).name)
                stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
                mirrors=([a.replay(raw,stamp[1],raw=True)] if stamp else [])+[raw]
                ident=hashlib.sha256(("mimimaid\n"+a.original(page)+"\n"+filename).encode()).hexdigest()
                results.append({"id":ident,"site":"mimimaid","page_url":page,"original_page_url":a.original(page),
                    "title":title,"brand":brand,"release_name":fields["素材名"],"filename":filename,"mirrors":mirrors,
                    "expected_md5":digest[1].lower() if digest else None,"expected_size":int(size[1].replace(",", "")) if size else None,
                    "size_text":fields.get("ファイルサイズ"),"published_at":date[0].replace("/","-") if date else None,"fields":fields})
    return list({r["id"]:r for r in results}.values())
