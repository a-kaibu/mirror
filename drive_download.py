"""Resolve public Google Drive share links through published download forms."""
import re
from urllib.parse import urlsplit,parse_qs,urlencode,urljoin


def file_id(url):
    parts=urlsplit(url)
    if parts.hostname!='drive.google.com':return None
    match=re.fullmatch(r'/file/d/([A-Za-z0-9_-]+)/view/?',parts.path)
    ident=match[1] if match else parse_qs(parts.query).get('id',[None])[0]
    return ident if ident and re.fullmatch(r'[A-Za-z0-9_-]{10,200}',ident) else None


def open_download(fetcher,url,headers=None):
    import archiver as a
    ident=file_id(url)
    if ident is None:return fetcher.open(url,headers)
    target='https://drive.usercontent.google.com/download?'+urlencode({'id':ident,'export':'download'})
    for _ in range(3):
        response=fetcher.open(target,headers)
        actual=urlsplit(response.geturl())
        if actual.scheme!='https' or not (actual.hostname in {'drive.google.com','drive.usercontent.google.com'} or (actual.hostname or '').endswith('.googleusercontent.com')):
            response.close();raise ValueError('Drive download left registered Google hosts')
        if response.headers.get_content_type() not in {'text/html','application/xhtml+xml'}:return response
        try:raw=response.read(2*1024*1024+1)
        finally:response.close()
        if len(raw)>2*1024*1024:raise ValueError('Drive confirmation exceeds bound')
        root=a.DOM(a.decode_html(raw)).root;target=None
        for form in root.all({'form'}):
            if form.attrs.get('id')!='download-form' or form.attrs.get('method','get').lower()!='get':continue
            action=urljoin(response.geturl(),form.attrs.get('action',''));parts=urlsplit(action)
            if parts.scheme!='https' or parts.hostname not in {'drive.google.com','drive.usercontent.google.com'} or parts.path not in {'/download','/uc'}:continue
            values={n.attrs['name']:n.attrs.get('value','') for n in form.all({'input'}) if n.attrs.get('name') in {'id','export','confirm','uuid','authuser','at'} and 'disabled' not in n.attrs}
            if values.get('id')!=ident or values.get('export')!='download':continue
            target=action+('&' if parts.query else '?')+urlencode(values);break
        if not target:raise ValueError('Drive public download unavailable or requires sign-in')
    raise ValueError('Drive confirmation did not reach a binary')
