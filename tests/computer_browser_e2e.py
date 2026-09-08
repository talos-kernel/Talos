"""Run inside a test Computer with its persistent browser and pinned Playwright.

Uses only a temporary loopback form. No real registration, email or paid CAPTCHA.
Usage: /opt/talos/browser-venv/bin/python tests/computer_browser_e2e.py
"""
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

sys.path.insert(0, "/opt/talos")
from playwright.sync_api import sync_playwright

DRIVER = os.environ.get('TALOS_BROWSER_TEST_DRIVER', '/opt/talos/browser.py')

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
    storage = tempfile.TemporaryDirectory(prefix='talos-form-receipts-')
    ledger = Path(storage.name) / 'submissions.json'
    ledger.write_text('[]')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            body = ("<button id=confirm onclick=\"parent.document.querySelector('iframe').remove()\">Test operator confirms</button>" if self.path.startswith('/challenge') else FORM)
            if self.path in ('/contact', '/cancel'):
                field = ('<label>Message <textarea id=message name=message required></textarea></label>'
                         if self.path == '/contact' else
                         '<label>Registration <input id=registration name=registration required></label>')
                body = ('<!doctype html><meta charset=utf-8><title>Local form fixture</title>'
                        f'<h1>{self.path[1:]} fixture</h1><form method=post action={self.path}-submit>'
                        '<label>Email <input id=email name=email type=email required></label>'
                        + field + '<button id=submit>Send test form</button></form>')
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
            if self.path in ('/contact-submit', '/cancel-submit'):
                assert values['email'] == ['test@example.invalid']
                key = 'message' if self.path == '/contact-submit' else 'registration'
                assert values[key] == ['TECHNICAL TEST' if key == 'message' else 'receipt-1']
                received.append({'kind': self.path, **values})
                ledger.write_text(json.dumps(received))
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers()
                self.wfile.write(f'<h1>{key} confirmed</h1><p id=receipt>receipt-{len(received)}</p>'.encode())
                return
            if self.path=='/fail':
                self.send_response(403);body='<h1>Submission rejected</h1>'
            else:
                assert values['trap']==['']
                assert values['course']==['swim'] and values['day']==['tue']
                assert values['date']==['12/05/2018'] and values['terms']==['on']
                received.append(values)
                ledger.write_text(json.dumps(received))
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
        context=browser.contexts[0];page=context.new_page()
        session=context.new_cdp_session(page)
        target_id=session.send('Target.getTargetInfo')['targetInfo']['targetId'];session.detach()
        errors, failed_requests, bad_responses = [], [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append({'text': message.text, 'location': message.location})
                if message.type == 'error' and not (message.location.get('url') == base+'/fail'
                and '403' in message.text) else None)
        page.on('requestfailed', lambda request: failed_requests.append(request.url))
        page.on('response', lambda response: bad_responses.append([response.status, response.url])
                if response.status >= 400 and not (response.status == 403 and response.url == base+'/fail') else None)
        sequence=0
        def action(kind,**extra):
            nonlocal sequence
            sequence+=1
            request={'op':'browser','project':project,'key':f'check-{sequence}',
                     'title':'Local browser E2E','action':kind,'tab':target_id,**extra}
            executed=subprocess.run([sys.executable,'-B',DRIVER],
                                    input=json.dumps(request),text=True,capture_output=True,timeout=60)
            assert executed.returncode==0,executed.stderr
            result=json.loads(executed.stdout)
            assert result.get('state')!='failed',result
            assert result.get('tab')==target_id,result
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
                assert json.loads(ledger.read_text()) == received
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
            for form, field, value in [('contact', 'message', 'TECHNICAL TEST'),
                                        ('cancel', 'registration', 'receipt-1')]:
                action('navigate',url=base+'/'+form)
                before = len(received)
                invalid = action('submit',selector='#submit')
                assert invalid['action_performed'] is False and len(received) == before
                action('fill',selector='#email',value='test@example.invalid')
                action('fill',selector='#'+field,value=value)
                out=action('submit',selector='#submit')
                assert f'{field} confirmed' in out['page']['text']
                assert any(r['status']==200 and r['url']==base+'/'+form+'-submit' for r in out['responses'])
                assert len(received)==before+1 and json.loads(ledger.read_text())==received
                report[form]={'confirmed':True,'stored_once':True,'receipt':f'receipt-{len(received)}'}
            assert not errors, errors
            assert not failed_requests, failed_requests
            assert not bad_responses, bad_responses
            report['browser_health']={'page_console_errors':0,'failed_requests':0,'unexpected_http_errors':0}
            report['durable_readback']=json.loads(ledger.read_text())==received
            report['stable_tab_across_connections']=True
            report['passed']=True
            print(json.dumps(report),flush=True)
        finally:
            page.close();server.shutdown();server.server_close()
            storage.cleanup()


if __name__=='__main__':main()
