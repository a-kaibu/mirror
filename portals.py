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


def parse_nekoneko(html,page):
    if urlsplit(a.original(page)).hostname not in {"nekoneko-web.multi-band.net","www.nekoneko-web.net"}:return []
    root=a.DOM(html).root
    brand=re.search(r"□\s*(.+?)さんのミラーページ",root.text())
    if not brand:return []
    brand=brand[1].strip();title=None;results=[]
    for node in root.all({"font","a"}):
        if node.tag=="font" and node.attrs.get("color", "").lower()=="#000000":
            heading=re.fullmatch(r"\s*『(.+?)』\s*",node.text())
            if heading:title=heading[1]
        if node.tag!="a" or not title:continue
        target=a.resolve(page,node.attrs.get("href", ""))
        if not target or not a.EXT.search(urlsplit(a.original(target)).path):continue
        context=node.parent;digest=None
        while context.parent and context.tag not in {"td","table","body"}:
            digest=re.search(r"MD5\s*[:：]\s*([a-fA-F0-9]{32})",context.text())
            if digest:break
            context=context.parent
        if not digest:continue
        binaries=[link for link in context.all({"a"}) if a.EXT.search(urlsplit(a.original(a.resolve(page,link.attrs.get("href", "")) or "")).path)]
        if len(binaries)!=1:continue
        text=context.text();size=re.search(r"『サイズ[：:]\s*([\d.]+\s*[KMG]B)』",text)
        if not size:continue
        release=text[:size.start()].strip()
        raw=a.original(target);filename=unquote(Path(urlsplit(raw).path).name);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
        ident=hashlib.sha256(("nekoneko\n"+a.original(page)+"\n"+filename).encode()).hexdigest()
        results.append({"id":ident,"site":"nekoneko","page_url":page,"original_page_url":a.original(page),
            "title":title,"brand":brand,"release_name":release,"filename":filename,
            "mirrors":([a.replay(raw,stamp[1],raw=True)] if stamp else [])+[raw],
            "expected_md5":digest[1].lower(),"expected_size":None,"size_text":size[1],"published_at":None,
            "fields":{"parser":"nekoneko","size_is_rounded":True}})
    return list({r["id"]:r for r in results}.values())


def parse_tsundere(html,page):
    if urlsplit(a.original(page)).hostname!="mirror.tsundere.ne.jp":return []
    root=a.DOM(html).root;fields=a.labels(root)
    label=fields.get("タイトル", "");brand=fields.get("ブランド")
    identity=re.split(r"\s{2,}|　",label,maxsplit=1)
    if not brand or len(identity)!=2:return []
    title,release=(v.strip() for v in identity)
    if not title or not release:return []
    digest=re.search(r"\b([a-fA-F0-9]{32})\b",fields.get("md5/sha1", ""))
    size=re.search(r"\(([0-9,]+)\s*Byte\)",fields.get("ファイルサイズ", ""),re.I)
    results=[]
    for span in root.all({"span"}):
        if "downloadlink" not in span.attrs.get("class", "").split():continue
        for link in span.all({"a"}):
            target=a.resolve(page,link.attrs.get("href", ""))
            if not target or not a.EXT.search(urlsplit(a.original(target)).path):continue
            raw=a.original(target);filename=unquote(Path(urlsplit(raw).path).name);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
            ident=hashlib.sha256(("tsundere\n"+a.original(page)+"\n"+filename).encode()).hexdigest()
            results.append({"id":ident,"site":"tsundere","page_url":page,"original_page_url":a.original(page),
                "title":title,"brand":brand,"release_name":release,"filename":filename,
                "mirrors":([a.replay(raw,stamp[1],raw=True)] if stamp else [])+[raw],
                "expected_md5":digest[1].lower() if digest else None,"expected_size":int(size[1].replace(",", "")) if size else None,
                "size_text":fields.get("ファイルサイズ"),"published_at":fields.get("当サイト公開日"),"fields":fields})
    return list({r["id"]:r for r in results}.values())


def parse_dlsite(html,page):
    parts=urlsplit(a.original(page))
    product=re.search(r'/product_id/([A-Z]{2}[0-9]+)\.html',parts.path)
    if parts.hostname!='www.dlsite.com' or not product:return []
    root=a.DOM(html).root;fields=a.labels(root);titles=[n.text() for n in root.all({'h1'}) if n.text()]
    brand=fields.get('サークル名') or fields.get('ブランド名') or fields.get('メーカー名')
    if not brand or not titles:return []
    results=[]
    for link in root.all({'a'}):
        if '体験版' not in link.text():continue
        target=a.resolve(page,link.attrs.get('href',''))
        if not target:continue
        raw=a.original(target)
        if urlsplit(raw).hostname!='trial.dlsite.com' or not a.EXT.search(urlsplit(raw).path):continue
        filename=unquote(Path(urlsplit(raw).path).name);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
        ident=hashlib.sha256(('dlsite\n'+a.original(page)+'\n'+filename).encode()).hexdigest()
        results.append({'id':ident,'site':'dlsite','page_url':page,'original_page_url':a.original(page),
            'title':titles[0],'brand':brand,'release_name':'体験版','filename':filename,
            'mirrors':([a.replay(raw,stamp[1],raw=True)] if stamp else [])+[raw],
            'expected_md5':None,'expected_size':None,'size_text':None,'published_at':None,
            'fields':{'dlsite_product_id':product[1],'published_link':target}})
    return list({r['id']:r for r in results}.values())


def parse_d_station(html,page):
    if urlsplit(a.original(page)).hostname!='mirror.studio-ramble.com':return []
    root=a.DOM(html).root;results=[]
    for link in root.all({'a'}):
        target=a.resolve(page,link.attrs.get('href',''))
        if not target or not urlsplit(a.original(target)).path.endswith('/download.php'):continue
        context=link.parent
        while context.parent and context.tag!='table':context=context.parent
        text=context.text();digest=re.search(r'MD5[：:]\s*([a-fA-F0-9]{32})',text)
        size=re.search(r'ファイルサイズ[：:]\s*([\d,]+)\s*バイト',text)
        if not digest or not size:continue
        headers=list(context.all({'tr'}))
        if len(headers)<2:continue
        makers=list(headers[1].all({'a'}))
        if not makers:continue
        brand=re.sub(r'^[（(][ＣC©][)）]\s*','',makers[0].text()).strip()
        combined=link.text();split=re.search(r'(CMデモムービー|デモムービー|体験版|修正パッチ|パッチ|ムービー)',combined)
        if not brand or not split:continue
        title=combined[:split.start()].strip();release=combined[split.start():].strip()
        if not title:continue
        raw=a.original(target);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
        ident=hashlib.sha256(('d_station\n'+raw+'\n'+digest[1].lower()).encode()).hexdigest()
        date=re.search(r'\b(\d{4})/(\d{1,2})/(\d{1,2})\b',text)
        results.append({'id':ident,'site':'d_station','page_url':page,'original_page_url':a.original(page),
            'title':title,'brand':brand,'release_name':release,'filename':None,
            'mirrors':([a.replay(raw,stamp[1],raw=True)] if stamp else [])+[raw],
            'expected_md5':digest[1].lower(),'expected_size':int(size[1].replace(',','')),'size_text':size[0],
            'published_at':f'{date[1]}-{int(date[2]):02}-{int(date[3]):02}' if date else None,
            'fields':{'parser':'d_station','download_endpoint':raw}})
    return list({r['id']:r for r in results}.values())
