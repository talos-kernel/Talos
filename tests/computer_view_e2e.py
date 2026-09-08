"""Candidate assets + real private backend. Read-only view E2E; never takes control."""
from pathlib import Path
import sys,json,hashlib,hmac,secrets,time,re,argparse,shutil
from playwright.sync_api import sync_playwright,expect
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--config',type=Path,required=True,help='Private Computer configuration (read only)')
parser.add_argument('--evidence-dir',type=Path,required=True,help='Private screenshots and result folder; never publish without review')
parser.add_argument('--candidate-assets',action='store_true',help='Test this checkout over the installed backend before activation')
parser.add_argument('--browser',default=shutil.which('chromium') or shutil.which('google-chrome'))
args=parser.parse_args()
ROOT=Path(__file__).resolve().parents[1]
cfg=json.loads(args.config.read_text())
origin=cfg['origin']
payload=f"{int(time.time())+3600}.{secrets.token_hex(16)}"
cookie=payload+'.'+hmac.new(cfg['view_secret'].encode(),payload.encode(),hashlib.sha256).hexdigest()
evidence=args.evidence_dir;evidence.mkdir(parents=True,exist_ok=True,mode=0o700)
candidate=args.candidate_assets
results=[]
with sync_playwright() as pw:
 browser=pw.chromium.launch(executable_path=args.browser,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
 for run in range(2):
  context=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=1)
  context.add_cookies([{'name':'talos_computer','value':cookie,'url':origin,'secure':True,'httpOnly':True,'sameSite':'Strict'}])
  if candidate:
   for url,file,typ in [('/','index.html','text/html'),('/app.js','app.js','text/javascript'),('/style.css','style.css','text/css')]:
    content=(ROOT/'talos/computer/web'/file).read_text()
    context.route(origin+url,lambda route,request,content=content,typ=typ:route.fulfill(status=200,body=content,content_type=typ))
  page=context.new_page();errors=[];bad=[];posts=[]
  page.on('pageerror',lambda e:errors.append(str(e)))
  page.on('console',lambda m:errors.append(m.text) if m.type=='error' else None)
  page.on('response',lambda r:bad.append([r.status,r.url.split('?')[0]]) if r.status>=400 else None)
  page.on('request',lambda r:posts.append(r.url) if r.method=='POST' else None)
  page.goto(origin,wait_until='domcontentloaded')
  expect(page.locator('#workspace')).to_be_visible()
  expect(page.locator('#capture')).to_be_visible(timeout=30000)
  page.wait_for_function("document.getElementById('capture').naturalWidth>0")
  def scale():
   return page.locator('#capture').evaluate('e=>Math.min(e.clientWidth/e.naturalWidth,e.clientHeight/e.naturalHeight)')
  before=scale()
  page.screenshot(path=str(evidence/f'{"candidate" if candidate else "live"}-{run+1}-before.png'))
  page.locator('#expand').click()
  expect(page.locator('#expand')).to_have_attribute('aria-pressed','true')
  expect(page.locator('.grid>aside')).to_be_hidden()
  expanded=scale()
  assert expanded>before*1.20,(before,expanded)
  page.locator('#screen-zoom').select_option('150')
  page.wait_for_function("document.getElementById('screen').scrollWidth>=2160")
  page.locator('#screen').evaluate('e=>{e.scrollLeft=240;e.scrollTop=120}')
  assert page.locator('#screen').evaluate('e=>e.scrollLeft')==240
  assert page.locator('#screen').evaluate('e=>e.scrollTop')==120
  page.reload(wait_until='domcontentloaded')
  expect(page.locator('#workspace')).to_be_visible()
  expect(page.locator('#capture')).to_be_visible(timeout=30000)
  page.wait_for_function("document.getElementById('capture').naturalWidth>0")
  expect(page.locator('#expand')).to_have_attribute('aria-pressed','true')
  expect(page.locator('#screen-zoom')).to_have_value('150')
  page.locator('#screen-zoom').select_option('fit')
  page.locator('#fullscreen').click()
  page.wait_for_function("document.fullscreenElement?.id==='workspace'")
  expect(page.locator('#fullscreen')).to_have_attribute('aria-pressed','true')
  page.locator('#fullscreen').click()
  page.wait_for_function('!document.fullscreenElement')
  page.keyboard.press('Escape')
  expect(page.locator('#expand')).to_have_attribute('aria-pressed','false')
  expect(page.locator('.grid>aside')).to_be_visible()
  page.locator('#expand').click()
  page.screenshot(path=str(evidence/f'{"candidate" if candidate else "live"}-{run+1}-expanded.png'))
  page.set_viewport_size({'width':390,'height':844})
  for ident in ['expand','fullscreen','screen-zoom','takeover']:
   b=page.locator('#'+ident).bounding_box()
   assert b and b['x']>=0 and b['x']+b['width']<=391,(ident,b)
  assert page.evaluate('document.documentElement.scrollWidth')<=390
  page.locator('#screen-zoom').select_option('200')
  assert page.locator('#screen').evaluate('e=>e.scrollWidth')>=2880
  assert page.evaluate('document.documentElement.scrollWidth')<=390
  page.locator('#screen-zoom').select_option('fit')
  page.screenshot(path=str(evidence/f'{"candidate" if candidate else "live"}-{run+1}-mobile.png'))
  assert not posts,posts
  assert not errors,errors
  assert not bad,bad
  current=context.request.get(origin+'/api/status').json()
  assert current['control']=='agent',current['control']
  results.append({'run':run+1,'normal_scale':round(before,3),'expanded_scale':round(expanded,3),'gain':round(expanded/before,2),'zoom_scroll':True,'preferences_reload':True,'fullscreen':True,'escape_restore':True,'mobile_no_overflow':True,'control_unchanged':True,'page_errors':errors,'http_errors':bad,'mutation_requests':len(posts)})
  print(json.dumps(results[-1]),flush=True)
  context.close()
 browser.close()
(evidence/('candidate.json' if candidate else 'live.json')).write_text(json.dumps(results,indent=2))
