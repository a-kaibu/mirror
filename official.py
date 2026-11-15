"""Collect publisher-hosted distributions using ErogameScape catalog seeds."""
import argparse
from collections import deque
import hashlib
import http.client
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import archiver as a
from catalog import release_info, normalized, KINDS
import erogamescape as e

BINARY = re.compile(r"\.(?:zip|exe|7z|rar|lzh|cab|msi|xp3|mp4|wmv|mpg|mpeg)(?:$|[?#])",re.I)
NAVIGATION = re.compile(r"download|trial|taiken|special|support|patch|体験|ダウンロード|だうんろーど|修正|アップデート|アップデータ|サポート",re.I)
SUPPORT_NAVIGATION = re.compile(r"support|patch|修正|アップデート|アップデータ|サポート",re.I)
ASSET_LABEL = re.compile(r"フライヤー|チラシ|パンフレット|アートワーク|バナー|壁紙|アイコン|ポスター|説明書|マニュアル|楽譜|歌詞|セーブ|banner|wallpaper|flyer|manual|artwork|pamphlet",re.I)


def initialize(store):
    e.initialize(store)
    store.db.execute("""CREATE TABLE IF NOT EXISTS official_scans(game_id INTEGER PRIMARY KEY,
        seed_url TEXT,status TEXT NOT NULL,found INTEGER NOT NULL DEFAULT 0,error TEXT,updated_at TEXT NOT NULL)""")
    store.db.execute("""CREATE TABLE IF NOT EXISTS official_page_visits(game_id INTEGER,url TEXT,
        checked_at TEXT,status TEXT,error TEXT,PRIMARY KEY(game_id,url))""")
    store.db.commit()


def binary_links(node, page):
    result=[]
    for link in node.all({"a","video","source"}):
        target=a.resolve(page,link.attrs.get("href") or link.attrs.get("src",""))
        if target and BINARY.search(urlsplit(target).path):result.append((link,target))
    return result


def labelled_product(node,page):
    targets=[]
    for link in node.all({'a'}):
        siblings=link.parent.children;position=siblings.index(link)
        prefix=''.join(n.text() if isinstance(n,a.Node) else n for n in siblings[max(0,position-3):position])
        if not re.search(r'タイトル名\s*[:：]?\s*$',prefix):continue
        target=a.resolve(page,link.attrs.get('href',''))
        if target:targets.append(a.canonical(a.original(target)))
    return next(iter(set(targets))) if len(set(targets))==1 else None


def publisher_patch_scope(node,brand):
    scopes=set()
    for label in node.all({'b','strong'}):
        if label.text().strip(' :：')!='タイトル名':continue
        siblings=label.parent.children;parts=[]
        for sibling in siblings[siblings.index(label)+1:]:
            if isinstance(sibling,a.Node):break
            parts.append(sibling)
        value=''.join(parts).strip(' :：')
        if re.fullmatch(re.escape(brand['brandname'])+r'タイトル全般【[^【】\r\n]{1,160}】',value):scopes.add(value)
    return next(iter(scopes)) if len(scopes)==1 else None


def appetite_kind(html,page,game,target,requested_page=None):
    parts=urlsplit(page);binary=urlsplit(target);requested=requested_page or page
    hosts={'www.appetite-game.com','appetite-game.com'}
    if parts.hostname not in hosts or binary.hostname!=parts.hostname or urlsplit(requested).hostname not in hosts:return None
    if not game.get('shoukai') or a.canonical(requested)!=a.canonical(game['shoukai']):return None
    def product_code(path,alias=False):
        suffix=r'(?:_kari)?' if alias else ''
        match=re.fullmatch(r'/(apt)_(\d+)'+suffix+r'\.html|/apadash/(aptd)_(\d+)'+suffix+r'\.html',path,re.I)
        if not match:return None
        return ((match[1] or match[3]).lower(),match[2] or match[4])
    product=product_code(parts.path)
    if not product or product_code(urlsplit(requested).path,alias=True)!=product:return None
    family,number=product;base='/apadash/' if family=='aptd' else '/'
    folder=base+family+'_'+number+'/'
    nested=r'(?:apt'+re.escape(number)+r'_dl/)?' if family=='apt' else ''
    asset=re.fullmatch(re.escape(folder)+nested+family+re.escape(number)+r'_(tr|tri(?:_exe)?|pv)\.(zip|mp4)',binary.path,re.I)
    if not asset:return None
    trial=asset[1].lower()!='pv'
    if trial and asset[2].lower()!='zip':return None
    labels=['体験版','CLOSER|TRIAL'] if trial else ['PV','CLOSER|PV']
    if not any(re.search(r'<!--\s*'+re.escape(label)+r'\s*-->',html,re.I) for label in labels):return None
    return 'trial' if trial else 'movie'


