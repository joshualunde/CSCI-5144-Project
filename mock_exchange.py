"""Minimal *synthetic* exchange-like HTTP backend for local HPA smoke testing.

NOT DeathStarBench. NOT a real transaction-processing exchange. No persistence.
The stateless order ID endpoints are synthetic, to allow multi-replica HTTP load.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import time
import uuid

PORT = int(os.getenv('PORT', '8000'))
WORK_FACTOR = int(os.getenv('WORK_FACTOR', '12000'))


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        return  # avoid overwhelming logs under load

    def respond(self, code, payload):
        encoded = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def cpu_work(self):
        # Predictable CPU demand solely for demonstrating Kubernetes CPU autoscaling.
        hashlib.pbkdf2_hmac('sha256', b'team-jam', b'benchmark', WORK_FACTOR)

    def do_GET(self):
        if self.path == '/health':
            return self.respond(200, {'status': 'ok'})
        self.cpu_work()
        parts = self.path.split('?')[0].strip('/').split('/')
        if len(parts) == 3 and parts[:2] == ['api', 'quote']:
            symbol = parts[2]
            price = 75 + int.from_bytes(hashlib.sha256(symbol.encode()).digest()[:2], 'big') % 250
            return self.respond(200, {'symbol': symbol, 'price': float(price)})
        if len(parts) == 3 and parts[:2] == ['api', 'orderbook']:
            return self.respond(200, {'symbol': parts[2], 'bids': [], 'asks': []})
        if len(parts) == 3 and parts[:2] == ['api', 'orders']:
            return self.respond(200, {'order_id': parts[2], 'status': 'simulated'})
        return self.respond(404, {'error': 'not found'})

    def do_POST(self):
        if self.path != '/api/orders':
            return self.respond(404, {'error': 'not found'})
        try:
            content_len = int(self.headers.get('Content-Length', '0'))
            if content_len <= 0 or content_len > 65536:
                return self.respond(400, {'error': 'body required or too large'})
            body = json.loads(self.rfile.read(content_len))
            if not isinstance(body, dict) or not body.get('symbol') or not body.get('side'):
                return self.respond(400, {'error': 'invalid order'})
        except (ValueError, TypeError, json.JSONDecodeError):
            return self.respond(400, {'error': 'bad JSON'})
        self.cpu_work()
        return self.respond(200, {'order_id': uuid.uuid4().hex, 'status': 'simulated'})

    def do_DELETE(self):
        parts = self.path.split('?')[0].strip('/').split('/')
        if len(parts) != 3 or parts[:2] != ['api', 'orders']:
            return self.respond(404, {'error': 'not found'})
        self.cpu_work()
        return self.respond(200, {'order_id': parts[2], 'status': 'simulated cancellation'})


if __name__ == '__main__':
    print(f'Synthetic exchange benchmark listening on 0.0.0.0:{PORT} (work={WORK_FACTOR})', flush=True)
    ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
