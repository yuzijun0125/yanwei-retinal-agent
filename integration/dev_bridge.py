"""Reuse the existing retrieval process while serving the integrated OCR tool."""
import os
from pathlib import Path
import sys
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '02_代码'))
import gateway
import review_release
from yanwei import integration_results, medical_records, mcp_tools, server

UPSTREAM = os.environ.get('EXISTING_RETRIEVAL', 'http://172.24.208.1:8235')


def call_tool(name, arguments):
    if name == 'read_medical_record':
        result = medical_records.read_tool(arguments)
    else:
        result = gateway.upstream_json(UPSTREAM, '/api/tool', data={'name': name, 'arguments': arguments}, method='POST')
    if name=='search_cases' and isinstance(result,dict):
        result=review_release.enrich(gateway.STATE,result)
    integration_results.save(arguments.get('request_id'), result)
    return result


mcp_tools.call_tool = call_tool


class Handler(gateway.Handler):
    _send = server.Handler._send
    _mcp_respond_single = server.Handler._mcp_respond_single
    _handle_mcp_streamable = server.Handler._handle_mcp_streamable

    def dispatch(self):
        path = urlsplit(self.path).path
        if path == '/mcp' and self.command == 'POST':
            return self._handle_mcp_streamable()
        if path == '/api/integration/result' and self.command == 'GET':
            request_id = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
            try:
                return self.json(integration_results.read(request_id))
            except ValueError:
                try:
                    return self.json(gateway.upstream_json(UPSTREAM, self.path))
                except ValueError:
                    return self.json({'ok': False, 'error': {'code': 'NOT_READY'}}, 404)
        length = int(self.headers.get('Content-Length') or 0)
        if length > 12 * 1024 * 1024:
            self.close_connection = True
            return self.json({'ok': False, 'error': {'code': 'TOO_LARGE'}}, 413)
        body = self.rfile.read(length) if length else b''
        return self.proxy(UPSTREAM, body)


if __name__ == '__main__':
    address = (os.environ.get('BRIDGE_HOST', '172.24.208.1'), 18135)
    print('Integrated retrieval bridge ready', flush=True)
    ThreadingHTTPServer(address, Handler).serve_forever()