def group_heading(node,link,page):
    """Find the preceding section heading without borrowing sibling hashes."""
    branch=link
    while branch.parent is not None and branch.parent is not node:branch=branch.parent
    if branch.parent is not node:return None
    heading=None
    for child in node.children:
        if child is branch:break
        if not isinstance(child,a.Node) or child.tag not in {'h1','h2','h3','h4','h5','h6','table'}:continue
        text=child.text().strip()
        if not text or len(text)>80 or binary_links(child,page):continue
        kind=release_info({'title':text,'filename':''})[0]
        heading=(kind,text) if kind in {'trial','patch','movie'} else None
    return heading


def product_title_present(game,text):
    # Publishers vary spacing around punctuation; retain every other character
    # so short names and translated titles do not become fuzzy associations.
    compact=lambda value:re.sub(r"\s+","",normalized(value))
    title=compact(game["gamename"])
    if not title:return False
    content=compact(text)
    if len(title)>4:return title in content
    if re.fullmatch(r'[a-z0-9]+',title):
        return bool(re.search(r'(?<![a-z0-9])'+re.escape(title)+r'(?![a-z0-9])',normalized(text)))
    # Short Japanese titles can also be ordinary prose: 「あると」 occurs in
    # 「異常であるという警告」. Require an explicit title or release label.
    return (content==title or bool(re.search(r'[「『【《〈“"]'+re.escape(title)+r'[」』】》〉”"]',content))
        or bool(re.search(r'(?:^|[。.!！?？:：])'+re.escape(title)+r'(?:の)?(?:体験版|修正|パッチ|ダウンロード|アップデート|アップデータ|サポート)',content)))



def product_identity_text(node):
    """Exclude publisher navigation and sidebars from local product evidence."""
    parts=[];pending=[node]
    while pending:
        current=pending.pop()
        if not isinstance(current,a.Node):parts.append(current);continue
        marker=' '.join(current.attrs.get(k,'') or '' for k in ('id','class')).lower()
        if (current.tag in {'nav','aside','header','footer','script','style'}
            or current.attrs.get('role') in {'navigation','complementary'}
            or re.search(r'(?:^|[ _-])(?:nav|navigation|menu|quicklink|sidebar|supplement)(?:[ _-]|$)',marker)
            or any(token.startswith(('quicklink','supplement','sidebar')) for token in marker.split())):continue
        if current.tag=='img':parts.append(current.attrs.get('alt') or '')
        pending.extend(reversed(current.children))
    return ' '.join(parts)


def product_folder_scoped(page,game,brand,shared_home=False):
    """A publisher directory or reused metadata home is not a product scope."""
    home=urlsplit(game.get('shoukai') or '')
    folder=home.path if home.path.endswith('/') else home.path.rsplit('/',1)[0]+'/'
    if shared_home or folder in {'','/'}:return False
    publisher=urlsplit(brand.get('url') or '')
    landing=lambda path:re.sub(r'/(?:index|default)\.(?:html?|php|asp)$','/',path,flags=re.I).rstrip('/')
    if home.hostname==publisher.hostname and landing(home.path)==landing(publisher.path):return False
    return urlsplit(page).path.startswith(folder)



