#!/usr/bin/env python3
"""Render one language of the public user guide from editable content."""
from pathlib import Path
import json, html, os
ROOT=Path(__file__).resolve().parent
PUBLIC=ROOT.parent/'public/guide'
LANG=os.environ.get('MANUAL_LANG','zh')
SUFFIX='.en' if LANG=='en' else ''
esc=lambda x:html.escape(str(x),quote=True)
chapters=json.loads((ROOT/f'content/chapters{SUFFIX}.json').read_text())
images=json.loads((ROOT/f'content/images{SUFFIX}.json').read_text()) if (ROOT/f'content/images{SUFFIX}.json').exists() else []
for image in images:
    if not (PUBLIC/image['src']).is_file(): raise FileNotFoundError(image['src'])

def figure(im):
    return f'''<figure class="figure" style="max-width:{int(im.get('maxWidth', 2000))}px"><button type="button" class="zoom-image" aria-label="放大查看：{esc(im['caption'])}"><img src="{esc(im['src'])}" alt="{esc(im.get('alt',im['caption']))}" width="{im.get('width', 1165)}" height="{im.get('height', 750)}" loading="lazy" decoding="async"></button><figcaption><span>图 {esc(im['figureNumber'])}</span><span>{esc(im['caption'])}</span><small>点击放大</small></figcaption></figure>'''

def numbering(c):
    if c.get('group','manual')=='manual':
        return str([x for x in chapters if x.get('group','manual')=='manual'].index(c)+1)
    if c['id']=='api-smartai': return '1'
    if c['id']=='api-providers': return '2'
    if c['id']=='api-relay': return '3'
    return '2.'+str([x for x in chapters if x.get('group')=='api' and x['id'] not in ['api-smartai','api-providers','api-relay']].index(c)+1)

# Figure labels follow their containing second-level section, in top-to-bottom order.
figure_counts={}
for im in images:
    owner=next(c for c in chapters if c['id']==im['chapter'])
    prefix=numbering(owner)+'.'+str(im.get('afterSection',0)+1)
    key=(im['chapter'],im.get('afterSection',0))
    index=figure_counts.get(key,0);figure_counts[key]=index+1
    im['figureNumber']=prefix+chr(ord('a')+index)

def chapter_markup(c,i):
    group=c.get('group','manual'); group_name='使用手册' if group=='manual' else '模型与 API 指南'
    siblings=[x for x in chapters if x.get('group','manual')==group]; local=siblings.index(c)
    num=numbering(c)
    cs=[im for im in images if im['chapter']==c['id']]
    body=''.join(figure(im) for im in cs if im.get('afterSection',0)==-1)
    for j,s in enumerate(c['sections']):
        sid=s.get('id') or f"{c['id']}-{j+1}"
        detail=s.get('details') or []
        if isinstance(detail,dict):detail=[detail]
        if isinstance(detail,str):detail=[{'title':'展开了解更多','body':detail}]
        ds=''.join(f'<details><summary>{esc(d["title"])}</summary><div>{d["body"]}</div></details>' for d in detail)
        pics=''.join(figure(im) for im in cs if im.get('afterSection',0)==j)
        section_title=(num+"."+str(j+1)+" "+s["title"]) if group=="manual" else s["title"]
        body+=f'<section id="{esc(sid)}"><h2>{esc(section_title)}</h2>{s["body"]}{pics}{ds}</section>'
    prev=f'<a href="#{siblings[local-1]["id"]}"><small>上一章</small>{esc(siblings[local-1]["title"])}</a>' if local else ''
    nxt=f'<a href="#{siblings[local+1]["id"]}"><small>下一章</small>{esc(siblings[local+1]["title"])}</a>' if local+1<len(siblings) else ''
    return f'''<article class="chapter" id="{esc(c['id'])}" data-group="{group}" data-title="{esc(c['title'])}" data-number="{num}" tabindex="-1"><p class="breadcrumb">{group_name}<span>/</span>{("2 厂商与 API 申请<span>/</span>" if num.startswith("2.") and group=="api" else "")}{num} {esc(c['title'])}</p><header class="chapter-heading"><div class="chapter-number">{("第 "+num+" 章") if "." not in num else ("厂商指南 · "+num)}</div><h1>{num} {esc(c['title'])}</h1><p class="intro">{esc(c['intro'])}</p></header>{body}<nav class="page-nav" aria-label="章节翻页">{prev}{nxt}</nav><footer class="page-foot"><span>SmarTAI · {group_name}</span><a href="#{esc(c['id'])}">回到本章开头 ↑</a></footer></article>'''

