#!/usr/bin/env python3
"""sheetkit - one tabbed Google Sheet per day, via the Drive connector.
  load   <saved_download.txt|.json> state.json  : xlsx export (download_file_content JSON) -> state
  xlsx   state.json out                         : state -> out.xlsx + out.b64 (base64 in 1000-char lines) = the MAIN upload:
                                                  create_file base64Content = the lines joined with no newlines,
                                                  contentMimeType application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
  build  state.json out.xml                     : FALLBACK only - Excel-2003 XML text (create_file textContent,
                                                  contentMimeType application/vnd.ms-excel); about 3x bigger
  verify <saved_download.txt> state.json        : compare an uploaded sheet with the state, cell by cell
  stats  state.json                             : tab names, row counts, XML size
  sections <saved_download.txt> state.json      : OLD single-tab sheet with "## SHEET: <name> ##" rows -> state
state.json = {"tabs": [{"name": "Jobs", "rows": [["Title","Company",...], [...], ...]}, ...]}  (row 0 = header)
"""
import sys, json, base64, io, re
from xml.sax.saxutils import escape

BAD = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')

def clean(v):
    v = '' if v is None else str(v)
    return BAD.sub(' ', v).replace('\r\n', '\n').replace('\r', '\n')

def norm_state(st):
    out = []
    seen = set()
    for t in st['tabs']:
        name = re.sub(r'[\[\]\*\?/\\:]', '-', str(t['name']))[:99]
        assert name not in seen, 'duplicate tab ' + name
        seen.add(name)
        rows = [[clean(c) for c in r] for r in t.get('rows', [])]
        for r in rows:
            while r and r[-1] == '':
                r.pop()
        out.append({'name': name, 'rows': rows})
    return {'tabs': out}

def build(st):
    st = norm_state(st)
    x = ['<?xml version="1.0"?>\n<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
         'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">\n'
         '<Styles><Style ss:ID="h"><Font ss:Bold="1"/></Style></Styles>\n']
    for t in st['tabs']:
        x.append('<Worksheet ss:Name="%s"><Table>\n' % escape(t['name'], {'"': '&quot;'}))
        for i, r in enumerate(t['rows']):
            open_ = '<Cell ss:StyleID="h"><Data ss:Type="String">' if i == 0 else '<Cell><Data>'
            close = '</Data></Cell>'
            cells = []
            skip = False
            for j, v in enumerate(r):
                if v == '':
                    skip = True
                    continue
                o = open_
                if skip:  # keep column position after blank cells
                    o = o.replace('<Cell', '<Cell ss:Index="%d"' % (j + 1), 1)
                    skip = False
                cells.append(o + escape(v).replace('\n', '&#10;') + close)
            x.append('<Row>' + ''.join(cells) + '</Row>\n')
        x.append('</Table></Worksheet>\n')
    x.append('</Workbook>\n')
    return ''.join(x)

def to_xlsx(st, path):
    import openpyxl
    from openpyxl.styles import Font
    st = norm_state(st)
    wb = openpyxl.Workbook(); wb.remove(wb.active)
    for t in st['tabs']:
        ws = wb.create_sheet(t['name'])
        for r in t['rows']:
            ws.append(r)
        for i, r in enumerate(ws.iter_rows()):
            for c in r:
                c.number_format = '@'
                if i == 0:
                    c.font = Font(bold=True)
        if t['rows']:
            ws.freeze_panes = 'A2'
            for j in range(len(t['rows'][0])):
                w = max([len(str(r[j])) if j < len(r) else 0 for r in t['rows'][:60]] + [8])
                ws.column_dimensions[openpyxl.utils.get_column_letter(j + 1)].width = min(60, w + 2)
    wb.save(path)

def load_download(path):
    raw = open(path, encoding='utf-8').read().strip()
    try:
        d = json.loads(raw)
        b64 = d['content'] if isinstance(d, dict) else d
    except Exception:
        b64 = raw
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(b64)), read_only=True)
    tabs = []
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(values_only=True):
            r = [clean(c) for c in r]
            while r and r[-1] == '':
                r.pop()
            rows.append(r)
        while rows and not rows[-1]:
            rows.pop()
        tabs.append({'name': ws.title, 'rows': rows})
    return {'tabs': tabs}