def alicesoft_support_details(link,page,brand):
    """Keep explicit expansion/append targets and conditions on support pages."""
    parts=urlsplit(page)
    if (parts.hostname not in {'www.alicesoft.com','alicesoft.com'} or not parts.path.startswith('/support/')
        or normalized(brand.get('brandname',''))!='alicesoft'):return None
    nodes=[];node=link.parent;section=None
    while node is not None:
        nodes.append(node)
        if node.tag=='section' and 'support-product' in node.attrs.get('class','').split():section=node;break
        node=node.parent
    if section is None:return None
    headings=[n.text().strip() for n in section.children if isinstance(n,a.Node) and n.tag=='h2']
    if len(headings)!=1 or not headings[0]:return {'ambiguous':True}
    target=headings[0];context=None
    for node in nodes:
        paths={urlsplit(a.original(url)).path for _,url in binary_links(node,page)}
        if len(paths)!=1:break
        text=product_identity_text(node)
        declarations=set(re.findall(r'["「『]([^"」』\r\n]{1,160})["」』](?:の更新を行うプログラムです|用のファイルです)',text))
        if len(declarations)>1:return {'ambiguous':True}
        if declarations:
            target=next(iter(declarations));context=node;break
        if context is None and (node.tag=='dd' or node.tag=='div' and 'cont01' in node.attrs.get('class','').split()):context=node
    if context is None:return {'ambiguous':True}
    versions=set()
    for heading in context.all({'h3','h4','h5','h6'}):
        versions.update(re.findall(r'アップデー(?:タ|ト)\s*Ver\.?\s*([0-9]+(?:\.[0-9]+)*[a-z]?)(?![a-z0-9_.])',heading.text(),re.I))
    conditions=[]
    for paragraph in context.all({'p'}):
        text=paragraph.text().strip()
        if any(token in text for token in ('適用できません','には関係ありません','用のファイルです')):
            conditions.append(text)
    return dict(title=target,version=next(iter(versions)) if len(versions)==1 else None,conditions=list(dict.fromkeys(conditions)))



def waffle_support_details(link,page,brand):
    """Use the product heading on Waffle's shared publisher patch cards."""
    parts=urlsplit(page)
    if (parts.hostname not in {'www.waffle1999.com','waffle1999.com'}
            or parts.path!='/official/patch.html' or normalized(brand.get('brandname',''))!='waffle'):
        return None
    node=link.parent
    while node is not None:
        if node.tag=='div' and 'article' in node.attrs.get('class','').split():
            headings=[child.text().strip() for child in node.children if isinstance(child,a.Node) and child.tag=='h5']
            if len(headings)!=1:return {'ambiguous':True}
            title=re.sub(r'\s*修正パッチ\s*$','',headings[0]).strip()
            if not title:return {'ambiguous':True}
            return dict(title=title,version=None,conditions=[],patch=bool(re.search(r'パッチ|修正|バージョンアップ|アップデー(?:ト|タ)',node.text())))
        node=node.parent
    return None


