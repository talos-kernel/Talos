"""Run inside a test Computer with its persistent browser and pinned Playwright.

Uses only a temporary loopback form. No real registration, email or paid CAPTCHA.
Usage: /opt/talos/browser-venv/bin/python tests/computer_browser_e2e.py
"""
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

sys.path.insert(0, "/opt/talos")
from browser import run
from playwright.sync_api import sync_playwright

FORM = """<!doctype html><meta charset=utf-8><title>Talos form test</title>
<style>body{font:18px sans-serif;padding:25px}label{display:block;margin:10px}iframe{height:90px}</style>
<h1>Local enrollment fixture</h1><form method=post action=/submit>
<label>Name <input id=name name=name required></label>
<label>Email <input id=email name=email type=email required></label>
<label>Course <select id=course name=course required onchange="setTimeout(()=>{day.disabled=false;day.innerHTML='<option value=mon>Monday</option><option value=tue>Tuesday</option>'},100)"><option value=''>Choose</option><option value=swim>Swimming</option></select></label>
<label>Day <select id=day name=day required disabled></select></label>
<label>Date <input id=date name=date required placeholder=DD/MM/YYYY onblur="if(!/^\\d{2}\\/\\d{2}\\/\\d{4}$/.test(this.value))this.value=''"></label>
<label>Private <input type=password value=fixture-private-value></label>
<label><input id=terms name=terms type=checkbox required>Terms</label>
<label>Note <textarea id=note name=note></textarea></label>
<input type=hidden name=trap value=''>
<button id=submit>Submit enrollment</button></form>
<script>if(location.search.includes('challenge'))document.write('<iframe title="Security challenge" src="/challenge"></iframe>');if(location.search.includes('fail'))document.querySelector('form').action='/fail';</script>
"""


def main():
    received=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            body = ("<button id=confirm onclick=\"parent.document.querySelector('iframe').remove()\">Test operator confirms</button>" if self.path.startswith('/challenge') else FORM)
            if self.path.startswith('/async'):
                body += """<p>""" + "Long page content. " * 250 + """</p><script>
                document.querySelector('form').addEventListener('submit',async e=>{
                  e.preventDefault();const f=e.target;
                  await fetch('http://localhost:'+location.port+'/telemetry',{method:'POST'});
                  const r=await fetch('/async-submit',{method:'POST',body:new URLSearchParams(new FormData(f))});
                  const receipt=await r.json();f.reset();
                  const toast=document.createElement('div');toast.className='toast-message';
                  toast.textContent=receipt.message;document.body.append(toast);
                });</script>"""
            self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.end_headers();self.wfile.write(body.encode())
        def do_POST(self):
            if self.path == '/telemetry':
                self.send_response(200);self.send_header('Access-Control-Allow-Origin','*');self.end_headers();self.wfile.write(b'ok');return
            values=parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode(),keep_blank_values=True)
            if self.path=='/fail':
                self.send_response(403);body='<h1>Submission rejected</h1>'
            else:
                assert values['trap']==['']
                assert values['course']==['swim'] and values['day']==['tue']
                assert values['date']==['12/05/2018'] and values['terms']==['on']
                received.append(values)
                self.send_response(200);body=f'<h1>Registration confirmed</h1><p id=receipt>receipt-{len(received)}</p>'
            if self.path == '/async-submit':
                time.sleep(0.8)
                body=json.dumps({'status':200,'message':'Delayed registration confirmed','access_token':'fixture-receipt-private'})
                self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(body.encode());return
            self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body.encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base=f'http://127.0.0.1:{server.server_port}'
    report={'cycles':[],'challenge':False,'rejected_submit':False}
    project='browser-e2e-fixture'
    with sync_playwright() as pw:
        browser=pw.chromium.connect_over_cdp('http://127.0.0.1:9222')
        context=browser.contexts[0];page=context.new_page();index=context.pages.index(page)
        sequence=0
        def action(kind,**extra):
            nonlocal sequence
            sequence+=1
            request={'op':'browser','project':project,'key':f'check-{sequence}',
                     'title':'Local browser E2E','action':kind,'page':index,**extra}
            executed=subprocess.run([sys.executable,'-B','/opt/talos/browser.py'],
                                    input=json.dumps(request),text=True,capture_output=True,timeout=60)
            assert executed.returncode==0,executed.stderr
            result=json.loads(executed.stdout)
            assert result.get('state')!='failed',result
            assert not result.get('page_errors'),result
            return result
        def fill():
            first=action('submit',selector='#submit')
            assert first['action_performed'] is False and first['reason']=='required fields are invalid'
            action('fill',selector='#name',value='Talos Test')
            action('fill',selector='#email',value='test@example.invalid')
            action('select',selector='#course',value='swim')
            action('select',selector='#day',value='tue')
            out=action('type',selector='#date',value='12/05/2018')
            assert out['observed_value']=='12/05/2018'
            action('check',selector='#terms',checked=True)
            action('fill',selector='#note',value='TECHNICAL TEST')
        try:
            for cycle in (1,2):
                out=action('navigate',url=base)
                assert 'fixture-private-value' not in json.dumps(out)
                fill()
                out=action('submit',selector='#submit')
                assert 'Registration confirmed' in out['page']['text']
                assert any(r['status']==200 and r['url'].endswith('/submit') for r in out['responses'])
                assert len(received)==cycle
                assert received[-1]['name']==['Talos Test']
                report['cycles'].append({'cycle':cycle,'http_status':200,'receipt':f'receipt-{cycle}','stored':received[-1]})
            action('navigate',url=base+'?challenge=1');fill()
            out=action('submit',selector='#submit')
            assert out['action_performed'] is False and 'challenge' in out['reason']
            assert len(received)==2
            # A controlled operator action, not a claim to solve a real CAPTCHA.
            page.frame_locator('iframe').locator('#confirm').click()
            out=action('inspect');assert not out['page']['challenge']['interactive']
            out=action('submit',selector='#submit');assert len(received)==3
            report['challenge']=True
            action('navigate',url=base+'?fail=1');fill()
            out=action('submit',selector='#submit')
            assert len(received)==3 and any(r['status']==403 for r in out['responses'])
            assert out['state']=='needs_review' and 'rejected' in out['page']['text'].lower()
            report['rejected_submit']=True
            action('navigate',url=base+'/async');fill()
            out=action('submit',selector='#submit')
            assert len(received)==4
            assert next(r for r in out['submissions'] if r['url'].endswith('/async-submit'))['receipt']['message']=='Delayed registration confirmed'
            assert 'Delayed registration confirmed' in out['page']['alerts']
            assert 'fixture-receipt-private' not in json.dumps(out)
            assert list(out).index('submissions') < list(out).index('page')
            report['delayed_ajax_receipt']=True
            report['passed']=True
            print(json.dumps(report),flush=True)
        finally:
            page.close();server.shutdown();server.server_close()


if __name__=='__main__':main()
