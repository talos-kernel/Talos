import json
import socket
import threading
import time
from talos import claudeworker as worker


def test_worker_uses_operator_binary_not_frame_or_global_fallback(monkeypatch):
    selected=[]
    monkeypatch.setenv('TALOS_CLAUDE_WORKER_BIN','/wrong/global')
    monkeypatch.setattr(worker,'make_spawn',lambda binary:selected.append(binary))
    jobs=worker._Jobs(claude_bin='/opt/worker/claude')
    worker.handle_frame(json.dumps({'op':'status','job_id':'absent','claude_bin':'/attacker'}).encode(),jobs)
    assert selected==['/opt/worker/claude']


def test_daemon_carries_binary_from_env_file_to_request_handler(tmp_path,monkeypatch):
    selected=[]
    monkeypatch.setattr(worker,'make_spawn',lambda binary:selected.append(binary))
    env=tmp_path/'worker.env';env.write_text(f'TALOS_CLAUDE_WORKER_HOME={tmp_path}/home\nTALOS_CLAUDE_WORKER_BIN=/opt/worker/private-cli\n')
    path=str(tmp_path/'worker.sock');stop=threading.Event()
    thread=threading.Thread(target=worker.serve,args=(path,str(env)),kwargs={'environ':{},'stop':stop},daemon=True)
    thread.start()
    try:
        deadline=time.monotonic()+3
        while not __import__('os').path.exists(path) and time.monotonic()<deadline:time.sleep(.01)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.connect(path);connection.sendall(b'{"op":"status","job_id":"absent"}\n')
            assert json.loads(connection.recv(4096))['ok'] is False
        assert selected==['/opt/worker/private-cli']
    finally:
        stop.set();thread.join(3)