def parse(html, page, game, brand, scoped=True, hint=None, requested_page=None):
    root=a.DOM(html).root
    records={}
    unsupported=[]
    filenames={a.filename_from_url(u) for _,u in binary_links(root,page)}
    for link,target in binary_links(root,page):
        filename=a.filename_from_url(target)
        own_label=' '.join([link.text(),link.attrs.get('title') or '',*[image.attrs.get('alt') or '' for image in link.all({'img'})]])
        # A shared trial layout or trial-page hint cannot turn an explicitly
        # labelled score/manual/wallpaper archive into a game distribution.
        if ASSET_LABEL.search(own_label) and release_info({'title':own_label,'filename':''})[0]=='other':continue
        def describe(n):
            images=list(n.all({"img"}))
            marker=" ".join((element.attrs.get(key) or "") for element in [n,*images] for key in ["id","class"])
            marker=re.sub(r'(?<![a-z])trial[_-]table(?:[_-]style[0-9]*)?(?![a-z0-9])','',marker,flags=re.I)
            # Container classes are often reused for unrelated layouts (for
            # example op_movie_t around a patch). New image hints belong only
            # to this link; retain the existing trial-container interpretation.
            own_marker=" ".join((element.attrs.get(key) or "") for element in [link,*link.all({"img"})] for key in ["id","class"])
            categories={"trial":"体験版","movie":"ムービー","patch":"パッチ"}
            labels=" ".join(label for token,label in categories.items() if re.search(r"(?<![a-z])"+token+r"(?![a-z])",marker if token=="trial" else own_marker,re.I))
            return " ".join([n.text(),n.attrs.get("title") or "",*[img.attrs.get("alt") or "" for img in images],labels])
        context=describe(link);context_node=link;node=link.parent
        group=None;metadata_context=None
        while node is not None and node.tag not in {"body","html","root"}:
            text=describe(node)
            node_binaries=binary_links(node,page)
            names={a.filename_from_url(u) for _,u in node_binaries}
            paths={urlsplit(a.original(u)).path for _,u in node_binaries}
            if len(text)>6000:break
            if not scoped and node.tag=="section":
                headings=[child.text() for child in node.children if isinstance(child,a.Node) and child.tag in {"h1","h2"}]
                if headings and not any(product_title_present(game,title) for title in headings):
                    # A shared support menu may name the requested game above
                    # another product's section. Never climb past that boundary.
                    break
            if len(names)>1 or (not scoped and len(paths)>1):
                if release_info({'title':context+' '+filename,'filename':filename})[0]=='other':
                    group=group_heading(node,link,page)
                if group:
                    metadata_context=context
                    context=text;context_node=node
                break
            context=text;context_node=node
            if re.search(r"体験|trial|taiken|パッチ|patch|修正|アップデート|アップデータ|update|ムービー|movie|動画|(?:^|\W)PV(?:$|\W)",text,re.I):
                if scoped or product_title_present(game,product_identity_text(node)) or labelled_product(node,page)==a.canonical(game.get('shoukai') or '') or publisher_patch_scope(node,brand):break
            node=node.parent
        label=context+" "+filename
        kind=release_info({"title":label,"filename":filename})[0] if group is None else group[0]
        waffle_support=waffle_support_details(link,page,brand)
        if waffle_support and waffle_support.get('patch') and kind in {'other','trial'}:kind='patch'
        if kind=="other" and ASSET_LABEL.search(label):
            continue  # Publisher scan targets trials, patches and movies.
        if kind=="other" and hint in KINDS and hint!="other" and len(filenames)==1:
            kind=hint
        published_kind=appetite_kind(html,page,game,target,requested_page)
        if kind=='other' and published_kind:kind=published_kind
        product_link=a.canonical(page) if published_kind else labelled_product(context_node,page)
        linked=bool(published_kind or (game.get('shoukai') and product_link==a.canonical(game['shoukai'])))
        common_scope=publisher_patch_scope(context_node,brand) if kind=='patch' else None
        support=(alicesoft_support_details(link,page,brand) if kind=='patch' else None) or waffle_support
        if support and support.get('ambiguous'):
            unsupported.append(target);continue
        if kind=="other" or (not scoped and not product_title_present(game,product_identity_text(context_node)) and not linked and not common_scope and not support):
            unsupported.append(target);continue
        key=hashlib.sha256(("official\n"+page+"\n"+filename).encode()).hexdigest()
        if key not in records:
            measured_context=context if metadata_context is None else metadata_context
            md5s=re.findall(r"MD5\s*[:：]?\s*([a-f0-9]{32})",measured_context,re.I)
            exact=re.findall(r"([0-9][0-9,]*)\s*bytes",measured_context,re.I)
            version=re.search(r"(?:ver(?:sion)?[._ -]*|v)(\d+(?:[._]\d+)*[a-z]?)(?![a-z0-9_]|\.[0-9])",filename,re.I)
            records[key]={"id":key,"site":"official","page_url":page,"original_page_url":page,
                "title":game["gamename"],"brand":brand["brandname"],"release_name":KINDS[kind]+(" ver"+version[1].replace("_", ".") if version else ""),
                "filename":filename,"mirrors":[],"expected_md5":md5s[0].lower() if len(set(md5s))==1 else None,
                "expected_size":int(exact[0].replace(",","")) if len(set(exact))==1 else None,
                "size_text":None,"published_at":None,"fields":{"erogamescape_game_id":game["id"],"erogamescape_brand_id":brand["id"],"official_page":page}}
        if support:
            records[key]['title']=support['title']
            records[key]['fields']['publisher_product_title']=support['title']
            compact=lambda text:re.sub(r'\s+','',normalized(text))
            if compact(game['gamename'])!=compact(support['title']):records[key]['fields'].pop('erogamescape_game_id',None)
            if support['version']:
                records[key]['release_name']=KINDS[kind]+' ver'+support['version']
                records[key]['fields']['バージョン']=support['version']
            if support['conditions']:records[key]['fields']['適用条件']='\n'.join(support['conditions'])
        if group:records[key]['fields']['official_group_heading']=group[1]
        if common_scope:
            records[key]['title']=common_scope
            records[key]['fields'].pop('erogamescape_game_id',None)
            records[key]['fields'].update(distribution_scope=common_scope,scope_kind='publisher')
        if linked:records[key]['fields']['publisher_product_link']=product_link
        if published_kind:records[key]['fields']['published_section_label']='体験版' if published_kind=='trial' else 'PV'
        if published_kind and requested_page and requested_page!=page:records[key]['fields']['official_request_url']=requested_page
        records[key]["mirrors"].append(target)
    for record in records.values():record["mirrors"]=list(dict.fromkeys(record["mirrors"]))
    return list(records.values()),unsupported


