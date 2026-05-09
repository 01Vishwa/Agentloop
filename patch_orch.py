path = r'e:\Agentloop\backend\core\ds_star_orchestrator.py'
content = open(path, encoding='utf-8').read()
old = 'if n.lower().endswith((".png", ".jpg", ".jpeg", ".svg"))'
new = 'if n.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".html"))'
if old in content:
    content = content.replace(old, new, 1)
    open(path, 'w', encoding='utf-8').write(content)
    print('PATCHED OK')
else:
    idx = content.find('image_artifacts')
    print('NOT FOUND. Context:', repr(content[idx:idx+300]))
