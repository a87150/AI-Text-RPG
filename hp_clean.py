# -*- coding: utf-8 -*-
import sys, json, pathlib, shutil, time
sys.stdout.reconfigure(encoding='utf-8')
bad = {'我', '你', '他', '她', '它', '咱们', '我们', '你们', '他们', '她们',
       '自己', '主角', '别人', '对方', '大家', '朋友', '路人', '众人'}
root = pathlib.Path('data')
stamp = time.strftime('%Y%m%d-%H%M%S')
backup = pathlib.Path('backups') / ('clean-' + stamp)
backup.mkdir(parents=True, exist_ok=True)
changed = 0
for f in list(root.glob('*.json')):
    try:
        d = json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        continue
    chars = d.get('characters')
    if not isinstance(chars, list):
        continue
    keep = [c for c in chars if isinstance(c, dict) and str(c.get('name') or '').strip() not in bad]
    if len(keep) == len(chars):
        continue
    shutil.copy2(f, backup / f.name)
    removed = [c.get('name') for c in chars if isinstance(c, dict) and str(c.get('name') or '').strip() in bad]
    d['characters'] = keep
    if keep:
        d['ai_name'] = str(keep[0].get('name') or '')
        d['ai_role'] = str(keep[0].get('role') or '')
    else:
        d['ai_name'] = ''
        d['ai_role'] = ''
    d['updated_at'] = int(time.time())
    f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
    changed += 1
    print('  %s 删掉 %s，剩 %s' % (f.name, removed, [c['name'] for c in keep]))
print('改了 %d 个文件，原件备份在 %s' % (changed, backup))