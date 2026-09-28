"""Opt-in GitHub release updates, using the existing validated installer."""
import asyncio,hashlib,json,os,re,secrets,shutil,time
from pathlib import Path
from urllib.parse import urlparse
import aiohttp
from aiohttp import web
from Updater.update_package import inspect_package,MAX_ZIP
from Updater.version import VERSION

ASSET='3d-printer-management.zip'

# Update channels, least to most adventurous. Each channel also receives the releases of the channels before it.
CHANNELS=('stable','beta','alpha')
RANK={'alpha':0,'beta':1,'stable':2}

def semver(value):
    """Comparable key for vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-beta.N / -alpha.N (alpha < beta < stable)."""
    match=re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(alpha|beta)\.(0|[1-9][0-9]*))?',str(value))
    if not match:raise ValueError('Use a version such as v1.0.1, v1.1.0-beta.1 or v1.1.0-alpha.1.')
    major,minor,patch,stage,number=match.groups()
    return (int(major),int(minor),int(patch),RANK[stage or 'stable'],int(number or 0))

def channel_of(value):
    return ('alpha','beta','stable')[semver(value)[3]]

def installed_channel():
    try:return channel_of(VERSION)
    except ValueError:return 'unknown'  # e.g. a local development build

def in_channel(version,channel):
    return CHANNELS.index(channel_of(version))<=CHANNELS.index(channel)

def release_info(data,repo):
    tag=data.get('tag_name','');semver(tag)
    if data.get('draft'):raise ValueError('Draft releases are not supported.')
    # GitHub's pre-release flag must agree with the tag, so a beta never shows as the stable "Latest" release.
    if bool(data.get('prerelease'))!=(channel_of(tag)!='stable'):
        raise ValueError(f'{tag}: alpha/beta tags must be marked as pre-releases on GitHub and stable tags must not.')
    assets={a['name']:a for a in data.get('assets',[]) if a.get('state')=='uploaded'}
    selected=[]
    for name,limit in [(ASSET,MAX_ZIP),(ASSET+'.sha256',256)]:
        asset=assets.get(name)
        if not asset or type(asset.get('id')) is not int or not 0<asset.get('size',0)<=limit:
            raise ValueError('Release needs a valid management ZIP and SHA-256 asset. Use the supplied release workflow.')
        selected.append({'url':f'https://api.github.com/repos/{repo}/releases/assets/{asset["id"]}','size':asset['size']})
    return {'tag':tag,'version':tag.lstrip('v'),'channel':channel_of(tag),'zip':selected[0],'checksum':selected[1],
            'url':f'https://github.com/{repo}/releases/tag/{tag}'}

def download_host(url):
    parsed=urlparse(url)
    return parsed.scheme=='https' and not parsed.username and not parsed.password and parsed.port in (None,443) and parsed.hostname in ('release-assets.githubusercontent.com','objects.githubusercontent.com','github.com','api.github.com')