def seeds(store):
    brands={row[0]:json.loads(row[1]) for row in store.db.execute("SELECT external_id,data FROM external_metadata WHERE provider='erogamescape' AND entity='brand'")}
    eligible={row[0] for row in store.db.execute("SELECT external_id FROM metadata_links WHERE entity='brand'")}
    ids=[row[0] for row in store.db.execute("SELECT external_id FROM external_metadata WHERE provider='erogamescape' AND entity='game' ORDER BY CASE WHEN length(coalesce(json_extract(data,'$.trial_url'),''))>0 THEN 0 ELSE 1 END,external_id DESC")]
    # No payload list or live read cursor survives a yield into a network scan.
    for ident in ids:
        row=store.db.execute("SELECT data FROM external_metadata WHERE provider='erogamescape' AND entity='game' AND external_id=?",(ident,)).fetchone()
        if not row:continue
        game=json.loads(row[0]);brand=brands.get(game["brandname"])
        if not brand or brand["id"] not in eligible:continue
        home=game.get("shoukai") or brand.get("url")
        if not home:continue
        allowed={urlsplit(u).hostname for u in [home,brand.get("url") or home]}
        trial=game.get("trial_url")
        # trial_url may be a third-party mirror; do not label that publisher-hosted.
        seed=trial if trial and urlsplit(trial).hostname in allowed else game.get("shoukai")
        if not seed or urlsplit(seed).scheme not in {"http","https"}:continue
        yield game,brand,a.canonical(seed)


