"""Create a reviewed, self-contained installer; never include config, runtime or secrets."""
import base64
import hashlib
import io
import json
import tarfile
import textwrap
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
OUTPUT=ROOT.parent/'output/mac-ci'
OUTPUT.mkdir(parents=True,exist_ok=True)
files=[]
for name in ('bento_ci','static','templates','scripts','tests','docs'):
    files += [p for p in (ROOT/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc']
files += [ROOT/name for name in ('README.md','COPILOT-WORK-MAC-SETUP.md','setup.sh','bento','config.example.json','requirements.txt','requirements.lock','requirements-dev.txt','.gitignore')]
files=sorted(set(files))
manifest={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
manifest_bytes=(json.dumps(manifest,indent=2)+'\n').encode()
buffer=io.BytesIO()
with tarfile.open(fileobj=buffer,mode='w:gz') as tar:
    for file in files:
        info=tar.gettarinfo(str(file),arcname=str(file.relative_to(ROOT)))
        info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0
        with file.open('rb') as source:tar.addfile(info,source)
    info=tarfile.TarInfo('SOURCE-MANIFEST.json');info.size=len(manifest_bytes);info.mode=0o600
    tar.addfile(info,io.BytesIO(manifest_bytes))
payload=buffer.getvalue()
sha=hashlib.sha256(payload).hexdigest()
header=r'''#!/bin/bash
set -euo pipefail
umask 077
BENTO_NEEDS_SOURCE=0
for BENTO_ARG in "$@"; do
  if [[ "$BENTO_NEEDS_SOURCE" == 1 ]]; then
    case "$BENTO_ARG" in github|gitlab) BENTO_NEEDS_SOURCE=0; continue ;; *) echo 'Source must be github or gitlab.' >&2; exit 1 ;; esac
  fi
  case "$BENTO_ARG" in
    --extract-only|--local|--no-start|--work-mac) ;;
    --source) BENTO_NEEDS_SOURCE=1 ;;
    --source=github|--source=gitlab) ;;
    *) echo 'Usage: bash bento-mac-ci-setup.sh [--extract-only] [--work-mac] [--local] [--no-start] [--source github|gitlab]' >&2; exit 1 ;;
  esac
done
if [[ "$BENTO_NEEDS_SOURCE" == 1 ]]; then echo '--source requires github or gitlab.' >&2; exit 1; fi
BENTO_SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
shopt -s dotglob nullglob
for BENTO_FILE in *; do
  if [[ "$PWD/$BENTO_FILE" != "$BENTO_SELF" && "$BENTO_FILE" != .DS_Store ]]; then
    echo 'Choose an empty destination directory; this installer never overwrites an existing project.' >&2; exit 1
  fi
done
BENTO_TEMP="$(mktemp -d)"
trap 'rm -rf "$BENTO_TEMP"' EXIT
BENTO_LINE="$(awk '/^__BENTO_PAYLOAD_BELOW__$/ { print NR + 1; exit }' "$BENTO_SELF")"
if [[ "$(uname -s)" == Darwin ]]; then
  tail -n +"$BENTO_LINE" "$BENTO_SELF" | base64 -D > "$BENTO_TEMP/package.tar.gz"
else
  tail -n +"$BENTO_LINE" "$BENTO_SELF" | base64 --decode > "$BENTO_TEMP/package.tar.gz"
fi
BENTO_ACTUAL="$(shasum -a 256 "$BENTO_TEMP/package.tar.gz" | awk '{print $1}')"
if [[ "$BENTO_ACTUAL" != "PAYLOAD_SHA" ]]; then echo 'Installer payload checksum failed.' >&2; exit 1; fi
tar -xzf "$BENTO_TEMP/package.tar.gz" -C "$PWD"
echo "Bento Mac CI extracted to $PWD"
for BENTO_ARG in "$@"; do
  if [[ "$BENTO_ARG" == '--extract-only' ]]; then exit 0; fi
done
bash ./setup.sh "$@"
exit $?
__BENTO_PAYLOAD_BELOW__
'''.replace('PAYLOAD_SHA',sha)
installer=OUTPUT/'bento-mac-ci-setup.sh'
installer.write_text(header+'\n'.join(textwrap.wrap(base64.b64encode(payload).decode(),76))+'\n')
installer.chmod(0o700)
with zipfile.ZipFile(OUTPUT/'Bento-Mac-CI.zip','w',zipfile.ZIP_DEFLATED) as z:
    for file in files:z.write(file,'Bento-Mac-CI/'+str(file.relative_to(ROOT)))
    z.writestr('Bento-Mac-CI/SOURCE-MANIFEST.json',manifest_bytes)
(OUTPUT/'SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in (installer,OUTPUT/'Bento-Mac-CI.zip')))
print(installer)
print(OUTPUT/'Bento-Mac-CI.zip')
