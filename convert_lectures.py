#!/usr/bin/env python3
"""Convert a lecture PDF to compact, citation-ready text.
Usage: python convert_lectures.py lecture.pdf L6 > L6.txt   (stats go to stderr)
Marker per page/slide: §L6.<n>
Hybrid: prose from pdftotext default mode (separates table columns),
        code lines from -layout mode (keeps order and indentation)."""
import re, subprocess, sys, unicodedata, collections, tempfile, os, glob

BIDI = re.compile('[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]')
TASHKEEL = re.compile('[\u064b-\u065f\u0670\u0640]')
YEH_FIX = str.maketrans({'ی': 'ي', 'ې': 'ي', 'ھ': 'ه', 'ہ': 'ه', 'ک': 'ك'})
PUA = re.compile('[\uf000-\uf0ff]')
PUA_MAP = {'\uf0e0': '→', '\uf071': '•'}
AR = '\u0600-\u06ff'

def run(pdf, mode):
    flag = {'layout': ['-layout'], 'raw': ['-raw']}.get(mode, [])
    args = ['pdftotext'] + flag + [pdf, '-']
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    pg = out.split('\f')
    return pg[:-1] if pg and pg[-1].strip() == '' else pg

def norm(line):
    line = BIDI.sub('', line).replace('\u2212', '-').replace('\xa0', ' ')
    line = unicodedata.normalize('NFKC', line).translate(YEH_FIX)
    line = re.sub(r' ?\u064b ?ا', 'ا', line)          # "إذ ًا" -> "إذا"
    line = re.sub(r'ا ?\u064b', 'ا', line)             # "أيضا ً" -> "أيضا"
    line = TASHKEEL.sub('', line)
    line = PUA.sub(lambda m: PUA_MAP.get(m.group(), '-'), line)
    line = re.sub(f'(?<=[{AR}])(?=[A-Za-z])|(?<=[A-Za-z])(?=[{AR}])', ' ', line)  # glued scripts
    line = re.sub(rf' ([.،:])(?=[{AR}])', r'\1 ', line)                              # " .تتم" -> ". تتم"
    line = re.sub(rf'(?<=\d)(?=[{AR}])', ' ', line)              # "1لم" -> "1 لم"
    line = re.sub(r'^(\s*):(\d+)', r'\1\2:', line)             # ":1" -> "1:"
    line = re.sub(r' \.([A-Za-z])( |$)', r' \1.\2', line)      # " .T " -> " T. "
    line = re.sub(r'" "(\w+)', r'"\1"', line)                   # '" "AVL_Insert' -> '"AVL_Insert"'
    return line.rstrip()

CODE_RE = re.compile(r'[;{}#]|^\s*[;)>]*\s*(if|else|case|switch|int|bool|void|struct|char|cout|cin|typedef|const|do|while)\b|->|//')
def is_code(l):
    return bool(CODE_RE.search(l)) and not re.search(f'[{AR}]{{3}}', l)

def fix_code_punct(l):
    """Page-1 code is stored right-to-left: trailing ; ) } > appear at line start."""
    code, sep, comment = l.partition('//')
    m = re.match(r'^(\s*)([;)}>{(<]+)(.*)$', code)
    if m:
        lead, punct, rest = m.groups()
        rest = rest.rstrip()
        open_first = punct.startswith(('{', '(')) and not punct.startswith(('{}', '()'))
        code = f'{lead}{rest}{punct[::-1]}'
    code = re.sub(r'(?<=\S) {2,}', ' ', code).rstrip()
    return code + (' //' + comment.strip() if sep else '')

def dedupe_runs(lines, min_run=3):
    """Remove contiguous runs (>=min_run lines) that repeat earlier text on the same slide
    (PowerPoint animation builds export the same block several times)."""
    out, i = [], 0
    while i < len(lines):
        best = 0
        for k in range(min(len(lines) - i, len(out)), min_run - 1, -1):
            seg = lines[i:i + k]
            if any(out[j:j + k] == seg for j in range(len(out) - k + 1)):
                best = k; break
        if best: i += best
        else: out.append(lines[i]); i += 1
    return out

def ocr_page(pdf, n):
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(['pdftoppm', '-png', '-r', '200', '-f', str(n), '-l', str(n), pdf, d + '/p'], check=True)
        img = glob.glob(d + '/p*.png')[0]
        t = subprocess.run(['tesseract', img, '-', '--psm', '6'], capture_output=True, text=True).stdout
    return '\n'.join(l.rstrip() for l in t.splitlines() if l.strip())

def key(l):
    return ''.join(sorted(re.sub(r'\W', '', l)))

def is_powerpoint(pdf):
    info = subprocess.run(['pdfinfo', pdf], capture_output=True, text=True).stdout
    return 'PowerPoint' in info or 'Impress' in info or 'Keynote' in info

