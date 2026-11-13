"""Distribution blocks on public catalogue pages; no purchase/download guessing."""
import hashlib
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit,unquote,parse_qs
import archiver as a


def parse_moepedia(html,page):
    original=a.original(page);parts=urlsplit(original)
    if parts.hostname!="moepedia.net" or not re.fullmatch(r"/game/\d+/?",parts.path):return []
    root=a.DOM(html).root;fields=a.labels(root)
    headings=[n.text() for n in root.all({"h2"}) if n.text()]
    brand=fields.get("ブランド")
    if not headings:raise ValueError("Moepedia game title missing")
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
            mirrors=([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw]
            if raw!=a.original(target):mirrors.append(a.original(target))
            if ident in result:
                result[ident]["mirrors"]=list(dict.fromkeys(result[ident]["mirrors"]+mirrors));continue
            result[ident]={"id":ident,"site":"moepedia","page_url":page,"original_page_url":original,
                "title":title,"brand":brand,"release_name":"体験版","filename":filename,
                "mirrors":mirrors,"expected_md5":None,"expected_size":None,"size_text":None,"published_at":None,
                "fields":{"moepedia_game_id":int(parts.path.strip('/').split('/')[1]),"distribution_label":link.text(),"published_link":target}}
    return list(result.values())


def _parse_multi_inside_legacy(root,page):
    credits=[n for n in root.all({'div'}) if re.match(r'^\s*(?:\(c\)|©)\s*',n.text(),re.I)]
    headings=[n.text().strip('[]［］ ') for n in root.all({'h1'}) if n.text().strip('[]［］ ').endswith('ミラー')]
    bodies=[n for n in root.all({'div'}) if '＜ファイル本体＞' in n.text()]
    if not credits or not headings or not bodies:return []
    credit=min(credits,key=lambda n:len(n.text()));body=min(bodies,key=lambda n:len(n.text()))
    publishers=[]
    for link in credit.all({'a'}):
        target=a.resolve(page,link.attrs.get('href',''))
        if target and link.text():publishers.append({'name':link.text(),'url':a.original(target)})
    if not publishers:return []
    brand=re.sub(r'^\s*(?:\(c\)|©)\s*','',credit.text(),flags=re.I).strip()
    if len(publishers)==1:brand=publishers[0]['name']
    else:
        # A nested official brand directory identifies the specific sub-brand;
        # unrelated copyright holders remain combined exactly as credited.
        paths=[urlsplit(entry['url']) for entry in publishers]
        directories=[entry.path.rsplit('/',1)[0]+'/' for entry in paths]
        if (all(entry.hostname==paths[-1].hostname for entry in paths)
                and all(directories[-1].startswith(parent) for parent in directories[:-1])
                and any(parent!=directories[-1] for parent in directories[:-1])):
            brand=publishers[-1]['name']
    title=re.sub(r'\s+[^\s]+ミラー$','',headings[0]).strip()
    if not title or title==headings[0]:return []
    hashes={}
    for match in re.finditer(r'\bMD5\s*\(\s*(.*?)\s*\)\s*=\s*([a-fA-F0-9]{32})\b',body.text(),re.S):
        hashes.setdefault(match[1].strip(),set()).add(match[2].lower())
    prefix=[];result={};stack=list(reversed(body.children))
    while stack:
        node=stack.pop()
        if isinstance(node,str):prefix.append(node);continue
        if node.tag=='br':prefix.append('\n')
        if node.tag=='a':
            href=node.attrs.get('href','');ftp=re.match(r'^(?:https?://web\.archive\.org/web/\d{14}(?:[a-z_]+)?/)?(ftp://.+)$',href)
            target=a.resolve(page,href);raw=ftp[1] if ftp else a.original(target) if target else None
            if raw and a.EXT.search(urlsplit(raw).path):
                filename=unquote(Path(urlsplit(raw).path).name);values=hashes.get(filename,set())
                if len(values)<=1:
                    before=' '.join(prefix);labels=list(re.finditer(r'◇\s*([^：:\n]+)\s*[：:]',before))
                    label=labels[-1][1].strip() if labels else filename
                    tail=before[before.rfind(filename)+len(filename):] if filename in before else ''
                    size=re.search(r'\[[^\]\r\n]*,\s*([\d.]+\s*[KMGT]B)\s*\]',tail,re.I)
                    ident=hashlib.sha256(('multi_inside\n'+a.original(page)+'\n'+filename).encode()).hexdigest()
                    entry=result.setdefault(filename,{'id':ident,'site':'multi_inside','page_url':page,'original_page_url':a.original(page),
                        'title':title,'brand':brand,'release_name':label,'filename':filename,'mirrors':[],
                        'expected_md5':next(iter(values),None),'expected_size':None,'size_text':size[1] if size else None,'published_at':None,
                        'fields':{'parser':'multi_inside_legacy','publisher_credit':credit.text(),'publisher_links':publishers,'size_is_rounded':True}})
                    if ftp:entry['fields'].setdefault('published_ftp_links',[]).append({'url':raw,'observed_href':href})
                    else:
                        stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
                        entry['mirrors']+=([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw]
        stack.extend(reversed(node.children))
    for entry in result.values():entry['mirrors']=list(dict.fromkeys(entry['mirrors']))
    return list(result.values())



def _parse_multi_inside_catalogue(root,page):
    result={}
    for block in root.all({'div'}):
        if 'main' not in block.attrs.get('class','').split():continue
        brands=[n.text().strip('[] ') for n in block.all({'span'}) if 'color-3' in n.attrs.get('class','').split()]
        titles=[n.text().strip() for n in block.all({'span'}) if 'color-4' in n.attrs.get('class','').split()]
        dates=[n.text() for n in block.all({'span'}) if 'date' in n.attrs.get('class','').split()]
        categories=[n.text() for n in block.all({'span'}) if 'color-9' in n.attrs.get('class','').split()]
        if len(brands)!=1 or len(titles)!=1 or len(dates)!=1 or not brands[0] or not titles[0]:continue
        date=re.fullmatch(r'-(\d{4})/(\d{2})/(\d{2})-',dates[0].strip())
        if not date:continue
        published='-'.join(date.groups())
        for link in block.all({'a'}):
            target=a.resolve(page,link.attrs.get('href',''))
            if not target or not a.EXT.search(urlsplit(a.original(target)).path):continue
            raw=a.original(target)
            try:a.archive_url_identity(raw)
            except ValueError:continue
            filename=unquote(Path(urlsplit(raw).path).name)
            siblings=link.parent.children;position=siblings.index(link);tail=[]
            for node in siblings[position+1:]:
                if isinstance(node,a.Node) and node.tag in {'a','hr','div'}:break
                tail.append(node.text() if isinstance(node,a.Node) else node)
            text=' '.join(tail);size=re.search(r'\[[^\],]+,\s*([\d.]+\s*[KMGT]B)\s*\]',text,re.I)
            label=re.search(r'\(([^)]+)\)',text)
            release=label[1].strip() if label else (' '.join(categories) or filename)
            # Preserve separate source observations even when a basename or
            # download URL was reused for another product or publication date.
            ident=hashlib.sha256(('multi_inside_catalogue\n'+a.original(page)+'\n'+brands[0]+'\n'+titles[0]+'\n'+published+'\n'+raw).encode()).hexdigest()
            stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
            mirrors=([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw]
            result[ident]={'id':ident,'site':'multi_inside','page_url':page,'original_page_url':a.original(page),
                'title':titles[0],'brand':brands[0],'release_name':release,'filename':filename,
                'expected_md5':None,'expected_size':None,'size_text':size[1] if size else None,
                'published_at':published,'mirrors':mirrors,
                'fields':{'parser':'multi_inside_catalogue','published_category':categories,'published_link':link.attrs.get('href'),'size_is_rounded':True}}
    return list(result.values())


def parse_multi_inside(html,page):
    parts=urlsplit(a.original(page))
    if parts.hostname not in {"www.pachi.ac","pachi.ac"}:return []
    root=a.DOM(html).root
    if parts.path in {'/~multi/mirror.shtml','/~multi/demo.mirror.shtml'}:return _parse_multi_inside_catalogue(root,page)
    if not parts.path.startswith("/~multi/download/index/"):return []
    def classified(node,name):return name in node.attrs.get("class", "").split()
    brands=[n for n in root.all({"div"}) if classified(n,"brand")]
    titles=[n for n in root.all({"div"}) if classified(n,"title")]
    if not brands or not titles:return _parse_multi_inside_legacy(root,page)
    brand_links=list(brands[0].all({"a"}))
    if not brand_links:return _parse_multi_inside_legacy(root,page)
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
            mirrors.extend(([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw])
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
        classes=heading.attrs.get("class", "").split()
        if "style9" not in classes:
            if heading.parent.tag!="p":continue
            before=heading.parent.children[:heading.parent.children.index(heading)]
            prefix="".join(n.text() if isinstance(n,a.Node) else n for n in before)
            if not re.fullmatch(r"\s*■\s*",prefix):continue
        title=heading.text();group=heading.parent
        while group.parent and group.tag!="table":group=group.parent
        for block in group.all({"table"}):
            if block.attrs.get("width")!="700":continue
            fields=a.labels(block)
            if not fields.get("素材名"):continue
            for row in block.all({"tr"}):
                cells=[n for n in row.children if isinstance(n,a.Node) and n.tag in {"td","th"}]
                if len(cells)>=2 and cells[0].text().strip()=="公開日":
                    fields["公開日"]=cells[1].text()
                    if len(cells)>2:fields["更新情報"]=" ".join(n.text() for n in cells[2:])
                    break
            text=block.text();digest=re.search(r"MD5[：:]\s*([a-fA-F0-9]{32})",text)
            size=re.search(r"\(([0-9,]+)\s*バイト\)",text)
            date=re.search(r"\d{4}/\d{2}/\d{2}",fields.get("公開日", ""))
            for link in block.all({"a"}):
                target=a.resolve(page,link.attrs.get("href", ""))
                if not target or not a.EXT.search(urlsplit(a.original(target)).path):continue
                raw=a.original(target);filename=unquote(Path(urlsplit(raw).path).name)
                stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
                mirrors=([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw]
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
            heading=re.fullmatch(r"\s*(?:□\s*)?『(.+?)』\s*",node.text())
            if not heading:
                heading=re.fullmatch(r"\s*[^『』\n]+『([^『』]+)』\s*",node.text())
            if heading:title=heading[1]
            elif node.attrs.get("size")=="3":
                heading=re.fullmatch(r"\s*□\s+(.+?)\s*",node.text())
                if heading and "ミラーページ" not in heading[1]:title=heading[1]
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
        release=text[:size.start()].strip();file_title=title
        episode=re.fullmatch(r"『([^『』]+)』\s*(.+)",release)
        if episode:
            file_title=episode[1];release=episode[2]
        raw=a.original(target);filename=unquote(Path(urlsplit(raw).path).name);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
        ident=hashlib.sha256(("nekoneko\n"+a.original(page)+"\n"+filename).encode()).hexdigest()
        results.append({"id":ident,"site":"nekoneko","page_url":page,"original_page_url":a.original(page),
            "title":file_title,"brand":brand,"release_name":release,"filename":filename,
            "mirrors":([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw],
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
                "mirrors":([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw],
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
        label=link.text()
        release='体験版' if '体験版' in label else ('ムービー' if re.search(r'ムービー\s*ダウンロード',label) else None)
        if release is None:continue
        target=a.resolve(page,link.attrs.get('href',''))
        if not target:continue
        raw=a.original(target)
        if urlsplit(raw).hostname!='trial.dlsite.com' or not a.EXT.search(urlsplit(raw).path):continue
        filename=unquote(Path(urlsplit(raw).path).name);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
        if release=='ムービー' and not filename.upper().startswith(product[1].upper()+'_'):continue
        size=re.search(r'\(([0-9.,]+\s*(?:[KMGT]i?B))\)',label,re.I)
        ident=hashlib.sha256(('dlsite\n'+a.original(page)+'\n'+filename).encode()).hexdigest()
        results.append({'id':ident,'site':'dlsite','page_url':page,'original_page_url':a.original(page),
            'title':titles[0],'brand':brand,'release_name':release,'filename':filename,
            'mirrors':([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw],
            'expected_md5':None,'expected_size':None,'size_text':size[1] if size else None,'published_at':None,
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
        rounded=re.search(r'ファイルサイズ[：:]\s*([\d,.]+\s*[KMGT]B)\b',text,re.I)
        if not digest or not (size or rounded):continue
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
            'mirrors':([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw],
            'expected_md5':digest[1].lower(),'expected_size':int(size[1].replace(',','')) if size else None,'size_text':size[0] if size else rounded[1],
            'published_at':f'{date[1]}-{int(date[2]):02}-{int(date[3]):02}' if date else None,
            'fields':{'parser':'d_station','download_endpoint':raw,'size_is_rounded':not bool(size)}})
    return list({r['id']:r for r in results}.values())


def parse_lapistan(html,page):
    parts=urlsplit(a.original(page));section=re.fullmatch(r'/(?:allage/)?list/([^/]+)/index\.htm[l]?',parts.path)
    if parts.hostname not in {'lapistan.jp','www.lapistan.jp'} or not section:return []
    root=a.DOM(html).root;folder=unquote(section[1]);brand=None
    for image in root.all({'img'}):
        if image.attrs.get('alt','').casefold()==folder.casefold():brand=image.attrs['alt'];break
    if not brand:
        for heading in root.all({'title'}):
            match=re.fullmatch(r'WindFriction\s+(.+)',heading.text())
            if match:brand=match[1].strip();break
    if not brand:return []
    results=[]
    for link in root.all({'a'}):
        target=a.resolve(page,link.attrs.get('href',''))
        if not target or not a.EXT.search(urlsplit(a.original(target)).path):continue
        split=re.split(r'　|\s{2,}',link.text(),maxsplit=1)
        if len(split)!=2:continue
        title,release=(v.strip() for v in split)
        siblings=link.parent.children;position=siblings.index(link);tail=[];before=[]
        for child in siblings[position+1:]:
            if isinstance(child,a.Node) and child.tag in {'hr','a'}:break
            tail.append(child.text() if isinstance(child,a.Node) else child)
        for child in reversed(siblings[:position]):
            if isinstance(child,a.Node) and child.tag=='hr':break
            before.append(child.text() if isinstance(child,a.Node) else child)
        text=' '.join(tail);date_text=' '.join(reversed(before))
        digest=re.search(r'MD5[：:]\s*([a-fA-F0-9]{32})',text)
        size=re.search(r'\[([\d.]+\s*[KMG]B)\]',text,re.I)
        if not digest:continue
        date=re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日',date_text)
        raw=a.original(target);filename=unquote(Path(urlsplit(raw).path).name);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
        ident=hashlib.sha256(('lapistan\n'+a.original(page)+'\n'+filename+'\n'+digest[1].lower()).encode()).hexdigest()
        results.append({'id':ident,'site':'lapistan','page_url':page,'original_page_url':a.original(page),
            'title':title,'brand':brand,'release_name':release,'filename':filename,
            'mirrors':([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw],
            'expected_md5':digest[1].lower(),'expected_size':None,'size_text':size[1] if size else None,
            'published_at':f'{date[1]}-{int(date[2]):02}-{int(date[3]):02}' if date else None,
            'fields':{'parser':'lapistan','size_is_rounded':True}})
    return list({r['id']:r for r in results}.values())


def parse_kokoron(html,page):
    if urlsplit(a.original(page)).hostname!='kokoron.madoka.org':return []
    root=a.DOM(html).root;heads=[n.text() for n in root.all({'title'})]
    if not heads:return []
    brand=re.sub(r'\s+mirror\s*$','',heads[0],flags=re.I).strip()
    if brand==heads[0] or not brand:return []
    results=[]
    for dt in root.all({'dt'}):
        children=[]
        for child in dt.children:
            if isinstance(child,a.Node) and child.tag in {'dt','dd'}:break
            children.append(child)
        text=' '.join(child.text() if isinstance(child,a.Node) else child for child in children)
        label=re.search(r'『(.+?)』\s*(.+?)\s+Size\s*=',text,re.S)
        digest=re.search(r'MD5\s*\(([^)]+)\)\s*=\s*([a-fA-F0-9]{32})',text)
        size=re.search(r'Size\s*=\s*([\d,]+)\s*bytes',text,re.I)
        if not label or not digest or not size:continue
        filename=digest[1];mirrors=[]
        for child in children:
            links=([child] if child.tag=='a' else list(child.all({'a'}))) if isinstance(child,a.Node) else []
            for link in links:
                target=a.resolve(page,link.attrs.get('href',''))
                if not target or unquote(Path(urlsplit(a.original(target)).path).name)!=filename:continue
                raw=a.original(target);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
                mirrors.extend(([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw])
        sha=re.search(r'SHA256\s*\('+re.escape(filename)+r'\)\s*=\s*([a-fA-F0-9]{64})',text)
        ident=hashlib.sha256(('kokoron\n'+a.original(page)+'\n'+filename+'\n'+digest[2].lower()).encode()).hexdigest()
        results.append({'id':ident,'site':'kokoron','page_url':page,'original_page_url':a.original(page),
            'title':label[1],'brand':brand,'release_name':label[2].strip(),'filename':filename,'mirrors':list(dict.fromkeys(mirrors)),
            'expected_md5':digest[2].lower(),'expected_sha256':sha[1].lower() if sha else None,
            'expected_size':int(size[1].replace(',','')),'size_text':size[0],'published_at':None,
            'fields':{'parser':'kokoron','removed_link':not mirrors}})
    # Removed builds may share a filename with a currently linked build. Keep
    # that URL as an explicitly derived CDX search hint, never as a published
    # download link for the removed build. Its own MD5/SHA must still match.
    linked={}
    for record in results:
        for url in record['mirrors']:
            linked.setdefault((record['title'],record['filename']),set()).add(a.original(url))
    for record in results:
        if not record['mirrors']:
            hints=sorted(linked.get((record['title'],record['filename']),[]))
            if hints:record['fields']['recovery_urls']=hints;record['fields']['recovery_reason']='same-page same-title same-filename linked build; search historical bytes only'
    return list({r['id']:r for r in results}.values())


def parse_dmm(html,page):
    parts=urlsplit(a.original(page))
    if parts.hostname not in {'dlsoft.dmm.co.jp','dlsoft.dmm.com'} or not re.fullmatch(r'/detail/[a-zA-Z0-9_]+/?',parts.path):return []
    root=a.DOM(html).root;titles=[n.text() for n in root.all({'h1'}) if n.text()]
    brands=set()
    for link in root.all({'a'}):
        brand=re.fullmatch(r'ブランド「(.+?)」へのリンク',link.attrs.get('title',''))
        if brand:brands.add(brand[1])
    # Archived product pages use an explicit brand row without link titles.
    # Require the maker listing link in that row, rather than sidebar brands.
    for row in root.all({'tr'}):
        if 'brand' not in row.attrs.get('class','').split():continue
        cells=[n for n in row.children if isinstance(n,a.Node) and n.tag in {'td','th'}]
        if len(cells)!=2 or cells[0].text().strip().rstrip('：:')!='ブランド名':continue
        links=list(cells[1].all({'a'}))
        if len(links)!=1 or not links[0].text().strip():continue
        target=a.resolve(page,links[0].attrs.get('href',''))
        if not target:continue
        maker=urlsplit(a.original(target))
        if maker.hostname!=parts.hostname or not re.fullmatch(r'/list/article=maker/id=\d+/?',maker.path):continue
        brands.add(links[0].text().strip())
    if len(titles)!=1 or len(brands)!=1:return []
    brand=next(iter(brands));results=[]
    for link in root.all({'a'}):
        if '体験版' not in link.text():continue
        target=a.resolve(page,link.attrs.get('href',''))
        if not target:continue
        raw=a.original(target)
        if not re.fullmatch(r'sample[0-9]+\.dmm\.(?:co\.jp|com)',urlsplit(raw).hostname or '') or not a.EXT.search(urlsplit(raw).path):continue
        filename=unquote(Path(urlsplit(raw).path).name);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
        ident=hashlib.sha256(('dmm\n'+a.original(page)+'\n'+filename).encode()).hexdigest()
        results.append({'id':ident,'site':'dmm','page_url':page,'original_page_url':a.original(page),
            'title':titles[0],'brand':brand,'release_name':'体験版','filename':filename,
            'mirrors':([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw],
            'expected_md5':None,'expected_size':None,'size_text':None,'published_at':None,
            'fields':{'dmm_content_id':parts.path.strip('/').split('/')[-1],'published_link':target}})
    return list({r['id']:r for r in results}.values())


def parse_alice_archives(html, page):
    original = a.original(page)
    parts = urlsplit(original)
    if parts.hostname != "retropc.net" or not re.fullmatch(r"/alice/\d+\.html", parts.path):
        return []
    root = a.DOM(html).root
    titles = [" ".join(n.text().split()) for n in root.all({"font"})
              if n.attrs.get("size") == "5"]
    fields = a.labels(root)
    brand = " ".join(fields.get("発売元", "").split())
    if len(titles) != 1 or brand not in {"アリスソフト", "チャンピオンソフト"}:
        raise ValueError("Alice archives product identity missing or ambiguous")
    result = {}
    for link in root.all({"a"}):
        target = a.resolve(page, link.attrs.get("href", ""))
        if not target:
            continue
        raw = a.original(target)
        binary = urlsplit(raw)
        if binary.hostname != "retropc.net" or not binary.path.startswith("/alice/files/") or not a.EXT.search(binary.path):
            continue
        label = " ".join(link.text().split())
        if not re.search("ディスクイメージ|CDイメージ|アーカイブ", label):
            continue
        filename = unquote(Path(binary.path).name)
        exact = re.findall(r"([\d,]+)\s*バイト", label)
        platform = re.match(r"(.+?版)", label)
        ident = hashlib.sha256(("alice_archives\n"+original+"\n"+binary.path).encode()).hexdigest()
        stamp = a.WAYBACK.match(target) or a.WAYBACK.match(page)
        mirrors = ([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else []) + [raw]
        record = {"id":ident,"site":"alice_archives","page_url":page,"original_page_url":original,
            "title":titles[0],"brand":brand,"release_name":"本編 "+(platform[1] if platform else label),
            "filename":filename,"mirrors":mirrors,"expected_md5":None,
            "expected_size":int(exact[0].replace(",","")) if len(exact)==1 else None,
            "size_text":label,"published_at":None,
            "fields":{"distribution_kind":"full","platform":platform[1] if platform else None,
                      "download_label":label,"発売年月":fields.get("発売年月"),
                      "license_url":"https://www.alicesoft.com/about/#cont08",
                      "source_conditions":"配布フリー宣言対象。原アーカイブの内容をそのまま保存。"}}
        result[ident] = record
    return list(result.values())


def parse_alicefree(html, page):
    original = a.original(page)
    if urlsplit(original).hostname != "alicefree.fastlast.org" or urlsplit(original).path not in {"/","/index.html"}:
        return []
    root = a.DOM(html).root
    if "配布フリー宣言" not in root.text():
        raise ValueError("Alice free catalogue identity missing")
    result = {}
    for paragraph in root.all({"p"}):
        titles = [" ".join(n.text().split()) for n in paragraph.children if isinstance(n,a.Node)
                  and n.tag == "font" and n.attrs.get("size") == "+1"]
        if len(titles) != 1:
            continue
        source_title = titles[0]
        for link in paragraph.all({"a"}):
            target = a.resolve(page,link.attrs.get("href", ""))
            if not target:
                continue
            raw = a.original(target);parts = urlsplit(raw)
            if parts.hostname != "d1.alicefree.fastlast.org" or not parts.path.startswith("/d/") or not a.EXT.search(parts.path):
                continue
            filename = unquote(Path(parts.path).name)
            label = " ".join(link.text().split())
            title = source_title
            # The final paragraph contains three different original distributions.
            if "/" in source_title:
                title = re.sub(r"\((?:RAR|ZIP|※\d+)\)","",label).strip()
            book = "ブックレット" in title
            if book:
                title = "鬼畜王ランス"
            part = re.search(r"\.part(\d+)\.rar$", filename, re.I)
            fields = {"distribution_kind":"other" if book else "full", "source_title":source_title,
                "download_label":label,"license_url":"https://www.alicesoft.com/about/#cont08",
                "requirements":" ".join(paragraph.text().split()),
                "archive_format":Path(parts.path).suffix.lower().lstrip(".")}
            if part:
                fields["part_number"] = int(part[1])
                fields["part_set"] = re.sub(r"\.part\d+\.rar$","",filename,flags=re.I)
            ident = hashlib.sha256(("alicefree\n"+original+"\n"+parts.path).encode()).hexdigest()
            stamp = a.WAYBACK.match(target) or a.WAYBACK.match(page)
            result[ident] = {"id":ident,"site":"alicefree","page_url":page,"original_page_url":original,
                "title":title,"brand":"アリスソフト","release_name":("資料 ブックレット " if book else "本編 ")+label,
                "filename":filename,"mirrors":([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw],
                "expected_md5":None,"expected_size":None,"size_text":None,"published_at":None,"fields":fields}
    return list(result.values())


def parse_getchu(html, page):
    parts=urlsplit(a.original(page))
    if parts.hostname not in {"www.getchu.com","getchu.com"} or not (parts.path.endswith("/soft.phtml") or re.fullmatch(r"/item/\d+/?",parts.path)):
        return []
    root=a.DOM(html).root
    titles=[]
    for node in root.all({'h1','h2'}):
        if node.attrs.get('id')!='soft-title':continue
        title=re.sub(r'\s*[（(]このタイトルの関連商品[)）]\s*$','',node.text()).strip()
        titles.append(re.sub(r"\s+\[同人\]$", "",title))
    og=[n.attrs.get("content","") for n in root.all({"meta"}) if n.attrs.get("property")=="og:title"]
    if len(og)!=1:return []
    brand=re.fullmatch(re.escape(titles[0])+r"\s*[(（](.+)[)）]",og[0]) if len(titles)==1 else None
    brand_name=brand[1] if brand else None
    if not brand_name:
        modern=re.fullmatch(r"(.+?)\s+\|\s+(.+)",og[0])
        document_titles=[n.text().strip() for n in root.all({'title'})]
        if not modern or len(document_titles)!=1:return []
        title,brand_name=(v.strip() for v in modern.groups())
        if titles and titles!=[title]:return []
        if not re.match(re.escape(title)+r"\s*[(（]"+re.escape(brand_name)+r"[)）]\s*\|",document_titles[0]):return []
        titles=[title]
    product_id=(parse_qs(parts.query).get('id') or [parts.path.strip('/').split('/')[-1]])[0]
    result={}
    for node in root.all({"video","source"}):
        parent=node
        while parent.parent and parent.tag!="video" and node.tag=="source":parent=parent.parent
        if parent.tag!="video":continue
        target=a.resolve(page,node.attrs.get("src", ""))
        if not target:continue
        raw=a.original(target);binary=urlsplit(raw)
        if binary.hostname!="trial.getchu.com" or not re.search(r"\.(mp4|wmv|mpg|mpeg|webm)$",binary.path,re.I):continue
        if not product_id.isdigit() or (product_id not in binary.path.split('/') and Path(binary.path).stem!=product_id):continue
        label=parent.attrs.get("getchutitle", "")
        filename=unquote(Path(binary.path).name)
        ident=hashlib.sha256(("getchu\n"+a.original(page)+"\n"+binary.path).encode()).hexdigest()
        stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
        result[ident]={"id":ident,"site":"getchu","page_url":page,"original_page_url":a.original(page),
            "title":titles[0],"brand":brand_name,"release_name":"ムービー","filename":filename,
            "mirrors":([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw],
            "expected_md5":None,"expected_size":None,"size_text":None,"published_at":None,
            "fields":{"published_media_label":label,"getchu_product_url":a.original(page)}}
    from mirror_sources import definitions
    restricted={name.casefold() for name in definitions()['getchu'].get('checksum_required_filenames',[])}
    for node in root.all({'a'}):
        target=a.resolve(page,node.attrs.get('href',''))
        if not target:continue
        raw=a.original(target);binary=urlsplit(raw)
        if binary.hostname!='trial.getchu.com' or not a.EXT.search(binary.path):continue
        if not product_id.isdigit() or product_id not in binary.path.split('/'):continue
        filename=unquote(Path(binary.path).name)
        # The separate checksum-linked importer handles the publisher's listed
        # compromised filenames; a labelled button alone cannot identify a build.
        if filename.casefold() in restricted:continue
        labels=[node.text()]+[image.attrs.get('alt','') for image in node.all({'img'})]
        buttons={Path(urlsplit(a.original(image.attrs.get('src',''))).path).name for image in node.all({'img'})}
        trial_button='demo_t.gif' in buttons;movie_button='demo_m.gif' in buttons
        label=' '.join(labels).strip()
        if trial_button or '体験版' in label:release='体験版'
        elif '修正' in label or 'パッチ' in label:release='修正パッチ'
        elif movie_button or 'ムービー' in label or '動画' in label:release='ムービー'
        else:continue
        ident=hashlib.sha256(('getchu\n'+a.original(page)+'\n'+binary.path).encode()).hexdigest()
        stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
        result[ident]={'id':ident,'site':'getchu','page_url':page,'original_page_url':a.original(page),
            'title':titles[0],'brand':brand_name,'release_name':release,'filename':filename,
            'mirrors':([a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme)] if stamp else [])+[raw],
            'expected_md5':None,'expected_size':None,'size_text':None,'published_at':None,
            'fields':{'published_media_label':label,'published_button':'demo_t.gif' if trial_button else ('demo_m.gif' if movie_button else None),'getchu_product_url':a.original(page)}}
    return list(result.values())


def parse_xgame(html,page):
    parts=urlsplit(a.original(page))
    if parts.hostname not in {'www.xgamedata.com','xgamedata.com'} or not re.fullmatch(r'/demo\d+/g\d+/',parts.path):return []
    root=a.DOM(html).root
    brands=set()
    links=[]
    for node in root.all({'a'}):
        target=a.resolve(page,node.attrs.get('href',''))
        if not target:continue
        raw=a.original(target);link=urlsplit(raw)
        fragment=urlsplit(a.original(node.attrs.get('href',''))).fragment
        if 'このブランド' in node.text() and link.hostname in {'www.xgamedata.com','xgamedata.com'} and fragment:
            brands.add(unicodedata.normalize('NFKC',unquote(fragment)).strip())
        if a.EXT.search(link.path):links.append(target)
    evidence={}
    if not brands:
        # Some later XES pages removed all brand-navigation hyperlinks. Require
        # agreement between two explicit publisher credits rather than infer a
        # brand from a filename or a possibly unrelated ownership notice.
        text=unicodedata.normalize('NFKC',root.text())
        key=lambda name:re.sub(r'\s+','',name).casefold()
        owners=re.findall(r'(?:Copyright\s*)?(?:©|\(C\))\s*(?:\d{4}(?:\s*[-–]\s*\d{4})?\s*)?(.{1,160}?)\s+All\s+Rights\s+Reserved',text,re.I|re.S)
        credits=re.findall(r'※この素材は、\s*(.{1,160}?)\s*様より転載しております',text,re.S)
        publishers=[re.sub(r'\s+(?:Official\s+Home\s*Page|Official\s+Web\s*Site|on\s+WEB|公式(?:サイト|ホームページ))\s*$','',credit,flags=re.I).strip() for credit in credits]
        matches={key(owner):(owner.strip(),publisher) for owner in owners for publisher in publishers if key(owner)==key(publisher)}
        if len(matches)==1:
            owner,publisher=next(iter(matches.values()));brands.add(owner)
            evidence={'method':'copyright_and_attribution','copyright_name':owner,'attribution_name':publisher}
    if len(brands)!=1:return []
    brand=brands.pop();result=[]
    if not evidence:evidence={'method':'brand_navigation','published_name':brand}
    sections=re.split(r'(?=【\s*(?:素\s*材|ソ\s*フ\s*ト)\s*名\s*】)',root.text())
    for section in sections[1:]:
        fields={re.sub(r'\s+','',unicodedata.normalize('NFKC',key)):value.strip().splitlines()[0].strip()
                for key,value in re.findall(r'【([^】]+)】\s*(.*?)(?=【|$)',section,re.S) if value.strip()}
        legacy='ソフト名' in fields
        label=fields.get('素材名') or fields.get('ソフト名','')
        title=re.fullmatch(r'『(.+?)』\s*(.+)',label)
        size=fields.get('ファイルサイズ/MD5','');checksum=re.search(r'MD5\s*[:：]\s*([a-fA-F0-9]{32})(?![a-fA-F0-9])',size,re.I)
        names=[name.strip() for name in fields.get('登録名','').split('/')]
        sizes=[]
        if legacy:
            # Older XES readmes publish exact byte counts, sometimes parallel
            # slash-separated filename/size/quality lists. Bind by position only
            # when every count is explicit and the lists have equal lengths.
            byte_values=fields.get('バイト数','').split('/')
            if len(byte_values)!=len(names):continue
            for value in byte_values:
                match=re.fullmatch(r'\s*([0-9][0-9,]*)\s*(?:bytes?|バイト)\s*',unicodedata.normalize('NFKC',value),re.I)
                if not match:break
                sizes.append(int(match[1].replace(',','')))
            if len(sizes)!=len(names) or any(size<=0 for size in sizes):continue
        elif not checksum or len(names)!=1:continue
        if not title or any(not name or not a.EXT.search(name) for name in names):continue
        releases=title[2].split('/')
        for index,filename in enumerate(names):
            candidates=[target for target in links if unquote(Path(urlsplit(a.original(target)).path).name)==filename]
            mirrors=[]
            for target in candidates:
                raw=a.original(target);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
                if stamp:mirrors.append(a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme))
                mirrors.append(raw)
            ident=hashlib.sha256(('xgame\n'+a.original(page)+'\n'+filename).encode()).hexdigest()
            result.append({'id':ident,'site':'xgame','page_url':page,'original_page_url':a.original(page),
                'title':title[1],'brand':brand,'release_name':releases[index].strip() if len(releases)==len(names) else title[2],'filename':filename,
                'expected_md5':checksum[1].lower() if checksum else None,'expected_size':sizes[index] if legacy else None,
                'size_text':str(sizes[index])+' bytes' if legacy else size.split('/')[0].strip(),
                'published_at':None,'mirrors':list(dict.fromkeys(mirrors)),
                'fields':{'published_distribution_fields':fields,'動作条件':fields.get('動作条件'),
                    '適用条件':fields.get('動作条件'),'転載条件':fields.get('転載条件'),
                    'brand_evidence':evidence,'source_link_missing':not bool(candidates)}})
    return result


def parse_redistribution(html,page):
    if urlsplit(a.original(page)).hostname not in {'www.redistribution.cc','redistribution.cc'}:return []
    root=a.DOM(html).root;result=[]
    for banner in root.all({'div'}):
        if 'banner' not in banner.attrs.get('class','').split():continue
        brand=title=None
        for node in banner.children:
            if not isinstance(node,a.Node):continue
            if node.tag=='div' and 'banner_date' in node.attrs.get('class','').split():
                if brand is None:brand=node.text().strip()
                else:title=node.text().strip()
                continue
            if node.tag!='table' or not brand or not title:continue
            fields=a.labels(node)
            checksum=fields.get('md5','').strip()
            if not fields.get('タイトル') or not re.fullmatch(r'[a-fA-F0-9]{32}',checksum):continue
            candidates=[]
            for link in node.all({'a'}):
                target=a.resolve(page,link.attrs.get('href',''))
                if not target:continue
                parts=urlsplit(a.original(target))
                if parts.hostname in {'www.redistribution.cc','redistribution.cc'} and parts.path.startswith('/contents/') and a.EXT.search(parts.path):candidates.append(target)
            names={unquote(Path(urlsplit(a.original(u)).path).name) for u in candidates}
            if not candidates:
                filename=fields.get('ダウンロード','').strip()
                if not a.EXT.search(filename) or '/' in filename:continue
                names={filename}
            if len(names)!=1:continue
            filename=names.pop();mirrors=[]
            for target in candidates:
                raw=a.original(target);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
                if stamp:mirrors.append(a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme))
                mirrors.append(raw)
            published=fields.get('公開日(当サイト)');date=re.fullmatch(r'(\d{4})年(\d{1,2})月(\d{1,2})日',published or '')
            if date:published=f'{int(date[1]):04}-{int(date[2]):02}-{int(date[3]):02}'
            ident=hashlib.sha256(('redistribution\n'+a.original(page)+'\n'+filename).encode()).hexdigest()
            result.append({'id':ident,'site':'redistribution','page_url':page,'original_page_url':a.original(page),
                'title':title,'brand':brand,'release_name':fields['タイトル'],'filename':filename,
                'expected_md5':checksum.lower(),'expected_size':None,'size_text':fields.get('ファイルサイズ'),
                'published_at':published,'mirrors':list(dict.fromkeys(mirrors)),
                'fields':{'published_distribution_fields':fields,'配布終了日':fields.get('公開終了日(当サイト)'),
                    '備考':fields.get('備考'),'source_link_missing':not bool(candidates)}})
    return result


def parse_magic_mirror(html,page):
    parts=urlsplit(a.original(page))
    if parts.hostname!='magics.ddo.jp' or not parts.path.startswith('/info/'):return []
    root=a.DOM(html).root;panels=[n for n in root.all({'div'}) if n.attrs.get('id')=='right']
    if len(panels)!=1:return []
    fields={};sections={};label=None
    for node in panels[0].children:
        if not isinstance(node,a.Node):continue
        if node.tag=='h2':
            label=node.text().strip()
            if label=='コメント':break
        elif node.tag=='p' and label:
            fields[label]=node.text().strip();sections[label]=node
    if not all(fields.get(key) for key in ['素材タイトル','ブランド','作品']):return []
    md5=fields.get('MD5','').strip()
    if md5 and not re.fullmatch(r'[a-fA-F0-9]{32}',md5):return []
    mirrors=[];filenames=set();block=sections.get('ダウンロード')
    for link in block.all({'a'}) if block else []:
        target=a.resolve(page,link.attrs.get('href',''))
        if not target or not a.EXT.search(urlsplit(a.original(target)).path):continue
        raw=a.original(target);filenames.add(unquote(Path(urlsplit(raw).path).name))
        stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
        if stamp:mirrors.append(a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme))
        mirrors.append(raw)
    if len(filenames)>1 or not (md5 or mirrors):return []
    date=re.search(r'\d{4}/\d{2}/\d{2}',fields.get('公開開始日(当サイト)',''))
    filename=next(iter(filenames),None)
    official=sections.get('公式サイト')
    official_urls=[a.original(target) for node in official.all({'a'}) if (target:=a.resolve(page,node.attrs.get('href','')))] if official else []
    ident=hashlib.sha256(('magic_mirror\n'+a.original(page)).encode()).hexdigest()
    return [{'id':ident,'site':'magic_mirror','page_url':page,'original_page_url':a.original(page),
        'title':fields['作品'],'brand':fields['ブランド'],'release_name':fields['素材タイトル'],
        'filename':filename,'expected_md5':md5.lower() or None,'expected_size':None,
        'size_text':fields.get('ダウンロード'),'published_at':date[0].replace('/','-') if date else None,
        'mirrors':list(dict.fromkeys(mirrors)),
        'fields':{'published_distribution_fields':fields,'published_filename_missing':filename is None,
            'copyright':fields.get('著作権'),'official_site_urls':list(dict.fromkeys(official_urls)),
            'holyseal_reference':fields.get('Holyseal情報')}}]


def _fuzzy2_file_sections(block,page):
    # A paragraph can contain a trial followed by its separately linked patch.
    # A bold, literal filename starts a new scope for text, hashes and links.
    sections=[]
    def visit(node):
        if isinstance(node,str):
            if sections:sections[-1]['text'].append(node)
            return
        name=node.text().strip() if node.tag=='b' else ''
        extension=a.EXT.search(name)
        if name and '/' not in name and extension and extension.end()==len(name):
            sections.append({'filename':name,'text':[],'links':[]})
            return
        if node.tag=='a' and sections:
            target=a.resolve(page,node.attrs.get('href',''))
            if target and a.EXT.search(urlsplit(a.original(target)).path):sections[-1]['links'].append(target)
        if node.tag=='br' and sections:sections[-1]['text'].append(' ')
        for child in node.children:visit(child)
    visit(block)
    return sections


def parse_fuzzy2(html,page):
    parts=urlsplit(a.original(page))
    if parts.hostname!='mirror.fuzzy2.com' or parts.path not in {'/distributes_jp/main.html','/jdm/main.html'}:return []
    root=a.DOM(html).root;title=brand=date=None;credits=[];results=[]
    for block in root.all({'p'}):
        text=" ".join(block.text().split())
        headings=[n for n in block.children if isinstance(n,a.Node) and n.tag=='font'
            and n.attrs.get('size') in {'4','5'} and any(b.text().strip() for b in n.all({'b'}))]
        stamp=re.match(r'(\d{4})\.(\d{1,2})\.(\d{1,2})\b',text)
        if headings or stamp:
            title=brand=date=None;credits=[]
            credits=re.findall(r'©\s*([^©]+?)\s+https?://',text)
            if stamp and credits and len(headings)==1:
                title=' '.join(headings[0].text().split());brand=credits[0].strip()
                date=f'{stamp[1]}-{int(stamp[2]):02}-{int(stamp[3]):02}'
            continue
        if not title or not brand:continue
        for section in _fuzzy2_file_sections(block,page):
            filename=section['filename']
            links=[u for u in section['links'] if unquote(Path(urlsplit(a.original(u)).path).name)==filename]
            mismatched=[u for u in section['links'] if u not in links]
            if not links:continue
            detail=' '.join(''.join(section['text']).split())
            hashes=set(v.lower() for v in re.findall(r'MD5\s*[:：]\s*([a-fA-F0-9]{32})',detail))
            if len(hashes)>1:continue
            size=re.search(r'([\d.]+\s*[KMGT]B)',detail,re.I)
            release=detail.split('(',1)[0].strip()
            if not release:release=re.split(r'[,，]',detail.lstrip('('),maxsplit=1)[0].strip()
            if not release:continue
            mirrors=[]
            for target in links:
                raw=a.original(target);stamp=a.WAYBACK.match(target) or a.WAYBACK.match(page)
                if stamp:mirrors.append(a.replay(raw,stamp[1],raw=True,scheme=urlsplit(stamp[0]).scheme))
                mirrors.append(raw)
            # Keep hashless releases on different published dates distinct.
            identity='fuzzy2\n'+a.original(page)+'\n'+filename+('\n'+date if not hashes else '')
            ident=hashlib.sha256(identity.encode()).hexdigest()
            results.append({'id':ident,'site':'fuzzy2','page_url':page,'original_page_url':a.original(page),
                'title':title,'brand':brand,'release_name':release,'filename':filename,
                'expected_md5':next(iter(hashes),None),'expected_size':None,'size_text':size[1] if size else None,
                'published_at':date,'mirrors':list(dict.fromkeys(mirrors)),
                'fields':{'parser':'fuzzy2-catalogue','published_heading':title,'size_is_rounded':True,
                    'publisher_credits':credits,'published_checksum_missing':not bool(hashes),
                    'mismatched_file_links':mismatched}})
    return list({r['id']:r for r in results}.values())
