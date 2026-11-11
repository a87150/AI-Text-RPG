# -*- coding: utf-8 -*-
import pathlib

# backup.js \u7684\u88f8 fetch \u8865\u5934
p = pathlib.Path('static/js/backup.js')
s = p.read_text(encoding='utf-8')
o = "const resp = await fetch('/api/backups', { method: 'POST' });"
n = "// \u5199\u63a5\u53e3\u53ea\u6536 application/json\uff0c\u8fd9\u91cc\u6ca1\u8d70 api() \u5c31\u5f97\u81ea\u5df1\u5e26\u4e0a\n    const resp = await fetch('/api/backups', {\n      method: 'POST',\n      headers: { 'Content-Type': 'application/json' },\n      body: '{}',\n    });"
assert o in s, 'backup fetch'
p.write_text(s.replace(o, n, 1), encoding='utf-8', newline='')
print('backup.js ok')
