"""Resolve public file-sharing links through their published download mechanisms."""
import re
from urllib.parse import urlsplit,urlunsplit,parse_qs,parse_qsl,urlencode,urljoin


def file_id(url):
    parts=urlsplit(url)
    if parts.hostname!='drive.google.com':return None
    match=re.fullmatch(r'/file/d/([A-Za-z0-9_-]+)/view/?',parts.path)
    ident=match[1] if match else parse_qs(parts.query).get('id',[None])[0]
    return ident if ident and re.fullmatch(r'[A-Za-z0-9_-]{10,200}',ident) else None


def open_download(fetcher,url,headers=None):
    import archiver as a
    parts=urlsplit(url)
    # Dropbox documents dl=1 for public shared files; retain the original
    # link in provenance and preserve its access parameters (including rlkey).
    # https://help.dropbox.com/share/force-download
    if (parts.scheme in {'http','https'} and parts.hostname in {'dropbox.com','www.dropbox.com'}
            and not parts.username and not parts.password
            and re.fullmatch(r'/(?:s|scl/fi)/[^/]+/[^/]+',parts.path)):
        query=[(k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True) if k not in {'dl','raw'}]
        target=urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(query+[('dl','1')]),parts.fragment))
        return fetcher.open(target,headers)
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