def scan(store, fetcher, game, brand, seed, max_pages=20, continuation=None):
    initialize(store)
    if continuation and continuation.get('seed')!=seed:raise ValueError('official continuation seed changed')
    queue=deque((entry['url'],entry['depth']) for entry in continuation['pending']) if continuation else deque([(seed,0)])
    home=game.get('shoukai');trial=game.get('trial_url')
    if not continuation and home and trial and a.canonical(trial)==seed and urlsplit(home).scheme in {'http','https'}:
        home=a.canonical(home)
        if home!=seed:queue.append((home,0))
    seen=set(continuation.get('seen',[])) if continuation else set()
    # Multiple catalogue games can point at a brand homepage or a reused URL.
    # Evaluate once per scan; every linked support page then requires its own
    # product evidence instead of inheriting the whole directory.
    shared_home=bool(game.get('shoukai') and store.db.execute("""SELECT 1 FROM external_metadata
        WHERE provider='erogamescape' AND entity='game' AND external_id!=?
        AND json_extract(data,'$.shoukai')=? LIMIT 1""",(game['id'],game['shoukai'])).fetchone())
    found=processed=0;failures=list(continuation.get('failures',[])) if continuation else [];unclassified=[]
    hints={entry['url']:set(entry.get('hints',[])) for entry in continuation['pending']} if continuation else ({seed:{'trial'}} if game.get('trial_url') and a.canonical(game['trial_url'])==seed else {})
    while queue and processed<max_pages:
        url,depth=queue.popleft()
        if url in seen:continue
        seen.add(url);processed+=1
        rel="html/"+hashlib.sha256(url.encode()).hexdigest()+".html"
        try:
            with fetcher.open(url) as response:
                raw=response.read(16*1024*1024+1);actual=a.canonical(response.geturl())
                charset=response.headers.get_content_charset()
            if len(raw)>16*1024*1024:raise ValueError("publisher HTML exceeds 16 MiB")
            (store.root/rel).parent.mkdir(parents=True,exist_ok=True);(store.root/rel).write_bytes(raw)
            store.db.execute("INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,error,response_url) VALUES(?,?,?,?,?,?,?)",(url,"official",rel,a.now(),"ok",None,actual));store.db.commit()
            html=a.decode_html(raw,charset)
            base=urlsplit(actual);folder=base.path.rsplit("/",1)[0]+"/"
            scoped=product_folder_scoped(actual,game,brand,shared_home)
            options=hints.get(url,set())
            hint=next(iter(options)) if len(options)==1 else None
            records,unknown=parse(html,actual,game,brand,scoped=scoped,hint=hint,requested_page=url)
            unclassified.extend(unknown)
            for record in records:
                from mirror_sources import add_record
                add_record(store,record)
            found+=len(records)
            store.db.execute('INSERT OR REPLACE INTO official_page_visits VALUES(?,?,?,?,?)',(game['id'],url,a.now(),'ok',None));store.db.commit()
            if depth<2:
                for link in a.DOM(html).root.all({"a","iframe","frame"}):
                    target=a.resolve(actual,link.attrs.get("href") or link.attrs.get("src", ""))
                    if not target or BINARY.search(urlsplit(target).path):continue
                    parts=urlsplit(target)
                    extension=Path(parts.path).suffix.lower()
                    if extension not in {"",".html",".htm",".php",".cgi",".asp",".aspx"}:continue
                    label=" ".join([link.text(),(link.attrs.get('title') or ''),*[(image.attrs.get('alt') or '') for image in link.all({'img'})]])
                    navigation=label+" "+normalized(label)+" "+parts.path
                    # Publisher support commonly lives outside a product's
                    # folder. Follow only observed support links there; parse()
                    # still requires product identity on these shared pages.
                    inside=parts.path.startswith(folder)
                    if parts.hostname==base.hostname and (inside or SUPPORT_NAVIGATION.search(navigation)) and (link.tag in {"iframe","frame"} or NAVIGATION.search(navigation)):
                        kind=release_info({"title":label+" "+parts.path.rsplit("/",1)[-1],"filename":""})[0]
                        if kind!="other":hints.setdefault(target,set()).add(kind)
                        queue.append((target,depth+1))
        except (OSError,ValueError,http.client.HTTPException) as error:
            failures.append({"url":url,"error":str(error)})
            store.db.execute('INSERT OR REPLACE INTO official_page_visits VALUES(?,?,?,?,?)',(game['id'],url,a.now(),'failed',str(error)))
            store.db.execute("INSERT OR REPLACE INTO pages(url,site,path,fetched_at,status,error,response_url) VALUES(?,?,?,?,?,?,?)",(url,"official",rel if (store.root/rel).exists() else None,a.now(),"failed",str(error),None));store.db.commit()
    pending={}
    for url,depth in queue:
        if url not in seen and (url not in pending or depth<pending[url]['depth']):
            pending[url]={'url':url,'depth':depth,'hints':sorted(hints.get(url,set()))}
    if pending:failures.append({'error':'page bound reached; needs continuation','remaining':len(pending),
        'seed':seed,'pending':list(pending.values()),'seen':sorted(seen)})
    if unclassified:failures.append({"error":"unclassified binary links need review","urls":list(dict.fromkeys(unclassified))})
    total_found=found+(continuation.get('found',0) if continuation else 0)
    status="review" if failures else ("found" if total_found else "no_direct_links")
    store.db.execute("INSERT OR REPLACE INTO official_scans VALUES(?,?,?,?,?,?)",(game["id"],seed,status,total_found,json.dumps(failures,ensure_ascii=False) if failures else None,a.now()));store.db.commit()
    print("official",game["id"],brand["brandname"],game["gamename"],status,total_found,flush=True)
    return found


