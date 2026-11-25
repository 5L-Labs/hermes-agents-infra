"""Disposable nginx integration test. No host ports or production containers are used.

Run with Python 3, OpenSSL and Docker or Podman. Optional overrides:
CONTAINER_ENGINE, TEST_NGINX_IMAGE, TEST_BACKEND_IMAGE.
"""
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import uuid

HELPER = 'rn=function(e){return`/`+e}'


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path == '/ws':
            accept = base64.b64encode(hashlib.sha1(
                (self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()
            ).digest()).decode()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
            return
        if self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'first\n')
            self.wfile.flush()
            time.sleep(1.5)
            self.wfile.write(b'last\n')
            self.wfile.flush()
            return
        if self.path.startswith('/assets/'):
            body = HELPER.encode()
            content_type = 'application/javascript'
        else:
            body = json.dumps({
                'port': self.server.server_port, 'path': self.path,
                'host': self.headers.get('Host'),
                'prefix': self.headers.get('X-Forwarded-Prefix'),
                'scheme': self.headers.get('X-Forwarded-Proto'),
                'forwarded_for': self.headers.get('X-Forwarded-For'),
            }).encode()
            content_type = 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve():
    for port in (9120, 9199, 8680):
        threading.Thread(target=ThreadingHTTPServer(('0.0.0.0', port), Backend).serve_forever,
                         daemon=True).start()
    threading.Event().wait()


def probe():
    def connect():
        return http.client.HTTPSConnection('wrapper', 9143, timeout=10,
                                           context=ssl._create_unverified_context())

    def request(path, headers=None):
        connection = connect()
        connection.request('GET', path, headers={'Host': 'hermes.example.com:9143', **(headers or {})})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    for _ in range(60):
        try:
            request('/')
            break
        except (OSError, http.client.HTTPException):
            time.sleep(0.5)
    else:
        raise RuntimeError('nginx did not become ready')

    status, headers, _ = request('/?keep=1')
    assert status == 302 and headers['Location'].endswith('/hermes-1/?keep=1')
    for instance in (1, 3, 9, 10, 99):
        status, headers, _ = request(f'/hermes-{instance}?keep=1')
        assert status == 308 and headers['Location'].endswith(f'/hermes-{instance}/?keep=1')
        status, _, body = request(f'/hermes-{instance}/api/probe?keep=1',
                                  {'X-Forwarded-For': 'spoofed', 'X-Forwarded-Proto': 'http'})
        result = json.loads(body)
        assert status == 200 and result['path'] == '/api/probe?keep=1'
        assert result['port'] == (9120 if instance in (1, 3) else 9199)
        assert result['prefix'] == f'/hermes-{instance}' and result['scheme'] == 'https'
        assert result['host'] == 'hermes.example.com:9143' and result['forwarded_for'] != 'spoofed'
        status, headers, _ = request(f'/hermes-{instance}/external-api?keep=1')
        assert status == 308 and headers['Location'].endswith(f'/hermes-{instance}/external-api/?keep=1')
        status, _, body = request(f'/hermes-{instance}/external-api/api/probe?keep=1')
        result = json.loads(body)
        assert status == 200 and result['port'] == 8680
        assert result['prefix'] == f'/hermes-{instance}/external-api'
        assert result['path'] == '/api/probe?keep=1'
    for path in ('/hermes-0/', '/hermes-01/', '/hermes-100/', '/hermes-1x/', '/api/health', '/assets/test.js'):
        assert request(path)[0] == 404, path
    # The reserved mount must not steal similarly named Webapp paths.
    result = json.loads(request('/hermes-1/external-apiary')[2])
    assert result['port'] == 9120 and result['path'] == '/external-apiary'

    key = base64.b64encode(b'0123456789abcdef').decode()
    for instance in (1, 3):
        status, headers, _ = request(f'/hermes-{instance}/ws', {
            'Upgrade': 'websocket', 'Connection': 'Upgrade',
            'Sec-WebSocket-Key': key, 'Sec-WebSocket-Version': '13',
        })
        expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
        assert status == 101 and headers['Sec-WebSocket-Accept'] == expected
        connection = connect()
        started = time.monotonic()
        connection.request('GET', f'/hermes-{instance}/stream')
        response = connection.getresponse()
        assert response.status == 200 and response.read(6) == b'first\n'
        assert time.monotonic() - started < 1.2, 'first stream chunk was buffered'
        assert response.read() == b'last\n'
        connection.close()
    asset = '/assets/react-vendor-R7IuHd60.js'
    assert request('/hermes-1' + asset)[2].decode() == HELPER
    assert request('/hermes-3' + asset)[2].decode() == HELPER
    assert 'window.__HERMES_BASE_PATH__' in request('/hermes-9' + asset)[2].decode()
    assert request('/hermes-9/assets/other.js')[2].decode() == HELPER
    assert request('/hermes-9/external-api' + asset)[2].decode() == HELPER
    print('PASS: canonical routing, redirects, API split, forwarding, WebSocket101, streaming, rollback filter')


