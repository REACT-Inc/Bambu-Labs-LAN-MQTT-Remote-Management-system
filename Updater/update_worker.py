"""Fixed privileged updater. Installed root-owned; never replaced by web packages."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import fcntl, hashlib, io, json, os, pwd, re, shutil, stat, subprocess, time, urllib.request
from update_package import inspect_package, MAX_ZIP
APP=Path('/opt/3d-printer-management')
RELEASES=Path('/opt/3d-printer-management-releases')
DATA=Path('/var/lib/3d-printer-management')
CONFIG=Path('/etc/3d-printer-management')
STATE=Path('/var/lib/pm-updater')
SERVICE='3d-printer-management'


def atomic_json(path,data,mode=0o600):
    temp=path.with_suffix('.tmp')
    with temp.open('w') as f:
        os.chmod(temp,mode);json.dump(data,f);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)
    fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def status(identity,phase,message):
    atomic_json(STATE/'status.json',{'id':identity,'state':phase,'message':message,'time':time.time()},0o644)


def run(args,timeout=120):
    # Detailed pip output can contain credentials: keep it out of the dashboard, and log only the last lines of a
    # failure to the journal (sudo journalctl -u pm-web-update), with any user:password@ in URLs removed.
    result=subprocess.run(args,timeout=timeout,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    if result.returncode:
        tail=result.stderr.decode(errors='replace').strip().splitlines()[-15:]
        print(f'{Path(args[0]).name} failed with exit {result.returncode}:',file=sys.stderr)
        for line in tail:print('  '+re.sub(r'://[^/@\s]+@','://***@',line)[:500],file=sys.stderr)
        raise subprocess.CalledProcessError(result.returncode,args[:1])


def readable(root):
    """Let the service user read (and run) everything in a release, whatever umask the venv/pip steps ran with."""
    for base,dirs,files in os.walk(root,followlinks=False):
        for path in [Path(base),*(Path(base)/name for name in dirs+files)]:
            info=os.lstat(path)
            if stat.S_ISLNK(info.st_mode):continue
            executable=stat.S_ISDIR(info.st_mode) or info.st_mode&stat.S_IXUSR
            os.chmod(path,stat.S_IMODE(info.st_mode)|(0o755 if executable else 0o644))


def service(action):run(['/usr/bin/systemctl',action,SERVICE])


def bounded_read(directory,name,limit):
    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
    with os.fdopen(fd,'rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):raise ValueError('Expected a regular file.')
        data=f.read(limit+1)
        if len(data)>limit:raise ValueError('File exceeds limit.')
        return data


def take_request():
    datafd=os.open(DATA,os.O_DIRECTORY|os.O_NOFOLLOW)
    try:inbox=os.open('updates',os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=datafd)
    finally:os.close(datafd)
    try:
        try:raw=bounded_read(inbox,'request.json',4096)
        except FileNotFoundError:return None
        finally:
            try:os.unlink('request.json',dir_fd=inbox)
            except FileNotFoundError:pass
        request=json.loads(raw)
        identity=request.get('id','');digest=request.get('sha256','')
        if not re.fullmatch('[0-9a-f]{32}',identity) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('Invalid request.')
        package=bounded_read(inbox,identity+'.zip',MAX_ZIP)
        if hashlib.sha256(package).hexdigest()!=digest:raise ValueError('Uploaded ZIP changed after review.')
        return identity,package
    finally:os.close(inbox)


def pointer(target):
    temp=APP.with_name(APP.name+'.next')
    temp.unlink(missing_ok=True);temp.symlink_to(target,target_is_directory=True);os.replace(temp,APP)


def copy_state(target):
    # Preserve links as links, never follow application-controlled paths as root.
    shutil.copytree(DATA,target/'data',symlinks=True,ignore=shutil.ignore_patterns('uploads','updates'))
    shutil.copytree(CONFIG,target/'config',symlinks=True)


def remove(path):
    if path.is_symlink() or path.is_file():path.unlink()
    elif path.exists():shutil.rmtree(path)


def restore_state(backup):
    for child in DATA.iterdir():
        if child.name not in ('uploads','updates'):remove(child)
    user=pwd.getpwnam('printermanager')
    for child in (backup/'data').iterdir():
        dest=DATA/child.name
        if child.is_dir() and not child.is_symlink():shutil.copytree(child,dest,symlinks=True)
        else:shutil.copy2(child,dest,follow_symlinks=False)
        for p in [dest,*dest.rglob('*')] if dest.is_dir() and not dest.is_symlink() else [dest]:
            os.chown(p,user.pw_uid,user.pw_gid,follow_symlinks=False)
    # Restore config contents and the original root/group ownership.
    for child in CONFIG.iterdir():remove(child)
    for child in (backup/'config').iterdir():
        dest=CONFIG/child.name
        if child.is_dir() and not child.is_symlink():shutil.copytree(child,dest,symlinks=True)
        else:shutil.copy2(child,dest,follow_symlinks=False)
        for p in [dest,*dest.rglob('*')] if dest.is_dir() and not dest.is_symlink() else [dest]:
            os.chown(p,0,user.pw_gid,follow_symlinks=False)


def rollback(journal):
    status(journal['id'],'rolling_back','Restoring the previous release and saved settings.')
    service('stop')
    previous=Path(journal['previous'])
    # The first migration might have stopped before the original directory moved.
    if previous.exists():
        if APP.exists() and not APP.is_symlink():raise RuntimeError('Unexpected application directory during recovery.')
        pointer(previous)
    restore_state(Path(journal['backup']))
    service('start')
    status(journal['id'],'rolled_back','Update did not complete. Previous code and saved data restored. Check service status if the dashboard stays unavailable.')
    (STATE/'journal.json').unlink(missing_ok=True)


def healthy(identity,timeout=75):
    config=json.loads((CONFIG/'config.json').read_text());port=int(config.get('port',8080))
    deadline=time.monotonic()+timeout;consecutive=0
    while time.monotonic()<deadline:
        try:
            run(['/usr/bin/systemctl','is-active','--quiet',SERVICE],10)
            with urllib.request.urlopen('http://127.0.0.1:'+str(port)+'/health',timeout=2) as r:reply=json.load(r)
            if reply.get('application')!='3d-printer-management' or reply.get('release')!=identity:raise ValueError('Release mismatch')
            consecutive+=1
            if consecutive>=4:return True
        except Exception:consecutive=0
        time.sleep(2)
    return False


def install(identity,package):
    status(identity,'validating','Validating the reviewed package.')
    metadata=inspect_package(io.BytesIO(package))
    if shutil.disk_usage(RELEASES).free<1024**3:raise ValueError('Free at least 1 GiB on the application disk.')
    release=RELEASES/identity;release.mkdir(mode=0o755)
    inspect_package(io.BytesIO(package),release)
    for base,dirs,files in os.walk(release):
        os.chmod(base,0o755)
        for name in files:os.chmod(Path(base)/name,0o644)
    atomic_json(release/'release.json',{'id':identity,'version':metadata['version']},0o644)
    status(identity,'preparing','Preparing Python dependencies. The existing dashboard is still running.')
    user=pwd.getpwnam('printermanager')
    venv=release/'venv';venv.mkdir();os.chown(venv,user.pw_uid,user.pw_gid)
    run(['/usr/sbin/runuser','-u','printermanager','--','/usr/bin/python3','-m','venv',str(venv)],180)
    # Pip executes only as the service user, never as root. Shell/install scripts from ZIPs are ignored.
    run(['/usr/sbin/runuser','-u','printermanager','--',str(venv/'bin/python'),'-m','pip','install','--disable-pip-version-check','--no-cache-dir','-r',str(release/'requirements.txt')],1200)
    for base,dirs,files in os.walk(venv,followlinks=False):
        for name in ['.',*dirs,*files]:os.chown(Path(base)/name,0,0,follow_symlinks=False)
    readable(release)
    status(identity,'backing_up','Stopping management briefly and backing up settings and queue data.')
    service('stop')
    backup=STATE/'backups'/identity;backup.mkdir(parents=True,mode=0o700)
    try:copy_state(backup)
    except Exception:service('start');raise
    previous=APP.resolve() if APP.is_symlink() else RELEASES/('legacy-'+identity)
    journal={'id':identity,'previous':str(previous),'backup':str(backup),'committed':False}
    atomic_json(STATE/'journal.json',journal)
    try:
        status(identity,'installing','Installing the new release.')
        if not APP.is_symlink():APP.rename(previous)
        pointer(release)
        service('start');status(identity,'checking','Checking that the new dashboard starts and stays available.')
        if not healthy(identity):raise RuntimeError('New release failed startup checks.')
        journal['committed']=True;atomic_json(STATE/'journal.json',journal)
        status(identity,'succeeded','Update installed: '+metadata['version']+'. Sign in again.')
        (STATE/'journal.json').unlink(missing_ok=True)
    except Exception:
        rollback(journal)


def main():
    if os.geteuid()!=0:raise SystemExit('Root updater service only.')
    # The release, its venv and the files pip installs must be readable by the service user. The unit's UMask=0077
    # (still on Pis installed before this fix) made the venv root-only, so the new release failed to start and rolled back.
    os.umask(0o022)
    STATE.mkdir(mode=0o755,exist_ok=True);RELEASES.mkdir(mode=0o755,exist_ok=True)
    with (STATE/'lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        journal=STATE/'journal.json'
        if journal.exists():
            saved=json.loads(journal.read_text())
            if saved.get('committed'):
                status(saved['id'],'succeeded','Update committed. Sign in again.');journal.unlink()
            else:rollback(saved)
        identity=''
        try:
            request=take_request()
            if request is None:
                try:old=json.loads((STATE/'status.json').read_text())
                except (OSError,ValueError):old={}
                if old.get('state') in ('validating','preparing','backing_up'):
                    service('start');status(old.get('id',''),'failed','Update interrupted before installation. Existing release restarted; upload again.')
                return
            identity,package=request
            install(identity,package)
        except Exception as error:
            # Avoid dumping configuration, tokens, dependency URLs, or arbitrary ZIP strings.
            if journal.exists():
                status(identity,'recovery_required','Automatic recovery needs attention. Run sudo journalctl -u pm-web-update and restart that service to retry recovery.')
                raise
            status(identity,'failed','Update failed before installation ('+type(error).__name__+'). Existing release kept. Check disk space, package format and internet access for dependencies.')
            raise

if __name__=='__main__':main()
