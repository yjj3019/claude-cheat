"""Isolated real HTTP dashboard; no user configuration or provider calls."""
import json
import sys
import routeDashboard as dash

dash.TOKEN = 'browser-fixture-token'
if sys.argv[1:2] == ['--launch']:
    # Real launcher/browser handshake; isolate OS browser opening and PID ownership.
    from unittest import mock
    import launcherStartup as startup
    port = int(sys.argv[2])
    with mock.patch.object(dash, 'DEFAULT_PORT', port), \
         mock.patch.object(dash, '_port_open', return_value=True), \
         mock.patch.object(dash, '_read_pid_record', return_value={'pid':1,'port':port}), \
         mock.patch.object(dash, '_verify_pid_record', return_value='verified'), \
         mock.patch.object(startup.webbrowser, 'open', side_effect=lambda url: print(json.dumps({'opened':url}),flush=True)):
        ok = startup.dashboard(setup=sys.argv[3:4] == ['setup'])
    sys.exit(0 if ok else 1)

class Handler(dash.Handler):
    def do_GET(self):
        path = self.path.split('?')[0]
        if path.startswith('/api/'):
            data = {'local': {'fields': {'LOCAL_LLM_MODEL': {'value': 'saved-model', 'set': True}}},
                    'openrouter': {'fields': {}}, 'cli': {'fields': {}}, 'default_provider': 'local'} if path == '/api/config' else {}
            if path == '/api/snapshot':
                data = {'generated_at':'2026-11-16T00:00:00Z', 'model':'running-model','next_model':'saved-model',
                        'configuration_pending':True, 'routing':{}, 'usage':{},'sessions':[], 'providers':{},
                        'startup_history':[{'code':'port_in_use','stage':'bind','provider':'local','exit_code':1}],
                        'proxy_metrics':{'localAdmission':{'active':1,'queued':2,'rejected':0}}}
            return self._send(200,'application/json',json.dumps(data))
        super().do_GET()
    def do_POST(self):
        if self.path != '/api/browser':
            return self._send(404,'application/json','{}')
        super().do_POST()

server = dash.DashboardServer(('127.0.0.1', int(sys.argv[1]) if len(sys.argv)>1 else 0), Handler)
print(server.server_address[1], flush=True)
server.serve_forever()