def validate():
    root = Path(__file__).resolve().parents[1]
    engine = os.environ.get('CONTAINER_ENGINE') or shutil.which('podman') or shutil.which('docker')
    if not engine:
        raise SystemExit('Docker or Podman is required')
    tag = 'hermes-wrapper-test-' + uuid.uuid4().hex[:10]
    backend, wrapper = tag + '-backend', tag + '-nginx'

    def run(*args, **kwargs):
        return subprocess.run([engine, *args], check=True, text=True, **kwargs)

    try:
        with tempfile.TemporaryDirectory(prefix=tag) as temporary:
            folder = Path(temporary)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-subj', '/CN=wrapper', '-days', '1', '-keyout', str(folder / 'key.pem'),
                            '-out', str(folder / 'cert.pem')], check=True, capture_output=True)
            subprocess.run(['openssl', 'dhparam', '-dsaparam', '-out', str(folder / 'dhparams4096.pem'),
                            '2048'], check=True, capture_output=True)
            run('network', 'create', tag, stdout=subprocess.DEVNULL)
            network = json.loads(run('network', 'inspect', tag, capture_output=True).stdout)[0]
            gateway = (network['subnets'][0]['gateway'] if 'subnets' in network else
                       network['IPAM']['Config'][0]['Gateway'])
            # Only substitute the synthetic network's DNS; production is never contacted.
            config = (root / 'nginx.conf').read_text().replace('10.203.1.1 10.203.3.1', gateway)
            (folder / 'nginx.conf').write_text(config)
            aliases = [value for n in (1, 3, 9, 10, 99)
                       for value in ('--network-alias', f'hermes-{n:02}')]
            run('run', '-d', '--name', backend, '--network', tag, *aliases,
                '-v', f'{root / "tests"}:/tests:ro', '--entrypoint', 'python3',
                os.environ.get('TEST_BACKEND_IMAGE', 'docker.io/library/python:3-alpine'),
                '/tests/validate.py', 'serve', stdout=subprocess.DEVNULL)
            mounts = [value for name in ('cert.pem', 'key.pem', 'dhparams4096.pem')
                      for value in ('-v', f'{folder / name}:/{name}:ro')]
            run('run', '-d', '--name', wrapper, '--network', tag, '--network-alias', 'wrapper',
                '-v', f'{folder / "nginx.conf"}:/etc/nginx/conf.d/webui-nginx.conf:ro', *mounts,
                os.environ.get('TEST_NGINX_IMAGE', 'docker.io/library/nginx:alpine'),
                stdout=subprocess.DEVNULL)
            run('exec', wrapper, 'nginx', '-t')
            run('exec', backend, 'python3', '/tests/validate.py', 'probe')
    finally:
        for name in (wrapper, backend):
            subprocess.run([engine, 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run([engine, 'network', 'rm', tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    if len(sys.argv) == 1:
        validate()
    elif sys.argv[1] == 'serve':
        serve()
    elif sys.argv[1] == 'probe':
        probe()