def nav_link(c):
    return f'<a href="#{esc(c["id"])}"><span class="num">{numbering(c)}</span><span>{esc(c["title"])}</span></a>'
nav=''
for group in ['manual','api']:
    members=[c for c in chapters if c.get('group','manual')==group]
    nav+=f'<ol class="chapter-nav" data-group="{group}"'+(' hidden' if group=='api' else '')+'>'
    for c in members:
        if group=='api' and numbering(c).startswith('2.'): continue
        nav+=f'<li data-chapter="{c["id"]}">'+nav_link(c)
        if group=='manual':
            nav+=f'<ol class="section-nav" data-parent="{c["id"]}" hidden>'
            for j,section in enumerate(c['sections']):
                sid=section.get('id') or f"{c['id']}-{j+1}"
                nav+=f'<li><a href="#{sid}"><span class="num">{numbering(c)}.{j+1}</span><span>{esc(section["title"])}</span></a></li>'
            nav+='</ol>'
        elif c['id']=='api-providers':
            nav+='<ol class="provider-nav">'+''.join('<li>'+nav_link(x)+'</li>' for x in members if numbering(x).startswith('2.'))+'</ol>'
        nav+='</li>'
    nav+='</ol>'

css=(ROOT/'manual.css').read_text();js=(ROOT/'manual.js').read_text()
page=f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="SmarTAI 教师使用手册：从新建任务、题目校对到批改、复核与成绩报告。"><meta name="color-scheme" content="light"><title>SmarTAI · 教师使用手册</title><style>{css}</style></head><body>
<a class="skip" href="#main">跳转到正文</a>
<header class="topbar"><div class="topbar-inner"><a class="brand" href="/" aria-label="SmarTAI 首页"><img class="brand-image" src="/brand/smartai-wordmark-blue.svg" alt="SmarTAI" width="176" height="40"></a><nav class="book-tabs" aria-label="手册分类"><a href="#quickstart" data-book="manual" aria-current="page">使用手册</a><a href="#api-smartai" data-book="api">模型与 API 指南</a></nav><div class="tools"><button type="button" class="language-toggle" id="language-toggle" aria-label="切换语言"><span data-lang="zh">CN</span><span class="language-divider">/</span><span data-lang="en">EN</span></button><a class="entry-link" href="/login">注册/登录</a><a class="entry-link" href="/frontier/enter">进入真实 Demo <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-6-6 6 6-6 6"/></svg></a></div><button class="mobile-menu" id="menu" aria-controls="sidebar" aria-expanded="false" aria-label="打开目录"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg></button></div></header>
<aside class="sidebar" id="sidebar"><div class="search-wrap"><svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/></svg><label class="sr-only" for="search" hidden>搜索手册</label><input class="search" id="search" type="search" placeholder="搜索手册、厂商或问题" aria-label="搜索手册" autocomplete="off"></div><p class="toc-label" id="toc-label">使用手册 · 目录</p><nav aria-label="手册章节">{nav}</nav><p class="sidebar-foot">按章节阅读，或跨页签搜索。</p></aside><div class="shade" id="shade"></div>
<main class="layout" id="main"><div class="reading-layout"><div class="article">{''.join(chapter_markup(c,i) for i,c in enumerate(chapters))}<div class="search-results" id="search-results" hidden aria-live="polite"></div></div><aside class="page-outline" aria-label="本章内容"><div class="outline-title">本章内容</div><nav id="outline"></nav></aside></div></main>
<dialog class="lightbox" id="lightbox"><div class="lightbox-head"><p id="lightbox-caption"></p><button class="lightbox-close" id="lightbox-close" type="button">关闭 · Esc</button></div><div class="lightbox-image-wrap"><img id="lightbox-image" alt=""></div></dialog>
<script>{js}</script></body></html>'''
if LANG=='en':
    replacements=json.loads((ROOT/'content/ui.en.json').read_text())
    for source,target in sorted(replacements.items(),key=lambda x:len(x[0]),reverse=True): page=page.replace(source,target)
    page=page.replace('<html lang="zh-CN">','<html lang="en">')
page=page.replace('<body>','<body data-language="'+LANG+'">')
