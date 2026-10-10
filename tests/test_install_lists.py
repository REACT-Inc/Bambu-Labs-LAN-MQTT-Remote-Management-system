"""install.sh and Updater/update.sh copy an explicit list of files; a missing file breaks installs/updates."""
import re,unittest
from pathlib import Path

ROOT=Path(__file__).parents[1]
# Installed separately (root-owned updater) or only used to build releases.
NOT_COPIED={'build_release.py','Updater/update_worker.py'}
# Not part of the Pi's installation: the desktop app for laptops is built into its own .exe.
NOT_ON_THE_PI=('desktop',)


def runtime_python():
    return {p.relative_to(ROOT).as_posix() for p in ROOT.rglob('*.py')
            if 'tests' not in p.relative_to(ROOT).parts and p.relative_to(ROOT).parts[0] not in NOT_ON_THE_PI
            and not any(part.startswith('.') or part in ('venv','__pycache__') for part in p.relative_to(ROOT).parts)}-NOT_COPIED


class InstallListTests(unittest.TestCase):
    def test_install_copies_every_runtime_file(self):
        listed=set(re.search(r'for file in ([^;]*);',(ROOT/'install.sh').read_text())[1].split())
        self.assertEqual(sorted(runtime_python()-listed),[],'Add these to the file list in install.sh')
        self.assertEqual(sorted(n for n in listed if not (ROOT/n).is_file()),[],'install.sh lists files that do not exist')

    def test_update_copies_and_checks_every_runtime_file(self):
        text=(ROOT/'Updater/update.sh').read_text()
        copied=set(re.search(r'PM_FILES=\(([^)]*)\)',text)[1].split())
        checked=set(re.findall(r"'([^']+\.py)'",re.search(r'for name in \(([^)]*)\)',text)[1]))
        # configure.py only runs on first install.
        self.assertEqual(sorted(runtime_python()-copied-{'configure.py'}),[],'Add these to PM_FILES in Updater/update.sh')
        self.assertEqual(sorted(n for n in copied if not (ROOT/n).is_file()),[],'Updater/update.sh lists files that do not exist')
        self.assertEqual(sorted({n for n in copied if n.endswith('.py')}-checked-{'Updater/version.py'}),[],'Add these to the syntax check in Updater/update.sh')


if __name__=='__main__':
    unittest.main()