def collect(store, fetcher, limit=None, retry=False):
    initialize(store);done=0
    for game,brand,seed in seeds(store):
        existing=store.db.execute("SELECT status FROM official_scans WHERE game_id=?",(game["id"],)).fetchone()
        if existing and not (retry and existing[0]=="review"):continue
        if limit is not None and done>=limit:break
        scan(store,fetcher,game,brand,seed);done+=1
    store.export();return done


class HostFetcher:
    """Independent publishers progress concurrently; each host retains pacing."""
    def __init__(self):
        import threading
        self.guard=threading.Lock();self.hosts={}

    def open(self,url,headers=None):
        import threading
        key=urlsplit(url).hostname
        with self.guard:
            if key not in self.hosts:self.hosts[key]=(threading.Lock(),a.Fetcher(delay=3,timeout=25,retries=2))
            lock,fetcher=self.hosts[key]
        with lock:return fetcher.open(url,headers)


def collect_parallel(root,workers=4,limit=None,retry=False):
    from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
    from archive_lock import exclusive_file
    store=a.Store(root);initialize(store);fetcher=HostFetcher();submitted=completed=0
    def collect_one(game,brand,seed):
        with exclusive_file(Path(root)/".claims"/("official-scan-"+str(game["id"])+".lock")):
            local=a.Store(root)
            try:
                existing=local.db.execute("SELECT status FROM official_scans WHERE game_id=?",(game["id"],)).fetchone()
                if existing and not (retry and existing[0]=="review"):return False
                scan(local,fetcher,game,brand,seed);return True
            finally:local.db.close()
    try:
        with ThreadPoolExecutor(max_workers=workers,thread_name_prefix="publisher") as pool:
            jobs=set()
            for game,brand,seed in seeds(store):
                existing=store.db.execute("SELECT status FROM official_scans WHERE game_id=?",(game["id"],)).fetchone()
                if existing and not (retry and existing[0]=="review"):continue
                if limit is not None and submitted>=limit:break
                jobs.add(pool.submit(collect_one,game,brand,seed));submitted+=1
                if len(jobs)>=workers:
                    done,jobs=wait(jobs,return_when=FIRST_COMPLETED)
                    completed+=sum(int(job.result()) for job in done)
            for job in jobs:completed+=int(job.result())
        store.export();return completed
    finally:store.db.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root",default="archive");parser.add_argument("--limit",type=a.positive)
    parser.add_argument("--retry",action="store_true");parser.add_argument("--workers",type=a.positive,default=4);args=parser.parse_args()
    if args.workers>1:
        print("official scanned",collect_parallel(args.root,args.workers,args.limit,args.retry),flush=True)
    else:
        store=a.Store(args.root)
        try:print("official scanned",collect(store,a.Fetcher(delay=3,timeout=25,retries=2),args.limit,args.retry),flush=True)
        finally:store.db.close()
