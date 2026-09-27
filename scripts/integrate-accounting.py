from pathlib import Path
import json

p=Path('templates/workspace.html')
s=p.read_text(encoding='utf-8').replace('</head>', '<link rel="stylesheet" href="{% static \'accounting.css\' %}"></head>')
s=s.replace('</body>', '<script src="{% static \'accounting.js\' %}"></script></body>')
p.write_text(s,encoding='utf-8')
p=Path('package.json'); d=json.loads(p.read_text(encoding='utf-8'));d['scripts']['check']+=' && node --check static/accounting.js';d['scripts']['test:accounting']='node scripts/accounting-smoke.cjs';p.write_text(json.dumps(d,indent=2)+'\n',encoding='utf-8')
