"""platform.sh: distribution detection and package commands used by install.sh."""
import os,stat,subprocess,tempfile,unittest
from pathlib import Path

ROOT=Path(__file__).parents[1]

RELEASES={
    'raspberry-pi-os':('ID=raspbian\nID_LIKE=debian\nPRETTY_NAME="Raspbian GNU/Linux 12 (bookworm)"','apt'),
    'debian':('ID=debian\nPRETTY_NAME="Debian GNU/Linux 12 (bookworm)"','apt'),
    'ubuntu':('ID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu 24.04 LTS"','apt'),
    'mint':('ID=linuxmint\nID_LIKE="ubuntu debian"\nPRETTY_NAME="Linux Mint 22"','apt'),
    'fedora':('ID=fedora\nPRETTY_NAME="Fedora Linux 40 (Workstation Edition)"','dnf'),
    'rocky':('ID="rocky"\nID_LIKE="rhel centos fedora"\nPRETTY_NAME="Rocky Linux 9.4"','dnf'),
    'arch':('ID=arch\nPRETTY_NAME="Arch Linux"','pacman'),
    'manjaro':('ID=manjaro\nID_LIKE=arch\nPRETTY_NAME="Manjaro Linux"','pacman'),
    'tumbleweed':('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\nPRETTY_NAME="openSUSE Tumbleweed"','zypper'),
    'alpine':('ID=alpine\nPRETTY_NAME="Alpine Linux v3.20"','unknown'),
}


def run(script,env=None):
    return subprocess.run(['bash','-c','set -euo pipefail; . "$PM_ROOT/platform.sh"; '+script],capture_output=True,text=True,
                          env={**os.environ,'PM_ROOT':str(ROOT),**(env or {})})


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.dir=Path(self.tmp.name)
    def tearDown(self):self.tmp.cleanup()

    def release(self,text):
        path=self.dir/'os-release';path.write_text(text+'\n');return str(path)

    def test_detects_package_manager(self):
        for name,(text,expected) in RELEASES.items():
            result=run('pm_detect_platform; echo "$PM_PKG|$PM_OS_NAME"',{'PM_OS_RELEASE':self.release(text)})
            self.assertEqual(result.returncode,0,result.stderr)
            pkg,pretty=result.stdout.strip().split('|')
            self.assertEqual(pkg,expected,name);self.assertTrue(pretty and pretty!='unknown Linux',name)
        self.assertEqual(run('pm_detect_platform; echo $PM_PKG',{'PM_OS_RELEASE':str(self.dir/'missing')}).stdout.strip(),'unknown')

    def test_dry_run_package_commands(self):
        expected={
            'ubuntu':['+ apt-get update','+ env DEBIAN_FRONTEND=noninteractive apt-get install -y python3 python3-venv sudo','+ env DEBIAN_FRONTEND=noninteractive apt-get install -y ffmpeg'],
            'fedora':['+ dnf install -y python3 sudo','+ dnf install -y ffmpeg-free'],
            'arch':['+ pacman -Syu --needed --noconfirm python sudo','+ pacman -S --needed --noconfirm ffmpeg'],
            'tumbleweed':['+ zypper --non-interactive install python3 sudo','+ zypper --non-interactive install ffmpeg'],
        }
        for name,lines in expected.items():
            result=run('pm_detect_platform; pm_install_packages',{'PM_OS_RELEASE':self.release(RELEASES[name][0]),'PM_DRY_RUN':'1'})
            self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip().splitlines(),lines,name)
        result=run('pm_detect_platform; pm_install_packages',{'PM_OS_RELEASE':self.release(RELEASES['alpine'][0]),'PM_DRY_RUN':'1'})
        self.assertEqual(result.returncode,0);self.assertIn('skipping package installation',result.stdout)

    def test_failed_required_install_stops(self):
        # Must fail even when called where `set -e` is suspended (e.g. `pm_install_packages || ...`).
        fake=self.dir/'bin';fake.mkdir()
        (fake/'apt-get').write_text('#!/bin/sh\n[ "$1" = update ] && exit 0\nexit 100\n');(fake/'apt-get').chmod(0o755)
        result=run('pm_detect_platform; if pm_install_packages; then echo CONTINUED; else echo STOPPED; fi',
                   {'PM_OS_RELEASE':self.release(RELEASES['debian'][0]),'PATH':f'{fake}:{os.environ["PATH"]}'})
        self.assertIn('STOPPED',result.stdout);self.assertNotIn('CONTINUED',result.stdout);self.assertIn('Could not install',result.stdout)

    def test_python_version_check(self):
        self.assertEqual(run('pm_check_python').returncode,0)  # the test runner's Python is 3.11+
        fake=self.dir/'bin';fake.mkdir()
        (fake/'python3').write_text('#!/bin/sh\n[ "$1" = "-V" ] && echo "Python 3.10.12"\nexit 1\n');(fake/'python3').chmod(0o755)
        result=run('pm_check_python',{'PATH':f'{fake}:{os.environ["PATH"]}'})
        self.assertNotEqual(result.returncode,0);self.assertIn('Python 3.11 or newer is required',result.stdout);self.assertIn('3.10.12',result.stdout)

    def test_nologin_and_helpers(self):
        shell=run('pm_nologin').stdout.strip()
        self.assertTrue(shell in ('/bin/false',) or shell.endswith('nologin'),shell)
        self.assertTrue(os.access(shell,os.X_OK))
        self.assertEqual(run('pm_firewall_hint 8080 127.0.0.1').returncode,0)
        self.assertEqual(run('pm_suggest_ip >/dev/null').returncode,0)

    def test_installer_uses_platform_helpers(self):
        text=(ROOT/'install.sh').read_text()
        for needed in ('. "$SOURCE_DIR/platform.sh"','pm_detect_platform','pm_check_systemd','pm_install_packages','pm_check_python','--shell "$(pm_nologin)"'):
            self.assertIn(needed,text)
        self.assertNotIn('apt-get',text)  # package commands live in platform.sh


if __name__=='__main__':
    unittest.main()