class GitHubUpdates:
    def __init__(self,dashboard):
        self.dashboard=dashboard;self.updater=dashboard.updater;self.core=dashboard.core
        self.path=self.core.DATA_DIR/'github-updates.json';self.lock=asyncio.Lock();self.task=None
        try:self.config=json.loads(self.path.read_text())
        except (OSError,ValueError):self.config={}
        self.latest=None;self.message='Not checked yet.';self.checked=0
        dashboard.app.add_routes([web.get('/api/github/status',self.status),web.post('/api/github/settings',self.settings),
            web.post('/api/github/check',self.check_route),web.post('/api/github/install',self.install_route)])
        dashboard.app.on_startup.append(self.start);dashboard.app.on_cleanup.append(self.close)

    def save(self):
        temp=self.path.with_suffix('.tmp')
        with temp.open('w') as f:os.chmod(temp,0o600);json.dump(self.config,f)
        os.replace(temp,self.path)

    @property
    def channel(self):
        channel=self.config.get('channel','stable')
        return channel if channel in CHANNELS else 'stable'

    def public(self):
        latest=self.latest
        return {'repository':self.config.get('repository',''),'automatic':self.config.get('automatic',False),
            'channel':self.channel,'channels':list(CHANNELS),'installed_channel':installed_channel(),
            'has_token':bool(self.config.get('token')),'installed':VERSION,'latest':latest['version'] if latest else None,
            'available':bool(latest and semver(latest['version'])>semver(VERSION)),
            'message':self.message,'checked':self.checked,'attempted':self.config.get('attempted','')}

    async def status(self,request):return web.json_response(self.public())

    async def settings(self,request):
        data=await request.json();repo=str(data.get('repository','')).strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}',repo):raise ValueError('Enter OWNER/REPOSITORY, not a URL.')
        auto=data.get('automatic',False)
        channel=data.get('channel',self.channel)
        if channel not in CHANNELS:raise ValueError('Choose the stable, beta or alpha update channel.')
        if type(auto) is not bool:raise ValueError('Automatic updates must be true or false.')
        if auto and data.get('confirmed') is not True:raise ValueError('Confirm that you trust releases from this repository to install automatically.')
        token=str(data.get('token','')).strip()
        if len(token)>512 or any(ord(c)<33 or ord(c)>126 for c in token):raise ValueError('Invalid GitHub token.')
        async with self.lock:
            if self.updater.busy():raise ValueError('Wait for the current update to finish.')
            changed=repo!=self.config.get('repository')
            if changed and self.config.get('token') and not token and not data.get('clear_token'):
                raise ValueError('Supply a token for the new repository or choose Clear saved token.')
            saved_token='' if data.get('clear_token') else token or self.config.get('token','')
            self.config.update(repository=repo,automatic=auto,token=saved_token,channel=channel)
            if changed:self.config.pop('attempted',None)
            self.latest=None;self.checked=0;self.message='Saved. Check for a release.';self.save()
        return web.json_response(self.public())

    async def fetch(self,url,limit,binary=False):
        # Credentials are sent only to GitHub's API, never to redirected asset hosts.
        if not url.startswith('https://api.github.com/repos/'+self.config['repository']+'/'):raise ValueError('Unexpected GitHub API URL.')
        headers={'Accept':'application/octet-stream' if binary else 'application/vnd.github+json',
                 'X-GitHub-Api-Version':'2022-11-28','User-Agent':'3d-printer-management'}
        if self.config.get('token'):headers['Authorization']='Bearer '+self.config['token']
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180,connect=15)) as session:
            for attempt in range(4):
                async with session.get(url,headers=headers,allow_redirects=False) as response:
                    if response.status in (301,302,303,307,308):
                        target=response.headers.get('Location','')
                        if not binary or not download_host(target):raise ValueError('Unexpected asset redirect.')
                        url=target;headers={'Accept':'application/octet-stream','User-Agent':'3d-printer-management'};continue
                    if response.status!=200:
                        raise ValueError(f'GitHub returned HTTP {response.status}. Check repository, release, token permissions or rate limits.')
                    data=bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data)>limit:raise ValueError('GitHub response exceeds size limit.')
                    return bytes(data)
        raise ValueError('Too many GitHub redirects.')

    async def check(self):
        repo=self.config.get('repository')
        if not repo:raise ValueError('Save a GitHub repository first.')
        data=json.loads(await self.fetch(f'https://api.github.com/repos/{repo}/releases?per_page=50',4*1024*1024))
        if not isinstance(data,list):raise ValueError('Unexpected GitHub releases response.')
        candidates=[]
        for item in data:
            try:
                if isinstance(item,dict) and in_channel(item.get('tag_name',''),self.channel):candidates.append(item)
            except ValueError:continue  # Tags that are not versions (or other channels) are ignored.
        if not candidates:raise ValueError(f'No {self.channel}-channel release found in {repo}.')
        best=max(candidates,key=lambda item:semver(item['tag_name']))
        self.latest=release_info(best,repo);self.checked=time.time()
        if semver(self.latest['version'])>semver(VERSION):self.message=f'New {self.latest["channel"]} release available.'
        elif semver(self.latest['version'])<semver(VERSION):
            # Switching to a more stable channel never downgrades; it waits for that channel to catch up.
            self.message=f'Installed {VERSION} is newer than the latest {self.channel}-channel release; the next newer one will be offered.'
        else:self.message='Already up to date.'
        return self.latest

    async def check_route(self,request):
        async with self.lock:await self.check()
        return web.json_response(self.public())

    async def install(self,automatic=False,expected=None,force=False,who='dashboard'):
        async with self.updater.lock:
            self.updater.idle_check(automatic,force)
            release=await self.check()
            if expected and release['version']!=expected:raise ValueError('Latest release changed. Review it and confirm again.')
            if semver(release['version'])<=semver(VERSION):raise ValueError(f'No newer release on the {self.channel} channel.')
            if automatic and self.config.get('attempted')==release['version']:
                self.message='This release was already attempted. Inspect updater status; manual retry or a newer release is required.';return
            if shutil.disk_usage(self.updater.inbox).free<256*1024*1024:raise ValueError('Free at least 256 MiB before downloading.')
            raw=await self.fetch(release['checksum']['url'],256,True)
            match=re.fullmatch(rb'([0-9a-fA-F]{64})[ \t]+\*?3d-printer-management\.zip\s*',raw)
            if not match:raise ValueError('Invalid release checksum asset.')
            payload=await self.fetch(release['zip']['url'],MAX_ZIP,True)
            digest=hashlib.sha256(payload).hexdigest()
            if len(payload)!=release['zip']['size'] or digest!=match[1].decode().lower():raise ValueError('Release download checksum/size mismatch.')
            token=secrets.token_hex(16);path=self.updater.inbox/(token+'.zip')
            try:
                path.write_bytes(payload)
                preview=await asyncio.to_thread(inspect_package,path)
                if preview['version'].lstrip('v')!=release['version']:raise ValueError('Release tag and package version do not match.')
                # Recheck printer state after network I/O, before creating the request.
                self.updater.idle_check(automatic,force)
                self.config['attempted']=release['version'];self.save()
                self.updater.queue_package(token,digest,automatic,force=force,who=who)
                self.message='Installation queued; sign in again after restart.'
            except BaseException:path.unlink(missing_ok=True);raise

    async def install_route(self,request):
        data=await request.json()
        if data.get('confirmed') is not True or not data.get('version'):raise ValueError('Check and confirm the release version first.')
        async with self.lock:await self.install(expected=data['version'],force=data.get('force') is True,who=f'dashboard ({request.remote or "?"})')
        return web.json_response(self.public())

    async def loop(self):
        await asyncio.sleep(60)
        while True:
            try:
                async with self.lock:
                    if self.config.get('repository') and not self.updater.busy():
                        if time.time()-self.checked>=3600:await self.check()
                        if self.config.get('automatic') and self.latest and semver(self.latest['version'])>semver(VERSION):
                            if self.config.get('attempted')!=self.latest['version']:await self.install(automatic=True)
            except asyncio.CancelledError:raise
            except Exception as exc:
                # Do not expose tokens, signed asset URLs or full network exception strings.
                self.message=str(exc) if isinstance(exc,ValueError) else 'GitHub check/download failed ('+type(exc).__name__+'). Will check again.'
            await asyncio.sleep(300)

    async def start(self,app):self.task=asyncio.create_task(self.loop())
    async def close(self,app):
        if self.task:self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