def main_slides(pdf, lid, OCR=False):
    """PowerPoint/slide exports: authored (content-stream) order keeps each text box together,
    so overlays and animation copies do not interleave. Indentation kept."""
    pages = [[norm(x) for x in p.splitlines()] for p in run(pdf, 'raw')]
    n = len(pages)
    def edges(p):
        ne = [x for x in p if x.strip()]
        return ne[:2] + ne[-2:]
    cnt = collections.Counter()
    for p in pages:
        for k in {key(x) for x in edges(p)}: cnt[k] += 1
    need = max(3, int(0.3 * n))
    repeated = {k for k, c in cnt.items() if c >= need and len(k) > 6}
    out, junk, low, arabic_pages = [], 0, [], []
    for i, p in enumerate(pages, 1):
        ne = [x for x in p if x.strip()]
        edge_ids = {id(x) for x in ne[:2] + ne[-2:]}
        body = []
        for l in p:
            s = l.strip()
            if not s: continue
            if (id(l) in edge_ids and key(s) in repeated) or re.fullmatch(r'\d{1,3}|[-+]|[-=_/ ]{6,}', s):
                junk += 1; continue
            if body and body[-1] == s and len(s) > 20:      # animation copy of the previous line
                continue
            body.append(l.rstrip() if l.startswith(' ') else s)
        body = dedupe_runs(body)
        if re.search(f'[{AR}]{{3}}', '\n'.join(body)): arabic_pages.append(i)
        text = '\n'.join(body)
        if len(text) < 120:
            low.append(i)
            if OCR:
                o = ocr_page(pdf, i)
                if o: text += ('\n' if text else '') + '[OCR, unverified]\n' + o
        out.append(f'§{lid}.{i}\n{text}')
    res = '\n'.join(out) + '\n'
    sys.stdout.write(res)
    print(f'mode=slides pages={n} chars={len(res)} utf8_bytes={len(res.encode())} '
          f'removed_header_or_mark_lines={junk} image_or_low_text_pages={low} '
          f'pages_with_arabic_check_text_layer={arabic_pages}', file=sys.stderr)

def main(pdf, lid, OCR=False):
    if is_powerpoint(pdf):
        return main_slides(pdf, lid, OCR)
    prose_pg = [[norm(x) for x in p.splitlines()] for p in run(pdf, 'default')]
    code_pg  = [[norm(x) for x in p.splitlines()] for p in run(pdf, 'layout')]
    n = max(len(prose_pg), len(code_pg))
    # repeated header/footer detection on PROSE lines only (never on code)
    cnt = collections.Counter()
    for p in prose_pg:
        for k in {key(x) for x in p if x.strip() and not is_code(x)}:
            cnt[k] += 1
    need = max(2, int(0.3 * n))
    repeated = {k for k, c in cnt.items() if c >= need}
    out, junk, low, arabic_pages = [], 0, [], []
    for i in range(n):
        body = []
        for l in prose_pg[i] if i < len(prose_pg) else []:
            s = l.strip()
            if not s or is_code(l): continue
            if key(s) in repeated or re.fullmatch(r'\d{1,3}|[-+]|[-=_/ ]{6,}', s):
                junk += 1; continue
            body.append(re.sub(r'\s+', ' ', s))
        raw_code = [l for l in (code_pg[i] if i < len(code_pg) else [])
                    if l.strip() and is_code(l) and not re.fullmatch(r'[-=_/ ]{6,}', l.strip())]
        rtl = sum(1 for l in raw_code if re.match(r'^\s*[;)>]', l)) >= 3
        code = [fix_code_punct(l) if rtl else re.sub(r'(?<=\S) {2,}', ' ', l).rstrip() for l in raw_code]
        if code:
            ind = min(len(c) - len(c.lstrip()) for c in code)
            code = [c[ind:] for c in code]
        body, code = dedupe_runs(body), dedupe_runs(code)
        if re.search(f'[{AR}]{{3}}', '\n'.join(body)): arabic_pages.append(i + 1)
        text = '\n'.join(body + (['```'] + code + ['```'] if code else []))
        if len(text) < 120:
            low.append(i + 1)
            if OCR:
                o = ocr_page(pdf, i + 1)
                if o: text += ('\n' if text else '') + '[OCR]\n' + o
        out.append(f'§{lid}.{i+1}\n{text}')
    res = '\n'.join(out) + '\n'
    sys.stdout.write(res)
    print(f'pages={n} chars={len(res)} utf8_bytes={len(res.encode())} '
          f'removed_header_or_mark_lines={junk} image_or_low_text_pages={low} '
          f'pages_with_arabic_check_text_layer={arabic_pages}', file=sys.stderr)

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], OCR='--ocr' in sys.argv)