def cmp(a, b):
    a, b = norm_state(a), norm_state(b)
    errs = []
    an = [t['name'] for t in a['tabs']]; bn = [t['name'] for t in b['tabs']]
    if an != bn:
        errs.append('tabs differ: uploaded %s vs expected %s' % (an, bn))
    bt = {t['name']: t['rows'] for t in b['tabs']}
    for t in a['tabs']:
        exp = [r for r in bt.get(t['name'], [])]
        got = t['rows']
        while exp and not exp[-1]: exp.pop()
        if len(got) != len(exp):
            errs.append('%s: %d rows uploaded vs %d expected' % (t['name'], len(got), len(exp)))
        for i, (g, e) in enumerate(zip(got, exp)):
            if [c.strip() for c in g] != [c.strip() for c in e]:
                errs.append('%s row %d differs: %r vs %r' % (t['name'], i + 1, g[:4], e[:4]))
                break
    return errs

if __name__ == '__main__':
    cmd = sys.argv[1]
    if cmd == 'build':
        st = json.load(open(sys.argv[2], encoding='utf-8'))
        xml = build(st)
        open(sys.argv[3], 'w', encoding='utf-8').write(xml)
        print('built', sys.argv[3], len(xml), 'chars,', ', '.join('%s=%d' % (t['name'], max(0, len(t['rows']) - 1)) for t in norm_state(st)['tabs']))
    elif cmd == 'xlsx':
        st = json.load(open(sys.argv[2], encoding='utf-8'))
        to_xlsx(st, sys.argv[3] + '.xlsx')
        b = base64.b64encode(open(sys.argv[3] + '.xlsx', 'rb').read()).decode()
        open(sys.argv[3] + '.b64', 'w').write('\n'.join(b[i:i + 1000] for i in range(0, len(b), 1000)) + '\n')
        print('built %s.xlsx; base64 %d chars in %d lines of %s.b64;' % (sys.argv[3], len(b), (len(b) + 999) // 1000, sys.argv[3]),
              ', '.join('%s=%d' % (t['name'], max(0, len(t['rows']) - 1)) for t in norm_state(st)['tabs']))
    elif cmd == 'load':
        st = load_download(sys.argv[2])
        json.dump(st, open(sys.argv[3], 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
        print('loaded', ', '.join('%s=%d' % (t['name'], max(0, len(t['rows']) - 1)) for t in st['tabs']))
    elif cmd == 'verify':
        got = load_download(sys.argv[2]); exp = json.load(open(sys.argv[3], encoding='utf-8'))
        e = cmp(got, exp)
        print('VERIFY PASS' if not e else 'VERIFY FAIL\n  ' + '\n  '.join(e[:10]))
        sys.exit(1 if e else 0)
    elif cmd == 'sections':
        src = load_download(sys.argv[2]); tabs = []; cur = None
        for t in src['tabs']:
            for r in t['rows']:
                m = re.match(r'^##\s*SHEET:\s*(.+?)\s*##$', (r[0] if r else '').strip())
                if m:
                    cur = {'name': m.group(1), 'rows': []}; tabs.append(cur)
                elif cur is not None and any(c.strip() for c in r):
                    cur['rows'].append(r)
        json.dump({'tabs': tabs}, open(sys.argv[3], 'w', encoding='utf-8'), ensure_ascii=False, indent=0)
        print('sections', ', '.join('%s=%d' % (t['name'], max(0, len(t['rows']) - 1)) for t in tabs))
    elif cmd == 'stats':
        st = norm_state(json.load(open(sys.argv[2], encoding='utf-8')))
        print(', '.join('%s=%d' % (t['name'], max(0, len(t['rows']) - 1)) for t in st['tabs']), '| xml', len(build(st)))
    else:
        sys.exit(__doc__)
