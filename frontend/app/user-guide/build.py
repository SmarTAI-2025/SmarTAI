#!/usr/bin/env python3
"""Build the public bilingual guide without embedding or duplicating screenshots."""
from pathlib import Path
import argparse
import json
import os
import runpy

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parent / 'public/guide/index.html'


def build():
    pages = {}
    for language in ('zh', 'en'):
        os.environ['MANUAL_LANG'] = language
        pages[language] = runpy.run_path(str(ROOT / 'build_language.py'))['page']
    data = json.dumps(pages, ensure_ascii=False).replace('</', '<\\/')
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="/favicon.svg" type="image/svg+xml"><title>SmarTAI · Teacher Manual / 教师使用手册</title></head><body><script>window.__manualPages=''' + data + ''';
window.__manualLanguage='zh';
try {
  const siteLanguage=localStorage.getItem('smartai_locale');
  window.__manualLanguage=siteLanguage ? (siteLanguage==='en-US'?'en':'zh') : (localStorage.getItem('smartai-manual-language')==='en'?'en':'zh');
} catch(e) {}
const requestedLanguage=new URLSearchParams(location.search).get('lang');
if(requestedLanguage==='zh'||requestedLanguage==='en')window.__manualLanguage=requestedLanguage;
function saveManualLanguage(){try{localStorage.setItem('smartai-manual-language',window.__manualLanguage);localStorage.setItem('smartai_locale',window.__manualLanguage==='en'?'en-US':'zh-CN')}catch(e){}}
saveManualLanguage();
window.switchManualLanguage=function(){
  window.__manualLanguage=window.__manualLanguage==='zh'?'en':'zh';
  saveManualLanguage();
  const url=new URL(location.href);url.searchParams.set('lang',window.__manualLanguage);location.replace(url.href);
};
setTimeout(()=>{document.open();document.write(window.__manualPages[window.__manualLanguage]);document.close()},0);
</script><noscript>请启用 JavaScript 阅读双语手册。Enable JavaScript to read this bilingual manual.</noscript></body></html>'''


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Fail if the published HTML differs from its editable source')
    args = parser.parse_args()
    rendered = build()
    if args.check:
        if not OUTPUT.is_file() or OUTPUT.read_text() != rendered:
            parser.exit(1, 'Guide is out of date. Run python user-guide/build.py.\n')
        print('Public guide matches its editable source; screenshot paths exist.')
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(rendered)
        print(f'Built {OUTPUT}: {len(rendered.encode()):,} bytes; screenshots stay external.')
